from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import goalflight_usage as usage  # noqa: E402


@pytest.fixture
def new_york_tz():
    """Pin the render-local clock so timestamp assertions are host-independent."""
    import os
    import time

    if not hasattr(time, "tzset"):
        pytest.skip("tzset unavailable on this platform")
    previous = os.environ.get("TZ")
    os.environ["TZ"] = "America/New_York"
    time.tzset()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time.tzset()


def _write_reader(directory: Path, filename: str, body: str) -> None:
    (directory / filename).write_text(body, encoding="utf-8")


def _kimi_usage_row(usage_obj: dict) -> dict:
    spec = usage.ReaderSpec("kimi", "moonshot", "kimi_usage.py")
    return usage.normalize_payload(
        spec,
        [
            {
                "label": "kimi-code",
                "source": "kimi_code_usages",
                "ok": True,
                "usage": usage_obj,
            }
        ],
    )[0]


def test_normalizes_codex_epoch_and_walled_state():
    spec = usage.ReaderSpec("codex", "codex", "codex_usage.py")
    rows = usage.normalize_payload(
        spec,
        [
            {
                "seat": "seat-a",
                "used_percent": 64,
                "reset_at": 2_000_000_000,
                "ok": True,
            },
            {
                "seat": "seat-b",
                "used_percent": 100,
                "reset_at": 2_000_000_100,
                "ok": True,
            },
        ],
    )

    assert rows[0] == {
        "provider": "codex",
        "account": "seat-a",
        "remaining": "36%",
        "reset_at": 2_000_000_000.0,
        "flags": [],
    }
    assert rows[1]["remaining"] == "0%"
    assert rows[1]["flags"] == ["walled"]


def test_normalizes_kimi_nested_usage_and_iso_reset():
    # The ext reader's payload contract (key/label/source) keeps the kimi
    # product names; the DISPLAY provider maps to the moonshot handle.
    spec = usage.ReaderSpec("kimi", "moonshot", "kimi_usage.py")
    reset_iso = "2033-05-18T03:33:20Z"
    row = usage.normalize_payload(
        spec,
        [
            {
                "label": "kimi-code",
                "source": "kimi_code_usages",
                "ok": True,
                "usage": {
                    "remaining": 66,
                    "limit": 100,
                    "resetTime": reset_iso,
                    "windows": [],
                },
            }
        ],
    )[0]

    assert row["provider"] == "moonshot"
    assert row["account"] is None
    assert row["remaining"] == "66/100"
    assert row["reset_at"] == datetime.fromisoformat(
        reset_iso.replace("Z", "+00:00")
    ).timestamp()
    assert row["flags"] == []


def test_exhausted_rate_window_is_the_binding_reading(new_york_tz):
    """Cycle headroom must not hide a spent rate window (live 2026-08-18).

    The cycle still had 65/100; the 5h window was 0/100. The window is what
    the API enforces, so the row must read as walled on the window's reset.
    """
    cycle_reset = "2026-08-19T04:47:00Z"
    window_reset = "2026-08-18T16:47:56Z"
    row = _kimi_usage_row(
        {
            "remaining": 65,
            "limit": 100,
            "resetTime": cycle_reset,
            "windows": [
                {
                    "duration": 300,
                    "timeUnit": "TIME_UNIT_MINUTE",
                    "remaining": 0,
                    "limit": 100,
                    "resetTime": window_reset,
                }
            ],
        }
    )

    window_ts = datetime.fromisoformat("2026-08-18T16:47:56+00:00").timestamp()
    assert row["remaining"].startswith("0/100")
    assert "5h window" in row["remaining"]
    assert "12:47" in row["remaining"]
    assert "65/100" not in row["remaining"]
    assert row["reset_at"] == window_ts
    assert row["flags"] == ["walled"]

    now = datetime.fromisoformat("2026-08-18T16:29:00+00:00").timestamp()
    rendered = usage.render_table([row], now=now)
    assert "0/100" in rendered
    assert "5h window" in rendered
    assert "12:47" in rendered
    assert "⛔wall" in rendered
    assert "Aug 19" not in rendered


def test_exhausted_cycle_still_walls_when_window_is_fresh():
    cycle_reset = "2026-08-19T04:47:00Z"
    window_reset = "2026-08-18T16:47:56Z"
    row = _kimi_usage_row(
        {
            "remaining": 0,
            "limit": 100,
            "resetTime": cycle_reset,
            "windows": [
                {
                    "duration": 300,
                    "timeUnit": "TIME_UNIT_MINUTE",
                    "remaining": 80,
                    "limit": 100,
                    "resetTime": window_reset,
                }
            ],
        }
    )

    cycle_ts = datetime.fromisoformat("2026-08-19T04:47:00+00:00").timestamp()
    assert row["remaining"] == "0/100"
    assert "window" not in row["remaining"]
    assert row["reset_at"] == cycle_ts
    assert row["flags"] == ["walled"]


def test_neither_exhausted_uses_tighter_constraint_and_its_reset():
    later = "2026-08-19T04:47:00Z"
    sooner = "2026-08-18T16:47:56Z"
    later_ts = datetime.fromisoformat("2026-08-19T04:47:00+00:00").timestamp()
    sooner_ts = datetime.fromisoformat("2026-08-18T16:47:56+00:00").timestamp()

    cycle_tighter = _kimi_usage_row(
        {
            "remaining": 50,
            "limit": 100,
            "resetTime": later,
            "windows": [
                {
                    "duration": 300,
                    "timeUnit": "TIME_UNIT_MINUTE",
                    "remaining": 80,
                    "limit": 100,
                    "resetTime": sooner,
                }
            ],
        }
    )
    assert cycle_tighter["remaining"] == "50/100"
    assert cycle_tighter["reset_at"] == later_ts
    assert cycle_tighter["flags"] == []

    window_tighter = _kimi_usage_row(
        {
            "remaining": 80,
            "limit": 100,
            "resetTime": later,
            "windows": [
                {
                    "duration": 300,
                    "timeUnit": "TIME_UNIT_MINUTE",
                    "remaining": 50,
                    "limit": 100,
                    "resetTime": sooner,
                }
            ],
        }
    )
    assert window_tighter["remaining"].startswith("50/100")
    assert "5h window" in window_tighter["remaining"]
    assert window_tighter["reset_at"] == sooner_ts
    assert window_tighter["flags"] == []


