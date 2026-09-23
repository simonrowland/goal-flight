"""Hermetic regressions for nested carrier and ingestion locks."""

import contextlib
import errno
import hashlib
import inspect
import json
import os
from pathlib import Path
import subprocess
import signal
import sqlite3
import sys
import tempfile
import threading
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import goalflight_messages as messages


@contextlib.contextmanager
def lock_holder(path):
    # Handshake, not a sleep: the child reports only after flock succeeds.
    code = """
import fcntl, sys
with open(sys.argv[1], 'a+') as fh:
    fcntl.flock(fh, fcntl.LOCK_EX)
    print('held', flush=True)
    sys.stdin.read()
"""
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(messages.mail_lock_path(path))],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    try:
        import select
        assert select.select([process.stdout], [], [], 5)[0], "holder did not start"
        assert process.stdout.readline().strip() == "held"
        yield process
    finally:
        process.communicate(timeout=5)


def probe_lock(path):
    code = """
import fcntl, sys
with open(sys.argv[1], 'a+') as fh:
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(3)
"""
    return subprocess.run(
        [sys.executable, "-c", code, str(messages.mail_lock_path(path))],
        timeout=5, check=False,
    ).returncode


def test_stream_ingestion_stream_reentry_completes():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        stream = base / "worker.jsonl"
        original_select = messages._select_ingestion_order

        def load_with_stream_reentry(conn, db_path, identity_hash):
            with messages.carrier_transaction(stream, lock_timeout_secs=0.05):
                return original_select(conn, db_path, identity_hash)

        with messages.carrier_transaction(stream, lock_timeout_secs=0.05):
            with mock.patch.object(messages, "_canonical_envelope_identity", return_value="event"):
                with mock.patch.object(messages, "_select_ingestion_order", side_effect=load_with_stream_reentry):
                    assert messages._ingestion_order_for_envelope(base, {}) > 0


def test_inner_exit_preserves_outer_lock_even_on_exception():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "worker.jsonl"
        alias = Path(tmp) / "alias"
        alias.symlink_to(tmp, target_is_directory=True)
        with messages.mail_lock(path):
            try:
                with messages.mail_lock(alias / path.name, timeout_secs=0.05):
                    raise RuntimeError("inner failure")
            except RuntimeError:
                pass
            assert probe_lock(path) == 3, "inner exit released outer lock"
        assert probe_lock(path) == 0, "outer exit failed to release lock"


def test_cross_process_mail_lock_exclusion():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "worker.jsonl"
        with lock_holder(path):
            try:
                with messages.mail_lock(path, timeout_secs=0.05):
                    raise AssertionError("entered another process's critical section")
            except TimeoutError as exc:
                assert str(messages.mail_lock_path(path)) in str(exc)
        with messages.mail_lock(path, timeout_secs=0.05):
            assert probe_lock(path) == 3
        assert probe_lock(path) == 0


def test_ingestion_lock_timeout_names_path_and_never_writes():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        path = base / messages.INGESTION_IDENTITY_FILE
        real_lock = messages.mail_lock

        @contextlib.contextmanager
        def short_lock(path, *, timeout_secs=None):
            assert timeout_secs is not None, "ingestion lock wait is unbounded"
            assert 0 < timeout_secs <= 30
            with real_lock(path, timeout_secs=0.05):
                yield

        with lock_holder(path):
            with mock.patch.object(messages, "_canonical_envelope_identity", return_value="event"):
                with mock.patch.object(messages, "mail_lock", side_effect=short_lock):
                    try:
                        messages._ingestion_order_for_envelope(base, {})
                    except TimeoutError as exc:
                        assert str(messages.mail_lock_path(path)) in str(exc)
                    else:
                        raise AssertionError("contention treated as success")
        assert not path.exists()
        assert not (base / messages.INGESTION_ORDER_FILE).exists()
        assert not (base / messages.INGESTION_IDENTITY_DB).exists()


def _legacy_map(base, orders):
    (base / messages.INGESTION_IDENTITY_FILE).write_text(json.dumps({
        "schema": messages.INGESTION_IDENTITY_SCHEMA,
        "schema_version": 1,
        "orders": orders,
    }))


