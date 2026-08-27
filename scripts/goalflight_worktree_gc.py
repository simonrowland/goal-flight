#!/usr/bin/env python3
"""Report (or remove) git worktrees whose removal is provably safe.

Report-only by default. ``--apply`` is required to remove anything, and every
survivor is printed with the reason it was retained — the reason is how an
operator confirms the tool understood the tree rather than guessed.

Routine merge-down command (run from the repo after integrating a worker branch)::

    python3 scripts/goalflight_worktree_gc.py --into main
    python3 scripts/goalflight_worktree_gc.py --into main --apply

Registered pool seats (``<repo>/worktrees/wt-N`` with a matching seat lock)
are maintained by ``goalflight_worktree_pool`` and are never reclaimed as
litter. A directory merely *named* ``wt-N`` is ordinary litter: exemption is
by registration, not basename. If registration cannot be determined, the
verdict is UNKNOWN and the tree is retained.

Removal requires the CONJUNCTION of all four conditions:

  1. the worktree's branch is merged into the integration branch; AND
  2. the worktree is clean (``git status --porcelain`` empty); AND
  3. no non-terminal dispatch records that path as its ``worker_cwd``, and no
     identity-live worker whose ledger row carries a liveness verdict; AND
  4. it is not the currently-checked-out path (nor the main worktree).

Why the conjunction, and why "merged" alone is not a predicate
--------------------------------------------------------------

Audit 2026-08-27: 37 worktrees had accumulated under ``worktrees/`` from prior
sessions. The obvious sweep — "branch is an ancestor of main, therefore safe to
delete" — returns TRUE for a worktree whose branch simply EQUALS main because
its worker has not committed yet. A merged-only sweep would have deleted four
ACTIVE workers' in-progress trees; all four were live at audit time. The
predicate answered a different question than the one being asked, and it looked
authoritative. Condition (3) is the one that saves live work.

Condition (3) reads the dispatch ledger — never ``ps``/``pgrep``. ``pgrep``
matches the searcher itself, and a process probe cannot see a worker that has
been dispatched but has not spawned yet; the ledger records the claim at
dispatch time, before any process exists. A future maintainer will be tempted
to drop this check as slow: the four live trees above are what it costs.

Three-state discipline (load-bearing)
-------------------------------------

Every condition distinguishes "I know this is false" from "I could not find
out". UNKNOWN always retains, with a reason naming the check that could not be
performed. If the ledger is unreadable, condition (3) is UNKNOWN for every
worktree — an unreadable record may be exactly the live dispatch that owns the
path. A tool that treats "could not read the ledger" as "no dispatch owns it"
deletes live workers' trees, which is precisely the failure this predicate
exists to prevent. Absence of proof is never proof of absence.

Removal uses ``git worktree remove`` semantics. A worktree whose directory is
already gone but whose administrative entry remains is reclaimed with
``git worktree prune`` and reported as ``pruned`` — a distinct outcome, not an
error and not a removal. Paths come exclusively from the repo's own
``git worktree list --porcelain`` output; nothing outside that list is touched.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import goalflight_compat  # noqa: E402
import goalflight_dispatch_states  # noqa: E402
import goalflight_ledger  # noqa: E402
import goalflight_worktree_pool  # noqa: E402

SCHEMA = "goalflight.worktree-gc.v1"

YES = "yes"
NO = "no"
UNKNOWN = "unknown"

_GIT_TIMEOUT = 30

# Ledger rows whose ``state`` / ``terminal_state`` looks settled but may still
# name a live process. ``idle_timeout`` in particular has been observed on a
# worker that stayed identity-live and mid-gate for tens of minutes.
LIVENESS_VERDICTS = frozenset(
    {
        "idle_timeout",
        "worker_dead",
        "blocked",
        "wedged",
        "liveness_indeterminate",
        "inconclusive_timeout",
        "watcher_stopped",
    }
)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=_GIT_TIMEOUT,
        check=False,
    )


def _presence(path: Path) -> str:
    """Return present / absent / unknown. Never use Path.exists() here.

    ``exists()`` answers False for both "not there" and "I could not look".
    Only FileNotFoundError is evidence of absence.
    """
    try:
        os.lstat(path)
    except FileNotFoundError:
        return "absent"
    except OSError:
        return "unknown"
    return "present"


def _resolve(path: str) -> str:
    return os.path.realpath(path)


def _condition(verdict: str, reason: str) -> dict[str, str]:
    return {"verdict": verdict, "reason": reason}


# --------------------------------------------------------------------------
# Worktree listing


def list_worktrees(repo: Path) -> tuple[list[dict[str, Any]], str | None]:
    """Parse ``git worktree list --porcelain``. (entries, error)."""
    try:
        proc = _git(repo, "worktree", "list", "--porcelain")
    except (OSError, subprocess.SubprocessError) as exc:
        return [], f"git worktree list failed ({exc.__class__.__name__})"
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip() or "not a git repository"
        return [], detail
    entries: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in proc.stdout.splitlines():
        if line.startswith("worktree "):
            current = {"path": line[len("worktree "):].strip(), "branch": None,
                       "detached": False, "prunable": False, "bare": False}
            entries.append(current)
        elif current is None:
            continue
        elif line.startswith("branch "):
            ref = line.split(" ", 1)[1].strip()
            current["branch"] = ref.removeprefix("refs/heads/")
        elif line == "detached":
            current["detached"] = True
        elif line.startswith("prunable"):
            current["prunable"] = True
        elif line == "bare":
            current["bare"] = True
    return entries, None


def main_worktree_path(repo: Path) -> str | None:
    """Absolute path of the main worktree, via the common git dir's parent."""
    try:
        proc = _git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    common = proc.stdout.strip()
    if not common:
        return None
    return _resolve(str(Path(common).parent))