def test_binding_constraint_is_the_minimum_across_every_window():
    row = _kimi_usage_row(
        {
            "remaining": 40,
            "limit": 100,
            "resetTime": "2026-08-19T04:47:00Z",
            "windows": [
                {
                    "duration": 5,
                    "timeUnit": "TIME_UNIT_MINUTE",
                    "remaining": 20,
                    "limit": 100,
                    "resetTime": "2026-08-18T16:10:00Z",
                },
                {
                    "duration": 300,
                    "timeUnit": "TIME_UNIT_MINUTE",
                    "remaining": 5,
                    "limit": 100,
                    "resetTime": "2026-08-18T16:47:56Z",
                },
            ],
        }
    )
    assert row["remaining"].startswith("5/100")
    assert "5h window" in row["remaining"]
    assert row["reset_at"] == datetime.fromisoformat(
        "2026-08-18T16:47:56+00:00"
    ).timestamp()
    assert row["flags"] == []


def test_engines_without_windows_keep_the_cycle_reading():
    """No windows list, or an empty one, must match the pre-binding renderer."""
    reset_iso = "2033-05-18T03:33:20Z"
    expected_reset = datetime.fromisoformat(
        reset_iso.replace("Z", "+00:00")
    ).timestamp()
    for windows in (None, [], "not-a-list"):
        usage_obj = {
            "remaining": 66,
            "limit": 100,
            "resetTime": reset_iso,
        }
        if windows is not None:
            usage_obj["windows"] = windows
        row = _kimi_usage_row(usage_obj)
        assert row["remaining"] == "66/100"
        assert row["reset_at"] == expected_reset
        assert row["flags"] == []


def test_remaining_column_header_names_the_direction():
    """The column shows leftover headroom; the header has to say so.

    `2%` is 2% left, not 2% consumed. An unlabelled PROBE READING is read
    backwards on the exact decision the table exists to inform.
    """
    rendered = usage.render_table(
        [
            {
                "provider": "codex",
                "account": "25ca6b",
                "remaining": "2%",
                "reset_at": None,
                "flags": [],
            }
        ],
        now=2_000_000_000,
    )
    header = rendered.splitlines()[0]
    assert "REMAINING" in header
    assert "PROBE READING" not in rendered
    assert "2%" in rendered


def test_normalizes_cursor_ui_only_shape():
    spec = usage.ReaderSpec("cursor", "cursor", "cursor_usage.py")
    row = usage.normalize_payload(
        spec,
        [
            {
                "label": "cursor",
                "source": "cursor_dashboard",
                "ok": True,
                "usage": None,
                "note": "UI-only",
            }
        ],
    )[0]

    assert row == {
        "provider": "cursor",
        "account": None,
        "remaining": "UI-only",
        "reset_at": None,
        "flags": [],
    }


def test_normalizes_current_and_drifted_claude_shapes_without_email():
    spec = usage.ReaderSpec("claude", "claude", "claude_usage.py")
    now = 2_000_000_000.0
    rows = usage.normalize_payload(
        spec,
        [
            {
                "label": "work",
                "email": "not-forwarded@example.test",
                "logged_in": True,
                "session_used_percent": 25,
                "weekly_used_percent": 80,
                "weekly_sonnet_used_percent": None,
                "cooldown_s": 300,
            },
            {
                "label": "nested",
                "logged_in": True,
                "usage": {
                    "remaining": 3,
                    "limit": 5,
                    "reset_at": 2_000_001_000,
                },
            },
        ],
        now=now,
    )

    assert rows[0] == {
        "provider": "claude",
        "account": "work",
        "remaining": "session 75%, week 20%",
        "reset_at": now + 300,
        "flags": [],
    }
    assert rows[1]["remaining"] == "3/5"
    assert rows[1]["reset_at"] == 2_000_001_000.0
    assert "email" not in rows[0]


def test_logged_out_and_reader_auth_errors_are_flagged():
    claude = usage.ReaderSpec("claude", "claude", "claude_usage.py")
    codex = usage.ReaderSpec("codex", "codex", "codex_usage.py")

    logged_out = usage.normalize_payload(
        claude, [{"label": "personal", "logged_in": False}]
    )[0]
    auth_error = usage.normalize_payload(
        codex,
        [
            {
                "seat": "seat-a",
                "ok": False,
                "error": "authentication unavailable",
            }
        ],
    )[0]

    assert logged_out["remaining"] == "needs-login"
    assert logged_out["flags"] == ["auth-broken"]
    assert auth_error["remaining"] == "needs-login"
    assert auth_error["flags"] == ["auth-broken"]


def test_claude_unknown_login_error_is_not_reported_healthy():
    claude = usage.ReaderSpec("claude", "claude", "claude_usage.py")

    row = usage.normalize_payload(
        claude,
        [
            {
                "label": "timed-out",
                "logged_in": None,
                "error": "auth status timed out",
            }
        ],
    )[0]

    assert row["remaining"] == "needs-login"
    assert row["flags"] == ["auth-broken"]


def test_claude_multi_label_payload_becomes_one_row_per_label():
    """The reader sweeps every saved label; each label keeps its own state and
    gets its own table row (t-193)."""
    claude = usage.ReaderSpec("claude", "claude", "claude_usage.py")
    now = 2_000_000_000.0

    rows = usage.normalize_payload(
        claude,
        [
            {
                "label": "third",
                "logged_in": True,
                "login_status": "ok",
                "session_used_percent": None,
                "weekly_used_percent": None,
                "weekly_sonnet_used_percent": None,
                "source": "claude_auth_status",
            },
            {
                "label": "personal",
                "logged_in": None,
                "login_status": "pending",
                "source": "claude_auth_status",
            },
            {
                "label": "work",
                "logged_in": False,
                "login_status": "expired",
                "source": "claude_auth_status",
            },
        ],
        now=now,
    )

    assert [row["account"] for row in rows] == ["third", "personal", "work"]
    assert [row["remaining"] for row in rows] == ["ok", "pending", "needs-login"]
    assert [row["flags"] for row in rows] == [[], [], ["auth-broken"]]
    assert all(tuple(row) == usage.ROW_KEYS for row in rows)
    assert all(row["provider"] == "claude" for row in rows)
    assert all(row["reset_at"] is None for row in rows)
    # Null resets must not shadow a real upcoming reset from another provider.
    codex = usage.normalize_payload(
        usage.ReaderSpec("codex", "codex", "codex_usage.py"),
        [{"seat": "seat-a", "used_percent": 10, "reset_at": now + 600, "ok": True}],
        now=now,
    )
    assert usage.soonest_reset(rows + codex, now=now) is codex[0]
    assert usage.soonest_reset(rows, now=now) is None

    rendered = usage.render_table(rows, now=now)
    assert "claude third      ok" in rendered
    assert "claude personal   pending" in rendered
    assert "claude work       needs-login  ⚠auth" in rendered