def _by_id():
    return mock.patch.object(messages, "_canonical_envelope_identity", side_effect=lambda e: e["id"])


def test_ingestion_order_hit_and_miss_use_sqlite_without_a_map():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        with _by_id():
            first = messages._ingestion_order_for_envelope(base, {"id": "a"})
            second = messages._ingestion_order_for_envelope(base, {"id": "b"})
            with mock.patch.object(messages, "mail_lock", side_effect=AssertionError("hit took the lock")):
                assert messages._ingestion_order_for_envelope(base, {"id": "a"}) == first
        assert second > first
        assert int((base / messages.INGESTION_ORDER_FILE).read_text()) == second
        assert not (base / messages.INGESTION_IDENTITY_FILE).exists()
        with sqlite3.connect(base / messages.INGESTION_IDENTITY_DB) as conn:
            rows = conn.execute("SELECT identity_hash, ingestion_order FROM identities").fetchall()
        assert sorted(order for _, order in rows) == [first, second]
        assert all(len(identity_hash) == 64 for identity_hash, _ in rows)


_CONCURRENT_INGEST = """
import sys
from pathlib import Path
from unittest import mock
sys.path.insert(0, sys.argv[2])
import goalflight_messages as messages
with mock.patch.object(messages, "_canonical_envelope_identity", return_value="same"):
    print(messages._ingestion_order_for_envelope(Path(sys.argv[1]), {}))
"""


def test_same_identity_from_concurrent_processes_gets_one_order():
    scripts = str(Path(messages.__file__).resolve().parent)
    with tempfile.TemporaryDirectory() as tmp:
        procs = [
            subprocess.Popen([sys.executable, "-c", _CONCURRENT_INGEST, tmp, scripts], stdout=subprocess.PIPE, text=True)
            for _ in range(8)
        ]
        outputs = [proc.communicate(timeout=30)[0].strip() for proc in procs]
        assert all(proc.returncode == 0 for proc in procs)
        assert len(set(outputs)) == 1
        with sqlite3.connect(Path(tmp) / messages.INGESTION_IDENTITY_DB) as conn:
            assert conn.execute("SELECT count(*) FROM identities").fetchone() == (1,)
        assert (Path(tmp) / messages.INGESTION_ORDER_FILE).read_text().strip() == outputs[0]


def test_legacy_map_imports_in_one_transaction_and_keeps_orders():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        _legacy_map(base, {"old": 7, "older": 3})
        (base / messages.INGESTION_ORDER_FILE).write_text("7\n")
        with _by_id():
            fresh = messages._ingestion_order_for_envelope(base, {"id": "new"})
            assert messages._ingestion_order_for_envelope(base, {"id": "old"}) == 7
            assert messages._ingestion_order_for_envelope(base, {"id": "older"}) == 3
        assert fresh > 7
        assert not (base / messages.INGESTION_IDENTITY_FILE).exists()
        assert len(list(base.glob(messages.INGESTION_IDENTITY_FILE + ".imported-*"))) == 1


def test_failed_legacy_import_rolls_back_and_keeps_the_map():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        _legacy_map(base, {"a": 1, "b": 2})
        real_connect = sqlite3.connect

        class FailingConn:
            def __init__(self, conn):
                self._conn = conn

            def executemany(self, *args):
                self._conn.executemany(*args)
                raise sqlite3.OperationalError("disk full")

            def __getattr__(self, name):
                return getattr(self._conn, name)

        with _by_id():
            with mock.patch.object(messages.sqlite3, "connect", side_effect=lambda *a, **k: FailingConn(real_connect(*a, **k))):
                try:
                    messages._ingestion_order_for_envelope(base, {"id": "c"})
                except messages.MessageError as exc:
                    assert "legacy ingestion identity import failed" in str(exc)
                else:
                    raise AssertionError("failed import treated as success")
        assert (base / messages.INGESTION_IDENTITY_FILE).exists()
        with sqlite3.connect(base / messages.INGESTION_IDENTITY_DB) as conn:
            assert conn.execute("SELECT count(*) FROM identities").fetchone() == (0,)


