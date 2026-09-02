#!/usr/bin/env python3
"""Captured-output redaction: shape-triggered, including seatless grok dispatch.

A grok launch that never resolves a named seat used to skip the env-side
credential strip. That left credential-shaped bytes free to land in the tail,
status JSON, watcher log, journal/outbox, and mail headlines. Redaction now
keys on the public secret shape, so those sinks stay clean even when no seat
resolves.
"""

from __future__ import annotations

from support import skip_posix_on_native_windows

skip_posix_on_native_windows("seatless grok dispatch uses POSIX spawn + flock")

import io
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
DISPATCH_PY = SCRIPTS / "goalflight_dispatch.py"
sys.path.insert(0, str(SCRIPTS))

import goalflight_acp_run  # noqa: E402
import goalflight_compat  # noqa: E402
import goalflight_dispatch as D  # noqa: E402
import goalflight_liveness  # noqa: E402
import goalflight_messages as GM  # noqa: E402
import goalflight_output_redact as redact  # noqa: E402

# Obviously fake. Matches the public shape; must never be a live credential.
PLACEHOLDER = "xai-testfake000000000000000000000000"
SHORT_LABEL = "xai-0"


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_redact_text_requires_presence_then_strips() -> None:
    raw = f"pre {PLACEHOLDER} post"
    assert PLACEHOLDER in raw
    scrubbed = redact.redact_text(raw)
    assert PLACEHOLDER not in scrubbed
    assert redact.REDACTED in scrubbed
    assert "pre " in scrubbed
    assert " post" in scrubbed


def test_redact_text_leaves_short_labels_alone() -> None:
    raw = f"account {SHORT_LABEL} stays"
    assert redact.redact_text(raw) == raw


def test_filter_stream_keeps_clean_lines_when_one_line_is_secret() -> None:
    src = io.BytesIO(f"keep-me\nLEAK:{PLACEHOLDER}\nkeep-too\n".encode("utf-8"))
    dst = io.BytesIO()
    assert PLACEHOLDER in src.getvalue().decode("utf-8")
    redact.filter_stream(src, dst)
    text = dst.getvalue().decode("utf-8")
    assert PLACEHOLDER not in text
    assert "keep-me\n" in text
    assert "keep-too\n" in text
    assert f"LEAK:{redact.REDACTED}\n" in text


class _ChunkedSource:
    """Readable that yields one chunk per read so a size flush can fire mid-stream."""

    def __init__(self, chunks: list[bytes], dst: io.BytesIO) -> None:
        self._chunks = list(chunks)
        self._dst = dst
        self.sizes_before_read: list[int] = []

    def read(self, _size: int) -> bytes:
        self.sizes_before_read.append(len(self._dst.getvalue()))
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


def test_filter_stream_size_flush_emits_before_newline() -> None:
    dst = io.BytesIO()
    src = _ChunkedSource([b"a" * 2000, b"b" * 2000], dst)
    redact.filter_stream(src, dst, flush_bytes=1024, flush_secs=0)
    assert src.sizes_before_read[0] == 0
    assert src.sizes_before_read[1] > 0, src.sizes_before_read
    text = dst.getvalue().decode("utf-8")
    assert "a" * 2000 in text
    assert "b" * 2000 in text


def test_filter_stream_redacts_secret_split_across_size_flush() -> None:
    assert PLACEHOLDER.startswith("xai-")
    prefix, rest = PLACEHOLDER[:10], PLACEHOLDER[10:]
    dst = io.BytesIO()
    src = _ChunkedSource(
        [f"LEAK:{prefix}".encode("utf-8"), f"{rest} trailing\n".encode("utf-8")],
        dst,
    )
    redact.filter_stream(src, dst, flush_bytes=8, flush_secs=0)
    text = dst.getvalue().decode("utf-8")
    assert PLACEHOLDER not in text
    assert prefix not in text
    assert rest not in text
    assert "LEAK:" in text
    assert redact.REDACTED in text
    assert "trailing" in text