def test_claude_pending_label_outranks_probe_error_text():
    """A not-yet-materialized label reports 'pending', never 'unavailable' or a
    misleading auth flag, even when the reader attaches a probe reason."""
    claude = usage.ReaderSpec("claude", "claude", "claude_usage.py")

    row = usage.normalize_payload(
        claude,
        [
            {
                "label": "personal",
                "logged_in": None,
                "login_status": "pending",
                "error": "keychain sync must finish before materializing",
                "error_stage": "materialize",
            }
        ],
    )[0]

    assert row["remaining"] == "pending"
    assert row["flags"] == []


def test_claude_error_stage_row_stays_degraded():
    """login_status 'error (<stage>)' keeps the existing failure treatment."""
    claude = usage.ReaderSpec("claude", "claude", "claude_usage.py")

    rows = usage.normalize_payload(
        claude,
        [
            {
                "label": "auth-broken",
                "logged_in": None,
                "login_status": "error (auth)",
                "error": "claude auth status failed",
            },
            {
                "label": "probe-broken",
                "logged_in": None,
                "login_status": "error (probe)",
                "error": "unexpected OSError",
            },
        ],
    )

    assert rows[0]["remaining"] == "needs-login"
    assert rows[0]["flags"] == ["auth-broken"]
    assert rows[1]["remaining"] == "unavailable"
    assert rows[1]["flags"] == ["unavailable"]


def test_claude_indeterminate_label_is_unknown_not_ok():
    claude = usage.ReaderSpec("claude", "claude", "claude_usage.py")

    row = usage.normalize_payload(
        claude,
        [{"label": "mystery", "logged_in": None, "login_status": "unknown"}],
    )[0]

    assert row["remaining"] == "unknown"
    assert row["flags"] == []


# A timed-out reader no longer degrades to "unavailable" - it reports a distinct
# timeout row, covered by test_reader_timeout_is_distinct_from_reader_failure.
@pytest.mark.parametrize(
    ("filename", "body", "timeout_s"),
    [
        ("error.py", "raise SystemExit(2)\n", 1),
        ("garbage.py", "print('not-json')\n", 1),
    ],
)
def test_erroring_and_garbage_readers_degrade(
    tmp_path: Path,
    filename: str,
    body: str,
    timeout_s: float,
):
    _write_reader(tmp_path, filename, body)
    spec = usage.ReaderSpec("codex", "codex", filename)

    assert usage.run_reader(
        spec,
        readers_dir=tmp_path,
        timeout_s=timeout_s,
    ) == [usage.unavailable_row("codex")]


def test_missing_reader_degrades_to_one_unavailable_row(tmp_path: Path):
    spec = usage.ReaderSpec("kimi", "moonshot", "missing.py")

    assert usage.run_reader(spec, readers_dir=tmp_path) == [
        usage.unavailable_row("moonshot")
    ]


def test_every_report_row_carries_probe_source_and_observation_time(
    tmp_path: Path,
):
    rows = usage.collect_usage(
        readers_dir=tmp_path,
        reader_specs=(usage.ReaderSpec("codex", "codex", "missing.py"),),
        now=1_786_000_000.0,
        ledger_records=[],
    )

    assert len(rows) == 1
    assert rows[0]["evidence"] == {
        "probe": {
            "source": "quota_probe",
            "state": "unavailable",
            "observed_at": 1_786_000_000.0,
        },
        "dispatch": None,
        "conflict": False,
        "verdict": "unknown",
        "winner": None,
    }


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (89 * 60, "89m"),
        (91 * 60, "1.5h"),
        (47 * 60 * 60, "47.0h"),
        (49 * 60 * 60, "2.0d"),
    ],
)
def test_humanized_delta_boundaries(seconds: float, expected: str):
    assert usage.humanize_delta(seconds) == expected


def test_soonest_reset_selects_across_epoch_and_iso_sources():
    now = 2_000_000_000.0
    codex = usage.normalize_payload(
        usage.ReaderSpec("codex", "codex", "codex_usage.py"),
        [
            {
                "seat": "later",
                "used_percent": 10,
                "reset_at": now + 5_000,
                "ok": True,
            }
        ],
        now=now,
    )
    kimi_reset = datetime.fromtimestamp(now + 3_000, tz=timezone.utc).isoformat()
    kimi = usage.normalize_payload(
        usage.ReaderSpec("kimi", "moonshot", "kimi_usage.py"),
        [
            {
                "label": "kimi-code",
                "source": "kimi_code_usages",
                "ok": True,
                "usage": {
                    "remaining": 50,
                    "limit": 100,
                    "resetTime": kimi_reset,
                    "windows": [],
                },
            }
        ],
        now=now,
    )

    assert usage.soonest_reset(codex + kimi, now=now) is kimi[0]
    rendered = usage.render_table(codex + kimi, now=now)
    assert "soonest reset: moonshot in 50m" in rendered


