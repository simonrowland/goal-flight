#!/usr/bin/env python3
"""Fleet-wide controller diagnostic: connected / idle / stale / unknown.

Current fleet state is the ACTIVE generation per (project, label). Ended
lease rows (EXPIRED / SUPERSEDED / RETIRED) are history, not members.
``disconnected`` is empty by construction: a cleanly released label has no
row, and an ACTIVE-but-holder-dead generation is ``stale``.

``unknown`` is the could-not-tell bucket (unreadable probes, unresolvable
roots, a label found only in a non-official journal file, or two journals
naming the same member differently). It never carries a retire command.
``stale`` is the action-bearing alarm.

Identity for a current fleet member is ``(official journal path, label)``.
A second index folder that git-canonicalizes to a live checkout is not
that project's roster.

    python3 scripts/goalflight_controllers.py
    python3 scripts/goalflight_controllers.py --json
    python3 scripts/goalflight_controllers.py --idle-hours 4
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(ROOT))

import goalflight_journal
import goalflight_session_status as sessions
import goalflight_task
import goalflight_wake


SCHEMA = "goalflight.controller-fleet.v1"
DEFAULT_IDLE_HOURS = 4.0
TABLE_COLUMNS = (
    "BUCKET",
    "LABEL",
    "PROJECT",
    "STATE",
    "IDLE",
    "UNREAD",
    "OWNED",
    "SUPERVISOR",
)
JSON_ROW_KEYS = (
    "bucket",
    "label",
    "project",
    "project_root",
    "state",
    "idle_seconds",
    "idle",
    "unread",
    "last_drain_at",
    "last_drain_seconds",
    "owned",
    "supervisor",
    "wake_armed",
    "occupies",
    "retire_command",
    "unknown_reason",
    "renew_hint",
    "retirement_eligible",
    "retirement_reason",
)
BUCKET_ORDER = {
    "stale": 0,
    "unknown": 1,
    "connected": 2,
    "idle": 3,
    None: 4,
}
HOLDER_LIVE = frozenset({"live-lock", "live-overdue"})
HOLDER_DEAD = frozenset({"dead-lock", "ended"})
LIVE_DISPLAY_STATES = frozenset({"live", "live-overdue"})
UNKNOWN_PROJECT = "unknown"
RENEW_HINT = "lease overdue — renew (--join)"
_RETIREMENT_REFUSED = "; retirement refused until it resolves"
_JOURNAL_SLUG_HASH = re.compile(r"^(.+)-([0-9a-f]{10})$")
_GENERIC_JOURNAL_SLUGS = frozenset({"project", "root", "repo", "tmp", "Users", "user"})
_CONTENTLESS_LABELS = frozenset({"", "unknown", "—"})


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def peek_journal_project_roots(path: Path) -> tuple[list[str] | None, str | None]:
    """Read-only peek of ACTIVE project_root values. ``(None, error)`` on fail."""
    pairs, error = peek_active_lease_identities(path)
    if pairs is None:
        return None, error
    roots: list[str] = []
    seen: set[str] = set()
    for root, _label in pairs:
        if root not in seen:
            seen.add(root)
            roots.append(root)
    return roots, None


def peek_active_lease_identities(
    path: Path,
) -> tuple[list[tuple[str, str]] | None, str | None]:
    """Read-only sqlite peek of current-generation (project_root, label) pairs."""
    connection: sqlite3.Connection | None = None
    try:
        connection = goalflight_journal._open_readonly_connection(
            path,
            timeout=0,
            isolation_level=None,
        )
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA busy_timeout = 0")
        rows = connection.execute(
            "SELECT DISTINCT project_root, label FROM controller_leases "
            "WHERE state = 'ACTIVE'"
        ).fetchall()
        pairs: list[tuple[str, str]] = []
        for row in rows:
            root = row[0]
            label = row[1]
            if isinstance(root, str) and root and isinstance(label, str) and label:
                pairs.append((root, label))
        return pairs, None
    except goalflight_journal.JournalBusy:
        return None, "JournalBusy"
    except goalflight_journal.JournalError as exc:
        return None, type(exc).__name__
    except sqlite3.OperationalError as exc:
        message = str(exc).lower()
        if any(marker in message for marker in ("locked", "busy")):
            return None, "JournalBusy"
        return None, type(exc).__name__
    except sqlite3.Error as exc:
        return None, type(exc).__name__
    except OSError as exc:
        return None, type(exc).__name__
    finally:
        if connection is not None:
            connection.close()


def canonical_project_root(raw: Path | str | None) -> Path | None:
    """Git-canonical project root, or None when the path is not a project."""
    if raw is None:
        return None
    start = Path(str(raw)).expanduser()
    if not start.is_dir():
        return None
    git_root = goalflight_task._git_canonical_root(start)
    if git_root is None:
        return None
    return goalflight_task._strip_managed_worktree(git_root)


def project_identity(raw: Path | str | None) -> tuple[str, Path | None, str]:
    """Return ``(group_key, canonical_root, display_name)``.

    Display is the canonical checkout's name. An unresolvable root is
    ``unknown``, never a path segment of the recorded value.
    """
    canonical = canonical_project_root(raw)
    if canonical is None:
        return UNKNOWN_PROJECT, None, UNKNOWN_PROJECT
    resolved = canonical.resolve(strict=False)
    name = resolved.name.strip() or UNKNOWN_PROJECT
    return f"project:{resolved}", resolved, name


def journal_display_name(journal_path: Path | None) -> str | None:
    """Index-folder identity: ``<repo-slug>-<10-hex>`` without inventing a path segment."""
    if journal_path is None:
        return None
    folder = journal_path.parent.name.strip()
    if not folder:
        return None
    match = _JOURNAL_SLUG_HASH.fullmatch(folder)
    if match:
        slug = match.group(1)
        if slug in _GENERIC_JOURNAL_SLUGS:
            return folder
        return slug
    return folder or None


def official_journal_path(project_root: Path | str | None) -> Path | None:
    """The journal ``resolve_journal_path`` names for this project, if any."""
    if project_root is None:
        return None
    try:
        return goalflight_journal.resolve_journal_path(project_root)
    except (OSError, RuntimeError, TypeError, ValueError):
        return None


def journal_file_is_official(
    path: Path, project_root: Path | str | None
) -> bool:
    """True when ``path`` is the journal the project resolver names.

    Discovery lists every ``*/state-journal.sqlite3``. Official membership
    is that resolver, not a folder-name heuristic.
    """
    official = official_journal_path(project_root)
    if official is None:
        return True
    return path.resolve(strict=False) == official.resolve(strict=False)


def resolve_journal_project(
    pairs: list[tuple[str, str]],
) -> tuple[Path | None, Path | str | None]:
    """Pick one project for a journal from its ACTIVE lease identities.

    A resolvable git root wins so every label in the journal shares that
    project, including leases whose stored ``project_root`` is junk. A
    directory that exists is the fallback probe target when nothing
    canonicalizes.
    """
    any_fallback: Path | str | None = None
    dir_fallback: Path | str | None = None
    for raw_root, _label in pairs:
        if any_fallback is None:
            any_fallback = raw_root
        path = Path(str(raw_root)).expanduser()
        if dir_fallback is None and path.is_dir():
            dir_fallback = raw_root
        _key, canonical, _display = project_identity(raw_root)
        if canonical is not None:
            return canonical, raw_root
    if dir_fallback is not None:
        return None, dir_fallback
    return None, any_fallback


def is_contentless_row(row: dict[str, Any]) -> bool:
    """True when the row names no controller and no project.

    An identified unknown (named journal/project, unknown measurements) is
    information. An anonymous all-unknown row is noise and must not emit.
    """
    label = row.get("label")
    if isinstance(label, str) and label.strip() and label.strip() not in _CONTENTLESS_LABELS:
        return False
    project = row.get("project")
    if (
        isinstance(project, str)
        and project.strip()
        and project.strip() != UNKNOWN_PROJECT
    ):
        return False
    return True


def holder_state(incarnation_state: object) -> str:
    """Map roster incarnation to the fleet STATE column.

    ``live-overdue`` is a live holder whose renewal horizon has passed.
    Keep it distinct from ``live`` (the operator action is renew, not
    investigate) and never collapse it into ``unknown`` or ``dead``.
    """
    token = str(incarnation_state or "")
    if token == "live-overdue":
        return "live-overdue"
    if token in HOLDER_LIVE:
        return "live"
    if token in HOLDER_DEAD:
        return "dead"
    return "unknown"


def is_live_state(state: object) -> bool:
    return str(state or "") in LIVE_DISPLAY_STATES


def supervisor_display(*, armed: bool | None, supervisor: object) -> str:
    if armed is True:
        return "armed"
    token = str(supervisor or "")
    if armed is False and token == goalflight_wake.SUPERVISOR_ABSENT:
        return "absent"
    if armed is False:
        return "unarmed"
    return "unknown"


def classify_bucket(
    record: dict[str, Any],
    *,
    idle_hours: float,
) -> str | None:
    """Current-generation buckets. Ended leases are not fleet members.

    ``stale`` is provably-dead-and-occupying (the action-bearing alarm).
    Holder-unknown occupying rows go to ``unknown``, not ``stale``, so a
    could-not-tell probe cannot inflate the alarm bucket or carry retire.
    """
    state = holder_state(record.get("incarnation_state"))
    retired = bool(record.get("retired"))
    occupies = not retired
    armed = record.get("wake_armed")
    idle_s = record.get("idle_seconds")
    threshold_s = idle_hours * 3600.0
    if retired or not occupies:
        return None
    if is_live_state(state) and armed is True:
        if idle_s is None:
            # Live and armed, but the idle classifier cannot run. Connected
            # with IDLE=unknown, never a fake idle verdict.
            return "connected"
        if idle_s > threshold_s:
            return "idle"
        return "connected"
    if is_live_state(state):
        # Holder is live (including live-overdue); supervisor may be
        # absent or unknown. Not stale, not unknown.
        return "connected"
    if state == "unknown":
        return "unknown"
    if state == "dead":
        return "stale"
    return "unknown"


def retire_command(project_root: Path | str, label: str) -> str:
    return (
        "python3 scripts/goalflight_session_status.py --project-root "
        f"{_shell_token(project_root)} --retire {_shell_token(label)} "
        "--acknowledge-retirement"
    )


def _shell_token(value: object) -> str:
    return shlex.quote(str(value))


def retire_command_project_root(command: str) -> str | None:
    """Extract ``--project-root`` from an emitted retire command."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    for index, token in enumerate(tokens):
        if token == "--project-root" and index + 1 < len(tokens):
            return tokens[index + 1]
    return None


