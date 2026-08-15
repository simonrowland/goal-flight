"""Real dispatch-path proof for Moonshot sandbox request/support/enforcement truth."""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import uuid
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import goalflight_dispatch as dispatch  # noqa: E402
import goalflight_fleet_console as fleet_console  # noqa: E402
import goalflight_ledger as ledger  # noqa: E402
import goalflight_status as status  # noqa: E402


def _fake_worker(home: Path) -> Path:
    binary = home / ".kimi-code" / "bin" / "kimi"
    binary.parent.mkdir(parents=True)
    binary.write_text(
        "#!/bin/sh\n"
        ': > "$HOME/launch-marker"\n'
        ': > "$PWD/probe"\n'
        "printf 'COMPLETE: %s — fake worker\\n' \"$GOALFLIGHT_DISPATCH_ID\"\n"
        "sleep 0.5\n",
        encoding="utf-8",
    )
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    (home / ".profile").write_text(
        'PATH="$HOME/.kimi-code/bin:/usr/bin:/bin"\nexport PATH\n',
        encoding="utf-8",
    )
    return binary


def _run(dispatch_id: str, project: Path, *, read_only: bool) -> tuple[int, str, str]:
    argv = [
        "--agent",
        "moonshot",
        "--dispatch-id",
        dispatch_id,
        "--cwd",
        str(project),
        "--prompt",
        "Inspect the current directory and return findings inline.",
        "--foreground",
        "--capacity-wait-s",
        "0",
        "--poll-secs",
        "0.05",
        "--max-idle-secs",
        "5",
        "--ignore-git-warn",
        "--no-orientation",
    ]
    if read_only:
        argv.append("--read-only")
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        return dispatch.main(argv), stdout.getvalue(), stderr.getvalue()


def _record(dispatch_id: str) -> dict:
    path = ledger.record_path(dispatch_id, create=False)
    assert path.exists(), f"real ledger writer did not create {path}"
    return json.loads(path.read_text(encoding="utf-8"))


def case_real_dispatch_records_honest_moonshot_sandbox_posture() -> None:
    with tempfile.TemporaryDirectory(prefix="gf-moonshot-posture-") as tmp:
        project = Path(tmp) / f"sandbox-project-{os.getpid()}-{uuid.uuid4().hex}"
        project.mkdir()
        try:
            temp_root = Path(tmp)
            fake_home = temp_root / "home"
            state_dir = temp_root / "state"
            fake_home.mkdir()
            _fake_worker(fake_home)
            env = {
                "GOALFLIGHT_STATE_DIR": str(state_dir),
                "GOALFLIGHT_TASK_STORE_DIR": str(temp_root / "task-store"),
                "GOALFLIGHT_JOURNAL_DIR": str(temp_root / "journal"),
                "GOALFLIGHT_MESSAGES_DIR": str(temp_root / "messages"),
                "GOALFLIGHT_WAKE_LEDGER_DIR": str(temp_root / "wake-ledger"),
                "HOME": str(fake_home),
            }
            with mock.patch.dict(os.environ, env, clear=False):
                rejected_id = "moonshot-read-only-rejected"
                rc, _stdout, stderr = _run(rejected_id, project, read_only=True)
                assert rc == 64, (rc, stderr)
                assert "supports only --os-sandbox off" in stderr, stderr
                assert "refusing before launch" in stderr, stderr
                assert not (project / "probe").exists(), "unsupported request launched writable worker"
                assert not (fake_home / "launch-marker").exists(), "worker launched before rejection"

                rejected = _record(rejected_id)
                assert rejected["state"] == "blocked_os_sandbox", rejected
                assert rejected["worker_pid"] is None, rejected
                rejected_posture = rejected["os_sandbox"]
                assert rejected_posture == {
                    "shape": "bash",
                    "requested_profile": "read-only",
                    "supported_profile": "off",
                    "enforced_profile": None,
                }, rejected_posture
                assert "read_only" not in rejected_posture, rejected_posture
                assert "os_sandbox_profile" not in rejected_posture, rejected_posture

                aggregate = status.scope_payload(status.status_payload(), str(project.resolve()))
                rejected_row = status.find_record(aggregate, rejected_id)
                assert rejected_row is not None, aggregate
                assert rejected_row["os_sandbox"] == rejected_posture, rejected_row
                rendered = "\n".join(status.render_text(aggregate, 20))
                assert (
                    "sandbox requested=read-only supported=off enforced=none" in rendered
                ), rendered
                ledger_status = io.StringIO()
                with contextlib.redirect_stdout(ledger_status):
                    ledger.cmd_status(type("Args", (), {"json": False, "limit": 20})())
                assert (
                    "sandbox requested=read-only supported=off enforced=none"
                    in ledger_status.getvalue()
                ), ledger_status.getvalue()
                dashboard_rows = status.dashboard_status_payload(project)["dispatches"]
                dashboard_row = next(row for row in dashboard_rows if row["dispatch_id"] == rejected_id)
                assert dashboard_row["os_sandbox"] == rejected_posture, dashboard_row
                fleet_row = fleet_console._worker_row(rejected)  # noqa: SLF001
                assert fleet_row["os_sandbox_requested"] == "read-only", fleet_row
                assert fleet_row["os_sandbox_supported"] == "off", fleet_row
                assert fleet_row["os_sandbox_enforced"] is None, fleet_row

                allowed_id = "moonshot-off-allowed"
                rc, stdout, stderr = _run(allowed_id, project, read_only=False)
                tail_path = state_dir / "dispatch" / f"{allowed_id}.tail"
                tail_text = tail_path.read_text(encoding="utf-8") if tail_path.exists() else None
                assert rc == 0, (
                    rc,
                    stdout,
                    stderr,
                    tail_text,
                    (fake_home / "launch-marker").exists(),
                    (project / "probe").exists(),
                )
                assert (fake_home / "launch-marker").exists(), "control worker never launched"
                assert (project / "probe").exists(), "control worker was not writable"
                allowed = _record(allowed_id)
                allowed_posture = allowed["os_sandbox"]
                assert allowed_posture == {
                    "shape": "bash",
                    "requested_profile": None,
                    "supported_profile": "off",
                    "enforced_profile": "off",
                }, allowed_posture
        finally:
            shutil.rmtree(project, ignore_errors=True)


def main() -> None:
    case_real_dispatch_records_honest_moonshot_sandbox_posture()
    print("test_dispatch_moonshot_sandbox_posture: all cases passed")


if __name__ == "__main__":
    main()
