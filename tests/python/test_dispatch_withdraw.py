"""Withdrawal closes authority before removing its runnable queue carrier."""
from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import shlex
import sqlite3
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from support import SCRIPTS

import goalflight_dispatch as dispatch
import goalflight_capacity as capacity
import goalflight_journal as journal
import goalflight_ledger as ledger
import goalflight_status as status


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    monkeypatch.setenv("GOALFLIGHT_CONTROLLER_LABEL", "owner")
    authority = journal.Journal.create(project)
    result = authority.prepare_attempt(
        "withdraw-test", owner_controller_label="owner", owner_session_nonce="test-session",
    )
    assert result.committed
    record = {
        "dispatch_id": "withdraw-test", "controller_label": "owner",
        "project_root": str(project), "state": "queued", "terminal_state": "unknown",
        "created_at": "2020-01-01T00:00:00Z",
        "request": {"argv": ["--agent", "codex"]},
    }
    ledger.write_record(record)
    carrier = dispatch._queue_entry_path("withdraw-test")
    carrier.parent.mkdir(parents=True, exist_ok=True)
    carrier.write_text(json.dumps(record))
    return project, authority, result.value, carrier


def withdraw(*args, dispatch_id="withdraw-test"):
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = dispatch.main(["withdraw", dispatch_id, "--reason", "obsolete", "--json", *args])
    return code, json.loads(output.getvalue())