def test_filter_stream_time_flush_redacts_secret_split_across_boundary() -> None:
    read_fd, write_fd = os.pipe()
    dst = io.BytesIO()
    events: list[tuple[float, bytes]] = []

    class _Recording(io.RawIOBase):
        def write(self, data: bytes) -> int:  # type: ignore[override]
            chunk = bytes(data)
            events.append((time.monotonic(), chunk))
            dst.write(chunk)
            return len(chunk)

        def flush(self) -> None:
            return None

    assert PLACEHOLDER.startswith("xai-")
    prefix, rest = PLACEHOLDER[:12], PLACEHOLDER[12:]

    def producer() -> None:
        os.write(write_fd, f"LEAK:{prefix}".encode("utf-8"))
        time.sleep(0.8)
        os.write(write_fd, f"{rest} trailing\n".encode("utf-8"))
        os.close(write_fd)

    worker = threading.Thread(target=producer)
    started = time.monotonic()
    worker.start()
    try:
        with os.fdopen(read_fd, "rb", buffering=0) as src:
            redact.filter_stream(src, _Recording(), flush_bytes=1_000_000, flush_secs=0.2)
    finally:
        worker.join(timeout=5)

    elapsed = time.monotonic() - started
    text = dst.getvalue().decode("utf-8")
    assert PLACEHOLDER not in text
    assert prefix not in text
    assert rest not in text
    assert redact.REDACTED in text
    assert "LEAK:" in text
    assert "trailing" in text
    assert events, "time flush must emit something before EOF"
    assert events[0][0] - started < 0.6, (events, elapsed)
    first_payload = b"".join(chunk for _ts, chunk in events[:1])
    assert b"LEAK:" in first_payload
    assert PLACEHOLDER.encode("utf-8") not in first_payload


def test_secret_holdback_keeps_incomplete_prefix() -> None:
    buf = b"hello xai-abcdefghij"
    hold = redact.secret_holdback_len(buf)
    assert hold == len(b"xai-abcdefghij")
    emit, keep = redact.split_partial_flush(buf)
    assert emit == b"hello "
    assert keep == b"xai-abcdefghij"
    assert PLACEHOLDER.encode("utf-8") not in emit


def test_secret_holdback_keeps_19_char_entropy_prefix() -> None:
    """xai- + 19 entropy chars sits on the 19/20 match boundary.

    One more char becomes a credential; holding 19 must not emit the prefix.
    """
    prefix = b"xai-" + b"a" * 19
    assert len(prefix) == 23
    buf = b"hello " + prefix
    hold = redact.secret_holdback_len(buf)
    assert hold == 23, hold
    emit, keep = redact.split_partial_flush(buf)
    assert emit == b"hello "
    assert keep == prefix
    completed = prefix + b"b trailing\n"
    flushed, leftover = redact.split_partial_flush(completed)
    assert leftover == b""
    assert prefix not in flushed
    assert b"xai-" + b"a" * 20 not in flushed
    assert redact.REDACTED.encode("utf-8") in flushed
    assert b"trailing" in flushed


def test_write_status_redacts_nested_marker_text(tmp_path: Path) -> None:
    path = tmp_path / "worker.status.json"
    payload = {
        "schema": "goalflight.status.v1",
        "last_marker": {"kind": "COMPLETE", "text": f"{PLACEHOLDER} done"},
        "error": {"agent_stderr_tail": f"bearer {PLACEHOLDER}"},
    }
    serialized_before = json.dumps(payload)
    assert PLACEHOLDER in serialized_before
    goalflight_liveness.write_status(path, payload)
    written = path.read_text(encoding="utf-8")
    assert PLACEHOLDER not in written
    parsed = json.loads(written)
    assert redact.REDACTED in parsed["last_marker"]["text"]
    assert PLACEHOLDER not in parsed["error"]["agent_stderr_tail"]


def test_agent_stderr_redacts_secret_split_across_chunks(tmp_path: Path) -> None:
    path = tmp_path / "worker.agent-stderr.log"
    capture = goalflight_acp_run.AgentStderrCapture(path, max_bytes=4096)
    assert PLACEHOLDER.startswith("xai-")
    capture._append(PLACEHOLDER[:6].encode("utf-8"))
    first = path.read_text(encoding="utf-8") if path.exists() else ""
    capture._append((PLACEHOLDER[6:] + " trailing\n").encode("utf-8"))
    final = path.read_text(encoding="utf-8")
    assert PLACEHOLDER[:6] in first or first == ""
    assert PLACEHOLDER not in final
    assert redact.REDACTED in final


def test_agent_stderr_truncation_does_not_reemit_secret(tmp_path: Path) -> None:
    path = tmp_path / "worker.agent-stderr.log"
    capture = goalflight_acp_run.AgentStderrCapture(path, max_bytes=64)
    capture._append((PLACEHOLDER + "\n").encode("utf-8"))
    assert PLACEHOLDER not in path.read_text(encoding="utf-8")
    capture._append(("n" * 200).encode("utf-8"))
    assert PLACEHOLDER not in path.read_text(encoding="utf-8")