def _project_display_name(canonical: Path | None) -> str:
    if canonical is None:
        return UNKNOWN_PROJECT
    name = canonical.name.strip()
    return name or UNKNOWN_PROJECT


def _unknown_reason(record: dict[str, Any], *, state: str, bucket: str | None) -> str | None:
    parts: list[str] = []
    incarnation = str(record.get("incarnation_state") or "")
    if state == "unknown":
        parts.append(f"holder {incarnation or 'unknown-lock'}")
    if record.get("wake_armed") is None:
        supervisor = str(record.get("supervisor") or "unknown")
        covered = record.get("wake_covered")
        parts.append(f"supervisor {supervisor}, coverage {covered}")
    if record.get("unread_addressed_mail") is None:
        parts.append("unread unmeasured")
    if record.get("nonterminal_owned_dispatches") is None:
        parts.append("owned unmeasured")
    if record.get("idle_seconds") is None and bucket in {
        "connected",
        "idle",
        "stale",
        "unknown",
    }:
        parts.append("idle age unmeasured")
    if not parts:
        return None
    return "; ".join(parts) + "; retirement refused until it resolves"


def _idle_cell(record: dict[str, Any], *, bucket: str | None) -> str:
    del bucket
    compact = record.get("idle_compact") or sessions._format_idle_compact(
        record.get("idle_seconds")
    )
    return f"idle {compact}"


