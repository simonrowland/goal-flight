#!/usr/bin/env python3
"""Compare poll, gated, and WAL-notify delivery with synthetic controllers."""

from __future__ import annotations

import argparse
from contextlib import ExitStack
import importlib
import json
import os
from pathlib import Path
import resource
import select
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT / "tests" / "python"))

from machine_isolation import isolated_machine_env  # noqa: E402
from support import AMBIENT_IDENTITY_ENV  # noqa: E402

goalflight_journal = None
goalflight_messages = None
goalflight_wake = None


def _environment(root: Path) -> dict[str, str]:
    root.mkdir(parents=True, exist_ok=True)
    isolated = isolated_machine_env(root)
    ambient_free = {
        key: value for key, value in os.environ.items()
        if key not in AMBIENT_IDENTITY_ENV
    }
    environment = {
        **ambient_free,
        **isolated,
        "GOALFLIGHT_WAKE_LEDGER": isolated["GOALFLIGHT_WAKE_LEDGER_DIR"],
        "GOALFLIGHT_TASK_STORE": isolated["GOALFLIGHT_TASK_STORE_DIR"],
        "GOALFLIGHT_DISPATCH_DIR": str(root / "dispatch"),
        "GOAL_FLIGHT_PIDFILE_DIR": isolated["GOAL_FLIGHT_PIDFILE_DIR"],
        "GOALFLIGHT_PIDFILE_DIR": isolated["GOAL_FLIGHT_PIDFILE_DIR"],
        "GOALFLIGHT_ROOT": str(ROOT),
        "GOALFLIGHT_PROCESS_ROLE": "controller",
        "GOALFLIGHT_TEST_MODE": "1",
        "GOALFLIGHT_CAPACITY_CONF": "/dev/null",
    }
    try:
        process_rows = subprocess.check_output(
            ["ps", "-axww", "-o", "pid=,command="],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=2,
        ).splitlines()
    except (OSError, subprocess.SubprocessError, UnicodeError):
        process_rows = []
    if not any(
        row.split(None, 1)[0] == str(os.getpid())
        for row in process_rows
        if row.strip()
    ):
        # Some sandboxed test hosts hide the process table; let a smoke run use
        # the recorded supervisor slots without altering normal host behavior.
        listing_dir = root / "process-listing"
        listing_dir.mkdir(parents=True, exist_ok=True)
        ps_shim = listing_dir / "ps"
        ps_shim.write_text("#!/bin/sh\nprintf '%s\\n' '1 init'\n", encoding="utf-8")
        ps_shim.chmod(0o755)
        environment["PATH"] = f"{listing_dir}:{os.environ.get('PATH', '')}"
    return environment


def _percentile(samples: list[float], fraction: float) -> float:
    ordered = sorted(samples)
    index = min(len(ordered) - 1, round((len(ordered) - 1) * fraction))
    return ordered[index]


