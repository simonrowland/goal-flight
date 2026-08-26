"""Opt-in arm-reports-pending listener behavior and exit-driven compatibility.

Live-verified 2026-08-15 (operator-designed semantics: the arm doubles as the
peek, and a controller that is awake enough to arm does not need the pop).

NOTE: `--report-pending` is now the DEFAULT, because a backlog left the
bare path ringing every armed doorbell at once — four doorbells against a
13-message backlog all fired immediately and coverage went to zero. The two
tests below still cover the legacy ring shape and now request it explicitly
with `--no-report-pending`, so both paths stay tested.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import goalflight_journal as journal  # noqa: E402
import goalflight_messages as messages  # noqa: E402
import goalflight_wake as wake  # noqa: E402


_DELAY_FIRST_LISTENER_PEEK = r"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.environ["GOALFLIGHT_TEST_SCRIPTS"])
import goalflight_journal as journal

original_cursor_peek = journal.Journal.cursor_peek
first_peek = True

def delayed_cursor_peek(self, *args, **kwargs):
    global first_peek
    if first_peek:
        first_peek = False
        Path(os.environ["GOALFLIGHT_TEST_PEEK_READY"]).write_text("ready\n")
        release = Path(os.environ["GOALFLIGHT_TEST_PEEK_RELEASE"])
        deadline = time.monotonic() + 3
        while not release.exists() and time.monotonic() < deadline:
            time.sleep(0.005)
        if not release.exists():
            raise RuntimeError("test did not release the listener arm snapshot")
    return original_cursor_peek(self, *args, **kwargs)

journal.Journal.cursor_peek = delayed_cursor_peek
import goalflight_messages
raise SystemExit(goalflight_messages.main(sys.argv[1:]))
"""


_FAIL_ARM_AFTER_PENDING_CLAIM = r"""
import os
import sys

sys.path.insert(0, os.environ["GOALFLIGHT_TEST_SCRIPTS"])
import goalflight_journal as journal

def fail_arm_after_claim(self, *args, **kwargs):
    raise journal.JournalUnavailable("injected failure after pending claim")

journal.Journal.arm_listener = fail_arm_after_claim
import goalflight_messages
raise SystemExit(goalflight_messages.main(sys.argv[1:]))
"""


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