def live_row_may_not_retire(row: dict[str, Any]) -> bool:
    """Hard invariant: a live current generation never carries retire."""
    return not (is_live_state(row.get("state")) and row.get("retire_command"))


def retire_command_is_canonical(row: dict[str, Any]) -> bool:
    """Emitted ``--project-root`` must round-trip through the project resolver."""
    command = row.get("retire_command")
    if not command:
        return True
    parsed = retire_command_project_root(str(command))
    if not parsed:
        return False
    canonical = canonical_project_root(parsed)
    if canonical is None:
        return False
    reported = row.get("project_root")
    if not reported:
        return False
    reported_canonical = canonical_project_root(str(reported))
    if reported_canonical is None:
        return False
    resolved = goalflight_task.resolve_project_root(parsed)
    return (
        resolved.resolve(strict=False) == canonical.resolve(strict=False)
        and reported_canonical.resolve(strict=False) == canonical.resolve(strict=False)
    )


def _may_emit_retire(
    *,
    bucket: str | None,
    state: str,
    occupies: bool,
    eligible: bool,
    canonical: Path | None,
) -> bool:
    if canonical is None:
        return False
    if is_live_state(state):
        return False
    if bucket != "stale" or state != "dead" or not occupies:
        return False
    return eligible is True


def fleet_row(
    record: dict[str, Any],
    *,
    canonical: Path | None,
    idle_hours: float,
    journal_name: str | None = None,
    recorded_root: Path | str | None = None,
) -> dict[str, Any]:
    state = holder_state(record.get("incarnation_state"))
    bucket = classify_bucket(record, idle_hours=idle_hours)
    occupies = not bool(record.get("retired"))
    eligible = record.get("retirement_eligible") is True
    show_retire = _may_emit_retire(
        bucket=bucket,
        state=state,
        occupies=occupies,
        eligible=eligible,
        canonical=canonical,
    )
    label = str(record.get("label") or "") or None
    unknown_reason = None
    if state == "unknown" or bucket in {None, "unknown"}:
        unknown_reason = _unknown_reason(record, state=state, bucket=bucket)
        show_retire = False
        if unknown_reason is None:
            unknown_reason = (
                "state unknown; retirement refused until it resolves"
            )
    if canonical is None:
        recorded = str(recorded_root) if recorded_root is not None else None
        if recorded:
            extra = f"unresolvable root {recorded}"
            base = (unknown_reason or "").removesuffix(
                "; retirement refused until it resolves"
            )
            unknown_reason = (
                f"{base}; {extra}; retirement refused until it resolves"
                if base
                else f"{extra}; retirement refused until it resolves"
            )
    display = _project_display_name(canonical)
    renew_hint = RENEW_HINT if state == "live-overdue" else None
    return {
        "bucket": bucket if bucket is not None else "unknown",
        "label": label,
        "project": display,
        "project_root": str(canonical) if canonical is not None else None,
        "state": state,
        "idle_seconds": record.get("idle_seconds"),
        "idle": _idle_cell(record, bucket=bucket),
        "unread": record.get("unread_addressed_mail"),
        "last_drain_at": record.get("last_drain_at"),
        "last_drain_seconds": record.get("last_drain_seconds"),
        "owned": record.get("nonterminal_owned_dispatches"),
        "supervisor": supervisor_display(
            armed=record.get("wake_armed"),
            supervisor=record.get("supervisor"),
        ),
        "wake_armed": record.get("wake_armed"),
        "occupies": occupies,
        "retire_command": (
            retire_command(canonical, label)
            if show_retire and label and canonical is not None
            else None
        ),
        "unknown_reason": unknown_reason,
        "renew_hint": renew_hint,
        "retirement_eligible": record.get("retirement_eligible"),
        "retirement_reason": record.get("retirement_reason"),
        "_source": journal_name,
    }


