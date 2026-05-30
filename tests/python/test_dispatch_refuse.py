"""Native Windows dispatch refusal tests."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_acp_client  # noqa: E402
import goalflight_acp_run  # noqa: E402
import goalflight_os_sandbox  # noqa: E402
import goalflight_review_job  # noqa: E402


def _assert_dispatch_message(text: str) -> None:
    assert "wsl --install" in text, text
    assert "re-run inside the distro" in text, text
    assert "Phase 2 enables native" in text, text


def case_acp_spawn_refuses_native_windows() -> None:
    async def _run() -> None:
        with patch("goalflight_compat.is_windows", return_value=True), \
            patch("goalflight_acp_client.require_acp_sdk", return_value=None):
            try:
                await goalflight_acp_client.spawn_acp_connection(
                    sys.executable,
                    ["-c", "print('never')"],
                    agent="codex-acp",
                    session_id="s",
                    cwd=str(ROOT),
                )
            except goalflight_acp_client.AcpError as exc:
                _assert_dispatch_message(str(exc))
            else:
                raise AssertionError("spawn_acp_connection accepted native Windows dispatch")

    asyncio.run(_run())


def case_review_job_refuses_native_windows() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        prompt = tmp / "prompt.md"
        prompt.write_text("review\n", encoding="utf-8")
        out_dir = tmp / "out"
        with patch("goalflight_compat.is_windows", return_value=True):
            with contextlib.redirect_stdout(io.StringIO()):
                rc = goalflight_review_job.main([
                    "--agent",
                    "custom",
                    "--repo",
                    str(ROOT),
                    "--prompt",
                    str(prompt),
                    "--output-dir",
                    str(out_dir),
                    "--json",
                    "--command",
                    sys.executable,
                    "-c",
                    "print('never')",
                ])
        assert rc == 2
        payload = json.loads((out_dir / "review.status.json").read_text(encoding="utf-8"))
        assert payload["state"] == "blocked_windows_dispatch"
        _assert_dispatch_message(payload["error"])


def case_acp_run_refuses_before_side_effects() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        status_path = tmp / "acp.status.json"
        missing_prompt = tmp / "missing-prompt.md"
        with patch("goalflight_compat.is_windows", return_value=True), \
            patch("goalflight_acp_client.require_acp_sdk", side_effect=AssertionError("SDK check ran")), \
            patch("goalflight_acp_run.create_and_route_dispatch_worktree", side_effect=AssertionError("worktree created")), \
            patch("goalflight_acp_run.goalflight_capacity.cmd_acquire", side_effect=AssertionError("capacity lease acquired")):
            with contextlib.redirect_stdout(io.StringIO()):
                try:
                    goalflight_acp_run.main(["--help"])
                except SystemExit as exc:
                    assert exc.code == 0
                else:
                    raise AssertionError("--help did not exit through argparse")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = goalflight_acp_run.main([
                    "--agent",
                    "codex-acp",
                    "--cwd",
                    str(tmp),
                    "--worktree",
                    "create",
                    "--prompt",
                    str(missing_prompt),
                    "--status-json",
                    str(status_path),
                    "--json",
                ])
        assert rc == 2
        payload = json.loads(status_path.read_text(encoding="utf-8"))
        assert payload["state"] == "blocked_windows_dispatch"
        assert payload["lease_id"] is None
        assert payload["worktree_path"] is None
        assert payload["planned_worktree_path"] is None
        assert not (tmp / "worktrees").exists()
        _assert_dispatch_message(payload["error"])


def case_os_sandbox_refuses_native_windows() -> None:
    with patch("goalflight_compat.is_windows", return_value=True):
        try:
            goalflight_os_sandbox.preflight_os_sandbox("read-only")
        except goalflight_os_sandbox.OsSandboxError as exc:
            text = str(exc)
            assert "macOS-only" in text, text
            assert "Drop --os-sandbox" in text, text
        else:
            raise AssertionError("OS sandbox accepted native Windows")


def main() -> None:
    case_acp_spawn_refuses_native_windows()
    case_review_job_refuses_native_windows()
    case_acp_run_refuses_before_side_effects()
    case_os_sandbox_refuses_native_windows()
    print("OK: native Windows dispatch refusal tests pass")


if __name__ == "__main__":
    main()
