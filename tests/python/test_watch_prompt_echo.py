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
import gzip
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
WATCH = ROOT / "scripts" / "goalflight_watch.py"
ROUND4_EVIDENCE = ROOT / "docs-private" / "research" / "gf-bug-watcher-round4"
PUBLIC_ROUND4_FIXTURES = ROOT / "tests" / "fixtures" / "watch_prompt_echo"
sys.path.insert(0, str(SCRIPTS))

import goalflight_watch  # noqa: E402


CODEX_BANNER_14 = (
    "OpenAI Codex v0.137.0\n"
    "--------\n"
    "workdir: /Users/simonrowland/Repos/goal-flight\n"
    "model: gpt-5.5\n"
    "provider: openai\n"
    "approval: never\n"
    "sandbox: workspace-write [workdir, /tmp, $TMPDIR]\n"
    "reasoning effort: xhigh\n"
    "reasoning summaries: none\n"
    "session id: 019eb974-0dee-79d2-b315-8d2910167bf4\n"
    "--------\n"
    "user\n"
    "You have a steer mailbox at `$GOALFLIGHT_STEER_FILE`. Read it AT THE TOP OF EACH ITERATION and IMMEDIATELY BEFORE ANY git commit/push. Incorporate new messages into your plan; ack each with `STEER-ACK\n"
    "\n"
)


def _run_watcher(
    tail: Path,
    status: Path,
    prompt: Path,
    ignore: bool,
    worker_pid: int,
    identity: dict | None = None,
    poll_secs: str = "1",
    max_idle_secs: str = "30",
    dispatch_id: str | None = None,
    project_root: Path | None = None,
    task_ids: str | None = None,
    agent: str | None = None,
):
    cmd = [sys.executable, str(WATCH), "--pid", str(worker_pid), "--tail", str(tail),
           "--status-json", str(status), "--poll-secs", poll_secs, "--max-idle-secs", max_idle_secs]
    if dispatch_id:
        cmd += ["--dispatch-id", dispatch_id]
    if project_root is not None:
        cmd += ["--project-root", str(project_root)]
    if task_ids:
        cmd += ["--task-ids", task_ids]
    if agent:
        cmd += ["--agent", agent]
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


def _write_task_store(project: Path) -> None:
    item = {
        "schema_version": 1,
        "id": "t-001",
        "kind": "task",
        "title": "Linked watcher task",
        "blocked_by": [],
        "links": [],
        "done": False,
    }
    docs = project / "docs-private"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "tasks.jsonl").write_text(json.dumps(item, separators=(",", ":")) + "\n", encoding="utf-8")
    (docs / "tasks-data.js").write_text(
        goalflight_watch.goalflight_task._items_data_js([item]),
        encoding="utf-8",
    )


