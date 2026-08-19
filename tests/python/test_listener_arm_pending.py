"""Opt-in arm-reports-pending listener behavior and exit-driven compatibility.

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
    monkeypatch.delenv("GOALFLIGHT_DISPATCH_ID", raising=False)
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
             "listen", "--project-root", str(project),
             "--controller-label", "armtest", "--report-pending"],
            env=listener_env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True,
        )
        try:
            # The backlog must be REPORTED tersely, with the advance command, and the
            # listener must remain armed (no pop for pending-at-arm events).
            deadline = time.monotonic() + 20
            header_lines: list[str] = []
            assert proc.stdout is not None
            while time.monotonic() < deadline:
                line = proc.stdout.readline()
                if not line:
                    break
                header_lines.append(line)
                if line.startswith("advance: "):
                    break
            joined = "".join(header_lines)
            assert "[controller-notice] arm-backlog seq=1 — backlog one" in joined
            assert "[controller-notice] arm-backlog seq=2 — backlog two" in joined
            advance_lines = [line for line in header_lines if line.startswith("advance: ")]
            assert len(advance_lines) == 1
            assert "pending-at-arm-json" not in joined
            assert "item(s) reported" not in joined
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


def test_default_arm_over_backlog_exits_promptly(
    isolated: tuple[Path, dict[str, str]],
) -> None:
    project, env = isolated
    authority = journal.open_or_create_journal(project)
    lease = authority.claim_or_renew_lease(
        "armtest", principal={"principal_id": "arm-compat-test"}
    ).value
    assert lease is not None
    with wake.register_lease_holder(
        project, controller_label="armtest", lease_nonce=lease.nonce
    ):
        _post(env, project, "compat backlog")
        listener_env = {
            **env,
            "GOALFLIGHT_CONTROLLER_LABEL": "armtest",
            "GOALFLIGHT_CONTROLLER_LEASE_NONCE": lease.nonce,
        }
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "goalflight_messages.py"),
                "listen",
                "--project-root",
                str(project),
                "--controller-label",
                "armtest",
                "--poll-secs",
                "0.01",
                "--timeout-s",
                "1",
            ],
            env=listener_env,
            capture_output=True,
            text=True,
            timeout=10,
        )

    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert len(lines) == 1, result.stdout
    assert lines[0].startswith("mail available; peek:")
    assert "pending-at-arm" not in result.stdout


def test_report_pending_json_is_jsonl_through_ring(
    isolated: tuple[Path, dict[str, str]],
) -> None:
    project, env = isolated
    authority = journal.open_or_create_journal(project)
    lease = authority.claim_or_renew_lease(
        "armtest", principal={"principal_id": "arm-json-test"}
    ).value
    assert lease is not None
    with wake.register_lease_holder(
        project, controller_label="armtest", lease_nonce=lease.nonce
    ):
        _post(env, project, "json backlog one")
        _post(env, project, "json backlog two")
        listener_env = {
            **env,
            "GOALFLIGHT_CONTROLLER_LABEL": "armtest",
            "GOALFLIGHT_CONTROLLER_LEASE_NONCE": lease.nonce,
        }
        proc = subprocess.Popen(
            [
                sys.executable,
                str(SCRIPTS / "goalflight_messages.py"),
                "listen",
                "--project-root",
                str(project),
                "--controller-label",
                "armtest",
                "--report-pending",
                "--json",
                "--poll-secs",
                "0.01",
            ],
            env=listener_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert proc.stdout is not None
            arm_line = proc.stdout.readline()
            arm_payload = json.loads(arm_line)
            assert arm_payload["kind"] == "pending-at-arm"
            assert len(arm_payload["items"]) == 2

            _post(env, project, "json ring")
            remaining_stdout, _stderr = proc.communicate(timeout=30)
            lines = [arm_line, *remaining_stdout.splitlines(keepends=True)]
            payloads = [json.loads(line) for line in lines if line.strip()]
            assert len(payloads) == 2, lines
            assert payloads[1]["kind"] == "ring"
            assert payloads[1]["reason"] == "event"
            assert proc.returncode == 0
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()


def test_replacement_arm_rings_events_arriving_after_first_report(
    isolated: tuple[Path, dict[str, str]],
) -> None:
    """A superseded re-arm must not swallow mail that arrived after the report.

    --report-pending raises a high-water so the same backlog cannot pop the
    whole pool. That water is the *reported* positions. A replacement arm
    that peeks later must not raise it to the current backlog.
    """
    project, env = isolated
    authority = journal.open_or_create_journal(project)
    lease = authority.claim_or_renew_lease(
        "armtest", principal={"principal_id": "arm-replace-test"}
    ).value
    assert lease is not None
    with wake.register_lease_holder(
        project, controller_label="armtest", lease_nonce=lease.nonce
    ):
        _post(env, project, "reported backlog")
        listener_env = {
            **env,
            "GOALFLIGHT_CONTROLLER_LABEL": "armtest",
            "GOALFLIGHT_CONTROLLER_LEASE_NONCE": lease.nonce,
        }
        first = subprocess.Popen(
            [
                sys.executable,
                str(SCRIPTS / "goalflight_messages.py"),
                "listen",
                "--project-root",
                str(project),
                "--controller-label",
                "armtest",
                "--report-pending",
                "--json",
                "--poll-secs",
                "0.01",
            ],
            env=listener_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        try:
            assert first.stdout is not None
            arm_line = first.stdout.readline()
            arm_payload = json.loads(arm_line)
            assert arm_payload["kind"] == "pending-at-arm"
            first.kill()
            first.wait(timeout=5)

            _post(env, project, "arrived after report")
            replacement = subprocess.Popen(
                [
                    sys.executable,
                    str(SCRIPTS / "goalflight_messages.py"),
                    "listen",
                    "--project-root",
                    str(project),
                    "--controller-label",
                    "armtest",
                    "--report-pending",
                    "--json",
                    "--poll-secs",
                    "0.01",
                    "--timeout-s",
                    "8",
                ],
                env=listener_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                stdout, stderr = replacement.communicate(timeout=15)
            finally:
                if replacement.poll() is None:
                    replacement.kill()
                    replacement.wait()
            assert replacement.returncode == 0, stderr
            payloads = [
                json.loads(line) for line in stdout.splitlines() if line.strip()
            ]
            assert payloads, stdout
            assert payloads[-1]["kind"] == "ring", payloads
            assert payloads[-1]["reason"] == "event"
            assert all(payload.get("kind") != "pending-at-arm" for payload in payloads)
        finally:
            if first.poll() is None:
                first.kill()
                first.wait()

        # Deliberate listen-auto invocation: deployed controllers still arm
        # with the alias; this locks back-compat rather than leaving it accidental.
        timeout_result = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "goalflight_messages.py"),
                "listen-auto",
                "--project-root",
                str(project),
                "--controller-label",
                "armtest",
                "--report-pending",
                "--json",
                "--poll-secs",
                "0.01",
                "--timeout-s",
                "0.05",
            ],
            env=listener_env,
            capture_output=True,
            text=True,
            timeout=10,
        )

    timeout_payloads = [
        json.loads(line) for line in timeout_result.stdout.splitlines() if line.strip()
    ]
    assert timeout_result.returncode == 1, timeout_result.stderr
    # Same lease generation already emitted the backlog. A later arm stays
    # silent and only JSONL-exits on timeout; reprinting would spend the
    # report (and, in a pool, the other doorbells) on mail already seen.
    assert [payload["kind"] for payload in timeout_payloads] == ["exit"]
    assert timeout_payloads[-1]["reason"] == "timeout"


def main() -> None:
    raise SystemExit(pytest.main([__file__, "-q"]))


if __name__ == "__main__":
    main()
