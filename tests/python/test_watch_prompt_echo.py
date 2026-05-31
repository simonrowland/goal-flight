#!/usr/bin/env python3
"""Regression test for the prompt-echo terminal-marker false-positive.

A worker that echoes its prompt (codex, grok) prints the prompt's own
"end with COMPLETE: ..." instruction to stdout BEFORE doing any work. Without a
guard, goalflight_watch.py matches that echoed marker and exits immediately
(observed 2026-05-30: dogfooded review jobs "completed" in ~5s). The watcher must
ignore marker lines that come verbatim from the prompt (--ignore-prompt-file) and
only complete on the worker's REAL marker.
"""

from __future__ import annotations

from support import skip_posix_on_native_windows

skip_posix_on_native_windows("watch prompt echo uses bash-tail and start_new_session")

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
WATCH = ROOT / "scripts" / "goalflight_watch.py"
sys.path.insert(0, str(SCRIPTS))

import goalflight_watch  # noqa: E402


def _run_watcher(
    tail: Path,
    status: Path,
    prompt: Path,
    ignore: bool,
    worker_pid: int,
    identity: dict | None = None,
    poll_secs: str = "1",
    max_idle_secs: str = "30",
):
    cmd = [sys.executable, str(WATCH), "--pid", str(worker_pid), "--tail", str(tail),
           "--status-json", str(status), "--poll-secs", poll_secs, "--max-idle-secs", max_idle_secs]
    if identity is not None:
        cmd += ["--worker-identity-json", json.dumps(identity, sort_keys=True)]
    if ignore:
        cmd += ["--ignore-prompt-file", str(prompt)]
    t0 = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=40)
    elapsed = time.time() - t0
    payload = {}
    term = {}
    if status.exists():
        payload = json.loads(status.read_text(encoding="utf-8"))
        term = (payload.get("terminal_marker") or {})
    return proc.returncode, elapsed, term, payload


def case_ignores_echoed_prompt_marker() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        prompt = tmp / "prompt.md"
        prompt.write_text("Do the review.\nCOMPLETE: PLACEHOLDER\n", encoding="utf-8")
        tail = tmp / "tail.txt"
        sink = tail.open("wb")
        try:
            # Worker echoes the prompt marker, then emits a byte-identical REAL
            # terminal marker. Only the initial prompt span may be ignored.
            worker = subprocess.Popen(
                ["bash", "-c", f'cat "{prompt}"; sleep 2; echo "COMPLETE: PLACEHOLDER"'],
                stdout=sink, stderr=subprocess.STDOUT, start_new_session=True)
        finally:
            sink.close()
        try:
            rc, elapsed, term, _ = _run_watcher(tail, tmp / "s.json", prompt, ignore=True, worker_pid=worker.pid)
        finally:
            worker.wait()
        assert rc == 0, f"expected exit 0 (complete), got {rc}"
        assert term.get("text") == "PLACEHOLDER", f"must complete on the REAL marker, got {term}"
        assert elapsed > 1.5, f"must wait past the echoed prompt, elapsed={elapsed:.1f}s (false-completed?)"


def case_without_ignore_trips_on_echo() -> None:
    # Control: WITHOUT the guard, the echoed PLACEHOLDER trips the watcher early.
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        prompt = tmp / "prompt.md"
        prompt.write_text("COMPLETE: PLACEHOLDER\n", encoding="utf-8")
        tail = tmp / "tail.txt"
        tail.write_text("COMPLETE: PLACEHOLDER\n", encoding="utf-8")  # only the echo so far
        worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"], start_new_session=True)
        try:
            rc, elapsed, term, _ = _run_watcher(tail, tmp / "s.json", prompt, ignore=False, worker_pid=worker.pid)
        finally:
            worker.terminate()
            worker.wait()
        assert term.get("text") == "PLACEHOLDER", f"control should trip on the echo, got {term}"


def case_prompt_ignore_stops_at_first_mismatch() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        prompt = tmp / "prompt.md"
        prompt.write_text("Do the review.\nCOMPLETE: PLACEHOLDER\n", encoding="utf-8")
        tail = tmp / "tail.txt"
        tail.write_text("Different first line.\nCOMPLETE: PLACEHOLDER\n", encoding="utf-8")
        worker = subprocess.Popen(["bash", "-c", "sleep 10"], start_new_session=True)
        try:
            rc, elapsed, term, _ = _run_watcher(
                tail,
                tmp / "s.json",
                prompt,
                ignore=True,
                worker_pid=worker.pid,
                max_idle_secs="3",
            )
        finally:
            worker.terminate()
            worker.wait()
        assert rc == 0, f"mismatch before marker must not mask real marker, got rc={rc}"
        assert term.get("text") == "PLACEHOLDER", f"real marker after mismatch was masked, got {term}"
        assert elapsed < 2.0, f"watcher should wake on real marker, elapsed={elapsed:.1f}s"


def case_identity_mismatch_not_alive() -> None:
    original = goalflight_watch.goalflight_ledger.process_identity
    try:
        goalflight_watch.goalflight_ledger.process_identity = lambda pid: {
            "pid": pid,
            "lstart": "actual process start",
            "comm": "worker",
        }
        is_alive, reason, current = goalflight_watch.worker_alive(
            12345,
            {"pid": 12345, "lstart": "expected process start", "comm": "worker"},
        )
    finally:
        goalflight_watch.goalflight_ledger.process_identity = original

    assert is_alive is False, current
    assert reason == "pid_reused_lstart", reason


def case_incomplete_identity_is_inconclusive_alive() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        prompt = tmp / "prompt.md"
        prompt.write_text("", encoding="utf-8")
        tail = tmp / "tail.txt"
        tail.write_text("COMPLETE: identity stayed fail-safe\n", encoding="utf-8")
        worker = subprocess.Popen(["bash", "-c", "sleep 10"], start_new_session=True)
        try:
            rc, _elapsed, term, payload = _run_watcher(
                tail,
                tmp / "s.json",
                prompt,
                ignore=False,
                worker_pid=worker.pid,
                identity={"pid": worker.pid},
            )
        finally:
            worker.terminate()
            worker.wait()
        assert rc == 0, f"incomplete identity should not classify live worker dead, got rc={rc}"
        assert term.get("text") == "identity stayed fail-safe", term
        assert payload.get("worker_alive") is True, payload
        assert payload.get("worker_identity_reason", "").startswith("identity_inconclusive_"), payload


def case_steer_ack_is_non_terminal_marker() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tail = Path(tmp) / "tail.txt"
        tail.write_text("STATUS: working\nSTEER-ACK: 7\n", encoding="utf-8")
        markers, _size = goalflight_watch.extract_markers(tail)

    assert markers[-1]["kind"] == "STEER-ACK", markers
    assert markers[-1]["text"] == "7", markers
    assert "STEER-ACK" not in goalflight_watch.TERMINAL_MARKERS


def main() -> None:
    case_ignores_echoed_prompt_marker()
    case_without_ignore_trips_on_echo()
    case_prompt_ignore_stops_at_first_mismatch()
    case_identity_mismatch_not_alive()
    case_incomplete_identity_is_inconclusive_alive()
    case_steer_ack_is_non_terminal_marker()
    print("OK: goalflight_watch prompt-echo guard tests pass")


if __name__ == "__main__":
    main()