def test_json_cli_shape_and_unavailable_exit_zero(
    tmp_path: Path, capsys, monkeypatch
):
    monkeypatch.setattr(usage, "PACKAGE_READERS_DIR", tmp_path / "empty-package")
    _write_reader(
        tmp_path,
        "codex_usage.py",
        "import json, sys\n"
        "assert sys.argv[1:] == ['--json']\n"
        "print(json.dumps([{'seat': 'safe', 'used_percent': 1, "
        "'reset_at': None, 'ok': True}]))\n",
    )

    assert usage.main(["--json", "--readers-dir", str(tmp_path)]) == 0
    rows = json.loads(capsys.readouterr().out)

    # One row per registered reader; derived, so adding a provider does not
    # fail this test for the wrong reason.
    assert len(rows) == len(usage.READERS)
    assert rows[0]["remaining"] == "99%"
    assert all(tuple(row) == usage.REPORT_ROW_KEYS for row in rows)
    assert [row["provider"] for row in rows[1:]] == [
        spec.provider for spec in usage.READERS if spec.key != "codex"
    ]
    # Every reader but the one written above is absent, so each degrades to a
    # single unavailable row rather than vanishing from the table.
    assert all(row["flags"] == ["unavailable"] for row in rows[1:])


def test_table_renders_health_flags():
    rows = [
        {
            "provider": "codex",
            "account": "wall",
            "remaining": "0%",
            "reset_at": None,
            "flags": ["walled"],
        },
        usage.unavailable_row("cursor"),
        {
            "provider": "claude",
            "account": "login",
            "remaining": "needs-login",
            "reset_at": None,
            "flags": ["auth-broken"],
        },
    ]

    rendered = usage.render_table(rows, now=2_000_000_000)
    assert "RESETS (local HH:MM)" in rendered
    assert "0%  ⛔wall" in rendered
    assert "unavailable  ⚠unavailable" in rendered
    assert "needs-login  ⚠auth" in rendered
    assert rendered.endswith("soonest reset: none")


def test_probe_wall_and_newer_served_dispatch_render_as_timestamped_conflict(
    tmp_path: Path,
    new_york_tz,
):
    """Reproduces the 2026-08-05 incident: the seat probe reports every Codex
    seat walled, while ledger evidence proves cf9f50 served after probed_at.

    The newer source wins: served-after-probe is HEALTHY via dispatch, not a
    0% wall with a CONFLICT banner.
    """
    probed_at = "2026-08-05T18:28:00+00:00"
    served_at = "2026-08-05T18:29:00+00:00"
    records = [
        {
            "seat": seat,
            "used_percent": 100.0,
            "reset_at": None,
            "probed_at": probed_at,
            "ok": True,
        }
        for seat in ("4c9435", "d78343", "cf9f50", "25ca6b")
    ]
    _write_reader(
        tmp_path,
        "codex_usage.py",
        "import json\nprint(json.dumps(" + repr(records) + "))\n",
    )
    rows = usage.collect_usage(
        readers_dir=tmp_path,
        reader_specs=(usage.ReaderSpec("codex", "codex", "codex_usage.py"),),
        now=datetime.fromisoformat("2026-08-05T19:00:00+00:00").timestamp(),
        ledger_records=[
            {
                "dispatch_id": "served-after-probe",
                "agent": "codex",
                "effective_account": "cf9f50",
                "state": "complete",
                "ended_at": served_at,
            },
            {
                "dispatch_id": "wall-at-dispatch",
                "agent": "codex",
                "effective_account": "d78343",
                "state": "quota_exhausted",
                "limit_kind": "exhausted",
                "reset_at": "2026-08-08T17:17:00+00:00",
                "ended_at": "2026-08-05T18:33:00+00:00",
            },
            {
                "dispatch_id": "legacy-wall",
                "agent": "codex",
                "effective_account": "4c9435",
                "state": "rate_limited",
                "ended_at": "2026-08-05T18:20:00+00:00",
            },
        ],
    )
    rendered = usage.render_table(
        rows,
        now=datetime.fromisoformat("2026-08-05T19:00:00+00:00").timestamp(),
    )
    served = next(row for row in rows if row["account"] == "cf9f50")
    exhausted = next(row for row in rows if row["account"] == "d78343")
    legacy = next(row for row in rows if row["account"] == "4c9435")

    assert all(tuple(row) == usage.REPORT_ROW_KEYS for row in rows)
    assert served["remaining"] == "ok"
    assert served["evidence"]["probe"]["state"] == "walled"
    assert served["evidence"]["dispatch"]["state"] == "served"
    assert served["evidence"]["verdict"] == "healthy"
    assert served["evidence"]["winner"] == "dispatch"
    assert served["evidence"]["conflict"] is False
    assert "walled" not in served["flags"]
    assert exhausted["evidence"]["dispatch"]["state"] == "quota_exhausted"
    assert exhausted["evidence"]["dispatch"]["reset_at"] == "2026-08-08T17:17:00+00:00"
    assert exhausted["evidence"]["verdict"] == "exhausted"
    assert exhausted["evidence"]["winner"] == "dispatch"
    assert legacy["evidence"]["dispatch"]["state"] == "limit_unknown"
    assert legacy["evidence"]["verdict"] == "exhausted"
    assert legacy["evidence"]["winner"] == "quota_probe"
    assert "⚠CONFLICT" not in rendered
    assert "winner: dispatch → healthy" in rendered
    assert "probe: walled" in rendered
    assert "dispatch: served" in rendered
    assert "Aug 05 14:28" in rendered
    assert "Aug 05 14:29" in rendered


def test_stale_served_dispatch_shows_without_false_conflict(
    tmp_path: Path,
    new_york_tz,
):
    """Live 2026-08-05 shape: seat 25ca6b served at 19:41Z, genuinely walled
    afterwards, and the 20:35Z probe then measured walled. The probe is the
    freshest reading, so both are shown with NO conflict banner - time alone
    explains the difference, and a banner here teaches operators to ignore it.
    """
    probed_at = "2026-08-05T20:35:11+00:00"
    records = [
        {
            "seat": "25ca6b",
            "used_percent": 100.0,
            "reset_at": None,
            "probed_at": probed_at,
            "ok": True,
        }
    ]
    _write_reader(
        tmp_path,
        "codex_usage.py",
        "import json\nprint(json.dumps(" + repr(records) + "))\n",
    )
    rows = usage.collect_usage(
        readers_dir=tmp_path,
        reader_specs=(usage.ReaderSpec("codex", "codex", "codex_usage.py"),),
        now=datetime.fromisoformat("2026-08-05T20:36:00+00:00").timestamp(),
        ledger_records=[
            {
                "dispatch_id": "served-before-probe",
                "agent": "codex",
                "effective_account": "25ca6b",
                "state": "complete",
                "ended_at": "2026-08-05T19:41:31+00:00",
            },
        ],
    )
    rendered = usage.render_table(
        rows,
        now=datetime.fromisoformat("2026-08-05T20:36:00+00:00").timestamp(),
    )

    seat = rows[0]
    assert seat["evidence"]["probe"]["state"] == "walled"
    assert seat["evidence"]["dispatch"]["state"] == "served"
    assert seat["evidence"]["verdict"] == "exhausted"
    assert seat["evidence"]["winner"] == "quota_probe"
    assert seat["evidence"]["conflict"] is False
    assert "⚠CONFLICT" not in rendered
    assert "winner: probe → exhausted" in rendered
    # Both readings stay visible with their own timestamps either way.
    assert "probe: walled" in rendered
    assert "dispatch: served" in rendered