def test_interim_shard_stores_import_with_earliest_order_winning():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        digest = lambda identity: hashlib.sha256(identity.encode("utf-8")).hexdigest()
        (base / (messages.INGESTION_IDENTITY_FILE + ".migrated")).write_text(json.dumps({
            "schema": messages.INGESTION_IDENTITY_SCHEMA, "schema_version": 1, "orders": {"hist": 3},
        }))
        shard_dir = base / ".ingestion-identities.d"
        for identity, order in (("hist", 3), ("window", 9)):
            entry = shard_dir / digest(identity)[:2] / digest(identity)
            entry.parent.mkdir(parents=True, exist_ok=True)
            entry.write_text(f"{order}\n")
        _legacy_map(base, {"hist": 40, "window": 41, "after": 42})
        (base / messages.INGESTION_ORDER_FILE).write_text("42\n")
        with _by_id():
            assert messages._ingestion_order_for_envelope(base, {"id": "hist"}) == 3
            assert messages._ingestion_order_for_envelope(base, {"id": "window"}) == 9
            assert messages._ingestion_order_for_envelope(base, {"id": "after"}) == 42
            assert messages._ingestion_order_for_envelope(base, {"id": "fresh"}) > 42
        assert not shard_dir.exists()
        assert not (base / (messages.INGESTION_IDENTITY_FILE + ".migrated")).exists()
        assert len(list(base.glob("*.imported-*"))) == 3


def test_upgraded_install_imports_a_large_legacy_map_on_first_ingestion():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        legacy_orders = {f"identity-{i}": i + 1 for i in range(5000)}
        _legacy_map(base, legacy_orders)
        (base / messages.INGESTION_ORDER_FILE).write_text("5000\n")
        with _by_id():
            assert messages._ingestion_order_for_envelope(base, {"id": "identity-4321"}) == 4322
            assert messages._ingestion_order_for_envelope(base, {"id": "fresh"}) > 5000
        assert not (base / messages.INGESTION_IDENTITY_FILE).exists()
        with sqlite3.connect(base / messages.INGESTION_IDENTITY_DB) as conn:
            assert conn.execute("SELECT count(*) FROM identities").fetchone() == (5001,)


_RACING_UPGRADE = """
import sys
from pathlib import Path
from unittest import mock
sys.path.insert(0, sys.argv[2])
import goalflight_messages as messages
with mock.patch.object(messages, "_canonical_envelope_identity", side_effect=lambda e: e["id"]):
    print(messages._ingestion_order_for_envelope(Path(sys.argv[1]), {"id": sys.argv[3]}))
"""


def test_processes_racing_the_upgrade_import_agree_on_every_order():
    scripts = str(Path(messages.__file__).resolve().parent)
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        _legacy_map(base, {f"identity-{i}": i + 1 for i in range(3000)})
        (base / messages.INGESTION_ORDER_FILE).write_text("3000\n")
        wanted = ["identity-7", "identity-2999", "new-a", "new-a", "new-b", "identity-7"]
        procs = [
            subprocess.Popen([sys.executable, "-c", _RACING_UPGRADE, tmp, scripts, identity], stdout=subprocess.PIPE, text=True)
            for identity in wanted
        ]
        results = [int(proc.communicate(timeout=60)[0]) for proc in procs]
        assert all(proc.returncode == 0 for proc in procs)
        got = dict(zip(wanted, results))
        assert [got[i] for i in wanted] == results
        assert got["identity-7"] == 8 and got["identity-2999"] == 3000
        assert got["new-a"] > 3000 and got["new-b"] > 3000 and got["new-a"] != got["new-b"]
        assert len(list(base.glob(messages.INGESTION_IDENTITY_FILE + ".imported-*"))) == 1