def _with_retirement_refused(reason: str) -> str:
    if reason.endswith(_RETIREMENT_REFUSED):
        return reason
    return reason + _RETIREMENT_REFUSED


def _journal_where(journal_path: Path | None) -> str:
    return f" at {journal_path}" if journal_path is not None else ""


def unknown_member_row(
    *,
    label: str | None,
    project_root: Path | str | None,
    journal_path: Path | None,
    reason: str,
    retirement_reason: str | None = None,
) -> dict[str, Any]:
    """Could-not-tell row. ``reason`` must state what was actually true."""
    canonical = canonical_project_root(project_root)
    display = _project_display_name(canonical)
    if display == UNKNOWN_PROJECT:
        named = journal_display_name(journal_path)
        if named:
            display = named
    source = journal_path.parent.name if journal_path is not None else None
    return {
        "bucket": "unknown",
        "label": label,
        "project": display,
        "project_root": str(canonical) if canonical is not None else None,
        "state": "unknown",
        "idle_seconds": None,
        "idle": "idle unknown",
        "unread": None,
        "last_drain_at": None,
        "last_drain_seconds": None,
        "owned": None,
        "supervisor": "unknown",
        "wake_armed": None,
        "occupies": None,
        "retire_command": None,
        "unknown_reason": _with_retirement_refused(reason),
        "renew_hint": None,
        "retirement_eligible": None,
        "retirement_reason": retirement_reason,
        "_source": source,
    }


