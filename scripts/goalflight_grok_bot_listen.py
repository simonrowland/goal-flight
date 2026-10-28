#!/usr/bin/env python3
"""Grok Bot listen wrapper: quote-check banner on every listen exit.

Claude Code PostToolUse context-meter / SessionStart hooks are not this
path. Do not port them. Grok Bot has no trustworthy window %; a fake
meter is worse than none. The doorbell process is outside the chat, so
this one-liner survives host autocompact.

Host-local defaults (honor explicit timeout, report, and label overrides;
do not change global listen defaults): ``--timeout-s 900``,
``--report-pending``, and ``--controller-label goalflight-grokbot``. The
helper always caps this controller at one listener slot.
"""

from __future__ import annotations

import signal
import subprocess
import sys
from pathlib import Path

QUOTE_CHECK_BANNER = (
    "QUOTE-CHECK: disk-read SKILL.md Hard Invariants; "
    "if you cannot quote them, stale — resume before acting."
)
DEFAULT_CONTROLLER_LABEL = "goalflight-grokbot"
DEFAULT_TIMEOUT_S = "900"
DEFAULT_LISTENER_SLOTS = "1"


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
    single_slot_args: list[str] = []
    skip_slot_value = False
    for arg in out:
        if skip_slot_value:
            skip_slot_value = False
            continue
        if arg == "--listener-slots":
            skip_slot_value = True
            continue
        if arg.startswith("--listener-slots="):
            continue
        single_slot_args.append(arg)
    out = [*single_slot_args, "--listener-slots", DEFAULT_LISTENER_SLOTS]
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
    proc: subprocess.Popen[bytes] | None = None
    pending_signal: int | None = None
    previous_handlers: dict[int, object] = {}

    def forward_signal(signum: int, _frame: object) -> None:
        nonlocal pending_signal
        if proc is None:
            pending_signal = signum
            return
        try:
            proc.send_signal(signum)
        except ProcessLookupError:
            pass

    try:
        for name in ("SIGTERM", "SIGHUP"):
            signum = getattr(signal, name, None)
            if signum is not None:
                previous_handlers[signum] = signal.signal(signum, forward_signal)
        proc = subprocess.Popen(listen)
        if pending_signal is not None:
            forward_signal(pending_signal, None)
        returncode = int(proc.wait())
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
    print(QUOTE_CHECK_BANNER, flush=True)
    if returncode < 0:
        returncode = 128 - returncode
    return returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
