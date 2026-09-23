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
* a listener's lease nonce cannot be read in full -> unknown, refuse
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

import goalflight_compat
import goalflight_journal
import goalflight_wake


PS_TIMEOUT_S = 15.0
# Between SIGTERM and the liveness re-check. Listeners exit on the signal; this
# only has to cover process teardown, not any work they might be doing.
TERM_GRACE_S = 0.5
_LISTENER_COMMANDS = frozenset({"listen", "listen-auto", "follow", "supervise"})


def listener_processes_by_nonce(project_root: Path) -> dict[str, list[dict[str, object]]] | None:
    """{lease_nonce: [{pid, start_token}, ...]} FOR THIS PROJECT.

    Each candidate carries a start token captured in the same enumeration pass;
    a PID without a proven generation is never actionable.

    ★ The project filter is load-bearing, not tidiness. The process table is
    machine-wide while lease records are per-project, so an unscoped listing
    compared against one project's journal makes every OTHER project's live
    listeners look orphaned. Observed 2026-09-06 within minutes of wiring this
    into startup: a test claiming a controller under a temp project root
    SIGTERM'd this session's real supervisor, because its nonce was absent from
    the temp journal. Scope the population to the same project whose leases
    decide the verdict, or the predicate is answering a different question than
    the one asked.

    None means "we could not look, or a listener argv was incomplete" and must
    never be read as "none found".
    """
    try:
        wanted = Path(project_root).expanduser().resolve(strict=False)
    except OSError:
        return None
    try:
        result = subprocess.run(
            ["ps", "-axww", "-o", "pid=,args="],
            capture_output=True,
            text=True,
            timeout=PS_TIMEOUT_S,
        )
        if getattr(result, "returncode", 0) != 0:
            return None
        listing = result.stdout
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return None
    if not isinstance(listing, str):
        return None

    found: dict[str, list[dict[str, object]]] = {}
    for line in listing.splitlines():
        head, _, rest = line.strip().partition(" ")
        if not head.isdigit() or not rest:
            continue
        classification, fields = goalflight_wake._probe_messages_argv(
            rest, commands=_LISTENER_COMMANDS
        )
        if classification == goalflight_wake._SUPERVISE_ARGV_UNKNOWN:
            return None
        if classification != goalflight_wake._SUPERVISE_ARGV_MATCH or fields is None:
            continue
        # Same-project only. A listener that does not say which project it
        # serves cannot be attributed, so the process table is not trustworthy.
        root = fields.get("project_root")
        if not root:
            return None
        try:
            if Path(root).expanduser().resolve(strict=False) != wanted:
                continue
        except OSError:
            return None
        nonce = fields.get("lease_nonce")
        if not nonce:
            # A truncated or unreadable nonce may belong to a live generation.
            # Do not let any other listener become reapable from this scan.
            return None
        identity = goalflight_compat.process_start_identity(int(head))
        if not isinstance(identity, dict) or not identity.get("start_token"):
            # PID/argv alone is not ownership. A failed or incomplete identity
            # probe stays out of the actionable population.
            continue
        found.setdefault(nonce, []).append(
            {"pid": int(head), "start_token": str(identity["start_token"])}
        )
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
    orphan_records = [record for n in orphan_nonces for record in by_nonce[n]]
    orphan_pids = [int(record["pid"]) for record in orphan_records]
    return {
        "known": True,
        "listeners": sum(len(v) for v in by_nonce.values()),
        "generations": len(by_nonce),
        "orphans": orphan_pids,
        "orphan_records": orphan_records,
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
        result = subprocess.run(
            ["ps", "-o", "pid=,state=", "-p", ",".join(str(p) for p in pids)],
            capture_output=True,
            text=True,
            timeout=PS_TIMEOUT_S,
        )
        if getattr(result, "returncode", 0) != 0:
            return {pid: None for pid in pids}
        listing = result.stdout
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

    protected = _protected_pids()
    targets = [
        record
        for record in report.get("orphan_records", [])
        if isinstance(record, dict)
        and isinstance(record.get("pid"), int)
        and isinstance(record.get("start_token"), str)
        and record["start_token"]
        and record["pid"] not in protected
    ]
    if dry_run:
        return {
            "reaped": 0,
            "would_reap": [record["pid"] for record in targets],
            "detail": report,
        }

    reaped: list[int] = []
    stubborn: list[dict] = []
    signalled: list[int] = []
    refused_identity: list[dict[str, object]] = []
    for record in targets:
        pid = int(record["pid"])
        current = goalflight_compat.process_start_identity(pid)
        if (
            not isinstance(current, dict)
            or not current.get("start_token")
            or current.get("pid") != pid
            or str(current["start_token"]) != record["start_token"]
        ):
            refused_identity.append({"pid": pid, "why": "identity-unverified"})
            continue
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
    if refused_identity:
        result["refused_identity"] = refused_identity
    if stubborn:
        result["stubborn"] = stubborn
    return result
