#!/usr/bin/env python3
"""Codex controller bash-tail worker for Goal Flight harness tests.

Spawns ``codex exec`` with stdin from a prompt file and streams stdout/stderr
into the tail file for ``watch-dispatch-tail.sh``.

See ``protocols/legacy/bash-tail.md`` for the canonical codex headless recipe.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

HOST_DIR = Path(__file__).resolve().parent
CONTROLLER_DIR = HOST_DIR.parent / "controller"
sys.path.insert(0, str(CONTROLLER_DIR))

from common import append_tail  # noqa: E402


def spawn_codex_worker(project_root: Path, prompt_file: Path, tail_path: Path) -> subprocess.Popen[str]:
    append_tail(tail_path, "STATUS: spawning codex exec")
    tail_handle = tail_path.open("a", encoding="utf-8")
    cmd = [
        "codex",
        "exec",
        "--skip-git-repo-check",
        "--sandbox",
        "workspace-write",
        "-c",
        "approval_policy=never",
        "-C",
        str(project_root),
    ]
    return subprocess.Popen(
        cmd,
        stdin=prompt_file.open("r", encoding="utf-8"),
        stdout=tail_handle,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=str(project_root),
    )


def run_codex_bash_tail(
    *,
    project_root: Path,
    prompt_text: str,
    session_id: str = "codex-controller-harness",
    timeout: float = 300.0,
) -> dict:
    from bash_tail_runner import run_bash_tail_session  # noqa: WPS433

    return run_bash_tail_session(
        project_root=project_root,
        prompt_text=prompt_text,
        agent_label="codex-bash-tail",
        session_id=session_id,
        spawn_worker=spawn_codex_worker,
        timeout=timeout,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Codex bash-tail controller harness worker")
    parser.add_argument("--directory", "-C", required=True, help="Project root")
    parser.add_argument("--prompt-file", type=Path, required=True)
    parser.add_argument("--tail", type=Path, required=True)
    args = parser.parse_args()

    project_root = Path(args.directory).resolve()
    prompt_file = args.prompt_file.resolve()
    tail_path = args.tail.resolve()
    proc = spawn_codex_worker(project_root, prompt_file, tail_path)
    return proc.wait()


if __name__ == "__main__":
    raise SystemExit(main())