def test_conflict_requires_dispatch_evidence_newer_than_probe():
    walled = {"state": "walled", "observed_at": 1_787_000_000.0}
    older_served = {"state": "served", "observed_at": 1_786_999_000.0}
    newer_served = {"state": "served", "observed_at": 1_787_000_001.0}
    # Freshness names a winner, so these are not CONFLICT banners.
    assert usage._evidence_conflicts(walled, older_served) is False
    assert usage._evidence_conflicts(walled, newer_served) is False
    # Unmeasured ordering cannot be proven coherent: stays loud.
    assert usage._evidence_conflicts({"state": "walled"}, {"state": "served"}) is True

    reported = {"state": "reported", "observed_at": 1_787_000_000.0}
    newer_exhausted = {"state": "quota_exhausted", "observed_at": 1_787_000_001.0}
    older_exhausted = {"state": "quota_exhausted", "observed_at": 1_786_999_000.0}
    assert usage._evidence_conflicts(reported, newer_exhausted) is False
    assert usage._evidence_conflicts(reported, older_exhausted) is False
    # Agreement kinds never conflict, in either direction of staleness.
    assert usage._evidence_conflicts(walled, newer_exhausted) is False
    unknown = {"state": "limit_unknown", "observed_at": 1_787_000_001.0}
    assert usage._evidence_conflicts(walled, unknown) is False

    assert usage.headroom_verdict(walled, newer_served)["winner"] == "dispatch"
    assert usage.headroom_verdict(walled, newer_served)["verdict"] == "healthy"
    assert usage.headroom_verdict(walled, older_served)["winner"] == "quota_probe"
    assert usage.headroom_verdict(walled, older_served)["verdict"] == "exhausted"
    assert usage.headroom_verdict(reported, newer_exhausted)["winner"] == "dispatch"
    assert usage.headroom_verdict(reported, newer_exhausted)["verdict"] == "exhausted"
    assert usage.headroom_verdict(reported, older_exhausted)["winner"] == "quota_probe"
    assert usage.headroom_verdict(reported, older_exhausted)["verdict"] == "healthy"


def test_claude_reader_invoked_with_skip_tui(tmp_path):
    """The claude sweep's full-TUI default exceeds the reader timeout; the
    aggregator must request the fast login-health pass (t-189)."""
    from goalflight_usage import READERS, run_reader

    spec = next(s for s in READERS if s.key == "claude")
    assert "--skip-tui" in spec.extra_args

    reader = tmp_path / spec.filename
    reader.write_text(
        "import json, sys\n"
        "if '--skip-tui' not in sys.argv: sys.exit(3)\n"
        "print(json.dumps([{'label': 'x', 'logged_in': False}]))\n"
    )
    rows = run_reader(spec, readers_dir=tmp_path, timeout_s=10.0, now=0.0)
    assert rows and 'unavailable' not in (rows[0].get('flags') or ())


def test_no_shipped_reader_opts_into_the_deep_capture(tmp_path):
    """--deep exists as a mechanism, but nothing opts into it yet.

    The claude reader is quarantined: its TUI capture does not isolate per
    account, and a sweep was observed leaving one label resolving to a different
    account, which its sync-back then writes through to that label's stored
    backup. Until isolation is proven, --deep must not change any reader's argv.
    """
    from goalflight_usage import READERS, run_reader

    claude = next(s for s in READERS if s.key == "claude")
    assert "--skip-tui" in claude.extra_args
    for spec in READERS:
        assert spec.deep_args is None
        assert spec.args_for(deep=True) == spec.extra_args
        assert spec.args_for(deep=False) == spec.extra_args

    # The reader still receives --skip-tui under --deep, so the safe fast path
    # is what actually runs.
    reader = tmp_path / claude.filename
    reader.write_text(
        "import json, sys\n"
        "if '--skip-tui' not in sys.argv: sys.exit(3)\n"
        "print(json.dumps([{'label': 'x', 'logged_in': True}]))\n"
    )
    rows = run_reader(claude, readers_dir=tmp_path, timeout_s=10.0, now=0.0, deep=True)
    assert rows and "unavailable" not in (rows[0].get("flags") or ())


def test_deep_variant_plumbing_still_works_when_a_spec_opts_in(tmp_path):
    """The mechanism itself must stay correct so the quarantine can be lifted."""
    from goalflight_usage import ReaderSpec, run_reader

    spec = ReaderSpec("claude", "claude", "r.py", ("--skip-tui",), deep_args=())
    (tmp_path / "r.py").write_text(
        "import json, sys\n"
        "if '--skip-tui' in sys.argv: sys.exit(3)\n"
        "print(json.dumps([{'label': 'x', 'logged_in': True,\n"
        "                   'weekly_reset_at': 1000.0}]))\n"
    )
    rows = run_reader(spec, readers_dir=tmp_path, timeout_s=10.0, now=0.0, deep=True)
    assert rows and rows[0].get("reset_at") == 1000.0