def write_legacy_malformed_record(record: dict) -> None:
    path = ledger.record_path(record["dispatch_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")


def write_never_launched_record(
    prepared,
    *,
    dispatch_id="withdraw-test",
    controller_label="owner",
    started_at="2020-01-01T00:00:00Z",
    worker_pid=None,
    worker_identity=None,
    monkeypatch=None,
):
    project, _authority, _attempt, _carrier = prepared
    record = {
        "dispatch_id": dispatch_id,
        "project_root": str(project),
        "state": "running",
        "terminal_state": "unknown",
        "started_at": started_at,
        "status_path": str(project / f"{dispatch_id}.missing.status.json"),
    }
    if controller_label is not None:
        record["controller_label"] = controller_label
    if worker_pid is not None:
        record["worker_pid"] = worker_pid
    if worker_identity is not None:
        record["worker_identity"] = worker_identity
    if monkeypatch is None:
        ledger.write_record(record)
    else:
        with monkeypatch.context() as patch:
            patch.setattr(ledger, "utc_now", lambda: started_at)
            ledger.write_record(record)
    carrier = dispatch._queue_entry_path(dispatch_id)
    carrier.parent.mkdir(parents=True, exist_ok=True)
    carrier.write_text(json.dumps({
        "dispatch_id": dispatch_id,
        "project_root": str(project),
        "state": "queued",
        "created_at": started_at,
        "updated_at": started_at,
    }))
    return record


def write_retry_child(
    prepared,
    dispatch_id,
    *,
    controller_label=None,
    parent_dispatch_id: str | None = "withdraw-test",
    dispatch_argv: list[str] | None = None,
):
    project, authority, _, _ = prepared
    result = authority.prepare_attempt(
        dispatch_id,
        owner_controller_label=controller_label,
        owner_session_nonce="retry-session" if controller_label else None,
    )
    assert result.committed
    record = {
        "dispatch_id": dispatch_id,
        "controller_label": controller_label,
        "project_root": str(project),
        "parent_dispatch_id": parent_dispatch_id,
        "requeued_from": parent_dispatch_id,
        "state": "worker_dead",
        "terminal_state": "worker_dead",
        "task_ids": ["t-retry"],
    }
    if dispatch_argv is not None:
        record["dispatch_argv"] = dispatch_argv
    ledger.write_record(record)
    return record


def snapshot(root):
    # SQLite read-only connections maintain transient WAL/SHM coordination files.
    # Compare persistent bytes; journal rows are also checked independently.
    return {
        str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*")
        if path.is_file() and not path.name.endswith((".sqlite3-shm", ".sqlite3-wal"))
    }


def attempt_row(authority):
    return authority.read_all("SELECT * FROM dispatch_attempts WHERE dispatch_id = ?", ("withdraw-test",))[0]


def record_dead_worker_evidence(project, authority, attempt, *, pid=999_999_999):
    identity = {"pid": pid, "start_token": "gone"}
    row = attempt_row(authority)
    running = authority.mark_attempt_running(
        attempt.attempt_id,
        attempt.launch_token,
        launch_epoch=row["launch_epoch"],
        worker_instance=identity,
    )
    assert running.committed
    status_path = project / "withdraw.status.json"
    status_path.write_text(
        json.dumps({
            "dispatch_id": "withdraw-test",
            "state": "running",
            "worker_pid": pid,
            "worker_identity": identity,
        })
    )
    record = ledger.read_record("withdraw-test")
    record.update(
        status_path=str(status_path),
        worker_pid=pid,
        worker_identity=identity,
    )
    ledger.write_record(record)


@pytest.fixture
def claimed(prepared):
    _, authority, attempt, carrier = prepared
    assert authority.start_attempt(attempt.attempt_id, attempt.launch_token).committed
    launcher = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        identity = ledger.process_identity(launcher.pid)
        assert identity and identity.get("start_token"), identity
    finally:
        launcher.terminate()
        launcher.wait(timeout=10)
    entry = json.loads(carrier.read_text())
    entry.update(
        queue_launch_token=attempt.launch_token, queue_launch_started=True,
        queue_claimer_pid=launcher.pid, queue_claimer_identity=identity,
        queue_launcher_pid=launcher.pid, queue_launcher_identity=identity,
    )
    carrier.write_text(json.dumps(entry))
    claim = dispatch._claim_queue_entry(carrier)
    assert claim is not None
    return claim


@pytest.mark.parametrize("alive", [True, False])
def test_claimed_worker_identity(prepared, claimed, alive):
    project, authority, attempt, carrier = prepared
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        identity = ledger.process_identity(child.pid)
        assert identity and identity.get("start_token"), identity
        entry = json.loads(claimed.read_text())
        entry.update(queue_worker_pid=child.pid, queue_worker_identity=identity)
        claimed.write_text(json.dumps(entry))
        if not alive:
            child.terminate()
            child.wait(timeout=10)
            record_dead_worker_evidence(
                project, authority, attempt, pid=child.pid,
            )

        original = claimed.read_bytes()
        row = attempt_row(authority)
        record = ledger.record_path("withdraw-test").read_bytes()
        code, result = withdraw()
        if alive:
            assert code == 1 and "never kills" in result["reason"], result
            assert child.poll() is None
            assert claimed.read_bytes() == original
            assert attempt_row(authority) == row
            assert ledger.record_path("withdraw-test").read_bytes() == record
        else:
            assert code == 0, result
            assert result["archived_carrier"] is not None
            assert Path(result["archived_carrier"]).read_bytes() == original
            assert not claimed.exists()
            assert attempt_row(authority)["terminal_state"] == "withdrawn"
            assert ledger.read_record("withdraw-test")["terminal_state"] == "withdrawn"
        assert not carrier.exists()
    finally:
        if child.poll() is None:
            child.terminate()
        child.wait(timeout=10)


def test_withdraw_releases_reserved_lease_after_projection(prepared):
    project, _, _, _ = prepared
    capacity.save_state(
        {
            "leases": {
                "withdraw-lease": {
                    "lease_id": "withdraw-lease",
                    "dispatch_id": "withdraw-test",
                    "state": "active",
                    "agent": "codex",
                    "machine_id": capacity.machine_id(),
                    "lease_schema": capacity.LEASE_SCHEMA,
                    "launch_state": "reserved",
                    "project_root": str(project),
                    "expires_at": capacity.iso(
                        capacity.utc_now() + dt.timedelta(hours=1)
                    ),
                }
            },
            "cooldowns": {},
        }
    )
    code, result = withdraw()
    assert code == 0, result
    assert (
        capacity.load_state()["leases"]["withdraw-lease"]["state"]
        == "withdrawn"
    )


def test_withdraw_retry_releases_after_initial_cleanup_failure(prepared, monkeypatch):
    project, _, _, _ = prepared
    capacity.save_state(
        {
            "leases": {
                "withdraw-retry-lease": {
                    "lease_id": "withdraw-retry-lease",
                    "dispatch_id": "withdraw-test",
                    "state": "active",
                    "agent": "codex",
                    "machine_id": capacity.machine_id(),
                    "lease_schema": capacity.LEASE_SCHEMA,
                    "launch_state": "reserved",
                    "project_root": str(project),
                    "expires_at": capacity.iso(
                        capacity.utc_now() + dt.timedelta(hours=1)
                    ),
                }
            },
            "cooldowns": {},
        }
    )

    def fail_cleanup(*_args):
        raise RuntimeError("capacity unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(dispatch, "_release_terminal_capacity", fail_cleanup)
        code, result = withdraw()
        assert code == 0, result
        assert capacity.load_state()["leases"]["withdraw-retry-lease"]["state"] == "active"

    code, result = withdraw()
    assert code == 0 and result["status"] == "already withdrawn", result
    assert (
        capacity.load_state()["leases"]["withdraw-retry-lease"]["state"]
        == "withdrawn"
    )


def test_claimed_spawn_intent_waits_for_stale_window(prepared, claimed, monkeypatch):
    _, authority, _, _ = prepared
    now = time.time()
    entry = json.loads(claimed.read_text())
    entry.update(queue_worker_spawn_intent=True, queue_worker_spawn_intent_at=now)
    claimed.write_text(json.dumps(entry))
    original = claimed.read_bytes()
    row = attempt_row(authority)
    code, result = withdraw()
    assert code == 1 and "claim-stale" in result["reason"], result
    assert claimed.read_bytes() == original
    assert attempt_row(authority) == row
    monkeypatch.setattr(dispatch.time, "time", lambda: now + dispatch.QUEUE_CLAIM_STALE_S + 1)
    code, result = withdraw()
    assert code == 0, result
    assert Path(result["archived_carrier"]).read_bytes() == original
    assert not claimed.exists()


@pytest.mark.parametrize("ownership", ["live", "indeterminate"])
def test_claimed_launch_ownership_rechecked_under_lock(prepared, claimed, monkeypatch, ownership):
    project, authority, attempt, _ = prepared
    record_dead_worker_evidence(project, authority, attempt)
    row = attempt_row(authority)
    original = claimed.read_bytes()
    entry = json.loads(claimed.read_text())
    launch_pids = {
        entry.get("queue_claimer_pid"), entry.get("queue_launcher_pid"),
    }
    original_lock = dispatch._queue_mutation_lock
    original_status = dispatch._queue_claim_identity_status
    locked = False

    @contextlib.contextmanager
    def queue_lock(path):
        nonlocal locked
        with original_lock(path):
            locked = True
            yield

    def identity_status(pid, identity):
        if locked and pid in launch_pids:
            return ownership, "launch_changed_before_lock"
        return original_status(pid, identity)

    monkeypatch.setattr(dispatch, "_queue_mutation_lock", queue_lock)
    monkeypatch.setattr(dispatch, "_queue_claim_identity_status", identity_status)
    code, result = withdraw()
    assert code == 1 and "launch ownership" in result["reason"], result
    assert locked
    assert claimed.read_bytes() == original
    assert attempt_row(authority) == row


@pytest.mark.parametrize("kind", ["unknown", "worker", "worker-unknown"])
@pytest.mark.parametrize("under_lock", [False, True])
@pytest.mark.parametrize("final", [False, True])
def test_parallel_carriers_refuse_unsafe_claim(prepared, claimed, monkeypatch, kind, under_lock, final):
    project, authority, attempt, carrier = prepared
    if not final:
        record_dead_worker_evidence(project, authority, attempt)
    if final:
        assert authority.commit_terminal(attempt.attempt_id, terminal_state="blocked").committed
    entry = json.loads(claimed.read_text())
    carrier.write_text(json.dumps(dispatch._sanitize_restore_envelope(entry, increment_recovery_count=False)))
    original_lock = dispatch._queue_mutation_lock
    original_status = dispatch._queue_claim_identity_status
    locked = False
    carrier_pids = {
        entry.get("queue_claimer_pid"), entry.get("queue_launcher_pid"),
    }
    child = None
    if kind in {"worker", "worker-unknown"}:
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        entry.update(queue_worker_pid=child.pid, queue_worker_identity=ledger.process_identity(child.pid))

    @contextlib.contextmanager
    def queue_lock(path):
        nonlocal locked
        with original_lock(path):
            locked = True
            claimed.write_text(json.dumps(entry))
            yield

    def identity_status(pid, identity):
        if kind == "unknown" and (
            not under_lock or (locked and pid in carrier_pids)
        ):
            return "indeterminate", "identity_provider_exception:OSError"
        if kind == "worker-unknown" and child and pid == child.pid:
            return "indeterminate", "identity_provider_exception:OSError"
        return original_status(pid, identity)

    if not under_lock:
        claimed.write_text(json.dumps(entry))
    monkeypatch.setattr(dispatch, "_queue_mutation_lock", queue_lock)
    monkeypatch.setattr(dispatch, "_queue_claim_identity_status", identity_status)
    row = attempt_row(authority)
    record = ledger.record_path("withdraw-test").read_bytes()
    plain = carrier.read_bytes()
    try:
        code, result = withdraw()
        assert code == 1, result
        assert locked == under_lock
        assert attempt_row(authority) == row
        assert ledger.record_path("withdraw-test").read_bytes() == record
        assert carrier.read_bytes() == plain
        assert json.loads(claimed.read_text()) == entry
        if child:
            assert child.poll() is None
    finally:
        if child:
            child.terminate()
            child.wait(timeout=10)


@pytest.mark.parametrize("plain,claim_count", [(True, 1), (False, 2), (True, 2)])
def test_parallel_carriers_archive_all(prepared, claimed, monkeypatch, tmp_path, plain, claim_count):
    project, authority, attempt, carrier = prepared
    record_dead_worker_evidence(project, authority, attempt)
    entry = json.loads(claimed.read_text())
    paths = [claimed]
    if plain:
        carrier.write_text(json.dumps(dispatch._sanitize_restore_envelope(entry, increment_recovery_count=False)))
        paths.append(carrier)
    if claim_count == 2:
        second = claimed.with_name(claimed.name + "-second")
        second.write_text(json.dumps(entry))
        paths.append(second)
    originals = {path: path.read_bytes() for path in paths}
    before = snapshot(tmp_path)
    row = attempt_row(authority)
    code, result = withdraw("--dry-run")
    assert code == 0, result
    assert {step["record"] for step in result["plan"][2:]} == {str(path) for path in paths}
    assert snapshot(tmp_path) == before
    assert attempt_row(authority) == row
    original_replace = Path.replace
    archived = {}

    def checked_replace(path, target):
        if path in originals:
            assert attempt_row(authority)["terminal_state"] == "withdrawn"
            assert ledger.read_record("withdraw-test")["terminal_state"] == "withdrawn"
            archived[path] = Path(target)
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", checked_replace)
    code, result = withdraw()
    assert code == 0, result
    assert set(archived) == set(paths)
    assert set(result["archived_carriers"]) == {str(path) for path in archived.values()}
    for path, data in originals.items():
        assert not path.exists()
        assert archived[path].read_bytes() == data
    settled = snapshot(tmp_path)
    assert withdraw()[1]["status"] == "already withdrawn"
    assert snapshot(tmp_path) == settled


def test_queued_withdraw_then_real_drain(prepared):
    project, authority, attempt, carrier = prepared
    original_carrier = carrier.read_bytes()
    code, result = withdraw()
    assert code == 0, result
    row = attempt_row(authority)
    assert row["lifecycle_state"] == "TERMINAL"
    record = ledger.read_record("withdraw-test")
    assert record["state"] == record["terminal_state"] == "withdrawn"
    assert record["liveness_state"] == "withdrawn"
    assert dispatch._dispatch_record_is_terminal(record)
    assert record["worker_still_alive"] is False
    assert record["attempt_id"] == attempt.attempt_id
    assert record["transition_id"] == row["terminal_transition_id"]
    assert record["reason"] == "obsolete"
    event = authority.read_all("SELECT * FROM terminal_outbox WHERE attempt_id = ?", (attempt.attempt_id,))[0]
    assert record["terminal_event_uuid"] == event["event_uuid"]
    assert Path(result["archived_carrier"]).read_bytes() == original_carrier
    assert not carrier.exists()
    # Use the default isolated queue: --queue-dir would skip ledger-orphan restoration.
    drained = subprocess.run(
        [sys.executable, str(SCRIPTS / "goalflight_dispatch.py"), "drain", "--json"],
        cwd=project, text=True, capture_output=True, timeout=45,
    )
    assert drained.returncode == 0, drained.stdout + drained.stderr
    assert not carrier.exists(), drained.stdout
    assert ledger.read_record("withdraw-test")["terminal_state"] == "withdrawn"


def test_prepared_without_ledger_or_carrier(prepared):
    project, authority, _, carrier = prepared
    ledger.record_path("withdraw-test").unlink()
    carrier.unlink()
    code, result = withdraw("--project-root", str(project))
    assert code == 0, result
    assert attempt_row(authority)["lifecycle_state"] == "TERMINAL"
    assert ledger.read_record("withdraw-test")["terminal_state"] == "withdrawn"


@pytest.mark.parametrize("source", ["ledger", "journal"])
def test_live_worker_refused(prepared, source):
    _, authority, attempt, _ = prepared
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        identity = ledger.process_identity(child.pid)
        assert identity and not identity.get("identity_probe_error"), identity
        if source == "ledger":
            record = ledger.read_record("withdraw-test")
            record.update(worker_pid=child.pid, worker_identity=identity)
            ledger.write_record(record)
        else:
            started = authority.start_attempt(attempt.attempt_id, attempt.launch_token)
            assert started.committed
            running = authority.mark_attempt_running(
                attempt.attempt_id, attempt.launch_token,
                launch_epoch=started.value.launch_epoch, worker_instance=identity,
            )
            assert running.committed
        code, result = withdraw()
        assert code == 1, result
        assert "never kills" in result["reason"]
        assert child.poll() is None
        assert attempt_row(authority)["terminal_state"] is None
    finally:
        child.terminate()
        child.wait(timeout=10)


def test_running_worker_without_identity_refused(prepared):
    _, authority, attempt, carrier = prepared
    started = authority.start_attempt(attempt.attempt_id, attempt.launch_token)
    assert started.committed and started.value is not None
    running = authority.mark_attempt_running(
        attempt.attempt_id,
        attempt.launch_token,
        launch_epoch=started.value.launch_epoch,
        worker_instance={},
    )
    assert running.committed

    code, result = withdraw()

    assert code == 1, result
    assert "liveness is indeterminate" in result["reason"]
    assert attempt_row(authority)["lifecycle_state"] == journal.ATTEMPT_RUNNING
    assert carrier.exists()


def test_starting_worker_without_identity_refused(prepared):
    _, authority, attempt, carrier = prepared
    started = authority.start_attempt(attempt.attempt_id, attempt.launch_token)
    assert started.committed and started.value is not None

    code, result = withdraw()

    assert code == 1, result
    assert "liveness is indeterminate" in result["reason"]
    assert attempt_row(authority)["lifecycle_state"] == journal.ATTEMPT_STARTING
    assert carrier.exists()


def test_active_missing_identity_evidence_refused_in_guidance_and_withdraw(prepared):
    project, authority, attempt, carrier = prepared
    started = authority.start_attempt(attempt.attempt_id, attempt.launch_token)
    assert started.committed and started.value is not None
    stale_pid = 999_999_999
    record = ledger.read_record("withdraw-test")
    record.update(
        state="worker_dead",
        worker_pid=stale_pid,
        worker_identity={"pid": stale_pid, "start_token": "gone"},
    )
    ledger.write_record(record)

    guidance = dispatch._completion_refusal_guidance(
        ['dispatch_id="withdraw-test" state="worker_dead"'],
        str(project),
        args=SimpleNamespace(task_ids=["t-missing-evidence"]),
    )

    assert "task t-missing-evidence is held by withdraw-test (state worker_dead)" in guidance
    assert "goalflight_dispatch.py withdraw withdraw-test" in guidance
    assert "withdraw refuses if it can't prove the worker is dead" in guidance

    code, result = withdraw()

    assert code == 1, result
    assert "dead journal worker identity" in result["reason"]
    assert attempt_row(authority)["lifecycle_state"] == journal.ATTEMPT_STARTING
    assert attempt_row(authority)["terminal_state"] is None
    assert ledger.read_record("withdraw-test")["terminal_state"] == "unknown"
    assert carrier.exists()


def test_active_carrier_worker_identity_cannot_bypass_worker_evidence(
    prepared, claimed
):
    _, authority, _, _carrier = prepared
    stale_pid = 999_999_999
    entry = json.loads(claimed.read_text())
    entry.update(
        queue_worker_pid=stale_pid,
        queue_worker_identity={"pid": stale_pid, "start_token": "gone"},
    )
    claimed.write_text(json.dumps(entry))

    code, result = withdraw()

    assert code == 1, result
    assert "dead ledger worker identity" in result["reason"]
    assert attempt_row(authority)["lifecycle_state"] == journal.ATTEMPT_STARTING
    assert claimed.exists()


def test_all_carrier_status_paths_veto_live_worker(prepared, claimed, tmp_path):
    project, authority, attempt, _carrier = prepared
    record_dead_worker_evidence(project, authority, attempt)
    status_path = tmp_path / "live.status.json"
    status_path.write_text(json.dumps({
        "dispatch_id": "withdraw-test",
        "state": "running",
        "worker_alive": True,
    }))
    entry = json.loads(claimed.read_text())
    entry["status_path"] = str(status_path)
    second = claimed.with_name(claimed.name + "-second")
    second.write_text(json.dumps(entry))

    code, result = withdraw()

    assert code == 1, result
    assert "status sidecar reports a live worker" in result["reason"]
    assert claimed.exists() and second.exists()
    assert attempt_row(authority)["terminal_state"] is None
    assert ledger.read_record("withdraw-test")["terminal_state"] == "unknown"


def test_status_sidecar_live_vetoes_dead_ledger_worker(prepared, tmp_path):
    project, authority, attempt, carrier = prepared
    status_path = tmp_path / "live.status.json"
    status_path.write_text(
        json.dumps({
            "dispatch_id": "withdraw-test",
            "state": "running",
            "worker_alive": True,
        })
    )
    record = ledger.read_record("withdraw-test")
    record.update(
        status_path=str(status_path),
        worker_pid=999_999_999,
        worker_identity={"pid": 999_999_999, "start_token": "gone"},
    )
    ledger.write_record(record)
    carrier.write_text(json.dumps(record))
    assert authority.commit_terminal(
        attempt.attempt_id,
        terminal_state="blocked",
        observation={"state": "blocked", "reason": "stale worker"},
    ).committed

    code, result = withdraw()

    assert code == 1, result
    assert "status sidecar reports a live worker" in result["reason"]
    assert attempt_row(authority)["terminal_state"] == "blocked"
    assert carrier.exists()
    assert ledger.read_record("withdraw-test")["terminal_state"] == "unknown"


def test_foreign_controller_refused_and_operator_allowed(prepared):
    _, authority, _, _ = prepared
    code, result = withdraw("--controller-label", "foreign")
    assert code == 1 and "--operator" in result["reason"]
    assert attempt_row(authority)["lifecycle_state"] == "PREPARED"
    code, result = withdraw("--controller-label", "foreign", "--operator")
    assert code == 0 and result["withdrawn_by"] == "operator"


def test_parent_owner_can_withdraw_ownerless_retry(prepared):
    child = write_retry_child(prepared, "withdraw-retry")
    assert not dispatch._record_proves_never_launched(child, [])

    code, result = withdraw(
        "--controller-label", "owner", dispatch_id="withdraw-retry"
    )

    assert code == 0 and result["withdrawn_by"] == "owner", result


def test_non_parent_owner_cannot_withdraw_ownerless_retry(prepared):
    write_retry_child(prepared, "withdraw-retry")

    code, result = withdraw("--controller-label", "foreign", dispatch_id="withdraw-retry")

    assert code == 1 and "dispatch belongs to 'owner'" in result["reason"], result


def test_existing_retry_owner_takes_precedence_over_parent(prepared):
    write_retry_child(
        prepared, "withdraw-retry", controller_label="retry-owner"
    )

    code, result = withdraw("--controller-label", "owner", dispatch_id="withdraw-retry")
    assert code == 1 and "dispatch belongs to 'retry-owner'" in result["reason"], result

    code, result = withdraw(
        "--controller-label", "retry-owner", dispatch_id="withdraw-retry"
    )
    assert code == 0 and result["withdrawn_by"] == "retry-owner", result


def test_ownerless_retry_without_owned_parent_accepts_named_controller(prepared):
    write_retry_child(
        prepared,
        "withdraw-orphan",
        parent_dispatch_id=None,
        dispatch_argv=["--controller-label", "owner"],
    )

    code, result = withdraw(
        "--controller-label", "owner", dispatch_id="withdraw-orphan"
    )

    assert code == 0 and result["status"] == "withdrawn", result
    assert result["withdrawn_by"] == "owner"


# Legacy out-of-shape labels remain readable for authorization checks; dry-run
# verifies normalization without rewriting them through the strict writer.
def test_whitespace_retry_owner_is_treated_as_absent(prepared):
    write_retry_child(
        prepared,
        "withdraw-whitespace-owner",
        parent_dispatch_id=None,
    )
    record = ledger.read_record("withdraw-whitespace-owner")
    record["controller_label"] = "   "
    write_legacy_malformed_record(record)

    code, result = withdraw(
        "--controller-label", "named-controller",
        "--dry-run",
        dispatch_id="withdraw-whitespace-owner",
    )

    assert code == 0 and result["status"] == "dry-run", result
    assert result["withdrawn_by"] == "named-controller"


def test_retry_owner_matching_strips_recorded_label(prepared):
    write_retry_child(
        prepared,
        "withdraw-spaced-owner",
        controller_label="retry-owner",
        parent_dispatch_id=None,
    )
    record = ledger.read_record("withdraw-spaced-owner")
    record["controller_label"] = " retry-owner "
    write_legacy_malformed_record(record)

    code, result = withdraw(
        "--controller-label", "retry-owner",
        "--dry-run",
        dispatch_id="withdraw-spaced-owner",
    )

    assert code == 0 and result["status"] == "dry-run", result
    assert result["withdrawn_by"] == "retry-owner"


def test_malformed_retry_owner_refuses_named_controller(prepared):
    project, *_ = prepared
    write_retry_child(
        prepared,
        "withdraw-malformed-owner",
        parent_dispatch_id=None,
    )
    record = ledger.read_record("withdraw-malformed-owner")
    record["controller_label"] = {}
    write_legacy_malformed_record(record)

    with pytest.raises(
        dispatch._WithdrawOwnerMetadataRefusal,
        match="malformed controller owner metadata",
    ):
        dispatch._withdraw_preflight(SimpleNamespace(
            dispatch_id="withdraw-malformed-owner",
            project_root=str(project),
            controller_label="named-controller",
            operator=False,
        ))

    code, result = withdraw(
        "--controller-label", "named-controller",
        dispatch_id="withdraw-malformed-owner",
    )

    assert code == 1, result
    assert "malformed controller owner metadata" in result["reason"], result


@pytest.mark.parametrize("ancestor_depth", [1, 2], ids=["parent", "grandparent"])
@pytest.mark.parametrize(
    "matching_caller",
    [True, False],
    ids=["stringified-label", "other-label"],
)
def test_malformed_inherited_owner_refuses_without_leaking(
    prepared, ancestor_depth, matching_caller,
):
    project, *_ = prepared
    raw_owner = {"secret": "inherited-owner-sentinel"}
    raw_label = str(raw_owner)
    parent_id = "withdraw-test"
    root_record = ledger.read_record(parent_id)
    root_record["controller_label"] = raw_owner
    write_legacy_malformed_record(root_record)

    for hop in range(1, ancestor_depth):
        next_parent_id = f"withdraw-owner-hop-{hop}"
        ledger.write_record({
            "dispatch_id": next_parent_id,
            "project_root": str(project),
            "controller_label": None,
            "parent_dispatch_id": parent_id,
        })
        parent_id = next_parent_id

    dispatch_id = f"withdraw-inherited-malformed-{ancestor_depth}-{matching_caller}"
    write_retry_child(
        prepared,
        dispatch_id,
        parent_dispatch_id=parent_id,
    )
    caller_label = raw_label if matching_caller else "unrelated-controller"

    code, result = withdraw(
        "--controller-label", caller_label,
        dispatch_id=dispatch_id,
    )

    assert code == 1, result
    reason = result["reason"]
    assert "malformed inherited controller owner metadata" in reason, result
    assert f"ancestor depth {ancestor_depth}" in reason, result
    assert "dict" in reason, result
    assert raw_label not in reason, result


def test_materialized_child_owner_cannot_bypass_malformed_parent(prepared):
    project, *_ = prepared
    raw_owner = {"secret": "materialized-owner-sentinel"}
    raw_label = str(raw_owner)
    parent = ledger.read_record("withdraw-test")
    parent["controller_label"] = raw_owner
    write_legacy_malformed_record(parent)
    write_retry_child(
        prepared,
        "withdraw-materialized-owner",
    )
    child = ledger.read_record("withdraw-materialized-owner")
    child["controller_label"] = raw_label
    write_legacy_malformed_record(child)

    code, result = withdraw(
        "--controller-label", raw_label,
        dispatch_id="withdraw-materialized-owner",
    )

    assert code == 1, result
    reason = result["reason"]
    assert "malformed inherited controller owner metadata" in reason, result
    assert "ancestor depth 1" in reason, result
    assert "dict" in reason, result
    assert raw_label not in reason, result


def test_malformed_nearest_owner_blocks_valid_grandparent(prepared):
    project, *_ = prepared
    raw_owner = {"secret": "nearest-owner-sentinel"}
    raw_label = str(raw_owner)
    middle_id = "withdraw-malformed-nearest"
    write_legacy_malformed_record({
        "dispatch_id": middle_id,
        "project_root": str(project),
        "controller_label": raw_owner,
        "parent_dispatch_id": "withdraw-test",
    })
    write_retry_child(
        prepared,
        "withdraw-malformed-nearest-child",
        parent_dispatch_id=middle_id,
    )

    code, result = withdraw(
        "--controller-label", raw_label,
        dispatch_id="withdraw-malformed-nearest-child",
    )

    assert code == 1, result
    reason = result["reason"]
    assert "malformed inherited controller owner metadata" in reason, result
    assert "ancestor depth 1" in reason, result
    assert "dict" in reason, result
    assert raw_label not in reason, result


def test_nearest_valid_owner_stops_before_malformed_grandparent(prepared):
    project, *_ = prepared
    root_record = ledger.read_record("withdraw-test")
    root_record["controller_label"] = {"secret": "older-owner-sentinel"}
    write_legacy_malformed_record(root_record)
    middle_id = "withdraw-valid-nearest"
    ledger.write_record({
        "dispatch_id": middle_id,
        "project_root": str(project),
        "controller_label": "nearest-owner",
        "parent_dispatch_id": "withdraw-test",
    })
    write_retry_child(
        prepared,
        "withdraw-valid-nearest-child",
        parent_dispatch_id=middle_id,
    )

    code, result = withdraw(
        "--controller-label", "nearest-owner",
        dispatch_id="withdraw-valid-nearest-child",
    )

    assert code == 0 and result["status"] == "withdrawn", result
    assert result["withdrawn_by"] == "nearest-owner"


def test_nonempty_carrier_tail_prevents_never_launched_withdraw_shortcut(
    prepared, tmp_path, monkeypatch,
):
    project, _authority, _attempt, _carrier = prepared
    tail = tmp_path / "partial-worker.tail"
    tail.write_text("partial worker output\n", encoding="utf-8")
    carrier = {
        "dispatch_id": "withdraw-test",
        "project_root": str(project),
        "request": {"tail": str(tail)},
    }
    record = {
        "dispatch_id": "withdraw-test",
        "project_root": str(project),
        "state": "worker_dead",
        "terminal_state": "worker_dead",
    }

    class Reader:
        def read_all(self, *_args):
            return [{
                "lifecycle_state": journal.ATTEMPT_TERMINAL,
                "launch_epoch": 0,
                "worker_instance_json": None,
                "terminal_outcome_json": None,
            }]

    monkeypatch.setattr(journal.Journal, "open_reader", lambda _root: Reader())
    assert not dispatch._record_proves_never_launched(record, [carrier])

    with pytest.raises(dispatch._WithdrawPreflightRefusal):
        dispatch._withdraw_liveness_preflight(
            dispatch_id="withdraw-test",
            attempt={"lifecycle_state": journal.ATTEMPT_STARTING},
            record=record,
            worker={},
            carriers={tmp_path / "carrier.json": carrier},
            status_paths=[],
        )


def test_unproven_retry_refusal_omits_never_launched_withdraw_command(prepared):
    project, _, _, _ = prepared
    child = write_retry_child(prepared, "withdraw-retry")
    assert not dispatch._record_proves_never_launched(child, [])

    with pytest.raises(dispatch.DispatchUsageError) as raised:
        dispatch._pin_redispatch_wip(child, [])

    command = dispatch._withdraw_recovery_command(
        "withdraw-retry", str(project), owner="owner"
    )
    message = str(raised.value)
    assert "advanced dispatch withdraw-retry has no worker cwd to pin" in message
    assert "never-launched record" not in message
    assert command not in message


def test_pin_redispatch_wip_refuses_relative_worker_cwd(prepared, monkeypatch):
    project, _authority, _attempt, _carrier = prepared
    seat = project / "relative-seat"
    seat.mkdir()
    subprocess.run(["git", "init", "-q", str(seat)], check=True)
    subprocess.run(
        [
            "git", "-C", str(seat), "-c", "user.email=test@example.com",
            "-c", "user.name=test", "commit", "--allow-empty", "-q", "-m", "base",
        ],
        check=True,
    )
    monkeypatch.setattr(
        dispatch.goalflight_worktree_pool,
        "try_acquire_worktree_path_lock",
        lambda *_args, **_kwargs: contextlib.nullcontext(),
    )

    with pytest.raises(dispatch.DispatchUsageError, match="worker_cwd is not absolute"):
        dispatch._pin_redispatch_wip(
            {"dispatch_id": "relative-seat", "worker_cwd": "relative-seat"}, []
        )


def test_redispatch_classification_refuses_relative_worker_cwd():
    with pytest.raises(dispatch.DispatchUsageError, match="worker_cwd is not absolute"):
        dispatch._redispatch_record_has_no_advanced_work(
            {
                "dispatch_id": "relative-seat-classification",
                "state": "worker_dead",
                "worker_cwd": "relative-seat",
                "worktree_base_sha": "a" * 40,
            }
        )


def test_moved_root_requires_override(prepared):
    project, authority, _, _ = prepared
    path = ledger.record_path("withdraw-test")
    record = json.loads(path.read_text())
    record["project_root"] = str(project / "gone")
    path.write_text(json.dumps(record))
    code, result = withdraw()
    assert code == 1 and "--project-root" in result["reason"]
    assert attempt_row(authority)["lifecycle_state"] == "PREPARED"
    code, result = withdraw("--project-root", str(project))
    assert code == 0, result
    assert ledger.read_record("withdraw-test")["project_root"] == str(project)


def test_override_must_contain_attempt(prepared, tmp_path):
    _, authority, _, _ = prepared
    other = tmp_path / "other"
    other.mkdir()
    journal.Journal.create(other)
    before = snapshot(tmp_path)
    code, result = withdraw("--project-root", str(other))
    assert code == 1 and "no attempt" in result["reason"]
    assert snapshot(tmp_path) == before
    assert attempt_row(authority)["lifecycle_state"] == "PREPARED"


def test_idempotent_second_call_changes_nothing(prepared, tmp_path):
    assert withdraw()[0] == 0
    before = snapshot(tmp_path)
    time.sleep(1.05)  # Timestamp writers must be observable even at second precision.
    code, result = withdraw("--reason", "different retry reason")
    assert code == 0 and result["status"] == "already withdrawn", result
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("replacement", [None, "replacement-dispatch"])
def test_concurrent_withdrawal_is_noop_after_lock(prepared, monkeypatch, tmp_path, replacement):
    _, authority, _, _ = prepared
    original_lock = dispatch._queue_mutation_lock
    original_write = ledger.write_record
    original_commit = journal.Journal.commit_terminal
    calls = []
    interleaved = False
    settled = None
    settled_row = None

    def write(record):
        calls.append("ledger")
        return original_write(record)

    def commit(self, *args, **kwargs):
        calls.append("journal")
        return original_commit(self, *args, **kwargs)

    @contextlib.contextmanager
    def queue_lock(path):
        nonlocal interleaved, settled, settled_row
        if not interleaved:
            interleaved = True
            args = ["--superseded-by", replacement] if replacement else []
            code, result = withdraw("--operator", *args)
            assert code == 0 and result["status"] == "withdrawn", result
            settled = snapshot(tmp_path)
            settled_row = attempt_row(authority)
        with original_lock(path):
            yield

    monkeypatch.setattr(dispatch, "_queue_mutation_lock", queue_lock)
    monkeypatch.setattr(ledger, "write_record", write)
    monkeypatch.setattr(journal.Journal, "commit_terminal", commit)
    code, result = withdraw()
    assert calls.count("ledger") == 1, calls
    assert calls.count("journal") == 1, calls
    assert code == 0 and result["status"] == "already withdrawn", result
    assert result["withdrawn_by"] == "operator"
    assert snapshot(tmp_path) == settled
    assert attempt_row(authority) == settled_row


@pytest.mark.parametrize("boundary", ["ledger", "carrier"])
def test_partial_withdrawal_retry_repairs_publication(prepared, claimed, monkeypatch, boundary):
    project, authority, attempt, _ = prepared
    record_dead_worker_evidence(project, authority, attempt)
    original = claimed.read_bytes()
    original_replace = Path.replace

    def fail_write(record):
        raise OSError("injected ledger publication failure")

    def fail_replace(path, target):
        if path == claimed:
            raise OSError("injected carrier archival failure")
        return original_replace(path, target)

    with monkeypatch.context() as fault:
        if boundary == "ledger":
            fault.setattr(ledger, "write_record", fail_write)
        else:
            fault.setattr(Path, "replace", fail_replace)
        code, result = withdraw()
        assert code == 1 and "injected" in result["reason"], result
    terminal_row = attempt_row(authority)
    assert terminal_row["terminal_state"] == "withdrawn"
    assert claimed.read_bytes() == original
    code, result = withdraw()
    assert code == 0 and result["status"] == "withdrawn", result
    assert attempt_row(authority) == terminal_row
    assert ledger.read_record("withdraw-test")["terminal_state"] == "withdrawn"
    assert Path(result["archived_carrier"]).read_bytes() == original
    assert not claimed.exists()
    assert withdraw()[1]["status"] == "already withdrawn"


def test_dry_run_writes_nothing(prepared, tmp_path):
    _, authority, _, _ = prepared
    before = snapshot(tmp_path)
    row = attempt_row(authority)
    code, result = withdraw("--dry-run")
    assert code == 0 and result["status"] == "dry-run", result
    assert result["plan"][0]["fields"]["lifecycle_state"] == "TERMINAL"
    assert result["plan"][1]["fields"]["terminal_state"] == "withdrawn"
    assert "dispatch-queue-withdrawn" in result["plan"][2]["move_to"]
    assert snapshot(tmp_path) == before
    assert attempt_row(authority) == row


def test_journal_then_ledger_then_carrier(prepared, monkeypatch):
    _, authority, _, carrier = prepared
    original_write = ledger.write_record
    original_replace = Path.replace
    seen = []

    def checked_write(record):
        assert attempt_row(authority)["lifecycle_state"] == "TERMINAL"
        assert carrier.exists(), "carrier moved before terminal ledger projection"
        seen.append("ledger")
        return original_write(record)

    def checked_replace(path, target):
        if path == carrier:
            assert ledger.read_record("withdraw-test")["terminal_state"] == "withdrawn"
            seen.append("carrier")
        return original_replace(path, target)

    monkeypatch.setattr(ledger, "write_record", checked_write)
    monkeypatch.setattr(Path, "replace", checked_replace)
    assert withdraw()[0] == 0
    assert seen == ["ledger", "carrier"]


@pytest.mark.parametrize("age,allowed", [(1, False), (301, True)])
def test_spawn_intent_stale_window(prepared, age, allowed):
    record = ledger.read_record("withdraw-test")
    record.update(queue_worker_spawn_intent=True, queue_worker_spawn_intent_at=time.time() - age)
    ledger.write_record(record)
    code, result = withdraw()
    assert (code == 0) == allowed, result
    if not allowed:
        assert "claim-stale" in result["reason"]


@pytest.mark.parametrize("replacement", [None, "replacement-dispatch"])
def test_queued_rows_never_own_worktree_and_withdraw_releases_task(prepared, replacement):
    project, authority, _, _ = prepared
    record = ledger.read_record("withdraw-test")
    record.update(worker_cwd=str(project), task_ids=["t-withdraw"])
    ledger.write_record(record)
    fresh = SimpleNamespace(cwd=str(project), dispatch_id="fresh-dispatch", agent="test")
    # Queued rows never own a worktree, even when they record worker_cwd.
    assert dispatch._prepare_attempt_worktree_occupancy(fresh) is None
    dispatch._release_worktree_occupancy_lock(fresh)
    args = ["--superseded-by", replacement] if replacement else []
    code, result = withdraw(*args)
    assert code == 0, result
    terminal = "superseded" if replacement else "withdrawn"
    record = ledger.read_record("withdraw-test")
    assert record["state"] == record["terminal_state"] == terminal
    assert record.get("superseded_by") == replacement
    assert ledger.terminal_state_for(terminal) == terminal
    assert attempt_row(authority)["lifecycle_state"] == "TERMINAL"
    assert dispatch._prepare_attempt_worktree_occupancy(fresh) is None
    dispatch._release_worktree_occupancy_lock(fresh)
    assert dispatch._ledger_task_ids_advanced(
        ["t-withdraw"], self_dispatch_id="fresh-dispatch", self_project_root=str(project),
    ) == (0, 0, "conclusive")
    assert ledger.classify(record) == terminal
    assert status.done_code(record) == 0
    ledger.reconcile_terminal_outbox(project)
    assert ledger.read_record("withdraw-test")["terminal_state"] == terminal
    assert withdraw(*args)[1]["status"] == "already withdrawn"


def _stub_main_admission(monkeypatch):
    monkeypatch.setattr(dispatch, "_validate_before_side_effects", lambda *_args: None)
    monkeypatch.setattr(dispatch, "_stamp_controller_session", lambda *_args: {})
    monkeypatch.setattr(dispatch, "_ensure_assigned_engine_session", lambda *_args: None)
    monkeypatch.setattr(dispatch, "_dispatch_warnings", lambda *_args: [])
    monkeypatch.setattr(dispatch, "_resolve_launch_account_env", lambda *_args: {})
    monkeypatch.setattr(dispatch, "_validate_claude_auth_before_attempt", lambda *_args: None)


def test_unowned_guidance_command_runs_with_operator(tmp_path, monkeypatch):
    project = tmp_path / "unowned-project"
    project.mkdir()
    monkeypatch.chdir(project)
    authority = journal.Journal.create(project)
    prepared_attempt = authority.prepare_attempt("unowned-holder")
    assert prepared_attempt.committed
    record = {
        "dispatch_id": "unowned-holder",
        "project_root": str(project),
        "state": "worker_dead",
        "terminal_state": "worker_dead",
        "task_ids": ["t-unowned"],
        "worker_still_alive": False,
    }
    ledger.write_record(record)
    carrier = dispatch._queue_entry_path("unowned-holder")
    carrier.parent.mkdir(parents=True, exist_ok=True)
    carrier.write_text(json.dumps(record))
    assert authority.commit_terminal(
        prepared_attempt.value.attempt_id,
        terminal_state="worker_dead",
        observation={"state": "worker_dead", "reason": "stale worker"},
    ).committed

    guidance = dispatch._completion_refusal_guidance(
        ['dispatch_id="unowned-holder" state="worker_dead"'],
        str(project),
        args=SimpleNamespace(task_ids=["t-unowned"], dispatch_id="replacement"),
    )
    command = next(
        line.strip()
        for line in guidance.splitlines()
        if "goalflight_dispatch.py" in line and " withdraw " in line
    )
    assert "--operator" in command

    result = subprocess.run(
        shlex.split(command), cwd=project, text=True, capture_output=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert ledger.read_record("unowned-holder")["terminal_state"] == "withdrawn"


@pytest.mark.parametrize(
    "terminal_state", ["worker_dead", "stale_dead", "abandoned", "superseded"],
)
def test_guidance_withdraw_step_runs_for_each_holder_state(
    prepared, terminal_state
):
    project, authority, attempt, carrier = prepared
    record = ledger.read_record("withdraw-test")
    record.update(
        task_ids=["t-guidance"],
        state=terminal_state,
        terminal_state=terminal_state,
        worker_still_alive=False,
    )
    ledger.write_record(record)
    carrier.write_text(json.dumps(record))
    assert authority.commit_terminal(
        attempt.attempt_id,
        terminal_state=terminal_state,
        observation={
            "state": terminal_state,
            "reason": "stale worker",
            **({"withdrawn_by": "owner"} if terminal_state == "superseded" else {}),
        },
    ).committed

    guidance = dispatch._completion_refusal_guidance(
        [f'dispatch_id="withdraw-test" state="{terminal_state}"'],
        str(project),
        args=SimpleNamespace(
            task_ids=["t-guidance"],
            dispatch_id="replacement-guidance",
            _original_argv=[
                "--agent", "codex", "--task", "t-guidance",
                "--dispatch-id", "replacement-guidance",
            ],
        ),
    )
    command = next(
        line.strip()
        for line in guidance.splitlines()
        if "goalflight_dispatch.py" in line and " withdraw " in line
    )
    assert "task t-guidance is held by withdraw-test (state " + terminal_state + ")" in guidance
    assert "withdraw refuses if it can't prove the worker is dead" in guidance
    assert "--superseded-by" not in command
    result = subprocess.run(
        shlex.split(command), cwd=project, text=True, capture_output=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert ledger.read_record("withdraw-test")["terminal_state"] in {
        "withdrawn", "superseded"
    }
    assert dispatch._ledger_task_ids_advanced(
        ["t-guidance"],
        self_dispatch_id="replacement-guidance",
        self_project_root=str(project),
    ) == (0, 0, "conclusive")


@pytest.mark.parametrize("terminal_state", ["worker_dead", "abandoned"])
def test_guidance_withdraw_repairs_final_without_outbox(prepared, terminal_state):
    project, authority, attempt, carrier = prepared
    holder = ledger.read_record("withdraw-test")
    holder.update(
        task_ids=["t-missing-outbox"],
        state=terminal_state,
        terminal_state=terminal_state,
        worker_still_alive=False,
    )
    ledger.write_record(holder)
    carrier.write_text(json.dumps(holder))
    assert authority.commit_terminal(
        attempt.attempt_id,
        terminal_state=terminal_state,
        observation={"state": terminal_state, "reason": "stale worker"},
    ).committed
    with sqlite3.connect(authority.path) as connection:
        connection.execute(
            "DELETE FROM terminal_outbox WHERE attempt_id = ?", (attempt.attempt_id,)
        )

    guidance = dispatch._completion_refusal_guidance(
        [f'dispatch_id="withdraw-test" state="{terminal_state}"'],
        str(project),
        args=SimpleNamespace(
            task_ids=["t-missing-outbox"],
        ),
    )
    withdraw_command = next(
        line.strip()
        for line in guidance.splitlines()
        if "goalflight_dispatch.py" in line and " withdraw " in line
    )
    withdrawn = subprocess.run(
        shlex.split(withdraw_command), cwd=project, text=True,
        capture_output=True, timeout=30,
    )
    assert withdrawn.returncode == 0, withdrawn.stdout + withdrawn.stderr
    assert ledger.read_record("withdraw-test")["terminal_state"] == "withdrawn"
    journal_row = authority.read_all(
        "SELECT * FROM dispatch_attempts WHERE dispatch_id = ?", ("withdraw-test",)
    )[0]
    assert journal_row["terminal_state"] == "withdrawn"
    assert authority.read_all(
        "SELECT * FROM terminal_outbox WHERE attempt_id = ?", (attempt.attempt_id,)
    )


def test_guidance_informs_mixed_holders(prepared):
    project, authority, attempt, carrier = prepared
    holder = ledger.read_record("withdraw-test")
    holder.update(
        task_ids=["t-mixed"], state="worker_dead", terminal_state="worker_dead",
        worker_still_alive=False,
    )
    ledger.write_record(holder)
    carrier.write_text(json.dumps(holder))
    assert authority.commit_terminal(
        attempt.attempt_id,
        terminal_state="worker_dead",
        observation={"state": "worker_dead", "reason": "stale worker"},
    ).committed

    second = authority.prepare_attempt(
        "live-holder", owner_controller_label="owner", owner_session_nonce="test-session",
    )
    assert second.committed and second.value is not None
    started = authority.start_attempt(second.value.attempt_id, second.value.launch_token)
    assert started.committed and started.value is not None
    assert authority.mark_attempt_running(
        second.value.attempt_id,
        second.value.launch_token,
        launch_epoch=started.value.launch_epoch,
        worker_instance={},
    ).committed
    ledger.write_record({
        "dispatch_id": "live-holder", "controller_label": "owner",
        "project_root": str(project), "state": "running",
        "terminal_state": "unknown", "task_ids": ["t-mixed"],
    })

    guidance = dispatch._completion_refusal_guidance(
        [
            'dispatch_id="withdraw-test" state="worker_dead"',
            'dispatch_id="live-holder" state="running"',
        ],
        str(project),
        args=SimpleNamespace(task_ids=["t-mixed"]),
    )

    assert guidance.count("goalflight_dispatch.py withdraw") == 1
    assert "task t-mixed is held by a worker that may still be running (running)" in guidance
    assert "Holder: live-holder" in guidance
    assert "wait for it, or steer it to stop before withdrawing" in guidance


def test_guidance_informs_superseded_without_withdrawn_by(prepared):
    project, authority, attempt, carrier = prepared
    holder = ledger.read_record("withdraw-test")
    holder.update(
        task_ids=["t-superseded"], state="superseded", terminal_state="superseded",
        worker_still_alive=False,
    )
    ledger.write_record(holder)
    carrier.write_text(json.dumps(holder))
    assert authority.commit_terminal(
        attempt.attempt_id,
        terminal_state="superseded",
        observation={"state": "superseded", "reason": "replacement"},
    ).committed

    guidance = dispatch._completion_refusal_guidance(
        ['dispatch_id="withdraw-test" state="superseded"'],
        str(project),
        args=SimpleNamespace(task_ids=["t-superseded"]),
    )

    assert "task t-superseded is held by withdraw-test (state superseded)" in guidance
    assert "goalflight_dispatch.py withdraw withdraw-test" in guidance
    assert "withdraw refuses if it can't prove the worker is dead" in guidance


def test_guidance_prints_each_holder_command(prepared):
    project, authority, attempt, carrier = prepared
    holder = ledger.read_record("withdraw-test")
    holder.update(
        task_ids=["t-900"], state="worker_dead", terminal_state="worker_dead",
        worker_still_alive=False,
    )
    ledger.write_record(holder)
    carrier.write_text(json.dumps(holder))
    assert authority.commit_terminal(
        attempt.attempt_id, terminal_state="worker_dead",
        observation={"state": "worker_dead", "reason": "stale worker"},
    ).committed

    second = authority.prepare_attempt(
        "withdraw-other", owner_controller_label="owner", owner_session_nonce="test-session",
    )
    assert second.committed and second.value is not None
    second_record = {
        "dispatch_id": "withdraw-other", "controller_label": "owner",
        "project_root": str(project), "state": "worker_dead",
        "terminal_state": "worker_dead", "task_ids": ["t-900"],
        "worker_still_alive": False,
    }
    ledger.write_record(second_record)
    second_carrier = dispatch._queue_entry_path("withdraw-other")
    second_carrier.parent.mkdir(parents=True, exist_ok=True)
    second_carrier.write_text(json.dumps(second_record))
    assert authority.commit_terminal(
        second.value.attempt_id, terminal_state="worker_dead",
        observation={"state": "worker_dead", "reason": "stale worker"},
    ).committed

    guidance = dispatch._completion_refusal_guidance(
        [
            'dispatch_id="withdraw-test" state="worker_dead"',
            'dispatch_id="withdraw-other" state="worker_dead"',
        ],
        str(project),
        args=SimpleNamespace(task_ids=["t-900"], dispatch_id="fresh-recovery"),
    )
    commands = [
        line.strip()
        for line in guidance.splitlines()
        if "goalflight_dispatch.py" in line and " withdraw " in line
    ]
    assert len(commands) == 2
    assert "withdraw-test (state worker_dead)" in guidance
    assert "withdraw-other (state worker_dead)" in guidance
    assert guidance.count("Then re-run this dispatch.") == 2
    assert ledger.read_record("withdraw-test")["terminal_state"] == "worker_dead"
    assert ledger.read_record("withdraw-other")["terminal_state"] == "worker_dead"


def test_main_guidance_reports_ledger_holder_without_liveness_probe(
    prepared, monkeypatch, capsys
):
    project, authority, attempt, _carrier = prepared
    started = authority.start_attempt(attempt.attempt_id, attempt.launch_token)
    assert started.committed
    running = authority.mark_attempt_running(
        attempt.attempt_id,
        attempt.launch_token,
        launch_epoch=started.value.launch_epoch,
        worker_instance={"pid": 12345, "start_token": "live"},
    )
    assert running.committed
    monkeypatch.setattr(
        dispatch,
        "_queue_claim_identity_status",
        lambda _pid, _identity: ("live", "test-live"),
    )
    _stub_main_admission(monkeypatch)

    def block(_entry, *, diagnostics=None):
        diagnostics.append('dispatch_id="withdraw-test" state="worker_dead"')
        return {"state": "worker_dead", "reason": "partial_task_supersession"}

    monkeypatch.setattr(dispatch, "_entry_completion_authority", block)

    code = dispatch.main([
        "--agent", "codex", "--shape", "bash", "--dispatch-id", "replacement-live",
        "--task", "t-123", "--prompt", "retry", "--cwd", str(project),
        "--controller-label", "owner",
    ])
    assert code == 64
    guidance = capsys.readouterr().err
    assert "task t-123 is held by withdraw-test (state worker_dead)" in guidance
    assert "withdraw refuses if it can't prove the worker is dead" in guidance
    assert "holder is LIVE" not in guidance


@pytest.mark.parametrize("terminal_state", ["blocked", "abandoned"])
def test_settle_preserves_final_journal(prepared, claimed, tmp_path, terminal_state):
    project, authority, attempt, carrier = prepared
    # The attempt reached STARTING, so supply a dead worker identity before
    # testing final journal projection and carrier archival.
    record_dead_worker_evidence(project, authority, attempt)
    status_path = project / "withdraw.status.json"
    status_path.unlink()
    record = ledger.read_record("withdraw-test")
    record.pop("status_path", None)
    ledger.write_record(record)
    # Include both plain and claimed carriers; all must be archived.
    carrier.write_text(json.dumps(dispatch._sanitize_restore_envelope(
        json.loads(claimed.read_text()), increment_recovery_count=False,
    )))
    assert authority.commit_terminal(
        attempt.attempt_id, terminal_state=terminal_state,
        observation={"reason": "original outcome", "state": "queued"},
    ).committed
    row = attempt_row(authority)
    outbox = authority.read_all("SELECT * FROM terminal_outbox")
    before = snapshot(tmp_path)
    code, preview = withdraw("--dry-run")
    assert code == 0 and preview["action"] == "settled", preview
    assert preview["plan"][0]["fields"] == preview["plan"][0]["terminal_outbox"] == {}
    assert snapshot(tmp_path) == before
    code, result = withdraw()
    assert code == 0 and result["status"] == "settled", result
    expected = "withdrawn" if terminal_state == "abandoned" else terminal_state
    record = ledger.read_record("withdraw-test")
    assert record["state"] == record["terminal_state"] == expected
    assert ledger.terminal_state_for(record["state"]) != "unknown"
    if terminal_state == "abandoned":
        assert record["journal_terminal_state"] == "abandoned"
    assert attempt_row(authority) == row
    assert authority.read_all("SELECT * FROM terminal_outbox") == outbox
    assert not carrier.exists() and not claimed.exists()
    assert len(result["archived_carriers"]) == 2
    settled = snapshot(tmp_path)
    assert withdraw()[1]["status"] == "settled"
    assert snapshot(tmp_path) == settled


def test_dry_run_accepts_proven_never_launched_terminal_record(prepared, monkeypatch):
    project, authority, attempt, carrier = prepared
    assert authority.commit_terminal(
        attempt.attempt_id, terminal_state="worker_dead",
        observation={"reason": "partial_task_supersession"},
    ).committed
    record = ledger.read_record("withdraw-test")
    record.update(
        state="worker_dead", terminal_state="worker_dead",
        status_path=str(project / "missing.status.json"),
    )
    ledger.write_record(record)
    carrier.write_text(json.dumps(record))
    monkeypatch.setattr(
        dispatch,
        "_record_proves_never_launched",
        lambda _record, _carriers, **_kwargs: True,
    )

    code, result = withdraw("--dry-run")
    assert code == 0 and result["status"] == "dry-run", result


def test_withdraw_accepts_stale_nonterminal_never_launched_record(
    prepared, monkeypatch
):
    write_never_launched_record(prepared, monkeypatch=monkeypatch)

    code, result = withdraw("--controller-label", "owner")

    assert code == 0 and result["status"] == "withdrawn", result


def test_withdraw_accepts_stale_waiting_capacity_without_status(prepared):
    project, authority, attempt, carrier = prepared
    record = write_never_launched_record(prepared)
    stale_claimant = 2**30
    lease_id = "withdraw-never-launched-lease"
    record.update(
        state="waiting_capacity",
        attempt_id=attempt.attempt_id,
        launch_token=attempt.launch_token,
        launch_epoch=0,
        agent="codex",
        engine="codex",
        account="default",
        transport="dispatch",
        hostname="fixture-host",
        claimant_pid=stale_claimant,
        claimant_identity={"pid": stale_claimant, "start_token": "exited"},
        logical_session_id="withdraw-test",
        lease_id=lease_id,
        remote_lease_id=lease_id,
        stdout_path=str(project / "withdraw-test.tail"),
        updated_at="2020-01-01T00:00:00Z",
    )
    write_legacy_malformed_record(record)
    capacity.save_state(
        {
            "leases": {
                lease_id: {
                    "lease_id": lease_id,
                    "dispatch_id": "withdraw-test",
                    "state": "active",
                    "agent": "codex",
                    "machine_id": capacity.machine_id(),
                    "lease_schema": capacity.LEASE_SCHEMA,
                    "launch_state": "reserved",
                    "claimant_pid": stale_claimant,
                    "project_root": str(project),
                    "expires_at": capacity.iso(
                        capacity.utc_now() + dt.timedelta(hours=1)
                    ),
                }
            },
            "cooldowns": {},
        }
    )
    carrier.unlink()

    code, result = withdraw("--controller-label", "owner")

    assert code == 0 and result["status"] == "withdrawn", result
    assert attempt_row(authority)["terminal_state"] == "withdrawn"
    assert ledger.read_record("withdraw-test")["state"] == "withdrawn"
    assert capacity.load_state()["leases"][lease_id]["state"] == "withdrawn"
    after_withdrawal = snapshot(project)

    code, retry = withdraw("--controller-label", "owner")

    assert code == 0 and retry["status"] == "already withdrawn", retry
    assert snapshot(project) == after_withdrawal


def test_withdraw_projects_matching_terminal_prelaunch_journal(prepared):
    project, authority, attempt, carrier = prepared
    lease_id = "terminal-prelaunch-lease"
    observation = {
        "reason": "worktree_occupied",
        "terminal_state": "failed_worktree",
        "reconciliation": {
            "source": "goalflight_dispatch.drain",
            "basis": "inferred_abandonment",
            "reason": "never_launched_no_final",
            "process_evidence": "no_recorded_pid",
            "status_evidence": "status_file_absent",
            "output_evidence": "no_worker_output",
            "lease_evidence": "lease_absent",
            "controller_evidence": "recorded_controller_beacon_absent",
            "progress_age_s": 7200.0,
            "checked_output": True,
            "observed_outcome": False,
        },
        "outcome": {"reason": "worktree_occupied"},
        "prelaunch_failure": True,
        "state": "failed_worktree",
        "worker_still_alive": False,
    }
    assert authority.commit_terminal(
        attempt.attempt_id,
        terminal_state="failed_worktree",
        observation=observation,
    ).committed
    attempt_before = attempt_row(authority)
    outbox_before = authority.read_all("SELECT * FROM terminal_outbox")
    record = ledger.read_record("withdraw-test")
    record.update(
        state="waiting_capacity",
        terminal_state="unknown",
        attempt_id=attempt.attempt_id,
        launch_epoch=0,
        lease_id=lease_id,
        status_path=str(project / "missing.status.json"),
        stdout_path=str(project / "missing.tail"),
    )
    proof_record = {
        "dispatch_id": "withdraw-test",
        "project_root": str(project),
        "state": "failed_worktree",
        "terminal_state": "failed_worktree",
        "attempt_id": attempt.attempt_id,
        "status_path": record["status_path"],
        "stdout_path": record["stdout_path"],
        "reason": "worktree_occupied",
        "outcome": {
            "terminal_state": "failed_worktree",
            "reason": "worktree_occupied",
            "prelaunch_failure": True,
        },
    }
    assert dispatch._record_proves_never_launched(proof_record, [])
    write_legacy_malformed_record(record)
    carrier.write_text(json.dumps(record))
    capacity.save_state(
        {
            "leases": {
                lease_id: {
                    "lease_id": lease_id,
                    "dispatch_id": "withdraw-test",
                    "state": "active",
                    "agent": "codex",
                    "machine_id": capacity.machine_id(),
                    "lease_schema": capacity.LEASE_SCHEMA,
                    "launch_state": "reserved",
                    "project_root": str(project),
                }
            },
            "cooldowns": {},
        }
    )

    code, result = withdraw("--controller-label", "owner")

    assert code == 0 and result["status"] == "settled", result
    projected = ledger.read_record("withdraw-test")
    assert projected["state"] == projected["terminal_state"] == "failed_worktree"
    assert projected["attempt_id"] == attempt.attempt_id
    assert capacity.load_state()["leases"][lease_id]["state"] == "failed_worktree"
    assert attempt_row(authority) == attempt_before
    assert authority.read_all("SELECT * FROM terminal_outbox") == outbox_before


def test_withdraw_refuses_terminal_nonzero_epoch_without_worker_identity(prepared):
    project, authority, attempt, carrier = prepared
    started = authority.start_attempt(attempt.attempt_id, attempt.launch_token)
    assert started.committed and started.value is not None
    assert authority.commit_terminal(
        attempt.attempt_id,
        terminal_state="failed_worktree",
        observation={"reason": "worker identity missing", "state": "failed_worktree"},
    ).committed
    attempt_before = attempt_row(authority)
    lease_id = "terminal-unknown-worker-lease"
    record = ledger.read_record("withdraw-test")
    record.update(
        state="waiting_capacity",
        terminal_state="unknown",
        attempt_id=attempt.attempt_id,
        launch_epoch=started.value.launch_epoch,
        lease_id=lease_id,
        status_path=str(project / "missing.status.json"),
    )
    write_legacy_malformed_record(record)
    carrier.write_text(json.dumps(record))
    capacity.save_state(
        {
            "leases": {
                lease_id: {
                    "lease_id": lease_id,
                    "dispatch_id": "withdraw-test",
                    "state": "active",
                    "agent": "codex",
                    "machine_id": capacity.machine_id(),
                    "lease_schema": capacity.LEASE_SCHEMA,
                    "launch_state": "reserved",
                    "project_root": str(project),
                }
            },
            "cooldowns": {},
        }
    )
    record_before = ledger.read_record("withdraw-test")

    code, result = withdraw("--controller-label", "owner")

    assert code == 1 and "liveness is indeterminate" in result["reason"], result
    assert ledger.read_record("withdraw-test") == record_before
    assert capacity.load_state()["leases"][lease_id]["state"] == "active"
    assert attempt_row(authority) == attempt_before
    assert carrier.exists()


def test_withdraw_refuses_live_waiting_capacity_claimant_without_status(
    prepared,
):
    _project, authority, _attempt, carrier = prepared
    record = write_never_launched_record(prepared)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        identity = ledger.process_identity(child.pid)
        assert identity and identity.get("start_token"), identity
        record.update(
            state="waiting_capacity",
            claimant_pid=child.pid,
            claimant_identity=identity,
            updated_at="2020-01-01T00:00:00Z",
        )
        write_legacy_malformed_record(record)
        carrier.unlink()

        code, result = withdraw("--dry-run", "--controller-label", "owner")

        assert code == 1 and "ledger claimant" in result["reason"], result
        assert "live" in result["reason"]
        assert attempt_row(authority)["lifecycle_state"] == journal.ATTEMPT_PREPARED
        assert child.poll() is None
    finally:
        if child.poll() is None:
            child.terminate()
        child.wait(timeout=10)


def test_withdraw_refuses_unknown_worker_pid_without_status(prepared, monkeypatch):
    _project, authority, _attempt, carrier = prepared
    record = write_never_launched_record(prepared)
    record.update(
        state="waiting_capacity",
        worker_pid=2**30,
        worker_identity={"pid": 2**30, "start_token": "unknown"},
        updated_at="2020-01-01T00:00:00Z",
    )
    write_legacy_malformed_record(record)
    carrier.unlink()
    monkeypatch.setattr(
        dispatch,
        "_queue_claim_identity_status",
        lambda _pid, _identity: ("indeterminate", "identity_provider_exception:OSError"),
    )

    code, result = withdraw("--dry-run", "--controller-label", "owner")

    assert code == 1 and "indeterminate" in result["reason"], result
    assert attempt_row(authority)["lifecycle_state"] == journal.ATTEMPT_PREPARED


def test_withdraw_refuses_fresh_nonterminal_never_launched_record(
    prepared, monkeypatch
):
    write_never_launched_record(
        prepared,
        started_at=dt.datetime.now(dt.timezone.utc).isoformat(),
        monkeypatch=monkeypatch,
    )

    code, result = withdraw("--dry-run", "--controller-label", "owner")

    assert code == 1 and "claim-stale window" in result["reason"], result
    assert "status_file_absent" not in result["reason"]


def test_withdraw_missing_status_stays_indeterminate_when_worker_pid_is_recorded(
    prepared, monkeypatch
):
    write_never_launched_record(
        prepared,
        worker_pid=2**30,
        worker_identity={"pid": 2**30, "start_token": "exited-test-worker"},
        monkeypatch=monkeypatch,
    )
    monkeypatch.setattr(
        dispatch,
        "_queue_claim_identity_status",
        lambda _pid, _identity: ("dead", "test worker exited"),
    )

    code, result = withdraw("--dry-run", "--controller-label", "owner")

    assert code == 1 and "status_file_absent" in result["reason"], result
    assert "worker liveness is indeterminate" in result["reason"], result


@pytest.mark.parametrize("alive", [False, True])
def test_settle_releases_capacity_only_for_dead_worker(prepared, alive):
    project, authority, attempt, carrier = prepared
    assert authority.commit_terminal(attempt.attempt_id, terminal_state="blocked").committed
    row = attempt_row(authority)
    assert ledger.read_record("withdraw-test")["state"] == "queued"
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        identity = ledger.process_identity(child.pid)
        assert identity and identity.get("start_token"), identity
        capacity.save_state({
            "leases": {
                "settle-lease": {
                    "lease_id": "settle-lease",
                    "dispatch_id": "withdraw-test",
                    "state": "active",
                    "agent": "codex",
                    "machine_id": capacity.machine_id(),
                    "lease_schema": capacity.LEASE_SCHEMA,
                    "launch_state": "attached",
                    "project_root": str(project),
                    "worker_pid": child.pid,
                    "worker_identity": identity,
                    "expires_at": capacity.iso(capacity.utc_now() + dt.timedelta(hours=1)),
                },
            },
            "cooldowns": {},
        })
        if not alive:
            child.terminate()
            child.wait(timeout=10)

        code, result = withdraw()
        assert code == 0 and result["status"] == "settled", result
        assert ledger.read_record("withdraw-test")["terminal_state"] == "blocked"
        assert attempt_row(authority) == row
        assert not carrier.exists()
        lease = capacity.load_state()["leases"]["settle-lease"]
        assert lease["state"] == ("active" if alive else "blocked")
        if alive:
            assert child.poll() is None
            assert "released_at" not in lease
        else:
            assert lease["released_at"]
            assert lease["reason"] == "dispatch_terminal"
    finally:
        if child.poll() is None:
            child.terminate()
        child.wait(timeout=10)


def test_settle_refuses_live_worker_in_parallel_carrier(prepared, claimed):
    _, authority, attempt, carrier = prepared
    assert authority.commit_terminal(attempt.attempt_id, terminal_state="blocked").committed
    entry = json.loads(claimed.read_text())
    carrier.write_text(json.dumps(dispatch._sanitize_restore_envelope(entry, increment_recovery_count=False)))
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        entry.update(queue_worker_pid=child.pid, queue_worker_identity=ledger.process_identity(child.pid))
        claimed.write_text(json.dumps(entry))
        row = attempt_row(authority)
        original = ledger.record_path("withdraw-test").read_bytes()
        code, result = withdraw()
        assert code == 1 and "never kills" in result["reason"], result
        assert carrier.exists() and claimed.exists()
        assert ledger.record_path("withdraw-test").read_bytes() == original
        assert attempt_row(authority) == row
        assert child.poll() is None
    finally:
        child.terminate()
        child.wait(timeout=10)


def test_final_outcome_arriving_before_withdraw_lock_is_settled(prepared, monkeypatch):
    _, authority, attempt, carrier = prepared
    original_lock = dispatch._queue_mutation_lock
    row = None

    @contextlib.contextmanager
    def queue_lock(path):
        nonlocal row
        assert authority.commit_terminal(attempt.attempt_id, terminal_state="blocked").committed
        row = attempt_row(authority)
        with original_lock(path):
            yield

    monkeypatch.setattr(dispatch, "_queue_mutation_lock", queue_lock)
    code, result = withdraw()
    assert code == 0 and result["status"] == "settled", result
    assert attempt_row(authority) == row
    assert ledger.read_record("withdraw-test")["terminal_state"] == "blocked"
    assert not carrier.exists()


@pytest.fixture
def drain_prepared(prepared, monkeypatch):
    project, _, _, carrier = prepared
    record = ledger.read_record("withdraw-test")
    record.update(dispatch_argv=["--agent", "codex", "--dispatch-id", "withdraw-test"],
                  agent="codex")
    ledger.write_record(record)
    carrier.write_text(json.dumps(record))
    monkeypatch.setattr(dispatch, "_release_stale_capacity_for_drain", lambda: None)
    monkeypatch.setattr(dispatch, "_run_drain_prelaunch_hook", lambda _: None)
    monkeypatch.setattr(dispatch, "_drain_capacity_slots", lambda: {"global_remaining": 1})
    def forbidden(*args, **kwargs):
        pytest.fail("drain must not bind or launch a final dispatch")
    monkeypatch.setattr(dispatch, "_drain_launch_argv", forbidden)
    monkeypatch.setattr(dispatch, "_bind_dispatch_worktree", forbidden)
    monkeypatch.setattr(dispatch, "_prepare_attempt_worktree_occupancy", forbidden)
    return SimpleNamespace(queue_dir=None, claim_stale_s=0, capacity_wait_s=0, limit=0)


@pytest.mark.parametrize("terminal_state", ["blocked", "blocked_capacity"])
@pytest.mark.parametrize("carrier_present", [False, True])
def test_drain_settles_before_restore_or_launch(prepared, drain_prepared, monkeypatch, carrier_present, terminal_state):
    _, authority, attempt, carrier = prepared
    assert authority.commit_terminal(attempt.attempt_id, terminal_state=terminal_state).committed
    row = attempt_row(authority)
    if not carrier_present:
        carrier.unlink()
    def no_restore(*args, **kwargs):
        pytest.fail("final journal attempt must never restore a carrier")
    monkeypatch.setattr(dispatch, "_republish_prelaunch_queue_carrier", no_restore)
    result = dispatch._drain_queue_once(drain_prepared)
    assert result["settled"] == 1, result
    assert result["launched"] == result["created"] == result["remaining"] == 0
    assert any(d.get("status") == "settled" for d in result["details"])
    assert ledger.read_record("withdraw-test")["terminal_state"] == terminal_state
    assert attempt_row(authority) == row
    assert not carrier.exists()
    again = dispatch._drain_queue_once(drain_prepared)
    assert again["settled"] == again["launched"] == again["created"] == 0


def test_abandoned_settlement_survives_sidecar_replay(prepared, tmp_path):
    project, authority, attempt, _ = prepared
    sidecar = tmp_path / "terminal-status.json"
    sidecar.write_text(json.dumps({"dispatch_id": "withdraw-test", "state": "blocked"}))
    record = ledger.read_record("withdraw-test")
    record.update(status_path=str(sidecar), task_ids=["t-withdraw"])
    ledger.write_record(record)
    assert authority.commit_terminal(attempt.attempt_id, terminal_state="abandoned").committed
    row = attempt_row(authority)
    outbox = authority.read_all("SELECT * FROM terminal_outbox")
    assert withdraw()[1]["status"] == "settled"
    assert authority.read_all("SELECT * FROM terminal_outbox") == outbox
    for _ in range(2):
        result = ledger.reconcile_terminal_outbox(project)
        assert result["already_terminal"] == 1, result
        record = ledger.read_record("withdraw-test")
        assert record["state"] == record["terminal_state"] == "withdrawn"
        assert record["journal_terminal_state"] == "abandoned"
        assert ledger.terminal_state_for(record["state"]) == "withdrawn"
        assert dispatch._ledger_task_ids_advanced(
            ["t-withdraw"], self_dispatch_id="replacement", self_project_root=str(project),
        ) == (0, 0, "conclusive")
        assert attempt_row(authority) == row


@pytest.mark.parametrize("unreadable", [False, True])
@pytest.mark.parametrize("state", ["queued", "blocked_capacity"])
def test_drain_nonfinal_or_unreadable_still_reaches_launch(prepared, drain_prepared, monkeypatch, tmp_path, unreadable, state):
    _, authority, _, carrier = prepared
    record = ledger.read_record("withdraw-test")
    record["state"] = state
    ledger.write_record(record)
    carrier.write_text(json.dumps(record))
    if unreadable:
        def broken(*args, **kwargs):
            raise OSError("unreadable journal")
        monkeypatch.setattr(journal.Journal, "open_reader", broken)
    before = snapshot(tmp_path)
    assert dispatch._settle_final_for_drain(json.loads(carrier.read_text()), carrier.parent) is None
    assert snapshot(tmp_path) == before
    class ReachedLaunch(Exception):
        pass
    def reached(*args, **kwargs):
        raise ReachedLaunch()
    # Instrument the existing claim boundary when its reader is unavailable;
    # an error escaping the new guard must never satisfy this assertion.
    monkeypatch.setattr(dispatch, "_queue_launch_token" if unreadable else "_drain_launch_argv", reached)
    with pytest.raises(ReachedLaunch):
        dispatch._drain_queue_once(drain_prepared)
    assert ledger.read_record("withdraw-test")["terminal_state"] == "unknown"


def test_withdraw_defaults_to_environment(prepared, monkeypatch):
    def no_ancestry():
        pytest.fail("explicit environment label must precede ancestry")
    monkeypatch.setattr(dispatch.goalflight_session_status, "_controller_process_ancestry", no_ancestry)
    code, result = withdraw()
    assert code == 0 and result["withdrawn_by"] == "owner", result


@pytest.mark.parametrize("matching_labels", [[], ["owner"], ["owner", "other"]])
def test_withdraw_defaults_to_unique_active_ancestor(prepared, monkeypatch, tmp_path, matching_labels):
    _, authority, _, _ = prepared
    monkeypatch.delenv("GOALFLIGHT_CONTROLLER_LABEL")
    ancestor = {"pid": 123456, "start_token": "ancestor-start"}
    monkeypatch.setattr(dispatch.goalflight_session_status, "_controller_process_ancestry", lambda: (ancestor,))
    for label in matching_labels:
        assert authority.claim_or_renew_lease(label, principal=ancestor).committed
    # An ACTIVE non-ancestor must not introduce ambiguity (including PID reuse).
    assert authority.claim_or_renew_lease(
        "unrelated", principal={**ancestor, "start_token": "different-start"},
    ).committed
    before = snapshot(tmp_path)
    code, result = withdraw()
    if len(matching_labels) == 1:
        assert code == 0 and result["withdrawn_by"] == "owner", result
    else:
        assert code == 1 and "--controller-label" in result["reason"], result
        assert snapshot(tmp_path) == before
