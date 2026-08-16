"""Arm-reports-pending: a listener armed over a backlog reports it and stays
armed; only events beyond the arm-time high-water ring it.

Live-verified 2026-08-15 (operator-designed semantics: the arm doubles as the
peek, and a controller that is awake enough to arm does not need the pop)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import goalflight_journal as journal  # noqa: E402
import goalflight_wake as wake  # noqa: E402


@pytest.fixture()
def isolated(monkeypatch: pytest.MonkeyPatch) -> tuple[Path, dict[str, str]]:
    td = Path(tempfile.mkdtemp(prefix="gf-arm-pending-"))
    env = {
        "GOALFLIGHT_JOURNAL_DIR": str(td / "journals"),
        "GOALFLIGHT_STATE_DIR": str(td / "state"),
        "GOALFLIGHT_WAKE_LEDGER_DIR": str(td / "wake-ledger"),
        "GOALFLIGHT_MESSAGES_DIR": str(td / "messages"),
        "GOALFLIGHT_TASK_STORE_DIR": str(td / "task-store"),
        "GOAL_FLIGHT_PIDFILE_DIR": str(td / "pids"),
        "GOALFLIGHT_CAPACITY_CONF": os.devnull,
    }
    for value in env.values():
        if value != os.devnull:
            Path(value).mkdir(parents=True, exist_ok=True)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("GOALFLIGHT_CONTROLLER_LABEL", raising=False)
    monkeypatch.delenv("GOALFLIGHT_CONTROLLER_LEASE_NONCE", raising=False)
    project = td / "project"
    project.mkdir()
    return project, {**os.environ, **env}


def _post(env: dict[str, str], project: Path, text: str) -> None:
    subprocess.run(
        [sys.executable, str(SCRIPTS / "goalflight_messages.py"), "post",
         "--to-controller", "armtest", "--dispatch-id", "arm-backlog",
         "--type", "controller-notice", "--text", text],
        env=env, cwd=project, check=True, capture_output=True,
    )


def test_arm_reports_backlog_stays_armed_and_rings_on_new(
    isolated: tuple[Path, dict[str, str]],
) -> None:
    project, env = isolated
    authority = journal.open_or_create_journal(project)
    lease = authority.claim_or_renew_lease(
        "armtest", principal={"principal_id": "arm-pending-test"}
    ).value
    assert lease is not None
    with wake.register_lease_holder(
        project, controller_label="armtest", lease_nonce=lease.nonce
    ):
        _post(env, project, "backlog one")
        _post(env, project, "backlog two")
        listener_env = {**env,
                        "GOALFLIGHT_CONTROLLER_LABEL": "armtest",
                        "GOALFLIGHT_CONTROLLER_LEASE_NONCE": lease.nonce}
        proc = subprocess.Popen(
            [sys.executable, str(SCRIPTS / "goalflight_messages.py"),
             "listen-auto", "--project-root", str(project),
             "--controller-label", "armtest"],
            env=listener_env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True,
        )
        try:
            # The backlog must be REPORTED, with the advance command, and the
            # listener must remain armed (no pop for pending-at-arm events).
            deadline = time.monotonic() + 20
            header_lines: list[str] = []
            assert proc.stdout is not None
            while time.monotonic() < deadline:
                line = proc.stdout.readline()
                if not line:
                    break
                header_lines.append(line)
                if "item(s) reported" in line:
                    break
            joined = "".join(header_lines)
            assert "pending-at-arm: [controller-notice] arm-backlog seq=1" in joined
            assert "pending-at-arm: [controller-notice] arm-backlog seq=2" in joined
            json_line = next(
                l for l in header_lines if l.startswith("pending-at-arm-json: ")
            )
            payload = json.loads(json_line.split("pending-at-arm-json: ", 1)[1])
            assert payload["advance_command"]
            time.sleep(2)
            assert proc.poll() is None, "listener popped on the arm-time backlog"

            # An event beyond the arm-time high-water must ring.
            _post(env, project, "the new event")
            proc.wait(timeout=30)
            assert proc.returncode == 0
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()


def main() -> None:
    raise SystemExit(pytest.main([__file__, "-q"]))


if __name__ == "__main__":
    main()