def test_nonzero_exit_with_valid_rows_is_kept(tmp_path):
    """A reader that exits nonzero while emitting valid rows is reporting
    degraded accounts, not failing - its rows must reach the table (t-189)."""
    from goalflight_usage import ReaderSpec, run_reader

    spec = ReaderSpec("claude", "claude", "r.py", ("--skip-tui",))
    reader = tmp_path / "r.py"
    reader.write_text(
        "import json, sys\n"
        "print(json.dumps([{'label': 'x', 'logged_in': False}]))\n"
        "sys.exit(1)\n"
    )
    rows = run_reader(spec, readers_dir=tmp_path, timeout_s=10.0, now=0.0)
    assert rows and 'unavailable' not in (rows[0].get('flags') or ())


def test_token_lapsed_renders_auto_heal_not_needs_login(tmp_path):
    """A reader error starting token_lapsed maps to 'lapsed (auto-heals)'
    with no auth flag - it self-repairs on the provider CLI's next use."""
    from goalflight_usage import ReaderSpec, run_reader
    import json as _json

    spec = ReaderSpec("kimi", "moonshot", "r.py")
    reader = tmp_path / "r.py"
    reader.write_text(
        "import json\n"
        "print(json.dumps([{'label': 'kimi-code', 'ok': False,"
        " 'error': 'token_lapsed: auto-heals on next kimi use'}]))\n"
    )
    rows = run_reader(spec, readers_dir=tmp_path, timeout_s=10.0, now=0.0)
    assert rows[0]["remaining"] == "lapsed (auto-heals)"
    assert "auth-broken" not in (rows[0].get("flags") or ())


def test_reader_timeout_is_distinct_from_reader_failure(tmp_path):
    """A reader that ran out of budget measured nothing; it did not measure a
    broken account. Collapsing the two sends the operator to debug the account
    while the real fault is the harness budget."""
    from goalflight_usage import ReaderSpec, run_reader

    spec = ReaderSpec("claude", "claude", "slow.py")
    (tmp_path / "slow.py").write_text("import time\ntime.sleep(5)\n")
    rows = run_reader(spec, readers_dir=tmp_path, timeout_s=0.4, now=0.0)
    assert rows[0]["flags"] == ["timeout"]
    assert rows[0]["remaining"] == "timed out"

    spec = ReaderSpec("claude", "claude", "broken.py")
    (tmp_path / "broken.py").write_text("print('not json')\n")
    rows = run_reader(spec, readers_dir=tmp_path, timeout_s=10.0, now=0.0)
    assert rows[0]["flags"] == ["unavailable"]


def test_grok_row_renders_as_a_resetting_window():
    """grok is a subscription credit pool, so it normalizes like a seat window."""
    spec = usage.ReaderSpec("grok", "grok", "grok_usage.py")
    row = usage.normalize_payload(
        spec,
        [
            {
                "label": "grok",
                "ok": True,
                "used_percent": 41.0,
                "reset_at": 2_000_000_000,
                "prepaid_balance": 0.0,
            }
        ],
    )[0]
    assert row == {
        "provider": "grok",
        "account": None,
        "remaining": "59% · prepaid=0",
        "reset_at": 2_000_000_000.0,
        "flags": [],
    }


def test_grok_reader_failure_never_renders_as_headroom():
    """The backing endpoint is undocumented; a contract change must not become
    a confident percentage."""
    spec = usage.ReaderSpec("grok", "grok", "grok_usage.py")
    row = usage.normalize_payload(
        spec,
        [{"label": "grok", "ok": False, "error": "billing response lacks config"}],
    )[0]
    assert row["remaining"] != "100%"
    assert not str(row["remaining"]).endswith("%")

    exhausted = usage.normalize_payload(
        spec,
        [
            {
                "label": "grok",
                "ok": True,
                "used_percent": 100.0,
                "prepaid_balance": 0.0,
            }
        ],
    )[0]
    assert exhausted["remaining"] == "0% · prepaid=0"
    assert exhausted["flags"] == ["walled"]


def test_grok_bundled_reader_is_fallback_when_operator_zone_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """run_reader: missing operator candidate -> bundled file -> normalized row."""
    spec = next(spec for spec in usage.READERS if spec.key == "grok")
    invoked_paths = []

    def fake_run(argv, **kwargs):
        del kwargs
        invoked_paths.append(Path(argv[1]))
        return usage.subprocess.CompletedProcess(
            argv,
            0,
            stdout=json.dumps(
                [
                    {
                        "label": "grok",
                        "ok": True,
                        "used_percent": 10.0,
                        "prepaid_balance": 0.0,
                    }
                ]
            ),
            stderr="",
        )

    monkeypatch.setattr(usage.subprocess, "run", fake_run)
    rows = usage.run_reader(spec, readers_dir=tmp_path / "absent-operator-zone")

    assert invoked_paths == [REPO_ROOT / "scripts" / "grok_usage.py"]
    assert rows[0]["remaining"] == "90% · prepaid=0"


def test_operator_grok_reader_shadows_bundled_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """run_reader: present operator candidate wins before bundled fallback."""
    spec = next(spec for spec in usage.READERS if spec.key == "grok")
    shadow = tmp_path / spec.filename
    shadow.write_text("# operator shadow\n", encoding="utf-8")
    invoked_paths = []

    def fake_run(argv, **kwargs):
        del kwargs
        invoked_paths.append(Path(argv[1]))
        return usage.subprocess.CompletedProcess(
            argv,
            0,
            stdout=json.dumps(
                [
                    {
                        "label": "grok",
                        "ok": True,
                        "used_percent": 20.0,
                        "prepaid_balance": 0.0,
                    }
                ]
            ),
            stderr="",
        )

    monkeypatch.setattr(usage.subprocess, "run", fake_run)
    rows = usage.run_reader(spec, readers_dir=tmp_path)

    assert invoked_paths == [shadow]
    assert rows[0]["remaining"] == "80% · prepaid=0"


@pytest.mark.parametrize("reader_key", ["codex", "kimi", "cursor", "claude"])
def test_ext_only_readers_stay_unavailable_without_operator_zone(
    tmp_path: Path, reader_key: str
) -> None:
    """run_reader: both candidates absent -> the provider's unavailable row."""
    spec = next(spec for spec in usage.READERS if spec.key == reader_key)

    assert usage.run_reader(spec, readers_dir=tmp_path / "absent-operator-zone") == [
        usage.unavailable_row(spec.provider)
    ]


