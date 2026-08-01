#!/usr/bin/env python3
"""Run a test gate quietly: full output to a log file, summary to the terminal.

The expensive part of a gate run is rarely the wall-clock - it is the output
stream being read back into an agent's context. A full suite emits thousands of
progress and log lines; an agent that runs it inline pays for every one, per
iteration. This wrapper spends that stream on disk instead: the caller sees a
few summary lines, the failures, and the log path, and reads deeper only when
something failed.

Usage:
    goalflight_gate.py                        # default: python3 -m pytest tests/python -q
    goalflight_gate.py -- <cmd> [args...]     # any gate command
    goalflight_gate.py --log PATH --tail N -- <cmd> [args...]

The child's exit code is passed through unchanged, so this wraps a gate without
altering what the gate means. The summary never invents a verdict: it reports
the exit code and whatever summary lines the output actually contained.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

DEFAULT_CMD = [sys.executable, "-m", "pytest", "tests/python", "-q"]
DEFAULT_TAIL = 5
MAX_FAILURE_LINES = 40

# Lines worth surfacing from a pytest-style stream. Other gate commands fall
# back to the plain tail, so nothing here is load-bearing for correctness.
SUMMARY_RE = re.compile(
    r"^(=+ .*(passed|failed|error|skipped|no tests ran).* =+"
    r"|\d+ (passed|failed|error|skipped).*"
    r"|(FAILED|ERROR) .*)$"
)


def run_gate(
    cmd: list[str],
    *,
    log_path: Path,
    tail: int = DEFAULT_TAIL,
    stdout=None,
) -> int:
    out = sys.stdout if stdout is None else stdout
    started = time.monotonic()
    with log_path.open("w", encoding="utf-8") as log:
        try:
            completed = subprocess.run(
                cmd, stdout=log, stderr=subprocess.STDOUT, check=False
            )
            rc = completed.returncode
        except FileNotFoundError:
            print(f"GATE ERROR: command not found: {cmd[0]}", file=out)
            return 127
    elapsed = time.monotonic() - started

    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        lines = []

    interesting = [line for line in lines if SUMMARY_RE.match(line.strip())]
    failures = [line for line in interesting if line.strip().startswith(("FAILED", "ERROR"))]
    summary = [line for line in interesting if line not in failures]
    failure_shaped = bool(failures) or any(
        re.search(r"\b[1-9]\d* (failed|error)", line) for line in summary
    )

    # A gate that produced no recognizable summary still gets its tail shown -
    # an empty report on a nonzero exit is how silent failures get ignored.
    recognized = bool(summary)
    if not summary:
        summary = lines[-tail:]

    # The label reports only what was measured. "GATE PASS" requires the exit
    # code and the output to AGREE: rc==0 beside failure-shaped lines means a
    # wrapper or script swallowed a failing exit somewhere, and rc==0 with no
    # recognizable summary proves only that a process exited - saying "PASS"
    # in either case invents coherence the evidence does not contain.
    if rc != 0:
        verdict = f"GATE FAIL (rc={rc})"
    elif failure_shaped:
        verdict = "GATE RC=0 WITH FAILURE-SHAPED OUTPUT — inspect log"
    elif not recognized:
        verdict = "GATE RC=0 (no summary recognized; see log)"
    else:
        verdict = "GATE PASS"
    print(f"{verdict} in {elapsed:.0f}s — {' '.join(cmd)}", file=out)
    for line in summary[-tail:]:
        print(f"  {line}", file=out)
    if failures:
        shown = failures[:MAX_FAILURE_LINES]
        for line in shown:
            print(f"  {line}", file=out)
        if len(failures) > len(shown):
            print(f"  ... and {len(failures) - len(shown)} more failures", file=out)
    print(f"full log: {log_path} ({len(lines)} lines)", file=out)
    return rc


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--" in argv:
        split = argv.index("--")
        own, cmd = argv[:split], argv[split + 1 :]
    else:
        own, cmd = argv, []

    parser = argparse.ArgumentParser(
        description="Run a test gate with full output to a file, summary to stdout."
    )
    parser.add_argument("--log", type=Path, default=None, help="log file path")
    parser.add_argument("--tail", type=int, default=DEFAULT_TAIL)
    args = parser.parse_args(own)

    log_path = args.log
    if log_path is None:
        log_dir = Path(tempfile.gettempdir()) / f"goalflight-gate-{os.getuid()}"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"gate-{int(time.time())}.log"

    rc = run_gate(cmd or DEFAULT_CMD, log_path=log_path, tail=args.tail)
    try:
        import goalflight_messages
    except Exception:
        pass
    else:
        goalflight_messages.emit_controller_mail_notice(
            project_root=Path.cwd(),
        )
        goalflight_messages.emit_controller_milestone_notice(
            project_root=Path.cwd(),
        )
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
