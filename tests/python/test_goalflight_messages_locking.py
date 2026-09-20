"""Hermetic regressions for nested carrier and ingestion locks."""

import contextlib
import errno
import inspect
import json
import os
from pathlib import Path
import subprocess
import signal
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
        original_load = messages._load_ingestion_identity_orders

        def load_with_stream_reentry(path):
            with messages.carrier_transaction(stream, lock_timeout_secs=0.05):
                return original_load(path)

        with messages.carrier_transaction(stream, lock_timeout_secs=0.05):
            with mock.patch.object(messages, "_canonical_envelope_identity", return_value="event"):
                with mock.patch.object(messages, "_load_ingestion_identity_orders", side_effect=load_with_stream_reentry):
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
        original_load = messages._load_ingestion_identity_orders
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

        def interrupt_load(path):
            nonlocal interrupted
            if not interrupted:
                interrupted = True
                signal.raise_signal(signal.SIGUSR1)
            return original_load(path)

        def bounded_lock(path, **kwargs):
            return original_lock(path, timeout_secs=0.05)

        previous = signal.signal(signal.SIGUSR1, handler)
        try:
            with mock.patch.object(messages, "_load_ingestion_identity_orders", side_effect=interrupt_load):
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
