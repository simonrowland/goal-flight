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
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from machine_isolation import AMBIENT_IDENTITY_ENV, isolated_machine_env, wait_until

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import goalflight_journal as journal  # noqa: E402
import goalflight_messages as messages  # noqa: E402
import goalflight_session_status as sessions  # noqa: E402
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
    raise journal.JournalIOError("injected failure after pending claim")

journal.Journal.arm_listener = fail_arm_after_claim
import goalflight_messages
raise SystemExit(goalflight_messages.main(sys.argv[1:]))
"""


_GATE_PENDING_REPORT_CLAIM = r"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.environ["GOALFLIGHT_TEST_SCRIPTS"])
import goalflight_wake

original_acquire = goalflight_wake.acquire_pending_report
ready = Path(os.environ["GOALFLIGHT_TEST_PENDING_REPORT_READY"])
release = Path(os.environ["GOALFLIGHT_TEST_PENDING_REPORT_RELEASE"])

def gated_acquire(*args, **kwargs):
    temporary = ready.with_suffix(".tmp")
    temporary.write_text("ready\n", encoding="utf-8")
    temporary.replace(ready)
    deadline = time.monotonic() + 45
    while not release.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not release.exists():
        raise RuntimeError("test did not release concurrent pending-report claims")
    return original_acquire(*args, **kwargs)

goalflight_wake.acquire_pending_report = gated_acquire
import goalflight_messages
result = goalflight_messages.main(sys.argv[1:])
finished_value = os.environ.get("GOALFLIGHT_TEST_LISTENER_FINISHED")
finish_release_value = os.environ.get("GOALFLIGHT_TEST_LISTENER_FINISH_RELEASE")
if finished_value and finish_release_value:
    # Keep a ringed claimant alive during the test's observation window; its
    # normal exit would allow the documented at-least-once takeover duplicate.
    finished = Path(finished_value)
    release_after_listen = Path(finish_release_value)
    temporary = finished.with_suffix(".tmp")
    temporary.write_text("finished\n", encoding="utf-8")
    temporary.replace(finished)
    deadline = time.monotonic() + 15
    while not release_after_listen.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not release_after_listen.exists():
        raise RuntimeError("test did not release the finished listener process")
raise SystemExit(result)
"""


_UNREPORTABLE_CARRIER = r"""
import os
import sys

sys.path.insert(0, os.environ["GOALFLIGHT_TEST_SCRIPTS"])
import goalflight_messages

real_read = goalflight_messages.read_envelopes_result
read_state = {"failed": False}

def unreadable_once(path, *args, **kwargs):
    if not read_state["failed"]:
        read_state["failed"] = True
        return goalflight_messages.CarrierReadResult(
            goalflight_messages.CarrierReadStatus.CARRIER_UNREADABLE,
            (),
            ({"error": f"{path}: injected unreadable", "reason": "injected unreadable"},),
        )
    return real_read(path, *args, **kwargs)

goalflight_messages.read_envelopes_result = unreadable_once

raise SystemExit(goalflight_messages.main(sys.argv[1:]))
"""


_MISSING_CARRIER_LISTENER = r"""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.environ["GOALFLIGHT_TEST_SCRIPTS"])
import goalflight_messages

real_read = goalflight_messages.read_envelopes_result
seen = Path(os.environ["GOALFLIGHT_TEST_MISSING_CARRIER_SEEN"])

def mark_missing(path, *args, **kwargs):
    result = real_read(path, *args, **kwargs)
    if result.status is goalflight_messages.CarrierReadStatus.CARRIER_MISSING:
        seen.write_text("seen\n")
    return result

goalflight_messages.read_envelopes_result = mark_missing
raise SystemExit(goalflight_messages.main(sys.argv[1:]))
"""


_UNREADABLE_CURSOR_STATUS = r"""
import os
import sys

sys.path.insert(0, os.environ["GOALFLIGHT_TEST_SCRIPTS"])
import goalflight_journal as journal

def fail_cursor_status(self, *args, **kwargs):
    raise RuntimeError("injected unreadable cursor")

journal.Journal.cursor_status = fail_cursor_status
import goalflight_messages
raise SystemExit(goalflight_messages.main(sys.argv[1:]))
"""


_UNREADABLE_LEASE_PROBE = r"""
import os
import sys

sys.path.insert(0, os.environ["GOALFLIGHT_TEST_SCRIPTS"])
import goalflight_journal as journal
import goalflight_messages as messages

def busy_open_reader(cls, project_root, **kwargs):
    raise journal.JournalBusy(
        "journal connection remained busy after 1 attempts within 1.000s"
    )

journal.Journal.open_reader = classmethod(busy_open_reader)
messages.LISTENER_JOURNAL_TOLERANCE_S = 0.05
messages.LISTENER_JOURNAL_BUSY_BUDGET_S = 0.01
raise SystemExit(messages.main(sys.argv[1:]))
"""


@pytest.fixture()
def isolated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[Path, dict[str, str]]:
    env = isolated_machine_env(tmp_path)
    for key in AMBIENT_IDENTITY_ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("GOALFLIGHT_WAKE_LEDGER", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    project = tmp_path / "project"
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
            assert proc.poll() is None, "listener popped on the arm-time backlog"

            # An event beyond the arm-time high-water must ring.
            _post(env, project, "the new event")
            proc.wait(timeout=30)
            assert proc.returncode == 0
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()


def test_ring_pass_skips_reported_backlog_before_peek_limit(
    isolated: tuple[Path, dict[str, str]],
) -> None:
    project, env = isolated
    authority = journal.open_or_create_journal(project)
    lease = authority.claim_or_renew_lease(
        "armtest", principal={"principal_id": "arm-reported-limit-test"}
    ).value
    assert lease is not None
    messages_dir = Path(env["GOALFLIGHT_MESSAGES_DIR"])
    addressee = messages.controller_addressee("armtest", project_root=project)
    with wake.register_lease_holder(
        project, controller_label="armtest", lease_nonce=lease.nonce
    ):
        for index in range(1000):
            messages.post_message(
                dispatch_id=f"old-backlog-{index:04d}",
                msg_type="controller-notice",
                payload={"text": f"old event {index}"},
                messages_dir=messages_dir,
                source={"node": "peer", "adapter": "pytest", "transport": "controller"},
                addressee=addressee,
                event_ts="2020-01-01T00:00:00Z",
            )
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
                "--timeout-s",
                "30",
            ],
            env=listener_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert proc.stdout is not None
            arm_report = json.loads(proc.stdout.readline())
            assert arm_report["kind"] == "pending-at-arm"
            assert len(arm_report["items"]) == 1000
            assert proc.poll() is None, "live reporter exited before the later event"
            report_state = wait_until(
                lambda: (
                    state
                    if (state := wake.pending_report_state(
                        project,
                        controller_label="armtest",
                        lease_nonce=lease.nonce,
                    ))
                    and state.phase == "reported"
                    else None
                ),
                timeout_s=5,
                message="live listener report to reach the reported phase",
            )
            assert report_state.phase == "reported"
            assert len(report_state.positions) == 1000
            assert proc.poll() is None, "reported owner exited before the later event"

            messages.post_message(
                dispatch_id="zzzz-new-event",
                msg_type="controller-notice",
                payload={"text": "new event after reported backlog"},
                messages_dir=messages_dir,
                source={"node": "peer", "adapter": "pytest", "transport": "controller"},
                addressee=addressee,
            )
            wait_until(
                lambda: proc.poll() is not None,
                timeout_s=35,
                message="listener to finish after the event beyond reported backlog",
            )
            remaining_stdout, stderr = proc.communicate()
            payloads = [
                json.loads(line)
                for line in remaining_stdout.splitlines()
                if line.strip()
            ]
            assert proc.returncode == 0, (stderr, payloads)
            assert payloads[0]["kind"] == "ring", payloads
            assert "zzzz-new-event=1" in payloads[0]["advance_command"]
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate(timeout=5)


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
        # Seed the entire backlog before arming so every listener observes one
        # cursor version. Posting after arm, while some peeks are still in
        # flight, splits the cursor and two doorbells can both claim a ring.
        for index in range(1, 11):
            _post(
                env,
                project,
                f"buffered event {index}",
                dispatch_id=f"ev-{index}",
            )
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
                    "60",
                ],
                env=listener_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _index in range(4)
        ]
        try:
            def _ring_invariant() -> tuple[int, list[subprocess.Popen[str]]] | None:
                live = wake.live_waiters(project, controller_label="armtest") or []
                exited = [process for process in processes if process.poll() is not None]
                if len(live) == 3 and len(exited) == 1:
                    return (len(live), exited)
                return None

            live_count, exited = wait_until(
                _ring_invariant,
                timeout_s=30,
                message="one of four listeners to ring and three slot locks to remain",
            )
            assert live_count == 3
            assert len(exited) == 1, [process.poll() for process in processes]

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