def _run_mode(
    mode: str,
    *,
    root: Path,
    controllers: int,
    events_per_controller: int,
    poll_s: float,
) -> dict[str, object]:
    env = _environment(root / "machine")
    os.environ.update(env)
    os.environ["GOALFLIGHT_WAKE_MODE"] = mode
    project = root / mode / "project"
    project.mkdir(parents=True)
    authority = goalflight_journal.open_or_create_journal(project)
    labels = [f"wake-soak-{mode}-{index}" for index in range(controllers)]
    authorities = {label: authority for label in labels}
    expected: dict[str, str] = {}
    event_specs: list[tuple[str, str]] = []
    for label in labels:
        for index in range(events_per_controller):
            event_id = f"soak-{mode}-{label}-{index}"
            expected[event_id] = label
            event_specs.append((event_id, label))
    event_budget_s = max(60.0, len(event_specs) * max(poll_s, 0.1) * 2.0)
    startup_budget_s = max(30.0, min(event_budget_s, 30.0 + len(labels)))
    phase_budget_s = max(45.0, event_budget_s / 2.0)
    restarted_label = labels[0]
    pending_restart_id = f"soak-{mode}-{restarted_label}-pending-restart"
    expected[pending_restart_id] = restarted_label

    holders = ExitStack()
    leases: dict[str, object] = {}
    for label in labels:
        claimed = authority.claim_or_renew_lease(
            label,
            principal={"principal_id": f"{label}-principal"},
        )
        if not claimed.committed or claimed.value is None:
            raise RuntimeError(f"could not claim synthetic lease for {label}")
        lease = claimed.value
        leases[label] = lease
        holders.enter_context(
            goalflight_wake.register_lease_holder(
                project,
                controller_label=label,
                lease_nonce=lease.nonce,
            )
        )

    poller = select.poll()
    by_fd: dict[int, dict[str, object]] = {}
    children: dict[str, dict[str, object]] = {}
    generations = {label: 0 for label in labels}
    supervisor_starts = {label: 0 for label in labels}
    cpu_records: list[dict[str, object]] = []
    ready: set[str] = set()
    supervised: set[str] = set()
    seen: dict[str, float] = {}
    duplicate_ids: set[str] = set()
    unexpected_ids: set[str] = set()
    degraded_labels: set[str] = set()
    recovered_labels: set[str] = set()
    commit_times: dict[str, float] = {}
    wake_times: dict[str, float] = {}
    latencies: dict[str, float] = {}
    projection_times: dict[str, float] = {}
    projection_context = threading.local()
    timing_lock = threading.Lock()
    writer_errors: list[BaseException] = []
    writer_cancel = threading.Event()
    writers: list[threading.Thread] = []
    timeout_error: str | None = None
    run_failure: dict[str, str] | None = None
    injection_stage = "startup"
    wal_rotation: dict[str, object] | None = None
    wal_replacement_id: str | None = None
    busy_label: str | None = None
    busy_journal_mode: str | None = None
    busy_lock_s: float | None = None
    missed_hint_id: str | None = None
    missed_hint_reached_safety_poll: float | None = None
    supervisor_cpu_path = root / f"{mode}-supervisor-children.jsonl"
    supervisor_wrapper = root / f"{mode}-supervisor-wrapper.py"
    supervisor_wrapper.write_text(
        "import json, os, resource, subprocess, sys\n"
        f"sys.path.insert(0, {str(SCRIPTS)!r})\n"
        f"cpu_path = {str(supervisor_cpu_path)!r}\n"
        "supervisor_before = resource.getrusage(resource.RUSAGE_SELF)\n"
        "recorded = set()\n"
        "def record_cpu(proc, before, after):\n"
        "    pid = getattr(proc, 'pid', None)\n"
        "    if pid is None or pid in recorded:\n"
        "        return\n"
        "    command = getattr(proc, 'args', '')\n"
        "    if isinstance(command, (list, tuple)):\n"
        "        command = ' '.join(str(part) for part in command)\n"
        "    command = str(command)\n"
        "    if 'goalflight_messages.py' not in command:\n"
        "        return\n"
        "    recorded.add(pid)\n"
        "    usage = {\n"
        "        'pid': pid,\n"
        "        'process': 'follow' if ' follow ' in (' ' + command + ' ') else 'listen',\n"
        "        'cpu_seconds': max(0.0, after.ru_utime - before.ru_utime) + max(0.0, after.ru_stime - before.ru_stime),\n"
        "    }\n"
        "    with open(cpu_path, 'a', encoding='utf-8') as stream:\n"
        "        stream.write(json.dumps(usage) + '\\n')\n"
        "original_poll = subprocess.Popen.poll\n"
        "original_wait = subprocess.Popen.wait\n"
        "def measured_poll(self, *args, **kwargs):\n"
        "    before = resource.getrusage(resource.RUSAGE_CHILDREN)\n"
        "    result = original_poll(self, *args, **kwargs)\n"
        "    after = resource.getrusage(resource.RUSAGE_CHILDREN)\n"
        "    if result is not None:\n"
        "        record_cpu(self, before, after)\n"
        "    return result\n"
        "def measured_wait(self, *args, **kwargs):\n"
        "    before = resource.getrusage(resource.RUSAGE_CHILDREN)\n"
        "    result = original_wait(self, *args, **kwargs)\n"
        "    after = resource.getrusage(resource.RUSAGE_CHILDREN)\n"
        "    record_cpu(self, before, after)\n"
        "    return result\n"
        "subprocess.Popen.poll = measured_poll\n"
        "subprocess.Popen.wait = measured_wait\n"
        "script = sys.argv[1]\n"
        "sys.argv = [script, *sys.argv[2:]]\n"
        "try:\n"
        "    import goalflight_messages\n"
        "    raise SystemExit(goalflight_messages.main())\n"
        "finally:\n"
        "    supervisor_after = resource.getrusage(resource.RUSAGE_SELF)\n"
        "    usage = {\n"
        "        'pid': os.getpid(),\n"
        "        'process': 'supervisor',\n"
        "        'cpu_seconds': max(0.0, supervisor_after.ru_utime - supervisor_before.ru_utime) + max(0.0, supervisor_after.ru_stime - supervisor_before.ru_stime),\n"
        "    }\n"
        "    with open(cpu_path, 'a', encoding='utf-8') as stream:\n"
        "        stream.write(json.dumps(usage) + '\\n')\n",
        encoding="utf-8",
    )

    def start_child(
        label: str,
        *,
        project_root: Path = project,
        missed_hint_wrapper: Path | None = None,
        with_supervisor: bool = False,
    ) -> None:
        lease = leases[label]
        if with_supervisor:
            supervisor_starts[label] += 1
            supervisor_cpu_path.unlink(missing_ok=True)
            command = [
                sys.executable,
                str(supervisor_wrapper),
                str(SCRIPTS / "goalflight_messages.py"),
                "supervise",
                "--project-root",
                str(project_root),
                "--controller-label",
                label,
                "--lease-nonce",
                lease.nonce,
                "--heartbeat-secs",
                "0.1",
                "--coverage-secs",
                "0.1",
                "--chatty",
            ]
            supervised.add(label)
        else:
            command = [
                sys.executable,
                str(SCRIPTS / "goalflight_messages.py"),
                "follow",
                "--project-root",
                str(project_root),
                "--controller-label",
                label,
                "--lease-nonce",
                lease.nonce,
                "--poll-secs",
                str(poll_s),
                "--heartbeat-secs",
                "60",
                "--frontier-floor-secs",
                "1200",
            ]
            supervised.discard(label)
        if missed_hint_wrapper is not None:
            command = [sys.executable, str(missed_hint_wrapper), *command[1:]]
        child_env = dict(env)
        child_env["GOALFLIGHT_WAKE_MODE"] = mode
        proc = subprocess.Popen(
            command,
            cwd=project_root,
            env=child_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        assert proc.stdout is not None and proc.stderr is not None
        fd = proc.stdout.fileno()
        os.set_blocking(fd, False)
        os.set_blocking(proc.stderr.fileno(), False)
        generations[label] = generations.get(label, 0) + 1
        child: dict[str, object] = {
            "label": label,
            "generation": generations[label],
            "proc": proc,
            "fd": fd,
            "buffer": bytearray(),
            "stderr": proc.stderr,
            "process": "supervisor" if with_supervisor else "follow",
        }
        children[label] = child
        by_fd[fd] = child
        poller.register(fd, select.POLLIN | select.POLLHUP | select.POLLERR)

    def stop_child(label: str) -> None:
        child = children.pop(label)
        fd = int(child["fd"])
        by_fd.pop(fd, None)
        try:
            poller.unregister(fd)
        except KeyError:
            pass
        proc = child["proc"]
        before = resource.getrusage(resource.RUSAGE_CHILDREN)
        try:
            proc.terminate()
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=max(15.0, poll_s * 20.0))
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=max(15.0, poll_s * 20.0))
        after = resource.getrusage(resource.RUSAGE_CHILDREN)
        cpu = (after.ru_utime - before.ru_utime) + (after.ru_stime - before.ru_stime)
        if child["process"] != "supervisor":
            cpu_records.append(
                {
                    "controller": label,
                    "generation": child["generation"],
                    "pid": proc.pid,
                    "process": child["process"],
                    "cpu_seconds": round(cpu, 6),
                }
            )
        if child["process"] == "supervisor" and supervisor_cpu_path.exists():
            for line in supervisor_cpu_path.read_text(encoding="utf-8").splitlines():
                measured = json.loads(line)
                cpu_records.append(
                    {
                        "controller": label,
                        "generation": child["generation"],
                        **measured,
                    }
                )
            supervisor_cpu_path.unlink(missing_ok=True)
        proc.stdout.close()
        proc.stderr.close()

    def acknowledge(label: str, payload: dict[str, object]) -> None:
        stream = str(payload["stream_id"])
        sequence = int(payload["stream_seq"])
        for _attempt in range(10):
            snapshot = authorities[label].cursor_peek(
                label,
                nonce=leases[label].nonce,
            )
            token = snapshot.stream_snapshots.get(stream)
            if token is None:
                status = authorities[label].cursor_status(label)
                if int(status.positions.get(stream, 0)) >= sequence:
                    return
                raise RuntimeError(f"no cursor snapshot for delivered stream {stream}")
            try:
                result = authorities[label].advance_cursor(
                    label,
                    nonce=leases[label].nonce,
                    expected_cursor_version=snapshot.cursor_version,
                    expected_stream_snapshots={stream: token},
                    advances={stream: sequence},
                    actor="listener-wake-soak",
                )
            except goalflight_journal.CASMismatch:
                continue
            if result.committed:
                return
        raise RuntimeError(f"could not acknowledge delivered event in {stream}")

    def receive(label: str, record: dict[str, object]) -> None:
        if record.get("kind") == "heartbeat":
            if label not in supervised:
                ready.add(label)
            return
        if record.get("kind") == "supervise":
            if (
                record.get("type") == "coverage"
                and record.get("live") == record.get("target")
            ):
                ready.add(label)
            return
        if record.get("kind") != "event":
            if record.get("kind") == "fault":
                raise RuntimeError(f"{mode} follow fault for {label}: {record}")
            return
        payload = record.get("payload")
        if not isinstance(payload, dict):
            raise RuntimeError(f"malformed follow event from {label}")
        event_type = str(payload.get("type") or "")
        if event_type == "listener-degraded":
            degraded_labels.add(label)
            return
        if event_type == "listener-recovered":
            recovered_labels.add(label)
            return
        if event_type == "listener-exit":
            return
        if event_type == "listener-fault" and payload.get("reason") == "carrier-corrupt":
            # Projection can wake follow before post-commit carrier work ends;
            # follow retries this recoverable condition on its normal cadence.
            return
        event_id = str(payload.get("dispatch_id") or "")
        if event_id not in expected or expected[event_id] != label:
            unexpected_ids.add(event_id or f"missing-id:{label}")
            return
        observed_at = time.monotonic()
        acknowledge_payload: dict[str, object] | None = None
        with timing_lock:
            if event_id in seen:
                duplicate_ids.add(event_id)
                return
            else:
                seen[event_id] = observed_at
                wake_times[event_id] = observed_at
                if event_id in commit_times:
                    latencies[event_id] = max(0.0, observed_at - commit_times[event_id])
                acknowledge_payload = payload
        if acknowledge_payload is not None:
            acknowledge(label, acknowledge_payload)

    def receive_headline(label: str, line: str) -> None:
        if not line.startswith("[") or "] " not in line:
            return
        kind, _separator, remainder = line.partition("] ")
        event_id = remainder.split(" ", 1)[0]
        if not event_id:
            return
        receive(
            label,
            {
                "kind": "event",
                "payload": {
                    "type": kind[1:],
                    "dispatch_id": event_id,
                    "stream_id": event_id,
                    "stream_seq": 1,
                },
            },
        )

    def pump_once(timeout_s: float) -> None:
        for fd, _flags in poller.poll(max(0, int(timeout_s * 1000))):
            child = by_fd.get(fd)
            if child is None:
                continue
            while True:
                try:
                    data = os.read(fd, 65536)
                except BlockingIOError:
                    break
                if not data:
                    try:
                        poller.unregister(fd)
                    except KeyError:
                        pass
                    by_fd.pop(fd, None)
                    break
                buffer = child["buffer"]
                buffer.extend(data)
                while b"\n" in buffer:
                    raw, remainder = buffer.split(b"\n", 1)
                    buffer = bytearray(remainder)
                    child["buffer"] = buffer
                    try:
                        record = json.loads(raw)
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        receive_headline(
                            str(child["label"]),
                            raw.decode("utf-8", errors="replace"),
                        )
                        continue
                    receive(str(child["label"]), record)

    def wait_ready(targets: set[str]) -> None:
        deadline = time.monotonic() + startup_budget_s
        while not targets.issubset(ready):
            exited = [
                label
                for label in sorted(targets - ready)
                if children[label]["proc"].poll() is not None
            ]
            if exited:
                details = []
                for label in exited:
                    child = children[label]
                    proc = child["proc"]
                    try:
                        error = os.read(proc.stderr.fileno(), 8192).decode(
                            "utf-8", errors="replace"
                        )
                    except (BlockingIOError, OSError):
                        error = ""
                    details.append(
                        f"{label} exit={proc.returncode} stderr={error.strip()!r}"
                    )
                raise TimeoutError("wake process exited before arming: " + "; ".join(details))
            if time.monotonic() >= deadline:
                details = []
                for label in sorted(targets - ready):
                    child = children[label]
                    proc = child["proc"]
                    try:
                        error = os.read(proc.stderr.fileno(), 8192).decode(
                            "utf-8", errors="replace"
                        )
                    except (BlockingIOError, OSError):
                        error = ""
                    details.append(
                        f"{label} exit={proc.poll()} stderr={error.strip()!r}"
                    )
                raise TimeoutError("follow did not start: " + "; ".join(details))
            pump_once(0.1)

    def publish_one(event_id: str, label: str) -> None:
        result = goalflight_messages.post_message(
            dispatch_id=event_id,
            msg_type="controller-notice",
            payload={"text": event_id},
            messages_dir=Path(env["GOALFLIGHT_MESSAGES_DIR"]),
            source={"node": "soak", "adapter": "manual", "transport": "controller"},
            addressee=goalflight_messages.controller_addressee(
                label,
                project_root=project,
            ),
        )
        event_uuid = str((result.get("envelope") or {}).get("id") or "")
        with timing_lock:
            committed_at = projection_times.pop(event_uuid, None)
            if committed_at is None:
                raise RuntimeError(
                    f"projection commit timestamp was not captured for {event_id}"
                )
            commit_times[event_id] = committed_at
            if event_id in wake_times:
                latencies[event_id] = max(0.0, wake_times[event_id] - committed_at)

    def publish_batch(specs: list[tuple[str, str]]) -> threading.Thread:
        writer_cancel.clear()

        def writer() -> None:
            try:
                for event_id, label in specs:
                    if writer_cancel.is_set():
                        break
                    publish_one(event_id, label)
            except BaseException as exc:
                writer_errors.append(exc)

        thread = threading.Thread(target=writer, daemon=True)
        thread.start()
        writers.append(thread)
        return thread

    def drain_until(target_ids: set[str], writer: threading.Thread) -> None:
        deadline = time.monotonic() + event_budget_s
        while not target_ids.issubset(seen):
            if writer_errors:
                raise RuntimeError(f"event writer failed: {writer_errors[0]}")
            if time.monotonic() >= deadline:
                writer_cancel.set()
                raise TimeoutError(
                    f"workload delivery exceeded its {event_budget_s:.1f}s "
                    f"budget; delivered {len(target_ids.intersection(seen))}/"
                    f"{len(target_ids)} event ids"
                )
            pump_once(0.1)
        writer.join(timeout=max(0.0, deadline - time.monotonic()))
        if writer.is_alive():
            writer_cancel.set()
            raise TimeoutError(
                f"event writer did not finish within the {event_budget_s:.1f}s "
                "event-count budget"
            )
        if writer_errors:
            raise RuntimeError(f"event writer failed: {writer_errors[0]}")
        if not target_ids.issubset(seen):
            raise TimeoutError(
                f"follow missed {len(target_ids - set(seen))} event ids "
                f"within the {event_budget_s:.1f}s event-count budget"
            )

    def pump_until(predicate, *, timeout_s: float, description: str) -> None:
        deadline = time.monotonic() + timeout_s
        while not predicate():
            if writer_errors:
                raise RuntimeError(f"event writer failed: {writer_errors[0]}")
            if time.monotonic() >= deadline:
                raise TimeoutError(description)
            pump_once(0.1)

    def rotate_wal() -> dict[str, object]:
        connection = sqlite3.connect(authority.path, timeout=5, isolation_level=None)
        wal = Path(f"{authority.path}-wal")
        before = wal.stat() if wal.exists() else None
        try:
            restart = tuple(connection.execute("PRAGMA wal_checkpoint(RESTART)").fetchone())
            truncate = tuple(connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone())
            if restart[0] or truncate[0]:
                raise RuntimeError(
                    f"WAL rotation checkpoint remained busy: {restart!r}, {truncate!r}"
                )
            journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0])
        finally:
            connection.close()
        if not wal.exists():
            # Per-operation poll readers close between passes, so SQLite may
            # remove the empty sidecar when the checkpoint connection closes.
            wal.touch()
        after = wal.stat()
        if after.st_size != 0:
            raise RuntimeError("WAL truncate did not leave an empty live WAL")
        replacement = wal.with_name(f"{wal.name}.replacement")
        replacement.write_bytes(b"")
        os.replace(replacement, wal)
        replaced = wal.stat()
        if replaced.st_ino == after.st_ino:
            raise RuntimeError("WAL replacement did not change its inode")
        return {
            "journal_mode": journal_mode,
            "checkpoint_restart": restart,
            "checkpoint_truncate": truncate,
            "wal_before": (
                {
                    "inode": int(before.st_ino),
                    "size": int(before.st_size),
                    "mtime_ns": int(before.st_mtime_ns),
                }
                if before is not None
                else None
            ),
            "wal_after_truncate": {
                "inode": int(after.st_ino),
                "size": int(after.st_size),
                "mtime_ns": int(after.st_mtime_ns),
            },
            "wal_after_replacement": {
                "inode": int(replaced.st_ino),
                "size": int(replaced.st_size),
                "mtime_ns": int(replaced.st_mtime_ns),
            },
            "live_listener_processes": len(children),
        }

    original_connect = goalflight_journal.Journal._connect
    original_domain_write = goalflight_journal.Journal._domain_write
    original_mark_delivery_projected = goalflight_journal.Journal.mark_delivery_projected

    def trace_projection_commit(authority, *args, **kwargs):
        connection = original_connect(authority, *args, **kwargs)
        if getattr(projection_context, "event_uuid", None):
            def capture(statement: str) -> None:
                if statement.strip().upper() == "COMMIT":
                    projection_context.commit_at = time.monotonic()

            connection.set_trace_callback(capture)
        return connection

    def record_domain_write(authority, action):
        result = original_domain_write(authority, action)
        event_uuid = getattr(projection_context, "event_uuid", None)
        commit_at = getattr(projection_context, "commit_at", None)
        if (
            event_uuid
            and result.committed
            and commit_at is not None
            and not getattr(projection_context, "stored", False)
        ):
            # The SQLite trace fires at COMMIT, before this method returns and
            # before mark_delivery_projected flushes post-commit webhooks.
            projection_times[event_uuid] = commit_at
            projection_context.stored = True
        return result

    def record_projection_commit(
        authority,
        *,
        recipient_label: str,
        origin_node: str,
        event_uuid: str,
    ):
        projection_context.event_uuid = event_uuid
        projection_context.commit_at = None
        projection_context.stored = False
        try:
            return original_mark_delivery_projected(
                authority,
                recipient_label=recipient_label,
                origin_node=origin_node,
                event_uuid=event_uuid,
            )
        finally:
            del projection_context.event_uuid
            del projection_context.commit_at
            del projection_context.stored

    goalflight_journal.Journal._connect = trace_projection_commit
    goalflight_journal.Journal._domain_write = record_domain_write
    goalflight_journal.Journal.mark_delivery_projected = record_projection_commit
    try:
        for label in labels:
            start_child(label)
        wait_ready(set(labels))

        injection_stage = "supervisor-restart"
        stop_child(restarted_label)
        ready.discard(restarted_label)
        start_child(restarted_label, with_supervisor=True)
        wait_ready({restarted_label})
        stop_child(restarted_label)
        ready.discard(restarted_label)
        publish_one(pending_restart_id, restarted_label)
        start_child(restarted_label, with_supervisor=True)
        wait_ready({restarted_label})
        pump_until(
            lambda: pending_restart_id in seen,
            timeout_s=phase_budget_s,
            description="restarted supervisor did not deliver its pending event",
        )
        stop_child(restarted_label)
        ready.discard(restarted_label)
        start_child(restarted_label)
        wait_ready({restarted_label})

        injection_stage = "journal-busy"
        busy_project = root / f"{mode}-busy" / "project"
        busy_project.mkdir(parents=True)
        busy_authority = goalflight_journal.open_or_create_journal(busy_project)
        busy_label = f"wake-soak-{mode}-journal-busy"
        busy_claim = busy_authority.claim_or_renew_lease(
            busy_label,
            principal={"principal_id": f"{busy_label}-principal"},
        )
        if not busy_claim.committed or busy_claim.value is None:
            raise RuntimeError("could not claim journal-busy recovery lease")
        leases[busy_label] = busy_claim.value
        authorities[busy_label] = busy_authority
        holders.enter_context(
            goalflight_wake.register_lease_holder(
                busy_project,
                controller_label=busy_label,
                lease_nonce=busy_claim.value.nonce,
            )
        )
        setup_connection = sqlite3.connect(
            busy_authority.path,
            timeout=5,
            isolation_level=None,
        )
        try:
            busy_journal_mode = str(
                setup_connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
            )
        finally:
            setup_connection.close()
        if busy_journal_mode.lower() != "delete":
            raise RuntimeError(f"could not prepare rollback journal: {busy_journal_mode}")
        start_child(busy_label, project_root=busy_project)
        wait_ready({busy_label})
        busy_connection = sqlite3.connect(
            busy_authority.path,
            timeout=5,
            isolation_level=None,
        )
        busy_connection.execute("BEGIN EXCLUSIVE")
        busy_started = time.monotonic()
        try:
            pump_until(
                lambda: busy_label in degraded_labels,
                timeout_s=max(45.0, phase_budget_s),
                description="listener did not report degraded during exclusive lock",
            )
        finally:
            busy_connection.rollback()
            busy_connection.close()
        busy_lock_s = time.monotonic() - busy_started
        pump_until(
            lambda: busy_label in recovered_labels,
            timeout_s=max(45.0, phase_budget_s),
            description="listener did not report recovery after exclusive lock",
        )
        stop_child(busy_label)

        injection_stage = "event-workload"
        workload_writer = publish_batch(event_specs)
        drain_until({event_id for event_id, _label in event_specs}, workload_writer)

        injection_stage = "wal-rotation"
        wal_rotation = rotate_wal()
        if mode in {"gated", "notify"}:
            injection_stage = "wal-replacement-delivery"
            wal_replacement_id = f"soak-{mode}-{labels[0]}-wal-replacement"
            expected[wal_replacement_id] = labels[0]
            publish_one(wal_replacement_id, labels[0])
            pump_until(
                lambda: wal_replacement_id in seen,
                timeout_s=phase_budget_s,
                description="listeners did not recover after live WAL inode replacement",
            )

        missed_hint_id = None
        missed_hint_reached_safety_poll = None
        if mode == "notify":
            injection_stage = "missed-notify-hint"
            missed_hint_id = f"soak-{mode}-{labels[0]}-missed-hint"
            expected[missed_hint_id] = labels[0]
            stop_child(labels[0])
            ready.discard(labels[0])
            status_path = root / "missed-notify-hint.status"
            status_path.unlink(missing_ok=True)
            target_path = goalflight_messages.inbox_path(
                Path(env["GOALFLIGHT_MESSAGES_DIR"]),
                missed_hint_id,
            )
            wrapper = root / "miss-one-notify-hint.py"
            wrapper.write_text(
                "import sys\n"
                "import time\n"
                "from pathlib import Path\n"
                f"sys.path.insert(0, {str(SCRIPTS)!r})\n"
                "import goalflight_messages\n"
                f"status_path = Path({str(status_path)!r})\n"
                f"target_path = Path({str(target_path)!r})\n"
                f"target_id = {missed_hint_id!r}\n"
                "original_wait = goalflight_messages._JournalChangeNotifier.wait\n"
                "def wait_once(self, timeout_s):\n"
                "    started = time.monotonic()\n"
                "    notified = original_wait(self, timeout_s)\n"
                "    if not notified or status_path.exists():\n"
                "        return notified\n"
                "    try:\n"
                "        target_committed = target_path.is_file() and target_id in target_path.read_text(encoding='utf-8')\n"
                "    except OSError:\n"
                "        target_committed = False\n"
                "    if not target_committed:\n"
                "        return notified\n"
                "    status_path.write_text('missed', encoding='utf-8')\n"
                "    deadline = started + max(0.0, timeout_s)\n"
                "    while self.active and time.monotonic() < deadline:\n"
                "        original_wait(self, max(0.0, deadline - time.monotonic()))\n"
                "    return False\n"
                "goalflight_messages._JournalChangeNotifier.wait = wait_once\n"
                "script = sys.argv[1]\n"
                "sys.argv = [script, *sys.argv[2:]]\n"
                "raise SystemExit(goalflight_messages.main())\n",
                encoding="utf-8",
            )
            start_child(labels[0], missed_hint_wrapper=wrapper)
            wait_ready({labels[0]})
            pump_once(0.25)
            publish_one(missed_hint_id, labels[0])
            pump_until(
                lambda: missed_hint_id in seen,
                timeout_s=goalflight_messages.WAKE_NOTIFY_SAFETY_POLL_S
                + phase_budget_s,
                description="notify did not deliver after its hint was suppressed",
            )
            missed_hint_reached_safety_poll = max(
                0.0,
                seen[missed_hint_id] - commit_times[missed_hint_id],
            )
            if not status_path.exists():
                raise RuntimeError("notify delivery did not suppress a WAL hint")

        for label in list(children):
            stop_child(label)
    except TimeoutError as exc:
        timeout_error = str(exc)
        run_failure = {
            "stage": injection_stage,
            "type": type(exc).__name__,
            "error": str(exc),
        }
        writer_cancel.set()
    except Exception as exc:
        run_failure = {
            "stage": injection_stage,
            "type": type(exc).__name__,
            "error": str(exc),
        }
        writer_cancel.set()
    finally:
        writer_cancel.set()
        for writer in writers:
            writer.join(timeout=5.0)
        for label in list(children):
            stop_child(label)
        holders.close()
        goalflight_journal.Journal._connect = original_connect
        goalflight_journal.Journal._domain_write = original_domain_write
        goalflight_journal.Journal.mark_delivery_projected = (
            original_mark_delivery_projected
        )

    missed_ids = sorted(set(expected) - set(seen))
    workload_ids = {event_id for event_id, _label in event_specs}
    samples = [
        latencies[event_id] * 1000
        for event_id in sorted(workload_ids.intersection(latencies))
    ]
    cpu_by_controller = {
        label: round(
            sum(
                float(record["cpu_seconds"])
                for record in cpu_records
                if record["controller"] == label
            ),
            6,
        )
        for label in labels
    }
    return {
        "mode": mode,
        "controllers": controllers,
        "events_per_controller": events_per_controller,
        "event_count_time_budget_seconds": round(event_budget_s, 3),
        "timed_out": timeout_error is not None,
        "timeout_error": timeout_error,
        "failure": run_failure,
        "latency_ms": {
            "p50": round(_percentile(samples, 0.50), 3) if samples else None,
            "p99": round(_percentile(samples, 0.99), 3) if samples else None,
            "samples": len(samples),
        },
        "missed_event_ids": missed_ids,
        "duplicate_event_ids": sorted(duplicate_ids),
        "unexpected_event_ids": sorted(unexpected_ids),
        "cpu_seconds_by_process": cpu_records,
        "cpu_seconds_by_controller": cpu_by_controller,
        "writer_threads_alive_after_cleanup": sum(
            thread.is_alive() for thread in writers
        ),
        "injections": {
            "failure": run_failure,
            "supervisor_restart_controller": restarted_label,
            "supervisor_process_restarted": supervisor_starts[restarted_label] >= 2,
            "event_delivered_after_supervisor_restart": pending_restart_id in seen,
            "supervisor_restart_event_latency_ms": (
                round(latencies[pending_restart_id] * 1000, 3)
                if pending_restart_id in latencies
                else None
            ),
            "wal_journal_rotation": wal_rotation,
            "wal_replacement_event_id": wal_replacement_id,
            "journal_busy_listener": busy_label,
            "journal_busy_mode": busy_journal_mode,
            "journal_busy_exclusive_lock_seconds": (
                round(busy_lock_s, 3) if busy_lock_s is not None else None
            ),
            "journal_busy_degraded_controllers": sorted(degraded_labels),
            "journal_busy_recovered_controllers": sorted(recovered_labels),
            "missed_hint_event_id": missed_hint_id,
            "missed_hint_safety_poll_seconds": (
                round(missed_hint_reached_safety_poll, 3)
                if missed_hint_reached_safety_poll is not None
                else None
            ),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--controllers", type=int, default=2)
    parser.add_argument("--events", type=int, default=100)
    parser.add_argument("--poll-secs", type=float, default=1.0)
    args = parser.parse_args()
    if args.controllers < 1 or args.events < 1 or args.poll_secs <= 0:
        parser.error("controllers, events, and poll-secs must be positive")
    scratch = os.environ.get("TMPDIR")
    with tempfile.TemporaryDirectory(prefix="listener-wake-soak-", dir=scratch) as name:
        root = Path(name)
        env = _environment(root / "machine")
        for key in AMBIENT_IDENTITY_ENV:
            os.environ.pop(key, None)
        os.environ.update(env)
        global goalflight_journal, goalflight_messages, goalflight_wake
        goalflight_journal = importlib.import_module("goalflight_journal")
        goalflight_messages = importlib.import_module("goalflight_messages")
        goalflight_wake = importlib.import_module("goalflight_wake")
        reports = [
            _run_mode(
                mode,
                root=root,
                controllers=args.controllers,
                events_per_controller=args.events,
                poll_s=args.poll_secs,
            )
            for mode in ("poll", "gated", "notify")
        ]
        poll_latency = reports[0]["latency_ms"]
        if isinstance(poll_latency, dict):
            for report in reports[1:]:
                mode_latency = report["latency_ms"]
                if isinstance(mode_latency, dict):
                    report["latency_delta_vs_poll_ms"] = {
                        metric: round(
                            float(mode_latency[metric]) - float(poll_latency[metric]),
                            3,
                        )
                        for metric in ("p50", "p99")
                        if mode_latency.get(metric) is not None
                        and poll_latency.get(metric) is not None
                    }
                    if report["mode"] == "gated":
                        deltas = report["latency_delta_vs_poll_ms"]
                        # Gated can be faster than poll; only extra delay is
                        # bounded by one poll interval.
                        report["latency_within_poll_interval"] = all(
                            float(delta) <= args.poll_secs * 1000
                            for delta in deltas.values()
                        )
    print(json.dumps(reports, indent=2, sort_keys=True))
    if any(
        report["missed_event_ids"]
        or report["duplicate_event_ids"]
        or report["unexpected_event_ids"]
        or report["timed_out"]
        or report["failure"] is not None
        or (
            report["mode"] == "gated"
            and not report.get("latency_within_poll_interval", False)
        )
        for report in reports
    ):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
