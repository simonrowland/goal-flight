#!/usr/bin/env python3
"""goal-flight session activation status helper.

Answers the post-compaction question: "is a goal-flight session active in
this project, or am I in for routine coding?"

Three signals are unioned (see `protocols/state-handoff.md` activation
contract):

1. `state:` field in the newest `docs-private/goal-queue-*.md` frontmatter.
2. Dispatch ledger active leases scoped to this project_root (via
   `scripts/goalflight_capacity.py status`).
3. Newest `docs-private/RESUME-NOTES-*.md` "state" line (if present).

A session id ties an orchestrator invocation to the run. The id lives in
`docs-private/.goal-flight-current-session.json` (per-terminal, gitignored).
The session id is also stamped into the active goal-queue frontmatter under
`current_session` (so multi-machine takeover is detectable) and appended to
`session_history` (audit trail).

Public CLI:

    --json                Emit JSON status (see `_to_json` for shape).
    --text                Emit a one-line plain-English verdict.
    --ensure-session      Read or generate `.goal-flight-current-session.json`,
                          print the session id.
    --claim --queue PATH  Stamp current session into the named goal-queue's
                          `current_session`. Refuses with diagnostic if the
                          queue is claimed by a different alive PID.
    --release [--queue P] Mark current session ended in current_session +
                          session_history; clear `.goal-flight-current-
                          session.json` if no `--queue` (terminal exit).
    --force-release-stale Clear current_session whose pid is dead. Useful
                          after a crash.

Exit codes:

    0  success
    2  refused (e.g., --claim hits a live different-pid owner)
    3  malformed queue frontmatter / fixture-only paths
"""

from __future__ import annotations

import argparse
import json
import os
import select
import shlex
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import goalflight_compat
import goalflight_journal
import goalflight_task
import goalflight_wake

SESSION_FILE_REL = Path("docs-private/.goal-flight-current-session.json")
QUEUE_GLOB = "docs-private/goal-queue-*.md"
RESUME_NOTES_GLOB = "docs-private/RESUME-NOTES-*.md"
CONTROLLER_LABEL_ENV = "GOALFLIGHT_CONTROLLER_LABEL"
CONTROLLER_PID_ENV = "GOALFLIGHT_CONTROLLER_PID"
CONTROLLER_SESSION_ID_ENV = "GOALFLIGHT_CONTROLLER_SESSION_ID"
CONTROLLER_HEARTBEAT_RECENCY_S = 15 * 60
CONTROLLER_HEARTBEAT_MAX_FUTURE_S = 60
NON_CONTROLLER_ROLES = frozenset({"listener", "drainer", "mirror", "dashboard"})
CONTROLLER_LOCK_READY_TIMEOUT_S = 3.0
CONTROLLER_LOCK_STARTUP_GRACE_S = 5.0
CONTROLLER_LOCK_POLL_S = 0.5
# Nonce-less retirement requires renew_deadline_at to lag now by at least one
# full journal lease horizon. DEFAULT_LEASE_HORIZON_S is the longest interval a
# live holder is expected to go without renewing; requiring an extra full
# horizon after that stored deadline is the margin that keeps a merely-overdue
# live holder — or a clock skewed by up to one horizon — from qualifying as
# proven-dead. A holder that is alive and renewing would have moved the
# deadline forward well before this elapsed.
DEAD_HOLDER_RETIRE_MARGIN_S = goalflight_journal.DEFAULT_LEASE_HORIZON_S
DEAD_HOLDER_RELEASE_REASON = "retired-dead-holder"

_EXPECTED_OPTIONAL_ERRORS = (
    ImportError,
    OSError,
    RuntimeError,
    TypeError,
    ValueError,
    sqlite3.DatabaseError,
    subprocess.SubprocessError,
)


# --- session id (per-terminal) ----------------------------------------------


def _session_file(project_root: Path) -> Path:
    return project_root / SESSION_FILE_REL


def _git_project_root() -> Path | None:
    try:
        return goalflight_task.resolve_project_root(str(Path.cwd()))
    except _EXPECTED_OPTIONAL_ERRORS:
        # Project discovery is best-effort identity evidence, never a launch gate.
        return None


def _normalize_controller_label(value: object) -> str | None:
    label = str(value or "").strip()
    return label[:64] or None


def resolve_controller_label(
    explicit_label: str | None = None,
    *,
    project_root: Path | None = None,
    environ: dict[str, str] | None = None,
) -> str | None:
    """Return the explicit label or the measured, worktree-invariant repo name."""
    env = os.environ if environ is None else environ
    value = explicit_label if explicit_label is not None else env.get(CONTROLLER_LABEL_ENV)
    label = _normalize_controller_label(value)
    if label:
        return label
    root = project_root
    if root is None and resolve_controller_pid(environ=env) is not None:
        root = _git_project_root()
    if root is None:
        return None
    try:
        return goalflight_task.resolve_project_root(str(root)).name[:64] or None
    except (OSError, RuntimeError):
        return None


def resolve_controller_pid(
    explicit_pid: object = None,
    *,
    environ: dict[str, str] | None = None,
) -> int | None:
    """Return the controller-declared long-lived PID, never the helper PID."""
    env = os.environ if environ is None else environ
    raw_pid = explicit_pid if explicit_pid is not None else env.get(CONTROLLER_PID_ENV)
    try:
        pid = int(raw_pid)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return pid if pid > 0 else None


def resolve_controller_session_id(
    explicit_session_id: object = None,
    *,
    environ: dict[str, str] | None = None,
) -> str | None:
    """Return an explicitly carried incarnation id, never an inferred one."""
    env = os.environ if environ is None else environ
    raw = (
        explicit_session_id
        if explicit_session_id is not None
        else env.get(CONTROLLER_SESSION_ID_ENV)
    )
    value = str(raw or "").strip()
    return value or None