def unknown_project_row(
    *,
    project_root: Path | str | None,
    journal_path: Path | None,
    error: str,
) -> dict[str, Any]:
    where = _journal_where(journal_path)
    return unknown_member_row(
        label=None,
        project_root=project_root,
        journal_path=journal_path,
        reason=f"journal unreadable ({error}){where}",
        retirement_reason=error,
    )


def labeled_unknown_row(
    label: str,
    *,
    project_root: Path | str | None,
    journal_path: Path | None,
    reason: str,
) -> dict[str, Any]:
    """Keep a peeked label with an honest could-not-tell reason.

    Do not wrap a readable journal or a resolved root in the
    ``journal unreadable`` / ``unresolvable`` template.
    """
    where = _journal_where(journal_path)
    text = reason if not where or reason.endswith(where) else f"{reason}{where}"
    return unknown_member_row(
        label=label,
        project_root=project_root,
        journal_path=journal_path,
        reason=text,
        retirement_reason=reason,
    )


def _row_group_key(row: dict[str, Any]) -> tuple[str, str] | None:
    """Identity is canonical root + label, else named journal + label.

    Two unresolvable journals that both render PROJECT ``unknown`` still
    collapse (same member, could-not-tell). Named leftover journals keep
    their folder identity so a peeked label is not dropped.
    """
    label = row.get("label")
    if not label:
        return None
    root = row.get("project_root")
    if root:
        return str(root), str(label)
    project = str(row.get("project") or UNKNOWN_PROJECT)
    return project, str(label)


def _row_facts(row: dict[str, Any]) -> tuple[object, object, object, object]:
    return (
        row.get("bucket"),
        row.get("state"),
        row.get("occupies"),
        row.get("supervisor"),
    )


def _disagreement_row(items: list[dict[str, Any]]) -> dict[str, Any]:
    sources = sorted(
        {
            str(item.get("_source") or item.get("project") or "journal")
            for item in items
        }
    )
    named = ", ".join(sources)
    first = items[0]
    project_roots = {item.get("project_root") for item in items}
    shared_root = project_roots.pop() if len(project_roots) == 1 else None
    canonical = canonical_project_root(shared_root) if shared_root else None
    return {
        "bucket": "unknown",
        "label": first.get("label"),
        "project": _project_display_name(canonical) if canonical is not None else UNKNOWN_PROJECT,
        "project_root": str(canonical) if canonical is not None else None,
        "state": "unknown",
        "idle_seconds": None,
        "idle": "idle unknown",
        "unread": None,
        "last_drain_at": None,
        "last_drain_seconds": None,
        "owned": None,
        "supervisor": "unknown",
        "wake_armed": None,
        "occupies": None,
        "retire_command": None,
        "unknown_reason": (
            f"journals disagree ({named}); retirement refused until it resolves"
        ),
        "renew_hint": None,
        "retirement_eligible": None,
        "retirement_reason": "journals_disagree",
        "_source": named,
    }


def merge_controller_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One (project, label) yields exactly one row."""
    unlabeled: list[dict[str, Any]] = []
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        key = _row_group_key(row)
        if key is None:
            unlabeled.append(row)
            continue
        grouped.setdefault(key, []).append(row)
    merged = list(unlabeled)
    for items in grouped.values():
        if len(items) == 1:
            merged.append(items[0])
            continue
        fact_set = {_row_facts(item) for item in items}
        if len(fact_set) == 1:
            merged.append(items[0])
            continue
        merged.append(_disagreement_row(items))
    return merged


def _sanitize_retire_commands(rows: list[dict[str, Any]]) -> None:
    for row in rows:
        if not live_row_may_not_retire(row) or not retire_command_is_canonical(row):
            row["retire_command"] = None


def _sort_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows.sort(
        key=lambda row: (
            BUCKET_ORDER.get(row.get("bucket"), 4),
            str(row.get("label") or ""),
            str(row.get("project") or ""),
            str(row.get("project_root") or ""),
        )
    )
    return rows


def _peeked_label_unknown_rows(
    pairs: list[tuple[str, str]],
    *,
    project_root: Path | str | None,
    journal_path: Path,
    reason: str,
) -> list[dict[str, Any]]:
    """Emit each peeked label instead of one unlabeled unknown row."""
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for _raw_root, label in pairs:
        if not label or label in seen:
            continue
        seen.add(label)
        rows.append(
            labeled_unknown_row(
                label,
                project_root=project_root,
                journal_path=journal_path,
                reason=reason,
            )
        )
    if not rows:
        rows.append(
            unknown_project_row(
                project_root=project_root,
                journal_path=journal_path,
                error="unreadable",
            )
        )
    return rows


def _non_official_journal_rows(
    pairs: list[tuple[str, str]],
    *,
    journal_path: Path,
    official_root: Path | str,
    skip_labels: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Labels that exist only in a non-official sibling journal.

    Official labels stay on the resolver's journal. Extra labels keep the
    source folder as PROJECT and never inherit the live checkout's name.
    """
    official_labels: set[str] = set(skip_labels or ())
    official = official_journal_path(official_root)
    if official is not None:
        official_pairs, _error = peek_active_lease_identities(official)
        if official_pairs:
            official_labels.update(label for _root, label in official_pairs)
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for _raw_root, label in pairs:
        if not label or label in seen or label in official_labels:
            continue
        seen.add(label)
        rows.append(
            labeled_unknown_row(
                label,
                project_root=None,
                journal_path=journal_path,
                reason="label found in a non-official journal file",
            )
        )
    return rows