def _post(
    env: dict[str, str],
    project: Path,
    text: str,
    *,
    dispatch_id: str = "arm-backlog",
) -> None:
    subprocess.run(
        [sys.executable, str(SCRIPTS / "goalflight_messages.py"), "post",
         "--to-controller", "armtest", "--dispatch-id", dispatch_id,
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


def test_default_ring_lists_entire_backlog_once_and_advances_every_stream(
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
        listener_env = {
            **env,
            "GOALFLIGHT_CONTROLLER_LABEL": "armtest",
            "GOALFLIGHT_CONTROLLER_LEASE_NONCE": lease.nonce,
        }
        processes = [
            subprocess.Popen(
                [
                    sys.executable,
                    str(SCRIPTS / "goalflight_messages.py"),
                    "listen",
                    "--project-root",
                    str(project),
                    "--controller-label",
                    "armtest",
                    "--listener-slots",
                    "4",
                    "--poll-secs",
                    "5",
                    "--no-report-pending",
                "--timeout-s",
                    "30",
                ],
                env=listener_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _index in range(4)
        ]
        try:
            arm_deadline = time.monotonic() + 10
            live_waiters = []
            while time.monotonic() < arm_deadline:
                live_waiters = wake.live_waiters(
                    project, controller_label="armtest"
                ) or []
                if len(live_waiters) == 4:
                    break
                time.sleep(0.02)
            assert len(live_waiters) == 4
            for index in range(1, 11):
                _post(
                    env,
                    project,
                    f"buffered event {index}",
                    dispatch_id=f"ev-{index}",
                )

            deadline = time.monotonic() + 10
            exited: list[subprocess.Popen[str]] = []
            while time.monotonic() < deadline:
                exited = [process for process in processes if process.poll() is not None]
                if exited:
                    break
                time.sleep(0.02)
            assert len(exited) == 1, [process.poll() for process in processes]
            time.sleep(0.2)
            assert len([process for process in processes if process.poll() is not None]) == 1

            winner = exited[0]
            stdout, stderr = winner.communicate(timeout=5)
            assert winner.returncode == 0, stderr
            lines = stdout.splitlines()
            receipt_lines = [line for line in lines if line.startswith("[controller-notice]")]
            assert len(receipt_lines) == 10, stdout
            for index in range(1, 11):
                assert (
                    f"[controller-notice] ev-{index} seq=1 — buffered event {index}"
                    in receipt_lines
                )
            advance_lines = [line for line in lines if line.startswith("advance: ")]
            assert len(advance_lines) == 1, stdout
            for index in range(1, 11):
                assert f"ev-{index}=1" in advance_lines[0]
            assert "mail available; peek:" not in stdout
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=5)


def test_default_ring_filters_controller_authored_items_from_listing(
    isolated: tuple[Path, dict[str, str]],
) -> None:
    project, env = isolated
    authority = journal.open_or_create_journal(project)
    lease = authority.claim_or_renew_lease(
        "armtest", principal={"principal_id": "arm-filter-test"}
    ).value
    assert lease is not None
    with wake.register_lease_holder(
        project, controller_label="armtest", lease_nonce=lease.nonce
    ):
        own_envelope = messages.post_message(
            dispatch_id="self-authored",
            msg_type="controller-notice",
            payload={"text": "private own traffic"},
            messages_dir=Path(env["GOALFLIGHT_MESSAGES_DIR"]),
            source={
                "node": "local",
                "adapter": "pytest",
                "transport": "controller",
                "controller_label": "armtest",
            },
            author_capability=lease.nonce,
            addressee=messages.controller_addressee(
                "armtest", project_root=project
            ),
        )["envelope"]
        assert messages.envelope_authored_by_controller(
            own_envelope,
            controller_label="armtest",
            lease_nonce=lease.nonce,
        )
        _post(env, project, "foreign traffic", dispatch_id="foreign")
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
                "--no-report-pending",
                "--timeout-s",
                "5",
            ],
            env=listener_env,
            capture_output=True,
            text=True,
            timeout=10,
        )

    assert result.returncode == 0, result.stderr
    assert "[controller-notice] foreign seq=1 — foreign traffic" in result.stdout
    assert "self-authored" not in "\n".join(
        line for line in result.stdout.splitlines() if line.startswith("[")
    )
    advance_lines = [
        line for line in result.stdout.splitlines() if line.startswith("advance: ")
    ]
    assert len(advance_lines) == 1, result.stdout
    assert "self-authored=1" in advance_lines[0]
    assert "foreign=1" in advance_lines[0]


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
        for index in range(1, 10):
            _post(env, project, f"json backlog {index}")
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
            assert len(arm_payload["items"]) == 9

            _post(env, project, "json ring 10")
            remaining_stdout, _stderr = proc.communicate(timeout=30)
            lines = [arm_line, *remaining_stdout.splitlines(keepends=True)]
            payloads = [json.loads(line) for line in lines if line.strip()]
            assert len(payloads) == 2, lines
            assert payloads[1]["kind"] == "ring"
            assert payloads[1]["reason"] == "event"
            # The arm payload carries items because an arm IS a peek. The ring
            # deliberately does not: it is a wake signal plus the instructions
            # to drain, and its keys are each justified as not-a-mail-body in
            # test_goalflight_p3's body-free contract. This asymmetry is easiest
            # to get wrong here, where both payloads are visible at once, so
            # pin it rather than leaving a gap.
            assert "items" not in payloads[1]
            assert payloads[1]["advance_command"]
            assert proc.returncode == 0
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()


def test_unread_reported_flush_is_re_reported_after_reporter_dies(
    isolated: tuple[Path, dict[str, str]],
) -> None:
    """A local stdout flush is not evidence that its reader received the line."""
    project, env = isolated
    authority = journal.open_or_create_journal(project)
    lease = authority.claim_or_renew_lease(
        "armtest", principal={"principal_id": "arm-unread-report-test"}
    ).value
    assert lease is not None
    listener_env = {
        **env,
        "GOALFLIGHT_CONTROLLER_LABEL": "armtest",
        "GOALFLIGHT_CONTROLLER_LEASE_NONCE": lease.nonce,
    }

    with wake.register_lease_holder(
        project, controller_label="armtest", lease_nonce=lease.nonce
    ):
        _post(env, project, "unread backlog", dispatch_id="unread-report")
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
            # Deliberately never read first.stdout. Wait only for the writer's
            # durable local-flush phase, then discard the unread pipe.
            report_deadline = time.monotonic() + 5
            while time.monotonic() < report_deadline:
                state = wake.pending_report_state(
                    project,
                    controller_label="armtest",
                    lease_nonce=lease.nonce,
                )
                if state is not None and state.phase == "reported":
                    break
                time.sleep(0.005)
            else:
                pytest.fail("unread pending-at-arm output never reached reported phase")
            first.kill()
            first.wait(timeout=5)
            assert first.stdout is not None
            first.stdout.close()

            replacement = subprocess.run(
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
                    "0.1",
                ],
                env=listener_env,
                capture_output=True,
                text=True,
                timeout=10,
            )
        finally:
            if first.poll() is None:
                first.kill()
                first.wait()

    payloads = [
        json.loads(line) for line in replacement.stdout.splitlines() if line.strip()
    ]
    assert replacement.returncode == 1, replacement.stderr
    assert [payload["kind"] for payload in payloads] == ["pending-at-arm", "exit"]
    assert [int(item["stream_seq"]) for item in payloads[0]["items"]] == [1]
    assert payloads[-1]["reason"] == "timeout"