def current_checkout_path(repo: Path) -> tuple[str | None, str | None]:
    """The checked-out path for the repo argument. (path, error)."""
    try:
        proc = _git(repo, "rev-parse", "--path-format=absolute", "--show-toplevel")
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"git rev-parse --show-toplevel failed ({exc.__class__.__name__})"
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip() or "cannot resolve checkout"
        return None, detail
    return _resolve(proc.stdout.strip()), None


# --------------------------------------------------------------------------
# The four conditions. Each returns _condition(YES|NO|UNKNOWN, reason).


def check_merged(repo: Path, branch: str | None, detached: bool, into: str) -> dict[str, str]:
    """Condition 1: the branch is merged into the integration branch."""
    if detached or not branch:
        return _condition(
            UNKNOWN,
            "detached HEAD: no branch to test for merge state",
        )
    try:
        base = _git(repo, "rev-parse", "--verify", "--quiet", f"{into}^{{commit}}")
    except (OSError, subprocess.SubprocessError) as exc:
        return _condition(
            UNKNOWN,
            f"cannot resolve integration branch {into!r} ({exc.__class__.__name__})",
        )
    if base.returncode != 0:
        return _condition(
            UNKNOWN,
            f"integration branch {into!r} does not exist, so merge state "
            "cannot be evaluated",
        )
    try:
        proc = _git(repo, "merge-base", "--is-ancestor", branch, into)
    except (OSError, subprocess.SubprocessError) as exc:
        return _condition(
            UNKNOWN,
            f"ancestry of branch {branch!r} could not be evaluated "
            f"({exc.__class__.__name__})",
        )
    if proc.returncode == 0:
        return _condition(YES, f"branch {branch!r} is merged into {into!r}")
    if proc.returncode == 1:
        return _condition(
            NO,
            f"branch {branch!r} has commits not in {into!r}",
        )
    detail = (proc.stderr or proc.stdout).strip() or f"exit {proc.returncode}"
    return _condition(
        UNKNOWN,
        f"ancestry of branch {branch!r} could not be evaluated ({detail})",
    )