@pytest.mark.parametrize(
    ("used_percent", "prepaid_balance", "expected_walled"),
    [
        (100.0, 25.0, False),
        (100.0, 0.0, True),
        (100.0, None, True),
        (99.0, None, False),
    ],
)
def test_grok_prepaid_balance_controls_wall_without_collapsing_unknown(
    used_percent: float,
    prepaid_balance: float | None,
    expected_walled: bool,
) -> None:
    """normalize_payload: reader values -> grok's tri-state wall predicate."""
    spec = next(spec for spec in usage.READERS if spec.key == "grok")
    row = usage.normalize_payload(
        spec,
        [
            {
                "label": "grok",
                "ok": True,
                "used_percent": used_percent,
                "prepaid_balance": prepaid_balance,
            }
        ],
    )[0]

    assert ("walled" in row["flags"]) is expected_walled
    expected_balance = (
        "unknown" if prepaid_balance is None else str(int(prepaid_balance))
    )
    assert f"prepaid={expected_balance}" in row["remaining"]


def test_non_grok_prepaid_balance_uses_generic_wall_and_display_path() -> None:
    """codex reader record -> generic post-normalization balance handling."""
    spec = next(spec for spec in usage.READERS if spec.key == "codex")
    row = usage.normalize_payload(
        spec,
        [
            {
                "seat": "future-hybrid-seat",
                "ok": True,
                "used_percent": 100.0,
                "prepaid_balance": 25.0,
            }
        ],
    )[0]

    assert row["flags"] == []
    assert row["remaining"] == "0% · prepaid=25"
    assert "0% · prepaid=25" in usage.render_table([row], now=2_000_000_000)


def test_probe_reading_shows_the_deciding_balance_and_omits_spend_attribution() -> None:
    """reader record -> normalized remaining -> REMAINING column.

    Only the balance earns a place in the row. `product_usage` percentages are
    shares of one already-spent budget and sum to 100 (measured: GrokBuild 95,
    GrokVoice 3, GrokChat 2) -- a receipt for where consumed credit went, not
    per-product headroom. Rendering it next to a remaining-percent column reads
    as "one lane is nearly spent, the others are free" when there is one budget
    and it is gone. `on_demand_*` drives no verdict either.

    Asserted as substrings, not as one exact line: the column pads to the widest
    row, so an exact-string assertion passes with the full fleet and fails on a
    single row, which is how the earlier version of this test broke.
    """
    spec = next(spec for spec in usage.READERS if spec.key == "grok")
    row = usage.normalize_payload(
        spec,
        [
            {
                "label": "grok",
                "ok": True,
                "used_percent": 100.0,
                "prepaid_balance": 0.0,
                "on_demand_cap": 0.0,
                "on_demand_used": 0.0,
                "product_usage": {
                    "GrokBuild": 95.0,
                    "GrokVoice": 3.0,
                    "GrokChat": 2.0,
                },
            }
        ],
    )[0]

    rendered = usage.render_table([row], now=2_000_000_000)
    assert "0% · prepaid=0" in rendered
    assert "⛔wall" in rendered, "a zero balance must not clear the wall"
    for omitted in ("components", "GrokBuild", "GrokVoice", "GrokChat", "on_demand"):
        assert omitted not in rendered, f"{omitted} must not reach the row"
    assert "PROVIDER/ACCOUNT  REMAINING" in rendered


def test_grok_rows_are_per_account_and_host_stays_unlabelled():
    """Input path: the reader returns one record per configured grok login.

    Several logins must render as several rows, each naming its own account, or
    a second grok seat's headroom is invisible and the operator cannot tell
    which pool a number belongs to. The host login carries account None and must
    keep rendering as a bare "grok" row so its existing identity is unchanged.
    """
    spec = next(spec for spec in usage.READERS if spec.key == "grok")
    rows = usage.normalize_payload(
        spec,
        [
            {
                "label": "grok",
                "account": None,
                "ok": True,
                "used_percent": 100.0,
                "prepaid_balance": 0.0,
                "reset_at": 1786000000,
            },
            {
                "label": "grok",
                "account": "6f3c47",
                "ok": True,
                "used_percent": 12.0,
                "prepaid_balance": 0.0,
                "reset_at": 1786600000,
            },
        ],
        now=1785900000,
    )
    assert len(rows) == 2
    assert [row["account"] for row in rows] == [None, "6f3c47"]
    assert all(row["provider"] == "grok" for row in rows)
    # the two rows must not collapse onto one another
    assert rows[0]["remaining"] != rows[1]["remaining"]


def test_grok_account_label_survives_a_reader_failure():
    """A failed row must still say which login failed, or it is unactionable."""
    spec = next(spec for spec in usage.READERS if spec.key == "grok")
    rows = usage.normalize_payload(
        spec,
        [{"label": "grok", "account": "6f3c47", "ok": False, "error": "no grok login found"}],
        now=1785900000,
    )
    assert rows[0]["account"] == "6f3c47"


def test_grok_absent_percent_renders_unknown_not_a_number():
    """used_percent None (endpoint omitted the key) must read as unknown."""
    spec = next(spec for spec in usage.READERS if spec.key == "grok")
    rows = usage.normalize_payload(
        spec,
        [
            {
                "label": "grok",
                "account": "6f3c47",
                "ok": True,
                "used_percent": None,
                "prepaid_balance": 0.0,
                "reset_at": 1786600000,
            }
        ],
        now=1785900000,
    )
    # the prepaid balance still renders alongside it (that is the deciding
    # field); what must never appear is a percentage invented from the absence.
    assert "unknown" in str(rows[0]["remaining"])
    assert "%" not in str(rows[0]["remaining"])
    assert "walled" not in rows[0].get("flags", ())


def test_grok_is_registered_in_readers_and_normalizers():
    assert "grok" in usage.NORMALIZERS
    assert any(spec.key == "grok" for spec in usage.READERS)


def _codex_reader(tmp_path: Path, records: list[dict]) -> None:
    _write_reader(
        tmp_path,
        "codex_usage.py",
        "import json\nprint(json.dumps(" + repr(records) + "))\n",
    )


