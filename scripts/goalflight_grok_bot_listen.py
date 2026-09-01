#!/usr/bin/env python3
"""Grok Bot listen wrapper: quote-check banner on every listen exit.

Claude Code PostToolUse context-meter / SessionStart hooks are not this
path. Do not port them. Grok Bot has no trustworthy window %; a fake
meter is worse than none. The doorbell process is outside the chat, so
this one-liner survives host autocompact.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

QUOTE_CHECK_BANNER = (
    "QUOTE-CHECK: disk-read SKILL.md Hard Invariants; "
    "if you cannot quote them, stale — resume before acting."
)


def _with_host_timeout(argv: list[str]) -> list[str]:
    """Host-local default is 900s. Do not change global listen default 0."""
    if any(arg == "--timeout-s" or arg.startswith("--timeout-s=") for arg in argv):
        return argv
    return [*argv, "--timeout-s", "900"]


def main(argv: list[str]) -> int:
    root = Path(__file__).resolve().parent
    listen = [
        sys.executable,
        str(root / "goalflight_messages.py"),
        "listen",
        *_with_host_timeout(argv),
    ]
    proc = subprocess.run(listen, check=False)
    print(QUOTE_CHECK_BANNER, flush=True)
    return int(proc.returncode)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
