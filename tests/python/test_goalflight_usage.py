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


@pytest.mark.parametrize(
    ("filename", "body", "timeout_s"),
    [
        ("error.py", "raise SystemExit(2)\n", 1),
        ("garbage.py", "print('not-json')\n", 1),
        ("timeout.py", "import time\ntime.sleep(2)\nprint('[]')\n", 0.05),
    ],
)
def test_erroring_garbage_and_timeout_readers_degrade(
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

    assert len(rows) == 4
    assert rows[0]["remaining"] == "99%"
    assert all(tuple(row) == usage.ROW_KEYS for row in rows)
    assert [row["provider"] for row in rows[1:]] == [
        "kimi-code",
        "cursor",
        "claude",
    ]
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