def test_fresh_healthy_probe_outranks_stale_exhaustion_record(
    tmp_path: Path,
    new_york_tz,
) -> None:
    """A probe taken after a dispatch wall is HEALTHY and names the probe."""
    now = datetime.fromisoformat("2026-08-27T18:00:00+00:00").timestamp()
    _codex_reader(
        tmp_path,
        [
            {
                "seat": "25ca6b",
                "used_percent": 0.0,
                "reset_at": None,
                "probed_at": "2026-08-27T18:00:00+00:00",
                "ok": True,
            }
        ],
    )
    rows = usage.collect_usage(
        readers_dir=tmp_path,
        reader_specs=(usage.ReaderSpec("codex", "codex", "codex_usage.py"),),
        now=now,
        ledger_records=[
            {
                "dispatch_id": "stale-wall",
                "agent": "codex",
                "effective_account": "25ca6b",
                "state": "quota_exhausted",
                "limit_kind": "exhausted",
                "reset_at": "2026-09-01T11:44:00+00:00",
                "ended_at": "2026-08-26T00:48:00+00:00",
            }
        ],
    )
    rendered = usage.render_table(rows, now=now)
    seat = rows[0]
    assert seat["remaining"] == "100%"
    assert "walled" not in seat["flags"]
    assert seat["evidence"]["verdict"] == "healthy"
    assert seat["evidence"]["winner"] == "quota_probe"
    assert "winner: probe → healthy" in rendered
    assert "dispatch: quota_exhausted" in rendered
    assert usage.not_before_still_gates(
        datetime.fromisoformat("2026-09-01T11:44:00+00:00").timestamp(),
        now=now,
        headroom=seat["evidence"],
    ) is False


def test_fresh_exhaustion_probe_outranks_stale_healthy_record(
    tmp_path: Path,
    new_york_tz,
) -> None:
    """The reverse: a newer wall probe beats an older served dispatch."""
    now = datetime.fromisoformat("2026-08-27T18:00:00+00:00").timestamp()
    _codex_reader(
        tmp_path,
        [
            {
                "seat": "25ca6b",
                "used_percent": 100.0,
                "reset_at": "2026-09-01T11:44:00+00:00",
                "probed_at": "2026-08-27T18:00:00+00:00",
                "ok": True,
            }
        ],
    )
    rows = usage.collect_usage(
        readers_dir=tmp_path,
        reader_specs=(usage.ReaderSpec("codex", "codex", "codex_usage.py"),),
        now=now,
        ledger_records=[
            {
                "dispatch_id": "stale-ok",
                "agent": "codex",
                "effective_account": "25ca6b",
                "state": "complete",
                "ended_at": "2026-08-26T00:48:00+00:00",
            }
        ],
    )
    rendered = usage.render_table(rows, now=now)
    seat = rows[0]
    assert seat["remaining"] == "0%"
    assert seat["flags"] == ["walled"]
    assert seat["evidence"]["verdict"] == "exhausted"
    assert seat["evidence"]["winner"] == "quota_probe"
    assert "winner: probe → exhausted" in rendered
    assert "dispatch: served" in rendered


def test_fresh_dispatch_exhaustion_outranks_stale_healthy_probe(
    tmp_path: Path,
    new_york_tz,
) -> None:
    """Both directions: a newer dispatch wall beats an older healthy probe."""
    now = datetime.fromisoformat("2026-08-27T18:00:00+00:00").timestamp()
    _codex_reader(
        tmp_path,
        [
            {
                "seat": "25ca6b",
                "used_percent": 8.0,
                "reset_at": None,
                "probed_at": "2026-08-27T12:00:00+00:00",
                "ok": True,
            }
        ],
    )
    rows = usage.collect_usage(
        readers_dir=tmp_path,
        reader_specs=(usage.ReaderSpec("codex", "codex", "codex_usage.py"),),
        now=now,
        ledger_records=[
            {
                "dispatch_id": "fresh-wall",
                "agent": "codex",
                "effective_account": "25ca6b",
                "state": "quota_exhausted",
                "limit_kind": "exhausted",
                "reset_at": "2026-09-01T11:44:00+00:00",
                "ended_at": "2026-08-27T17:50:00+00:00",
            }
        ],
    )
    rendered = usage.render_table(rows, now=now)
    seat = rows[0]
    assert seat["remaining"] == "exhausted"
    assert "walled" in seat["flags"]
    assert seat["evidence"]["verdict"] == "exhausted"
    assert seat["evidence"]["winner"] == "dispatch"
    assert "winner: dispatch → exhausted" in rendered


def test_unavailable_probe_renders_unknown_not_stale_exhaustion(
    tmp_path: Path,
    new_york_tz,
) -> None:
    """A probe that cannot be taken is UNKNOWN. The stale wall is not truth."""
    now = datetime.fromisoformat("2026-08-27T18:00:00+00:00").timestamp()
    _codex_reader(
        tmp_path,
        [
            {
                "seat": "seat-a",
                "ok": False,
                "error": "billing endpoint unreachable",
                "probed_at": "2026-08-27T18:00:00+00:00",
            }
        ],
    )
    rows = usage.collect_usage(
        readers_dir=tmp_path,
        reader_specs=(usage.ReaderSpec("codex", "codex", "codex_usage.py"),),
        now=now,
        ledger_records=[
            {
                "dispatch_id": "stale-wall",
                "agent": "codex",
                "effective_account": "seat-a",
                "state": "quota_exhausted",
                "limit_kind": "exhausted",
                "reset_at": "2026-09-01T11:44:00+00:00",
                "ended_at": "2026-08-26T00:48:00+00:00",
            }
        ],
    )
    rendered = usage.render_table(rows, now=now)
    seat = rows[0]
    assert seat["evidence"]["verdict"] == "unknown"
    assert seat["evidence"]["winner"] is None
    assert "walled" not in seat["flags"]
    assert seat["remaining"] not in {"exhausted", "0%", "ok"}
    assert "winner: none → unknown" in rendered
    # Unknown keeps a stored not_before: the wall flag is the only evidence.
    assert usage.not_before_still_gates(
        datetime.fromisoformat("2026-09-01T11:44:00+00:00").timestamp(),
        now=now,
        headroom=seat["evidence"],
    ) is True
