#!/usr/bin/env python3
"""Grok Bot listen wrapper: quote-check banner on every listen exit.

Claude Code PostToolUse context-meter / SessionStart hooks are not this
path. Do not port them. Grok Bot has no trustworthy window %; a fake
meter is worse than none. The doorbell process is outside the chat, so
this one-liner survives host autocompact.

Host-local defaults (honor explicit overrides; do not change global
listen defaults): ``--timeout-s 900``, ``--report-pending``, and
``--controller-label goalflight-grokbot``. A bare helper must not listen
on an unlabeled inbox.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

QUOTE_CHECK_BANNER = (
    "QUOTE-CHECK: disk-read SKILL.md Hard Invariants; "
    "if you cannot quote them, stale — resume before acting."
)
DEFAULT_CONTROLLER_LABEL = "goalflight-grokbot"
DEFAULT_TIMEOUT_S = "900"


def _has_option(argv: list[str], name: str) -> bool:
    prefix = f"{name}="
    return any(arg == name or arg.startswith(prefix) for arg in argv)


def _with_host_defaults(argv: list[str]) -> list[str]:
    """Inject grok-bot listen defaults. Explicit flags win."""
    out = list(argv)
    if not _has_option(out, "--timeout-s"):
        out.extend(["--timeout-s", DEFAULT_TIMEOUT_S])
    if not _has_option(out, "--controller-label"):
        out.extend(["--controller-label", DEFAULT_CONTROLLER_LABEL])
    if not (
        "--report-pending" in out
        or "--no-report-pending" in out
        or any(arg.startswith("--report-pending=") for arg in out)
    ):
        out.append("--report-pending")
    return out


def _with_host_timeout(argv: list[str]) -> list[str]:
    """Back-compat alias; defaults now include label and report-pending."""
    return _with_host_defaults(argv)


def main(argv: list[str]) -> int:
    root = Path(__file__).resolve().parent
    listen = [
        sys.executable,
        str(root / "goalflight_messages.py"),
        "listen",
        *_with_host_defaults(argv),
    ]
    proc = subprocess.run(listen, check=False)
    print(QUOTE_CHECK_BANNER, flush=True)
    return int(proc.returncode)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