def _official_extra_label_reason(raw_root: str) -> str:
    _key, label_canonical, _display = project_identity(raw_root)
    if label_canonical is None:
        return f"stored root {raw_root} did not resolve"
    return "label not in official roster"


def collect_controller_rows(
    *,
    idle_hours: float,
    ledger_records: list[dict] | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    roster_cache: dict[str, dict[str, Any] | BaseException] = {}
    files = goalflight_journal.iter_journal_files()
    for path in files:
        pairs, peek_error = peek_active_lease_identities(path)
        if pairs is None:
            rows.append(
                unknown_project_row(
                    project_root=None,
                    journal_path=path,
                    error=peek_error or "unreadable",
                )
            )
            continue
        if not pairs:
            # No ACTIVE generation — not a fleet member, not disconnected.
            continue
        canonical, recorded_root = resolve_journal_project(pairs)
        probe_root: Path | None
        if canonical is not None:
            probe_root = canonical
        elif recorded_root is not None:
            probe_root = Path(str(recorded_root))
        else:
            rows.append(
                unknown_project_row(
                    project_root=None,
                    journal_path=path,
                    error="unresolvable",
                )
            )
            continue
        if not journal_file_is_official(path, probe_root):
            already = {
                str(row.get("label"))
                for row in rows
                if row.get("label")
                and row.get("project_root") == str(probe_root)
            }
            rows.extend(
                _non_official_journal_rows(
                    pairs,
                    journal_path=path,
                    official_root=probe_root,
                    skip_labels=already,
                )
            )
            continue
        probe_token = str(probe_root)
        cached = roster_cache.get(probe_token)
        if cached is None:
            try:
                cached = sessions.controller_roster(
                    probe_root,
                    include_retired=False,
                    ledger_records=ledger_records,
                )
            except (
                goalflight_journal.JournalBusy,
                goalflight_journal.JournalDisappeared,
                goalflight_journal.JournalIOError,
                goalflight_journal.JournalUpgradeRequired,
                goalflight_journal.JournalError,
                OSError,
                RuntimeError,
                ValueError,
            ) as exc:
                cached = exc
            roster_cache[probe_token] = cached
        if isinstance(cached, BaseException):
            rows.extend(
                _peeked_label_unknown_rows(
                    pairs,
                    project_root=canonical,
                    journal_path=path,
                    reason=f"journal unreadable ({type(cached).__name__})",
                )
            )
            continue
        roster = cached
        measurements = roster.get("measurements") or {}
        registry = measurements.get("controller_registry") or {}
        if registry.get("measured") is False:
            rows.extend(
                _peeked_label_unknown_rows(
                    pairs,
                    project_root=canonical,
                    journal_path=path,
                    reason=(
                        "journal unreadable "
                        f"({registry.get('error') or 'unreadable'})"
                    ),
                )
            )
            continue
        seen_labels: set[str] = set()
        for record in roster.get("controllers") or []:
            if not isinstance(record, dict):
                continue
            if record.get("retired"):
                continue
            rows.append(
                fleet_row(
                    record,
                    canonical=canonical,
                    idle_hours=idle_hours,
                    journal_name=path.parent.name,
                    recorded_root=recorded_root,
                )
            )
            label = str(record.get("label") or "")
            if label:
                seen_labels.add(label)
        for raw_root, label in pairs:
            if not label or label in seen_labels:
                continue
            # ACTIVE in this official journal, missing from the roster.
            rows.append(
                labeled_unknown_row(
                    label,
                    project_root=canonical,
                    journal_path=path,
                    reason=_official_extra_label_reason(raw_root),
                )
            )
            seen_labels.add(label)
    rows = merge_controller_rows(rows)
    rows = [row for row in rows if not is_contentless_row(row)]
    _sanitize_retire_commands(rows)
    return _sort_rows(rows)


def render_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "no known controllers"
    counts: dict[str, int] = {
        "connected": 0,
        "idle": 0,
        "stale": 0,
        "unknown": 0,
    }
    display_rows: list[tuple[str, ...]] = []
    for row in rows:
        bucket = row.get("bucket")
        if bucket in counts:
            counts[bucket] += 1
        else:
            counts["unknown"] += 1
        unread = row.get("unread")
        unread_text = "unknown" if unread is None else _unread_cell_from_row(row)
        owned = row.get("owned")
        display_rows.append(
            (
                str(bucket or "unknown"),
                str(row.get("label") or "—"),
                str(row.get("project") or "unknown"),
                str(row.get("state") or "unknown"),
                str(row.get("idle") or "idle unknown"),
                unread_text,
                "unknown" if owned is None else str(owned),
                str(row.get("supervisor") or "unknown"),
            )
        )
    headers = TABLE_COLUMNS
    widths = [
        max(len(headers[index]), *(len(row[index]) for row in display_rows))
        for index in range(len(headers))
    ]
    lines = [
        "  ".join(value.ljust(widths[index]) for index, value in enumerate(headers))
    ]
    lines.extend(
        "  ".join(value.ljust(widths[index]) for index, value in enumerate(row))
        for row in display_rows
    )
    lines.append("---")
    lines.append(
        "controllers: "
        f"{len(rows)}  stale {counts['stale']} · unknown {counts['unknown']} · "
        f"connected {counts['connected']} · idle {counts['idle']}"
    )
    retire_lines = [
        str(row["retire_command"])
        for row in rows
        if row.get("retire_command")
    ]
    if retire_lines:
        lines.append("retire (proof of death):")
        lines.extend(f"  {command}" for command in retire_lines)
    unknown_lines = []
    for row in rows:
        reason = row.get("unknown_reason")
        if not reason:
            continue
        label = row.get("label") or "—"
        project = row.get("project") or "unknown"
        unknown_lines.append(f"  {label} @ {project}: {reason}")
    if unknown_lines:
        lines.append("unknown (retirement refused):")
        lines.extend(unknown_lines)
    renew_lines = []
    for row in rows:
        hint = row.get("renew_hint")
        if not hint:
            continue
        label = row.get("label") or "—"
        project = row.get("project") or "unknown"
        renew_lines.append(f"  {label} @ {project}: {hint}")
    if renew_lines:
        lines.append("renew (lease overdue):")
        lines.extend(renew_lines)
    return "\n".join(lines)


def _unread_cell_from_row(row: dict[str, Any]) -> str:
    unread = row.get("unread")
    if unread is None:
        return "unknown"
    drain_s = row.get("last_drain_seconds")
    if drain_s is None:
        return str(unread)
    return f"{unread} · drain {sessions._format_idle_compact(drain_s)}"


def build_payload(
    rows: list[dict[str, Any]],
    *,
    idle_hours: float,
) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "generated_at": _utc_now().isoformat(),
        "idle_hours": idle_hours,
        "last_drain_available": True,
        "controllers": [
            {key: row.get(key) for key in JSON_ROW_KEYS} for row in rows
        ],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "List current-generation controllers across every project journal: "
            "connected, idle, stale, or unknown."
        )
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="machine payload (stable keys); default is the aligned table",
    )
    parser.add_argument(
        "--idle-hours",
        type=float,
        default=DEFAULT_IDLE_HOURS,
        help=(
            "idle-bucket threshold in hours (default 4). Displayed IDLE age "
            "is always the raw heartbeat age; this flag only classifies."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not isinstance(args.idle_hours, (int, float)) or args.idle_hours <= 0:
        parser.error("--idle-hours must be > 0")
    idle_hours = float(args.idle_hours)
    rows = collect_controller_rows(idle_hours=idle_hours)
    if args.json:
        print(json.dumps(build_payload(rows, idle_hours=idle_hours), indent=2, sort_keys=True))
        return 0
    print(render_table(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