def check_clean(path: str, *, directory_state: str) -> dict[str, str]:
    """Condition 2: the worktree is clean.

    A missing directory is vacuously clean: there is no on-disk work left to
    protect, and reclamation of the administrative entry is the prune path.
    An unverifiable directory is never assumed clean.
    """
    if directory_state == "absent":
        return _condition(
            YES,
            "worktree directory absent; no on-disk work to protect",
        )
    if directory_state == "unknown":
        return _condition(
            UNKNOWN,
            "worktree directory presence unverifiable, so cleanliness is unknown",
        )
    try:
        proc = _git(Path(path), "status", "--porcelain")
    except (OSError, subprocess.SubprocessError) as exc:
        return _condition(
            UNKNOWN,
            f"git status could not run ({exc.__class__.__name__}), "
            "so cleanliness is unknown",
        )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip() or f"exit {proc.returncode}"
        return _condition(
            UNKNOWN,
            f"git status failed ({detail}), so cleanliness is unknown",
        )
    dirty = [line for line in proc.stdout.splitlines() if line.strip()]
    if dirty:
        return _condition(
            NO,
            f"worktree has uncommitted or untracked files ({len(dirty)} entries)",
        )
    return _condition(YES, "worktree is clean")


def read_ledger_records(ledger_dir: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """Return (records, unreadable_files) from the dispatch runs directory.

    ``goalflight_ledger.read_records`` collapses a corrupt file into an
    ``unreadable`` placeholder so production never raises; this sweep needs the
    distinction preserved, because an unreadable record may be exactly the live
    dispatch that owns the path under evaluation.
    """
    state = _presence(ledger_dir)
    if state == "absent":
        # No runs directory at all: no dispatch has ever recorded a claim here.
        return [], []
    if state == "unknown":
        return [], [str(ledger_dir)]
    records: list[dict[str, Any]] = []
    unreadable: list[str] = []
    try:
        children = sorted(ledger_dir.glob("*.json"))
    except OSError:
        return [], [str(ledger_dir)]
    for child in children:
        try:
            payload = json.loads(child.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            unreadable.append(child.name)
            continue
        if not isinstance(payload, dict):
            unreadable.append(child.name)
            continue
        records.append(payload)
    return records, unreadable


def _record_cwd_matches(record: dict[str, Any], path: str) -> bool:
    raw_cwd = record.get("worker_cwd")
    if not isinstance(raw_cwd, str) or not raw_cwd.strip():
        return False
    cwd = _resolve(raw_cwd)
    target = _resolve(path)
    return cwd == target or cwd.startswith(target + os.sep)


def _record_states(record: dict[str, Any]) -> list[str]:
    states: list[str] = []
    for key in ("state", "terminal_state"):
        value = record.get(key)
        if isinstance(value, str) and value:
            states.append(value)
    return states


def _is_liveness_verdict(record: dict[str, Any]) -> bool:
    for state in _record_states(record):
        if state in LIVENESS_VERDICTS or state.startswith("blocked"):
            return True
    return False


def _identity_live(record: dict[str, Any]) -> bool | None:
    """pid + start_token liveness. Never pgrep, never pid alone.

    True: the recorded generation is still that process.
    False: no pid was recorded, or the generation is proven gone/replaced.
    None: a pid exists but the check could not complete — fail closed.
    """
    identity = record.get("worker_identity")
    pid = None
    start_token = ""
    if isinstance(identity, dict):
        raw_pid = identity.get("pid")
        if isinstance(raw_pid, int) and not isinstance(raw_pid, bool) and raw_pid > 0:
            pid = raw_pid
        token = identity.get("start_token")
        if isinstance(token, str) and token:
            start_token = token
    if pid is None:
        raw_pid = record.get("worker_pid")
        if isinstance(raw_pid, int) and not isinstance(raw_pid, bool) and raw_pid > 0:
            pid = raw_pid
        else:
            return False
    if not start_token:
        return None
    return goalflight_compat.process_identity_matches(pid, start_token)


def _record_owns_path(record: dict[str, Any], path: str) -> bool:
    """True when a dispatch still owns this path as its worker cwd.

    A missing state is treated as non-terminal: we did not observe the record
    settle, so we do not get to assume it did. A cwd recorded inside the
    worktree (a worker that cd'd deeper) still means the tree is in use.

    A liveness verdict (``idle_timeout``, ``worker_dead``, ``blocked``, …) is
    not proof the process is gone. Observed: a worker sat in ``idle_timeout``
    for 35 minutes while identity-live and mid-gate. If the recorded pid +
    start_token still match, the row owns the path.
    """
    if not _record_cwd_matches(record, path):
        return False
    live = _identity_live(record)
    if live is True:
        return True
    if live is None and _is_liveness_verdict(record):
        return True
    for state in _record_states(record):
        if goalflight_dispatch_states.is_terminal_state(state):
            return False
    return True


def check_unowned(path: str, ledger_dir: Path) -> dict[str, str]:
    """Condition 3: no non-terminal dispatch has this path as its cwd.

    This is the condition that saves live work — see the module docstring. On
    2026-08-27 a merged-only sweep scored four ACTIVE workers' trees as
    deletable because each uncommitted branch still EQUALED main; only the
    ledger claim distinguished "merged" from "not started". The ledger is
    consulted rather than ps/pgrep because a process probe cannot see a worker
    that has been dispatched but has not spawned yet (and pgrep matches the
    searcher itself). Do not drop this check as slow, and do not let an
    unreadable ledger collapse into a green light: an unreadable record may be
    exactly the live dispatch that owns this path, so UNKNOWN retains.
    """
    records, unreadable = read_ledger_records(ledger_dir)
    if unreadable:
        return _condition(
            UNKNOWN,
            "dispatch ledger unreadable ("
            + ", ".join(unreadable)
            + "); cannot prove no live dispatch owns this path",
        )
    owners = [
        str(record.get("dispatch_id") or "<unknown>")
        for record in records
        if _record_owns_path(record, path)
    ]
    if owners:
        states = {
            str(record.get("state") or "<none>")
            for record in records
            if _record_owns_path(record, path)
        }
        return _condition(
            NO,
            "non-terminal dispatch "
            + ", ".join(sorted(owners))
            + " (state="
            + ", ".join(sorted(states))
            + ") records this path as worker_cwd",
        )
    return _condition(YES, "no non-terminal dispatch records this path")


def check_not_current(
    path: str,
    *,
    current_checkout: str | None,
    current_error: str | None,
) -> dict[str, str]:
    """Condition 4: this is not the currently-checked-out path."""
    if current_error is not None:
        return _condition(
            UNKNOWN,
            f"current checkout could not be determined ({current_error})",
        )
    if current_checkout is not None and _resolve(path) == current_checkout:
        return _condition(NO, "this path is the currently-checked-out worktree")
    return _condition(YES, "not the currently-checked-out worktree")


# --------------------------------------------------------------------------
# Classification


def classify(
    repo: Path,
    entry: dict[str, Any],
    *,
    into: str,
    ledger_dir: Path,
    main_path: str | None,
    current_checkout: str | None,
    current_error: str | None,
) -> dict[str, Any]:
    """Evaluate one listed worktree against the full conjunction."""
    path = entry["path"]
    directory_state = _presence(Path(path))
    result: dict[str, Any] = {
        "path": path,
        "branch": entry.get("branch"),
        "detached": bool(entry.get("detached")),
        "missing_on_disk": directory_state == "absent",
    }

    if main_path is not None and _resolve(path) == main_path:
        result["decision"] = "retain"
        result["reason"] = "main worktree is never a removal candidate"
        result["conditions"] = {}
        return result

    seat_verdict, seat_reason = goalflight_worktree_pool.registered_pool_seat_verdict(
        path, project_root=repo
    )
    if seat_verdict == YES:
        result["decision"] = "retain"
        result["reason"] = (
            "managed pool seat "
            f"{Path(path).name} is maintained by the worktree pool, not litter"
        )
        result["conditions"] = {}
        result["pool_seat"] = {"verdict": seat_verdict, "reason": seat_reason}
        return result
    if seat_verdict == UNKNOWN:
        result["decision"] = "retain"
        result["reason"] = (
            "pool-seat registration unknown ("
            f"{seat_reason}); cannot prove this path is not a maintained seat"
        )
        result["conditions"] = {}
        result["pool_seat"] = {"verdict": seat_verdict, "reason": seat_reason}
        return result

    conditions = {
        "merged": check_merged(repo, entry.get("branch"), bool(entry.get("detached")), into),
        "clean": check_clean(path, directory_state=directory_state),
        "unowned": check_unowned(path, ledger_dir),
        "not_current": check_not_current(
            path, current_checkout=current_checkout, current_error=current_error
        ),
    }
    result["conditions"] = conditions

    blockers = [
        f"{name}: {cond['reason']}"
        for name, cond in conditions.items()
        if cond["verdict"] != YES
    ]
    if blockers:
        result["decision"] = "retain"
        result["reason"] = "; ".join(blockers)
        return result

    result["decision"] = "prune" if directory_state == "absent" else "remove"
    result["reason"] = (
        "all four conditions hold: branch merged into "
        f"{into!r}, worktree clean, no non-terminal dispatch owns the path, "
        "not the current checkout"
    )
    return result


# --------------------------------------------------------------------------
# Removal


def _remove_worktree(repo: Path, path: str) -> tuple[bool, str]:
    proc = _git(repo, "worktree", "remove", path)
    if proc.returncode == 0:
        return True, ""
    return False, (proc.stderr or proc.stdout).strip() or f"exit {proc.returncode}"


def _prune_worktrees(repo: Path) -> tuple[bool, str]:
    proc = _git(repo, "worktree", "prune")
    if proc.returncode == 0:
        return True, ""
    return False, (proc.stderr or proc.stdout).strip() or f"exit {proc.returncode}"


def apply_removals(
    repo: Path,
    entries: list[dict[str, Any]],
    *,
    into: str,
    ledger_dir: Path,
    main_path: str | None,
    current_checkout: str | None,
    current_error: str | None,
) -> None:
    """Act on decided entries, re-verifying each immediately beforehand.

    The scan is a snapshot; a branch can acquire commits, a tree can acquire
    files, and a dispatch can claim a path between listing and acting. A stale
    decision is never executed: anything no longer removable is reported as
    ``changed_before_remove`` instead.
    """
    targets = [e for e in entries if e["decision"] in {"remove", "prune"}]
    if not targets:
        return

    for entry in targets:
        path = entry["path"]

        # Re-list: the fresh listing is the only authority on what exists NOW.
        # A path that dropped off the list was reclaimed by someone else.
        listed, list_error = list_worktrees(repo)
        fresh = next(
            (item for item in listed if _resolve(item["path"]) == _resolve(path)),
            None,
        )
        if list_error is not None or fresh is None:
            entry["outcome"] = "retained"
            entry["reason"] = (
                f"changed_before_remove: worktree listing changed ({list_error or 'path no longer listed'})"
            )
            continue
        current = classify(
            repo,
            fresh,
            into=into,
            ledger_dir=ledger_dir,
            main_path=main_path,
            current_checkout=current_checkout,
            current_error=current_error,
        )
        if current["decision"] not in {"remove", "prune"}:
            entry["outcome"] = "retained"
            entry["reason"] = f"changed_before_remove: {current['reason']}"
            continue

        # ``git worktree prune`` clears the admin entry of EVERY worktree
        # whose directory is gone — it takes no path argument. Run it only
        # when the set of stale entries git would clear is exactly the set we
        # vetted; otherwise pruning would also clear entries that never passed
        # the conjunction.
        stale = {item["path"] for item in listed if _presence(Path(item["path"])) == "absent"}
        prune_allowed = stale <= {path}

        if current["decision"] == "prune":
            if not prune_allowed:
                entry["outcome"] = "failed"
                entry["error"] = (
                    "git worktree prune would also clear administrative entries "
                    "that did not pass the conjunction; skipped"
                )
                continue
            ok, detail = _prune_worktrees(repo)
            if ok:
                entry["outcome"] = "pruned"
            else:
                entry["outcome"] = "failed"
                entry["error"] = detail
            continue
        ok, detail = _remove_worktree(repo, path)
        if ok:
            entry["outcome"] = "removed"
        elif _presence(Path(path)) == "absent" and prune_allowed:
            # The directory disappeared between scan and removal; reclaim the
            # administrative entry instead of reporting an error.
            ok, detail = _prune_worktrees(repo)
            entry["outcome"] = "pruned" if ok else "failed"
            if not ok:
                entry["error"] = detail
        else:
            entry["outcome"] = "failed"
            entry["error"] = detail


# --------------------------------------------------------------------------
# Reporting


def _counts(entries: list[dict[str, Any]]) -> dict[str, int]:
    counts = {
        "worktrees": len(entries),
        "removable": 0,
        "retained": 0,
        "removed": 0,
        "pruned": 0,
        "failed": 0,
    }
    for entry in entries:
        if entry["decision"] == "retain":
            counts["retained"] += 1
        else:
            counts["removable"] += 1
        outcome = entry.get("outcome")
        if outcome == "removed":
            counts["removed"] += 1
        elif outcome == "pruned":
            counts["pruned"] += 1
        elif outcome == "failed":
            counts["failed"] += 1
    return counts


def format_human(repo: Path, into: str, entries: list[dict[str, Any]], *, applied: bool) -> str:
    counts = _counts(entries)
    mode = "apply" if applied else "report"
    lines = [
        f"worktree gc ({mode}): {repo}  into={into}",
        f"  worktrees : {counts['worktrees']}",
        f"  removable : {counts['removable']}",
        f"  retained  : {counts['retained']}",
    ]
    if applied:
        lines.append(
            f"  removed {counts['removed']}, pruned {counts['pruned']}, "
            f"failed {counts['failed']}"
        )
    removable = [e for e in entries if e["decision"] != "retain"]
    retained = [e for e in entries if e["decision"] == "retain"]
    if removable:
        lines.append("\n  removable worktrees:")
        for entry in removable:
            verb = entry.get("outcome") or f"would_{entry['decision']}"
            branch = entry["branch"] or "(detached)"
            lines.append(f"    {verb:<14} {entry['path']}  branch={branch}")
            if entry.get("error"):
                lines.append(f"                   error={entry['error']}")
    if retained:
        lines.append("\n  retained:")
        for entry in retained:
            branch = entry["branch"] or "(detached)"
            lines.append(f"    {entry['path']}  branch={branch}")
            lines.append(f"        why={entry['reason']}")
    if not applied and counts["removable"]:
        lines.append("\n  report only - nothing removed. Re-run with --apply to remove.")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Report (or with --apply, remove) git worktrees that are merged, "
            "clean, unowned by a live dispatch, and not checked out. "
            "Registered wt-N pool seats are never reclaimed; a directory "
            "merely named wt-N is ordinary litter. Run after merging "
            "a worker branch into the integration branch."
        )
    )
    parser.add_argument(
        "repo",
        nargs="?",
        type=Path,
        default=Path.cwd(),
        help="Repository (or any of its worktrees) to sweep. Default: cwd.",
    )
    parser.add_argument(
        "--into",
        default="main",
        help="Integration branch for the merged check (default: main).",
    )
    parser.add_argument(
        "--ledger-dir",
        type=Path,
        default=None,
        help="Dispatch ledger runs directory "
        "(default: Goal Flight machine-state runs directory).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually remove reclaimable worktrees. Default is report-only.",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo = args.repo.resolve()
    ledger_dir = args.ledger_dir or goalflight_ledger.runs_dir(create=False)

    listed, list_error = list_worktrees(repo)
    if list_error is not None:
        print(f"cannot list worktrees for {repo}: {list_error}", file=sys.stderr)
        return 1
    main_path = main_worktree_path(repo)
    current_checkout, current_error = current_checkout_path(repo)

    entries = [
        classify(
            repo,
            entry,
            into=args.into,
            ledger_dir=ledger_dir,
            main_path=main_path,
            current_checkout=current_checkout,
            current_error=current_error,
        )
        for entry in listed
    ]

    if args.apply:
        apply_removals(
            repo,
            entries,
            into=args.into,
            ledger_dir=ledger_dir,
            main_path=main_path,
            current_checkout=current_checkout,
            current_error=current_error,
        )

    report = {
        "schema": SCHEMA,
        "repo": str(repo),
        "into": args.into,
        "ledger_dir": str(ledger_dir),
        "mode": "apply" if args.apply else "report",
        **_counts(entries),
        "entries": entries,
    }
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(format_human(repo, args.into, entries, applied=bool(args.apply)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
