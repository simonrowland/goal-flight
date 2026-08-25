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
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
WATCH = ROOT / "scripts" / "goalflight_watch.py"
ROUND4_EVIDENCE = ROOT / "docs-private" / "research" / "gf-bug-watcher-round4"
PUBLIC_ROUND4_FIXTURES = ROOT / "tests" / "fixtures" / "watch_prompt_echo"
sys.path.insert(0, str(SCRIPTS))

import goalflight_watch  # noqa: E402
import goalflight_dispatch  # noqa: E402
import goalflight_terminal  # noqa: E402


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


def _watcher_command(
    *,
    tail: Path,
    status: Path,
    worker_pid: int,
    dispatch_id: str,
    poll_secs: str,
    max_idle_secs: str,
) -> list[str]:
    return [
        sys.executable,
        str(WATCH),
        "--pid",
        str(worker_pid),
        "--tail",
        str(tail),
        "--status-json",
        str(status),
        "--dispatch-id",
        dispatch_id,
        "--poll-secs",
        poll_secs,
        "--max-idle-secs",
        max_idle_secs,
        "--stay-after-terminal",
    ]


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
    if not dispatch_id:
        # These fixtures predate dispatch-bound markers. Infer only the expected
        # id from their parser-valid terminal payload; the watcher still applies
        # prompt-echo/fence/position validation independently.
        candidate = goalflight_watch._final_terminal_marker(tail)
        candidate_text = str((candidate or {}).get("text") or "").strip()
        candidate_id = candidate_text.split(maxsplit=1)[0] if candidate_text else ""
        dispatch_id = (
            candidate_id
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", candidate_id)
            else "watch-prompt-no-terminal"
        )
    cmd = _watcher_command(
        tail=tail,
        status=status,
        worker_pid=worker_pid,
        dispatch_id=dispatch_id,
        poll_secs=poll_secs,
        max_idle_secs=max_idle_secs,
    )
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
        # Production recovery has either an existing status or a live runs.d
        # record. Most legacy fixtures predate the ledger, so preserve the
        # existing-status half of that startup contract.
        if not status.exists():
            status.write_text("{}\n", encoding="utf-8")
    env = _watcher_env(status)
    t0 = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=40, env=env)
    elapsed = time.time() - t0
    payload = {}
    term = {}
    if status.exists():
        payload = json.loads(status.read_text(encoding="utf-8"))
        term = (payload.get("terminal_marker") or {})
    return proc.returncode, elapsed, term, payload