def test_import_failing_on_a_later_source_rolls_back_every_source():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        interim = base / (messages.INGESTION_IDENTITY_FILE + ".migrated")
        interim.write_text(json.dumps({
            "schema": messages.INGESTION_IDENTITY_SCHEMA, "schema_version": 1, "orders": {"hist": 3},
        }))
        _legacy_map(base, {"live": 4})
        real_connect = sqlite3.connect
        calls = {"n": 0}

        class FailSecondSource:
            def __init__(self, conn):
                self._conn = conn

            def executemany(self, *args):
                calls["n"] += 1
                if calls["n"] == 2:
                    raise sqlite3.OperationalError("disk I/O error")
                return self._conn.executemany(*args)

            def __getattr__(self, name):
                return getattr(self._conn, name)

        with _by_id():
            with mock.patch.object(messages.sqlite3, "connect", side_effect=lambda *a, **k: FailSecondSource(real_connect(*a, **k))):
                try:
                    messages._ingestion_order_for_envelope(base, {"id": "c"})
                except messages.MessageError as exc:
                    assert "disk I/O error" in str(exc)
                else:
                    raise AssertionError("failed import treated as success")
        assert calls["n"] == 2
        assert interim.exists() and (base / messages.INGESTION_IDENTITY_FILE).exists()
        with sqlite3.connect(base / messages.INGESTION_IDENTITY_DB) as conn:
            assert conn.execute("SELECT count(*) FROM identities").fetchone() == (0,)


def test_zero_byte_ingestion_db_fails_closed_instead_of_reading_empty():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        (base / messages.INGESTION_IDENTITY_DB).write_bytes(b"")
        with _by_id():
            try:
                messages._ingestion_order_for_envelope(base, {"id": "a"})
            except messages.MessageError as exc:
                assert "no identities table" in str(exc)
            else:
                raise AssertionError("empty store treated as an empty map")
        assert not (base / messages.INGESTION_ORDER_FILE).exists()


def test_legacy_map_recreated_by_old_code_never_changes_a_stored_order():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        with _by_id():
            kept = messages._ingestion_order_for_envelope(base, {"id": "a"})
            _legacy_map(base, {"a": kept + 100, "z": 5})
            messages._ingestion_order_for_envelope(base, {"id": "b"})
            assert messages._ingestion_order_for_envelope(base, {"id": "a"}) == kept
            assert messages._ingestion_order_for_envelope(base, {"id": "z"}) == 5
        assert not (base / messages.INGESTION_IDENTITY_FILE).exists()


def test_corrupt_ingestion_db_fails_closed():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        db = base / messages.INGESTION_IDENTITY_DB
        db.write_bytes(b"not a sqlite database" * 100)
        with _by_id():
            try:
                messages._ingestion_order_for_envelope(base, {"id": "a"})
            except messages.MessageError as exc:
                assert str(db) in str(exc)
            else:
                raise AssertionError("corrupt store treated as an empty map")
        assert not (base / messages.INGESTION_ORDER_FILE).exists()


def test_fork_cannot_reuse_parent_lock_ownership():
    if not hasattr(os, "fork"):
        return
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "worker.jsonl"
        with messages.mail_lock(path):
            pid = os.fork()
            if pid == 0:
                try:
                    with messages.mail_lock(path, timeout_secs=0.05):
                        os._exit(1)
                except TimeoutError:
                    os._exit(0)
                except BaseException:
                    os._exit(2)
            _, status = os.waitpid(pid, 0)
            assert os.waitstatus_to_exitcode(status) == 0


def test_failed_acquisition_does_not_unlock_or_mask_timeout():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "worker.jsonl"

        def unavailable(_fh, operation):
            assert operation != messages.goalflight_compat.LOCK_UN, "unowned unlock"
            raise BlockingIOError(errno.EAGAIN, "contended")

        with mock.patch.object(messages.goalflight_compat, "flock", side_effect=unavailable):
            try:
                with messages.mail_lock(path, timeout_secs=0):
                    raise AssertionError("entered without a lock")
            except TimeoutError:
                pass
        with messages.mail_lock(path, timeout_secs=0):
            assert probe_lock(path) == 3, "failed acquire left stale ownership"