def test_sanitize_display_redacts_mail_headlines() -> None:
    raw = f"COMPLETE: job — {PLACEHOLDER}"
    assert PLACEHOLDER in raw
    shown = GM.sanitize_display(raw, limit=200)
    assert PLACEHOLDER not in shown
    assert redact.REDACTED in shown


def test_spawn_filter_redacts_tail_regardless_of_seat(tmp_path: Path) -> None:
    tail = tmp_path / "spawn.tail"
    sidecar = tmp_path / "before.txt"
    script = (
        "from pathlib import Path\n"
        f"secret = {PLACEHOLDER!r}\n"
        f"Path({str(sidecar)!r}).write_text(secret)\n"
        "print('LEAK:' + secret, flush=True)\n"
    )
    pid = D._spawn_daemonized_process(
        [sys.executable, "-c", script],
        env=os.environ.copy(),
        stdout_path=tail,
        stdout_mode="wb",
        stderr="stdout",
        serialize_stdout=True,
        label="redact-filter",
    )
    deadline = time.monotonic() + 10.0
    text = ""
    while time.monotonic() < deadline:
        if not goalflight_compat.pid_alive(pid) and tail.exists():
            try:
                with D._tail_reconciliation_lock(tail):
                    text = tail.read_text(encoding="utf-8", errors="replace")
                break
            except D._TailLockBusy:
                time.sleep(0.05)
                continue
        time.sleep(0.05)
    else:
        raise AssertionError("spawned worker/filter did not release the tail")
    assert sidecar.is_file(), "sidecar missing: fixture never wrote the placeholder"
    assert PLACEHOLDER in sidecar.read_text(encoding="utf-8")
    assert PLACEHOLDER not in text
    assert "LEAK:" in text
    assert redact.REDACTED in text


