"""Generic bash-tail session runner for orchestrator behavior harnesses."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from common import append_tail, monotonic_elapsed, run_bash_tail_watch


WorkerSpawn = Callable[[Path, Path, Path], subprocess.Popen[Any]]


def run_bash_tail_session(
    *,
    project_root: Path,
    prompt_text: str,
    agent_label: str,
    session_id: str,
    spawn_worker: WorkerSpawn,
    timeout: float = 300.0,
) -> dict[str, Any]:
    started = time.time()
    if not shutil.which("bash"):
        return {"ok": False, "error": "bash missing"}
    tail_path = Path(tempfile.mktemp(prefix=f"{session_id}-tail-", suffix=".txt"))
    prompt_path = Path(tempfile.mktemp(prefix=f"{session_id}-prompt-", suffix=".md"))
    pidfile_dir = Path(tempfile.mkdtemp(prefix="goal-flight-controller-pids-"))
    prompt_path.write_text(prompt_text.strip() + "\n", encoding="utf-8")
    append_tail(tail_path, f"STATUS: {agent_label} controller harness starting")

    worker_proc = spawn_worker(project_root, prompt_path, tail_path)
    watch = run_bash_tail_watch(
        worker_proc=worker_proc,
        tail_path=tail_path,
        agent_label=agent_label,
        session_id=session_id,
        timeout=timeout,
        pidfile_dir=pidfile_dir,
    )

    tail_text = watch.get("tail_text") or ""
    if watch.get("worker_returncode") == 0 and not watch.get("complete_marker") and not watch.get("blocked_marker"):
        append_tail(tail_path, "COMPLETE: true")
        tail_text = tail_path.read_text(encoding="utf-8")
        watch["complete_marker"] = True
        watch["ok"] = watch.get("watcher_returncode") == 0

    watch["elapsed_s"] = monotonic_elapsed(started)
    watch["prompt_path"] = str(prompt_path)
    try:
        tail_path.unlink(missing_ok=True)
        prompt_path.unlink(missing_ok=True)
        shutil.rmtree(pidfile_dir, ignore_errors=True)
    except OSError:
        pass
    return watch
