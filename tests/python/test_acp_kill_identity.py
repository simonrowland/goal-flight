#!/usr/bin/env python3
"""Regression tests for ACP worker PID identity checks."""

from __future__ import annotations

from support import skip_posix_on_native_windows

skip_posix_on_native_windows("asserts POSIX bash process identity strings")

import sys
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_acp_client  # noqa: E402
from goalflight_acp_client import _same_process  # noqa: E402


def case_exec_comm_change_keeps_identity() -> None:
    started = ("Wed May 20 17:55:24 2026", "bash")
    live = ("Wed May 20 17:55:24 2026", "/Users/example/.local/bin/cursor-agent")
    assert _same_process(started, live) is True


def case_pid_reuse_lstart_change_is_different() -> None:
    started = ("Wed May 20 17:55:24 2026", "cursor-agent")
    live = ("Wed May 20 17:55:25 2026", "cursor-agent")
    assert _same_process(started, live) is False


def case_unavailable_meta_preserves_kill_fallthrough() -> None:
    live = ("Wed May 20 17:55:24 2026", "cursor-agent")
    assert _same_process(None, live) is True
    assert _same_process(live, None) is True


def case_windows_cleanup_skips_bare_pidfile_pid() -> None:
    with tempfile.TemporaryDirectory() as td:
        pid_dir = Path(td)
        stale = pid_dir / "999999.jsonl"
        stale.write_text(json.dumps({"pid": 12345, "agent": "codex-acp"}) + "\n", encoding="utf-8")

        def fake_pid_alive(pid: int) -> bool:
            return pid == 12345

        with patch("goalflight_acp_client._PIDFILE_DIR", pid_dir), \
            patch("goalflight_acp_client._ps_meta", return_value=None), \
            patch("goalflight_compat.is_windows", return_value=True), \
            patch("goalflight_compat.pid_alive", side_effect=fake_pid_alive), \
            patch("goalflight_compat.kill_pid", side_effect=AssertionError("bare reused pid killed")), \
            patch("goalflight_acp_client.log.warning") as warn:
            killed = goalflight_acp_client.cleanup_ghosts()
    assert killed == 0
    assert not stale.exists()
    assert warn.called


def main() -> None:
    case_exec_comm_change_keeps_identity()
    case_pid_reuse_lstart_change_is_different()
    case_unavailable_meta_preserves_kill_fallthrough()
    case_windows_cleanup_skips_bare_pidfile_pid()
    print("OK: ACP kill identity tests pass")


if __name__ == "__main__":
    main()