def _isolated_env(tmp_path: Path, home: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["GOALFLIGHT_STATE_DIR"] = str(tmp_path / "state")
    env["GOALFLIGHT_DISPATCH_DIR"] = str(tmp_path / "dispatch")
    env["GOALFLIGHT_JOURNAL_DIR"] = str(tmp_path / "journals")
    env["GOALFLIGHT_WAKE_LEDGER"] = str(tmp_path / "wake-ledger")
    env["GOALFLIGHT_WAKE_LEDGER_DIR"] = str(tmp_path / "wake-ledger")
    env["GOALFLIGHT_MESSAGES_DIR"] = str(tmp_path / "messages")
    env["GOALFLIGHT_TASK_STORE"] = str(tmp_path / "tasks")
    env["GOALFLIGHT_TASK_STORE_DIR"] = str(tmp_path / "tasks")
    env["GOALFLIGHT_PIDFILE_DIR"] = str(tmp_path / "pids")
    env["GOAL_FLIGHT_PIDFILE_DIR"] = str(tmp_path / "pids")
    env["GOALFLIGHT_CAPACITY_CONF"] = os.devnull
    env["GOALFLIGHT_CAPACITY_WAIT_S"] = "0"
    env["GOALFLIGHT_TEST_PROJECT_ROOT"] = str(tmp_path)
    env.pop("GOALFLIGHT_STEER_FILE", None)
    env.pop("GROK_HOME", None)
    return env


def _tree_text(root: Path) -> str:
    chunks: list[str] = []
    if not root.exists():
        return ""
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            chunks.append(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return "\n".join(chunks)


def test_seatless_grok_dispatch_redacts_every_capture_sink(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write(
        home / ".grok" / "config.toml",
        '[ui]\npermission_mode = "always-approve"\n',
    )
    # "Seatless" means no named seats, not no evidence: an unpinned grok launch
    # is refused unless a MEASURED usable probe exists (a placeholder key cannot
    # be probed, and unknown is never permission to bill the host). Give the
    # isolated HOME a fresh usable host record so selection lands on the host.
    _write(
        home / ".goal-flight" / "grok-seat-states.json",
        json.dumps(
            {
                "version": 1,
                "updated_at": time.time(),
                "seats": {
                    "": {
                        "ok": True,
                        "used_percent": 1.0,
                        "probe_state": "usable",
                        "auth_state": "valid",
                        "error": None,
                    }
                },
            }
        ),
    )
    dispatch_id = "seatless-redact"
    dispatch_dir = tmp_path / "dispatch"
    tail = dispatch_dir / f"{dispatch_id}.tail"
    status_path = dispatch_dir / f"{dispatch_id}.status.json"
    watch_log = dispatch_dir / f"{dispatch_id}.watcher.log"
    sidecar = tmp_path / "before-secret.txt"
    env = _isolated_env(tmp_path, home)
    env["GROK_API_KEY"] = PLACEHOLDER
    env["XAI_API_KEY"] = PLACEHOLDER
    worker_py = tmp_path / "seatless_worker.py"
    worker_py.write_text(
        "from pathlib import Path\n"
        "import json, os\n"
        f"secret = {PLACEHOLDER!r}\n"
        "payload = {\n"
        "    'printed': secret,\n"
        "    'env': os.environ.get('XAI_API_KEY') or os.environ.get('GROK_API_KEY') or '',\n"
        "}\n"
        f"Path({str(sidecar)!r}).write_text(json.dumps(payload))\n"
        "print('pre:' + secret, flush=True)\n"
        "print('COMPLETE: ' + os.environ['GOALFLIGHT_DISPATCH_ID']"
        " + ' — ' + secret, flush=True)\n",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [
            sys.executable,
            str(DISPATCH_PY),
            "--unregistered-forced",
            "--agent",
            "grok-code",
            "--dispatch-id",
            dispatch_id,
            "--tail",
            str(tail),
            "--status-json",
            str(status_path),
            "--cwd",
            str(tmp_path),
            "--poll-secs",
            "0.1",
            "--max-idle-secs",
            "8",
            "--foreground",
            "--",
            sys.executable,
            str(worker_py),
        ],
        cwd=str(tmp_path),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=90,
    )
    assert sidecar.is_file(), f"fixture never wrote the placeholder\nstderr={proc.stderr}"
    before = json.loads(sidecar.read_text(encoding="utf-8"))
    assert before["printed"] == PLACEHOLDER
    assert PLACEHOLDER in before["printed"]
    # Seatless: no named seat, so the env-side strip must not have been the
    # thing that hid this value. The worker inherited it and printed it.
    assert before["env"] == PLACEHOLDER

    sinks = {
        "tail": tail.read_text(encoding="utf-8", errors="replace") if tail.exists() else "",
        "status": status_path.read_text(encoding="utf-8", errors="replace")
        if status_path.exists()
        else "",
        "watcher": watch_log.read_text(encoding="utf-8", errors="replace")
        if watch_log.exists()
        else "",
        "journals": _tree_text(tmp_path / "journals"),
        "messages": _tree_text(tmp_path / "messages"),
        "stdout": proc.stdout or "",
        "stderr": proc.stderr or "",
        "dispatch_dir": _tree_text(dispatch_dir),
        "state": _tree_text(tmp_path / "state"),
    }
    for name, blob in sinks.items():
        assert PLACEHOLDER not in blob, f"{name} leaked placeholder\n{blob[-500:]}"

    assert proc.returncode == 0, (
        f"seatless grok dispatch failed rc={proc.returncode}\n"
        f"stdout={proc.stdout}\nstderr={proc.stderr}"
    )
    assert status_path.is_file(), "status JSON missing"
    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert status.get("effective_account") in (None, "", "default")
    assert "pre:" in sinks["tail"]
    assert redact.REDACTED in sinks["tail"]

    journal_hits = list((tmp_path / "journals").rglob("*.sqlite3"))
    assert journal_hits, "journal was not created; cannot prove outbox scrub"
    saw_outbox = False
    for db_path in journal_hits:
        try:
            connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        except sqlite3.Error:
            continue
        try:
            rows = connection.execute(
                "SELECT payload_json FROM terminal_outbox"
            ).fetchall()
        except sqlite3.Error:
            rows = []
        finally:
            connection.close()
        for (payload_json,) in rows:
            saw_outbox = True
            assert payload_json, "outbox row had empty payload"
            assert PLACEHOLDER not in payload_json
            payload = json.loads(payload_json)
            text = payload.get("text") or ""
            observation = json.dumps(payload.get("observation") or {})
            assert PLACEHOLDER not in text
            assert PLACEHOLDER not in observation
            assert redact.REDACTED in text or redact.REDACTED in observation
    assert saw_outbox, "no terminal_outbox rows; cannot prove outbox scrub"
