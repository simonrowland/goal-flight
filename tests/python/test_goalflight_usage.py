from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import goalflight_usage as usage  # noqa: E402


def _write_reader(directory: Path, filename: str, body: str) -> None:
    (directory / filename).write_text(body, encoding="utf-8")


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
    spec = usage.ReaderSpec("kimi", "kimi-code", "kimi_usage.py")
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

    assert row["provider"] == "kimi-code"
    assert row["account"] is None
    assert row["remaining"] == "66/100"
    assert row["reset_at"] == datetime.fromisoformat(
        reset_iso.replace("Z", "+00:00")
    ).timestamp()
    assert row["flags"] == []


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
    spec = usage.ReaderSpec("kimi", "kimi-code", "missing.py")

    assert usage.run_reader(spec, readers_dir=tmp_path) == [
        usage.unavailable_row("kimi-code")
    ]


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
        usage.ReaderSpec("kimi", "kimi-code", "kimi_usage.py"),
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
    assert "soonest reset: kimi-code in 50m" in rendered


def test_json_cli_shape_and_unavailable_exit_zero(tmp_path: Path, capsys):
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
    assert all(tuple(row) == usage.ROW_KEYS for row in rows)
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

    spec = ReaderSpec("kimi", "kimi-code", "r.py")
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
        [{"label": "grok", "ok": True, "used_percent": 41.0, "reset_at": 2_000_000_000}],
    )[0]
    assert row == {
        "provider": "grok",
        "account": None,
        "remaining": "59%",
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
        spec, [{"label": "grok", "ok": True, "used_percent": 100.0}]
    )[0]
    assert exhausted["remaining"] == "0%"
    assert exhausted["flags"] == ["walled"]


def test_grok_is_registered_in_readers_and_normalizers():
    assert "grok" in usage.NORMALIZERS
    assert any(spec.key == "grok" for spec in usage.READERS)