def _parse_utc(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _heartbeat_age_s(record: dict, *, now: datetime | None = None) -> float | None:
    heartbeat = _parse_utc(record.get("heartbeat_at"))
    if heartbeat is None:
        return None
    measured_now = now or datetime.now(timezone.utc)
    if measured_now.tzinfo is None:
        measured_now = measured_now.replace(tzinfo=timezone.utc)
    return (measured_now.astimezone(timezone.utc) - heartbeat).total_seconds()


def _probe_registered_controller_records(
    project_root: Path,
    *,
    include_retired: bool = False,
) -> tuple[list[dict] | None, str | None]:
    """Return ``(records, error)`` for one registry read.

    A disappeared journal is an honest empty roster. Busy or IO failures are
    unreadable: ``records`` is None and ``error`` is the exception type. Do not
    collapse those into ``[]`` — that is indistinguishable from nothing
    registered.
    """
    try:
        root = goalflight_task.resolve_project_root(str(project_root))
        authority = goalflight_journal.Journal.open_reader(root)
        records = []
        for row in authority.lease_records(include_ended=include_retired):
            state = str(row.get("state") or "")
            principal = json.loads(str(row.get("principal_json") or "{}"))
            record = {
                "controller_registry": True,
                "label": row.get("label"),
                "id": row.get("nonce"),
                "lease_nonce": row.get("nonce"),
                "generation": row.get("generation"),
                "created_at": row.get("claimed_at"),
                "started_at": row.get("claimed_at"),
                "heartbeat_at": row.get("renewed_at"),
                "renew_deadline_at": row.get("renew_deadline_at"),
                "hostname": principal.get("hostname"),
                "pid": row.get("pid"),
                "process_identity": (
                    {"pid": row.get("pid"), "start_token": row.get("start_token")}
                    if row.get("pid") is not None
                    else None
                ),
                "lease_state": state,
            }
            if state != goalflight_journal.LEASE_ACTIVE:
                record["retired_at"] = row.get("ended_at")
                record["retired_by"] = row.get("ended_reason")
            records.append(record)
        return records, None
    except goalflight_journal.JournalDisappeared:
        return [], None
    except (goalflight_journal.JournalBusy, goalflight_journal.JournalIOError) as exc:
        return None, type(exc).__name__


def _registered_controller_records(
    project_root: Path,
    *,
    include_retired: bool = False,
) -> list[dict]:
    records, _error = _probe_registered_controller_records(
        project_root,
        include_retired=include_retired,
    )
    return list(records or [])


def registered_controller_labels(project_root: Path) -> set[str]:
    return {
        str(record["label"])
        for record in _registered_controller_records(project_root)
        if record.get("label") and not record.get("retired_at")
    }


def _index_controller_project(project_root: Path) -> None:
    """Best-effort discovery index; controller truth remains in the journal lease."""
    try:
        import goalflight_task  # type: ignore

        goalflight_task.upsert_project_registry(project_root)
    except _EXPECTED_OPTIONAL_ERRORS:
        pass


# Controller principal measurement verifies journal lease claims; the lease itself is
# the liveness and ownership authority.


def _controller_process_snapshot(pid: int, *, include_ancestry: bool = False) -> dict | None:
    """Return process identity, optionally with measured ancestry fields."""
    return goalflight_compat.process_start_identity(pid, include_ancestry=include_ancestry)


def _controller_process_identity(pid: int) -> dict | None:
    """Return the existing PID-reuse-safe identity shape for beacon records."""
    snapshot = _controller_process_snapshot(pid)
    if snapshot is None:
        return None
    return {"pid": snapshot["pid"], "start_token": snapshot["start_token"]}


def _controller_process_ancestry(pid: int | None = None) -> tuple[dict, ...]:
    """Measure the helper-to-root process chain without invoking ``ps``."""
    current = os.getpid() if pid is None else pid
    ancestry: list[dict] = []
    seen: set[int] = set()
    while current > 0 and current not in seen and len(ancestry) < 64:
        seen.add(current)
        snapshot = _controller_process_snapshot(current, include_ancestry=True)
        if snapshot is None:
            break
        ancestry.append(snapshot)
        parent = snapshot.get("ppid")
        if not isinstance(parent, int):
            break
        current = parent
    return tuple(ancestry)


def _select_durable_controller_ancestor(ancestry: tuple[dict, ...]) -> dict | None:
    """Select the leader of the first process session outside the tool call.

    Agent harnesses launch each tool call in a transient POSIX session. The
    helper and its calling shell share that session. The first outer session is
    the durable host's session; selecting its measured leader skips transient
    shells/interpreters while refusing to drift upward to PID 1 (launchd/init).
    If that leader is not present in the measured parent chain, fail rather than
    invent an identity.
    """
    if not ancestry:
        return None
    helper_session = ancestry[0].get("session_id")
    if not isinstance(helper_session, int):
        return None
    outer_index = next(
        (
            index
            for index, process in enumerate(ancestry[1:], start=1)
            if process.get("session_id") != helper_session
        ),
        None,
    )
    if outer_index is None:
        return None
    outer_session = ancestry[outer_index].get("session_id")
    if not isinstance(outer_session, int) or outer_session < 1:
        return None
    # Prefer the outer session's own leader. In a terminal harness that is the
    # shell or terminal that owns the whole run, which is the longest-lived
    # thing that is still specific to this controller.
    for process in ancestry[outer_index:]:
        if (
            process.get("pid") == outer_session
            and process.get("session_id") == outer_session
            and process.get("pid") != 1
        ):
            return process
    # No leader to select: fall back to the process that SPAWNED our session.
    #
    # A GUI-launched host has no session of its own -- macOS puts it in session
    # 1, whose leader is launchd. Refusing session 1 outright (as this did) made
    # registration impossible for every desktop-launched controller, which is
    # not a drift-to-init problem but a measurement gap: the spawner here is the
    # host process itself, alive exactly as long as the controller it runs, and
    # neither init nor a transient shell.
    #
    # The first ancestor outside our session is that spawner by construction --
    # it created the session we are in, so it cannot be shorter-lived than this
    # call. Anything above it (the enclosing application) is shared across
    # sessions and would outlive this controller, which is the failure that
    # matters: a beacon that never dies makes a dead controller look alive.
    spawner = ancestry[outer_index]
    if spawner.get("pid") == 1 or not isinstance(spawner.get("pid"), int):
        return None
    return spawner


def _doomed_invocation_pid(pid: int, ancestry: tuple[dict, ...]) -> bool:
    """True only when measurement proves the claimed process is this helper.

    A parent in the helper's POSIX session is suspicious, but it can remain
    alive after the helper exits. Session membership alone is therefore not a
    lifetime proof.
    """
    return pid == os.getpid()


def _suspicious_invocation_pid(pid: int, ancestry: tuple[dict, ...]) -> bool:
    if not ancestry:
        return False
    helper_session = ancestry[0].get("session_id")
    if not isinstance(helper_session, int):
        return False
    return any(
        process.get("pid") == pid
        and process.get("pid") != os.getpid()
        and process.get("session_id") == helper_session
        for process in ancestry
    )


def _resolve_optional_incarnation(
    pid: int | None,
    *,
    environ: dict[str, str] | None = None,
    pid_from_ancestry: bool = False,
    default_to_current: bool = False,
) -> tuple[dict | None, dict | None]:
    """Resolve one PID generation once and carry its start token forward."""
    env = os.environ if environ is None else environ
    declared_pid = resolve_controller_pid(pid, environ=env)
    if pid_from_ancestry:
        if declared_pid is not None:
            return None, {
                "reason": "conflicting_controller_pid_sources",
                "message": (
                    "--controller-pid-from-ancestry cannot be combined with "
                    "--session-pid or GOALFLIGHT_CONTROLLER_PID"
                ),
            }
        ancestor = _select_durable_controller_ancestor(_controller_process_ancestry())
        if ancestor is None:
            return None, {
                "reason": "controller_ancestry_unavailable",
                "message": "no durable host session leader was measurable",
            }
        identity = {
            "pid": int(ancestor["pid"]),
            "start_token": str(ancestor.get("start_token") or ""),
        }
        if not identity["start_token"]:
            return None, {
                "reason": "controller_process_generation_unavailable",
                "controller_pid": identity["pid"],
            }
        return {"pid": identity["pid"], "process_identity": identity}, None

    if declared_pid is None and default_to_current:
        declared_pid = os.getpid()
    if declared_pid is None:
        return None, None
    ancestry = _controller_process_ancestry()
    if _doomed_invocation_pid(declared_pid, ancestry):
        return None, {
            "reason": "controller_pid_cannot_outlive_claim",
            "controller_pid": declared_pid,
            "message": (
                "the declared controller PID is the claim helper itself and cannot "
                "outlive this invocation; use --controller-pid-from-ancestry or "
                "supply a live launcher PID"
            ),
        }
    identity = _controller_process_identity(declared_pid)
    if identity is None:
        reason = (
            "controller_process_generation_unavailable"
            if _pid_alive(declared_pid)
            else "controller_pid_not_live"
        )
        return None, {"reason": reason, "controller_pid": declared_pid}
    resolution = {"pid": declared_pid, "process_identity": identity}
    if _suspicious_invocation_pid(declared_pid, ancestry):
        resolution["warning"] = {
            "reason": "controller_pid_lifetime_suspicious",
            "message": (
                "the declared PID shares the helper's POSIX session; measurement "
                "cannot prove whether it will outlive this invocation"
            ),
        }
    return resolution, None


# Removed _same_controller_process: PID/start-token inference was superseded by held kernel-lock liveness.


def _lease_holder_liveness(
    lease: goalflight_journal.LeaseIdentity | None,
) -> goalflight_journal.LeaseLivenessEvidence | None:
    """Measure one incumbent from its held kernel lock, never from PID state."""
    if lease is None:
        return None
    return goalflight_journal.LeaseLivenessEvidence(
        generation=lease.generation,
        nonce=lease.nonce,
        alive=goalflight_wake.lease_holder_alive(
            lease.project_root,
            controller_label=lease.label,
            lease_nonce=lease.nonce,
        ),
    )


def _auto_claim_refusal_reason(
    *,
    role: str,
    has_session_beacon: bool,
    worker_dispatch: bool,
    session_entry: bool,
) -> str | None:
    """Pure policy: only a controller session beacon may auto-claim."""
    if role != "controller":
        return "role_does_not_auto_claim"
    if worker_dispatch:
        return "worker_dispatch_does_not_claim"
    if not session_entry:
        return "one_shot_cli_does_not_claim"
    if not has_session_beacon:
        return "missing_session_beacon"
    return None


def auto_claim_controller_entry(
    project_root: Path,
    *,
    role: str | None = None,
    label: str | None = None,
    environ: dict[str, str] | None = None,
    takeover: bool = False,
    session_entry: bool = False,
) -> dict:
    """Auto-claim only for an explicit, beacon-backed session entry."""
    env = os.environ if environ is None else environ
    resolved_role = str(role or env.get("GOALFLIGHT_PROCESS_ROLE") or "controller").strip()
    resolved_pid = resolve_controller_pid(environ=env)
    refusal = _auto_claim_refusal_reason(
        role=resolved_role,
        has_session_beacon=resolved_pid is not None,
        worker_dispatch=bool(str(env.get("GOALFLIGHT_DISPATCH_ID") or "").strip()),
        session_entry=session_entry,
    )
    if refusal is not None:
        return {"claimed": False, "reason": refusal, "role": resolved_role}
    return claim_controller_startup(
        project_root,
        pid=resolved_pid,
        label=label,
        environ=env,
        role=resolved_role,
        session_id=resolve_controller_session_id(environ=env),
        takeover=takeover,
        hold_lock=True,
    )


def _same_lease_principal(
    lease: goalflight_journal.LeaseIdentity | None,
    principal: dict[str, object],
) -> bool:
    if lease is None:
        return False
    stored = lease.principal
    if stored.get("pid") is not None or stored.get("start_token") is not None:
        return bool(
            stored.get("pid") == principal.get("pid")
            and stored.get("start_token")
            and stored.get("start_token") == principal.get("start_token")
        )
    return bool(
        stored.get("principal_id")
        and stored.get("principal_id") == principal.get("principal_id")
    )


def _live_incumbent_label_for_principal(
    project_root: Path,
    *,
    requested_label: str,
    principal: dict[str, object],
) -> str | None:
    """Return another live label already held by this measured principal."""
    root = goalflight_task.resolve_project_root(str(project_root))
    authority = goalflight_journal.open_or_create_journal(root)
    expiry = authority.expire_stale_leases()
    if not expiry.committed:
        raise RuntimeError(
            f"lease expiry sweep failed before incumbent adoption: {expiry.reason}"
        )
    requested = authority.active_lease(requested_label)
    if _same_lease_principal(requested, principal):
        return None
    incumbents: list[goalflight_journal.LeaseIdentity] = []
    for row in authority.lease_records():
        label = str(row.get("label") or "")
        if not label or label == requested_label:
            continue
        lease = authority.active_lease(label)
        if not _same_lease_principal(lease, principal):
            continue
        liveness = _lease_holder_liveness(lease)
        if liveness is not None and liveness.alive is True and lease is not None:
            incumbents.append(lease)
    if not incumbents:
        return None
    incumbents.sort(key=lambda lease: (lease.claimed_at, lease.label))
    return incumbents[0].label


def _controller_adoption_notice(
    project_root: Path,
    *,
    adopted_label: str,
    requested_label: str,
    pid: int,
) -> str:
    root = goalflight_task.resolve_project_root(str(project_root))
    script = str(
        goalflight_compat.advertised_script(
            "goalflight_session_status.py",
            running_file=__file__,
        )
    )
    release = shlex.join(
        [
            "python3",
            script,
            "--project-root",
            str(root),
            "--release-session",
            "--session-pid",
            str(pid),
        ]
    )
    reclaim = shlex.join(
        [
            "python3",
            script,
            "--project-root",
            str(root),
            "--controller-startup",
            "--session-pid",
            str(pid),
            "--session-label",
            requested_label,
        ]
    )
    return (
        f"controller startup: adopted existing label {adopted_label!r} for this process; "
        f"if that match is wrong, re-seat with: {release} && {reclaim}"
    )


def _publish_lease_generation_event(
    project_root: Path,
    lease: goalflight_journal.LeaseIdentity,
    *,
    only_if_present: bool = False,
) -> None:
    if only_if_present and goalflight_wake.lease_generation_event_stamp(
        project_root,
        controller_label=lease.label,
    ) is None:
        return
    goalflight_wake.publish_lease_generation_event(
        project_root,
        controller_label=lease.label,
        lease_nonce=lease.nonce,
        generation=lease.generation,
        state=lease.state,
    )


def _stop_lock_holder(process: subprocess.Popen[str] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


def _start_lock_holder(
    project_root: Path,
    *,
    label: str,
    nonce: str,
    pid: int,
    start_token: str,
) -> subprocess.Popen[str]:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--project-root",
        str(project_root),
        "--hold-controller-lock",
        "--session-label",
        label,
        "--controller-session-id",
        nonce,
        "--session-pid",
        str(pid),
        "--controller-start-token",
        start_token,
    ]
    env = dict(os.environ)
    env["GOALFLIGHT_PROCESS_ROLE"] = "beacon"
    process = subprocess.Popen(
        command,
        cwd=project_root,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        start_new_session=True,
    )
    if process.stdout is None:
        _stop_lock_holder(process)
        raise RuntimeError("controller lease lock holder has no readiness pipe")
    ready, _, _ = select.select(
        [process.stdout],
        [],
        [],
        CONTROLLER_LOCK_READY_TIMEOUT_S,
    )
    line = process.stdout.readline() if ready else ""
    process.stdout.close()
    try:
        payload = json.loads(line)
    except (TypeError, ValueError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict) or payload.get("ready") is not True:
        _stop_lock_holder(process)
        raise RuntimeError("controller lease lock holder failed to become ready")
    return process


def hold_controller_lock(
    project_root: Path,
    *,
    label: str,
    nonce: str,
    pid: int,
    start_token: str,
) -> int:
    """Hold one session lock; cheap ticks stat one event token and check the host."""
    expected_identity = {"pid": pid, "start_token": start_token}
    if _controller_process_identity(pid) != expected_identity:
        return 2
    try:
        registration = goalflight_wake.register_lease_holder(
            project_root,
            controller_label=label,
            lease_nonce=nonce,
        )
    except (OSError, RuntimeError, ValueError):
        return 2
    print(json.dumps({"ready": True, "pid": os.getpid()}), flush=True)
    startup_deadline = time.monotonic() + CONTROLLER_LOCK_STARTUP_GRACE_S
    matched_generation = False
    validated_generation: int | None = None
    validated_stamp: tuple[int, int, int] | None = None
    try:
        while True:
            holder_matches = goalflight_compat.process_identity_matches(pid, start_token)
            if holder_matches is False:
                return 0
            try:
                candidate_stamp = goalflight_wake.lease_generation_event_stamp(
                    project_root,
                    controller_label=label,
                )
            except OSError:
                return 2
            if candidate_stamp != validated_stamp:
                try:
                    event = goalflight_wake.read_lease_generation_event(
                        project_root,
                        controller_label=label,
                    )
                    if event is None:
                        lease = None
                    else:
                        event_label = str(event.get("label") or "")
                        event_nonce = str(event.get("nonce") or "")
                        event_generation = int(event.get("generation") or 0)
                        if event_label != label or event_generation < 1:
                            return 2
                        lease = goalflight_journal.Journal.open_reader(project_root).active_lease(label)
                        if (
                            str(event.get("state") or "") == goalflight_journal.LEASE_ACTIVE
                            and lease is not None
                            and lease.nonce == event_nonce
                            and lease.generation == event_generation
                        ):
                            validated_stamp = candidate_stamp
                            validated_generation = event_generation
                            if event_nonce == nonce:
                                matched_generation = True
                            elif matched_generation:
                                return 0
                        elif matched_generation and (
                            lease is None
                            or lease.nonce != nonce
                            or lease.generation != validated_generation
                        ):
                            return 0
                except _EXPECTED_OPTIONAL_ERRORS:
                    # A transiently unreadable event/journal never manufactures
                    # a generation change. Leave the stamp unconsumed and retry.
                    pass
            if not matched_generation and time.monotonic() >= startup_deadline:
                return 2
            time.sleep(CONTROLLER_LOCK_POLL_S)
    finally:
        registration.close()


def claim_session(
    project_root: Path,
    *,
    pid: int,
    session_id: str | None = None,
    label: str | None = None,
    process_identity: dict | None = None,
    takeover: bool = False,
    hold_lock: bool = False,
) -> dict:
    """Claim or renew one journal lease without stealing a live generation."""
    root = goalflight_task.resolve_project_root(str(project_root))
    measured_identity = _controller_process_identity(pid)
    if measured_identity is None:
        raise RuntimeError("controller process generation is unavailable")
    if process_identity is not None:
        expected = {"pid": process_identity.get("pid"), "start_token": process_identity.get("start_token")}
        if expected != measured_identity:
            raise RuntimeError("controller process generation changed before claim")
        process_identity = expected
    else:
        process_identity = measured_identity
    resolved_label = resolve_controller_label(label, project_root=root)
    if resolved_label is None:
        raise RuntimeError("controller label is unavailable")
    authority = goalflight_journal.open_or_create_journal(root)
    principal = {
        "pid": pid,
        "start_token": process_identity["start_token"],
        "hostname": socket.gethostname(),
    }
    incumbent = authority.active_lease(resolved_label)
    incumbent_liveness = _lease_holder_liveness(incumbent)
    same_principal = _same_lease_principal(incumbent, principal)
    if same_principal and incumbent is not None:
        candidate_nonce = incumbent.nonce
    elif session_id and (incumbent is None or session_id != incumbent.nonce):
        candidate_nonce = session_id
    else:
        candidate_nonce = uuid.uuid4().hex
    holder: subprocess.Popen[str] | None = None
    needs_holder = bool(
        hold_lock
        and not (
            same_principal
            and incumbent_liveness is not None
            and incumbent_liveness.alive is True
        )
        and not (
            incumbent is not None
            and not same_principal
            and incumbent_liveness is not None
            and incumbent_liveness.alive is True
            and not takeover
        )
    )
    if needs_holder:
        holder = _start_lock_holder(
            root,
            label=resolved_label,
            nonce=candidate_nonce,
            pid=pid,
            start_token=str(process_identity["start_token"]),
        )
    result = authority.claim_or_renew_lease(
        resolved_label,
        principal=principal,
        nonce=candidate_nonce if hold_lock else session_id,
        takeover=takeover,
        incumbent_liveness=incumbent_liveness,
    )
    if not result.committed or result.value is None:
        _stop_lock_holder(holder)
        raise RuntimeError(result.reason or "controller lease claim failed")
    lease = result.value
    if holder is not None and lease.nonce != candidate_nonce:
        _stop_lock_holder(holder)
        raise RuntimeError("controller lease nonce changed after lock registration")
    if hold_lock:
        try:
            _publish_lease_generation_event(root, lease)
        except (OSError, RuntimeError, ValueError):
            _stop_lock_holder(holder)
            raise
    return {
        "id": lease.nonce,
        "lease_nonce": lease.nonce,
        "generation": lease.generation,
        "pid": pid,
        "started_at": lease.claimed_at,
        "heartbeat_at": lease.renewed_at,
        "renew_deadline_at": lease.renew_deadline_at,
        "hostname": socket.gethostname(),
        "beacon": True,
        "controller_registry": True,
        "label": lease.label,
        "process_identity": process_identity,
        "kernel_lock_held": bool(
            _lease_holder_liveness(lease) is not None
            and _lease_holder_liveness(lease).alive is True
        ),
    }


def _session_dict_from_lease(
    lease: goalflight_journal.LeaseIdentity,
    *,
    pid: int | None,
) -> dict | None:
    principal = lease.principal
    if pid is not None and principal.get("pid") != pid:
        return None
    process_identity = None
    if principal.get("pid") is not None:
        process_identity = {
            "pid": principal.get("pid"),
            "start_token": principal.get("start_token"),
        }
    return {
        "id": lease.nonce,
        "lease_nonce": lease.nonce,
        "generation": lease.generation,
        "pid": principal.get("pid"),
        "started_at": lease.claimed_at,
        "heartbeat_at": lease.renewed_at,
        "renew_deadline_at": lease.renew_deadline_at,
        "hostname": principal.get("hostname"),
        "beacon": True,
        "controller_registry": True,
        "label": lease.label,
        "process_identity": process_identity,
    }


def probe_live_session(
    project_root: Path,
    *,
    label: str | None = None,
    pid: int | None = None,
) -> tuple[str, dict | None]:
    """Return ``(live|dead|unreadable, session-or-None)``.

    Reads through ``Journal.open_reader`` so a busy write constructor is not
    "there is no live session". Unreadable means the caller could not tell
    and must retry; only a readable absent or changed lease is dead.
    """
    root = goalflight_task.resolve_project_root(str(project_root))
    if label is None and pid is None:
        declared_pid = resolve_controller_pid()
        label_was_declared = bool(str(os.environ.get(CONTROLLER_LABEL_ENV) or "").strip())
        if label_was_declared or declared_pid is not None:
            declared_label = resolve_controller_label(project_root=project_root)
            if declared_label is None:
                return "dead", None
            label, pid = declared_label, declared_pid
    try:
        authority = goalflight_journal.Journal.open_reader(
            root,
            retry_budget_s=0.05,
            open_retry_budget_s=0.05,
        )
        lease = authority.active_lease(label) if label is not None else None
        if lease is None and label is None:
            rows = authority.lease_records()
            if len(rows) != 1:
                return "dead", None
            lease = authority.active_lease(str(rows[0]["label"]))
    except goalflight_journal.JournalDisappeared:
        return "dead", None
    except goalflight_journal.JournalUpgradeRequired:
        raise
    except goalflight_journal.JournalBusy:
        return "unreadable", None
    except goalflight_journal.JournalIOError:
        return "unreadable", None
    except (OSError, goalflight_journal.JournalError):
        return "unreadable", None
    if lease is None:
        return "dead", None
    liveness = _lease_holder_liveness(lease)
    if liveness is None or liveness.alive is None:
        return "unreadable", None
    if liveness.alive is not True:
        return "dead", None
    session = _session_dict_from_lease(lease, pid=pid)
    if session is None:
        return "dead", None
    return "live", session


def live_session(
    project_root: Path,
    *,
    label: str | None = None,
    pid: int | None = None,
) -> dict | None:
    """Return the kernel-lock-live active journal lease, or ``None``.

    ``None`` still collapses unreadable and absent for legacy callers. New
    code that must not treat a busy journal as a dead lease should call
    ``probe_live_session`` instead.
    """
    state, session = probe_live_session(project_root, label=label, pid=pid)
    return session if state == "live" else None


def _listener_depth_after_claim(
    project_root: Path,
    label: str,
    lease_nonce: str,
) -> dict[str, object] | None:
    """Fail-open remaining-depth plan so a claim never blocks on the wake plane."""
    try:
        status = goalflight_wake.coverage_status(
            project_root,
            controller_label=label,
            lease_nonce=lease_nonce,
        )
        authority = goalflight_journal.Journal.open_reader(project_root)
        plan = goalflight_wake.coverage_rearm_plan(
            status,
            project_root,
            controller_label=label,
            lease_nonce=lease_nonce,
            work_in_flight=authority.care_work_exists(label),
        )
        # t-272: ``command`` already carries the one project-root copy.
        # ``supervise_command`` is a second path-bearing argv used only by
        # hint printers. Only proven supervisor absence may expose component
        # commands or depth: UNKNOWN is not evidence that direct arming is
        # safe, and RUNNING owns its pool without controller intervention.
        return goalflight_wake.operator_rearm_plan(plan)
    except Exception:
        return None


def claim_controller_startup(
    project_root: Path,
    *,
    pid: int | None = None,
    label: str | None = None,
    environ: dict[str, str] | None = None,
    pid_from_ancestry: bool = False,
    role: str | None = None,
    session_id: str | None = None,
    takeover: bool = False,
    hold_lock: bool = False,
) -> dict:
    """Best-effort startup registration; observability must never block work."""
    try:
        env = os.environ if environ is None else environ
        resolved_role = str(role or env.get("GOALFLIGHT_PROCESS_ROLE") or "controller").strip()
        if resolved_role in NON_CONTROLLER_ROLES:
            return {"claimed": False, "reason": "role_does_not_claim", "role": resolved_role}
        resolved_label = resolve_controller_label(
            label,
            project_root=project_root,
            environ=env,
        )
        if not resolved_label:
            return {"claimed": False, "reason": "missing_controller_label"}
        resolution, pid_error = _resolve_optional_incarnation(
            pid,
            environ=env,
            pid_from_ancestry=pid_from_ancestry,
        )
        if pid_error is not None:
            return {"claimed": False, **pid_error}
        if resolution is None:
            return {"claimed": False, "reason": "missing_controller_pid"}
        resolved_pid = int(resolution["pid"])
        effective_label = resolved_label
        process_identity = resolution.get("process_identity")
        if isinstance(process_identity, dict) and process_identity.get("start_token"):
            incumbent_label = _live_incumbent_label_for_principal(
                project_root,
                requested_label=resolved_label,
                principal={
                    "pid": resolved_pid,
                    "start_token": process_identity["start_token"],
                },
            )
            if incumbent_label is not None:
                effective_label = incumbent_label
        record = claim_session(
            project_root,
            pid=resolved_pid,
            label=effective_label,
            session_id=resolve_controller_session_id(session_id, environ=env),
            process_identity=process_identity,
            takeover=takeover,
            hold_lock=hold_lock,
        )
        if record.get("label") != effective_label:
            return {
                "claimed": False,
                "reason": "controller_label_mismatch",
                "existing_label": record.get("label"),
            }
        if hold_lock:
            live = live_session(
                project_root,
                label=effective_label,
                pid=resolved_pid,
            )
        else:
            lease = goalflight_journal.Journal.open_reader(project_root).active_lease(
                effective_label
            )
            live = (
                {"id": lease.nonce}
                if lease is not None and lease.principal.get("pid") == resolved_pid
                else None
            )
        if not isinstance(live, dict) or live.get("id") != record.get("id"):
            return {"claimed": False, "reason": "claim_not_live"}
        if live.get("conflicting_beacons"):
            return {
                "claimed": False,
                "reason": "controller_label_conflict",
                "conflicting_beacons": live["conflicting_beacons"],
            }
    except (
        goalflight_journal.JournalBusy,
        goalflight_journal.JournalDisappeared,
        goalflight_journal.JournalIOError,
    ) as exc:
        return {
            "claimed": False,
            "reason": "claim_failed",
            "error_type": type(exc).__name__,
        }
    except goalflight_journal.JournalError:
        raise
    except (ImportError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        detail = str(exc)
        if "label in use" in detail:
            return {
                "claimed": False,
                "reason": "label_in_use",
                "message": detail,
                "label": resolved_label,
            }
        return {
            "claimed": False,
            "reason": "claim_failed",
            "error_type": type(exc).__name__,
        }
    result = {"claimed": True, "session": record}
    if effective_label != resolved_label:
        result["adopted_label"] = effective_label
        result["requested_label"] = resolved_label
    if resolution.get("warning"):
        result["warnings"] = [resolution["warning"]]
    depth = _listener_depth_after_claim(
        project_root,
        effective_label,
        str(record["id"]),
    )
    if depth is not None:
        supervisor = str(depth.get("supervisor") or "")
        if supervisor == goalflight_wake.SUPERVISOR_RUNNING:
            result["wake_supervisor"] = supervisor
        else:
            result["listener_depth"] = depth
    return result


def register_controller(
    project_root: Path,
    name: str,
    *,
    pid: int | None = None,
    session_id: str | None = None,
    process_identity: dict | None = None,
    hold_lock: bool = False,
) -> dict:
    """Create one active lease; a live incumbent returns ``label in use``."""
    label = _normalize_controller_label(name)
    if label is None:
        return {"registered": False, "reason": "missing_controller_label"}
    if pid is not None and _doomed_invocation_pid(pid, _controller_process_ancestry()):
        return {
            "registered": False,
            "reason": "controller_pid_cannot_outlive_claim",
            "controller_pid": pid,
        }
    if pid is not None:
        measured_identity = _controller_process_identity(pid)
        if measured_identity is None:
            return {"registered": False, "reason": "controller_pid_not_live"}
        if process_identity is not None and measured_identity != process_identity:
            return {"registered": False, "reason": "controller_process_generation_changed"}
        process_identity = process_identity or measured_identity
    if hold_lock:
        if pid is None:
            return {"registered": False, "reason": "missing_session_beacon"}
        try:
            record = claim_session(
                project_root,
                pid=pid,
                session_id=session_id,
                label=label,
                process_identity=process_identity,
                hold_lock=True,
            )
        except (
            goalflight_journal.JournalBusy,
            goalflight_journal.JournalDisappeared,
            goalflight_journal.JournalIOError,
        ) as exc:
            return {
                "registered": False,
                "reason": "claim_failed",
                "message": str(exc),
            }
        except goalflight_journal.JournalError:
            raise
        except (ImportError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
            detail = str(exc)
            return {
                "registered": False,
                "reason": "label_in_use" if "label in use" in detail else "claim_failed",
                "message": detail,
            }
        return {"registered": True, "session": record}
    principal = (
        {
            "pid": pid,
            "start_token": process_identity["start_token"],
            "hostname": socket.gethostname(),
        }
        if pid is not None and process_identity is not None
        else {
            "principal_id": session_id or str(uuid.uuid4()),
            "hostname": socket.gethostname(),
        }
    )
    try:
        authority = goalflight_journal.open_or_create_journal(project_root)
        incumbent_liveness = _lease_holder_liveness(authority.active_lease(label))
        result = authority.claim_or_renew_lease(
            label,
            principal=principal,
            nonce=session_id,
            incumbent_liveness=incumbent_liveness,
        )
    except (
        goalflight_journal.JournalBusy,
        goalflight_journal.JournalDisappeared,
        goalflight_journal.JournalIOError,
    ) as exc:
        return {"registered": False, "reason": "claim_failed", "message": str(exc)}
    except goalflight_journal.JournalError:
        raise
    except (ImportError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        return {"registered": False, "reason": "claim_failed", "message": str(exc)}
    if not result.committed or result.value is None:
        return {
            "registered": False,
            "reason": "label_in_use" if "label in use" in str(result.reason) else "claim_failed",
            "message": result.reason,
        }
    lease = result.value
    return {
        "registered": True,
        "session": {
            "id": lease.nonce,
            "lease_nonce": lease.nonce,
            "generation": lease.generation,
            "label": lease.label,
            "pid": pid,
            "started_at": lease.claimed_at,
            "heartbeat_at": lease.renewed_at,
            "renew_deadline_at": lease.renew_deadline_at,
            "hostname": socket.gethostname(),
            "controller_registry": True,
            "process_identity": process_identity,
        },
    }


def join_controller(
    project_root: Path,
    name: str,
    *,
    pid: int | None = None,
    session_id: str | None = None,
    acknowledge_conflict: bool = False,
    process_identity: dict | None = None,
    hold_lock: bool = False,
) -> dict:
    """Renew the incumbent or perform an explicit generation takeover."""
    label = _normalize_controller_label(name)
    if label is None:
        return {"joined": False, "reason": "missing_controller_label"}
    if pid is not None and _doomed_invocation_pid(pid, _controller_process_ancestry()):
        return {
            "joined": False,
            "reason": "controller_pid_cannot_outlive_claim",
            "controller_pid": pid,
        }
    if pid is not None:
        measured_identity = _controller_process_identity(pid)
        if measured_identity is None:
            return {"joined": False, "reason": "controller_pid_not_live"}
        if process_identity is not None and measured_identity != process_identity:
            return {"joined": False, "reason": "controller_process_generation_changed"}
        process_identity = process_identity or measured_identity
    if hold_lock:
        if pid is None:
            return {"joined": False, "reason": "missing_session_beacon"}
        authority = goalflight_journal.open_or_create_journal(project_root)
        before = authority.active_lease(label)
        try:
            record = claim_session(
                project_root,
                pid=pid,
                session_id=session_id,
                label=label,
                process_identity=process_identity,
                takeover=acknowledge_conflict,
                hold_lock=True,
            )
        except (
            goalflight_journal.JournalBusy,
            goalflight_journal.JournalDisappeared,
            goalflight_journal.JournalIOError,
        ) as exc:
            return {
                "joined": False,
                "reason": "claim_failed",
                "message": str(exc),
                "acknowledgement_available": True,
            }
        except goalflight_journal.JournalError:
            raise
        except (ImportError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
            detail = str(exc)
            return {
                "joined": False,
                "reason": "label_in_use" if "label in use" in detail else "claim_failed",
                "message": detail,
                "acknowledgement_available": True,
            }
        succession = before is not None and before.generation != record["generation"]
        return {
            "joined": True,
            "succession": succession,
            "conflict_acknowledged": bool(acknowledge_conflict and succession),
            "session": record,
        }
    principal = (
        {
            "pid": pid,
            "start_token": process_identity["start_token"],
            "hostname": socket.gethostname(),
        }
        if pid is not None and process_identity is not None
        else {
            "principal_id": session_id or str(uuid.uuid4()),
            "hostname": socket.gethostname(),
        }
    )
    authority = goalflight_journal.open_or_create_journal(project_root)
    before = authority.active_lease(label)
    result = authority.claim_or_renew_lease(
        label,
        principal=principal,
        nonce=session_id,
        takeover=acknowledge_conflict,
        incumbent_liveness=_lease_holder_liveness(before),
    )
    if not result.committed or result.value is None:
        return {
            "joined": False,
            "reason": "label_in_use" if "label in use" in str(result.reason) else "claim_failed",
            "message": result.reason,
            "acknowledgement_available": True,
        }
    lease = result.value
    succession = before is not None and before.generation != lease.generation
    return {
        "joined": True,
        "succession": succession,
        "conflict_acknowledged": bool(acknowledge_conflict and succession),
        "session": {
            "id": lease.nonce,
            "lease_nonce": lease.nonce,
            "generation": lease.generation,
            "label": lease.label,
            "pid": pid,
            "started_at": lease.claimed_at,
            "heartbeat_at": lease.renewed_at,
            "renew_deadline_at": lease.renew_deadline_at,
            "hostname": socket.gethostname(),
            "controller_registry": True,
            "process_identity": process_identity,
        },
    }


def _format_idle_duration(age_s: float | None) -> str:
    if age_s is None:
        return "idle unknown"
    if age_s < -CONTROLLER_HEARTBEAT_MAX_FUTURE_S:
        return "clock skew: future heartbeat"
    age_s = max(0.0, age_s)
    if age_s < 60:
        return "idle <1 minute"
    if age_s < 3600:
        minutes = int(age_s // 60)
        return f"idle {minutes} minute{'s' if minutes != 1 else ''}"
    if age_s < 86400:
        hours = int(age_s // 3600)
        return f"idle {hours} hour{'s' if hours != 1 else ''}"
    days = int(age_s // 86400)
    return f"idle {days} day{'s' if days != 1 else ''}"


def _incarnation_state(
    record: dict,
    *,
    lease_lock_live: bool | None,
    now: datetime | None = None,
) -> tuple[str, bool | None]:
    """Classify one lease for holder-visible reporting.

    ``live-lock`` / ``dead-lock`` are facts about the kernel lock, not a
    health verdict. ``unknown-lock`` is the third token when the lock probe
    could not tell. A held lock whose ``renew_deadline_at`` is in the past is
    ``live-overdue``: the process may still be working, and another controller
    may legitimately reclaim. ``live-overdue`` requires a proven live lock; do
    not map overdue or unreadable onto ``dead-lock``.
    """
    if record.get("retired_at"):
        return "ended", False
    if lease_lock_live is None:
        return "unknown-lock", None
    if not lease_lock_live:
        return "dead-lock", False
    deadline = _parse_utc(record.get("renew_deadline_at"))
    if deadline is None:
        return "live-lock", True
    measured_now = now or datetime.now(timezone.utc)
    if measured_now.tzinfo is None:
        measured_now = measured_now.replace(tzinfo=timezone.utc)
    if deadline <= measured_now.astimezone(timezone.utc):
        return "live-overdue", True
    return "live-lock", True


def _addressed_unread_counts(
    project_root: Path,
    *,
    messages_dir: Path | None = None,
    fleet_dir: Path | None = None,
) -> tuple[dict[str, int] | None, str | None]:
    try:
        authority = goalflight_journal.Journal.open_reader(project_root)
        counts = {
            str(record["label"]): len(
                authority.pending_delivery_events(
                    str(record["label"]),
                    waking_only=False,
                    limit=10_000,
                )
            )
            for record in authority.lease_records()
        }
        return counts, None
    except _EXPECTED_OPTIONAL_ERRORS as exc:
        return None, type(exc).__name__


def _nonterminal_owned_dispatches(
    project_root: Path,
    *,
    records: list[dict] | None = None,
) -> tuple[dict[str, list[dict]] | None, str | None]:
    try:
        import goalflight_dispatch_states as dispatch_states  # type: ignore
        import goalflight_ledger  # type: ignore

        ledger_records = goalflight_ledger.read_records() if records is None else records
        root = str(project_root.resolve())
        by_label: dict[str, list[dict]] = {}
        for record in ledger_records:
            label = _normalize_controller_label(record.get("controller_label"))
            if label is None or str(record.get("project_root") or "") != root:
                continue
            if any(
                dispatch_states.is_terminal_state(record.get(key))
                for key in ("state", "terminal_state", "classification")
            ):
                continue
            by_label.setdefault(label, []).append(
                {
                    "dispatch_id": record.get("dispatch_id"),
                    "state": record.get("state"),
                }
            )
        return by_label, None
    except _EXPECTED_OPTIONAL_ERRORS as exc:
        return None, type(exc).__name__


def controller_roster(
    project_root: Path,
    *,
    include_retired: bool = False,
    now: datetime | None = None,
    messages_dir: Path | None = None,
    fleet_dir: Path | None = None,
    ledger_records: list[dict] | None = None,
) -> dict:
    """Return measured durable controller state for human and console consumers."""
    measured_now = now or datetime.now(timezone.utc)
    unread, unread_error = _addressed_unread_counts(
        project_root,
        messages_dir=messages_dir,
        fleet_dir=fleet_dir,
    )
    owned, owned_error = _nonterminal_owned_dispatches(
        project_root,
        records=ledger_records,
    )
    records, registry_error = _probe_registered_controller_records(
        project_root,
        include_retired=include_retired,
    )
    controllers = []
    for record in records or []:
        label = str(record.get("label") or "")
        idle_s = _heartbeat_age_s(record, now=measured_now)
        probe_state, live = probe_live_session(project_root, label=label)
        if probe_state == "live":
            lock_live: bool | None = True
        elif probe_state == "dead":
            lock_live = False
        else:
            lock_live = None
        incarnation_state, lease_lock_live = _incarnation_state(
            record,
            lease_lock_live=lock_live,
            now=measured_now,
        )
        conflicting_beacons = (
            int(live.get("conflicting_beacons") or 0)
            if isinstance(live, dict)
            else 0
        )
        heartbeat_clock_state = (
            "future-skew"
            if idle_s is not None and idle_s < -CONTROLLER_HEARTBEAT_MAX_FUTURE_S
            else "trusted"
        )
        controllers.append(
            {
                "label": label,
                "last_heartbeat_at": record.get("heartbeat_at"),
                "idle_seconds": round(idle_s, 3) if idle_s is not None else None,
                "idle": _format_idle_duration(idle_s),
                "heartbeat_clock_state": heartbeat_clock_state,
                "incarnation_state": incarnation_state,
                "conflicting_beacons": conflicting_beacons,
                "pid": record.get("pid") if isinstance(record.get("pid"), int) else None,
                "pid_live": None,
                "lease_lock_live": lease_lock_live,
                "session_id": record.get("id"),
                "unread_addressed_mail": unread.get(label, 0) if unread is not None else None,
                "nonterminal_owned_dispatches": len(owned.get(label, [])) if owned is not None else None,
                "retired": bool(record.get("retired_at")),
                "retired_at": record.get("retired_at"),
            }
        )
    return {
        "schema": "goalflight.controller-roster.v1",
        "generated_at": measured_now.astimezone(timezone.utc).isoformat(),
        "heartbeat_recency_seconds": CONTROLLER_HEARTBEAT_RECENCY_S,
        "measurements": {
            "unread_addressed_mail": {"measured": unread is not None, "error": unread_error},
            "nonterminal_owned_dispatches": {"measured": owned is not None, "error": owned_error},
            "controller_registry": {
                "measured": records is not None,
                "error": registry_error,
            },
        },
        "controllers": controllers,
    }


def controller_roster_lines(roster: dict) -> list[str]:
    measurements = roster.get("measurements")
    registry = (
        measurements.get("controller_registry")
        if isinstance(measurements, dict)
        else None
    )
    if isinstance(registry, dict) and registry.get("measured") is False:
        error = str(registry.get("error") or "unreadable").strip() or "unreadable"
        return [f"controllers unreadable ({error})"]
    lines = []
    for record in roster.get("controllers") or []:
        unread = record.get("unread_addressed_mail")
        owned = record.get("nonterminal_owned_dispatches")
        label = "".join(
            char if char.isprintable() and char not in "\r\n" else "?"
            for char in str(record.get("label") or "")
        )
        conflict = int(record.get("conflicting_beacons") or 0)
        state = str(record.get("incarnation_state") or "unknown")
        if conflict > 1:
            state = f"{state}, conflict {conflict}"
        renew_note = (
            " | lease overdue — renew (--join)"
            if record.get("incarnation_state") == "live-overdue"
            else ""
        )
        lines.append(
            f"{label} | {record.get('idle')} | "
            f"{state} | "
            f"unread {unread if unread is not None else 'unknown'} | "
            f"owned {owned if owned is not None else 'unknown'}"
            f"{renew_note}"
        )
    return lines


def _stored_pid_principal(
    lease: goalflight_journal.LeaseIdentity,
) -> tuple[int, str] | None:
    """Return the stored PID-backed principal, or None if the lease has none."""
    principal = lease.principal if isinstance(lease.principal, dict) else {}
    pid = principal.get("pid")
    start_token = principal.get("start_token")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    token = str(start_token or "").strip()
    if not token:
        return None
    return pid, token


def _incumbent_nonce_refusal() -> dict:
    return {
        "retired": False,
        "reason": "retirer_not_incumbent",
        "message": "retirement requires the active lease nonce",
    }


def _renew_deadline_expired_for_dead_holder(
    lease: goalflight_journal.LeaseIdentity,
    *,
    now: datetime | None = None,
) -> tuple[bool, datetime | None]:
    deadline = _parse_utc(lease.renew_deadline_at)
    if deadline is None:
        return False, None
    measured_now = now or datetime.now(timezone.utc)
    if measured_now.tzinfo is None:
        measured_now = measured_now.replace(tzinfo=timezone.utc)
    expired = measured_now.astimezone(timezone.utc) >= (
        deadline + timedelta(seconds=DEAD_HOLDER_RETIRE_MARGIN_S)
    )
    return expired, deadline


def _dead_holder_retirement_gate(
    lease: goalflight_journal.LeaseIdentity,
    project_root: Path,
    *,
    label: str,
    ledger_records: list[dict] | None,
) -> dict | None:
    """Return a refusal, or None when nonce-less retirement is proven.

    Generation-lock state may be unknown or unreadable; that is the case this
    path exists for. Death is proven from the stored PID-backed principal, the
    renew deadline, and a measured-zero owned-dispatch count — never from an
    indeterminate probe.
    """
    stored = _stored_pid_principal(lease)
    if stored is None:
        return {
            "retired": False,
            "reason": "missing_stored_principal",
            "message": (
                "nonce-less retirement requires a stored PID-backed principal "
                "(pid + start_token); this lease has none. Repair the journal "
                "manually (t-238) or retire with the active lease nonce."
            ),
        }
    pid, _start_token = stored
    liveness = goalflight_compat.pid_liveness(pid)
    if liveness is None:
        return {
            "retired": False,
            "reason": "holder_liveness_indeterminate",
            "message": (
                f"stored principal pid {pid} liveness is indeterminate; "
                "the probe could not find out whether the holder is dead. "
                "Confirmed death is pid_liveness(...) is False; an "
                "EPERM/unavailable probe is not death."
            ),
            "controller_pid": pid,
        }
    if liveness is True:
        return _incumbent_nonce_refusal()
    live_state, _session = probe_live_session(project_root, label=label)
    if live_state == "live":
        return _incumbent_nonce_refusal()
    expired, deadline = _renew_deadline_expired_for_dead_holder(lease)
    if deadline is None:
        return {
            "retired": False,
            "reason": "renew_deadline_unreadable",
            "message": (
                "stored renew_deadline_at is missing or unparseable; "
                "nonce-less retirement cannot prove the deadline is past "
                f"one full lease horizon ({int(DEAD_HOLDER_RETIRE_MARGIN_S)}s). "
                "Repair the journal manually (t-238) or retire with the "
                "active lease nonce."
            ),
            "renew_deadline_at": lease.renew_deadline_at,
            "required_margin_s": DEAD_HOLDER_RETIRE_MARGIN_S,
        }
    if not expired:
        return {
            "retired": False,
            "reason": "renew_deadline_not_past_horizon",
            "message": (
                f"stored renew_deadline_at {deadline.isoformat()} is not past "
                f"by one full lease horizon ({int(DEAD_HOLDER_RETIRE_MARGIN_S)}s). "
                "Nonce-less retirement needs that margin so a merely-overdue "
                "or clock-skewed live holder cannot qualify."
            ),
            "renew_deadline_at": lease.renew_deadline_at,
            "required_margin_s": DEAD_HOLDER_RETIRE_MARGIN_S,
        }
    owned, owned_error = _nonterminal_owned_dispatches(
        goalflight_task.resolve_project_root(str(project_root)),
        records=ledger_records,
    )
    if owned is None:
        return {
            "retired": False,
            "reason": "owned_dispatches_unmeasured",
            "message": (
                "nonterminal owned dispatches could not be measured "
                f"({owned_error}); unknown is not zero. Fix the ledger read "
                "and retry, or retire with the active lease nonce."
            ),
            "owned_dispatch_measurement_error": owned_error,
        }
    owned_dispatches = owned.get(label, [])
    if owned_dispatches:
        return {
            "retired": False,
            "reason": "dead_holder_owns_nonterminal_dispatches",
            "message": (
                f"the lease still owns {len(owned_dispatches)} nonterminal "
                "dispatch(es); nonce-less retirement requires zero. Reap or "
                "rehome those dispatches, then retry."
            ),
            "nonterminal_owned_dispatches": owned_dispatches,
        }
    return None


def retire_controller(
    project_root: Path,
    name: str,
    *,
    pid: int | None = None,
    session_id: str | None = None,
    process_identity: dict | None = None,
    acknowledge: bool = False,
    messages_dir: Path | None = None,
    fleet_dir: Path | None = None,
    ledger_records: list[dict] | None = None,
) -> dict:
    """Retire the authenticated active lease; legacy mailbox digests do not exist."""
    label = _normalize_controller_label(name)
    if label is None:
        return {"retired": False, "reason": "missing_controller_label"}
    try:
        authority = goalflight_journal.Journal(project_root)
    except (
        goalflight_journal.JournalBusy,
        goalflight_journal.JournalIOError,
    ) as exc:
        return {
            "retired": False,
            "reason": "registry_unreadable",
            "error_type": type(exc).__name__,
            "message": str(exc),
        }
    except goalflight_journal.JournalDisappeared:
        return {"retired": False, "reason": "controller_not_registered"}
    lease = authority.active_lease(label)
    if lease is None:
        return {"retired": False, "reason": "controller_not_registered"}
    resolved_nonce = str(session_id or "")
    nonce_matches = bool(resolved_nonce) and resolved_nonce == lease.nonce
    if nonce_matches:
        if pid is not None:
            measured = _controller_process_identity(pid)
            expected = lease.principal
            if (
                measured is None
                or expected.get("pid") != pid
                or expected.get("start_token") != measured.get("start_token")
                or (process_identity is not None and process_identity != measured)
            ):
                return {"retired": False, "reason": "retirer_not_incumbent"}
        owned, owned_error = _nonterminal_owned_dispatches(
            goalflight_task.resolve_project_root(str(project_root)),
            records=ledger_records,
        )
        owned_dispatches = owned.get(label, []) if owned is not None else []
        if (owned_dispatches or owned_error) and not acknowledge:
            return {
                "retired": False,
                "reason": "retirement_requires_acknowledgement",
                "acknowledgement_flag": "--acknowledge-retirement",
                "nonterminal_owned_dispatches": owned_dispatches,
                "owned_dispatch_measurement_error": owned_error,
            }
        result = authority.release_lease(label, nonce=lease.nonce, reason="retired")
        if not result.committed or result.value is None:
            return {
                "retired": False,
                "reason": "retirement_cas_lost",
                "message": result.reason,
            }
        ended = result.value
        _publish_lease_generation_event(project_root, ended, only_if_present=True)
        return {
            "retired": True,
            "label": label,
            "generation": ended.generation,
            "retired_at": _now_iso(),
            "acknowledged": bool(owned_dispatches or owned_error),
        }

    refusal = _dead_holder_retirement_gate(
        lease,
        project_root,
        label=label,
        ledger_records=ledger_records,
    )
    if refusal is not None:
        return refusal
    result = authority.release_lease(
        label,
        nonce=lease.nonce,
        reason=DEAD_HOLDER_RELEASE_REASON,
    )
    if not result.committed or result.value is None:
        return {
            "retired": False,
            "reason": "retirement_cas_lost",
            "message": result.reason,
        }
    ended = result.value
    _publish_lease_generation_event(project_root, ended, only_if_present=True)
    return {
        "retired": True,
        "label": label,
        "generation": ended.generation,
        "retired_at": _now_iso(),
        "acknowledged": False,
        "release_reason": DEAD_HOLDER_RELEASE_REASON,
    }


def release_session(project_root: Path, *, pid: int) -> dict:
    """Release the active lease owned by this exact process generation."""
    try:
        authority = goalflight_journal.Journal(project_root)
    except (
        goalflight_journal.JournalBusy,
        goalflight_journal.JournalIOError,
    ) as exc:
        return {
            "released": False,
            "reason": "registry_unreadable",
            "error_type": type(exc).__name__,
            "message": str(exc),
        }
    except goalflight_journal.JournalDisappeared:
        return {"released": False, "reason": "controller_not_registered"}
    measured = _controller_process_identity(pid)
    if measured is None:
        return {"released": False}
    for row in authority.lease_records():
        principal = json.loads(str(row.get("principal_json") or "{}"))
        if principal.get("pid") != pid or principal.get("start_token") != measured.get("start_token"):
            continue
        released = authority.release_lease(
            str(row["label"]),
            nonce=str(row["nonce"]),
            reason="released",
        )
        if released.committed and released.value is not None:
            _publish_lease_generation_event(
                project_root,
                released.value,
                only_if_present=True,
            )
        return {"released": bool(released.committed)}
    return {"released": False}


def ensure_session(project_root: Path, *, pid: int | None = None) -> dict:
    """Read or generate the per-terminal session id record.

    Per-terminal scope: the session record is keyed by `(project_root, pid)`.
    The file lives at `project_root/docs-private/.goal-flight-current-session.json`
    but the persisted shape is a MAP of `pid -> record`, so two terminals in
    the same project_root each have their own slot. Within a single PID the
    record persists across compactions; across PIDs they are independent.

    Returns dict with id/pid/started_at/hostname for the CURRENT PID. If the
    file already has a record for this PID (e.g., earlier command in the
    same terminal), returns it. Otherwise creates a fresh record.

    The session-file mutation runs under a session-file lock + atomic
    write with a unique temp path. Two concurrent ensure_session()s from
    different terminals serialize on the lock, then merge their writes
    safely (each adds its own pid slot without clobbering the other).
    """
    pid = pid or os.getpid()
    path = _session_file(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _file_lock(path):
        data: dict[str, dict] = {}
        if path.exists():
            try:
                raw = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                raw = None
            # Back-compat: previous shape was a single record without a pid map.
            # If we find that, migrate it under its own pid key.
            if isinstance(raw, dict):
                if "id" in raw and "pid" in raw and not all(
                    isinstance(v, dict) for v in raw.values()
                ):
                    data = {str(raw.get("pid")): raw}
                else:
                    # Map-shape: keys are pid strings, values are records.
                    data = {str(k): v for k, v in raw.items() if isinstance(v, dict)}
        key = str(pid)
        if key in data:
            # Existing record for this PID — return it as-is. Pruning of
            # dead-pid slots from OTHER terminals is a maintenance concern
            # handled by --force-release-stale, not the ensure_session path
            # (which is hot — runs on every CLI invocation in a goal-flight
            # terminal).
            result = data[key]
        else:
            result = {
                "id": str(uuid.uuid4()),
                "pid": pid,
                "started_at": _now_iso(),
                "hostname": socket.gethostname(),
            }
            data[key] = result
        # Atomic write via unique temp file rename. Unique suffix prevents
        # concurrent ensure_session()s from clobbering each other's temp
        # files (lock-serialized but defensive).
        tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}.{uuid.uuid4().hex[:8]}")
        tmp.write_text(json.dumps(data, indent=2) + "\n")
        tmp.replace(path)
    return result


def _pid_alive(pid: object) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    return goalflight_compat.pid_alive(pid)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# --- queue frontmatter parsing ----------------------------------------------


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Return (frontmatter_dict, body) for a markdown file with optional
    YAML frontmatter. Empty dict if no frontmatter.

    We use a minimal YAML-subset parser to avoid the PyYAML dependency;
    this is intentional and matches `validate_no_host_tool_leaks`-style
    procedural parsing elsewhere.
    """
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, text
    raw = text[4:end]
    body = text[end + 5 :]
    return _parse_yaml_subset(raw), body


def _parse_yaml_subset(raw: str) -> dict:
    """Tiny YAML-subset parser. Supports `key: value` flat lines,
    `key:` with a nested block of `  - item` or `  subkey: ...`, and
    skips empty lines + comments. Values are strings unless they parse
    as JSON literal.
    """
    out: dict = {}
    lines = raw.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue
        if ":" in line and not line.startswith(" "):
            key, _, val = line.partition(":")
            key = key.strip()
            val = val.strip()
            if val:
                out[key] = _coerce_scalar(val)
                i += 1
                continue
            # Nested block starts on next line(s) — list or map.
            i += 1
            nested_lines: list[str] = []
            while i < len(lines) and (lines[i].startswith("  ") or not lines[i].strip()):
                nested_lines.append(lines[i])
                i += 1
            out[key] = _parse_nested(nested_lines)
            continue
        i += 1
    return out


def _parse_nested(lines: list[str]) -> list | dict:
    list_items: list = []
    map_items: dict = {}
    saw_dash = False
    saw_map = False
    current_item: dict | None = None
    for raw in lines:
        if not raw.strip():
            continue
        if raw.startswith("  - "):
            saw_dash = True
            tail = raw[4:].strip()
            if tail and ":" in tail and not tail.startswith("{"):
                # Inline `- key: val` starts a new map item.
                current_item = {}
                key, _, val = tail.partition(":")
                current_item[key.strip()] = _coerce_scalar(val.strip())
                list_items.append(current_item)
            else:
                list_items.append(_coerce_scalar(tail))
                current_item = None
        elif raw.startswith("    ") and current_item is not None:
            inner = raw.strip()
            if ":" in inner:
                key, _, val = inner.partition(":")
                current_item[key.strip()] = _coerce_scalar(val.strip())
        elif raw.startswith("  ") and ":" in raw:
            saw_map = True
            inner = raw[2:]
            key, _, val = inner.partition(":")
            map_items[key.strip()] = _coerce_scalar(val.strip())
    if saw_dash and not saw_map:
        return list_items
    if saw_map and not saw_dash:
        return map_items
    return list_items or map_items


def _coerce_scalar(val: str):
    if not val:
        return ""
    try:
        return json.loads(val)
    except (json.JSONDecodeError, ValueError):
        pass
    return val.strip().strip('"').strip("'")


def _dump_frontmatter(data: dict) -> str:
    """Emit the minimal-YAML form `_parse_yaml_subset` accepts. Order is
    preserved from dict insertion order so existing files stay stable.
    """
    out = ["---"]
    for key, value in data.items():
        out.extend(_dump_pair(key, value, 0))
    out.append("---")
    out.append("")
    return "\n".join(out)


def _dump_pair(key: str, value, depth: int) -> list[str]:
    indent = "  " * depth
    if isinstance(value, dict):
        lines = [f"{indent}{key}:"]
        for k, v in value.items():
            lines.extend(_dump_pair(k, v, depth + 1))
        return lines
    if isinstance(value, list):
        if not value:
            return [f"{indent}{key}: []"]
        lines = [f"{indent}{key}:"]
        for item in value:
            if isinstance(item, dict):
                first = True
                for k, v in item.items():
                    prefix = f"{indent}  - " if first else f"{indent}    "
                    lines.append(f"{prefix}{k}: {_dump_scalar(v)}")
                    first = False
            else:
                lines.append(f"{indent}  - {_dump_scalar(item)}")
        return lines
    return [f"{indent}{key}: {_dump_scalar(value)}"]


def _dump_scalar(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    s = str(value)
    if any(c in s for c in [":", "#", "\n"]) or s.strip() != s:
        return json.dumps(s)
    return s


# --- queue discovery + state aggregation ------------------------------------


def find_queues(project_root: Path) -> list[Path]:
    return sorted(project_root.glob(QUEUE_GLOB))


def find_resume_notes(project_root: Path) -> list[Path]:
    return sorted(project_root.glob(RESUME_NOTES_GLOB))


def newest(paths: list[Path]) -> Path | None:
    if not paths:
        return None
    return max(paths, key=lambda p: p.stat().st_mtime)


def read_queue_state(path: Path) -> dict:
    """Return parsed frontmatter for a goal-queue file, or {} on parse error."""
    try:
        text = path.read_text()
    except OSError:
        return {}
    front, _ = _parse_frontmatter(text)
    return front


def queue_active_now(front: dict, *, ttl_days: int = 7) -> tuple[bool, str]:
    """Return (is_active, reason). Active iff `state: active` AND last-touched
    within ttl. `state: active` without last-touched is treated as active.
    """
    state = str(front.get("state", "")).lower()
    if state != "active":
        return False, f"state={state or 'missing'}"
    last = front.get("last-touched") or front.get("last_touched")
    if not last:
        return True, "active (no last-touched stamp)"
    try:
        ts = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
    except ValueError:
        return True, "active (last-touched unparseable)"
    age_s = (datetime.now(timezone.utc) - ts).total_seconds()
    if age_s > ttl_days * 86400:
        return False, f"active but last-touched > {ttl_days}d ago (treat abandoned)"
    return True, f"active (last-touched {int(age_s)}s ago)"


def aggregate_status(project_root: Path, *, ttl_days: int = 7) -> dict:
    """Union the three signals (queue / leases / resume-notes) and return
    a single verdict with full breakdown. See module docstring.
    """
    queues = find_queues(project_root)
    newest_queue = newest(queues)
    queue_front: dict = {}
    queue_active = False
    queue_reason = "no queue files"
    if newest_queue is not None:
        queue_front = read_queue_state(newest_queue)
        queue_active, queue_reason = queue_active_now(queue_front, ttl_days=ttl_days)
    leases_for_project = _active_leases_for(project_root)
    notes = find_resume_notes(project_root)
    newest_notes = newest(notes)
    notes_active, notes_reason = _resume_notes_active(newest_notes, ttl_days=ttl_days)
    active = queue_active or bool(leases_for_project) or notes_active
    backlog_counts, backlog_error = _task_backlog_counts(project_root)
    ready_frontier, ready_frontier_error = _ready_frontier(project_root)
    return {
        "active": active,
        "queue_file": str(newest_queue.relative_to(project_root)) if newest_queue else None,
        "queue_state": queue_front.get("state"),
        "queue_reason": queue_reason,
        "queue_slug": queue_front.get("slug"),
        "queue_last_touched": queue_front.get("last-touched") or queue_front.get("last_touched"),
        "queue_current_session": queue_front.get("current_session"),
        "active_capacity_leases_in_project": len(leases_for_project),
        "active_capacity_lease_dispatch_ids": [
            lease.get("dispatch_id") for lease in leases_for_project
        ],
        "newest_resume_notes": str(newest_notes.relative_to(project_root)) if newest_notes else None,
        "resume_notes_active": notes_active,
        "resume_notes_reason": notes_reason,
        "backlog_counts": backlog_counts,
        "backlog_error": backlog_error,
        "ready_frontier": ready_frontier,
        "ready_frontier_error": ready_frontier_error,
        "ttl_days": ttl_days,
    }


def _task_backlog_counts(project_root: Path) -> tuple[dict[str, int] | None, str | None]:
    # Read through the canonical store, not the in-tree export: the export can be
    # absent/stale (e.g. a sync race removed it) while the durable store is intact.
    # goalflight_task.list() reads canonical and returns [] for an absent store.
    tasks_path = project_root / "docs-private" / "tasks.jsonl"
    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        import goalflight_task

        rows = goalflight_task.list(project_root=project_root)
    except goalflight_task.TaskError as exc:
        # A present-but-unreadable store degrades to a counted absence with the
        # reason surfaced; it must not crash session status. TaskError cannot
        # live in _EXPECTED_OPTIONAL_ERRORS because goalflight_task imports
        # lazily inside this function.
        return None, f"{tasks_path}: {exc}"
    except _EXPECTED_OPTIONAL_ERRORS as exc:
        return None, f"{tasks_path}: {exc}"

    def done_reviewed(row: dict) -> bool:
        return row.get("done_reviewed") is True or (row.get("kind") == "decision" and row.get("done") is True)

    counts = {"deferred": 0, "held": 0, "blocked": 0}
    for row in rows:
        if done_reviewed(row):
            continue
        lane = row.get("lane")
        if lane in ("deferred", "held"):
            counts[lane] += 1
            continue
        if row.get("derived_status") == "waiting":
            counts["blocked"] += 1
    return counts, None


def _ready_frontier(project_root: Path) -> tuple[dict[str, object] | None, str | None]:
    # Read through the canonical store, not the in-tree export (which can be
    # absent/stale after a sync race); next_frontier() reads canonical.
    tasks_path = project_root / "docs-private" / "tasks.jsonl"
    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        import goalflight_task

        rows = goalflight_task.TaskStore(project_root).next_frontier()
    except goalflight_task.TaskError as exc:
        # Same degradation contract as _task_backlog_counts above.
        return None, f"{tasks_path}: {exc}"
    except _EXPECTED_OPTIONAL_ERRORS as exc:
        return None, f"{tasks_path}: {exc}"
    if not rows:
        return {"count": 0}, None
    top = rows[0]
    prompt_path = top.get("prompt_path")
    return {
        "count": len(rows),
        "top_id": str(top.get("id") or ""),
        "top_title": str(top.get("title") or ""),
        "prompt_path": prompt_path if isinstance(prompt_path, str) and prompt_path else None,
    }, None


def _post_resume_nudge(project_root: Path) -> None:
    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        import goalflight_task

        goalflight_task.post_resume_nudge(project_root)
    except _EXPECTED_OPTIONAL_ERRORS:
        # Optional controller-attention hint only. Losing it gives up one nudge;
        # queue and lease state remain authoritative.
        return


def _backlog_counts_text(status: dict) -> str | None:
    if status.get("backlog_counts") is None and status.get("backlog_error"):
        return "backlog: store read degraded"
    counts = status.get("backlog_counts") or {}
    parts = []
    for label in ("deferred", "held", "blocked"):
        value = int(counts.get(label) or 0)
        if value > 0:
            parts.append(f"{value} {label}")
    return " · ".join(parts) if parts else None


def _resume_directive_text(status: dict) -> str | None:
    frontier = status.get("ready_frontier")
    if not isinstance(frontier, dict):
        return None
    count = int(frontier.get("count") or 0)
    if count <= 0:
        return None
    top_id = str(frontier.get("top_id") or "").strip()
    top_title = str(frontier.get("top_title") or "").strip().replace("\n", " ")
    top = f" ({top_id} {top_title})" if top_id else ""
    return f"resume: run python3 goalflight_task.py next -> continue the top task{top}"


def _resume_notes_active(notes_path: Path | None, *, ttl_days: int = 7) -> tuple[bool, str]:
    """Read the newest RESUME-NOTES file and infer activation state from its
    front matter (if YAML) or its TL;DR section. Tolerant by design: if the
    file is unparseable or has no signal, returns (False, "no signal").

    Signals (any one is enough for active=True):
      - YAML frontmatter `state: active` (canonical)
      - First H1 / TL;DR section contains "**Status:** active" or
        "**Active**" or "in flight" line
      - File mtime within TTL AND title matches a date stamp (heuristic)

    Reads at most 2KB to avoid pulling in entire long notes files.
    """
    if notes_path is None or not notes_path.exists():
        return False, "no resume notes"
    try:
        head = notes_path.read_text(encoding="utf-8", errors="ignore")[:2048]
    except OSError:
        return False, "resume notes unreadable"
    # Try YAML frontmatter first.
    if head.startswith("---\n"):
        front, _ = _parse_frontmatter(head)
        state = str(front.get("state", "")).lower()
        if state == "active":
            return True, "frontmatter state: active"
        if state in ("complete", "done", "completed", "abandoned"):
            return False, f"frontmatter state: {state}"
    head_lower = head.lower()
    # Look for explicit active/complete signals in TL;DR-style prose.
    if "**status:** active" in head_lower or "status: active" in head_lower:
        return True, "TL;DR Status: active"
    if "in flight" in head_lower or "in-flight" in head_lower:
        return True, "TL;DR mentions in-flight"
    if (
        "**status:** complete" in head_lower
        or "all chunks done" in head_lower
        or "push pending" in head_lower
        or "all chunks committed" in head_lower
    ):
        return False, "TL;DR complete-state signal"
    return False, "no signal"


def _active_leases_for(project_root: Path) -> list[dict]:
    """Call goalflight_capacity.py status --json, return active leases
    whose project_root matches ours. Best-effort: empty list on any failure.
    """
    try:
        import subprocess

        out = subprocess.run(
            [goalflight_compat.python_executable(), str(ROOT / "scripts/goalflight_capacity.py"), "status", "--json"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        if out.returncode != 0:
            return []
        data = json.loads(out.stdout or "{}")
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
        return []
    # capacity status JSON: `{"active": [<lease>, ...]}` already filtered.
    active = data.get("active") or []
    target = str(project_root.resolve())
    matched: list[dict] = []
    for lease in active:
        if lease.get("state") and lease.get("state") != "active":
            continue
        lp = lease.get("project_root")
        if lp and str(Path(lp).resolve()) == target:
            matched.append(lease)
    return matched


def to_text(status: dict) -> str:
    counts_text = _backlog_counts_text(status)
    resume_text = _resume_directive_text(status)
    if not status["active"]:
        if status["queue_file"] is None:
            text = "no goal-flight queue files; not an active session"
        else:
            text = (
            f"no active goal-flight session (queue {status['queue_file']} "
            f"state={status['queue_state'] or 'unset'}; "
            f"{status['queue_reason']})"
            )
        pieces = [text]
        if counts_text:
            pieces.append(counts_text)
        if resume_text:
            pieces.append(resume_text)
        return "; ".join(pieces)
    pieces = [
        f"active goal-flight session ({status['queue_slug'] or 'unnamed'})",
        f"queue={status['queue_file']}",
        f"capacity_leases={status['active_capacity_leases_in_project']}",
    ]
    if status["queue_last_touched"]:
        pieces.append(f"last-touched={status['queue_last_touched']}")
    if counts_text:
        pieces.append(counts_text)
    if resume_text:
        pieces.append(resume_text)
    return "; ".join(pieces)


# --- claim / release --------------------------------------------------------


def _validate_queue_in_project(project_root: Path, queue: Path) -> Path | None:
    """Resolve queue path and ensure it lives under project_root/docs-private/.
    Returns the resolved path on success, None if it escapes scope.
    Review A P3: refuse out-of-scope --queue arguments.
    """
    target = queue.resolve()
    expected_root = (project_root / "docs-private").resolve()
    try:
        target.relative_to(expected_root)
    except ValueError:
        return None
    return target


def _file_lock(path: Path):
    """Per-file lock using fcntl.flock. Context-manager that opens
    `path.lock` and acquires an exclusive lock; releases on exit.
    Two concurrent claims on the same queue now serialize.

    Network filesystems (NFS, SMB) sometimes refuse `fcntl.flock` with
    `ENOLCK` or `EOPNOTSUPP`. We catch those, emit a single stderr
    diagnostic, and fall through to lock-free execution. The race is
    bounded by the atomic write semantics (temp + rename) so even
    without locking, two concurrent writers degrade to "last-writer-
    wins on the lost slot" rather than crash.
    """
    import contextlib
    import goalflight_compat as fcntl

    @contextlib.contextmanager
    def _ctx():
        lock_path = path.with_suffix(path.suffix + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT, 0o600)
        except OSError as exc:
            sys.stderr.write(
                f"goalflight_session_status: lock open failed for {lock_path} "
                f"({exc.__class__.__name__}: {exc}); proceeding lock-free. "
                "On a network FS without flock support, two concurrent "
                "writers can lose a slot; recover with --force-release-stale.\n"
            )
            yield
            return
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
            except OSError as exc:
                sys.stderr.write(
                    f"goalflight_session_status: flock unsupported on "
                    f"{lock_path} ({exc.__class__.__name__}); proceeding lock-free.\n"
                )
                yield
                return
            try:
                yield
            finally:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass  # already unlocked / unsupported
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

    return _ctx()


def claim(project_root: Path, queue: Path, *, force: bool = False) -> tuple[bool, str]:
    """Stamp current session into queue frontmatter. Refuses on live owner.

    Uses an exclusive file lock to serialize concurrent claims. Reads + mutates
    + writes the queue inside the lock so two terminals' claims can't both
    win. Verifies the queue path is under project_root/docs-private (A P3).
    """
    if not queue.exists():
        return False, f"queue not found: {queue}"
    resolved = _validate_queue_in_project(project_root, queue)
    if resolved is None:
        return False, (
            f"queue {queue} is outside {project_root}/docs-private/; refusing"
        )
    queue = resolved
    with _file_lock(queue):
        text = queue.read_text()
        front, body = _parse_frontmatter(text)
        if not front:
            return False, f"queue {queue.name} has no frontmatter to stamp into"
        session = ensure_session(project_root)
        current = front.get("current_session")
        if isinstance(current, dict) and current.get("id") and current.get("id") != session["id"]:
            owner_alive = _pid_alive(current.get("pid"))
            if owner_alive and not force:
                return False, (
                    f"queue already claimed by session {current.get('id')} "
                    f"(pid {current.get('pid')} alive); pass --force to take over"
                )
        history = list(front.get("session_history") or [])
        history.append({
            "id": session["id"],
            "pid": session["pid"],
            "started_at": session["started_at"],
            "claimed_at": _now_iso(),
            "ended_at": None,
            "ended_reason": None,
        })
        front["current_session"] = {
            "id": session["id"],
            "pid": session["pid"],
            "started_at": session["started_at"],
            "hostname": session["hostname"],
        }
        front["session_history"] = history
        front["last-touched"] = _now_iso()
        _atomic_write(queue, _dump_frontmatter(front) + body)
    return True, f"claimed by session {session['id']}"


def _atomic_write(path: Path, content: str) -> None:
    """Write atomically via temp + rename — avoids torn writes if interrupted."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content)
    tmp.replace(path)


def release(project_root: Path, queue: Path | None, *, reason: str = "user-exit") -> tuple[bool, str]:
    """Mark session ended in queue frontmatter (if --queue) and clear the
    per-terminal session file.

    Compare-after-read: only releases when current_session.id matches THIS
    terminal's session id; refuses if a different session owns it. Use
    --force-release-stale or claim --force to take over instead.
    """
    msgs: list[str] = []
    if queue is not None and queue.exists():
        resolved = _validate_queue_in_project(project_root, queue)
        if resolved is None:
            return False, f"queue {queue} is outside {project_root}/docs-private/; refusing"
        queue = resolved
        my_session = ensure_session(project_root)
        with _file_lock(queue):
            text = queue.read_text()
            front, body = _parse_frontmatter(text)
            if front:
                current = front.get("current_session")
                if isinstance(current, dict) and current.get("id"):
                    if current.get("id") != my_session["id"]:
                        return False, (
                            f"queue current_session={current.get('id')} is not "
                            f"this terminal's session ({my_session['id']}); "
                            "refusing to release. Use --force-release-stale "
                            "or claim --force to take over."
                        )
                history = list(front.get("session_history") or [])
                if isinstance(current, dict) and current.get("id"):
                    session_id = current.get("id")
                    for entry in reversed(history):
                        if isinstance(entry, dict) and entry.get("id") == session_id and entry.get("ended_at") is None:
                            entry["ended_at"] = _now_iso()
                            entry["ended_reason"] = reason
                            break
                front["current_session"] = None
                front["session_history"] = history
                front["last-touched"] = _now_iso()
                _atomic_write(queue, _dump_frontmatter(front) + body)
                msgs.append(f"released queue {queue.name}")
    # Per-terminal session map: remove only this PID's slot, keep others.
    # Lock + atomic write — same discipline as ensure_session.
    sf = _session_file(project_root)
    if sf.exists():
        with _file_lock(sf):
            try:
                raw = json.loads(sf.read_text())
            except (json.JSONDecodeError, OSError):
                raw = None
            my_pid = str(os.getpid())
            if isinstance(raw, dict):
                if "id" in raw and "pid" in raw and not all(
                    isinstance(v, dict) for v in raw.values()
                ):
                    # Old single-record shape (back-compat) — drop the whole file.
                    sf.unlink()
                    msgs.append("cleared session file (back-compat)")
                else:
                    # Map shape: remove only this pid's slot.
                    if my_pid in raw:
                        del raw[my_pid]
                    if raw:
                        _atomic_write(sf, json.dumps(raw, indent=2) + "\n")
                    else:
                        sf.unlink()
                    msgs.append("cleared this terminal's session slot")
    if not msgs:
        return False, "nothing to release"
    return True, "; ".join(msgs)


def force_release_stale(project_root: Path) -> tuple[int, list[str]]:
    """Across all goal-queues, clear current_session where pid is dead.
    Locks each queue for the duration of its mutation."""
    cleared: list[str] = []
    for queue in find_queues(project_root):
        with _file_lock(queue):
            front, body = _parse_frontmatter(queue.read_text())
            current = front.get("current_session")
            if isinstance(current, dict) and current.get("pid") and not _pid_alive(current.get("pid")):
                history = list(front.get("session_history") or [])
                for entry in reversed(history):
                    if isinstance(entry, dict) and entry.get("id") == current.get("id") and entry.get("ended_at") is None:
                        entry["ended_at"] = _now_iso()
                        entry["ended_reason"] = "stale-pid"
                        break
                front["current_session"] = None
                front["session_history"] = history
                front["last-touched"] = _now_iso()
                _atomic_write(queue, _dump_frontmatter(front) + body)
                cleared.append(queue.name)
    return len(cleared), cleared


# --- CLI --------------------------------------------------------------------


def _default_project_root() -> str:
    """Cwd-stable default for --project-root: prefer the git toplevel of
    the current working directory; fall back to cwd if not in a git repo.
    Sweep C P1 fix — invocations from subdirs now resolve to the repo
    root automatically.
    """
    project_root = _git_project_root()
    return str(project_root if project_root is not None else Path.cwd())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="goal-flight session status helper")
    parser.add_argument("--project-root", default=_default_project_root())
    parser.add_argument("--ttl-days", type=int, default=7)
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true")
    output.add_argument("--text", action="store_true")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--ensure-session", action="store_true")
    mode.add_argument("--claim", action="store_true")
    mode.add_argument("--release", action="store_true")
    mode.add_argument("--force-release-stale", action="store_true")
    mode.add_argument(
        "--claim-session",
        action="store_true",
        help="bind a session id to a beacon pid (--session-pid, default: this process)",
    )
    mode.add_argument(
        "--controller-startup",
        action="store_true",
        help=(
            "best-effort named controller registration from --session-pid/"
            "GOALFLIGHT_CONTROLLER_PID and --session-label/"
            "GOALFLIGHT_CONTROLLER_LABEL; always exits successfully"
        ),
    )
    mode.add_argument(
        "--live-session",
        action="store_true",
        help="print the live beacon session for this project, or exit 1 if none",
    )
    mode.add_argument(
        "--release-session",
        action="store_true",
        help="drop a beacon slot (--session-pid, default: this process)",
    )
    mode.add_argument("--hold-controller-lock", action="store_true", help=argparse.SUPPRESS)
    mode.add_argument(
        "--list-controllers",
        action="store_true",
        help="print the durable controller roster; combine with --json for machine output",
    )
    mode.add_argument("--join", metavar="NAME", help="join an existing durable controller name")
    mode.add_argument("--register", metavar="NAME", help="register a new durable controller name")
    mode.add_argument("--retire", metavar="NAME", help="digest mail and retire a controller name")
    parser.add_argument("--session-pid", type=int)
    parser.add_argument("--session-label")
    parser.add_argument("--controller-start-token", help=argparse.SUPPRESS)
    parser.add_argument(
        "--controller-session-id",
        help=(
            "incarnation id returned by register/join; default: "
            "GOALFLIGHT_CONTROLLER_SESSION_ID"
        ),
    )
    parser.add_argument(
        "--controller-pid-from-ancestry",
        action="store_true",
        help=(
            "with --controller-startup or --claim-session, claim the measured durable host session "
            "leader instead of requiring a declared PID"
        ),
    )
    parser.add_argument("--queue")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--reason", default="user-exit")
    parser.add_argument("--include-retired", action="store_true")
    parser.add_argument("--acknowledge-controller-conflict", action="store_true")
    parser.add_argument("--acknowledge-retirement", action="store_true")
    parser.add_argument(
        "--takeover",
        action="store_true",
        help=(
            "with --controller-startup or --claim-session, deliberately supersede "
            "a live different holder of the requested label"
        ),
    )
    args = parser.parse_args(argv)
    if args.takeover and not (args.controller_startup or args.claim_session):
        parser.error("--takeover requires --controller-startup or --claim-session")
    project_root = goalflight_task.resolve_project_root(args.project_root)

    if args.hold_controller_lock:
        if (
            args.session_pid is None
            or not args.session_label
            or not args.controller_session_id
            or not args.controller_start_token
        ):
            return 2
        return hold_controller_lock(
            project_root,
            label=args.session_label,
            nonce=args.controller_session_id,
            pid=args.session_pid,
            start_token=args.controller_start_token,
        )

    try:
        import goalflight_messages

        goalflight_messages.emit_wake_entry_notice(
            project_root=project_root,
            controller_label=args.session_label,
            stream=sys.stderr,
        )
    except _EXPECTED_OPTIONAL_ERRORS:
        pass

    if args.list_controllers:
        roster = controller_roster(project_root, include_retired=args.include_retired)
        if args.json:
            print(json.dumps(roster, indent=2, sort_keys=True))
        else:
            lines = controller_roster_lines(roster)
            print("\n".join(lines) if lines else "no known controllers")
        return 0

    if args.register or args.join or args.retire:
        resolution, pid_error = _resolve_optional_incarnation(
            args.session_pid,
            pid_from_ancestry=args.controller_pid_from_ancestry,
        )
        if pid_error is not None:
            action = "registered" if args.register else "joined" if args.join else "retired"
            print(json.dumps({action: False, **pid_error}, sort_keys=True))
            return 2
        resolved_pid = int(resolution["pid"]) if resolution else None
        process_identity = resolution.get("process_identity") if resolution else None
        resolved_session_id = resolve_controller_session_id(args.controller_session_id)
        if args.register:
            result = register_controller(
                project_root,
                args.register,
                pid=resolved_pid,
                session_id=resolved_session_id,
                process_identity=process_identity,
                hold_lock=True,
            )
            if resolution and resolution.get("warning"):
                result.setdefault("warnings", []).append(resolution["warning"])
            if result.get("registered"):
                _index_controller_project(project_root)
            print(json.dumps(result, sort_keys=True))
            return 0 if result.get("registered") else 2
        if args.join:
            result = join_controller(
                project_root,
                args.join,
                pid=resolved_pid,
                session_id=resolved_session_id,
                acknowledge_conflict=args.acknowledge_controller_conflict,
                process_identity=process_identity,
                hold_lock=True,
            )
            if resolution and resolution.get("warning"):
                result.setdefault("warnings", []).append(resolution["warning"])
            if result.get("joined"):
                _index_controller_project(project_root)
            print(json.dumps(result, sort_keys=True))
            return 0 if result.get("joined") else 2
        result = retire_controller(
            project_root,
            args.retire,
            pid=resolved_pid,
            session_id=resolved_session_id,
            process_identity=process_identity,
            acknowledge=args.acknowledge_retirement,
        )
        if resolution and resolution.get("warning"):
            result.setdefault("warnings", []).append(resolution["warning"])
        print(json.dumps(result, sort_keys=True))
        return 0 if result.get("retired") else 2

    if args.ensure_session:
        record = ensure_session(project_root)
        print(json.dumps(record))
        return 0

    if args.claim_session:
        resolution, pid_error = _resolve_optional_incarnation(
            args.session_pid,
            pid_from_ancestry=args.controller_pid_from_ancestry,
            default_to_current=True,
        )
        if pid_error is not None or resolution is None:
            print(json.dumps({"claimed": False, **(pid_error or {"reason": "missing_controller_pid"})}))
            return 0
        try:
            record = claim_session(
                project_root,
                pid=int(resolution["pid"]),
                session_id=resolve_controller_session_id(args.controller_session_id),
                label=(
                    args.session_label
                    or resolve_controller_label(project_root=project_root, environ=os.environ)
                ),
                process_identity=resolution.get("process_identity"),
                takeover=args.takeover,
                hold_lock=True,
            )
        except (
            goalflight_journal.JournalBusy,
            goalflight_journal.JournalDisappeared,
            goalflight_journal.JournalIOError,
        ) as exc:
            print(
                json.dumps(
                    {
                        "claimed": False,
                        "reason": "claim_failed",
                        "error_type": type(exc).__name__,
                    }
                )
            )
            return 0
        except goalflight_journal.JournalError:
            raise
        except (ImportError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
            print(
                json.dumps(
                    {
                        "claimed": False,
                        "reason": "claim_failed",
                        "error_type": type(exc).__name__,
                    }
                )
            )
            return 0
        _index_controller_project(project_root)
        payload = dict(record)
        if resolution.get("warning"):
            payload["warnings"] = [resolution["warning"]]
        print(json.dumps(payload))
        return 0

    if args.controller_startup:
        result = claim_controller_startup(
            project_root,
            pid=args.session_pid,
            label=args.session_label,
            pid_from_ancestry=args.controller_pid_from_ancestry,
            session_id=args.controller_session_id,
            takeover=args.takeover,
            hold_lock=True,
        )
        if result.get("claimed"):
            _index_controller_project(project_root)
        adopted_label = result.get("adopted_label")
        requested_label = result.get("requested_label")
        session = result.get("session")
        if (
            isinstance(adopted_label, str)
            and isinstance(requested_label, str)
            and isinstance(session, dict)
            and isinstance(session.get("pid"), int)
        ):
            print(
                _controller_adoption_notice(
                    project_root,
                    adopted_label=adopted_label,
                    requested_label=requested_label,
                    pid=int(session["pid"]),
                ),
                file=sys.stderr,
            )
        print(json.dumps(result))
        return 0

    if args.live_session:
        record = live_session(project_root)
        if record is None:
            # Exit 1, not an empty object: 'no controller has claimed this
            # project' must not be mistaken for a session with blank fields.
            print("no live controller session for this project", file=sys.stderr)
            return 1
        print(json.dumps(record))
        return 0

    if args.release_session:
        result = release_session(project_root, pid=args.session_pid or os.getpid())
        print(json.dumps(result))
        return 0 if result.get("released") else 1

    if args.claim:
        if not args.queue:
            parser.error("--claim requires --queue")
        ok, msg = claim(project_root, Path(args.queue).resolve(), force=args.force)
        print(msg)
        return 0 if ok else 2

    if args.release:
        queue = Path(args.queue).resolve() if args.queue else None
        ok, msg = release(project_root, queue, reason=args.reason)
        print(msg)
        return 0 if ok else 2

    if args.force_release_stale:
        count, names = force_release_stale(project_root)
        print(json.dumps({"cleared": count, "files": names}))
        return 0

    status = aggregate_status(project_root, ttl_days=args.ttl_days)
    if args.text and status.get("active"):
        _post_resume_nudge(project_root)
    if args.json or not args.text:
        # Default to JSON for machine consumers; --text for humans.
        if args.text:
            print(to_text(status))
        else:
            print(json.dumps(status, indent=2))
    else:
        print(to_text(status))
    try:
        import goalflight_messages

        goalflight_messages.emit_listener_activity_signal(
            project_root=project_root,
            controller_label=args.session_label,
        )
    except _EXPECTED_OPTIONAL_ERRORS:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