def test_terminating_signal_posts_once_and_unwinds_outer_transaction():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        original_select = messages._select_ingestion_order
        original_lock = messages.mail_lock
        interrupted = False

        def post(text):
            return messages.post_message(
                dispatch_id="worker-signal", msg_type="status", payload={"text": text},
                messages_dir=base, project_journal_delivery=False,
            )

        def handler(_signum, _frame):
            post("inner")
            # Match watcher.handle_signal: the interrupted writer never resumes.
            raise SystemExit(128 + signal.SIGUSR1)

        def interrupt_load(conn, db_path, identity_hash):
            nonlocal interrupted
            if not interrupted:
                interrupted = True
                signal.raise_signal(signal.SIGUSR1)
            return original_select(conn, db_path, identity_hash)

        def bounded_lock(path, **kwargs):
            return original_lock(path, timeout_secs=0.05)

        previous = signal.signal(signal.SIGUSR1, handler)
        try:
            with mock.patch.object(messages, "_select_ingestion_order", side_effect=interrupt_load):
                with mock.patch.object(messages, "mail_lock", side_effect=bounded_lock):
                    try:
                        post("outer")
                    except SystemExit as exc:
                        assert exc.code == 128 + signal.SIGUSR1
                    else:
                        raise AssertionError("outer writer resumed after signal")
        finally:
            signal.signal(signal.SIGUSR1, previous)
        import json
        rows = [json.loads(line) for line in (base / "worker-signal.jsonl").read_text().splitlines()]
        assert [(row["seq"], row["payload"]["text"]) for row in rows] == [(1, "inner")]
        assert probe_lock(base / "worker-signal.jsonl") == 0
        assert probe_lock(base / messages.INGESTION_IDENTITY_FILE) == 0


def test_reentry_at_flock_acquire_and_release_boundaries():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "worker.jsonl"
        real_flock = messages.goalflight_compat.flock
        reentering = False

        def interrupted_flock(fh, operation):
            nonlocal reentering
            real_flock(fh, operation)
            if not reentering:
                reentering = True
                try:
                    with messages.mail_lock(path, timeout_secs=0.05):
                        assert probe_lock(path) == 3
                finally:
                    reentering = False

        with mock.patch.object(messages.goalflight_compat, "flock", side_effect=interrupted_flock):
            with messages.mail_lock(path, timeout_secs=0.05):
                assert probe_lock(path) == 3
        assert probe_lock(path) == 0


def test_signal_exception_at_cache_deletion_cannot_leave_false_ownership():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "worker.jsonl"
        source, start = inspect.getsourcelines(messages.mail_lock.__wrapped__)
        deletion = next(start + i for i, line in enumerate(source)
                        if "del _HELD_MAIL_LOCKS[owner]" in line)
        interrupted = False

        def handler(_signum, _frame):
            with messages.mail_lock(path, timeout_secs=0.05):
                assert probe_lock(path) == 3
                raise SystemExit(73)

        def trace(frame, event, _arg):
            nonlocal interrupted
            if (not interrupted and event == "line"
                    and frame.f_code is messages.mail_lock.__wrapped__.__code__
                    and frame.f_lineno == deletion):
                interrupted = True
                signal.raise_signal(signal.SIGUSR1)
            return trace

        previous_handler = signal.signal(signal.SIGUSR1, handler)
        previous_trace = sys.gettrace()
        try:
            sys.settrace(trace)
            try:
                with messages.mail_lock(path, timeout_secs=0.05):
                    pass
            except SystemExit as exc:
                assert exc.code == 73
            else:
                raise AssertionError("deletion was not interrupted")
        finally:
            sys.settrace(previous_trace)
            signal.signal(signal.SIGUSR1, previous_handler)
        assert interrupted
        assert probe_lock(path) == 0
        with lock_holder(path):
            try:
                with messages.mail_lock(path, timeout_secs=0.05):
                    raise AssertionError("stale ownership bypassed external holder")
            except TimeoutError:
                pass
        with messages.mail_lock(path, timeout_secs=0.05):
            assert probe_lock(path) == 3


