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


def case_matching_lstart_ignores_comm_form_change() -> None:
    original = goalflight_watch.goalflight_ledger.process_identity
    try:
        goalflight_watch.goalflight_ledger.process_identity = lambda pid: {
            "pid": pid,
            "lstart": "Sun May 31 19:28:48 2026",
            "comm": "(grok-0.2.11-maco)",
        }
        is_alive, reason, current = goalflight_watch.worker_alive(
            12345,
            {"pid": 12345, "lstart": "Sun May 31 19:28:48 2026", "comm": "grok"},
        )
    finally:
        goalflight_watch.goalflight_ledger.process_identity = original

    assert is_alive is True, current
    assert reason == "live", reason


def case_same_second_pid_reuse_with_different_comm_is_not_alive() -> None:
    # P1a (lstart granularity hole): lstart is second-granularity, so a pid
    # reused within the same formatted second has an identical lstart. A matching
    # lstart must NOT alone read "live" when comm proves a different process.
    original = goalflight_watch.goalflight_ledger.process_identity
    try:
        goalflight_watch.goalflight_ledger.process_identity = lambda pid: {
            "pid": pid,
            "lstart": "Sun May 31 19:28:48 2026",
            "comm": "node",
        }
        is_alive, reason, current = goalflight_watch.worker_alive(
            12345,
            {"pid": 12345, "lstart": "Sun May 31 19:28:48 2026", "comm": "grok"},
        )
    finally:
        goalflight_watch.goalflight_ledger.process_identity = original

    assert is_alive is False, current
    assert reason == "pid_reused_lstart_comm", reason


def case_missing_lstart_uses_tolerant_comm_fallback() -> None:
    original = goalflight_watch.goalflight_ledger.process_identity
    try:
        goalflight_watch.goalflight_ledger.process_identity = lambda pid: {
            "pid": pid,
            "comm": "(grok-0.2.11-maco)",
        }
        is_alive, reason, current = goalflight_watch.worker_alive(
            12345,
            {"pid": 12345, "comm": "grok"},
        )
    finally:
        goalflight_watch.goalflight_ledger.process_identity = original

    assert is_alive is True, current
    assert reason == "live", reason


def case_missing_lstart_unrelated_comm_is_not_alive() -> None:
    original = goalflight_watch.goalflight_ledger.process_identity
    try:
        goalflight_watch.goalflight_ledger.process_identity = lambda pid: {
            "pid": pid,
            "comm": "python",
        }
        is_alive, reason, current = goalflight_watch.worker_alive(
            12345,
            {"pid": 12345, "comm": "grok"},
        )
    finally:
        goalflight_watch.goalflight_ledger.process_identity = original

    assert is_alive is False, current
    assert reason == "pid_reused_comm", reason


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


