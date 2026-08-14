#!/usr/bin/env python3
"""Claim a prepared Goal Flight attempt as RUNNING, then exec its worker."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import goalflight_journal  # noqa: E402
import goalflight_ledger  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="claim Goal Flight attempt then exec worker")
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--launch-token", required=True)
    parser.add_argument("--launch-epoch", required=True, type=int)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = list(args.command)
    if command and command[0] == "--":
        command.pop(0)
    if not command:
        parser.error("worker command is required after --")
    authority = goalflight_journal.Journal(args.project_root)
    result = authority.mark_attempt_running(
        args.attempt_id,
        args.launch_token,
        launch_epoch=args.launch_epoch,
        worker_instance=goalflight_ledger.process_identity(os.getpid())
        or {"pid": os.getpid()},
    )
    if not result.committed:
        print(
            f"goalflight launch refused: {result.disposition.value}: {result.reason}",
            file=sys.stderr,
            flush=True,
        )
        return 75 if result.retryable else 73
    os.execvpe(command[0], command, os.environ)
    return 70


if __name__ == "__main__":
    raise SystemExit(main())