def test_fork_normal_context_exit_preserves_parent_exclusion():
    if not hasattr(os, "fork"):
        return
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "worker.jsonl"
        parent = os.getpid()
        try:
            with messages.mail_lock(path):
                pid = os.fork()
                if pid:
                    _, status = os.waitpid(pid, 0)
                    assert os.waitstatus_to_exitcode(status) == 0
                    assert probe_lock(path) == 3, "child cleanup unlocked parent"
                    with messages.mail_lock(path, timeout_secs=0.05):
                        assert probe_lock(path) == 3
            if os.getpid() != parent:
                os._exit(0)  # AFTER normal inherited-context cleanup.
        finally:
            if os.getpid() != parent:
                os._exit(1)
        assert probe_lock(path) == 0


def test_other_thread_contends_then_acquires_after_outer_exit():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "worker.jsonl"
        outcomes = []

        def acquire():
            try:
                with messages.mail_lock(path, timeout_secs=0.05):
                    outcomes.append(("entered", probe_lock(path)))
            except TimeoutError:
                outcomes.append("timeout")

        with messages.mail_lock(path):
            thread = threading.Thread(target=acquire, daemon=True)
            thread.start()
            thread.join(timeout=5)
            assert not thread.is_alive()
            assert outcomes == ["timeout"]
            assert probe_lock(path) == 3
        thread = threading.Thread(target=acquire, daemon=True)
        thread.start()
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert outcomes == ["timeout", ("entered", 3)]
        assert probe_lock(path) == 0


def test_externally_blocked_acquire_interrupted_by_terminating_handler():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "worker.jsonl"
        real_flock = messages.goalflight_compat.flock
        interrupted = False
        inner_timed_out = False

        def handler(_signum, _frame):
            nonlocal inner_timed_out
            try:
                with messages.mail_lock(path, timeout_secs=0.05):
                    raise AssertionError("handler bypassed external holder")
            except TimeoutError:
                inner_timed_out = True
            raise SystemExit(74)

        def interrupt_blocked(fh, operation):
            nonlocal interrupted
            try:
                return real_flock(fh, operation)
            except BlockingIOError:
                if not interrupted:
                    interrupted = True
                    signal.raise_signal(signal.SIGUSR1)
                raise

        previous = signal.signal(signal.SIGUSR1, handler)
        try:
            with lock_holder(path):
                with mock.patch.object(messages.goalflight_compat, "flock", side_effect=interrupt_blocked):
                    try:
                        with messages.mail_lock(path, timeout_secs=0.05):
                            raise AssertionError("outer bypassed external holder")
                    except SystemExit as exc:
                        assert exc.code == 74
                assert probe_lock(path) == 3
        finally:
            signal.signal(signal.SIGUSR1, previous)
        assert interrupted and inner_timed_out
        with messages.mail_lock(path, timeout_secs=0.05):
            assert probe_lock(path) == 3


def test_trace_attention_retries_after_real_ingestion_timeout():
    import goalflight_watch as watch

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        posted = set()
        attempts = []
        timeouts = []

        def post(**kwargs):
            attempts.append(kwargs)
            try:
                return messages.post_message(
                    messages_dir=base, project_journal_delivery=False, **kwargs,
                )
            except TimeoutError as exc:
                timeouts.append(str(exc))
                raise

        identity_path = base / messages.INGESTION_IDENTITY_FILE
        stream = base / "worker-attention.jsonl"
        with lock_holder(identity_path):
            watch.post_trace_attention("worker-attention", "long_running", posted, post_func=post)
        assert len(timeouts) == 1
        assert str(messages.mail_lock_path(identity_path)) in timeouts[0]
        assert not stream.exists()
        assert not posted, "timeout made attention permanently ineligible for retry"
        watch.post_trace_attention("worker-attention", "long_running", posted, post_func=post)
        watch.post_trace_attention("worker-attention", "long_running", posted, post_func=post)
        assert len(attempts) == 2
        assert posted == {"long_running"}
        rows = [json.loads(line) for line in stream.read_text().splitlines()]
        assert len(rows) == 1 and rows[0]["type"] == "monitor"