def case_mid_output_marker_ignored() -> None:
    """Regression for P1 terminal-marker injection: a tail with marker token in
    mid-output (printed, cat'ed, or inside fence) must NOT set terminal/complete.
    Genuine terminal marker as the actual last non-empty line must still complete.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        tail = tmp / "tail.txt"
        # mid-output RESULT (as if cat or printf of data) + fenced example + more content:
        # marker is present but not last nonempty line -> watcher must ignore for terminal.
        tail.write_text(
            "work on chunk 42\n"
            "RESULT: {\"injected_mid\":true}\n"
            "fenced demo:\n```\nCOMPLETE: bad\n```\n"
            "still more output after the would-be markers\n",
            encoding="utf-8",
        )
        worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"], start_new_session=True)
        try:
            rc, elapsed, term, _ = _run_watcher(
                tail, tmp / "s.json", tmp / "p.md", ignore=False, worker_pid=worker.pid,
                poll_secs="0.2", max_idle_secs="1",
            )
        finally:
            worker.terminate()
            worker.wait()
        assert rc == 2, f"mid-output marker must not complete (expect idle rc=2), got {rc}"
        assert not term or term.get("kind") not in goalflight_watch.TERMINAL_MARKERS, f"terminal_marker must be absent or non-terminal for mid case, got {term}"
        assert elapsed < 4.0

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        tail = tmp / "tail.txt"
        # Same mid junk, but genuine COMPLETE as the *last* line -> must complete on it.
        tail.write_text(
            "work on chunk 42\n"
            "RESULT: {\"injected_mid\":true}\n"
            "fenced demo:\n```\nCOMPLETE: bad\n```\n"
            "still more output after the would-be markers\n"
            "COMPLETE: genuine-payload\n",
            encoding="utf-8",
        )
        worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"], start_new_session=True)
        try:
            rc, elapsed, term, _ = _run_watcher(
                tail, tmp / "s.json", tmp / "p.md", ignore=False, worker_pid=worker.pid,
                poll_secs="0.2", max_idle_secs="2",
            )
        finally:
            worker.terminate()
            worker.wait()
        assert rc == 0, f"genuine last-line terminal must complete, got {rc}"
        assert term.get("kind") == "COMPLETE", term
        assert term.get("text") == "genuine-payload", term


def case_ready_terminal_marker() -> None:
    """READY: is a terminal marker only on the last non-empty line (Investigator shape)."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        tail = tmp / "tail.txt"
        tail.write_text(
            "TL;DR: audit done\n"
            "READY: docs-private/research/2026-06-03-audit/findings.md\n"
            "more output after READY (not terminal)\n",
            encoding="utf-8",
        )
        worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"], start_new_session=True)
        try:
            rc, elapsed, term, _ = _run_watcher(
                tail, tmp / "s.json", tmp / "p.md", ignore=False, worker_pid=worker.pid,
                poll_secs="0.2", max_idle_secs="1",
            )
        finally:
            worker.terminate()
            worker.wait()
        assert rc == 2, f"mid-output READY must not complete, got {rc}"
        assert not term or term.get("kind") not in goalflight_watch.TERMINAL_MARKERS, f"got {term}"
        assert elapsed < 2.0

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        tail = tmp / "tail.txt"
        tail.write_text(
            "TL;DR: audit done\n"
            "Findings: P0 0, P1 1, P2 0, P3 0\n"
            "Strongest concern: none\n"
            "READY: docs-private/research/2026-06-03-audit/findings.md\n",
            encoding="utf-8",
        )
        worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"], start_new_session=True)
        try:
            rc, elapsed, term, _ = _run_watcher(
                tail, tmp / "s.json", tmp / "p.md", ignore=False, worker_pid=worker.pid,
                poll_secs="0.2", max_idle_secs="2",
            )
        finally:
            worker.terminate()
            worker.wait()
        assert rc == 0, f"last-line READY must complete, got {rc}"
        assert term.get("kind") == "READY", term
        assert "findings.md" in term.get("text", ""), term


PYNEC_OBSERVED_TAIL = """
RESULT: W-pynec-fixes-2
- `short_dipole`: max_gain_dbi `1.7496324917822492`, directivity/gain linear `1.4961090471558036`
- `half_wave_dipole`: max_gain_dbi `2.17743874914555`, directivity/gain linear `1.6509878413064911`
- `small_loop_screen`: max_gain_dbi `1.7429620750016896`, gain linear `1.4938129068081143`
- Grading remains honestly `BLOCKED(pynec-source-unresolved)` / `REPORT_ONLY`; no literature numbers fabricated.

COMPLETE: W-pynec-fixes-2

No commit made. `GOALFLIGHT_STEER_FILE` was unset in this process, so no steer ack was possible.

"""

RF_B5_OBSERVED_TRAILER = """- [live-grade-2026-06-11-round5.md](/Users/simonrowland/Repos/kiln/docs-private/research/2026-06-11-battery-blast/rf-b5/live-grade-2026-06-11-round5.md)

Verification:
- `PYTHONPATH=$PWD:$HOME/Repos python3 -m pytest templates/tests/test_analytic_plasma_decks.py -q`
- `48 passed, 11 skipped`
- `git diff --check` clean

Production controller should run production RF-B5 variants: base, half-ne, double-ne, double-b, flip-b, vacuum, then grade with `grade_rf_faraday_openpmd` in an environment with `h5py`.

FARR/PyNEC files were not touched; FARR P1 must align to this family Faraday sign convention in follow-up.

"""

SYNCHRAD_OBSERVED_TAIL = """- Run-spec env coverage.
- No-device fail-closed test without real `pyopencl`.

Verification:
- `PYTHONPATH=$PWD:$HOME/Repos python3 -m pytest templates/tests/test_rf_synchrad_larmor.py -q` -> `12 passed in 0.75s`
- `git diff --check` clean for target files.
- `RESULT: W-synchrad-ctx pytest exit=0`
- `COMPLETE: W-synchrad-ctx tests`

No live SynchRad run. No commit. `$GOALFLIGHT_STEER_FILE` was unset in tool env, so no steer messages to ack.

"""


