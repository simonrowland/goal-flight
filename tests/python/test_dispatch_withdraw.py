"""Withdrawal closes authority before removing its runnable queue carrier."""
from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from support import SCRIPTS

import goalflight_dispatch as dispatch
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


def withdraw(*args):
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = dispatch.main(["withdraw", "withdraw-test", "--reason", "obsolete", "--json", *args])
    return code, json.loads(output.getvalue())


def snapshot(root):
    # SQLite read-only connections maintain transient WAL/SHM coordination files.
    # Compare persistent bytes; journal rows are also checked independently.
    return {
        str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*")
        if path.is_file() and not path.name.endswith((".sqlite3-shm", ".sqlite3-wal"))
    }


def attempt_row(authority):
    return authority.read_all("SELECT * FROM dispatch_attempts WHERE dispatch_id = ?", ("withdraw-test",))[0]


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
    _, authority, _, carrier = prepared
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
    _, authority, _, _ = prepared
    row = attempt_row(authority)
    original = claimed.read_bytes()
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
        if locked:
            return ownership, "launch_changed_before_lock"
        return original_status(pid, identity)

    monkeypatch.setattr(dispatch, "_queue_mutation_lock", queue_lock)
    monkeypatch.setattr(dispatch, "_queue_claim_identity_status", identity_status)
    code, result = withdraw()
    assert code == 1 and "launch ownership" in result["reason"], result
    assert locked
    assert claimed.read_bytes() == original
    assert attempt_row(authority) == row


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


def test_foreign_controller_refused_and_operator_allowed(prepared):
    _, authority, _, _ = prepared
    code, result = withdraw("--controller-label", "foreign")
    assert code == 1 and "--operator" in result["reason"]
    assert attempt_row(authority)["lifecycle_state"] == "PREPARED"
    code, result = withdraw("--controller-label", "foreign", "--operator")
    assert code == 0 and result["withdrawn_by"] == "operator"


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
    _, authority, _, _ = prepared
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
def test_withdraw_releases_seat_and_task(prepared, replacement):
    project, authority, _, _ = prepared
    record = ledger.read_record("withdraw-test")
    record.update(worker_cwd=str(project), task_ids=["t-withdraw"])
    ledger.write_record(record)
    fresh = SimpleNamespace(cwd=str(project), dispatch_id="fresh-dispatch", agent="test")
    # Exercise the real ledger + kernel-lock admission gate, not a mocked verdict.
    with pytest.raises(dispatch.DispatchUsageError, match="withdraw-test"):
        dispatch._prepare_attempt_worktree_occupancy(fresh)
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