def test_missing_carrier_is_rechecked_until_restored(
    isolated: tuple[Path, dict[str, str]],
) -> None:
    project, env = isolated
    authority = journal.open_or_create_journal(project)
    lease = authority.claim_or_renew_lease(
        "armtest", principal={"principal_id": "arm-missing-carrier-retry-test"}
    ).value
    assert lease is not None
    dispatch_id = "restorable-carrier"
    carrier = messages.inbox_path(Path(env["GOALFLIGHT_MESSAGES_DIR"]), dispatch_id)
    _post(env, project, "restore this carrier", dispatch_id=dispatch_id)
    original = carrier.read_bytes()
    carrier.unlink()
    seen = project / "missing-carrier-seen"
    listener_env = {
        **env,
        "GOALFLIGHT_CONTROLLER_LABEL": "armtest",
        "GOALFLIGHT_CONTROLLER_LEASE_NONCE": lease.nonce,
        "GOALFLIGHT_TEST_SCRIPTS": str(SCRIPTS),
        "GOALFLIGHT_TEST_MISSING_CARRIER_SEEN": str(seen),
    }

    with wake.register_lease_holder(
        project, controller_label="armtest", lease_nonce=lease.nonce
    ):
        listener = subprocess.Popen(
            [
                sys.executable,
                "-c",
                _MISSING_CARRIER_LISTENER,
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
            wait_until(
                lambda: seen if seen.exists() else None,
                timeout_s=5,
                message="listener never observed missing carrier",
            )
            carrier.write_bytes(original)
            stdout, stderr = listener.communicate(timeout=15)
        finally:
            if listener.poll() is None:
                listener.kill()
                listener.wait(timeout=5)

    payloads = [json.loads(line) for line in stdout.splitlines() if line.strip()]
    assert listener.returncode == 0, (stderr, payloads)
    assert payloads[-1]["kind"] == "ring", payloads
    assert f"{dispatch_id}=1" in payloads[-1]["advance_command"]


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


def test_rearm_reports_unconsumed_remainder_when_cursor_covers_a_subset(
    isolated: tuple[Path, dict[str, str]],
) -> None:
    """Subset CAS must not poison the replacement listener.

    Two streams in one claim, cursor advances only A. Reconstructing the
    original {A,B} boundary fails, and mapping that to journal-unavailable
    exit 2 makes every re-arm die while B stays muted behind arm_high.
    """
    project, env = isolated
    authority = journal.open_or_create_journal(project)
    lease = authority.claim_or_renew_lease(
        "armtest", principal={"principal_id": "arm-subset-claim-test"}
    ).value
    assert lease is not None
    listener_env = {
        **env,
        "GOALFLIGHT_CONTROLLER_LABEL": "armtest",
        "GOALFLIGHT_CONTROLLER_LEASE_NONCE": lease.nonce,
    }
    dispatch_a = "subset-stream-a"
    dispatch_b = "subset-stream-b"

    with wake.register_lease_holder(
        project, controller_label="armtest", lease_nonce=lease.nonce
    ):
        _post(env, project, "consumed stream a", dispatch_id=dispatch_a)
        _post(env, project, "pending stream b", dispatch_id=dispatch_b)
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
            reported_streams = {
                str(item["stream_id"]) for item in report["items"]
            }
            assert reported_streams == {dispatch_a, dispatch_b}
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
            assert advances == {dispatch_a: 1, dispatch_b: 1}
            committed = authority.advance_cursor(
                "armtest",
                nonce=lease.nonce,
                expected_cursor_version=peek.cursor_version,
                expected_stream_snapshots={dispatch_a: peek.stream_snapshots[dispatch_a]},
                advances={dispatch_a: 1},
                actor="subset-consumed-without-sidecar",
            )
            assert committed.committed
            unsynced = wake.pending_report_state(
                project,
                controller_label="armtest",
                lease_nonce=lease.nonce,
            )
            assert unsynced is not None
            assert unsynced.phase == "reported"
            assert unsynced.positions == {dispatch_a: 1, dispatch_b: 1}
            first.kill()
            first.wait(timeout=5)

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
                assert replacement.stdout is not None
                remainder_report = json.loads(replacement.stdout.readline())
                assert remainder_report["kind"] == "pending-at-arm", (
                    remainder_report,
                    replacement.stderr.read() if replacement.poll() is not None else "",
                )
                remainder_streams = [
                    str(item["stream_id"]) for item in remainder_report["items"]
                ]
                assert remainder_streams == [dispatch_b]
                assert replacement.poll() is None
                coverage = authority.active_coverage("armtest")
                assert coverage is not None
                assert coverage["state"] == "ARMED"
                reduced = wake.pending_report_state(
                    project,
                    controller_label="armtest",
                    lease_nonce=lease.nonce,
                )
                assert reduced is not None
                assert reduced.positions == {dispatch_b: 1}
                assert dispatch_a not in reduced.positions

                _post(env, project, "new after subset re-arm", dispatch_id=dispatch_a)
                remaining_stdout, remaining_stderr = replacement.communicate(timeout=15)
            finally:
                if replacement.poll() is None:
                    replacement.kill()
                    replacement.wait()
        finally:
            if first.poll() is None:
                first.kill()
                first.wait()

    payloads = [
        json.loads(line)
        for line in remaining_stdout.splitlines()
        if line.strip()
    ]
    assert replacement.returncode == 0, (remaining_stderr, payloads)
    assert payloads
    assert all(payload.get("kind") != "exit" or payload.get("reason") != "journal-unavailable" for payload in payloads)
    assert payloads[-1]["kind"] == "ring"
    assert payloads[-1]["reason"] == "event"


def test_unreadable_cursor_stays_armed_instead_of_poisoning_rearm(
    isolated: tuple[Path, dict[str, str]],
) -> None:
    """An unreadable cursor is retryable, not a permanent exit-2 poison."""
    project, env = isolated
    authority = journal.open_or_create_journal(project)
    lease = authority.claim_or_renew_lease(
        "armtest", principal={"principal_id": "arm-unreadable-cursor-test"}
    ).value
    assert lease is not None
    listener_env = {
        **env,
        "GOALFLIGHT_CONTROLLER_LABEL": "armtest",
        "GOALFLIGHT_CONTROLLER_LEASE_NONCE": lease.nonce,
        "GOALFLIGHT_TEST_SCRIPTS": str(SCRIPTS),
    }

    with wake.register_lease_holder(
        project, controller_label="armtest", lease_nonce=lease.nonce
    ):
        _post(env, project, "cursor unread backlog", dispatch_id="unread-cursor")
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
            first.kill()
            first.wait(timeout=5)

            replacement = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    _UNREADABLE_CURSOR_STATUS,
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
                    "0.4",
                ],
                env=listener_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                live_deadline = time.monotonic() + 5
                while time.monotonic() < live_deadline:
                    live = wake.live_waiters(
                        project,
                        controller_label="armtest",
                        kinds={"listener"},
                    ) or []
                    if live and replacement.poll() is None:
                        break
                    time.sleep(0.01)
                else:
                    stdout, stderr = replacement.communicate(timeout=5)
                    pytest.fail(
                        f"unreadable-cursor replacement never stayed live: "
                        f"code={replacement.returncode} stdout={stdout!r} stderr={stderr!r}"
                    )
                coverage = authority.active_coverage("armtest")
                assert coverage is not None
                assert coverage["state"] == "ARMED"
                stdout, stderr = replacement.communicate(timeout=10)
            finally:
                if replacement.poll() is None:
                    replacement.kill()
                    replacement.wait()
        finally:
            if first.poll() is None:
                first.kill()
                first.wait()

    payloads = [json.loads(line) for line in stdout.splitlines() if line.strip()]
    assert replacement.returncode != 2, (stderr, payloads)
    assert all(payload.get("reason") != "journal-unavailable" for payload in payloads)
    assert replacement.returncode == 1
    assert payloads
    assert payloads[-1]["kind"] == "exit"
    assert payloads[-1]["reason"] == "timeout"


def test_journal_cursor_read_keeps_covering_short_and_unreadable_distinct(
    isolated: tuple[Path, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, _env = isolated
    authority = journal.open_or_create_journal(project)
    lease = authority.claim_or_renew_lease(
        "armtest", principal={"principal_id": "cursor-read-test"}
    ).value
    assert lease is not None

    empty = messages._read_journal_cursor_positions(authority, "armtest")
    assert empty.readable is True
    assert empty.positions == {}

    def missing_status(self, *_args, **_kwargs):
        return None

    monkeypatch.setattr(journal.Journal, "cursor_status", missing_status)
    missing = messages._read_journal_cursor_positions(authority, "armtest")
    assert missing.readable is False
    with pytest.raises(RuntimeError, match="unreadable"):
        messages._journal_cursor_positions(authority, "armtest")

    def broken_status(self, *_args, **_kwargs):
        raise RuntimeError("injected cursor fault")

    monkeypatch.setattr(journal.Journal, "cursor_status", broken_status)
    broken = messages._read_journal_cursor_positions(authority, "armtest")
    assert broken.readable is False
    assert (
        messages._settle_pending_report_if_consumed(
            authority,
            project,
            controller_label="armtest",
            lease_nonce=lease.nonce,
            claim_positions={"stream-a": 1},
        )
        is None
    )


def test_unreconstructable_claim_is_quarantined_and_does_not_exit_2(
    isolated: tuple[Path, dict[str, str]],
) -> None:
    """A claim-unit reconstruction fault must not be reported as journal-unavailable."""
    project, env = isolated
    authority = journal.open_or_create_journal(project)
    lease = authority.claim_or_renew_lease(
        "armtest", principal={"principal_id": "arm-ghost-claim-test"}
    ).value
    assert lease is not None
    listener_env = {
        **env,
        "GOALFLIGHT_CONTROLLER_LABEL": "armtest",
        "GOALFLIGHT_CONTROLLER_LEASE_NONCE": lease.nonce,
    }
    path = wake._pending_report_path(
        project,
        controller_label="armtest",
        lease_nonce=lease.nonce,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    snapshot = "ab" * 32
    path.write_text(
        json.dumps(
            {
                "schema": "goalflight.pending-report.v3",
                "phase": "reported",
                "positions": {"ghost-stream": 1},
                "cursor_version": 0,
                "stream_snapshots": {"ghost-stream": snapshot},
                "claim_token": "dead" * 8,
                "owner": {"pid": 2_000_000, "start_token": "dead-owner"},
                "claimed_at_epoch": None,
                "reported_at_epoch": time.time(),
                "acknowledged_at_epoch": None,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    with wake.register_lease_holder(
        project, controller_label="armtest", lease_nonce=lease.nonce
    ):
        _post(env, project, "real mail after ghost claim", dispatch_id="real-mail")
        result = subprocess.run(
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

    payloads = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    assert result.returncode != 2, (result.stderr, payloads)
    assert all(payload.get("reason") != "journal-unavailable" for payload in payloads)
    quarantines = list(path.parent.glob(f".{path.name}.*.corrupt"))
    assert quarantines, "ghost claim should be quarantined rather than killing the waiter"
    assert not path.exists()
    assert payloads
    assert payloads[-1]["kind"] == "ring"
    assert payloads[-1]["reason"] == "event"
    assert result.returncode == 0


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


@pytest.mark.parametrize(
    "healthy_followup",
    [True, False],
    ids=["healthy-followup", "timeout-with-stuck-carrier"],
)
def test_rearm_does_not_raise_unreportable_carrier_water(
    isolated: tuple[Path, dict[str, str]],
    healthy_followup: bool,
) -> None:
    project, env = isolated
    authority = journal.open_or_create_journal(project)
    lease = authority.claim_or_renew_lease(
        "armtest", principal={"principal_id": "arm-unreportable-carrier-test"}
    ).value
    assert lease is not None
    listener_env = {
        **env,
        "GOALFLIGHT_CONTROLLER_LABEL": "armtest",
        "GOALFLIGHT_CONTROLLER_LEASE_NONCE": lease.nonce,
        "GOALFLIGHT_TEST_SCRIPTS": str(SCRIPTS),
    }
    dispatch_id = "unreportable-carrier"
    carrier = messages.inbox_path(Path(env["GOALFLIGHT_MESSAGES_DIR"]), dispatch_id)

    with wake.register_lease_holder(
        project, controller_label="armtest", lease_nonce=lease.nonce
    ):
        _post(env, project, "backlog before carrier failure", dispatch_id=dispatch_id)
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
                "--report-pending",
                "--json",
            ],
            env=listener_env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert failed.returncode == 2, failed.stderr
        assert carrier.exists()
        os.chmod(carrier, 0o000)
        replacement = subprocess.Popen(
            [
                sys.executable,
                "-c",
                _UNREPORTABLE_CARRIER,
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
                "5",
            ],
            env=listener_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            wait_until(
                lambda: (
                    coverage
                    if (coverage := authority.active_coverage("armtest"))
                    and int(coverage["pid"]) == replacement.pid
                    else None
                ),
                timeout_s=5,
                message="replacement listener never armed",
            )
            pending = wait_until(
                lambda: (
                    state
                    if (state := wake.pending_report_state(
                        project,
                        controller_label="armtest",
                        lease_nonce=lease.nonce,
                    ))
                    and state.phase == "claimed"
                    else None
                ),
                timeout_s=5,
                message="unreportable claim was marked reported",
            )
            assert pending.positions == {dispatch_id: 1}
            healthy_dispatch_id = "healthy-after-carrier-failure"
            if healthy_followup:
                _post(env, project, "healthy after carrier failure", dispatch_id=healthy_dispatch_id)
            stdout, stderr = replacement.communicate(timeout=10)
        finally:
            os.chmod(carrier, 0o600)
            if replacement.poll() is None:
                replacement.kill()
                replacement.wait(timeout=5)

    payloads = [
        json.loads(line) for line in stdout.splitlines() if line.strip()
    ]
    if healthy_followup:
        assert replacement.returncode == 0, (stderr, payloads)
        assert payloads[-1]["kind"] == "ring", payloads
        assert f"{healthy_dispatch_id}=1" in payloads[-1]["advance_command"]
        assert f"{dispatch_id}=" not in payloads[-1]["advance_command"]
    else:
        assert replacement.returncode == 1, (stderr, payloads)
        assert payloads[-1]["kind"] == "exit", payloads
        assert payloads[-1]["reason"] == "timeout", payloads
    assert "WARNING: carrier corruption" in stderr
    assert "STUCK:" in stderr
    assert str(carrier) in stderr


def test_stuck_carrier_does_not_block_later_same_or_other_streams(
    isolated: tuple[Path, dict[str, str]],
) -> None:
    project, env = isolated
    authority = journal.open_or_create_journal(project)
    lease = authority.claim_or_renew_lease(
        "armtest", principal={"principal_id": "arm-stuck-stream-progress"}
    ).value
    assert lease is not None
    listener_env = {
        **env,
        "GOALFLIGHT_CONTROLLER_LABEL": "armtest",
        "GOALFLIGHT_CONTROLLER_LEASE_NONCE": lease.nonce,
    }
    stream_id = "stuck-stream-with-followup"
    carrier = messages.inbox_path(Path(env["GOALFLIGHT_MESSAGES_DIR"]), stream_id)
    with wake.register_lease_holder(
        project, controller_label="armtest", lease_nonce=lease.nonce
    ):
        _post(env, project, "will be malformed", dispatch_id=stream_id)
        _post(env, project, "same-stream followup", dispatch_id=stream_id)
        _post(env, project, "other-stream followup", dispatch_id="healthy-stream")
        same_stream_later = next(
            row
            for row in authority.pending_delivery_events("armtest")
            if row["stream_id"] == stream_id and int(row["stream_seq"]) == 2
        )
        lines = carrier.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        carrier.write_text("{invalid json\n" + lines[1] + "\n", encoding="utf-8")

        listener = subprocess.run(
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
                "--timeout-s",
                "1",
            ],
            env=listener_env,
            capture_output=True,
            text=True,
            timeout=10,
        )

    payloads = [json.loads(line) for line in listener.stdout.splitlines() if line]
    report = next(payload for payload in payloads if payload.get("kind") == "pending-at-arm")
    assert {item["stream_id"] for item in report["items"]} == {"healthy-stream"}
    assert report["pending_behind_stuck"] == [
        {
            "id": same_stream_later["event_uuid"],
            "stream_id": "stuck-stream-with-followup",
            "stream_seq": 2,
            "type": "controller-notice",
        }
    ]
    assert "stuck-stream-with-followup=" not in report["advance_command"]
    assert "healthy-stream=1" in report["advance_command"]
    fault_events = [
        payload
        for payload in payloads
        if payload.get("kind") == "event"
        and isinstance(payload.get("payload"), dict)
        and payload["payload"].get("type") == "listener-fault"
    ]
    assert any(
        event["payload"].get("dispatch_id") == stream_id
        and event["payload"].get("stream_id") == stream_id
        and event["payload"].get("stream_seq") == 1
        and event["payload"].get("reason") == "carrier-corrupt"
        for event in fault_events
    ), payloads
    assert listener.returncode == 1, (listener.stdout, listener.stderr)
    assert "STUCK:" in listener.stderr

    # Restore the missing first carrier row. The next listener snapshot sees
    # the repaired first event and the already-visible same-stream followup;
    # only then may its position advance through seq 2.
    carrier.write_text("\n".join(lines) + "\n", encoding="utf-8")
    repaired = authority.cursor_peek("armtest", nonce=lease.nonce, limit=1000)
    materialized = messages._envelopes_with_rows(
        authority, list(repaired.items), controller_label="armtest"
    )
    repaired_stream = [
        envelope
        for row, envelope in materialized
        if row["stream_id"] == "stuck-stream-with-followup"
    ]
    assert [int(envelope["seq"]) for envelope in repaired_stream] == [1, 2]
    repaired_positions = messages._cursor_positions_for_shown_rows(
        list(repaired.items), materialized
    )
    assert repaired_positions == {
        "stuck-stream-with-followup": 2,
        "healthy-stream": 1,
    }
    advanced = authority.advance_cursor(
        "armtest",
        nonce=lease.nonce,
        expected_cursor_version=repaired.cursor_version,
        expected_stream_snapshots=repaired.stream_snapshots,
        advances=repaired_positions,
        actor="armtest",
    )
    assert advanced.committed
    assert not authority.cursor_peek("armtest", nonce=lease.nonce).items


def test_listener_ring_hides_terminal_until_stuck_head_is_repaired(
    isolated: tuple[Path, dict[str, str]],
) -> None:
    project, env = isolated
    authority = journal.open_or_create_journal(project)
    lease = authority.claim_or_renew_lease(
        "armtest", principal={"principal_id": "arm-stuck-ring"}
    ).value
    assert lease is not None
    stream_id = "stuck-ring-terminal"
    addressee = messages.controller_addressee("armtest", project_root=project)
    source = {"node": "peer", "adapter": "pytest", "transport": "controller"}
    first = messages.post_message(
        dispatch_id=stream_id,
        msg_type="controller-notice",
        payload={"text": "unreadable ring head"},
        messages_dir=Path(env["GOALFLIGHT_MESSAGES_DIR"]),
        source=source,
        addressee=addressee,
    )
    result = messages.post_message(
        dispatch_id=stream_id,
        msg_type="result",
        payload={"complete": True, "text": "ring terminal"},
        messages_dir=Path(env["GOALFLIGHT_MESSAGES_DIR"]),
        source=source,
        addressee=addressee,
    )
    carrier = Path(first["path"])
    original_lines = carrier.read_text(encoding="utf-8").splitlines()
    assert len(original_lines) == 2
    carrier.write_text("{invalid head\n" + original_lines[1] + "\n", encoding="utf-8")
    listener_env = {
        **env,
        "GOALFLIGHT_CONTROLLER_LABEL": "armtest",
        "GOALFLIGHT_CONTROLLER_LEASE_NONCE": lease.nonce,
    }
    command = [
        sys.executable,
        str(SCRIPTS / "goalflight_messages.py"),
        "listen",
        "--project-root",
        str(project),
        "--controller-label",
        "armtest",
        "--lease-nonce",
        lease.nonce,
        "--no-report-pending",
        "--poll-secs",
        "0.02",
        "--timeout-s",
        "2",
    ]
    with wake.register_lease_holder(
        project, controller_label="armtest", lease_nonce=lease.nonce
    ):
        stuck = subprocess.run(
            command,
            env=listener_env,
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert stuck.returncode == 1, (stuck.stdout, stuck.stderr)
        assert "[result]" not in stuck.stdout
        pending_records = [
            json.loads(line)
            for line in stuck.stdout.splitlines()
            if line.startswith("{")
        ]
        assert any(
            record.get("payload", {}).get("type") == "listener-fault"
            and record["payload"].get("stream_id") == stream_id
            and record["payload"].get("stream_seq") == 1
            for record in pending_records
        ), stuck.stdout
        assert any(
            record.get("payload", {}).get("reason") == "pending_behind_stuck"
            and record["payload"].get("pending_behind_stuck", {}).get("id")
            == result["envelope"]["id"]
            for record in pending_records
        ), stuck.stdout

        carrier.write_text("\n".join(original_lines) + "\n", encoding="utf-8")
        repaired = subprocess.run(
            [*command[:-1], "2"],
            env=listener_env,
            capture_output=True,
            text=True,
            timeout=5,
        )

    assert repaired.returncode == 0, (repaired.stdout, repaired.stderr)
    result_lines = [
        line for line in repaired.stdout.splitlines()
        if line.startswith("[result] ") and f"{stream_id} seq=2" in line
    ]
    assert len(result_lines) == 1, repaired.stdout


def test_listener_hides_terminal_behind_unprojected_head_until_projection(
    isolated: tuple[Path, dict[str, str]],
) -> None:
    project, env = isolated
    authority = journal.open_or_create_journal(project)
    lease = authority.claim_or_renew_lease(
        "armtest", principal={"principal_id": "arm-unprojected-ring"}
    ).value
    assert lease is not None
    stream_id = "unprojected-ring-terminal"
    addressee = messages.controller_addressee("armtest", project_root=project)
    source = {"node": "peer", "adapter": "pytest", "transport": "controller"}
    first = messages.post_message(
        dispatch_id=stream_id,
        msg_type="controller-notice",
        payload={"text": "unprojected ring head"},
        messages_dir=Path(env["GOALFLIGHT_MESSAGES_DIR"]),
        source=source,
        addressee=addressee,
    )
    second = messages.post_message(
        dispatch_id=stream_id,
        msg_type="result",
        payload={"complete": True, "text": "ring terminal"},
        messages_dir=Path(env["GOALFLIGHT_MESSAGES_DIR"]),
        source=source,
        addressee=addressee,
    )
    projected_at = authority.read_all(
        "SELECT projected_at FROM delivery_events WHERE event_uuid = ?",
        (first["envelope"]["id"],),
    )[0]["projected_at"]
    connection = authority._connect()
    try:
        connection.execute(
            "UPDATE delivery_events SET projected_at = NULL WHERE event_uuid = ?",
            (first["envelope"]["id"],),
        )
    finally:
        connection.close()
    listener_env = {
        **env,
        "GOALFLIGHT_CONTROLLER_LABEL": "armtest",
        "GOALFLIGHT_CONTROLLER_LEASE_NONCE": lease.nonce,
    }
    command = [
        sys.executable,
        str(SCRIPTS / "goalflight_messages.py"),
        "listen",
        "--project-root",
        str(project),
        "--controller-label",
        "armtest",
        "--lease-nonce",
        lease.nonce,
        "--report-pending",
        "--poll-secs",
        "0.02",
        "--timeout-s",
        "4",
    ]
    stdout_path = project.parent / "unprojected-listener.stdout"
    stderr_path = project.parent / "unprojected-listener.stderr"
    with wake.register_lease_holder(
        project, controller_label="armtest", lease_nonce=lease.nonce
    ):
        with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open(
            "w", encoding="utf-8"
        ) as stderr:
            proc = subprocess.Popen(
                command,
                env=listener_env,
                stdout=stdout,
                stderr=stderr,
                text=True,
            )
            try:
                wait_until(
                    lambda: (
                        output
                        if (output := stdout_path.read_text(encoding="utf-8"))
                        and "pending_behind_stuck" in output
                        else None
                    ),
                    timeout_s=3,
                    message="listener did not report the unprojected barrier",
                )
                interim = [
                    json.loads(line)
                    for line in stdout_path.read_text(encoding="utf-8").splitlines()
                    if line.startswith("{")
                ]
                assert not any(
                    isinstance(record.get("payload"), dict)
                    and record["payload"].get("dispatch_id") == stream_id
                    and record["payload"].get("stream_seq") == 2
                    and record["payload"].get("type") == "result"
                    for record in interim
                ), interim
                assert any(
                    isinstance(record.get("payload"), dict)
                    and record["payload"].get("type") == "pending-projection"
                    and record["payload"].get("pending_projection") == {
                        "stream_id": stream_id,
                        "stream_seq": 1,
                    }
                    for record in interim
                ), interim
                assert not any(
                    isinstance(record.get("payload"), dict)
                    and record["payload"].get("type") == "listener-fault"
                    and record["payload"].get("stream_id") == stream_id
                    and record["payload"].get("stream_seq") == 1
                    for record in interim
                ), interim
                assert proc.poll() is None, "listener exited while an unprojected row was pending"

                connection = authority._connect()
                try:
                    connection.execute(
                        "UPDATE delivery_events SET projected_at = ? WHERE event_uuid = ?",
                        (projected_at, first["envelope"]["id"]),
                    )
                finally:
                    connection.close()
                wait_until(
                    lambda: (
                        output
                        if (output := stdout_path.read_text(encoding="utf-8"))
                        and f"[controller-notice] {stream_id} seq=1" in output
                        and f"[result] {stream_id} seq=2" in output
                        else None
                    ),
                    timeout_s=3,
                    message="listener did not deliver both rows after projection",
                )
                output = stdout_path.read_text(encoding="utf-8")
                assert output.count(f"[controller-notice] {stream_id} seq=1") == 1
                assert output.count(f"[result] {stream_id} seq=2") == 1
                assert proc.wait(timeout=3) == 0, output
            finally:
                if proc.poll() is None:
                    proc.kill()
                proc.wait(timeout=5)


def test_listener_reports_stuck_once_and_waits_for_repair(
    isolated: tuple[Path, dict[str, str]],
) -> None:
    project, env = isolated
    authority = journal.open_or_create_journal(project)
    lease = authority.claim_or_renew_lease(
        "armtest", principal={"principal_id": "arm-stuck-wait-repair"}
    ).value
    assert lease is not None
    listener_env = {
        **env,
        "GOALFLIGHT_CONTROLLER_LABEL": "armtest",
        "GOALFLIGHT_CONTROLLER_LEASE_NONCE": lease.nonce,
    }
    stream_id = "stuck-wait-stream"
    carrier = messages.inbox_path(Path(env["GOALFLIGHT_MESSAGES_DIR"]), stream_id)
    stdout_path = project.parent / "stuck-listener.stdout"
    stderr_path = project.parent / "stuck-listener.stderr"
    with wake.register_lease_holder(
        project, controller_label="armtest", lease_nonce=lease.nonce
    ):
        _post(env, project, "stuck head", dispatch_id=stream_id)
        _post(env, project, "readable successor", dispatch_id=stream_id)
        original_lines = carrier.read_text(encoding="utf-8").splitlines()
        assert len(original_lines) == 2
        carrier.write_text("{invalid stuck head\n" + original_lines[1] + "\n", encoding="utf-8")
        with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open(
            "w", encoding="utf-8"
        ) as stderr:
            listener = subprocess.Popen(
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
                    "0.02",
                    "--timeout-s",
                    "3",
                ],
                env=listener_env,
                stdout=stdout,
                stderr=stderr,
                text=True,
            )
            wait_until(
                lambda: (
                    output
                    if (output := stdout_path.read_text(encoding="utf-8"))
                    and "listener-fault" in output
                    else None
                ),
                timeout_s=5,
                message="listener did not report the stuck head",
            )
            time.sleep(0.16)
            assert listener.poll() is None, "stuck successor caused the listener to exit"
            interim = [
                json.loads(line)
                for line in stdout_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            interim_faults = [
                item for item in interim
                if item.get("kind") == "event"
                and isinstance(item.get("payload"), dict)
                and item["payload"].get("type") == "listener-fault"
            ]
            interim_stuck = [
                item
                for item in interim_faults
                if item["payload"].get("reason") != "pending_behind_stuck"
            ]
            interim_pending = [
                item
                for item in interim_faults
                if item["payload"].get("reason") == "pending_behind_stuck"
            ]
            assert len(interim_stuck) == 1, interim
            assert len(interim_pending) == 1, interim

            carrier.write_text("\n".join(original_lines) + "\n", encoding="utf-8")
            listener.wait(timeout=5)

    payloads = [
        json.loads(line)
        for line in stdout_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    faults = [
        item for item in payloads
        if item.get("kind") == "event"
        and isinstance(item.get("payload"), dict)
        and item["payload"].get("type") == "listener-fault"
    ]
    rings = [item for item in payloads if item.get("kind") == "ring"]
    stuck = [
        item
        for item in faults
        if item["payload"].get("reason") != "pending_behind_stuck"
    ]
    pending = [
        item
        for item in faults
        if item["payload"].get("reason") == "pending_behind_stuck"
    ]
    assert listener.returncode == 0, (payloads, stderr_path.read_text(encoding="utf-8"))
    assert len(stuck) == 1, payloads
    assert len(pending) == 1, payloads
    assert len(rings) == 1, payloads
    assert f"{stream_id}=2" in rings[0]["advance_command"]


def test_recovered_materialization_clears_stale_stuck_flag_before_safe_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = {
        "carrier_path": "/carrier",
        "event_uuid": "recovered-event",
        "event_type": "result",
        "stream_id": "single-stuck-repair",
        "stream_seq": 1,
    }
    attempts = 0
    envelope = {
        "id": "recovered-event",
        "dispatch_id": "single-stuck-repair",
        "seq": 1,
        "type": "result",
    }

    def recover_after_first_attempt(source, item, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise messages.MessageError("STUCK: injected unreadable carrier")
        return envelope

    monkeypatch.setattr(messages, "_listener_envelope", recover_after_first_attempt)
    first_errors: list[dict[str, object]] = []
    assert messages._envelopes_with_rows(
        object(), [row], carrier_errors=first_errors
    ) == []
    assert row["listener_status"] == "STUCK"

    recovered = messages._envelopes_with_rows(object(), [row])
    assert recovered == [(row, envelope)]
    assert messages._cursor_positions_for_shown_rows([row], recovered) == {
        "single-stuck-repair": 1
    }


def test_unprojected_row_has_internal_pending_code_and_remains_unshown() -> None:
    row = {
        "carrier_path": "journal:project",
        "event_uuid": "pending-projection-event",
        "event_type": "result",
        "stream_id": "pending-projection-stream",
        "stream_seq": 1,
        "projected_at": None,
    }
    errors: list[dict[str, object]] = []

    assert messages._deliverable_rows(object(), [row], carrier_errors=errors) == []
    assert errors == [
        {
            "code": "projection_pending",
            "carrier_path": "journal:project",
            "error": "STUCK: delivery row is not projected",
            "reason": "STUCK: delivery row is not projected",
            "status": "STUCK",
            "delivery_id": "pending-projection-event",
            "dispatch_id": "pending-projection-stream",
            "stream_id": "pending-projection-stream",
            "stream_seq": 1,
            "type": "result",
        }
    ]


def test_dead_claim_takeover_delivers_at_least_once_and_preserves_listener_pool(
    isolated: tuple[Path, dict[str, str]],
    record_property: Callable[[str, object], None],
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
    error_paths = [project.parent / f"takeover-{index}.stderr" for index in range(4)]
    gate_directory = project.parent / "takeover-gate"
    gate_directory.mkdir()
    release_path = gate_directory / "release"
    finished_path = gate_directory / "listener-finished"
    finish_release_path = gate_directory / "finish-release"
    ready_paths = [gate_directory / f"ready-{index}" for index in range(4)]
    processes: list[subprocess.Popen[str]] = []

    with wake.register_lease_holder(
        project, controller_label="armtest", lease_nonce=lease.nonce
    ):
        _post(env, project, "pool backlog", dispatch_id="claim-pool")
        _post(env, project, "second pool backlog", dispatch_id="claim-pool-secondary")
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
            def _payloads() -> list[dict]:
                payloads = []
                for path in output_paths:
                    try:
                        contents = path.read_text(encoding="utf-8")
                    except FileNotFoundError:
                        continue
                    for line in contents.splitlines():
                        if not line.strip():
                            continue
                        try:
                            payloads.append(json.loads(line))
                        except json.JSONDecodeError:
                            # A listener may have opened the file but not flushed
                            # its complete JSON line yet.
                            continue
                return payloads

            def _payload_count(kind: str) -> int:
                return sum(payload.get("kind") == kind for payload in _payloads())

            def _pending_reports() -> int:
                return _payload_count("pending-at-arm")

            def _ring_signatures() -> list[tuple[int, str]]:
                return [
                    (
                        int(payload.get("cursor_version") or 0),
                        str(payload.get("advance_command") or ""),
                    )
                    for payload in _payloads()
                    if payload.get("kind") == "ring"
                ]

            def _reported_backlog_positions() -> set[tuple[str, int]]:
                return {
                    (
                        str(item.get("stream_id") or ""),
                        int(item.get("stream_seq") or 0),
                    )
                    for payload in _payloads()
                    if payload.get("kind") == "pending-at-arm"
                    for item in payload.get("items", [])
                }

            def _payload_summary():
                summary = []
                for index, path in enumerate(output_paths):
                    try:
                        contents = path.read_text(encoding="utf-8")
                    except FileNotFoundError:
                        continue
                    rows = []
                    for line in contents.splitlines():
                        try:
                            payload = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        items = [
                            (
                                str(item.get("stream_id") or ""),
                                int(item.get("stream_seq") or 0),
                            )
                            for item in payload.get("items", [])
                        ]
                        rows.append((str(payload.get("kind") or ""), items))
                    if rows:
                        summary.append((processes[index].pid, rows))
                return summary

            def _start_rearmed_listener() -> None:
                index = len(processes)
                output_path = project.parent / f"takeover-rearm-{index}.jsonl"
                error_path = project.parent / f"takeover-rearm-{index}.stderr"
                ready_path = gate_directory / f"rearm-ready-{index}"
                output_paths.append(output_path)
                error_paths.append(error_path)
                ready_paths.append(ready_path)
                with output_path.open("w", encoding="utf-8") as stdout:
                    with error_path.open("w", encoding="utf-8") as stderr:
                        process = subprocess.Popen(
                            [
                                sys.executable,
                                "-c",
                                _GATE_PENDING_REPORT_CLAIM,
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
                                "300",
                            ],
                            env={
                                **listener_env,
                                "GOALFLIGHT_TEST_PENDING_REPORT_READY": str(
                                    ready_path
                                ),
                                "GOALFLIGHT_TEST_PENDING_REPORT_RELEASE": str(
                                    release_path
                                ),
                                "GOALFLIGHT_TEST_LISTENER_FINISHED": str(
                                    finished_path
                                ),
                                "GOALFLIGHT_TEST_LISTENER_FINISH_RELEASE": str(
                                    finish_release_path
                                ),
                            },
                            stdout=stdout,
                            stderr=stderr,
                            text=True,
                        )
                processes.append(process)

            for index, (path, ready_path) in enumerate(
                zip(output_paths, ready_paths)
            ):
                handle = path.open("w", encoding="utf-8")
                error_handle = error_paths[index].open("w", encoding="utf-8")
                processes.append(
                    subprocess.Popen(
                        [
                            sys.executable,
                            "-c",
                            _GATE_PENDING_REPORT_CLAIM,
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
                            "300",
                        ],
                        env={
                            **listener_env,
                            "GOALFLIGHT_TEST_PENDING_REPORT_READY": str(ready_path),
                            "GOALFLIGHT_TEST_PENDING_REPORT_RELEASE": str(release_path),
                            "GOALFLIGHT_TEST_LISTENER_FINISHED": str(finished_path),
                            "GOALFLIGHT_TEST_LISTENER_FINISH_RELEASE": str(
                                finish_release_path
                            ),
                        },
                        stdout=handle,
                        stderr=error_handle,
                        text=True,
                    )
                )
                handle.close()
                error_handle.close()

            wait_until(
                lambda: all(path.exists() for path in ready_paths),
                timeout_s=45,
                message="all replacement listeners reach the pending-report claim gate",
            )
            temporary_release = release_path.with_suffix(".tmp")
            temporary_release.write_text("release\n", encoding="utf-8")
            temporary_release.replace(release_path)
            wait_until(
                lambda: _pending_reports() >= 1,
                timeout_s=30,
                message="a pending-at-arm report after concurrent dead-claim takeover",
            )
            wait_until(
                lambda: {("claim-pool", 1), ("claim-pool-secondary", 1)}
                <= _reported_backlog_positions(),
                timeout_s=10,
                message="every pending backlog event appears in a pending-at-arm report",
            )
            wait_until(
                lambda: (
                    state
                    if (state := wake.pending_report_state(
                        project,
                        controller_label="armtest",
                        lease_nonce=lease.nonce,
                    ))
                    and state.phase == "reported"
                    else None
                ),
                timeout_s=10,
                message="the pending-at-arm report is durably marked before re-arm",
            )

            report_observation_deadline = time.monotonic() + 1.0
            wait_until(
                lambda: time.monotonic() >= report_observation_deadline,
                timeout_s=1.5,
                interval_s=0.02,
                message="one-second observation after concurrent pending-at-arm reports",
            )
            record_property(
                "duplicate_pending_reports_observed",
                max(0, _pending_reports() - 1),
            )

            backlog = authority.cursor_peek("armtest", nonce=lease.nonce)
            advances = {
                str(item["stream_id"]): int(item["stream_seq"])
                for item in backlog.items
                if str(item.get("stream_id") or "")
                in {"claim-pool", "claim-pool-secondary"}
            }
            assert advances == {
                "claim-pool": 1,
                "claim-pool-secondary": 1,
            }
            acknowledged = authority.advance_cursor(
                "armtest",
                nonce=lease.nonce,
                expected_cursor_version=backlog.cursor_version,
                expected_stream_snapshots={
                    stream_id: backlog.stream_snapshots[stream_id]
                    for stream_id in advances
                },
                advances=advances,
                actor="test-concurrent-takeover-ack",
            )
            assert acknowledged.committed

            live = wake.live_waiters(
                project,
                controller_label="armtest",
                kinds={"listener"},
            ) or []
            while len(live) < 4:
                _start_rearmed_listener()
                wait_until(
                    lambda: len(
                        wake.live_waiters(
                            project,
                            controller_label="armtest",
                            kinds={"listener"},
                        )
                        or []
                    )
                    > len(live),
                    timeout_s=10,
                    message="re-armed listener restores one missing pool slot",
                )
                live = wake.live_waiters(
                    project,
                    controller_label="armtest",
                    kinds={"listener"},
                ) or []

            live = (
                wake.live_waiters(
                    project,
                    controller_label="armtest",
                    kinds={"listener"},
                )
                or []
            )
            assert len(live) == 4

            assert _pending_reports() >= 1, _payloads()
            assert all(process.poll() is None for process in processes)

            _post(env, project, "new after pool takeover", dispatch_id="claim-pool")
            wait_until(
                lambda: any(
                    payload.get("kind") == "ring"
                    and "claim-pool=2" in payload.get("advance_command", "")
                    for payload in _payloads()
                )
                and finished_path.exists(),
                timeout_s=5,
                message="one listener rings and reaches its bounded observation hold",
            )
            assert _payload_count("ring") >= 1
            assert all(process.poll() is None for process in processes)
            observation_deadline = time.monotonic() + 1.0
            wait_until(
                lambda: time.monotonic() >= observation_deadline,
                timeout_s=1.5,
                interval_s=0.02,
                message="one-second observation after the first listener rings",
            )
            assert all(process.poll() is None for process in processes)
            assert _pending_reports() >= 1, {
                "payloads": _payload_summary(),
                "stderr": [path.read_text(encoding="utf-8") for path in error_paths],
            }
            assert _reported_backlog_positions() >= {
                ("claim-pool", 1),
                ("claim-pool-secondary", 1),
            }
            ring_count = _payload_count("ring")
            assert ring_count >= 1
            ring_signatures = _ring_signatures()
            record_property(
                "duplicate_rings_observed",
                max(0, ring_count - len(set(ring_signatures))),
            )
        finally:
            temporary_release = finish_release_path.with_suffix(".tmp")
            temporary_release.write_text("release\n", encoding="utf-8")
            temporary_release.replace(finish_release_path)
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


def test_dead_lease_nonce_exits_did_not_arm_not_ring(
    isolated: tuple[Path, dict[str, str]],
) -> None:
    """ACTIVE journal row whose holder lock is unheld must not look like a ring."""
    project, env = isolated
    authority = journal.open_or_create_journal(project)
    lease = authority.claim_or_renew_lease(
        "armtest", principal={"principal_id": "dead-nonce-arm"}
    ).value
    assert lease is not None
    holder = wake.register_lease_holder(
        project, controller_label="armtest", lease_nonce=lease.nonce
    )
    holder.__enter__()
    holder.close()
    state, _session = sessions.probe_live_session(project, label="armtest")
    assert state == "dead", "precondition: released holder lock is readable dead"
    _post(env, project, "would have rung a dead-nonce doorbell")
    listener_env = {
        **env,
        "GOALFLIGHT_CONTROLLER_LABEL": "armtest",
        "GOALFLIGHT_CONTROLLER_LEASE_NONCE": lease.nonce,
        "GOALFLIGHT_TEST_MODE": "1",
        "GOALFLIGHT_PROCESS_ROLE": "controller",
    }
    refused = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "goalflight_messages.py"),
            "listen",
            "--project-root",
            str(project),
            "--controller-label",
            "armtest",
            "--lease-nonce",
            lease.nonce,
            "--no-report-pending",
            "--poll-secs",
            "0.05",
            "--timeout-s",
            "8",
            "--json",
        ],
        env=listener_env,
        cwd=project,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert refused.returncode == messages.LISTENER_DID_NOT_ARM_EXIT, refused.stderr
    assert "did-not-arm" in refused.stderr
    assert "lease-nonce-not-live" in refused.stderr
    assert "will not cover the pool" in refused.stderr
    assert not (wake.live_waiters(project, controller_label="armtest") or [])
    assert refused.returncode != 0


def test_unreadable_lease_nonce_is_retryable_not_dead(
    isolated: tuple[Path, dict[str, str]],
) -> None:
    """Busy open_reader through the real probe must not become did-not-arm.

    Drives ``probe_live_session``: ``Journal.open_reader`` raises
    ``JournalBusy`` under the probe's 0.05s budgets. A stub that returns
    ``("unreadable", None)`` cannot catch a collapse of unreadable into
    dead inside the probe.
    """
    project, env = isolated
    authority = journal.open_or_create_journal(project)
    lease = authority.claim_or_renew_lease(
        "armtest", principal={"principal_id": "unreadable-nonce-arm"}
    ).value
    assert lease is not None
    listener_env = {
        **env,
        "GOALFLIGHT_CONTROLLER_LABEL": "armtest",
        "GOALFLIGHT_CONTROLLER_LEASE_NONCE": lease.nonce,
        "GOALFLIGHT_TEST_SCRIPTS": str(SCRIPTS),
        "GOALFLIGHT_TEST_MODE": "1",
        "GOALFLIGHT_PROCESS_ROLE": "controller",
    }
    with wake.register_lease_holder(
        project, controller_label="armtest", lease_nonce=lease.nonce
    ):
        state, session = sessions.probe_live_session(project, label="armtest")
        assert state == "live" and session is not None
        refused = subprocess.run(
            [
                sys.executable,
                "-c",
                _UNREADABLE_LEASE_PROBE,
                "listen",
                "--project-root",
                str(project),
                "--controller-label",
                "armtest",
                "--lease-nonce",
                lease.nonce,
                "--poll-secs",
                "0.05",
                "--timeout-s",
                "3",
                "--json",
            ],
            env=listener_env,
            cwd=project,
            capture_output=True,
            text=True,
            timeout=10,
        )
    assert refused.returncode == 2, refused.stderr
    assert refused.returncode != messages.LISTENER_DID_NOT_ARM_EXIT
    assert "journal-unavailable" in refused.stderr
    assert "retryable, not a dead nonce" in refused.stderr
    assert "lease-nonce-not-live" not in refused.stderr
    assert "did-not-arm" not in refused.stderr
    assert not (wake.live_waiters(project, controller_label="armtest") or [])


def test_genuine_ring_still_exits_zero(
    isolated: tuple[Path, dict[str, str]],
) -> None:
    project, env = isolated
    authority = journal.open_or_create_journal(project)
    lease = authority.claim_or_renew_lease(
        "armtest", principal={"principal_id": "live-ring-arm"}
    ).value
    assert lease is not None
    listener_env = {
        **env,
        "GOALFLIGHT_CONTROLLER_LABEL": "armtest",
        "GOALFLIGHT_CONTROLLER_LEASE_NONCE": lease.nonce,
        "GOALFLIGHT_TEST_MODE": "1",
        "GOALFLIGHT_PROCESS_ROLE": "controller",
    }
    with wake.register_lease_holder(
        project, controller_label="armtest", lease_nonce=lease.nonce
    ):
        _post(env, project, "live ring mail")
        rang = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "goalflight_messages.py"),
                "listen",
                "--project-root",
                str(project),
                "--controller-label",
                "armtest",
                "--lease-nonce",
                lease.nonce,
                "--no-report-pending",
                "--poll-secs",
                "0.05",
                "--timeout-s",
                "8",
                "--json",
            ],
            env=listener_env,
            cwd=project,
            capture_output=True,
            text=True,
            timeout=12,
        )
    assert rang.returncode == 0, rang.stderr
    payloads = [json.loads(line) for line in rang.stdout.splitlines() if line.strip()]
    assert payloads
    assert payloads[-1]["kind"] == "ring"
    assert payloads[-1]["reason"] == "event"


def test_grokbot_helper_reports_duplicate_arm_and_forwards_hup(
    isolated: tuple[Path, dict[str, str]],
) -> None:
    project, env = isolated
    label = "grokbot-single-arm"
    authority = journal.open_or_create_journal(project)
    claimed = authority.claim_or_renew_lease(
        label, principal={"principal_id": "grokbot-single-arm-test"}
    )
    assert claimed.committed and claimed.value is not None
    lease = claimed.value
    command = [
        sys.executable,
        str(SCRIPTS / "goalflight_grok_bot_listen.py"),
        "--project-root",
        str(project),
        "--controller-label",
        label,
        "--lease-nonce",
        lease.nonce,
        "--timeout-s",
        "60",
    ]
    with wake.register_lease_holder(
        project, controller_label=label, lease_nonce=lease.nonce
    ):
        first = subprocess.Popen(
            command,
            cwd=project,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.monotonic() + 5
            listener_pids: list[int] = []
            while time.monotonic() < deadline:
                listener_pids = wake.listener_slot_holder_pids(
                    project, controller_label=label
                )
                if listener_pids:
                    break
                assert first.poll() is None
                time.sleep(0.02)
            assert len(listener_pids) == 1
            incumbent = listener_pids[0]
            assert incumbent != first.pid

            second = subprocess.run(
                command,
                cwd=project,
                env=env,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            assert second.returncode == 3, (second.stdout, second.stderr)
            assert f"pid(s)={incumbent}" in second.stderr
            assert "duplicate arm not started" in second.stderr
            assert first.poll() is None
            assert wake.listener_slot_holder_pids(project, controller_label=label) == [
                incumbent
            ]

            first.send_signal(signal.SIGHUP)
            stdout, stderr = first.communicate(timeout=4)
            assert first.returncode == messages.listener_posix_signal_exit_code(
                signal.SIGHUP
            ), (stdout, stderr)
            assert "QUOTE-CHECK:" in stdout
            assert wake.listener_slot_holder_pids(project, controller_label=label) == []
        finally:
            if first.poll() is None:
                first.kill()
                first.communicate(timeout=2)


def main() -> None:
    raise SystemExit(pytest.main([__file__, "-q"]))


if __name__ == "__main__":
    main()