def _read_task(project: Path) -> dict:
    rows = [
        json.loads(line)
        for line in (project / "docs-private" / "tasks.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(rows) == 1, rows
    return rows[0]


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
        assert elapsed < 4.0, f"watcher should wake on real marker, elapsed={elapsed:.1f}s"


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
        assert elapsed < 10.0

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
        assert elapsed < 10.0

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


def case_task_terminal_breadcrumb_failure_blocks_completion() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        project = tmp / "project"
        _write_task_store(project)
        (project / "docs-private" / "tasks.jsonl").write_text("{bad json\n", encoding="utf-8")
        tail = tmp / "tail.txt"
        tail.write_text("work done\nCOMPLETE: linked task\n", encoding="utf-8")
        worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"], start_new_session=True)
        try:
            rc, _, term, payload = _run_watcher(
                tail,
                tmp / "s.json",
                tmp / "p.md",
                ignore=False,
                worker_pid=worker.pid,
                poll_secs="0.2",
                max_idle_secs="2",
                dispatch_id="watch-task-breadcrumb-fail",
                project_root=project,
                task_ids="t-001",
                agent="codex",
            )
        finally:
            worker.terminate()
            worker.wait()
        assert rc == 4, (rc, payload)
        assert payload["state"] == "blocked_task_breadcrumb", payload
        assert payload["reason"] == "task_breadcrumb_error", payload
        assert payload["task_breadcrumb_failed_state"] == "complete", payload
        assert payload["task_breadcrumb_error"]["type"] in {"JSONDecodeError", "TaskError"}, payload
        assert term.get("kind") == "COMPLETE", term


def case_task_terminal_breadcrumb_happy_path_persists() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        project = tmp / "project"
        _write_task_store(project)
        tail = tmp / "tail.txt"
        tail.write_text("work done\nCOMPLETE: linked task\n", encoding="utf-8")
        worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"], start_new_session=True)
        try:
            rc, _, term, payload = _run_watcher(
                tail,
                tmp / "s.json",
                tmp / "p.md",
                ignore=False,
                worker_pid=worker.pid,
                poll_secs="0.2",
                max_idle_secs="2",
                dispatch_id="watch-task-breadcrumb-ok",
                project_root=project,
                task_ids="t-001",
                agent="codex",
            )
        finally:
            worker.terminate()
            worker.wait()
        assert rc == 0, (rc, payload)
        assert payload["state"] == "complete", payload
        assert "task_breadcrumb_error" not in payload, payload
        assert term.get("kind") == "COMPLETE", term
        dispatches = _read_task(project).get("dispatches", [])
        terminal = [entry for entry in dispatches if entry.get("state") == "worker-finished"]
        assert terminal, dispatches
        assert terminal[-1]["dispatch_id"] == "watch-task-breadcrumb-ok", terminal[-1]
        assert terminal[-1]["last_worker_state"]["state"] == "complete", terminal[-1]


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


def case_worker_dead_accepts_single_prefix_variants_outside_hunk() -> None:
    cases = [
        ("plus", "+COMPLETE: x\npost-marker tail\n"),
        ("plus space", "+ COMPLETE: x\npost-marker tail\n"),
        ("minus", "-COMPLETE: x\npost-marker tail\n"),
        ("minus space", "- COMPLETE: x\npost-marker tail\n"),
        ("quote", "> COMPLETE: x\npost-marker tail\n"),
        ("bold", "**COMPLETE:** x\npost-marker tail\n"),
    ]
    for label, tail_text in cases:
        rc, _elapsed, term, payload = _run_dead_worker_tail(tail_text)
        assert rc == 0, f"{label}: prefixed marker should reconcile, got rc={rc} ({payload})"
        assert payload.get("reason") == "marker:COMPLETE:final_reconciliation", payload
        assert term.get("kind") == "COMPLETE", term
        assert term.get("text") == "x", term


def case_worker_dead_accepts_prefixed_ready_with_trailing_tail() -> None:
    rc, _elapsed, term, payload = _run_dead_worker_tail(
        "Structure check passed: 4 HIGH, 4 MED, 1 LOW, 1 INFO; verdict present.\n"
        "+READY: docs-private/research/2026-06-19-v4-frame-negotiation/review-frame-adversarial.md\n"
        "hook: Stop\n"
        "tokens used\n"
        "123\n"
        "Verified: verdict present, counts inline, final line is the requested `READY:` marker.\n"
    )
    assert rc == 0, f"prefixed READY must reconcile to exit 0, got rc={rc} ({payload})"
    assert payload.get("state") == "complete", payload
    assert payload.get("reason") == "marker:READY:final_reconciliation", payload
    assert term.get("kind") == "READY", term
    assert term.get("text") == (
        "docs-private/research/2026-06-19-v4-frame-negotiation/review-frame-adversarial.md"
    ), term


def case_worker_dead_rejects_prefixed_terminal_inside_diff_hunk() -> None:
    for marker in ("READY", "COMPLETE"):
        rc, _elapsed, term, payload = _run_dead_worker_tail(
            "diff --git a/file.md b/file.md\n"
            "@@ -1 +1 @@\n"
            f"+{marker}: docs-private/research/quoted-from-diff.md\n"
            "worker died before sign-off\n"
        )
        assert rc == 1, f"{marker} inside a real hunk must stay worker_dead, got rc={rc} ({payload})"
        assert payload.get("reason") == "worker_dead_no_terminal_marker", payload
        assert not term, term


def case_plain_ready_last_line_still_works() -> None:
    rc, _elapsed, term, payload = _run_dead_worker_tail(
        "TL;DR: audit done\n"
        "READY: docs-private/research/plain-ready/findings.md\n"
    )
    assert rc == 0, f"plain READY terminal marker regressed, got rc={rc} ({payload})"
    assert payload.get("state") == "complete", payload
    assert payload.get("reason") == "marker:READY", payload
    assert term.get("kind") == "READY", term
    assert term.get("text") == "docs-private/research/plain-ready/findings.md", term


def case_worker_dead_rejects_banner_offset_prompt_echo() -> None:
    prompt = (
        "Do the watcher reconciliation.\n"
        "The final line must be exactly:\n"
        "COMPLETE: gf-fence-offset-fix\n"
        "or BLOCKED: reason.\n"
    )
    rc, _elapsed, term, payload = _run_dead_worker_tail(
        CODEX_BANNER_14
        + prompt
        + "worker started\n"
        + "mcp: context-mode/ctx_execute started\n",
        prompt_text=prompt,
    )
    assert rc == 1, f"banner-offset prompt echo must stay worker_dead, got rc={rc} ({payload})"
    assert payload.get("reason") == "worker_dead_no_terminal_marker", payload
    assert not term, term


def case_worker_dead_accepts_banner_offset_genuine_bare_marker() -> None:
    prompt = (
        "Do the watcher reconciliation.\n"
        "The final line must be exactly:\n"
        "COMPLETE: gf-fence-offset-fix\n"
        "or BLOCKED: reason.\n"
    )
    rc, _elapsed, term, payload = _run_dead_worker_tail(
        CODEX_BANNER_14
        + prompt
        + "worker finished real work\n"
        + "COMPLETE: gf-fence-offset-fix\n"
        + "post-marker summary\n",
        prompt_text=prompt,
    )
    assert rc == 0, f"genuine post-echo marker must reconcile, got rc={rc} ({payload})"
    assert payload.get("state") == "complete", payload
    assert payload.get("reason") == "marker:COMPLETE:final_reconciliation", payload
    assert term.get("kind") == "COMPLETE", term
    assert term.get("text") == "gf-fence-offset-fix", term


def case_worker_dead_accepts_fenceless_final_prompt_quoted_marker() -> None:
    prompt = (
        "Do the watcher reconciliation.\n"
        "Final line of your output MUST be exactly:\n"
        "COMPLETE: gf-fence-offset-fix-r2\n"
    )
    rc, _elapsed, term, payload = _run_dead_worker_tail(
        "grok worker completed review\n"
        "COMPLETE: gf-fence-offset-fix-r2\n",
        prompt_text=prompt,
    )
    assert rc == 0, f"fence-less genuine final marker must complete, got rc={rc} ({payload})"
    assert payload.get("state") == "complete", payload
    assert payload.get("reason") == "marker:COMPLETE", payload
    assert term.get("kind") == "COMPLETE", term
    assert term.get("text") == "gf-fence-offset-fix-r2", term


def case_worker_dead_rejects_fenceless_mid_tail_prompt_quote() -> None:
    prompt = (
        "Do the watcher reconciliation.\n"
        "Final line of your output MUST be exactly:\n"
        "COMPLETE: gf-fence-offset-fix-r2\n"
    )
    rc, _elapsed, term, payload = _run_dead_worker_tail(
        "grok worker quoted its brief\n"
        "COMPLETE: gf-fence-offset-fix-r2\n"
        "worker died before sign-off\n",
        prompt_text=prompt,
    )
    assert rc == 1, f"fence-less mid-tail prompt quote must stay worker_dead, got rc={rc} ({payload})"
    assert payload.get("reason") == "worker_dead_no_terminal_marker", payload
    assert not term, term


def case_worker_dead_early_latch_retries_prompt_anchor() -> None:
    prompt = (
        "Do the watcher reconciliation.\n"
        "The final line must be exactly:\n"
        "COMPLETE: gf-fence-offset-fix-r2\n"
        "or BLOCKED: reason.\n"
    )
    rc, _elapsed, term, payload = _run_dead_worker_tail(
        "Do the watcher reconciliation.\n"
        "narration line happens to match prompt line one, but this is not the prompt echo\n"
        + prompt
        + "worker died before sign-off\n",
        prompt_text=prompt,
    )
    assert rc == 1, f"second prompt anchor must be fenced, got rc={rc} ({payload})"
    assert payload.get("reason") == "worker_dead_no_terminal_marker", payload
    assert not term, term


def case_worker_dead_fenceless_decorated_marker_still_reconciles() -> None:
    prompt = (
        "Do the watcher reconciliation.\n"
        "COMPLETE: quoted-only\n"
    )
    rc, _elapsed, term, payload = _run_dead_worker_tail(
        "tail window starts after the prompt anchor\n"
        "STATUS: COMPLETE: quoted-only\n",
        prompt_text=prompt,
    )
    assert rc == 0, f"fence-less decorated marker should reconcile, got rc={rc} ({payload})"
    assert payload.get("state") == "complete", payload
    assert payload.get("reason") == "marker:COMPLETE:final_reconciliation", payload
    assert term.get("kind") == "COMPLETE", term
    assert term.get("text") == "quoted-only", term


def case_worker_dead_failed_marker_blocks() -> None:
    rc, _elapsed, term, payload = _run_dead_worker_tail("FAILED: x\n")
    assert rc == 4, f"FAILED should map to blocked exit 4, got rc={rc} ({payload})"
    assert payload.get("state") == "blocked", payload
    assert payload.get("reason") == "marker:FAILED:final_reconciliation", payload
    assert term.get("kind") == "FAILED", term
    assert term.get("text") == "x", term


def _prompt_lines(path: Path) -> list[str]:
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
    ]


def case_round4_public_trimmed_tail_final_marker_wins() -> None:
    prompt_path = PUBLIC_ROUND4_FIXTURES / "round4-trimmed-assembled.prompt"
    tail_path = PUBLIC_ROUND4_FIXTURES / "round4-trimmed-tail.txt"
    expected = "public-watch-round4"

    prompt_lines = _prompt_lines(prompt_path)
    tail_text = tail_path.read_text(encoding="utf-8", errors="replace")
    tail_lines = tail_text.splitlines()
    final_line = len(tail_lines)
    echo_marker_line = next(
        idx for idx, line in enumerate(tail_lines, start=1)
        if line == f"COMPLETE: {expected}"
    )

    assert prompt_lines[0] == "You have a steer mailbox at `$GOALFLIGHT_STEER_FILE`."
    assert tail_lines[3] == "Brief task: inspect sanitized watcher output."
    assert prompt_lines[0] != tail_lines[3]
    assert sum(1 for line in tail_lines if line.strip() == "```") == 1
    assert tail_lines[-1] == f"COMPLETE: {expected}"

    with tempfile.TemporaryDirectory() as tmp:
        tail = Path(tmp) / "tail.txt"
        tail.write_text(tail_text, encoding="utf-8")
        prompt_echo_lines, echo_anchor_found, _ = goalflight_watch._prompt_echo_scan(
            tail_lines,
            prompt_lines,
        )
        last = goalflight_watch._last_line_is_terminal_marker(tail, ignore_prefix_lines=prompt_lines)
        final = goalflight_watch._final_terminal_marker(tail, ignore_prefix_lines=prompt_lines)
        markers, _size = goalflight_watch.extract_markers(tail, ignore_prefix_lines=prompt_lines)

    assert echo_anchor_found is True
    assert echo_marker_line - 1 in prompt_echo_lines, prompt_echo_lines
    assert last == {"line": final_line, "kind": "COMPLETE", "text": expected}, last
    assert final == {"line": final_line, "kind": "COMPLETE", "text": expected}, final
    assert markers[-1] == {"line": final_line, "kind": "COMPLETE", "text": expected}, markers[-3:]
    assert all(marker.get("line") != echo_marker_line for marker in markers), markers[:3]


def case_round4_verbatim_tail_final_marker_wins() -> None:
    prompt_path = ROUND4_EVIDENCE / "evidence-assembled.prompt"
    tail_gz_path = ROUND4_EVIDENCE / "evidence-tail-65k.gz"
    if not prompt_path.exists() or not tail_gz_path.exists():
        print("SKIP: round4 private evidence fixture absent")
        return

    prompt_lines = _prompt_lines(prompt_path)
    with tempfile.TemporaryDirectory() as tmp:
        tail = Path(tmp) / "tail.txt"
        with gzip.open(tail_gz_path, "rt", errors="replace") as src:
            tail.write_text(src.read(), encoding="utf-8")

        last = goalflight_watch._last_line_is_terminal_marker(tail, ignore_prefix_lines=prompt_lines)
        final = goalflight_watch._final_terminal_marker(tail, ignore_prefix_lines=prompt_lines)
        markers, _size = goalflight_watch.extract_markers(tail, ignore_prefix_lines=prompt_lines)

    expected = "gf-capacity-queue-parity-r2"
    assert last == {"line": 65073, "kind": "COMPLETE", "text": expected}, last
    assert final == {"line": 65073, "kind": "COMPLETE", "text": expected}, final
    assert markers[-1] == {"line": 65073, "kind": "COMPLETE", "text": expected}, markers[-3:]
    assert all(marker.get("line") != 61 for marker in markers), markers[:3]


def case_round4_second_verbatim_tail_final_marker_wins() -> None:
    tail_gz_path = ROUND4_EVIDENCE / "evidence2-cwd-droppings-tail.gz"
    if not tail_gz_path.exists():
        print("SKIP: round4 second private evidence fixture absent")
        return

    with tempfile.TemporaryDirectory() as tmp:
        tail = Path(tmp) / "tail.txt"
        with gzip.open(tail_gz_path, "rt", errors="replace") as src:
            tail.write_text(src.read(), encoding="utf-8")

        last = goalflight_watch._last_line_is_terminal_marker(tail)
        final = goalflight_watch._final_terminal_marker(tail)
        markers, _size = goalflight_watch.extract_markers(tail)

    expected = "gf-cwd-droppings"
    assert last == {"line": 10068, "kind": "COMPLETE", "text": expected}, last
    assert final == {"line": 10068, "kind": "COMPLETE", "text": expected}, final
    assert markers[-1] == {"line": 10068, "kind": "COMPLETE", "text": expected}, markers[-3:]


def case_steer_wrapper_prompt_brief_only_echo_anchor() -> None:
    prompt = (
        "You have a steer mailbox at `$GOALFLIGHT_STEER_FILE`.\n"
        "\n"
        "Do the watcher reconciliation.\n"
        "Final line of your output MUST be exactly:\n"
        "COMPLETE: wrapped-brief-only\n"
        "or BLOCKED: reason.\n"
    )
    prompt_lines = [line.strip() for line in prompt.splitlines()]
    brief_echo = (
        "OpenAI Codex v0.137.0\n"
        "--------\n"
        "user\n"
        "Do the watcher reconciliation.\n"
        "Final line of your output MUST be exactly:\n"
        "COMPLETE: wrapped-brief-only\n"
        "or BLOCKED: reason.\n"
        "worker died before sign-off\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        tail = Path(tmp) / "tail.txt"
        tail.write_text(brief_echo, encoding="utf-8")
        prompt_echo_lines, echo_anchor_found, _ = goalflight_watch._prompt_echo_scan(
            brief_echo.splitlines(),
            prompt_lines,
        )
        final = goalflight_watch._final_terminal_marker(tail, ignore_prefix_lines=prompt_lines)

    assert echo_anchor_found is True
    assert 5 in prompt_echo_lines, prompt_echo_lines
    assert final is None, final

    with tempfile.TemporaryDirectory() as tmp:
        tail = Path(tmp) / "tail.txt"
        tail.write_text(
            brief_echo + "real work finished\nCOMPLETE: wrapped-brief-only\n",
            encoding="utf-8",
        )
        last = goalflight_watch._last_line_is_terminal_marker(tail, ignore_prefix_lines=prompt_lines)
        final = goalflight_watch._final_terminal_marker(tail, ignore_prefix_lines=prompt_lines)

    assert last and last.get("text") == "wrapped-brief-only", last
    assert final and final.get("text") == "wrapped-brief-only", final


def case_unbalanced_fence_cannot_blind_final_marker() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tail = Path(tmp) / "tail.txt"
        tail.write_text(
            "work started\n"
            "    ~~~~^^\n"
            "traceback underline left the scanner in a fence-like state\n"
            "COMPLETE: unbalanced-final\n",
            encoding="utf-8",
        )
        last = goalflight_watch._last_line_is_terminal_marker(tail)
        final = goalflight_watch._final_terminal_marker(tail)
        markers, _size = goalflight_watch.extract_markers(tail)

    assert last == {"line": 4, "kind": "COMPLETE", "text": "unbalanced-final"}, last
    assert final == {"line": 4, "kind": "COMPLETE", "text": "unbalanced-final"}, final
    assert markers[-1] == {"line": 4, "kind": "COMPLETE", "text": "unbalanced-final"}, markers


def case_balanced_fence_marker_still_suppressed() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tail = Path(tmp) / "tail.txt"
        tail.write_text(
            "worker quoted an example\n"
            "```\n"
            "COMPLETE: fenced-only\n"
            "```\n"
            "worker died before sign-off\n",
            encoding="utf-8",
        )
        last = goalflight_watch._last_line_is_terminal_marker(tail)
        final = goalflight_watch._final_terminal_marker(tail)
        markers, _size = goalflight_watch.extract_markers(tail)

    assert last is None, last
    assert final is None, final
    assert all(marker.get("text") != "fenced-only" for marker in markers), markers


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
    case_task_terminal_breadcrumb_failure_blocks_completion()
    case_task_terminal_breadcrumb_happy_path_persists()
    case_worker_dead_final_reconciliation_observed_shapes()
    case_worker_dead_final_reconciliation_rejects_diff_and_prompt_echo()
    case_worker_dead_accepts_single_prefix_variants_outside_hunk()
    case_worker_dead_accepts_prefixed_ready_with_trailing_tail()
    case_worker_dead_rejects_prefixed_terminal_inside_diff_hunk()
    case_plain_ready_last_line_still_works()
    case_worker_dead_rejects_banner_offset_prompt_echo()
    case_worker_dead_accepts_banner_offset_genuine_bare_marker()
    case_worker_dead_accepts_fenceless_final_prompt_quoted_marker()
    case_worker_dead_rejects_fenceless_mid_tail_prompt_quote()
    case_worker_dead_early_latch_retries_prompt_anchor()
    case_worker_dead_fenceless_decorated_marker_still_reconciles()
    case_worker_dead_failed_marker_blocks()
    case_round4_public_trimmed_tail_final_marker_wins()
    case_round4_verbatim_tail_final_marker_wins()
    case_round4_second_verbatim_tail_final_marker_wins()
    case_steer_wrapper_prompt_brief_only_echo_anchor()
    case_unbalanced_fence_cannot_blind_final_marker()
    case_balanced_fence_marker_still_suppressed()
    print("OK: goalflight_watch prompt-echo guard tests pass")


if __name__ == "__main__":
    main()
