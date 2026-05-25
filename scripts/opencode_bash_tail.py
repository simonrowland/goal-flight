#!/usr/bin/env python3
"""OpenCode bash-tail worker via the HTTP API.

Bare ``opencode run`` can hang in headless environments; this script mirrors
``opencode_prompt.py`` but writes Goal Flight terminal markers to a tail file
for ``watch-dispatch-tail.sh``.

Usage (background from execute.md bash-tail branch):

  python3 scripts/opencode_bash_tail.py \\
    --directory /path/to/repo \\
    --tail /tmp/opencode-<slug>.txt \\
    --prompt-file /tmp/prompt-<slug>.md

The controller backgrounds this process and attaches ``watch-dispatch-tail.sh``
with ``--agent opencode-bash-tail``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from opencode_prompt import (  # noqa: E402
    DEFAULT_MODEL,
    DEFAULT_PORT,
    REPO_ROOT as _PROMPT_REPO_ROOT,
    _load_litellm_env,
    prompt_once,
)


def _write_tail(tail_path: Path, line: str) -> None:
    tail_path.parent.mkdir(parents=True, exist_ok=True)
    with tail_path.open("a", encoding="utf-8") as handle:
        handle.write(line.rstrip() + "\n")
        handle.flush()


def _read_prompt(*, prompt_file: Path | None, prompt_text: str | None) -> str:
    if prompt_text:
        return prompt_text.strip()
    if prompt_file is not None:
        return prompt_file.read_text(encoding="utf-8").strip()
    raise ValueError("one of --prompt-file or --prompt-text is required")


def run_bash_tail(
    *,
    directory: Path,
    tail_path: Path,
    message: str,
    model: str,
    port: int,
    boot_timeout_s: float,
    reply_timeout_s: float,
    log_path: Path,
) -> int:
    _write_tail(tail_path, "STATUS: opencode bash-tail worker starting")
    try:
        _write_tail(tail_path, "STATUS: contacting OpenCode HTTP API")
        reply = prompt_once(
            message,
            directory=directory,
            model=model,
            port=port,
            boot_timeout_s=boot_timeout_s,
            reply_timeout_s=reply_timeout_s,
            keep_server=False,
            log_path=log_path,
        )
    except Exception as exc:
        _write_tail(tail_path, f"BLOCKED: {exc}")
        return 1

    _write_tail(tail_path, "STATUS: model reply received")
    for line in reply.splitlines():
        if line.strip():
            _write_tail(tail_path, line)
    _write_tail(tail_path, "COMPLETE: true")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="OpenCode bash-tail worker (HTTP API + markers)")
    parser.add_argument("--directory", "-C", default=str(_PROMPT_REPO_ROOT), help="Project directory")
    parser.add_argument("--tail", required=True, help="Tail file path (stdout/stderr log for watcher)")
    parser.add_argument("--prompt-file", type=Path, help="Prompt file path")
    parser.add_argument("--prompt-text", help="Inline prompt text")
    parser.add_argument("--model", "-m", default=DEFAULT_MODEL, help=f"provider/model (default: {DEFAULT_MODEL})")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"OpenCode serve port (default: {DEFAULT_PORT})")
    parser.add_argument("--boot-timeout", type=float, default=120.0, help="Seconds to wait for server health")
    parser.add_argument("--timeout", type=float, default=180.0, help="Seconds to wait for model reply")
    parser.add_argument("--log", default=str(Path("/tmp/opencode-serve.log")), help="Serve log when auto-starting")
    args = parser.parse_args()

    _load_litellm_env()
    directory = Path(args.directory).resolve()
    tail_path = Path(args.tail).resolve()
    try:
        message = _read_prompt(prompt_file=args.prompt_file, prompt_text=args.prompt_text)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    return run_bash_tail(
        directory=directory,
        tail_path=tail_path,
        message=message,
        model=args.model,
        port=args.port,
        boot_timeout_s=args.boot_timeout,
        reply_timeout_s=args.timeout,
        log_path=Path(args.log),
    )


if __name__ == "__main__":
    raise SystemExit(main())