def _run_dead_worker_tail(tail_text: str, prompt_text: str = ""):
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        prompt = tmp / "prompt.md"
        prompt.write_text(prompt_text, encoding="utf-8")
        tail = tmp / "tail.txt"
        tail.write_text(tail_text, encoding="utf-8")
        worker = subprocess.Popen([sys.executable, "-c", ""], start_new_session=True)
        worker.wait()
        return _run_watcher(
            tail,
            tmp / "s.json",
            prompt,
            ignore=True,
            worker_pid=worker.pid,
            poll_secs="0.2",
            max_idle_secs="30",
        )


def case_worker_dead_final_reconciliation_observed_shapes() -> None:
    cases = [
        ("pynec bare complete", PYNEC_OBSERVED_TAIL, "W-pynec-fixes-2"),
        (
            "rf status complete",
            "STATUS: COMPLETE: W-rf-b5-round5\n"
            + "".join(f"post-marker summary line {idx}\n" for idx in range(1, 13))
            + RF_B5_OBSERVED_TRAILER,
            "W-rf-b5-round5",
        ),
        ("synchrad bullet backtick complete", SYNCHRAD_OBSERVED_TAIL, "W-synchrad-ctx tests"),
    ]
    for label, tail_text, expected_text in cases:
        rc, _elapsed, term, payload = _run_dead_worker_tail(tail_text)
        assert rc == 0, f"{label}: expected final reconciliation exit 0, got {rc} ({payload})"
        assert payload.get("state") == "complete", f"{label}: {payload}"
        assert payload.get("reason") == "marker:COMPLETE:final_reconciliation", f"{label}: {payload}"
        assert term.get("kind") == "COMPLETE", f"{label}: {term}"
        assert term.get("text") == expected_text, f"{label}: {term}"


def case_worker_dead_final_reconciliation_rejects_diff_and_prompt_echo() -> None:
    rc, _elapsed, term, payload = _run_dead_worker_tail(
        "diff --git a/file b/file\n"
        "@@ -1 +1 @@\n"
        "+STATUS: COMPLETE: diff-output-only\n"
        "worker died before sign-off\n"
    )
    assert rc == 1, f"diff echo must not complete, got rc={rc} ({payload})"
    assert payload.get("reason") == "worker_dead_no_terminal_marker", payload
    assert not term, term

    negative_cases = [
        ("deletion no space", "-STATUS: COMPLETE: x\n"),
        ("context line leading space", " STATUS: COMPLETE: x\n"),
        ("hunk deletion indented marker", "@@ -1,1 +1,0 @@\n-    COMPLETE: x\n"),
    ]
    for label, tail_text in negative_cases:
        rc, _elapsed, term, payload = _run_dead_worker_tail(tail_text)
        assert rc == 1, f"{label}: expected worker-dead no-marker exit 1, got rc={rc} ({payload})"
        assert payload.get("reason") == "worker_dead_no_terminal_marker", f"{label}: {payload}"
        assert not term, f"{label}: {term}"

    prompt = "Do the work.\nCOMPLETE: prompt-only\n"
    rc, _elapsed, term, payload = _run_dead_worker_tail(
        prompt + "worker died before sign-off\n",
        prompt_text=prompt,
    )
    assert rc == 1, f"prompt echo only must not complete, got rc={rc} ({payload})"
    assert payload.get("reason") == "worker_dead_no_terminal_marker", payload
    assert not term, term


def case_worker_dead_failed_marker_blocks() -> None:
    rc, _elapsed, term, payload = _run_dead_worker_tail("FAILED: x\n")
    assert rc == 4, f"FAILED should map to blocked exit 4, got rc={rc} ({payload})"
    assert payload.get("state") == "blocked", payload
    assert payload.get("reason") == "marker:FAILED:final_reconciliation", payload
    assert term.get("kind") == "FAILED", term
    assert term.get("text") == "x", term


def main() -> None:
    case_ignores_echoed_prompt_marker()
    case_without_ignore_trips_on_echo()
    case_prompt_ignore_stops_at_first_mismatch()
    case_identity_mismatch_not_alive()
    case_matching_lstart_ignores_comm_form_change()
    case_same_second_pid_reuse_with_different_comm_is_not_alive()
    case_missing_lstart_uses_tolerant_comm_fallback()
    case_missing_lstart_unrelated_comm_is_not_alive()
    case_incomplete_identity_is_inconclusive_alive()
    case_steer_ack_is_non_terminal_marker()
    case_mid_output_marker_ignored()
    case_ready_terminal_marker()
    case_worker_dead_final_reconciliation_observed_shapes()
    case_worker_dead_final_reconciliation_rejects_diff_and_prompt_echo()
    case_worker_dead_failed_marker_blocks()
    print("OK: goalflight_watch prompt-echo guard tests pass")


if __name__ == "__main__":
    main()