def _watcher_env(status: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["GOALFLIGHT_TEST_MODE"] = "1"
    env["GOALFLIGHT_TEST_PGROUP_CPU_PCT"] = "0.0"
    env["GOALFLIGHT_STATE_DIR"] = str(status.parent / "state")
    env["GOALFLIGHT_TASK_STORE_DIR"] = str(status.parent / "task-store")
    env["GOALFLIGHT_JOURNAL_DIR"] = str(status.parent / "journal")
    env["GOALFLIGHT_MESSAGES_DIR"] = str(status.parent / "messages")
    env["GOALFLIGHT_WAKE_LEDGER_DIR"] = str(status.parent / "wake-ledger")
    env["GOAL_FLIGHT_PIDFILE_DIR"] = str(status.parent / "pids")
    return env


def _wait_for_status(status: Path, timeout_s: float = 2.0) -> dict:
    deadline = time.monotonic() + timeout_s
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if status.exists():
            try:
                return json.loads(status.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                last_error = exc
        time.sleep(0.05)
    detail = f"; last error: {last_error}" if last_error else ""
    raise AssertionError(f"status was not readable within {timeout_s:.1f}s{detail}")


def _wait_for_status_matching(
    status: Path,
    predicate,
    *,
    timeout_s: float = 30.0,
) -> dict:
    deadline = time.monotonic() + timeout_s
    last_payload: dict = {}
    while time.monotonic() < deadline:
        if status.exists():
            try:
                last_payload = json.loads(status.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
            else:
                if predicate(last_payload):
                    return last_payload
        time.sleep(0.05)
    raise AssertionError(
        f"watcher status did not reach expected state within {timeout_s:.1f}s: "
        f"{last_payload}"
    )


def _run_live_veto_scenario(
    *,
    tail: Path,
    status: Path,
    dispatch_id: str,
    final_marker: str,
) -> tuple[dict, str]:
    """Hold the worker until the live-growth veto is positively observed."""

    hold = status.with_suffix(".hold")
    hold.write_text("hold\n", encoding="utf-8")
    worker_code = (
        "from pathlib import Path\n"
        "import sys, time\n"
        "hold = Path(sys.argv[1])\n"
        "while hold.exists():\n"
        "    time.sleep(0.05)\n"
        "print(sys.argv[2], flush=True)\n"
    )
    sink = tail.open("ab")
    try:
        worker = subprocess.Popen(
            [sys.executable, "-c", worker_code, str(hold), final_marker],
            stdout=sink,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        sink.close()

    watcher = subprocess.Popen(
        _watcher_command(
            tail=tail,
            status=status,
            worker_pid=worker.pid,
            dispatch_id=dispatch_id,
            poll_secs="0.1",
            max_idle_secs="30",
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=_watcher_env(status),
    )
    output_lines: list[str] = []
    discard_seen = threading.Event()

    def collect_output() -> None:
        assert watcher.stdout is not None
        for line in watcher.stdout:
            output_lines.append(line)
            if line.startswith("WATCHER-DISCARD "):
                discard_seen.set()

    reader = threading.Thread(target=collect_output)
    reader.start()
    try:
        pending = _wait_for_status_matching(
            status,
            lambda payload: (
                payload.get("state") == "running_after_terminal"
                and payload.get("worker_alive") is True
            ),
        )
        assert pending.get("terminal_marker", {}).get("kind") in {"COMPLETE", "READY"}
        with tail.open("a", encoding="utf-8") as stream:
            stream.write("worker continued after provisional terminal evidence\n")
        assert discard_seen.wait(timeout=30), "live-growth veto did not emit WATCHER-DISCARD"
        assert worker.poll() is None, "worker died before the live-veto branch was observed"

        # Releasing the hold is the only way the worker can emit the scenario's
        # final marker, so WATCHER-DISCARD is causally before the final verdict.
        hold.unlink()
        worker.wait(timeout=30)
        watcher.wait(timeout=30)
        reader.join(timeout=2)
        assert not reader.is_alive(), "watcher output reader did not finish"
    finally:
        hold.unlink(missing_ok=True)
        if worker.poll() is None:
            worker.terminate()
            worker.wait(timeout=5)
        if watcher.poll() is None:
            watcher.terminate()
            watcher.wait(timeout=5)
        reader.join(timeout=2)

    output = "".join(output_lines)
    payload = json.loads(status.read_text(encoding="utf-8"))
    assert watcher.returncode == 0, (watcher.returncode, output, payload)
    assert payload.get("state") == "complete", payload
    assert "WATCHER-DISCARD " in output, output
    assert output.index("WATCHER-DISCARD ") < output.rfind('"state": "complete"'), output
    return payload, output


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
        reaper = threading.Thread(target=worker.wait)
        reaper.start()
        try:
            rc, elapsed, term, _ = _run_watcher(
                tail,
                tmp / "s.json",
                prompt,
                ignore=True,
                worker_pid=worker.pid,
                dispatch_id="PLACEHOLDER",
            )
        finally:
            reaper.join(timeout=5)
            assert not reaper.is_alive(), "worker was not reaped after its real terminal exit"
        assert rc == 0, f"expected exit 0 (complete), got {rc}"
        assert term.get("text") == "PLACEHOLDER", f"must complete on the REAL marker, got {term}"
        assert elapsed > 1.5, f"must wait past the echoed prompt, elapsed={elapsed:.1f}s (false-completed?)"


def case_without_ignore_accepts_echo_only_after_live_worker_exit() -> None:
    # Control: WITHOUT the prompt guard, the echo remains valid evidence, but a
    # live worker keeps the success candidate pending through the exit grace.
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        prompt = tmp / "prompt.md"
        prompt.write_text("COMPLETE: PLACEHOLDER\n", encoding="utf-8")
        tail = tmp / "tail.txt"
        tail.write_text("COMPLETE: PLACEHOLDER\n", encoding="utf-8")  # only the echo so far
        payload, _output = _run_live_veto_scenario(
            tail=tail,
            status=tmp / "s.json",
            dispatch_id="PLACEHOLDER",
            final_marker="COMPLETE: PLACEHOLDER",
        )
        term = payload.get("terminal_marker") or {}
        assert term.get("text") == "PLACEHOLDER", f"control should trip on the echo, got {term}"


def case_prompt_ignore_stops_at_first_mismatch() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        prompt = tmp / "prompt.md"
        prompt.write_text("Do the review.\nCOMPLETE: PLACEHOLDER\n", encoding="utf-8")
        tail = tmp / "tail.txt"
        tail.write_text("Different first line.\nCOMPLETE: PLACEHOLDER\n", encoding="utf-8")
        worker = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
        worker.wait()
        rc, elapsed, term, _ = _run_watcher(
            tail,
            tmp / "s.json",
            prompt,
            ignore=True,
            worker_pid=worker.pid,
            max_idle_secs="3",
        )
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


def case_exec_comm_change_with_same_lstart_is_alive() -> None:
    # The launcher records its Python identity before execing the worker CLI.
    # exec preserves pid+lstart while legitimately replacing comm.
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

    assert is_alive is True, current
    assert reason == "live", reason


def case_missing_lstart_matching_comm_is_inconclusive_alive() -> None:
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
    assert reason == "identity_inconclusive_missing_expected_current_lstart", reason


def case_missing_lstart_unrelated_comm_is_inconclusive_alive() -> None:
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

    assert is_alive is True, current
    assert reason == "identity_inconclusive_missing_expected_current_lstart", reason


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
                poll_secs="0.2",
                max_idle_secs="1",
            )
        finally:
            worker.terminate()
            worker.wait()
        assert rc == 1, f"idle live worker with terminal evidence must be inconclusive, got rc={rc}"
        assert term.get("text") == "identity stayed fail-safe", term
        assert payload.get("worker_alive") is True, payload
        assert payload.get("state") == "inconclusive_timeout", payload
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
        payload, _output = _run_live_veto_scenario(
            tail=tail,
            status=tmp / "s.json",
            dispatch_id="genuine-payload",
            final_marker="COMPLETE: genuine-payload",
        )
        term = payload.get("terminal_marker") or {}
        assert term.get("kind") == "COMPLETE", term
        assert term.get("text") == "genuine-payload", term


def case_live_failed_marker_blocks_not_rate_limited() -> None:
    """Live path must recognize FAILED (shared marker set) before idle_timeout."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        tail = tmp / "tail.txt"
        tail.write_text(
            "upstream validation failed\n"
            "FAILED: upstream returned rate limit while validating user input\n",
            encoding="utf-8",
        )
        worker = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            start_new_session=True,
        )
        try:
            rc, elapsed, term, payload = _run_watcher(
                tail,
                tmp / "s.json",
                tmp / "p.md",
                ignore=False,
                worker_pid=worker.pid,
                poll_secs="0.2",
                max_idle_secs="3",
            )
        finally:
            worker.terminate()
            worker.wait()
        assert rc == 4, f"FAILED live marker must block (rc=4), got rc={rc} ({payload})"
        assert payload.get("state") == "blocked", payload
        assert payload.get("liveness_state") == "blocked", payload
        assert payload.get("reason") != "dispatch_worker_rate_limited", payload
        assert not isinstance(payload.get("reason"), dict), payload
        assert term.get("kind") == "FAILED", term
        assert elapsed < 3.0, f"FAILED must terminate before idle_timeout, elapsed={elapsed}"


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
            "READY: ready-terminal — docs-private/research/2026-06-03-audit/findings.md\n",
            encoding="utf-8",
        )
        payload, _output = _run_live_veto_scenario(
            tail=tail,
            status=tmp / "s.json",
            dispatch_id="ready-terminal",
            final_marker=(
                "READY: ready-terminal — "
                "docs-private/research/2026-06-03-audit/findings.md"
            ),
        )
        term = payload.get("terminal_marker") or {}
        assert term.get("kind") == "READY", term
        assert "findings.md" in term.get("text", ""), term


def test_alive_growing_terminal_candidate_is_discarded_then_final_completes() -> None:
    """Constructed tails exercise the watcher decision without consulting ps."""
    dispatch_id = "b-143-constructed"
    with tempfile.TemporaryDirectory() as tmp:
        tail = Path(tmp) / "tail.txt"
        tail.write_text(
            f"!READY: {dispatch_id} — loading the requested files\n",
            encoding="utf-8",
        )
        scanner = goalflight_watch.IncrementalTailScanner(
            tail,
            expected_dispatch_id=dispatch_id,
        )
        premature = scanner.scan()
        assert premature.terminal is not None, premature.metrics()
        assert premature.terminal["kind"] == "READY", premature.terminal

        with tail.open("a", encoding="utf-8") as stream:
            stream.write("implementation still running\ntests are producing output\n")
        growing = scanner.scan()
        assert growing.size > premature.size
        assert growing.terminal is None
        assert goalflight_watch._post_terminal_candidate_action(
            worker_alive=True,
            tail_grew=growing.size > premature.size,
            grace_expired=True,
            idle_confirmed=False,
        ) == "discard"

        with tail.open("a", encoding="utf-8") as stream:
            stream.write(f"!COMPLETE: {dispatch_id} — implementation and tests done\n")
        final = scanner.scan()
        assert final.terminal is not None, final.metrics()
        assert final.terminal["kind"] == "COMPLETE", final.terminal
        assert goalflight_watch._post_terminal_candidate_action(
            worker_alive=False,
            tail_grew=False,
            grace_expired=False,
            idle_confirmed=False,
        ) == "terminalize"
        assert goalflight_watch._marker_state(final.terminal) == "complete"


def test_scenario_helper_matches_production_stay_after_terminal() -> None:
    cmd = _watcher_command(
        tail=Path("/tmp/isolated-watch/scenario.tail"),
        status=Path("/tmp/isolated-watch/scenario.status.json"),
        worker_pid=4242,
        dispatch_id="scenario-production-parity",
        poll_secs="0.2",
        max_idle_secs="30",
    )
    assert cmd.count("--stay-after-terminal") == 1, cmd


def test_dispatch_watcher_argv_ignores_the_materialized_prompt() -> None:
    """The spawn argv binds prompt-echo filtering to the dispatched brief."""
    prompt = Path("/tmp/isolated-dispatch/b-143.prompt.md")
    argv = goalflight_dispatch._watcher_spawn_argv(
        worker_pid=4242,
        tail=Path("/tmp/isolated-dispatch/b-143.tail"),
        status_json=Path("/tmp/isolated-dispatch/b-143.status.json"),
        agent="codex",
        poll_secs=2.0,
        max_idle_secs=600.0,
        dispatch_id="b-143-dispatch",
        pgid=4242,
        prompt_path=prompt,
    )
    prompt_flag = argv.index("--ignore-prompt-file")
    assert argv[prompt_flag + 1] == str(prompt)


def test_recovery_watcher_reloads_same_mtime_atomic_prompt_replacement() -> None:
    dispatch_id = "reload-steer-prompt"
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        tail = tmp / "tail.txt"
        status = tmp / "status.json"
        prompt = tmp / "assembled.prompt"
        hold = tmp / "worker.hold"
        tail.write_text("worker began\n", encoding="utf-8")
        prompt.write_text("Original task\n", encoding="utf-8")
        status.write_text("{}\n", encoding="utf-8")
        hold.write_text("hold\n", encoding="utf-8")
        initial_prompt_stat = prompt.stat()
        initial_mtime_ns = initial_prompt_stat.st_mtime_ns
        initial_ino = initial_prompt_stat.st_ino
        worker_code = (
            "from pathlib import Path\n"
            "import sys, time\n"
            "hold = Path(sys.argv[1])\n"
            "while hold.exists():\n"
            "    time.sleep(0.05)\n"
            "print(sys.argv[2], flush=True)\n"
        )
        sink = tail.open("ab")
        try:
            worker = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    worker_code,
                    str(hold),
                    f"COMPLETE: {dispatch_id} — genuine final",
                ],
                stdout=sink,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        finally:
            sink.close()

        watcher_cmd = _watcher_command(
            tail=tail,
            status=status,
            worker_pid=worker.pid,
            dispatch_id=dispatch_id,
            poll_secs="0.1",
            max_idle_secs="30",
        ) + ["--ignore-prompt-file", str(prompt)]
        watcher = subprocess.Popen(
            watcher_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=_watcher_env(status),
        )
        try:
            _wait_for_status_matching(
                status,
                lambda payload: payload.get("ignore_prompt_mtime_ns")
                == initial_mtime_ns,
            )
            steer_echo = (
                "Steer turn: inspect the next result\n"
                f"READY: {dispatch_id} — echoed steer, not terminal\n"
            )
            replacement = prompt.with_name(".assembled.prompt.tmp")
            replacement.write_text(
                "Original task\n\n" + steer_echo,
                encoding="utf-8",
            )
            os.utime(replacement, ns=(initial_mtime_ns, initial_mtime_ns))
            replacement.replace(prompt)
            refreshed_prompt_stat = prompt.stat()
            refreshed_mtime_ns = refreshed_prompt_stat.st_mtime_ns
            refreshed_ino = refreshed_prompt_stat.st_ino
            assert refreshed_mtime_ns == initial_mtime_ns
            assert refreshed_ino != initial_ino

            # Append immediately after replacement to cover the narrow race in
            # which a poll scanned the new echo under the old exclusions.
            with tail.open("a", encoding="utf-8") as stream:
                stream.write(steer_echo)
            echo_size = tail.stat().st_size
            after_echo = _wait_for_status_matching(
                status,
                lambda payload: (
                    payload.get("ignore_prompt_mtime_ns") == refreshed_mtime_ns
                    and payload.get("ignore_prompt_signature", {}).get("ino")
                    == refreshed_ino
                    and payload.get("tail_scan", {}).get("offset", -1)
                    >= echo_size
                ),
            )
            assert after_echo.get("terminal_marker") in (None, {}), after_echo
            assert after_echo.get("state") != "running_after_terminal", after_echo

            hold.unlink()
            worker.wait(timeout=10)
            stdout, _stderr = watcher.communicate(timeout=10)
        finally:
            hold.unlink(missing_ok=True)
            if worker.poll() is None:
                worker.terminate()
                worker.wait(timeout=5)
            if watcher.poll() is None:
                watcher.terminate()
                stdout, _stderr = watcher.communicate(timeout=5)

        payload = json.loads(status.read_text(encoding="utf-8"))
        assert watcher.returncode == 0, (watcher.returncode, stdout, payload)
        assert payload.get("state") == "complete", payload
        assert payload.get("terminal_marker", {}).get("kind") == "COMPLETE", payload
        assert "WATCHER-DISCARD " not in stdout, stdout


def test_prompt_reload_coalesces_changes_within_one_poll_interval() -> None:
    loaded = (100, 10, 1)
    changed = (100, 20, 2)

    assert not goalflight_watch._prompt_reload_due(
        changed,
        loaded,
        last_reload_at=10.0,
        now=10.19,
        poll_secs=0.2,
    )
    assert goalflight_watch._prompt_reload_due(
        changed,
        loaded,
        last_reload_at=10.0,
        now=10.2,
        poll_secs=0.2,
    )
    assert not goalflight_watch._prompt_reload_due(
        loaded,
        loaded,
        last_reload_at=None,
        now=20.0,
        poll_secs=0.2,
    )


def test_prompt_signature_detects_same_mtime_different_inode() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        first = tmp / "first.prompt"
        second = tmp / "second.prompt"
        first.write_text("same bytes\n", encoding="utf-8")
        second.write_text("same bytes\n", encoding="utf-8")
        shared_mtime_ns = first.stat().st_mtime_ns
        os.utime(second, ns=(shared_mtime_ns, shared_mtime_ns))

        first_signature = goalflight_watch._prompt_file_signature(first.stat())
        second_signature = goalflight_watch._prompt_file_signature(second.stat())

    assert first_signature[:2] == second_signature[:2]
    assert first_signature[2] != second_signature[2]
    assert first_signature != second_signature


def test_discarded_candidate_identity_requires_same_marker_and_offset() -> None:
    marker = {"line": 7, "kind": "COMPLETE", "text": "veto-id — candidate"}
    evidence = {"marker": dict(marker), "offset": 321}

    assert goalflight_watch._discarded_terminal_candidate_matches(
        evidence,
        marker,
        321,
    )
    assert not goalflight_watch._discarded_terminal_candidate_matches(
        evidence,
        marker,
        322,
    )
    assert not goalflight_watch._discarded_terminal_candidate_matches(
        evidence,
        {**marker, "text": "veto-id — fresh"},
        321,
    )


def test_prompt_reload_does_not_resurrect_same_offset_vetoed_candidate() -> None:
    dispatch_id = "reload-veto"
    marker_line = f"COMPLETE: {dispatch_id} — same marker"

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        tail = tmp / "tail.txt"
        status = tmp / "status.json"
        prompt = tmp / "assembled.prompt"
        hold = tmp / "worker.hold"
        tail.write_text(marker_line + "\n", encoding="utf-8")
        status.write_text("{}\n", encoding="utf-8")
        prompt.write_text("Original task\n", encoding="utf-8")
        hold.write_text("hold\n", encoding="utf-8")
        initial_marker_offset = len((marker_line + "\n").encode("utf-8"))

        worker_code = (
            "from pathlib import Path\n"
            "import sys, time\n"
            "hold = Path(sys.argv[1])\n"
            "while hold.exists():\n"
            "    time.sleep(0.05)\n"
            "print(sys.argv[2], flush=True)\n"
        )
        sink = tail.open("ab")
        try:
            worker = subprocess.Popen(
                [sys.executable, "-c", worker_code, str(hold), marker_line],
                stdout=sink,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        finally:
            sink.close()

        watcher = subprocess.Popen(
            _watcher_command(
                tail=tail,
                status=status,
                worker_pid=worker.pid,
                dispatch_id=dispatch_id,
                poll_secs="0.1",
                max_idle_secs="30",
            )
            + ["--ignore-prompt-file", str(prompt)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=_watcher_env(status),
        )
        output_lines: list[str] = []
        discard_seen = threading.Event()

        def collect_output() -> None:
            assert watcher.stdout is not None
            for line in watcher.stdout:
                output_lines.append(line)
                if line.startswith("WATCHER-DISCARD "):
                    discard_seen.set()

        reader = threading.Thread(target=collect_output)
        reader.start()
        try:
            _wait_for_status_matching(
                status,
                lambda payload: payload.get("state") == "running_after_terminal",
            )
            # Blank growth disproves the live candidate without replacing it as
            # the scanner's last nonempty line.
            with tail.open("a", encoding="utf-8") as stream:
                stream.write("\n")
            assert discard_seen.wait(timeout=30), "candidate was not vetoed"

            replacement = prompt.with_name(".assembled.prompt.tmp")
            replacement.write_text("Original task\n\nUnrelated steer\n", encoding="utf-8")
            replacement.replace(prompt)
            refreshed_ino = prompt.stat().st_ino
            retained = _wait_for_status_matching(
                status,
                lambda payload: (
                    payload.get("ignore_prompt_signature", {}).get("ino")
                    == refreshed_ino
                    and payload.get("replayed_discarded_terminal_evidence") is True
                ),
            )
            evidence = retained.get("last_discarded_terminal_evidence") or {}
            assert evidence.get("offset") == initial_marker_offset, retained
            assert evidence.get("marker", {}).get("text") == (
                f"{dispatch_id} — same marker"
            ), retained
            assert retained.get("terminal_marker") in (None, {}), retained
            assert retained.get("state") != "running_after_terminal", retained

            # The same marker genuinely re-emitted at a later offset is fresh.
            hold.unlink()
            worker.wait(timeout=10)
            watcher.wait(timeout=10)
            reader.join(timeout=2)
        finally:
            hold.unlink(missing_ok=True)
            if worker.poll() is None:
                worker.terminate()
                worker.wait(timeout=5)
            if watcher.poll() is None:
                watcher.terminate()
                watcher.wait(timeout=5)
            reader.join(timeout=2)

        payload = json.loads(status.read_text(encoding="utf-8"))
        assert watcher.returncode == 0, (watcher.returncode, "".join(output_lines), payload)
        assert payload.get("state") == "complete", payload
        assert payload.get("terminal_marker", {}).get("text") == (
            f"{dispatch_id} — same marker"
        ), payload


def case_task_breadcrumb_missing_item_keeps_worker_verdict() -> None:
    """A task id absent from the store must not rewrite a finished run as blocked.

    Live failure: a worker completed, emitted its terminal marker and staged a
    298-line test file. The breadcrumb append then failed with
    "item not found: t-482" -- the dispatch referenced a task this repo's store
    did not have -- and the state flipped from `complete` to
    `blocked_task_breadcrumb`. The run read as blocked and was hand-salvaged for
    work that was already done and already detected.

    The store here is intact; only the referenced id is absent. Contrast
    case_task_terminal_breadcrumb_failure_blocks_completion, where the store
    itself is corrupt and blocking is still correct.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        project = tmp / "project"
        _write_task_store(project)  # valid store, contains t-001 only
        tail = tmp / "tail.txt"
        tail.write_text(
            "work done\nCOMPLETE: watch-task-breadcrumb-missing — pinned the claim\n",
            encoding="utf-8",
        )
        worker = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
        worker.wait()
        rc, _, term, payload = _run_watcher(
                tail,
                tmp / "s.json",
                tmp / "p.md",
                ignore=False,
                worker_pid=worker.pid,
                poll_secs="0.2",
                max_idle_secs="2",
                dispatch_id="watch-task-breadcrumb-missing",
                project_root=project,
                task_ids="t-482",  # not in the store
                agent="codex",
            )
        assert term.get("kind") == "COMPLETE", term
        assert payload["state"] == "complete", payload
        assert payload["reason"] != "task_breadcrumb_error", payload
        # The bookkeeping failure is still surfaced, just not as the verdict.
        assert payload["task_breadcrumb_failed"] is True, payload
        assert "item not found" in payload["task_breadcrumb_error"]["message"], payload


def case_task_terminal_breadcrumb_failure_blocks_completion() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        project = tmp / "project"
        _write_task_store(project)
        (project / "docs-private" / "tasks.jsonl").write_text("{bad json\n", encoding="utf-8")
        tail = tmp / "tail.txt"
        tail.write_text(
            "work done\nCOMPLETE: watch-task-breadcrumb-fail — linked task\n",
            encoding="utf-8",
        )
        worker = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
        worker.wait()
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
        tail.write_text(
            "work done\nCOMPLETE: watch-task-breadcrumb-ok — linked task\n",
            encoding="utf-8",
        )
        worker = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
        worker.wait()
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


B054_FALSE_COMPLETE_SANITIZED_TAIL = """Long worker and review jobs require a ledger/status path. Status contract requires heartbeat markers for live workers.

Workers communicate with one-line markers:
- `STATUS:`
- `STEER-ACK:`
- `RESULT:`
- `USER-NEED:`
- `USER-CONFIRM:`
- `BLOCKED:`
- `COMPLETE:`

Details live in `protocols/worker-markers.md`.

ERROR: Selected model is at capacity. Please try a different model.
ERROR: Selected model is at capacity. Please try a different model.
tokens used
179,057
"""

# A REAL tail from the 2026-08-24 network death, with the timestamps, module
# paths and trailing context a worker actually emits.
#
# The previous fixture used the tidied three-line excerpt that appeared in the
# task brief. Bare signature lines like that never occur in practice, and an
# implementation matching them by line EQUALITY passed against them while being
# unable to classify any real tail. Fixture and implementation shared the same
# laundered evidence, so the test confirmed their agreement rather than the
# behaviour. Keep this fixture ugly on purpose.
OBSERVED_NETWORK_DEATH_TAIL = """2026-08-24T23:04:44.838554Z ERROR codex_api::endpoint::responses_websocket: failed to connect to websocket: IO error: failed to lookup address information: nodename nor servname provided, or not known, url: wss://chatgpt.com/backend-api/codex/responses
ERROR: Reconnecting... 2/5
warning: Falling back from WebSockets to HTTPS transport. stream disconnected before completion: failed to lookup address information: nodename nor servname provided
ERROR: Reconnecting... 4/5
ERROR: Reconnecting... 5/5
"""

# A single transient disconnect is NOT a network death: it proves nothing about
# why the worker stopped, and classifying it would silence real failures.
LONE_DISCONNECT_TAIL = (
    "2026-08-24T22:10:02Z ERROR: stream disconnected before completion: retrying\n"
    "ok, resumed\n"
)

# Real Grok tool error shape that archived review tails recovered from.
OBSERVED_RECOVERABLE_TOOL_ERROR_TAIL = (
    "ERROR tool_error: tool_output_error tool_name=read_file\n"
    "review continued after the failed read\n"
)

# Real Grok acceptEdits narration recorded in goalflight_dispatch.py; the write
# never happened, but the prose alone does not prove why.
OBSERVED_NARRATION_ONLY_DEATH_TAIL = (
    "Creating `artifact.txt` with the requested contents.\n"
)

NO_EVIDENCE_WORKER_DEAD_REASON = (
    "worker_dead_no_terminal_marker:death_cause=no_evidence"
)


def _worker_dead_reason(cause: str) -> str:
    return f"worker_dead_no_terminal_marker:death_cause={cause}"


def _nested_worker_dead_reason(payload: dict) -> object:
    reason = payload.get("reason")
    return reason.get("reason") if isinstance(reason, dict) else reason


def _run_dead_worker_tail(
    tail_text: str,
    prompt_text: str = "Do the requested work.\n",
    max_idle_secs: str = "0.2",
    prompt_mode: str = "file",
):
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        prompt = tmp / "prompt.md"
        if prompt_mode == "file":
            prompt.write_text(prompt_text, encoding="utf-8")
        elif prompt_mode == "directory":
            prompt.mkdir()
        elif prompt_mode not in {"missing", "omitted"}:
            raise ValueError(f"unknown prompt mode: {prompt_mode}")
        tail = tmp / "tail.txt"
        tail.write_text(tail_text, encoding="utf-8")
        worker = subprocess.Popen([sys.executable, "-c", ""], start_new_session=True)
        worker.wait()
        return _run_watcher(
            tail,
            tmp / "s.json",
            prompt,
            ignore=prompt_mode != "omitted",
            worker_pid=worker.pid,
            poll_secs="0.05",
            max_idle_secs=max_idle_secs,
        )


def case_dead_pid_fresh_output_vetoes_worker_dead() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        prompt = tmp / "prompt.md"
        prompt.write_text("", encoding="utf-8")
        tail = tmp / "tail.txt"
        tail.write_text("still producing output\n", encoding="utf-8")
        status = tmp / "s.json"
        status.write_text("{}\n", encoding="utf-8")
        worker = subprocess.Popen([sys.executable, "-c", ""], start_new_session=True)
        worker.wait()
        cmd = [
            sys.executable,
            str(WATCH),
            "--pid",
            str(worker.pid),
            "--tail",
            str(tail),
            "--status-json",
            str(status),
            "--dispatch-id",
            "dead-fresh-output",
            "--poll-secs",
            "0.05",
            "--max-idle-secs",
            "3",
            "--ignore-prompt-file",
            str(prompt),
        ]
        env = os.environ.copy()
        env["GOALFLIGHT_STATE_DIR"] = str(tmp / "state")
        env["GOALFLIGHT_TASK_STORE_DIR"] = str(tmp / "task-store")
        env["GOALFLIGHT_JOURNAL_DIR"] = str(tmp / "journal")
        env["GOALFLIGHT_MESSAGES_DIR"] = str(tmp / "messages")
        env["GOALFLIGHT_WAKE_LEDGER_DIR"] = str(tmp / "wake-ledger")
        env["GOAL_FLIGHT_PIDFILE_DIR"] = str(tmp / "pids")
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        try:
            payload = _wait_for_status_matching(
                status,
                lambda candidate: candidate.get("state") == "running",
            )
            assert proc.poll() is None, payload
            assert payload.get("state") == "running", payload
            assert payload.get("liveness_state") == "running_via_output", payload
            assert payload.get("worker_alive") is True, payload
            assert payload.get("reason") == "pid_resolved_dead_output_fresh", payload
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


def case_dead_pid_stale_output_bounds_worker_dead() -> None:
    rc, elapsed, term, payload = _run_dead_worker_tail("still producing output\n", max_idle_secs="0.2")
    assert rc == 1, f"stale output must become worker_dead, got rc={rc} ({payload})"
    assert elapsed < 3.0, f"worker_dead should be bounded by the fresh window, elapsed={elapsed:.1f}s"
    assert payload.get("state") == "worker_dead", payload
    assert payload.get("liveness_state") == "worker_dead", payload
    assert payload.get("reason") == NO_EVIDENCE_WORKER_DEAD_REASON, payload
    assert not term, term


def test_dead_pid_network_tail_surfaces_upstream_network() -> None:
    rc, _elapsed, term, payload = _run_dead_worker_tail(OBSERVED_NETWORK_DEATH_TAIL)
    assert rc == 1, payload
    assert payload.get("state") == "worker_dead", payload
    assert payload.get("liveness_state") == "worker_dead", payload
    assert payload.get("reason") == _worker_dead_reason("upstream_network"), payload
    assert not term, term


def test_real_tail_lines_classify_despite_timestamps_and_context() -> None:
    """A signature is CONTAINED in a real log line, never equal to it.

    The first implementation compared the trailing lines for EQUALITY against
    the bare signature strings, so it could not classify any real tail -- every
    real line carries a timestamp, a module path and trailing context. It passed
    its fixture because the fixture had been tidied down to bare signatures in
    the task brief, so implementation and fixture shared one laundered excerpt
    and the test confirmed their agreement rather than the behaviour.

    This pins the ugly shape directly at the classifier.
    """
    import goalflight_terminal as term_mod

    assert term_mod.classify_worker_death_text(
        OBSERVED_NETWORK_DEATH_TAIL) == "upstream_network"

    # And the discriminator that must survive it: a lone transient disconnect
    # proves nothing about why the worker stopped.
    assert term_mod.classify_worker_death_text(
        LONE_DISCONNECT_TAIL) == term_mod.WORKER_DEATH_CAUSE_NO_EVIDENCE


def test_recovered_network_incident_does_not_classify_later_death() -> None:
    rc, _elapsed, term, payload = _run_dead_worker_tail(
        OBSERVED_NETWORK_DEATH_TAIL
        + "connectivity restored\n"
        + "work continued successfully\n"
        + "fatal: brief validation failed\n"
    )
    assert rc == 1, payload
    assert payload.get("state") == "worker_dead", payload
    assert payload.get("reason") == NO_EVIDENCE_WORKER_DEAD_REASON, payload
    assert not term, term


def test_recoverable_tool_error_tail_surfaces_no_evidence() -> None:
    rc, _elapsed, term, payload = _run_dead_worker_tail(
        OBSERVED_RECOVERABLE_TOOL_ERROR_TAIL
    )
    assert rc == 1, payload
    assert payload.get("state") == "worker_dead", payload
    assert payload.get("reason") == NO_EVIDENCE_WORKER_DEAD_REASON, payload
    assert not term, term


def test_dead_pid_narration_only_tail_surfaces_no_evidence() -> None:
    rc, _elapsed, term, payload = _run_dead_worker_tail(
        OBSERVED_NARRATION_ONLY_DEATH_TAIL
    )
    assert rc == 1, payload
    assert payload.get("state") == "worker_dead", payload
    assert payload.get("liveness_state") == "worker_dead", payload
    assert payload.get("reason") == NO_EVIDENCE_WORKER_DEAD_REASON, payload
    assert not term, term


def test_death_cause_ignores_evidence_echoed_from_prompt() -> None:
    prompt = "Investigate this prior incident:\n" + OBSERVED_NETWORK_DEATH_TAIL
    rc, _elapsed, term, payload = _run_dead_worker_tail(
        prompt + "worker died before sign-off\n",
        prompt_text=prompt,
    )
    assert rc == 1, payload
    assert payload.get("state") == "worker_dead", payload
    assert payload.get("reason") == NO_EVIDENCE_WORKER_DEAD_REASON, payload
    assert not term, term


def test_death_cause_ignores_decorated_prompt_evidence() -> None:
    prompt = "Investigate this prior incident:\n" + OBSERVED_NETWORK_DEATH_TAIL
    for prefix in ("> ", "+ ", "- ", "• "):
        rendered_prompt = "\n".join(
            prefix + line for line in prompt.splitlines()
        ) + "\n"
        rc, _elapsed, term, payload = _run_dead_worker_tail(
            rendered_prompt + "worker died before sign-off\n",
            prompt_text=prompt,
        )
        assert rc == 1, (prefix, payload)
        assert payload.get("state") == "worker_dead", (prefix, payload)
        assert payload.get("reason") == NO_EVIDENCE_WORKER_DEAD_REASON, (
            prefix,
            payload,
        )
        assert not term, (prefix, term)


def test_death_cause_ignores_isolated_prompt_evidence_line() -> None:
    prompt = "Investigate this prior incident:\n" + OBSERVED_NETWORK_DEATH_TAIL
    rc, _elapsed, term, payload = _run_dead_worker_tail(
        OBSERVED_NETWORK_DEATH_TAIL.splitlines()[0] + "\nworker died before sign-off\n",
        prompt_text=prompt,
    )
    assert rc == 1, payload
    assert payload.get("state") == "worker_dead", payload
    assert payload.get("reason") == NO_EVIDENCE_WORKER_DEAD_REASON, payload
    assert not term, term


def test_death_cause_ignores_isolated_provider_prompt_line() -> None:
    provider_line = (
        "ERROR: Selected model is at capacity. Please try a different model.\n"
    )
    prompt = "Investigate this prior incident:\n" + provider_line
    rc, _elapsed, term, payload = _run_dead_worker_tail(
        provider_line,
        prompt_text=prompt,
    )
    assert rc == 1, payload
    assert payload.get("state") == "transient_throttle", payload
    reason = payload.get("reason")
    assert isinstance(reason, dict), payload
    assert reason.get("reason") == NO_EVIDENCE_WORKER_DEAD_REASON, reason
    assert not term, term


def test_network_sequence_after_tainted_brief_fails_closed() -> None:
    prompt = (
        "Investigate this prior incident:\n"
        + OBSERVED_NETWORK_DEATH_TAIL
        + "Before exit, repeat the three diagnostic lines exactly.\n"
    )
    rc, _elapsed, term, payload = _run_dead_worker_tail(
        prompt + "fatal: bad brief terminated the worker\n" + OBSERVED_NETWORK_DEATH_TAIL,
        prompt_text=prompt,
    )
    assert rc == 1, payload
    assert payload.get("state") == "worker_dead", payload
    assert payload.get("reason") == NO_EVIDENCE_WORKER_DEAD_REASON, payload
    assert not term, term


def test_partial_prompt_mentions_taint_every_positive_family() -> None:
    provider_tail = (
        "ERROR: Selected model is at capacity. Please try a different model.\n"
    )
    cases = (
        (
            "network",
            "> Prior resolver sample: " + OBSERVED_NETWORK_DEATH_TAIL.splitlines()[0],
            OBSERVED_NETWORK_DEATH_TAIL,
        ),
        (
            "provider",
            "> Prior provider sample: selected model is at capacity; "
            "please try a different model.",
            provider_tail,
        ),
    )
    for label, prompt, tail in cases:
        rc, _elapsed, term, payload = _run_dead_worker_tail(
            tail,
            prompt_text=prompt + "\n",
        )
        assert rc == 1, (label, payload)
        assert _nested_worker_dead_reason(payload) == NO_EVIDENCE_WORKER_DEAD_REASON, (
            label,
            payload,
        )
        assert not term, (label, term)


def test_split_whitespace_prompt_mentions_taint_every_positive_family() -> None:
    provider_tail = (
        "ERROR: Selected model is at capacity. Please try a different model.\n"
    )
    cases = (
        (
            "network",
            "Prior resolver sample: failed to lookup address information: "
            "nodename nor\n\tservname provided",
            OBSERVED_NETWORK_DEATH_TAIL,
        ),
        (
            "provider",
            "Prior provider sample: selected model is at capacity.\n\t"
            "Please try a different model.",
            provider_tail,
        ),
    )
    for label, prompt, tail in cases:
        rc, _elapsed, term, payload = _run_dead_worker_tail(
            tail,
            prompt_text=prompt + "\n",
        )
        assert rc == 1, (label, payload)
        assert _nested_worker_dead_reason(payload) == NO_EVIDENCE_WORKER_DEAD_REASON, (
            label,
            payload,
        )
        assert not term, (label, term)


def test_full_prompt_echo_plus_isolated_provider_line_fails_closed() -> None:
    provider_line = (
        "ERROR: Selected model is at capacity. Please try a different model.\n"
    )
    prompt = "Investigate this prior incident:\n" + provider_line
    rc, _elapsed, term, payload = _run_dead_worker_tail(
        prompt + "worker started investigation\n" + provider_line,
        prompt_text=prompt,
    )
    assert rc == 1, payload
    assert _nested_worker_dead_reason(payload) == NO_EVIDENCE_WORKER_DEAD_REASON, payload
    assert not term, term


def test_recovered_provider_incident_surfaces_no_evidence() -> None:
    provider_tail = (
        "ERROR: Selected model is at capacity. Please try a different model.\n"
    )
    cases = (
        (
            "provider",
            provider_tail
            + "provider connection recovered\n"
            + "work continued successfully\n"
            + "fatal: unrelated brief validation failed\n",
        ),
    )
    for label, tail in cases:
        rc, _elapsed, term, payload = _run_dead_worker_tail(tail)
        assert rc == 1, (label, payload)
        expected_state = "transient_throttle" if label == "provider" else "worker_dead"
        assert payload.get("state") == expected_state, (label, payload)
        assert _nested_worker_dead_reason(payload) == NO_EVIDENCE_WORKER_DEAD_REASON, (
            label,
            payload,
        )
        assert not term, (label, term)


def test_provider_footer_requires_observed_ordered_pair() -> None:
    provider_line = (
        "ERROR: Selected model is at capacity. Please try a different model.\n"
    )
    for poison_suffix in (
        "42\n",
        "179,057\ntokens used\n",
        "tokens used\n",
        "tokens used\n179,057\ncontinued work\n",
    ):
        cause = goalflight_terminal.classify_worker_death_text(
            provider_line + poison_suffix
        )
        assert cause == "no_evidence", (poison_suffix, cause)

    usage_prefix = "You've hit your usage limit. Please try again at "
    for poison_reset in (
        "arbitrary prose",
        "6:13 AM. Connectivity recovered and work continued.",
        "Jun 21st and then continue",
    ):
        cause = goalflight_terminal.classify_worker_death_text(
            usage_prefix + poison_reset + "\n"
        )
        assert cause == "no_evidence", (poison_reset, cause)


def test_unavailable_prompt_sidecar_fails_death_cause_closed() -> None:
    provider_tail = (
        "ERROR: Selected model is at capacity. Please try a different model.\n"
    )
    cases = (
        ("missing", "Do the requested work.\n"),
        ("directory", "Do the requested work.\n"),
        ("file", ""),
        ("file", " \n\t\n"),
        ("omitted", "Do the requested work.\n"),
    )
    for prompt_mode, prompt_text in cases:
        rc, _elapsed, term, payload = _run_dead_worker_tail(
            provider_tail,
            prompt_text=prompt_text,
            prompt_mode=prompt_mode,
        )
        label = (prompt_mode, repr(prompt_text))
        assert rc == 1, (label, payload)
        assert payload.get("state") == "transient_throttle", (label, payload)
        assert _nested_worker_dead_reason(payload) == NO_EVIDENCE_WORKER_DEAD_REASON, (
            label,
            payload,
        )
        assert not term, (label, term)


def test_second_prompt_replacement_invalidates_death_cause_before_reload() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        prompt = tmp / "prompt.md"
        prompt.write_text("Initial benign prompt.\n", encoding="utf-8")
        initial_signature = goalflight_watch._prompt_file_signature(prompt.stat())
        prompt.write_text("First benign replacement.\n", encoding="utf-8")
        first_signature = goalflight_watch._prompt_file_signature(prompt.stat())
        assert first_signature != initial_signature
        first_lines = prompt.read_text(encoding="utf-8").splitlines()
        prompt.write_text(
            "Second replacement mentions failed to lookup address information: "
            "nodename nor servname provided.\n",
            encoding="utf-8",
        )
        second_signature = goalflight_watch._prompt_file_signature(prompt.stat())
        assert second_signature != first_signature
        provenance_available = goalflight_watch._prompt_provenance_matches_loaded_snapshot(
            second_signature,
            first_signature,
            True,
        )
        tail = tmp / "tail.txt"
        tail.write_text(OBSERVED_NETWORK_DEATH_TAIL, encoding="utf-8")
        reason = goalflight_watch._worker_dead_no_marker_reason(
            tail,
            first_lines,
            prompt_provenance_available=provenance_available,
        )
        assert reason == NO_EVIDENCE_WORKER_DEAD_REASON, reason


def test_prompt_signature_reversion_retrusts_cached_snapshot() -> None:
    loaded_signature = (101, 202, 303)
    changed_signature = (404, 505, 606)
    assert not goalflight_watch._prompt_provenance_matches_loaded_snapshot(
        changed_signature,
        loaded_signature,
        True,
    )
    provenance_available = goalflight_watch._prompt_provenance_matches_loaded_snapshot(
        loaded_signature,
        loaded_signature,
        True,
    )
    assert provenance_available
    with tempfile.TemporaryDirectory() as tmp:
        tail = Path(tmp) / "tail.txt"
        tail.write_text(OBSERVED_NETWORK_DEATH_TAIL, encoding="utf-8")
        reason = goalflight_watch._worker_dead_no_marker_reason(
            tail,
            ["Initial benign prompt."],
            prompt_provenance_available=provenance_available,
        )
    assert reason == _worker_dead_reason("upstream_network"), reason


def test_prompt_change_during_final_tail_snapshot_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        tail = tmp / "tail.txt"
        tail.write_text(OBSERVED_NETWORK_DEATH_TAIL, encoding="utf-8")
        prompt = tmp / "prompt.md"
        prompt.write_text("Initial benign prompt.\n", encoding="utf-8")
        expected_signature = (101, 202, 303)
        changed_signature = (404, 505, 606)
        snapshots = iter(
            (
                (["Initial benign prompt."], expected_signature),
                (["Second prompt mentions the network evidence."], changed_signature),
            )
        )
        original_reader = goalflight_watch._read_prompt_exclusion_snapshot
        goalflight_watch._read_prompt_exclusion_snapshot = lambda _path: next(snapshots)
        try:
            reason = goalflight_watch._worker_dead_no_marker_reason(
                tail,
                ["Initial benign prompt."],
                prompt_provenance_available=True,
                prompt_path=prompt,
                prompt_signature=expected_signature,
            )
        finally:
            goalflight_watch._read_prompt_exclusion_snapshot = original_reader
    assert reason == NO_EVIDENCE_WORKER_DEAD_REASON, reason


def test_restored_same_prompt_signature_reenables_classification() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        prompt = tmp / "prompt.md"
        prompt.write_text("Investigate the worker death.\n", encoding="utf-8")
        original_signature = goalflight_watch._prompt_file_signature(prompt.stat())
        held_prompt = tmp / "prompt.held"
        tail = tmp / "tail.txt"
        tail.write_text("worker started\n", encoding="utf-8")
        status = tmp / "s.json"
        status.write_text("{}\n", encoding="utf-8")
        worker = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
        )
        cmd = _watcher_command(
            tail=tail,
            status=status,
            worker_pid=worker.pid,
            dispatch_id="prompt-provenance-restored",
            poll_secs="0.05",
            max_idle_secs="1",
        ) + ["--ignore-prompt-file", str(prompt)]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=_watcher_env(status),
        )
        try:
            _wait_for_status_matching(status, lambda payload: payload.get("state") == "running")
            prompt.rename(held_prompt)
            time.sleep(0.25)
            held_prompt.rename(prompt)
            assert goalflight_watch._prompt_file_signature(prompt.stat()) == original_signature
            time.sleep(0.25)
            tail.write_text(OBSERVED_NETWORK_DEATH_TAIL, encoding="utf-8")
            worker.terminate()
            worker.wait(timeout=5)
            stdout, stderr = proc.communicate(timeout=5)
            assert proc.returncode == 1, (stdout, stderr)
            payload = json.loads(status.read_text(encoding="utf-8"))
            assert payload.get("state") == "worker_dead", payload
            assert payload.get("reason") == _worker_dead_reason("upstream_network"), payload
        finally:
            if worker.poll() is None:
                worker.terminate()
                worker.wait(timeout=5)
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=5)


def test_ordinary_provider_like_prose_surfaces_no_evidence() -> None:
    rc, _elapsed, term, payload = _run_dead_worker_tail(
        "Updated UI test for copy: try again at 6:13 AM\n"
        + ("ordinary progress\n" * 150)
        + "fatal: brief validation failed\n"
    )
    assert rc == 1, payload
    assert payload.get("state") == "worker_dead", payload
    assert payload.get("reason") == NO_EVIDENCE_WORKER_DEAD_REASON, payload
    assert not term, term


def case_dead_pid_unbound_done_signoff_is_rejected() -> None:
    for tail_text in (
        "DONE.\n",
        "complete\n",
        "FINISHED!\npost-marker summary\n",
    ):
        rc, _elapsed, term, payload = _run_dead_worker_tail(tail_text)
        assert rc == 1, f"{tail_text!r} must not satisfy dispatch identity, got {rc} ({payload})"
        assert payload.get("state") == "worker_dead", payload
        assert payload.get("reason") == NO_EVIDENCE_WORKER_DEAD_REASON, payload
        assert not term, term


def case_dead_pid_usage_limit_without_success_marker_reclassifies() -> None:
    rc, _elapsed, term, payload = _run_dead_worker_tail(
        "You've hit your usage limit. Please try again at 6:13 AM.\n"
    )
    assert rc == 1, f"quota exhaustion must stop, got rc={rc} ({payload})"
    assert payload.get("state") == "quota_exhausted", payload
    assert payload.get("liveness_state") == "quota_exhausted", payload
    reason = payload.get("reason")
    assert isinstance(reason, dict), payload
    assert reason.get("message") == "dispatch_worker_limit_reached", reason
    assert reason.get("limit_kind") == "exhausted", reason
    assert reason.get("rate_limit_signature") == "usage limit", reason
    assert reason.get("reason") == _worker_dead_reason("provider_limit"), reason
    assert "try again at 6:13 AM" in reason.get("tail_excerpt", ""), reason
    assert not term, term


def case_b054_real_evidence_marker_vocab_bullet_reclassifies_rate_limited() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tail = Path(tmp) / "tail.txt"
        tail.write_text(B054_FALSE_COMPLETE_SANITIZED_TAIL, encoding="utf-8")
        final = goalflight_watch._final_terminal_marker(tail)

    assert final is None, final

    rc, _elapsed, term, payload = _run_dead_worker_tail(B054_FALSE_COMPLETE_SANITIZED_TAIL)
    assert rc == 1, f"b-054 specimen must not complete, got rc={rc} ({payload})"
    assert payload.get("state") == "transient_throttle", payload
    assert payload.get("liveness_state") == "transient_throttle", payload
    reason = payload.get("reason")
    assert isinstance(reason, dict), payload
    assert reason.get("message") == "dispatch_worker_limit_reached", reason
    assert reason.get("limit_kind") == "transient", reason
    assert reason.get("rate_limit_signature") == "selected model is at capacity", reason
    assert reason.get("reason") == _worker_dead_reason("provider_limit"), reason
    assert not term, term


def case_b054_error_after_reconciled_marker_vetoes_complete() -> None:
    rc, _elapsed, term, payload = _run_dead_worker_tail(
        "work reported as done before the provider died\n"
        "COMPLETE: dashboard fold, tests green\n"
        "ERROR: Selected model is at capacity. Please try a different model.\n"
        "ERROR: Selected model is at capacity. Please try a different model.\n"
        "tokens used\n"
        "179,057\n"
    )
    assert rc == 1, f"provider error after candidate marker must veto complete, got rc={rc} ({payload})"
    assert payload.get("state") == "transient_throttle", payload
    assert payload.get("liveness_state") == "transient_throttle", payload
    assert term.get("kind") == "COMPLETE", term
    assert term.get("text") == "dashboard fold, tests green", term
    reason = payload.get("reason")
    assert isinstance(reason, dict), payload
    assert reason.get("message") == "dispatch_worker_limit_reached", reason
    assert reason.get("limit_kind") == "transient", reason
    assert reason.get("rate_limit_signature") == "selected model is at capacity", reason
    assert reason.get("reason") == "marker:COMPLETE:final_reconciliation", reason
    assert reason.get("vetoed_terminal_marker") == term, reason


def case_b054_hook_stop_footer_after_complete_does_not_veto() -> None:
    rc, _elapsed, term, payload = _run_dead_worker_tail(
        "COMPLETE: dashboard fold, tests green\n"
        "hook: Stop\n"
        "hook: Stop Completed\n"
        "tokens used\n"
        "42,000\n"
        "Verified: dashboard fold and focused tests passed.\n"
    )
    assert rc == 0, f"healthy hook footer must stay complete, got rc={rc} ({payload})"
    assert payload.get("state") == "complete", payload
    assert payload.get("liveness_state") == "completed", payload
    assert payload.get("reason") == "marker:COMPLETE:final_reconciliation", payload
    assert term.get("kind") == "COMPLETE", term
    assert term.get("text") == "dashboard fold, tests green", term


def case_dead_pid_usage_limit_mentions_with_success_marker_complete() -> None:
    cases = [
        (
            "marker-seen",
            "Summary mentions usage limit, 429, try again at 6:13 AM, rate limit, at capacity.\n"
            "COMPLETE: capped terms documented\n",
            "marker:COMPLETE",
        ),
        (
            "final-reconciliation",
            "READY: capped terms documented\n"
            "Summary mentions usage limit, 429, try again at 6:13 AM, rate limit, at capacity.\n",
            "marker:READY:final_reconciliation",
        ),
    ]
    for label, tail_text, expected_reason in cases:
        rc, _elapsed, term, payload = _run_dead_worker_tail(tail_text)
        assert rc == 0, f"{label}: success marker must complete, got rc={rc} ({payload})"
        assert payload.get("state") == "complete", f"{label}: {payload}"
        assert payload.get("liveness_state") == "completed", f"{label}: {payload}"
        assert payload.get("reason") == expected_reason, f"{label}: {payload}"
        assert term.get("kind") in {"COMPLETE", "READY"}, f"{label}: {term}"


def case_dead_pid_usage_limit_mentions_with_failure_markers_stay_blocked() -> None:
    cases = [
        ("FAILED", "FAILED: upstream returned rate limit while validating user input\n"),
        ("BLOCKED", "BLOCKED: cannot write sandbox path; prose mentions usage limit\n"),
        ("USER-NEED", "USER-NEED: docs API rate limit; use cached data or wait?\n"),
        ("USER-CONFIRM", "USER-CONFIRM: provider at capacity; retry now?\n"),
    ]
    for kind, tail_text in cases:
        rc, _elapsed, term, payload = _run_dead_worker_tail(tail_text)
        assert rc == 4, f"{kind}: deliberate marker must stay blocked, got rc={rc} ({payload})"
        assert payload.get("state") == "blocked", f"{kind}: {payload}"
        assert payload.get("liveness_state") == "blocked", f"{kind}: {payload}"
        assert payload.get("reason") != "dispatch_worker_rate_limited", f"{kind}: {payload}"
        assert not isinstance(payload.get("reason"), dict), f"{kind}: {payload}"
        assert term.get("kind") == kind, f"{kind}: {term}"


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
        assert payload.get("liveness_state") == "completed", f"{label}: {payload}"
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
    assert payload.get("reason") == NO_EVIDENCE_WORKER_DEAD_REASON, payload
    assert not term, term

    negative_cases = [
        ("context line leading space", " STATUS: COMPLETE: x\n"),
        ("hunk deletion indented marker", "@@ -1,1 +1,0 @@\n-    COMPLETE: x\n"),
    ]
    for label, tail_text in negative_cases:
        rc, _elapsed, term, payload = _run_dead_worker_tail(tail_text)
        assert rc == 1, f"{label}: expected worker-dead no-marker exit 1, got rc={rc} ({payload})"
        assert payload.get("reason") == NO_EVIDENCE_WORKER_DEAD_REASON, f"{label}: {payload}"
        assert not term, f"{label}: {term}"

    prompt = "Do the work.\nCOMPLETE: prompt-only\n"
    rc, _elapsed, term, payload = _run_dead_worker_tail(
        prompt + "worker died before sign-off\n",
        prompt_text=prompt,
    )
    assert rc == 1, f"prompt echo only must not complete, got rc={rc} ({payload})"
    assert payload.get("reason") == NO_EVIDENCE_WORKER_DEAD_REASON, payload
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
        "+READY: prefixed-ready — docs-private/research/2026-06-19-v4-frame-negotiation/review-frame-adversarial.md\n"
        "hook: Stop\n"
        "tokens used\n"
        "123\n"
        "Verified: verdict present, counts inline, final line is the requested `READY:` marker.\n"
    )
    assert rc == 0, f"prefixed READY must reconcile to exit 0, got rc={rc} ({payload})"
    assert payload.get("state") == "complete", payload
    assert payload.get("reason") == "marker:READY:final_reconciliation", payload
    assert term.get("kind") == "READY", term
    assert term.get("text", "").endswith(
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
        assert payload.get("reason") == NO_EVIDENCE_WORKER_DEAD_REASON, payload
        assert not term, term


def case_plain_ready_last_line_still_works() -> None:
    rc, _elapsed, term, payload = _run_dead_worker_tail(
        "TL;DR: audit done\n"
        "READY: plain-ready — docs-private/research/plain-ready/findings.md\n"
    )
    assert rc == 0, f"plain READY terminal marker regressed, got rc={rc} ({payload})"
    assert payload.get("state") == "complete", payload
    assert payload.get("reason") == "marker:READY", payload
    assert term.get("kind") == "READY", term
    assert term.get("text", "").endswith("docs-private/research/plain-ready/findings.md"), term


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
    assert payload.get("reason") == NO_EVIDENCE_WORKER_DEAD_REASON, payload
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
    assert payload.get("reason") == NO_EVIDENCE_WORKER_DEAD_REASON, payload
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
    assert payload.get("reason") == NO_EVIDENCE_WORKER_DEAD_REASON, payload
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
    assert payload.get("reason") == "marker:FAILED", payload
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
    case_without_ignore_accepts_echo_only_after_live_worker_exit()
    case_prompt_ignore_stops_at_first_mismatch()
    case_identity_mismatch_not_alive()
    case_matching_lstart_ignores_comm_form_change()
    case_exec_comm_change_with_same_lstart_is_alive()
    case_missing_lstart_matching_comm_is_inconclusive_alive()
    case_missing_lstart_unrelated_comm_is_inconclusive_alive()
    case_incomplete_identity_is_inconclusive_alive()
    case_steer_ack_is_non_terminal_marker()
    case_mid_output_marker_ignored()
    case_live_failed_marker_blocks_not_rate_limited()
    case_ready_terminal_marker()
    test_alive_growing_terminal_candidate_is_discarded_then_final_completes()
    test_scenario_helper_matches_production_stay_after_terminal()
    test_dispatch_watcher_argv_ignores_the_materialized_prompt()
    test_recovery_watcher_reloads_same_mtime_atomic_prompt_replacement()
    test_prompt_reload_coalesces_changes_within_one_poll_interval()
    test_prompt_signature_detects_same_mtime_different_inode()
    test_discarded_candidate_identity_requires_same_marker_and_offset()
    test_prompt_reload_does_not_resurrect_same_offset_vetoed_candidate()
    case_task_breadcrumb_missing_item_keeps_worker_verdict()
    case_task_terminal_breadcrumb_failure_blocks_completion()
    case_task_terminal_breadcrumb_happy_path_persists()
    case_dead_pid_fresh_output_vetoes_worker_dead()
    case_dead_pid_stale_output_bounds_worker_dead()
    test_dead_pid_network_tail_surfaces_upstream_network()
    test_recovered_network_incident_does_not_classify_later_death()
    test_recoverable_tool_error_tail_surfaces_no_evidence()
    test_dead_pid_narration_only_tail_surfaces_no_evidence()
    test_death_cause_ignores_evidence_echoed_from_prompt()
    test_death_cause_ignores_decorated_prompt_evidence()
    test_death_cause_ignores_isolated_prompt_evidence_line()
    test_death_cause_ignores_isolated_provider_prompt_line()
    test_network_sequence_after_tainted_brief_fails_closed()
    test_partial_prompt_mentions_taint_every_positive_family()
    test_split_whitespace_prompt_mentions_taint_every_positive_family()
    test_full_prompt_echo_plus_isolated_provider_line_fails_closed()
    test_recovered_provider_incident_surfaces_no_evidence()
    test_provider_footer_requires_observed_ordered_pair()
    test_unavailable_prompt_sidecar_fails_death_cause_closed()
    test_second_prompt_replacement_invalidates_death_cause_before_reload()
    test_prompt_signature_reversion_retrusts_cached_snapshot()
    test_prompt_change_during_final_tail_snapshot_fails_closed()
    test_restored_same_prompt_signature_reenables_classification()
    test_ordinary_provider_like_prose_surfaces_no_evidence()
    case_dead_pid_unbound_done_signoff_is_rejected()
    case_dead_pid_usage_limit_without_success_marker_reclassifies()
    case_b054_real_evidence_marker_vocab_bullet_reclassifies_rate_limited()
    case_b054_error_after_reconciled_marker_vetoes_complete()
    case_b054_hook_stop_footer_after_complete_does_not_veto()
    case_dead_pid_usage_limit_mentions_with_success_marker_complete()
    case_dead_pid_usage_limit_mentions_with_failure_markers_stay_blocked()
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