def test_rearm_stays_armed_when_committed_cursor_did_not_settle_sidecar(
    isolated: tuple[Path, dict[str, str]],
) -> None:
    """A committed CAS that skipped sidecar ack is already-consumed, not undelivered.

    Controllers re-arm after every ring. If takeover reconstructs a claim the
    cursor already consumed, the replacement exits 2 and the generation dies
    on the next re-arm instead of staying armed.
    """
    project, env = isolated
    authority = journal.open_or_create_journal(project)
    lease = authority.claim_or_renew_lease(
        "armtest", principal={"principal_id": "arm-consumed-claim-test"}
    ).value
    assert lease is not None
    listener_env = {
        **env,
        "GOALFLIGHT_CONTROLLER_LABEL": "armtest",
        "GOALFLIGHT_CONTROLLER_LEASE_NONCE": lease.nonce,
    }
    dispatch_id = "consumed-claim"

    with wake.register_lease_holder(
        project, controller_label="armtest", lease_nonce=lease.nonce
    ):
        _post(env, project, "consumed backlog", dispatch_id=dispatch_id)
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
            report = json.loads(first.stdout.readline())
            assert report["kind"] == "pending-at-arm"
            report_deadline = time.monotonic() + 5
            while time.monotonic() < report_deadline:
                state = wake.pending_report_state(
                    project,
                    controller_label="armtest",
                    lease_nonce=lease.nonce,
                )
                if state is not None and state.phase == "reported":
                    break
                time.sleep(0.005)
            else:
                pytest.fail("pending-at-arm never reached reported phase")

            peek = authority.cursor_peek("armtest", nonce=lease.nonce)
            advances = {
                str(row["stream_id"]): int(row["stream_seq"]) for row in peek.items
            }
            assert advances == {dispatch_id: 1}
            committed = authority.advance_cursor(
                "armtest",
                nonce=lease.nonce,
                expected_cursor_version=peek.cursor_version,
                expected_stream_snapshots=peek.stream_snapshots,
                advances=advances,
                actor="consumed-claim-without-sidecar",
            )
            assert committed.committed
            unsynced = wake.pending_report_state(
                project,
                controller_label="armtest",
                lease_nonce=lease.nonce,
            )
            assert unsynced is not None
            assert unsynced.phase == "reported"
            first.kill()
            first.wait(timeout=5)

            _post(env, project, "new after unsynced advance", dispatch_id=dispatch_id)
            replacement = subprocess.run(
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
                capture_output=True,
                text=True,
                timeout=15,
            )
        finally:
            if first.poll() is None:
                first.kill()
                first.wait()

    payloads = [
        json.loads(line) for line in replacement.stdout.splitlines() if line.strip()
    ]
    assert replacement.returncode == 0, (replacement.stderr, payloads)
    assert payloads, replacement.stdout
    assert all(payload.get("kind") != "pending-at-arm" for payload in payloads)
    assert payloads[-1]["kind"] == "ring"
    assert payloads[-1]["reason"] == "event"
    settled = wake.pending_report_state(
        project,
        controller_label="armtest",
        lease_nonce=lease.nonce,
    )
    assert settled is not None
    assert settled.phase == "acknowledged"


