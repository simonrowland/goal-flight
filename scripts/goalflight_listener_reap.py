#!/usr/bin/env python3
"""Listener processes outliving their controller generation (b-340).

A listener is spawned under a controller's lease nonce and should end with
that generation. When a session restarts or is taken over the lease record
goes and the processes do not. Measured 2026-09-06: 44 live listeners across
9 nonces while the journal held ONE lease record -- 35 processes belonging to
eight generations that no longer existed, growing with every restart.

That is a correctness problem, not just resource use: SKILL.md's rule is that
a supervisor plus loose listeners DOUBLE-DELIVER, so an orphan can duplicate
mail into a live stream.

ONE rule, in one place. The reporter (doctor) and the killer (controller
startup) both call `orphaned_listeners` here, because a killer whose notion of
"orphaned" has drifted from the reporter's is exactly the bug this module
exists to fix.

★ The dangerous failure is not missing an orphan, it is mistaking a LIVE
generation for a dead one. Everything below is shaped so that a question we
cannot answer yields `known=False` and the caller kills nothing:

* the process table cannot be read      -> unknown, refuse
* the lease records cannot be read      -> unknown, refuse
  (this one matters most: an empty "known nonces" set would make EVERY
  listener look orphaned, so a journal that is merely busy would otherwise
  take out the whole live fleet)
* a process carries no `--lease-nonce`  -> unattributable, never reaped
* the caller's own generation           -> never reaped, even when the journal
  has no record for it yet, because startup races the first lease write
"""

from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
import time

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import goalflight_journal
import goalflight_wake


PS_TIMEOUT_S = 15.0
# Between SIGTERM and the liveness re-check. Listeners exit on the signal; this
# only has to cover process teardown, not any work they might be doing.
TERM_GRACE_S = 0.5


