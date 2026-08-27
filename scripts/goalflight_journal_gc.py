#!/usr/bin/env python3
"""Report (or delete) per-project journals whose recorded root is gone.

Report-only by default. ``--apply`` is required to delete anything.

Five states, not three — the codedb reaper originally conflated "the marker
is not there" with "the marker is there and I could not read it" via a bare
``except OSError`` / ``Path.exists()``. Absence of proof is never proof of
absence:

  live        recorded project_root exists
  root-gone   recorded project_root is absent (FileNotFoundError only)
  orphaned    root-gone AND retained only by non-terminal dispatch records
              that can never reconcile (see below); NOT reclaimed
  empty       journal holds no domain data AND its recorded root is gone
  unknown     root or contents unverifiable; NEVER reclaimed

A journal may be dead by root and still referenced. An ACTIVE lease with a
live holder retains it, as does a lease whose holder liveness we cannot
determine. An unreadable journal is unknown: we cannot prove it is
unreferenced. Quarantine sidecars matching ``.dev-casualty-<stamp>`` are
their own category and are never deleted.

Orphaned non-terminal dispatch records: dispatch reconciliation is driven
from the project root, so once the root is *proven* gone the workers are
dead, nothing will ever transition PREPARED/STARTING/RUNNING records, and a
plain retention guard would pin the journal forever — circularly
unreclaimable. Those journals are surfaced as ``orphaned``: visibly stuck,
distinct from both "reclaimable" and "retained because live work references
it". They are NOT deleted: the records are history, and the guard against
deleting referenced journals is unchanged whenever the root still exists (or
cannot be proven gone — ``_classify_root`` returns ``unknown`` for any
OSError that is not FileNotFoundError, so "cannot tell" never folds into
"gone").

DECISION (terminalizing orphans): the diagnosis above is not the cure. The
cure is a reconcile pass that transitions these records to ATTEMPT_ABANDONED
via the journal's existing ``commit_terminal(..., terminal="abandoned")``
path, gated on the same FileNotFoundError-only proof of a gone root. That
pass must run from the journal side, not the project root. It is NOT
implemented here: terminalizing writes domain state (outbox events, ledger
transitions) that shared-machine controllers and the fleet ledger drain, and
choosing the terminal token and observation payload is a controller-level
decision, new authority this report/reclaim tool does not hold. Recommended:
add an opt-in ``--terminalize-orphans`` (or fleet-reconciler equivalent) that
only touches records whose root is proven gone, then the journal ages into
the ordinary root-gone/reclaimable path.

Re-verify the classification immediately before deleting. The scan is a
snapshot; state can move between listing and acting.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat as statmod
import sys

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import goalflight_journal as journal  # noqa: E402
import goalflight_wake as wake  # noqa: E402


SCHEMA = "goalflight.journal-gc.v2"
CASUALTY_MARKER = ".dev-casualty-"
_UNSTABLE_PREFIXES = ("/Volumes/", "/net/", "/mnt/", "/media/")
_BOOKKEEPING_TABLES = frozenset(
    {
        "journal_meta",
        "journal_epochs",
        "journal_migrations",
        "sqlite_sequence",
        "sqlite_stat1",
        "sqlite_stat4",
    }
)
_LIVE_ATTEMPT_STATES = frozenset(journal.ATTEMPT_LIVE_STATES)
_SQLITE_NAME = journal.JOURNAL_FILE_NAME


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


def dir_bytes(path: Path) -> int:
    total = 0
    for dirpath, _dirs, files in os.walk(path, onerror=lambda _e: None):
        for name in files:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                continue
    return total


def _entry(
    journal_dir: Path,
    *,
    state: str,
    why: str,
    root: str | None = None,
    reclaimable: bool = False,
    roots: list[str] | None = None,
) -> dict:
    payload = {
        "journal": str(journal_dir),
        "root": root,
        "state": state,
        "reclaimable": reclaimable,
        "why": why,
    }
    if roots is not None:
        payload["roots"] = list(roots)
    return payload


def _keep_unknown(
    journal_dir: Path,
    why: str,
    root: str | None = None,
    roots: list[str] | None = None,
) -> dict:
    return _entry(
        journal_dir,
        state="unknown",
        why=why,
        root=root,
        roots=roots,
        reclaimable=False,
    )


def _is_casualty_name(name: str) -> bool:
    return CASUALTY_MARKER in name


def _open_readonly(sqlite_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(
        f"file:{sqlite_path}?mode=ro",
        uri=True,
        timeout=0,
    )


def _table_names(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    return {str(row[0]) for row in rows}


def _count_rows(connection: sqlite3.Connection, table: str) -> int | None:
    if not table.isidentifier() or table.startswith("sqlite_"):
        return None
    try:
        row = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    except sqlite3.Error:
        return None
    return int(row[0]) if row is not None else None


def _distinct_roots(connection: sqlite3.Connection, table: str) -> list[str] | None:
    if table not in _table_names(connection):
        return []
    try:
        rows = connection.execute(
            f"SELECT DISTINCT project_root FROM {table}"
        ).fetchall()
    except sqlite3.Error:
        return None
    roots: list[str] = []
    seen: set[str] = set()
    for row in rows:
        value = "" if row[0] is None else str(row[0]).strip()
        if value and value not in seen:
            seen.add(value)
            roots.append(value)
    return roots


def _union_distinct_roots(*groups: list[str] | None) -> list[str] | None:
    """Union recorded project_root values across tables. None if any group failed."""
    if any(group is None for group in groups):
        return None
    seen: set[str] = set()
    roots: list[str] = []
    for group in groups:
        for value in group:
            if value not in seen:
                seen.add(value)
                roots.append(value)
    return roots


def _considered_roots(entry: dict) -> list[str]:
    """Distinct project_root values this classification considered."""
    raw = entry.get("roots")
    if not isinstance(raw, list):
        raw = [entry["root"]] if entry.get("root") else []
    seen: set[str] = set()
    roots: list[str] = []
    for item in raw:
        value = "" if item is None else str(item).strip()
        if value and value not in seen:
            seen.add(value)
            roots.append(value)
    return roots


def _roots_annotation(entry: dict) -> str:
    considered = _considered_roots(entry)
    if considered:
        return "  roots=" + ", ".join(considered)
    return ""


def _domain_row_count(connection: sqlite3.Connection, tables: set[str]) -> int | None:
    total = 0
    for table in tables:
        if table in _BOOKKEEPING_TABLES or table.startswith("sqlite_"):
            continue
        counted = _count_rows(connection, table)
        if counted is None:
            return None
        total += counted
    return total


def _holder_retain_reason(root: str, label: str, nonce: str) -> str | None:
    """Return a retain reason if a live holder is proven or unverifiable.

    ``lease_holder_alive`` answers None when the lock path cannot be opened,
    including FileNotFoundError. A missing lock is not a live holder; an
    unreadable lock (the file is there and we could not look) is unknown.
    """
    try:
        alive = wake.lease_holder_alive(
            root,
            controller_label=label,
            lease_nonce=nonce,
            prune_dead=False,
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return (
            f"ACTIVE lease holder liveness indeterminate "
            f"(label={label!r}, {exc.__class__.__name__})"
        )
    if alive is True:
        return f"ACTIVE lease with a live holder (label={label})"
    if alive is False:
        return None
    try:
        # The public probe returns None for both "lock is gone" and "lock is
        # there and I could not look". The lock path is the distinguishing
        # marker; lstat separates FileNotFoundError from other OSError.
        identity = wake._lease_lock_identity(label, nonce)
        lock_path = wake._generation_lock_path(
            root,
            kind=wake.LEASE_KIND,
            label=identity,
            generation_key=nonce,
        )
    except ValueError:
        return f"ACTIVE lease holder identity invalid (label={label!r})"
    lock_state = _presence(lock_path)
    dir_state = _presence(lock_path.parent)
    if lock_state == "present" or lock_state == "unknown" or dir_state == "unknown":
        return f"ACTIVE lease holder liveness unknown (label={label})"
    return None


def _reference_reasons(
    connection: sqlite3.Connection, tables: set[str]
) -> dict[str, list[str]] | None:
    """Return retain-reasons by kind, or None if unverifiable.

    ``lease`` reasons are live-holder or liveness-indeterminate evidence:
    they retain regardless of root state. ``dispatch`` reasons are
    non-terminal dispatch records; when the root is *proven* gone those are
    orphans (reconciliation runs from the project root, so nothing can ever
    transition them), not evidence of live work.
    """
    reasons: dict[str, list[str]] = {"lease": [], "dispatch": []}
    if "controller_leases" in tables:
        try:
            rows = connection.execute(
                "SELECT project_root, label, nonce FROM controller_leases "
                "WHERE state = ?",
                (journal.LEASE_ACTIVE,),
            ).fetchall()
        except sqlite3.Error:
            return None
        for project_root, label, nonce in rows:
            root = "" if project_root is None else str(project_root)
            lease_label = "" if label is None else str(label)
            lease_nonce = "" if nonce is None else str(nonce)
            reason = _holder_retain_reason(root, lease_label, lease_nonce)
            if reason:
                reasons["lease"].append(reason)
    if "dispatch_attempts" in tables:
        try:
            rows = connection.execute(
                "SELECT dispatch_id, lifecycle_state FROM dispatch_attempts"
            ).fetchall()
        except sqlite3.Error:
            return None
        for dispatch_id, lifecycle_state in rows:
            state = "" if lifecycle_state is None else str(lifecycle_state)
            if state in _LIVE_ATTEMPT_STATES:
                reasons["dispatch"].append(
                    f"non-terminal dispatch "
                    f"(dispatch_id={dispatch_id}, state={state})"
                )
    return reasons


def _classify_root(root: str) -> tuple[str, str]:
    """Return (state, why) for one recorded root path."""
    if not os.path.isabs(root):
        return "unknown", "recorded project_root is not an absolute path"
    if root.startswith(_UNSTABLE_PREFIXES):
        return (
            "unknown",
            "root on a mount that may be detached rather than deleted",
        )
    try:
        os.stat(root)
    except FileNotFoundError:
        return "root_gone", "root no longer exists"
    except OSError as exc:
        return (
            "unknown",
            f"root could not be checked ({exc.__class__.__name__}), "
            "so absence is unverified",
        )
    return "live", "root still exists"


def classify(journal_dir: Path) -> dict:
    """Classify one journal directory. Unverifiable is never reclaimable."""
    name = journal_dir.name
    if _is_casualty_name(name):
        return _entry(
            journal_dir,
            state="casualty",
            why="quarantine sidecar, not a live journal",
            reclaimable=False,
        )

    try:
        st = os.lstat(journal_dir)
    except FileNotFoundError:
        return _keep_unknown(journal_dir, "journal directory disappeared during scan")
    except OSError as exc:
        return _keep_unknown(
            journal_dir,
            f"journal directory unreadable ({exc.__class__.__name__})",
        )
    if statmod.S_ISLNK(st.st_mode):
        return _keep_unknown(journal_dir, "journal path is a symlink")
    if not statmod.S_ISDIR(st.st_mode):
        return _keep_unknown(journal_dir, "journal path is not a directory")

    sqlite_path = journal_dir / _SQLITE_NAME
    sqlite_state = _presence(sqlite_path)
    if sqlite_state == "unknown":
        return _keep_unknown(
            journal_dir,
            "journal sqlite presence unverifiable, so contents are unknown",
        )

    wal_state = _presence(Path(f"{sqlite_path}-wal"))
    shm_state = _presence(Path(f"{sqlite_path}-shm"))
    if "unknown" in {wal_state, shm_state}:
        return _keep_unknown(
            journal_dir,
            "journal sqlite sidecar presence unverifiable",
        )

    if sqlite_state == "absent":
        if wal_state == "present" or shm_state == "present":
            return _keep_unknown(
                journal_dir,
                "sqlite file absent but WAL/SHM present, contents unknown",
            )
        lock_path = journal.journal_write_lock_path(sqlite_path)
        lock_state = _presence(lock_path)
        if lock_state == "unknown":
            return _keep_unknown(
                journal_dir,
                "journal write-lock presence unverifiable",
            )
        if lock_state == "present":
            return _keep_unknown(
                journal_dir,
                "sqlite file absent but write lock present, create in progress",
            )
        return _entry(
            journal_dir,
            state="empty",
            why="journal holds no data",
            reclaimable=True,
        )

    try:
        connection = _open_readonly(sqlite_path)
    except (OSError, sqlite3.Error) as exc:
        return _keep_unknown(
            journal_dir,
            f"journal sqlite unreadable ({exc.__class__.__name__}), "
            "so absence and references are unverifiable",
        )
    try:
        connection.row_factory = sqlite3.Row
        try:
            tables = _table_names(connection)
            domain_rows = _domain_row_count(connection, tables)
            lease_roots = _distinct_roots(connection, "controller_leases")
            attempt_roots = _distinct_roots(connection, "dispatch_attempts")
            references = _reference_reasons(connection, tables)
        except sqlite3.Error as exc:
            return _keep_unknown(
                journal_dir,
                f"journal sqlite unreadable ({exc.__class__.__name__}), "
                "so absence and references are unverifiable",
            )
    finally:
        connection.close()

    if (
        domain_rows is None
        or lease_roots is None
        or attempt_roots is None
        or references is None
    ):
        return _keep_unknown(
            journal_dir,
            "journal sqlite unreadable, so absence and references are unverifiable",
        )

    # Union across tables, then classify the root, before treating an
    # empty sqlite as reclaimable. A create-only journal has no recorded
    # project_root: that is unknown, not empty.
    roots = _union_distinct_roots(lease_roots, attempt_roots)
    if not roots:
        return _keep_unknown(
            journal_dir,
            "no recorded project_root - root unknown, so absence is unverifiable",
        )
    if len(roots) != 1:
        return _keep_unknown(
            journal_dir,
            "multiple recorded project_root values ("
            + ", ".join(roots)
            + "), so the live root is unverifiable",
            roots=roots,
        )

    root = roots[0]
    state, why = _classify_root(root)
    if state == "live":
        return _entry(
            journal_dir,
            state="live",
            why=why,
            root=root,
            roots=roots,
            reclaimable=False,
        )
    if state == "unknown":
        return _keep_unknown(journal_dir, why, root=root, roots=roots)

    if domain_rows == 0:
        return _entry(
            journal_dir,
            state="empty",
            why="journal holds no data",
            root=root,
            roots=roots,
            reclaimable=True,
        )

    # root-gone: still refuse if referenced.
    lease_reasons = references["lease"]
    dispatch_reasons = references["dispatch"]
    if lease_reasons:
        # A live holder, or a holder whose liveness we cannot determine, is
        # live-work evidence (or can't-tell): retain, unchanged.
        return _entry(
            journal_dir,
            state="root_gone",
            why="; ".join(lease_reasons + dispatch_reasons),
            root=root,
            roots=roots,
            reclaimable=False,
        )
    if dispatch_reasons:
        # The root is proven gone and the ONLY references are non-terminal
        # dispatch records. Reconciliation is driven from the project root,
        # so these records can never transition: they are orphans, and
        # retaining under a live-work reason would pin the journal forever.
        # Surface as stuck — not reclaimable, not "retained because busy".
        return _entry(
            journal_dir,
            state="orphaned",
            why=(
                f"root gone with {len(dispatch_reasons)} orphaned "
                "non-terminal dispatch record(s): " + "; ".join(dispatch_reasons)
            ),
            root=root,
            roots=roots,
            reclaimable=False,
        )
    return _entry(
        journal_dir,
        state="root_gone",
        why=why,
        root=root,
        roots=roots,
        reclaimable=True,
    )


def journals_store() -> Path:
    return journal.journals_index_dir()


def scan(store: Path) -> list[dict]:
    entries: list[dict] = []
    try:
        children = sorted(store.iterdir(), key=lambda path: path.name)
    except FileNotFoundError:
        return []
    except OSError as exc:
        return [
            _keep_unknown(
                store,
                f"journal store unreadable ({exc.__class__.__name__})",
            )
        ]
    for child in children:
        kind = _presence(child)
        if kind == "unknown":
            entries.append(
                _keep_unknown(
                    child,
                    "journal directory presence unverifiable",
                )
            )
            continue
        if kind == "absent":
            continue
        try:
            st = os.lstat(child)
        except FileNotFoundError:
            continue
        except OSError as exc:
            entries.append(
                _keep_unknown(
                    child, f"journal directory unreadable ({exc.__class__.__name__})"
                )
            )
            continue
        if not statmod.S_ISDIR(st.st_mode) and not statmod.S_ISLNK(st.st_mode):
            continue
        entries.append(classify(child))
    return entries


def _counts(entries: list[dict]) -> dict[str, int]:
    # reclaimable / orphaned / retained partition the entries: orphaned
    # journals are stuck (never reclaimed, never busy) and are counted apart
    # from retained so the totals always add up at a glance.
    counts = {
        "live": 0,
        "root_gone": 0,
        "orphaned": 0,
        "empty": 0,
        "unknown": 0,
        "casualty": 0,
        "reclaimable": 0,
        "retained": 0,
    }
    for entry in entries:
        state = entry["state"]
        if state in counts:
            counts[state] += 1
        if entry["reclaimable"]:
            counts["reclaimable"] += 1
        elif state != "orphaned":
            counts["retained"] += 1
    return counts


def apply_deletes(entries: list[dict], *, limit: int) -> tuple[int, list[dict]]:
    targets = [entry for entry in entries if entry["reclaimable"]]
    if limit:
        targets = targets[:limit]
    deleted = 0
    failed: list[dict] = []
    for entry in targets:
        path = Path(entry["journal"])
        # Re-verify immediately before deleting: the listing above is a
        # snapshot, and a root that reappeared must not be reaped.
        current = classify(path)
        considered = _considered_roots(current)
        # Re-verify the cross-table union here, not only at scan: a stale
        # listing that named one gone root must not delete while another
        # recorded root is still live. Zero roots is the empty-journal case.
        if len(considered) > 1 or not current["reclaimable"]:
            failed.append(
                {
                    "journal": entry["journal"],
                    "error": "no longer reclaimable",
                    "why": (
                        "multiple recorded project_root values ("
                        + ", ".join(considered)
                        + "), so the live root is unverifiable"
                        if len(considered) > 1
                        else current["why"]
                    ),
                }
            )
            continue
        try:
            shutil.rmtree(path)
            deleted += 1
        except OSError as exc:
            failed.append({"journal": entry["journal"], "error": str(exc)})
    return deleted, failed


def format_human(
    store: Path,
    entries: list[dict],
    *,
    applied: bool,
    deleted: int,
    failed: list[dict],
) -> str:
    counts = _counts(entries)
    lines = [
        f"journal store: {store}",
        f"  live        : {counts['live']}",
        f"  root-gone   : {counts['root_gone']}",
        f"  orphaned    : {counts['orphaned']}",
        f"  empty       : {counts['empty']}",
        f"  unknown     : {counts['unknown']}",
        f"  casualty    : {counts['casualty']}",
        f"  reclaimable : {counts['reclaimable']}",
        f"  retained    : {counts['retained']}",
    ]
    reclaimable = [e for e in entries if e["reclaimable"]]
    orphaned = [e for e in entries if e["state"] == "orphaned"]
    retained = [
        e for e in entries if not e["reclaimable"] and e["state"] != "orphaned"
    ]
    if reclaimable:
        lines.append("\n  reclaimable journals:")
        for entry in reclaimable:
            root = _roots_annotation(entry)
            lines.append(
                f"    {entry['state']:<10} {entry['journal']}{root}  why={entry['why']}"
            )
    if orphaned:
        lines.append(
            "\n  stuck - root gone with orphaned non-terminal records"
            " (never reclaimed; nothing left to reconcile them):"
        )
        for entry in orphaned:
            root = _roots_annotation(entry)
            lines.append(
                f"    {entry['state']:<10} {entry['journal']}{root}  why={entry['why']}"
            )
    if retained:
        lines.append("\n  retained:")
        for entry in retained:
            root = _roots_annotation(entry)
            lines.append(
                f"    {entry['state']:<10} {entry['journal']}{root}  why={entry['why']}"
            )
    if applied:
        lines.append(f"\n  deleted {deleted} journal(s)")
        for item in failed:
            lines.append(f"    FAILED {item['journal']}: {item['error']}")
    elif counts["reclaimable"]:
        lines.append("\n  dry run - nothing deleted. Re-run with --apply to reclaim.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Report (or delete) per-project journals whose recorded root is gone."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete reclaimable journals. Default is dry-run.",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON.")
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Delete at most N reclaimable journals (0 = no limit).",
    )
    args = parser.parse_args(argv)

    store = journals_store()
    store_presence = _presence(store)
    if store_presence == "absent":
        report = {
            "schema": SCHEMA,
            "store": str(store),
            "live": 0,
            "root_gone": 0,
            "orphaned": 0,
            "empty": 0,
            "unknown": 0,
            "casualty": 0,
            "reclaimable": 0,
            "retained": 0,
            "applied": bool(args.apply),
            "deleted": 0,
            "failed": [],
            "entries": [],
        }
        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            print(f"no journal store at {store}")
        return 0
    if store_presence == "unknown":
        print(f"journal store unreadable at {store}", file=sys.stderr)
        return 1
    try:
        mode = os.lstat(store).st_mode
    except FileNotFoundError:
        print(f"no journal store at {store}")
        return 0
    except OSError as exc:
        print(f"journal store unreadable at {store} ({exc.__class__.__name__})", file=sys.stderr)
        return 1
    if not statmod.S_ISDIR(mode):
        print(f"journal store is not a directory: {store}", file=sys.stderr)
        return 1

    if args.apply and args.limit < 0:
        print("--limit must be >= 0 (0 means no limit)", file=sys.stderr)
        return 1

    entries = scan(store)
    for entry in entries:
        if entry["reclaimable"] or entry["state"] in {
            "root_gone",
            "orphaned",
            "empty",
            "casualty",
        }:
            entry["bytes"] = dir_bytes(Path(entry["journal"]))

    deleted = 0
    failed: list[dict] = []
    if args.apply:
        deleted, failed = apply_deletes(entries, limit=args.limit)

    counts = _counts(entries)
    report = {
        "schema": SCHEMA,
        "store": str(store),
        "applied": bool(args.apply),
        "deleted": deleted,
        "failed": failed,
        **counts,
        "entries": entries,
    }
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0

    print(format_human(store, entries, applied=bool(args.apply), deleted=deleted, failed=failed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