def test_replacement_arm_rings_events_arriving_after_first_report(
    isolated: tuple[Path, dict[str, str]],
) -> None:
    """A superseded re-arm must not swallow mail that arrived after the report.

    --report-pending raises a high-water so the same backlog cannot pop the
    whole pool. That water is the fixed claim boundary. A replacement arm
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
            # Delivery is not durable merely because the pipe exposed a line.
            # Wait for the reporter's fsynced phase transition before modeling
            # an exit *after* the first report.
            report_deadline = time.monotonic() + 2
            while time.monotonic() < report_deadline:
                state = wake.pending_report_state(
                    project,
                    controller_label="armtest",
                    lease_nonce=lease.nonce,
                )
                if state is not None and state.phase == "reported":
                    break
                time.sleep(0.005)
            else:
                pytest.fail("pending-at-arm output never became durably reported")
            advance_argv = shlex.split(arm_payload["advance_command"])
            acknowledged = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "goalflight_messages.py"),
                    *advance_argv[2:],
                ],
                cwd=project,
                env=listener_env,
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert acknowledged.returncode == 0, acknowledged.stderr
            state = wake.pending_report_state(
                project,
                controller_label="armtest",
                lease_nonce=lease.nonce,
            )
            assert state is not None
            assert state.phase == "acknowledged"
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
        # Same lease generation already acknowledged the backlog. A later arm
        # stays silent and only JSONL-exits on timeout; reprinting would spend
        # the pool's output on mail already settled by the controller.
    assert [payload["kind"] for payload in timeout_payloads] == ["exit"]
    assert timeout_payloads[-1]["reason"] == "timeout"


def test_replacement_takes_over_claim_when_first_arm_dies_before_report(
    isolated: tuple[Path, dict[str, str]],
) -> None:
    """An unreported claim keeps its original water and remains deliverable.

    This is the production order: the first process snapshots and persists its
    claim, then ``Journal.arm_listener`` fails before stdout can carry the
    ``pending-at-arm`` record. Mail posted after that durable boundary must not
    be folded into a replacement's local snapshot.
    """
    project, env = isolated
    authority = journal.open_or_create_journal(project)
    lease = authority.claim_or_renew_lease(
        "armtest", principal={"principal_id": "arm-claim-takeover-test"}
    ).value
    assert lease is not None
    listener_env = {
        **env,
        "GOALFLIGHT_CONTROLLER_LABEL": "armtest",
        "GOALFLIGHT_CONTROLLER_LEASE_NONCE": lease.nonce,
        "GOALFLIGHT_TEST_SCRIPTS": str(SCRIPTS),
    }
    dispatch_id = "claim-takeover"

    with wake.register_lease_holder(
        project, controller_label="armtest", lease_nonce=lease.nonce
    ):
        _post(env, project, "backlog before failed arm", dispatch_id=dispatch_id)
        failed = subprocess.run(
            [
                sys.executable,
                "-c",
                _FAIL_ARM_AFTER_PENDING_CLAIM,
                "listen",
                "--project-root",
                str(project),
                "--controller-label",
                "armtest",
                "--json",
            ],
            env=listener_env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert failed.returncode == 2, failed.stderr
        assert not failed.stdout.strip(), failed.stdout
        assert wake.pending_report_high_water(
            project,
            controller_label="armtest",
            lease_nonce=lease.nonce,
        ) == {dispatch_id: 1}
        claimed = wake.pending_report_state(
            project,
            controller_label="armtest",
            lease_nonce=lease.nonce,
        )
        assert claimed is not None
        assert claimed.phase == "claimed"

        # This is genuinely new mail relative to the persisted first snapshot.
        # A replacement must report only seq=1, then ring for seq=2.
        _post(env, project, "new after failed arm", dispatch_id=dispatch_id)
        replacement = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "goalflight_messages.py"),
                "listen",
                "--project-root",
                str(project),
                "--controller-label",
                "armtest",
                "--json",
                "--poll-secs",
                "0.01",
                "--timeout-s",
                "3",
            ],
            env=listener_env,
            capture_output=True,
            text=True,
            timeout=10,
        )

    payloads = [
        json.loads(line) for line in replacement.stdout.splitlines() if line.strip()
    ]
    assert replacement.returncode == 0, (replacement.stderr, payloads)
    assert [payload["kind"] for payload in payloads] == ["pending-at-arm", "ring"]
    assert [int(item["stream_seq"]) for item in payloads[0]["items"]] == [1]
    assert payloads[1]["reason"] == "event"
    assert f"{dispatch_id}=2" in payloads[1]["advance_command"]
    reported = wake.pending_report_state(
        project,
        controller_label="armtest",
        lease_nonce=lease.nonce,
    )
    assert reported is not None
    assert reported.phase == "reported"


def test_dead_claim_takeover_reports_once_without_spending_listener_pool(
    isolated: tuple[Path, dict[str, str]],
) -> None:
    project, env = isolated
    authority = journal.open_or_create_journal(project)
    lease = authority.claim_or_renew_lease(
        "armtest", principal={"principal_id": "arm-claim-pool-test"}
    ).value
    assert lease is not None
    listener_env = {
        **env,
        "GOALFLIGHT_CONTROLLER_LABEL": "armtest",
        "GOALFLIGHT_CONTROLLER_LEASE_NONCE": lease.nonce,
        "GOALFLIGHT_TEST_SCRIPTS": str(SCRIPTS),
    }
    output_paths = [project.parent / f"takeover-{index}.jsonl" for index in range(4)]
    processes: list[subprocess.Popen[str]] = []

    with wake.register_lease_holder(
        project, controller_label="armtest", lease_nonce=lease.nonce
    ):
        _post(env, project, "pool backlog", dispatch_id="claim-pool")
        failed = subprocess.run(
            [
                sys.executable,
                "-c",
                _FAIL_ARM_AFTER_PENDING_CLAIM,
                "listen",
                "--project-root",
                str(project),
                "--controller-label",
                "armtest",
                "--json",
            ],
            env=listener_env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert failed.returncode == 2, failed.stderr

        try:
            for path in output_paths:
                handle = path.open("w", encoding="utf-8")
                processes.append(
                    subprocess.Popen(
                        [
                            sys.executable,
                            str(SCRIPTS / "goalflight_messages.py"),
                            "listen",
                            "--project-root",
                            str(project),
                            "--controller-label",
                            "armtest",
                            "--listener-slots",
                            "4",
                            "--json",
                            "--poll-secs",
                            "0.01",
                            "--timeout-s",
                            "10",
                        ],
                        env=listener_env,
                        stdout=handle,
                        stderr=subprocess.DEVNULL,
                        text=True,
                    )
                )
                handle.close()

            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                live = wake.live_waiters(
                    project,
                    controller_label="armtest",
                    kinds={"listener"},
                ) or []
                if len(live) == 4:
                    break
                time.sleep(0.02)
            assert len(live) == 4
            time.sleep(0.2)
            payloads_before_new = [
                json.loads(line)
                for path in output_paths
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            assert [
                payload["kind"] for payload in payloads_before_new
            ].count("pending-at-arm") == 1
            assert all(process.poll() is None for process in processes)

            _post(env, project, "new after pool takeover", dispatch_id="claim-pool")
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                exited = [process for process in processes if process.poll() is not None]
                if exited:
                    break
                time.sleep(0.02)
            assert len(exited) == 1
            assert exited[0].returncode == 0
            live = wake.live_waiters(
                project,
                controller_label="armtest",
                kinds={"listener"},
            ) or []
            assert len(live) == 3
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)


@pytest.mark.parametrize("run", range(1, 4))
def test_rearmed_listener_with_unread_backlog_rings_new_event_three_runs(
    isolated: tuple[Path, dict[str, str]],
    run: int,
) -> None:
    """Coverage cannot become visible before its backlog threshold is fixed."""
    project, env = isolated
    authority = journal.open_or_create_journal(project)
    lease = authority.claim_or_renew_lease(
        "armtest", principal={"principal_id": f"arm-production-rearm-{run}"}
    ).value
    assert lease is not None
    listener_env = {
        **env,
        "GOALFLIGHT_CONTROLLER_LABEL": "armtest",
        "GOALFLIGHT_CONTROLLER_LEASE_NONCE": lease.nonce,
    }
    dispatch_id = f"production-rearm-{run}"
    peek_ready = project / "peek-ready"
    peek_release = project / "peek-release"
    listener_env.update(
        {
            "GOALFLIGHT_TEST_SCRIPTS": str(SCRIPTS),
            "GOALFLIGHT_TEST_PEEK_READY": str(peek_ready),
            "GOALFLIGHT_TEST_PEEK_RELEASE": str(peek_release),
        }
    )
    command = [
        sys.executable,
        "-c",
        _DELAY_FIRST_LISTENER_PEEK,
        "listen",
        "--project-root",
        str(project),
        "--controller-label",
        "armtest",
        "--json",
        "--poll-secs",
        "0.01",
        "--timeout-s",
        "2",
    ]

    with wake.register_lease_holder(
        project, controller_label="armtest", lease_nonce=lease.nonce
    ):
        _post(env, project, "backlog already pending", dispatch_id=dispatch_id)
        replacement = subprocess.Popen(
            command,
            env=listener_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and not peek_ready.exists():
                time.sleep(0.01)
            assert peek_ready.exists(), "listener never reached its arm snapshot"

            coverage = authority.active_coverage("armtest")
            coverage_was_visible_before_snapshot = bool(
                coverage is not None and coverage["pid"] == replacement.pid
            )
            if coverage_was_visible_before_snapshot:
                _post(
                    env,
                    project,
                    "new event after replacement arm",
                    dispatch_id=dispatch_id,
                )
                peek_release.write_text("release\n", encoding="utf-8")
            else:
                peek_release.write_text("release\n", encoding="utf-8")
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    coverage = authority.active_coverage("armtest")
                    if coverage is not None and coverage["pid"] == replacement.pid:
                        break
                    time.sleep(0.01)
                else:
                    pytest.fail("production-shape replacement never armed")
                _post(
                    env,
                    project,
                    "new event after replacement arm",
                    dispatch_id=dispatch_id,
                )

            stdout, stderr = replacement.communicate(timeout=3)
            arm_high = wake.pending_report_high_water(
                project,
                controller_label="armtest",
                lease_nonce=lease.nonce,
            )
            pending = authority.cursor_peek("armtest", nonce=lease.nonce)
            event_positions = {
                str(item["stream_id"]): int(item["stream_seq"])
                for item in pending.items
            }
            print(
                "PRODUCTION_REARM "
                f"run={run} arm_high={arm_high} "
                f"event_positions={event_positions} "
                f"cursor_version={pending.cursor_version} "
                f"coverage_before_snapshot={coverage_was_visible_before_snapshot}"
            )
            payloads = [
                json.loads(line) for line in stdout.splitlines() if line.strip()
            ]
            assert coverage_was_visible_before_snapshot is False
            assert arm_high == {dispatch_id: 1}
            assert replacement.returncode == 0, (stderr, payloads)
            assert payloads[0]["kind"] == "pending-at-arm", payloads
            assert payloads[-1]["kind"] == "ring", payloads
            assert payloads[-1]["reason"] == "event"
            assert f"{dispatch_id}=2" in payloads[-1]["advance_command"]
        finally:
            peek_release.write_text("release\n", encoding="utf-8")
            if replacement.poll() is None:
                replacement.kill()
                replacement.communicate(timeout=3)


def main() -> None:
    raise SystemExit(pytest.main([__file__, "-q"]))


if __name__ == "__main__":
    main()