def listener_processes_by_nonce(project_root: Path) -> dict[str, list[int]] | None:
    """{lease_nonce: [pid, ...]} FOR THIS PROJECT, or None if ps cannot be read.

    ★ The project filter is load-bearing, not tidiness. The process table is
    machine-wide while lease records are per-project, so an unscoped listing
    compared against one project's journal makes every OTHER project's live
    listeners look orphaned. Observed 2026-09-06 within minutes of wiring this
    into startup: a test claiming a controller under a temp project root
    SIGTERM'd this session's real supervisor, because its nonce was absent from
    the temp journal. Scope the population to the same project whose leases
    decide the verdict, or the predicate is answering a different question than
    the one asked.

    None means "we could not look" and must never be read as "none found".
    """
    try:
        wanted = Path(project_root).expanduser().resolve(strict=False)
    except OSError:
        return None
    try:
        listing = subprocess.run(
            ["ps", "-ax", "-o", "pid=,args="],
            capture_output=True,
            text=True,
            timeout=PS_TIMEOUT_S,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None

    found: dict[str, list[int]] = {}
    for line in listing.splitlines():
        head, _, rest = line.strip().partition(" ")
        if not head.isdigit() or not rest:
            continue
        parts = rest.split()
        if not any(goalflight_wake._is_messages_argv_name(p) for p in parts):
            continue
        # Same-project only. A listener that does not say which project it
        # serves cannot be attributed, so it is never reapable.
        root = None
        for index, part in enumerate(parts):
            if part == "--project-root" and index + 1 < len(parts):
                root = parts[index + 1]
                break
        if not root:
            continue
        try:
            if Path(root).expanduser().resolve(strict=False) != wanted:
                continue
        except OSError:
            continue
        nonce = None
        for index, part in enumerate(parts):
            if part == "--lease-nonce" and index + 1 < len(parts):
                nonce = parts[index + 1]
                break
        if not nonce:
            # Unattributable: it may belong to a live generation. Never reapable.
            continue
        found.setdefault(nonce, []).append(int(head))
    return found


def _known_lease_nonces(project_root: Path) -> set[str] | None:
    try:
        records = goalflight_journal.Journal.open_reader(
            Path(project_root).resolve()
        ).lease_records()
    except Exception:
        return None
    if not isinstance(records, list):
        return None
    known: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            continue
        nonce = record.get("lease_nonce") or record.get("nonce")
        if isinstance(nonce, str) and nonce:
            known.add(nonce)
    return known


def orphaned_listeners(
    project_root: Path, *, current_nonce: str | None = None
) -> dict:
    """Which listener processes belong to generations that no longer exist."""
    by_nonce = listener_processes_by_nonce(project_root)
    if by_nonce is None:
        return {
            "known": False,
            "reason": "process table unreadable; orphan status unknown",
        }
    if not by_nonce:
        return {
            "known": True,
            "listeners": 0,
            "generations": 0,
            "orphans": [],
            "orphan_generations": [],
        }

    known = _known_lease_nonces(project_root)
    if known is None:
        return {
            "known": False,
            "listeners": sum(len(v) for v in by_nonce.values()),
            "generations": len(by_nonce),
            "reason": (
                "lease records unreadable; every generation would look orphaned, "
                "so orphan status is unknown"
            ),
        }

    live = set(known)
    if current_nonce:
        live.add(current_nonce)
    orphan_nonces = sorted(n for n in by_nonce if n not in live)
    orphan_pids = [pid for n in orphan_nonces for pid in by_nonce[n]]
    return {
        "known": True,
        "listeners": sum(len(v) for v in by_nonce.values()),
        "generations": len(by_nonce),
        "orphans": orphan_pids,
        "orphan_generations": orphan_nonces,
    }


def _protected_pids() -> set[int]:
    """This process and its ancestors are never reaped."""
    protected = {os.getpid()}
    try:
        protected.add(os.getppid())
    except OSError:
        pass
    return protected


def _liveness(pids: list[int]) -> dict[int, bool | None]:
    """{pid: alive}, in ONE ps call. None means the state could not be read.

    A ZOMBIE IS DEAD. `os.kill(pid, 0)` succeeds on an unreaped child, so a
    signal-based check reports "survived SIGTERM" for a process that has
    already exited -- which is how the first version of this module scored a
    successful kill as stubborn. Read the state instead; `Z` is gone.
    """
    if not pids:
        return {}
    try:
        listing = subprocess.run(
            ["ps", "-o", "pid=,state=", "-p", ",".join(str(p) for p in pids)],
            capture_output=True,
            text=True,
            timeout=PS_TIMEOUT_S,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {pid: None for pid in pids}
    # ps omits pids that no longer exist, so absence here is a measured exit.
    states: dict[int, bool | None] = {pid: False for pid in pids}
    for line in listing.splitlines():
        head, _, rest = line.strip().partition(" ")
        if not head.isdigit():
            continue
        state = rest.strip()
        states[int(head)] = not state.startswith("Z")
    return states


def reap_orphaned_listeners(
    project_root: Path,
    *,
    current_nonce: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Terminate listeners whose controller generation is gone.

    Refuses entirely when orphan status is unknown. Reports what it actually
    did, verified by a liveness re-check rather than by the fact that a signal
    was sent -- an unverified action script reports intent as outcome.
    """
    report = orphaned_listeners(project_root, current_nonce=current_nonce)
    if not report.get("known"):
        return {
            "reaped": 0,
            "refused": report.get("reason", "orphan status unknown"),
            "detail": report,
        }

    targets = [pid for pid in report["orphans"] if pid not in _protected_pids()]
    if dry_run:
        return {"reaped": 0, "would_reap": targets, "detail": report}

    reaped: list[int] = []
    stubborn: list[dict] = []
    signalled: list[int] = []
    for pid in targets:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            reaped.append(pid)  # already gone; the goal is met
        except PermissionError:
            stubborn.append({"pid": pid, "why": "not-ours"})
        except OSError as exc:
            stubborn.append({"pid": pid, "why": type(exc).__name__})
        else:
            signalled.append(pid)

    if signalled:
        time.sleep(TERM_GRACE_S)
        for pid, alive in _liveness(signalled).items():
            if alive is False:
                reaped.append(pid)
            elif alive is None:
                stubborn.append({"pid": pid, "why": "liveness-unmeasurable"})
            else:
                stubborn.append({"pid": pid, "why": "still-alive-after-term"})
    result = {
        "reaped": len(reaped),
        "reaped_pids": reaped,
        "generations": report["orphan_generations"],
        "detail": report,
    }
    if stubborn:
        result["stubborn"] = stubborn
    return result
