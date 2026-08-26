#!/usr/bin/env python3
"""Backend-only, shareable projections for the Goal Flight fleet console.

This module consumes the aggregate status authority, then uses bounded
read-only journal and status-sidecar evidence to explain or reconcile authority
disagreements. It does not inspect tails or marker bodies, and it does not invent
a worker classification. The attention plane samples the process table once when
HUNG controllers need supervision-aware recovery advice. Fleet and attention
samples remain independent so a fast mailbox refresh never pretends to refresh
worker liveness.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import datetime as dt
import hashlib
import json
import math
import os
import re
import shlex
import sqlite3
import sys
import uuid
from pathlib import Path
from typing import Any, Callable

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(ROOT))

import goalflight_compat
import goalflight_dispatch_states
import goalflight_fleet_console_history
import goalflight_fleet_status_cli
import goalflight_journal
import goalflight_messages
import goalflight_session_status
import goalflight_status
import goalflight_task
import goalflight_usage
import goalflight_wake


FLEET_SCHEMA = "goalflight.fleet-console.fleet.v2"
ATTENTION_SCHEMA = "goalflight.fleet-console.attention.v1"
PRODUCER_NAME = "goalflight_fleet_console.py"
SCRIPT_GLOBALS = {"fleet": "GF_FLEET", "attention": "GF_ATTENTION"}
# Standalone/manual defaults mirror the installer. Installed launchd jobs pass
# their StartInterval explicitly, so that value remains authoritative if these
# defaults and a deployed schedule ever diverge again.
PLANE_CADENCE_SECONDS = {"attention": 20, "fleet": 60}

# Head of the recency-ordered project registry to sample per tick. See
# _registered_projects for the measurement that sets this: the per-project
# cost is ~1.0s, and the drain tick this rides is ~60s, so the whole
# per-project pass has to fit in a fraction of that alongside everything else.
DEFAULT_MAX_PROJECTS = 12
# Basenames that collide across scratch/tmp checkouts. Prefer repo_identity;
# these leftovers get a short project_id suffix so the omitted list is readable.
GENERIC_PROJECT_BASENAMES = frozenset({"project", "proj"})

# Only ACTIVE controller lease generations belong to the short-poll plane.
# Bound even malformed/multi-active history to the newest eight per label;
# ended generations remain available through immutable slow history.
CONTROLLER_GENERATION_PROBE_LIMIT = 8
# The attention producer has a 3s wall budget and an observed 1.12s baseline.
# A stuck process-table read must degrade to UNKNOWN without preserving a stale
# ABSENT/direct-listener action from the prior sample.
HUNG_SUPERVISOR_PROBE_TIMEOUT_S = 0.5

# The short-poll mirror keeps a small warm terminal window for continuity.
# Permanent immutable rows live in history-data.js instead: per project the
# union is terminals ended in the last two hours plus the newest five.
FAST_TERMINAL_RECENCY_SECONDS = 2 * 60 * 60
FAST_TERMINAL_MIN_PER_PROJECT = 5

CONTROLLER_LIVENESS_STATES = frozenset(
    {"ALIVE", "HUNG", "WAITING-ON-USER", "DEAD", "UNKNOWN"}
)

# A half-day is 12 hours × 60 minutes/hour × 60 seconds/minute = 43,200
# seconds. This deliberately removes the reported 15-100 hour non-terminal
# records while leaving shorter long-running work in the default view.
WORKER_AGE_FILTER_SECONDS = 12 * 60 * 60
# Two 60-second fleet refresh intervals = 120 seconds. A status-sidecar
# liveness sample older than that cannot make an old ledger row reappear.
WORKER_LIVE_SAMPLE_FRESH_SECONDS = 2 * 60
WORKER_AGE_FILTER_POLICY = {
    "threshold_seconds": WORKER_AGE_FILTER_SECONDS,
    "default_enabled": True,
    "unknown_started_at": "show",
    "order": "observed_live_then_newest_started",
}

# Fields rejected at the shareable-mirror boundary even when an upstream
# payload supplies them.  The allowlists below are the positive authority;
# this deny set documents the highest-risk omissions for audits and tests.
DENIED_FIELDS = frozenset(
    {
        "account",
        "account_identity",
        "argv",
        "codex_home",
        "controller_session_id",
        "effective_account",
        "error",
        "last_marker",
        "marker",
        "marker_body",
        "payload",
        "project_root",
        "prompt",
        "prompt_path",
        "raw_marker",
        "reason",
        "status_path",
        "stderr_path",
        "stdout_path",
        "tail",
        "tail_path",
        "text",
        "worker_identity",
    }
)

# ``None`` means a scalar leaf; ``[schema]`` means a homogeneous list.  These
# schemas double as executable allowlists: every completed projection is
# validated before it can be returned or published.
FLEET_FIELD_ALLOWLIST: dict[str, Any] = {
    "schema": None,
    "generation_id": None,
    "sample_started_at": None,
    "sample_finished_at": None,
    "last_success_at": None,
    "producer": {"name": None, "plane": None},
    "last_error": None,
    "incomplete": None,
    "cadence_seconds": None,
    "registry_total": None,
    "registry_deep_sampled": None,
    "registry_unsampled": None,
    "registry_unsampled_projects": [
        {
            "name": None,
            "project_id": None,
            "repo_identity": None,
            "last_seen": None,
        }
    ],
    "history_excluded": None,
    "worker_age_filter": {
        "threshold_seconds": None,
        "default_enabled": None,
        "unknown_started_at": None,
        "order": None,
    },
    "machine": {
        "queue_depth": None,
        "operating_cap": None,
        "active_leases": None,
        "local_workers": None,
        "rate_pressure": [
            {"provider": None, "scope": None, "count": None}
        ],
        "warnings": [{"code": None, "severity": None, "count": None}],
    },
    "vendors": [
        {
            "provider": None,
            "seat_index": None,
            "remaining": None,
            "reset_at": None,
            "flags": [None],
        }
    ],
    "remote": {
        "available": None,
        "history_excluded": None,
        "nodes": [{"node_id": None, "dispatches": None, "auth_states": [None]}],
        "workers": [
            {
                "dispatch_id": None,
                "node_id": None,
                "agent": None,
                "engine": None,
                "shape": None,
                "transport": None,
                "os_sandbox": None,
                "os_sandbox_requested": None,
                "os_sandbox_supported": None,
                "os_sandbox_enforced": None,
                "state": None,
                "classification": None,
                "terminal_state": None,
                "liveness_state": None,
                "worker_alive": None,
                "started_at": None,
                "ended_at": None,
                "display_state": None,
                "is_terminal": None,
                "classification_conflict": None,
                "authority_detail": None,
                "authority_resolution": None,
                "controller_session_digest": None,
                "controller_pid": None,
                "controller_label": None,
                "controller_display": None,
                "controller_state": None,
                "controller_liveness_state": None,
                "age_filter_match": None,
                "age_filter_reason": None,
                "observed_live": None,
                "observed_live_source": None,
                "task_ids": [None],
                "prompt_file": None,
                "quarantine_reason": None,
                "ssh_reachable": None,
                "may_release": None,
            }
        ],
    },
    "projects": [
        {
            "project_id": None,
            "name": None,
            "registered": None,
            "last_seen": None,
            "skill_version": None,
            "history_excluded": None,
            "parent_project_id": None,
            "parent_name": None,
            "worktree_name": None,
            "repo_identity": None,
            "queue": {
                "depth": None,
                "lanes": [{"agent": None, "count": None}],
                "oldest_created_at": None,
            },
            "session": {
                "available": None,
                "active": None,
                "queue_state": None,
                "queue_last_touched": None,
                "active_leases": None,
            },
            "milestone": {
                "available": None,
                "active_cadence": None,
                "commits_since": None,
                "cadence": None,
                "due": None,
            },
            "workers": [
                {
                    "dispatch_id": None,
                    "node_id": None,
                    "agent": None,
                    "engine": None,
                    "shape": None,
                    "transport": None,
                    "os_sandbox": None,
                    "os_sandbox_requested": None,
                    "os_sandbox_supported": None,
                    "os_sandbox_enforced": None,
                    "state": None,
                    "classification": None,
                    "terminal_state": None,
                    "liveness_state": None,
                    "worker_alive": None,
                    "started_at": None,
                    "ended_at": None,
                    "display_state": None,
                    "is_terminal": None,
                    "classification_conflict": None,
                    "authority_detail": None,
                    "authority_resolution": None,
                    "controller_session_digest": None,
                    "controller_pid": None,
                    "controller_label": None,
                    "controller_display": None,
                    "controller_state": None,
                    "controller_liveness_state": None,
                    "age_filter_match": None,
                    "age_filter_reason": None,
                    "observed_live": None,
                    "observed_live_source": None,
                    "task_ids": [None],
                    "prompt_file": None,
                }
            ],
        }
    ],
    "controllers": [
        {
            "controller_key": None,
            "label": None,
            "project_id": None,
            "project_name": None,
            "parent_project_id": None,
            "parent_name": None,
            "controller_liveness_state": None,
            "listener_live": None,
            "listener_target": None,
            "wake_mode": None,
            "in_flight_count": None,
            "owned_live": None,
            "last_seen": None,
            "generation": None,
            "retire_command": None,
            "last_error": None,
            "probe_command": None,
        }
    ],
    "unassigned_workers": [
        {
            "dispatch_id": None,
            "node_id": None,
            "agent": None,
            "engine": None,
            "shape": None,
            "transport": None,
            "os_sandbox": None,
            "os_sandbox_requested": None,
            "os_sandbox_supported": None,
            "os_sandbox_enforced": None,
            "state": None,
            "classification": None,
            "terminal_state": None,
            "liveness_state": None,
            "worker_alive": None,
            "started_at": None,
            "ended_at": None,
            "display_state": None,
            "is_terminal": None,
            "classification_conflict": None,
            "authority_detail": None,
            "authority_resolution": None,
            "controller_session_digest": None,
            "controller_pid": None,
            "controller_label": None,
            "controller_display": None,
            "controller_state": None,
            "controller_liveness_state": None,
            "age_filter_match": None,
            "age_filter_reason": None,
            "observed_live": None,
            "observed_live_source": None,
            "task_ids": [None],
            "prompt_file": None,
        }
    ],
}

ATTENTION_FIELD_ALLOWLIST: dict[str, Any] = {
    "schema": None,
    "generation_id": None,
    "sample_started_at": None,
    "sample_finished_at": None,
    "last_success_at": None,
    "producer": {"name": None, "plane": None},
    "last_error": None,
    "incomplete": None,
    "cadence_seconds": None,
    "controller_history_probes_truncated": None,
    "age_granularity": None,
    "items": [
        {
            "dispatch_id": None,
            "seq": None,
            "kind": None,
            "action": None,
            "observed_at": None,
            "headline": None,
        }
    ],
}

FIELD_ALLOWLISTS = {
    "fleet": FLEET_FIELD_ALLOWLIST,
    "attention": ATTENTION_FIELD_ALLOWLIST,
}

_ABSOLUTE_PATH = re.compile(
    r"(^|[\s(\[{'\"])/(?!/)[^\s<>\"']+|(^|[\s(\[{'\"])[A-Za-z]:\\[^\s<>\"']+"
)
_ATTENTION_KINDS = frozenset({"user_need", "user_confirm", "blocked", "advisory"})
_TIMESTAMP_FIELDS = frozenset(
    {
        "sample_started_at",
        "sample_finished_at",
        "last_success_at",
        "last_seen",
        "oldest_created_at",
        "queue_last_touched",
        "started_at",
        "ended_at",
        "observed_at",
    }
)


class ProjectionSecurityError(ValueError):
    """A projection tried to cross the mirror boundary with an unsafe shape."""


def _validate_allowlist(value: Any, schema: Any, *, path: str) -> None:
    if schema is None:
        if isinstance(value, (dict, list, tuple)):
            raise ProjectionSecurityError(f"{path}: expected scalar")
        return
    if isinstance(schema, dict):
        if not isinstance(value, dict):
            raise ProjectionSecurityError(f"{path}: expected object")
        unexpected = set(value) - set(schema)
        if unexpected:
            names = ", ".join(sorted(str(item) for item in unexpected))
            raise ProjectionSecurityError(f"{path}: fields not allowlisted: {names}")
        missing = set(schema) - set(value)
        if missing:
            names = ", ".join(sorted(str(item) for item in missing))
            raise ProjectionSecurityError(f"{path}: required fields absent: {names}")
        for key, child_schema in schema.items():
            _validate_allowlist(value[key], child_schema, path=f"{path}.{key}")
        return
    if isinstance(schema, list) and len(schema) == 1:
        if not isinstance(value, list):
            raise ProjectionSecurityError(f"{path}: expected list")
        for index, item in enumerate(value):
            _validate_allowlist(item, schema[0], path=f"{path}[{index}]")
        return
    raise ProjectionSecurityError(f"{path}: invalid allowlist schema")


def _is_listener_start_action(value: str) -> bool:
    try:
        argv = shlex.split(value)
    except ValueError:
        return False
    # The generated command carries --report-pending (the arm doubles as the
    # peek); it is a bare flag, so drop it before the positional shape check
    # rather than duplicating every accepted length.
    if argv and argv[-1] == "--report-pending":
        argv = argv[:-1]
    if len(argv) not in {5, 7}:
        return False
    advertised = goalflight_compat.advertised_script(
        "goalflight_messages.py",
        running_file=goalflight_wake.__file__,
    )
    if not (
        argv[0] == "python3"
        and Path(argv[1]).is_absolute()
        and Path(os.path.abspath(argv[1])) == advertised
        and argv[2:4] == ["listen", "--project-root"]
        and Path(argv[4]).is_absolute()
    ):
        return False
    if len(argv) == 7 and argv[5] != "--controller-label":
        return False
    label = argv[6] if len(argv) == 7 else None
    return value == goalflight_wake.listener_start_command(
        argv[4],
        controller_label=label,
    )


def _validate_no_absolute_paths(value: Any, *, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            _validate_no_absolute_paths(item, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _validate_no_absolute_paths(item, path=f"{path}[{index}]")
    elif (
        isinstance(value, str)
        and path.endswith(".action")
        and _is_listener_start_action(value)
    ):
        # HUNG recovery is intentionally an exact wake-layer command. This is
        # the one narrow shareable-boundary exception to path redaction.
        return
    elif isinstance(value, str) and _ABSOLUTE_PATH.search(value):
        raise ProjectionSecurityError(f"{path}: absolute path denied")


def _validate_scalar_types(value: Any, *, path: str = "$") -> None:
    """Reject scalar coercions that could turn malformed metadata into facts."""
    if isinstance(value, dict):
        for key, item in value.items():
            item_path = f"{path}.{key}"
            if key in _TIMESTAMP_FIELDS and item is not None:
                if not isinstance(item, str) or not item.strip():
                    raise ProjectionSecurityError(f"{item_path}: expected timestamp string or null")
            if key == "generation_id":
                if not isinstance(item, str) or not item:
                    raise ProjectionSecurityError(f"{item_path}: expected non-empty string")
            if key == "cadence_seconds":
                if isinstance(item, bool) or not isinstance(item, int) or item <= 0:
                    raise ProjectionSecurityError(
                        f"{item_path}: expected a positive integer"
                    )
            if key == "controller_liveness_state":
                if item not in CONTROLLER_LIVENESS_STATES:
                    raise ProjectionSecurityError(
                        f"{item_path}: expected a registered controller liveness state"
                    )
            if key == "incomplete" and item is not None:
                if not isinstance(item, bool):
                    raise ProjectionSecurityError(f"{item_path}: expected a boolean")
            if key == "controller_history_probes_truncated" and item is not None:
                if isinstance(item, bool) or not isinstance(item, int) or item < 0:
                    raise ProjectionSecurityError(
                        f"{item_path}: expected a non-negative integer or null"
                    )
            if key == "registry_unsampled" and item is not None:
                if isinstance(item, bool) or not isinstance(item, int) or item < 0:
                    raise ProjectionSecurityError(
                        f"{item_path}: expected a non-negative integer or null"
                    )
            if key == "history_excluded" and item is not None:
                if isinstance(item, bool) or not isinstance(item, int) or item < 0:
                    raise ProjectionSecurityError(
                        f"{item_path}: expected a non-negative integer or null"
                    )
            if key == "prompt_file" and item is not None:
                if not isinstance(item, str) or re.fullmatch(r"[0-9a-f]{32}\.txt", item) is None:
                    raise ProjectionSecurityError(
                        f"{item_path}: expected a hashed prompt filename or null"
                    )
            _validate_scalar_types(item, path=item_path)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _validate_scalar_types(item, path=f"{path}[{index}]")


def validate_projection(payload: dict[str, Any], plane: str) -> None:
    """Validate the explicit field allowlist and path-denial policy."""
    schema = FIELD_ALLOWLISTS.get(plane)
    if schema is None:
        raise ProjectionSecurityError(f"unknown plane: {plane}")
    _validate_allowlist(payload, schema, path="$" )
    _validate_scalar_types(payload)
    _validate_no_absolute_paths(payload)


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _parse_timestamp(value: object) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _iso_timestamp(value: object) -> str | None:
    parsed = _parse_timestamp(value)
    return parsed.isoformat(timespec="seconds") if parsed is not None else None


def _number(value: object) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return int(number) if number.is_integer() else number


def _display(value: object, *, limit: int = 96) -> str | None:
    if value is None:
        return None
    text = goalflight_messages.sanitize_display(value, limit=limit)
    text = _ABSOLUTE_PATH.sub(lambda match: f"{match.group(1) or match.group(2) or ''}[path]", text)
    return text or None


def _repo_identity_scalar(value: object) -> str | None:
    """Cached registry identity only. Missing or blank is unknown, never guessed."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    return _display(text, limit=160)


def _repo_display(identity: object) -> str | None:
    """Operator label for a cached identity: owner/name, never a filesystem path."""
    text = _repo_identity_scalar(identity)
    if text is None:
        return None
    if text.startswith("file"):
        tail = text.rsplit("/", 1)[-1]
        return _display(tail or "local", limit=64)
    parts = text.split("/")
    shown = "/".join(parts[1:]) if len(parts) >= 3 else text
    return _display(shown, limit=64)


def _generic_basename_label(basename: str, project_id: str) -> str:
    """Disambiguate leftover generic basenames with the short project_id digest."""
    if basename.casefold() not in GENERIC_PROJECT_BASENAMES:
        return basename
    suffix = project_id.rsplit("-", 1)[-1]
    if suffix and suffix != basename:
        return _display(f"{basename} · {suffix}", limit=64) or project_id
    return project_id


def _safe_error(source: str, exc: BaseException) -> str:
    return f"{source}:{type(exc).__name__}"


def _generation_id(plane: str, supplied: str | None) -> str:
    value = supplied or f"{plane}-{uuid.uuid4()}"
    return _display(value, limit=128) or f"{plane}-unknown"


def _current_cadence_seconds(plane: str, supplied: int | None) -> int:
    cadence = PLANE_CADENCE_SECONDS[plane] if supplied is None else supplied
    if isinstance(cadence, bool) or not isinstance(cadence, int) or cadence <= 0:
        raise ValueError("cadence must be a positive integer number of seconds")
    return cadence


def _metadata(
    plane: str,
    *,
    generation_id: str,
    started_at: str,
    finished_at: str,
    errors: list[str],
    cadence_seconds: int | None = None,
) -> dict[str, Any]:
    error = (
        f"{errors[-1]} · {_operator_action(plane)}"
        if errors
        else None
    )
    return {
        "generation_id": generation_id,
        "sample_started_at": started_at,
        "sample_finished_at": finished_at,
        "last_success_at": finished_at if not errors else None,
        "producer": {"name": PRODUCER_NAME, "plane": plane},
        "last_error": error,
        "incomplete": bool(errors),
        "cadence_seconds": _current_cadence_seconds(plane, cadence_seconds),
    }


def _operator_action(plane: str) -> str:
    return (
        f"action: read ~/.goal-flight/fleet-console-{plane}-launchd.log; "
        f"run scripts/install-fleet-console.sh --status --plane {plane}"
    )


def _capture(
    source: str,
    errors: list[str],
    producer: Callable[[], Any],
    fallback: Any,
) -> Any:
    try:
        return producer()
    except Exception as exc:  # source failures are data, not a false healthy sample
        errors.append(_safe_error(source, exc))
        return fallback


def _project_id(project_root: str) -> str:
    name = Path(project_root).name or "project"
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-") or "project"
    digest = hashlib.sha256(project_root.encode("utf-8")).hexdigest()[:10]
    return f"{safe_name}-{digest}"


def _canonical_root(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return str(Path(value).expanduser().resolve(strict=False))


def _record_time(record: dict[str, Any]) -> dt.datetime | None:
    for key in ("ended_at", "updated_at", "started_at"):
        parsed = _parse_timestamp(record.get(key))
        if parsed is not None:
            return parsed
    return None


def _fast_plane_records(
    records: list[dict[str, Any]],
    *,
    sampled_at: dt.datetime | None,
    journal_authority: dict[str, dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Return the live rows plus the bounded warm terminal union.

    Unresolved contradictions are retained as live/changing state. A row is
    terminal only when the producer verdict says so, preventing an old stale
    classification from hiding a record whose authorities still disagree.
    """
    live: list[dict[str, Any]] = []
    terminal: list[dict[str, Any]] = []
    for record in records:
        journal = (journal_authority or {}).get(str(record.get("dispatch_id") or ""))
        lifecycle = str((journal or {}).get("lifecycle_state") or "")
        if _record_is_terminal(record):
            # A committed terminal ledger row is immutable fast-path history.
            # Never reopen a status sidecar/journal contradiction to promote it
            # back into every short-poll sample.
            verdict = {"is_terminal": True}
        elif lifecycle in goalflight_journal.ATTEMPT_FINAL_STATES:
            verdict = {"is_terminal": True}
        elif lifecycle in goalflight_journal.ATTEMPT_LIVE_STATES:
            verdict = {"is_terminal": False}
        else:
            verdict = _worker_display_verdict(record)
        (terminal if verdict["is_terminal"] is True else live).append(record)
    terminal.sort(
        key=lambda record: (
            _record_time(record) is not None,
            _record_time(record) or dt.datetime.min.replace(tzinfo=dt.timezone.utc),
            str(record.get("dispatch_id") or ""),
        ),
        reverse=True,
    )
    newest_ids = {
        id(record) for record in terminal[:FAST_TERMINAL_MIN_PER_PROJECT]
    }
    recent_ids: set[int] = set()
    if sampled_at is not None:
        cutoff = sampled_at - dt.timedelta(seconds=FAST_TERMINAL_RECENCY_SECONDS)
        recent_ids = {
            id(record)
            for record in terminal
            if (observed := _record_time(record)) is not None and observed >= cutoff
        }
    kept_terminal = [
        record for record in terminal if id(record) in newest_ids or id(record) in recent_ids
    ]
    kept_ids = {id(record) for record in live + kept_terminal}
    kept = [record for record in records if id(record) in kept_ids]
    return kept, len(records) - len(kept)


def _record_is_reconciled_detached_live(record: object) -> bool:
    """True when the ledger rechecked a detached orphan's exact identity.

    ``orphaned``/``controller_dead`` describe how the controller exited, not
    whether its detached worker died.  The ledger emits ``expected_live`` only
    after the worker PID and start identity still match, so that measured
    verdict outranks the stale controller-exit state on fast-plane retention.
    """
    if not isinstance(record, dict) or record.get("detached") is not True:
        return False
    state = record.get("state")
    reason = record.get("reason") or record.get("error")
    controller_exit = state == "controller_dead" or (
        state == "orphaned" and reason == "controller_dead"
    )
    return controller_exit and record.get("classification") == "expected_live"


def _state_evidence(value: object) -> str | None:
    normalized = goalflight_dispatch_states.normalize_dispatch_state(value)
    if not normalized or normalized == "unknown":
        return None
    if goalflight_dispatch_states.is_terminal_state(value):
        return "terminal:" + normalized
    if (
        goalflight_dispatch_states.is_running_state(value)
        or goalflight_dispatch_states.is_attention_state(value)
        or normalized in {"queued", "waiting", "starting"}
    ):
        return "live:" + normalized
    return None


def _authority_snapshot(
    record: dict[str, Any],
    *,
    status: dict[str, Any] | None,
    journal: dict[str, Any] | None,
) -> tuple[dict[str, Any], str | None, str | None]:
    """Resolve a display verdict and name every disagreeing source field."""
    base = _worker_display_verdict(record)
    evidence: list[tuple[str, object, str]] = []
    reconciled_detached_live = _record_is_reconciled_detached_live(record)
    for field in ("state", "terminal_state", "classification"):
        value = record.get(field)
        if reconciled_detached_live and field == "state":
            # The state records controller exit; expected_live records the
            # newer exact worker-identity check.  Treating both as concurrent
            # worker verdicts would invent an authority conflict.
            continue
        normalized = _state_evidence(value)
        if normalized is not None:
            evidence.append((f"ledger.{field}", value, normalized))
    worker_alive = record.get("worker_still_alive")
    if isinstance(worker_alive, bool):
        terminal_kind = next(
            (kind for _name, _value, kind in evidence if kind.startswith("terminal:")),
            "terminal:worker_dead",
        )
        evidence.append(
            (
                "ledger.worker_still_alive",
                worker_alive,
                "live:running" if worker_alive else terminal_kind,
            )
        )

    status_field = None
    status_value = None
    status_matches = (
        isinstance(status, dict)
        and bool(record.get("dispatch_id"))
        and status.get("dispatch_id") == record.get("dispatch_id")
    )
    if status_matches:
        assert isinstance(status, dict)
        for field in ("terminal_pending_state", "terminal_state", "state"):
            if _state_evidence(status.get(field)) is not None:
                status_field, status_value = field, status.get(field)
                evidence.append(
                    (f"status.json.{field}", status_value, _state_evidence(status_value) or "")
                )
                break

    journal_value = None
    journal_field = None
    lifecycle = str((journal or {}).get("lifecycle_state") or "")
    if lifecycle in goalflight_journal.ATTEMPT_FINAL_STATES:
        journal_field = "terminal_state"
        journal_value = (journal or {}).get("terminal_state") or "terminal"
    elif lifecycle in goalflight_journal.ATTEMPT_LIVE_STATES:
        journal_field = "lifecycle_state"
        journal_value = {
            goalflight_journal.ATTEMPT_PREPARED: "queued",
            goalflight_journal.ATTEMPT_STARTING: "starting",
            goalflight_journal.ATTEMPT_RUNNING: "running",
        }.get(lifecycle, "running")
    if journal_field is not None:
        evidence.append(
            (
                f"journal.{journal_field}",
                journal_value,
                _state_evidence(journal_value) or "",
            )
        )

    distinct = {item[2] for item in evidence if item[2]}
    detail = None
    resolution = None
    if len(distinct) > 1:
        detail = "; ".join(f"{name}={value}" for name, value, _kind in evidence)

    if journal_field is not None:
        resolution = "journal"
        if goalflight_dispatch_states.is_terminal_state(journal_value):
            verdict = {
                "display_state": (
                    goalflight_dispatch_states.normalize_dispatch_state(journal_value)
                    or "terminal"
                ),
                "is_terminal": True,
                "classification_conflict": False,
            }
        else:
            verdict = {
                "display_state": (
                    goalflight_dispatch_states.normalize_dispatch_state(journal_value)
                    or "running"
                ),
                "is_terminal": False,
                "classification_conflict": False,
            }
        if detail:
            detail += "; reconciled by journal authority"
        return verdict, detail, resolution

    # A status sidecar is structurally newer only when both sources carry an
    # observation time and the sidecar's is later. Otherwise disagreement is
    # honestly unresolved rather than guessed away.
    if status_field is not None:
        assert isinstance(status, dict)
        status_time = _parse_timestamp((status or {}).get("heartbeat_at"))
        if status_time is None:
            numeric_updated = _number((status or {}).get("updated_at"))
            if numeric_updated is not None:
                with contextlib.suppress(OverflowError, OSError, ValueError):
                    status_time = dt.datetime.fromtimestamp(
                        numeric_updated, tz=dt.timezone.utc
                    )
        ledger_time = _parse_timestamp(record.get("updated_at"))
        if status_time is not None and ledger_time is not None and status_time > ledger_time:
            resolution = "status.json:newer"
            is_terminal = goalflight_dispatch_states.is_terminal_state(status_value)
            verdict = {
                "display_state": (
                    goalflight_dispatch_states.normalize_dispatch_state(status_value)
                    or ("terminal" if is_terminal else "running")
                ),
                "is_terminal": bool(is_terminal),
                "classification_conflict": False,
            }
            if detail:
                detail += "; reconciled by newer status.json observation"
            return verdict, detail, resolution

    if len(distinct) > 1:
        polarities = {item.split(":", 1)[0] for item in distinct}
        base = {
            "display_state": "unknown",
            "is_terminal": True if polarities == {"terminal"} else None,
            "classification_conflict": True,
        }
    return base, detail, resolution


def _journal_authority_by_dispatch(
    project_root: Path,
    records: list[dict[str, Any]],
    *,
    authority: goalflight_journal.Journal | None = None,
    open_if_missing: bool = True,
) -> dict[str, dict[str, Any]]:
    dispatch_ids = sorted(
        {
            str(record.get("dispatch_id"))
            for record in records
            if record.get("dispatch_id")
        }
    )
    if not dispatch_ids:
        return {}
    try:
        if authority is None and open_if_missing:
            authority = goalflight_journal.Journal.open_reader(project_root)
        if authority is None:
            return {}
        rows = []
        # Keep bind counts comfortably below conservative SQLite builds while
        # retaining one reader generation for the whole project.
        for offset in range(0, len(dispatch_ids), 400):
            chunk = dispatch_ids[offset : offset + 400]
            placeholders = ",".join("?" for _ in chunk)
            rows.extend(
                authority.read_all(
                    "SELECT dispatch_id, lifecycle_state, terminal_state, "
                    "state_updated_at, owner_controller_label "
                    "FROM dispatch_attempts WHERE dispatch_id IN ("
                    + placeholders
                    + ")",
                    tuple(chunk),
                )
            )
    except (
        goalflight_journal.JournalBusy,
        goalflight_journal.JournalDisappeared,
        goalflight_journal.JournalIOError,
    ):
        return {}
    except (goalflight_journal.JournalError, OSError, ValueError):
        return {}
    return {
        str(item["dispatch_id"]): item
        for row in rows
        if (item := dict(row)).get("dispatch_id")
    }


def _project_journal_reader(
    project_root: Path,
) -> goalflight_journal.Journal | None:
    """Open at most one read-only journal handle for a fast-plane project."""
    try:
        return goalflight_journal.Journal.open_reader(project_root)
    except (
        goalflight_journal.JournalBusy,
        goalflight_journal.JournalDisappeared,
        goalflight_journal.JournalIOError,
    ):
        return None
    except (goalflight_journal.JournalError, OSError, ValueError):
        return None


def _worker_display_verdict(record: dict[str, Any]) -> dict[str, Any]:
    """Resolve one presentation verdict from the reconciled authority fields."""
    if _record_is_reconciled_detached_live(record):
        return {
            "display_state": "running",
            "is_terminal": False,
            "classification_conflict": False,
        }
    limit_kind = goalflight_dispatch_states.limit_kind_for_record(record)
    state_values = [record.get(key) for key in ("state", "terminal_state", "classification")]
    terminal_values = [
        value for value in state_values if goalflight_dispatch_states.is_terminal_state(value)
    ]
    terminal_states = {
        goalflight_dispatch_states.normalize_dispatch_state(value) for value in terminal_values
    }
    attention = any(goalflight_dispatch_states.is_attention_state(value) for value in state_values)
    running_values = [
        value for value in state_values if goalflight_dispatch_states.is_running_state(value)
    ]
    classification = record.get("classification")
    liveness_state = record.get("liveness_state")
    alive = record.get("worker_still_alive")
    live_evidence = bool(
        attention
        or running_values
        or classification == "expected_live"
        or goalflight_dispatch_states.is_running_state(liveness_state)
        or alive is True
    )
    conflict = bool(
        ((terminal_values or alive is False) and live_evidence)
        or len(terminal_states) > 1
    )

    if conflict and len(terminal_states) > 1 and not live_evidence:
        return {
            "display_state": "unknown",
            "is_terminal": True,
            "classification_conflict": True,
        }
    if conflict:
        return {
            "display_state": "unknown",
            "is_terminal": None,
            "classification_conflict": True,
        }
    if terminal_values:
        normalized = (
            goalflight_dispatch_states.limit_state_for_kind(limit_kind)
            if limit_kind
            else goalflight_dispatch_states.normalize_dispatch_state(terminal_values[0])
        )
        return {
            "display_state": normalized or "terminal",
            "is_terminal": True,
            "classification_conflict": False,
        }
    if attention:
        return {
            "display_state": "attention",
            "is_terminal": False,
            "classification_conflict": False,
        }
    if live_evidence:
        normalized_liveness = goalflight_dispatch_states.normalize_dispatch_state(liveness_state)
        if normalized_liveness == "running_quiet":
            display_state = "running_quiet"
        elif running_values:
            display_state = goalflight_dispatch_states.normalize_dispatch_state(running_values[0]) or "running"
        else:
            display_state = "running"
        return {
            "display_state": display_state,
            "is_terminal": False,
            "classification_conflict": False,
        }
    return {
        "display_state": "unknown",
        "is_terminal": None,
        "classification_conflict": False,
    }


def classify_controller(
    holder_lock: bool | None,
    live_waiter_count: int | None,
    in_flight_count: int,
) -> str:
    """Classify one controller from kernel/journal facts, without host I/O."""
    if live_waiter_count is not None and (
        isinstance(live_waiter_count, bool)
        or not isinstance(live_waiter_count, int)
        or live_waiter_count < 0
    ):
        raise ValueError("live_waiter_count must be a non-negative integer or None")
    for name, count in (("in_flight_count", in_flight_count),):
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError(f"{name} must be a non-negative integer")
    if live_waiter_count is None:
        return "UNKNOWN"
    if holder_lock is None:
        return "UNKNOWN"
    if holder_lock is False:
        return "DEAD"
    if live_waiter_count > 0:
        return "ALIVE"
    if in_flight_count > 0:
        return "HUNG"
    return "WAITING-ON-USER"


_MAIN_CHECKOUT_CACHE: dict[str, str] = {}
_LIVENESS_RANK = {
    "ALIVE": 0,
    "HUNG": 1,
    "WAITING-ON-USER": 2,
    "UNKNOWN": 3,
    "DEAD": 4,
}


def _git_main_checkout(project_root: str) -> str:
    """Return the main checkout for a worktree without shelling out.

    A linked worktree stores ``gitdir:`` in ``.git``; ``commondir`` then
    names the shared ``.git`` directory of the parent checkout. Results are
    cached per root so the fast plane pays one small-file read, not one
    ``git`` per row.
    """
    cached = _MAIN_CHECKOUT_CACHE.get(project_root)
    if cached is not None:
        return cached
    root = Path(project_root)
    main = project_root
    git = root / ".git"
    try:
        if git.is_file():
            first = git.read_text(encoding="utf-8").splitlines()[0].strip()
            if first.lower().startswith("gitdir:"):
                gitdir = Path(first.split(":", 1)[1].strip())
                if not gitdir.is_absolute():
                    gitdir = root / gitdir
                gitdir = gitdir.resolve(strict=False)
                commondir = gitdir / "commondir"
                if commondir.is_file():
                    rel = commondir.read_text(encoding="utf-8").strip()
                    common_git = (gitdir / rel).resolve(strict=False)
                    if common_git.name == ".git":
                        main = str(common_git.parent)
                elif (
                    gitdir.parent.name == "worktrees"
                    and gitdir.parent.parent.name == ".git"
                ):
                    main = str(gitdir.parent.parent.parent)
        elif git.is_dir():
            main = project_root
    except (OSError, UnicodeError, IndexError, ValueError):
        main = project_root
    _MAIN_CHECKOUT_CACHE[project_root] = main
    return main


def _parent_fields(project_root: str) -> dict[str, Any]:
    main = _git_main_checkout(project_root)
    is_worktree = os.path.abspath(main) != os.path.abspath(project_root)
    return {
        "parent_project_id": _project_id(main),
        "parent_name": _display(Path(main).name or "project", limit=64),
        "worktree_name": (
            _display(Path(project_root).name or "worktree", limit=64)
            if is_worktree
            else None
        ),
    }


def _controller_retire_command(label: str) -> str:
    """Exact session-status retire invocation; relative so the plane stays path-free."""
    return "python3 scripts/goalflight_session_status.py --retire " + shlex.quote(label)


def _controller_probe_command(label: str, generation: int | None = None) -> str:
    """Read-only lock probe; relative so the plane stays path-free."""
    command = (
        "python3 scripts/goalflight_fleet_console.py probe-holder --label "
        + shlex.quote(label)
    )
    if isinstance(generation, int) and not isinstance(generation, bool) and generation >= 0:
        command += f" --generation {generation}"
    return command


def probe_holder_lock(
    project_root: Path | str,
    *,
    controller_label: str,
    generation: int | None = None,
) -> bool | None:
    """Return lease_holder_alive for one labelled generation.

    Read-only: never expire, claim, or prune. Missing journal, missing lock,
    or an ambiguous generation is None — the same UNKNOWN the console shows.
    """
    root = Path(project_root)
    try:
        authority = goalflight_journal.Journal.open_reader(root)
        rows = authority.lease_records(include_ended=generation is not None)
    except (
        goalflight_journal.JournalBusy,
        goalflight_journal.JournalDisappeared,
        goalflight_journal.JournalIOError,
    ):
        return None
    except (goalflight_journal.JournalError, OSError, ValueError):
        return None
    matches: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("label") != controller_label:
            continue
        nonce = row.get("nonce")
        if not isinstance(nonce, str) or not nonce:
            continue
        if generation is None:
            if row.get("state") != goalflight_journal.LEASE_ACTIVE:
                continue
        elif row.get("generation") != generation:
            continue
        matches.append(row)
    if len(matches) != 1:
        return None
    try:
        return goalflight_wake.lease_holder_alive(
            root,
            controller_label=controller_label,
            lease_nonce=str(matches[0]["nonce"]),
        )
    except (OSError, RuntimeError, ValueError):
        return None


def _controller_unknown_error(
    *,
    holder_lock: bool | None,
    live_waiter_count: int | None,
    lock_reason: str | None,
    probe_error: str | None,
) -> str | None:
    """Why classify_controller returned UNKNOWN; None when the state is known."""
    if live_waiter_count is not None and holder_lock is not None:
        return None
    parts: list[str] = []
    if probe_error:
        parts.append(f"lease probe failed:{probe_error}")
    elif holder_lock is None and lock_reason:
        parts.append(lock_reason)
    if live_waiter_count is None:
        parts.append("waiter probe unavailable")
    return " · ".join(parts) or None


def _journals_dir() -> Path | None:
    override = os.environ.get("GOALFLIGHT_JOURNAL_DIR", "").strip()
    if override:
        return Path(override).expanduser() / "journals"
    # Isolated console tests must never walk the operator's default state dir.
    if os.environ.get("GOALFLIGHT_TEST_MODE") == "1":
        return None
    return goalflight_task.resolve_state_base_dir() / "journals"


def _active_controller_roots_from_journals() -> set[str]:
    """Project roots that still have an ACTIVE lease row.

    One directory listing plus a DISTINCT on the lease table — not a
    per-controller history walk and not a pass over the 1,400-root registry.
    """
    roots: set[str] = set()
    base = _journals_dir()
    if base is None:
        return roots
    try:
        paths = list(base.glob(f"*/{goalflight_journal.JOURNAL_FILE_NAME}"))
    except OSError:
        return roots
    for path in paths:
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        except sqlite3.Error:
            continue
        try:
            rows = conn.execute(
                "SELECT DISTINCT project_root FROM controller_leases WHERE state = 'ACTIVE'"
            )
            for row in rows:
                supplied = row[0] if row else None
                if isinstance(supplied, str) and supplied:
                    roots.add(supplied)
        except sqlite3.Error:
            continue
        finally:
            conn.close()
    return roots


def _empty_controller_context() -> dict[str, object | None]:
    return {
        "label": None,
        "generation": None,
        "liveness_state": "UNKNOWN",
        "listener_live": None,
        "listener_target": None,
        "wake_mode": None,
        "in_flight_count": 0,
        "last_seen": None,
        "last_error": "journal unreadable",
    }


def _controller_panel_row(
    project_root: str,
    context: dict[str, object | None],
) -> dict[str, Any]:
    raw_label = context.get("label")
    label = raw_label if isinstance(raw_label, str) and raw_label else None
    state = str(context.get("liveness_state") or "UNKNOWN")
    if state not in CONTROLLER_LIVENESS_STATES:
        state = "UNKNOWN"
    generation = context.get("generation")
    in_flight = context.get("in_flight_count")
    listener_live = context.get("listener_live")
    listener_target = context.get("listener_target")
    wake_mode = context.get("wake_mode")
    retire = (
        _controller_retire_command(label)
        if state == "DEAD" and label
        else None
    )
    probe = (
        _controller_probe_command(
            label,
            generation if isinstance(generation, int) and not isinstance(generation, bool) else None,
        )
        if state == "UNKNOWN" and label
        else None
    )
    last_error = (
        _display(context.get("last_error"), limit=160)
        if state == "UNKNOWN"
        else None
    )
    parent = _parent_fields(project_root)
    return {
        "controller_key": _display(
            f"{parent['parent_project_id']}:{label or 'unknown'}",
            limit=128,
        ),
        "label": _display(label, limit=64),
        "project_id": _project_id(project_root),
        "project_name": _display(Path(project_root).name or "project", limit=64),
        "parent_project_id": parent["parent_project_id"],
        "parent_name": parent["parent_name"],
        "controller_liveness_state": state,
        "listener_live": listener_live if isinstance(listener_live, int) and not isinstance(listener_live, bool) else None,
        "listener_target": listener_target if isinstance(listener_target, int) and not isinstance(listener_target, bool) else None,
        "wake_mode": wake_mode if wake_mode in {"persistent", "portable"} else None,
        "in_flight_count": in_flight if isinstance(in_flight, int) and not isinstance(in_flight, bool) and in_flight >= 0 else 0,
        "owned_live": 0,
        "last_seen": _iso_timestamp(context.get("last_seen")),
        "generation": generation if isinstance(generation, int) and not isinstance(generation, bool) else None,
        "retire_command": _display(retire, limit=160) if retire else None,
        "last_error": last_error,
        "probe_command": _display(probe, limit=160) if probe else None,
    }


def _controller_sort_key(row: dict[str, Any]) -> tuple[Any, ...]:
    state = str(row.get("controller_liveness_state") or "UNKNOWN")
    rank = _LIVENESS_RANK.get(state, 3)
    return (
        1 if state == "DEAD" else 0,
        rank,
        str(row.get("label") or ""),
        str(row.get("parent_name") or row.get("project_name") or ""),
        str(row.get("controller_key") or ""),
    )


def _owned_live_counts(
    projects: list[dict[str, Any]],
    unassigned: list[dict[str, Any]],
    remote_workers: list[dict[str, Any]],
) -> dict[str, int]:
    """Count live workers per owner label wherever those workers run."""
    counts: dict[str, int] = {}

    def add(workers: list[dict[str, Any]]) -> None:
        for worker in workers:
            label = worker.get("controller_label")
            if not isinstance(label, str) or not label:
                continue
            if worker.get("is_terminal") is True:
                continue
            counts[label] = counts.get(label, 0) + 1

    for project in projects:
        add(project.get("workers") or [])
    add(unassigned)
    add(remote_workers)
    return counts


def _aggregate_controller_rows(
    rows: list[dict[str, Any]],
    owned: dict[str, int],
) -> list[dict[str, Any]]:
    """Collapse leftover per-journal rows to one owner-label row across repos."""
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        label = row.get("label")
        if not isinstance(label, str) or not label:
            continue
        current = grouped.get(label)
        if current is None:
            grouped[label] = dict(row)
            continue
        current_rank = _LIVENESS_RANK.get(
            str(current.get("controller_liveness_state") or "UNKNOWN"), 3
        )
        new_rank = _LIVENESS_RANK.get(
            str(row.get("controller_liveness_state") or "UNKNOWN"), 3
        )
        if new_rank < current_rank:
            current["controller_liveness_state"] = row.get("controller_liveness_state")
            current["listener_live"] = row.get("listener_live")
            current["listener_target"] = row.get("listener_target")
            current["wake_mode"] = row.get("wake_mode")
            current["generation"] = row.get("generation")
            current["retire_command"] = row.get("retire_command")
            current["last_error"] = row.get("last_error")
            current["probe_command"] = row.get("probe_command")
        current_flight = current.get("in_flight_count")
        new_flight = row.get("in_flight_count")
        if isinstance(current_flight, int) and isinstance(new_flight, int):
            current["in_flight_count"] = max(current_flight, new_flight)
        elif isinstance(new_flight, int):
            current["in_flight_count"] = new_flight
        current_seen = current.get("last_seen") or ""
        new_seen = row.get("last_seen") or ""
        if new_seen > current_seen:
            current["last_seen"] = row.get("last_seen")
    for label, row in grouped.items():
        row["owned_live"] = owned.get(label, 0)
        row["controller_key"] = _display(label, limit=128)
        if row.get("controller_liveness_state") != "DEAD":
            row["retire_command"] = None
        elif row.get("label"):
            row["retire_command"] = _display(
                _controller_retire_command(str(row["label"])), limit=160
            )
        if row.get("controller_liveness_state") != "UNKNOWN":
            row["last_error"] = None
            row["probe_command"] = None
        elif row.get("label") and not row.get("probe_command"):
            generation = row.get("generation")
            row["probe_command"] = _display(
                _controller_probe_command(
                    str(row["label"]),
                    generation if isinstance(generation, int) and not isinstance(generation, bool) else None,
                ),
                limit=160,
            )
    return sorted(grouped.values(), key=_controller_sort_key)


def _raw_controller_session_id(value: object) -> str | None:
    """Return an ownership identity unchanged; presentation is a later step."""
    return value if isinstance(value, str) and value else None


def _controller_session_digest(value: object) -> str | None:
    """Return the wake layer's stable short identity hash for publication."""
    raw_session_id = _raw_controller_session_id(value)
    return goalflight_wake.controller_session_digest(raw_session_id)


def _journal_in_flight_count(
    authority: goalflight_journal.Journal,
    *,
    controller_label: str,
) -> int:
    """Count live work this controller is responsible for being wakeable for."""
    rows = authority.read_all(
        """
        SELECT attempts.attempt_id, attempts.owner_controller_label,
               EXISTS (
                   SELECT 1
                   FROM controller_leases AS owner_lease
                   WHERE owner_lease.project_root = attempts.project_root
                     AND owner_lease.label = attempts.owner_controller_label
                     AND owner_lease.state = 'ACTIVE'
               ) AS owner_has_active_lease
        FROM dispatch_attempts AS attempts
        WHERE attempts.project_root = ?
          AND attempts.lifecycle_state IN (?, ?, ?)
        ORDER BY attempts.attempt_id
        """,
        (str(authority.project_root), *goalflight_journal.ATTEMPT_LIVE_STATES),
    )
    # A retired/absent owner lease makes the attempt unowned. Its terminal will
    # fan out to every controller, so everybody is responsible for being
    # wakeable. Mail delivery and cursors are irrelevant: HUNG measures work.
    return sum(
        1
        for row in rows
        if row["owner_controller_label"] is None
        or not bool(row["owner_has_active_lease"])
        or row["owner_controller_label"] == controller_label
    )


def _controller_contexts_by_session(
    project_root: Path,
    records: list[dict[str, Any]] | None,
    *,
    include_all: bool = False,
    include_ended: bool = False,
    include_locked_ended: bool = False,
    probe_metadata: dict[str, int] | None = None,
    authority: goalflight_journal.Journal | None = None,
    open_if_missing: bool = True,
) -> dict[str, dict[str, object | None]]:
    """Resolve identity and liveness once per journal lease generation.

    ``records`` limits the work to sessions that can appear in fleet worker
    rows; it never supplies attempt ownership. ``include_all`` lets the
    attention plane classify every lease generation in its selected scope.
    """
    requested = (
        None
        if records is None or include_all
        else {
            session_id
            for record in records
            if (session_id := _raw_controller_session_id(
                record.get("controller_session_id")
            ))
        }
    )
    if requested == set():
        return {}
    try:
        if authority is None and open_if_missing:
            authority = goalflight_journal.Journal.open_reader(project_root)
        if authority is None:
            return {
                session_id: _empty_controller_context()
                for session_id in (requested or set())
            }
        lease_rows = authority.lease_records(
            include_ended=include_ended or include_locked_ended
        )
    except (
        goalflight_journal.JournalBusy,
        goalflight_journal.JournalDisappeared,
        goalflight_journal.JournalIOError,
    ):
        return {
            session_id: _empty_controller_context()
            for session_id in (requested or set())
        }
    except (goalflight_journal.JournalError, OSError, ValueError):
        return {
            session_id: _empty_controller_context()
            for session_id in (requested or set())
        }

    if not include_ended:
        candidates_by_label: dict[str, list[dict[str, object]]] = {}
        for row in lease_rows:
            raw_label = row.get("label")
            if (
                (
                    row.get("state") == goalflight_journal.LEASE_ACTIVE
                    or include_locked_ended
                )
                and isinstance(raw_label, str)
                and raw_label
            ):
                candidates_by_label.setdefault(raw_label, []).append(row)
        bounded_rows: list[dict[str, object]] = []
        truncated = 0
        for rows in candidates_by_label.values():
            newest = sorted(
                rows,
                key=lambda row: (
                    int(row["generation"])
                    if isinstance(row.get("generation"), int)
                    else -1
                ),
                reverse=True,
            )
            bounded_rows.extend(newest[:CONTROLLER_GENERATION_PROBE_LIMIT])
            truncated += max(0, len(newest) - CONTROLLER_GENERATION_PROBE_LIMIT)
        if probe_metadata is not None:
            probe_metadata["controller_history_probes_truncated"] = (
                probe_metadata.get("controller_history_probes_truncated", 0)
                + truncated
            )
        lease_rows = bounded_rows

    labels: dict[str, set[str]] = {}
    rows_by_session: dict[str, list[dict[str, object]]] = {}
    active_rows: dict[str, list[dict[str, object]]] = {}
    for row in lease_rows:
        session_id = _raw_controller_session_id(row.get("nonce"))
        raw_label = row.get("label")
        label = raw_label if isinstance(raw_label, str) and raw_label else None
        if session_id is None:
            continue
        rows_by_session.setdefault(session_id, []).append(row)
        if label:
            labels.setdefault(session_id, set()).add(label)
        if row.get("state") == goalflight_journal.LEASE_ACTIVE:
            active_rows.setdefault(session_id, []).append(row)

    holder_by_session: dict[str, bool | None] = {}
    locked_ended_rows: dict[str, list[dict[str, object]]] = {}
    if include_locked_ended:
        # ``lease_rows`` is already the newest-eight-per-label union. Probe
        # every ended row in that bounded set; the truncation counter above
        # says exactly how many generations were not checked.
        for row in lease_rows:
            if row.get("state") == goalflight_journal.LEASE_ACTIVE:
                continue
            session_id = _raw_controller_session_id(row.get("nonce"))
            raw_label = row.get("label")
            label = raw_label if isinstance(raw_label, str) and raw_label else None
            nonce = _raw_controller_session_id(row.get("nonce"))
            if session_id is None or label is None or nonce is None:
                continue
            try:
                held = goalflight_wake.lease_holder_alive(
                    project_root,
                    controller_label=label,
                    lease_nonce=nonce,
                )
            except (OSError, RuntimeError, ValueError):
                held = None
            holder_by_session[session_id] = held
            # Kernel state outranks lease bookkeeping: a superseded or
            # otherwise ended generation that still owns its exact lock is
            # a live zombie candidate and remains in attention scope.
            if held is True:
                locked_ended_rows.setdefault(session_id, []).append(row)

    if requested is not None:
        wanted = requested
    elif include_locked_ended:
        wanted = set(active_rows) | set(locked_ended_rows)
    else:
        wanted = set(rows_by_session)

    contexts: dict[str, dict[str, object | None]] = {}
    for session_id in wanted:
        label_values = labels.get(session_id, set())
        label = next(iter(label_values)) if len(label_values) == 1 else None
        matches = active_rows.get(session_id, []) or locked_ended_rows.get(
            session_id, []
        )
        wake_coverage: dict[str, object] | None = None
        lock_reason: str | None = None
        probe_error: str | None = None
        if not matches:
            holder_lock: bool | None = False
            live_waiter_count = 0
        elif len(matches) != 1 or label is None:
            holder_lock = None
            live_waiter_count = 0
            lock_reason = "lease generation is ambiguous"
        else:
            try:
                raw_nonce = str(matches[0].get("nonce") or "")
                holder_lock = holder_by_session.get(session_id)
                if session_id not in holder_by_session:
                    holder_lock = goalflight_wake.lease_holder_alive(
                        project_root,
                        controller_label=label,
                        lease_nonce=raw_nonce,
                    )
                if holder_lock is None:
                    lock_reason = "lease lock file missing"
                live_waiters = goalflight_wake.live_waiters(
                    project_root,
                    controller_label=label,
                    generation_key=raw_nonce,
                    prune_dead=False,
                )
                if live_waiters is None:
                    live_waiter_count = None
                else:
                    live_waiter_count = len(live_waiters)
                wake_coverage = goalflight_wake.coverage_status(
                    project_root,
                    controller_label=label,
                    lease_nonce=raw_nonce,
                    observed_waiters=live_waiters,
                )
            except (OSError, RuntimeError, ValueError) as exc:
                holder_lock = None
                live_waiter_count = None
                probe_error = type(exc).__name__
                lock_reason = "lease probe failed"
        in_flight_count = _journal_in_flight_count(
            authority,
            controller_label=label or "",
        )
        generation_rows = matches or rows_by_session.get(session_id, [])
        generation = (
            int(generation_rows[0]["generation"])
            if len(generation_rows) == 1
            and isinstance(generation_rows[0].get("generation"), int)
            else None
        )
        last_seen = None
        if generation_rows:
            last_seen = generation_rows[0].get("renewed_at") or generation_rows[0].get(
                "claimed_at"
            )
        listener_live = (
            wake_coverage.get("live_waiters")
            if isinstance(wake_coverage, dict)
            else None
        )
        if not isinstance(listener_live, int) or isinstance(listener_live, bool):
            listener_live = None
        listener_target = (
            wake_coverage.get("target_waiters")
            if isinstance(wake_coverage, dict)
            else None
        )
        if not isinstance(listener_target, int) or isinstance(listener_target, bool):
            listener_target = None
        wake_mode = (
            wake_coverage.get("wake_mode")
            if isinstance(wake_coverage, dict)
            else None
        )
        contexts[session_id] = {
            "label": label,
            "generation": generation,
            "liveness_state": classify_controller(
                holder_lock,
                live_waiter_count,
                in_flight_count,
            ),
            "listener_live": listener_live,
            "listener_target": listener_target,
            "wake_mode": (
                wake_mode if wake_mode in {"persistent", "portable"} else None
            ),
            "in_flight_count": in_flight_count,
            "last_seen": last_seen,
            "last_error": _controller_unknown_error(
                holder_lock=holder_lock,
                live_waiter_count=live_waiter_count,
                lock_reason=lock_reason,
                probe_error=probe_error,
            ),
        }
    return contexts


def _controller_labels_by_session(
    project_root: Path,
    records: list[dict[str, Any]],
) -> dict[str, str]:
    """Return only unambiguous journal labels for stamped controller sessions."""
    wanted = {
        session_id
        for record in records
        if (session_id := _raw_controller_session_id(
            record.get("controller_session_id")
        ))
    }
    if not wanted:
        return {}
    try:
        lease_rows = goalflight_journal.Journal.open_reader(
            project_root
        ).lease_records(include_ended=True)
    except (
        goalflight_journal.JournalBusy,
        goalflight_journal.JournalDisappeared,
        goalflight_journal.JournalIOError,
    ):
        return {}
    except (goalflight_journal.JournalError, OSError, ValueError):
        return {}
    labels: dict[str, set[str]] = {}
    for row in lease_rows:
        session_id = _raw_controller_session_id(row.get("nonce"))
        label = _display(row.get("label"), limit=64)
        if session_id in wanted and label:
            labels.setdefault(session_id, set()).add(label)
    return {
        session_id: next(iter(values))
        for session_id, values in labels.items()
        if len(values) == 1
    }


def _controller_fields(
    record: dict[str, Any],
    controller_labels: dict[str, str],
    controller_liveness: dict[str, str] | None = None,
    *,
    journal_owner_label: object = None,
) -> dict[str, Any]:
    raw_session_id = _raw_controller_session_id(record.get("controller_session_id"))
    session_digest = _controller_session_digest(raw_session_id)
    raw_pid = record.get("controller_pid")
    controller_pid = raw_pid if isinstance(raw_pid, int) and not isinstance(raw_pid, bool) and raw_pid > 0 else None
    session_label = (
        _display(controller_labels.get(raw_session_id), limit=64)
        if raw_session_id
        else None
    )
    # Session map is the live lease identity. Journal owner and the stamped
    # record label are the same attribution when the session is absent, so a
    # worker is not silently unowned while in_flight_count still names it.
    label = (
        session_label
        or _display(journal_owner_label, limit=64)
        or _display(record.get("controller_label"), limit=64)
    )
    if label:
        display, state = label, "label"
    elif session_digest:
        display, state = f"session · {session_digest}", "session"
    elif controller_pid is not None:
        display, state = "owned · identity unknown", "owned_unknown"
    else:
        display, state = "unowned", "unowned"
    liveness_state = (
        (controller_liveness or {}).get(raw_session_id)
        if raw_session_id
        else "UNKNOWN"
    )
    if liveness_state not in CONTROLLER_LIVENESS_STATES:
        liveness_state = "UNKNOWN"
    return {
        "controller_session_digest": session_digest,
        "controller_pid": controller_pid,
        "controller_label": label,
        "controller_display": display,
        "controller_state": state,
        "controller_liveness_state": liveness_state,
    }


def _worker_age_filter_fields(
    *,
    started_at: str | None,
    is_terminal: bool | None,
    sampled_at: dt.datetime | None,
    observed_live: bool | None,
) -> dict[str, Any]:
    if is_terminal is True:
        return {"age_filter_match": False, "age_filter_reason": "terminal"}
    if observed_live is True:
        return {"age_filter_match": False, "age_filter_reason": "observed_live"}
    parsed = _parse_timestamp(started_at)
    if parsed is None:
        return {"age_filter_match": False, "age_filter_reason": "started_at_unknown"}
    if sampled_at is None:
        return {"age_filter_match": False, "age_filter_reason": "sample_time_unknown"}
    age_seconds = (sampled_at - parsed).total_seconds()
    if age_seconds < 0:
        return {"age_filter_match": False, "age_filter_reason": "started_at_future"}
    if age_seconds > WORKER_AGE_FILTER_SECONDS:
        return {"age_filter_match": True, "age_filter_reason": "older_than_threshold"}
    return {"age_filter_match": False, "age_filter_reason": "within_threshold"}


def _worker_observed_live_fields(
    record: dict[str, Any],
    *,
    sampled_at: dt.datetime | None,
    status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    alive = record.get("worker_still_alive")
    if isinstance(alive, bool):
        return {"observed_live": alive, "observed_live_source": "identity_recheck"}
    status = status or {}
    dispatch_id = record.get("dispatch_id")
    if not status or not dispatch_id or status.get("dispatch_id") != dispatch_id:
        return {"observed_live": None, "observed_live_source": "unobserved"}
    observed_at = _parse_timestamp(status.get("heartbeat_at"))
    if observed_at is None:
        updated_at = _number(status.get("updated_at"))
        if updated_at is not None:
            with contextlib.suppress(OverflowError, OSError, ValueError):
                observed_at = dt.datetime.fromtimestamp(updated_at, tz=dt.timezone.utc)
    status_alive = status.get("worker_alive")
    if sampled_at is None or observed_at is None or not isinstance(status_alive, bool):
        return {"observed_live": None, "observed_live_source": "unobserved"}
    sample_age_s = (sampled_at - observed_at).total_seconds()
    if abs(sample_age_s) <= WORKER_LIVE_SAMPLE_FRESH_SECONDS:
        return {"observed_live": status_alive, "observed_live_source": "fresh_status"}
    return {"observed_live": None, "observed_live_source": "unobserved"}


def _worker_row(
    record: dict[str, Any],
    *,
    node_id: str | None = "local",
    sampled_at: dt.datetime | None = None,
    controller_labels: dict[str, str] | None = None,
    controller_liveness: dict[str, str] | None = None,
    journal_authority: dict[str, Any] | None = None,
) -> dict[str, Any]:
    # Raw authority fields remain available for diagnosis, while renderers use
    # only the canonical verdict below for filtering and presentation.
    record = goalflight_status.reconcile_fast_plane_record(
        record,
        retain_status_snapshot=True,
        tail_hint_required=True,
    )
    alive = record.get("worker_still_alive")
    started_at = _iso_timestamp(record.get("started_at"))
    # This runs only after retention has bounded the warm rows. Reading their
    # sidecars preserves presentation truth without reopening every permanent
    # runs.d record in the machine-wide status pass.
    status = goalflight_status._status_json_payload(record)  # noqa: SLF001
    verdict, authority_detail, authority_resolution = _authority_snapshot(
        record,
        status=status,
        journal=journal_authority,
    )
    observed_live = _worker_observed_live_fields(
        record,
        sampled_at=sampled_at,
        status=status,
    )
    os_sandbox = record.get("os_sandbox")
    sandbox_posture = os_sandbox if isinstance(os_sandbox, dict) else {}
    return {
        "dispatch_id": _display(record.get("dispatch_id"), limit=128),
        "node_id": _display(node_id, limit=96),
        "agent": _display(record.get("agent"), limit=64),
        "engine": _display(record.get("engine"), limit=64),
        "shape": _display(record.get("shape"), limit=64),
        "transport": _display(record.get("transport"), limit=64),
        "os_sandbox": _display(record.get("os_sandbox"), limit=32),
        "os_sandbox_requested": _display(
            sandbox_posture.get("requested_profile"), limit=32
        ),
        "os_sandbox_supported": _display(
            sandbox_posture.get("supported_profile"), limit=32
        ),
        "os_sandbox_enforced": _display(
            sandbox_posture.get("enforced_profile"), limit=32
        ),
        "state": _display(record.get("state"), limit=64),
        "classification": _display(record.get("classification"), limit=64),
        "terminal_state": _display(record.get("terminal_state"), limit=64),
        "liveness_state": _display(record.get("liveness_state"), limit=64),
        "worker_alive": alive if isinstance(alive, bool) else None,
        "started_at": started_at,
        "ended_at": _iso_timestamp(record.get("ended_at")),
        **verdict,
        "authority_detail": _display(authority_detail, limit=512),
        "authority_resolution": _display(authority_resolution, limit=64),
        **_controller_fields(
            record,
            controller_labels or {},
            controller_liveness or {},
            journal_owner_label=(
                journal_authority.get("owner_controller_label")
                if isinstance(journal_authority, dict)
                else None
            ),
        ),
        **observed_live,
        **_worker_age_filter_fields(
            started_at=started_at,
            is_terminal=verdict["is_terminal"],
            sampled_at=sampled_at,
            observed_live=observed_live["observed_live"],
        ),
        "task_ids": [
            item
            for item in (
                _display(task_id, limit=64)
                for task_id in (
                    record.get("task_ids")
                    if isinstance(record.get("task_ids"), list)
                    else []
                )
            )
            if item is not None
        ],
        "prompt_file": goalflight_fleet_console_history.prompt_filename(
            record.get("dispatch_id")
        ),
    }


def _worker_sort_key(row: dict[str, Any]) -> tuple[Any, ...]:
    """Newest started first; missing starts last; identities break equal-time ties."""
    started_at = _parse_timestamp(row.get("started_at"))
    return (
        row.get("observed_live") is not True,
        started_at is None,
        -started_at.timestamp() if started_at is not None else 0.0,
        str(row.get("dispatch_id") or ""),
        str(row.get("node_id") or ""),
        str(row.get("agent") or ""),
        str(row.get("transport") or ""),
    )


def _sort_worker_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(rows, key=_worker_sort_key)


def _rate_pressure_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    pressure = payload.get("rate_pressure")
    pressure = pressure if isinstance(pressure, dict) else {}
    rows = []
    for item in pressure.get("providers_under_pressure") or []:
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "provider": _display(item.get("provider"), limit=64),
                "scope": _display(item.get("scope"), limit=32),
                "count": _number(item.get("count")),
            }
        )
    return rows


def _warning_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for item in payload.get("warnings") or []:
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "code": _display(item.get("code"), limit=64),
                "severity": _display(item.get("severity"), limit=16),
                "count": _number(item.get("queue_depth")),
            }
        )
    return rows


def _machine_row(payload: dict[str, Any]) -> dict[str, Any]:
    capacity = payload.get("capacity")
    capacity = capacity if isinstance(capacity, dict) else {}
    capacity_state = payload.get("capacity_state")
    capacity_state = capacity_state if isinstance(capacity_state, dict) else {}
    leases = capacity_state.get("leases")
    leases = leases if isinstance(leases, dict) else {}
    active_leases = sum(
        1 for item in leases.values() if isinstance(item, dict) and item.get("state") == "active"
    )
    dispatch = payload.get("dispatch")
    dispatch = dispatch if isinstance(dispatch, dict) else {}
    records = [item for item in dispatch.get("records") or [] if isinstance(item, dict)]
    return {
        "operating_cap": _number(capacity.get("operating_cap")),
        "active_leases": active_leases,
        # Live workers, not len(records). The ledger keeps terminal records
        # permanently -- 1541 on this machine against 37 actually running -- so
        # counting rows answered "how much history is there", printed under a
        # label that reads "how much is running".
        "local_workers": sum(1 for item in records if _record_is_running(item)),
        "rate_pressure": _rate_pressure_rows(payload),
        "warnings": _warning_rows(payload),
    }


def _vendor_rows(rows: object) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        return []
    seat_indexes: dict[str, int] = {}
    projected = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        provider = _display(row.get("provider"), limit=64) or "unknown"
        seat_indexes[provider] = seat_indexes.get(provider, 0) + 1
        flags = row.get("flags") if isinstance(row.get("flags"), list) else []
        projected.append(
            {
                "provider": provider,
                "seat_index": seat_indexes[provider],
                "remaining": _display(row.get("remaining"), limit=128),
                "reset_at": _number(row.get("reset_at")),
                "flags": [item for item in (_display(flag, limit=32) for flag in flags) if item],
            }
        )
    return projected


def _remote_row(
    payload: object,
    *,
    sampled_at: dt.datetime | None = None,
) -> dict[str, Any]:
    data = payload if isinstance(payload, dict) else {}
    raw_dispatches = [
        row for row in data.get("dispatches") or [] if isinstance(row, dict)
    ]
    fast_dispatches, history_excluded = _fast_plane_records(
        raw_dispatches,
        sampled_at=sampled_at,
    )
    history_excluded += max(0, int(data.get("history_excluded") or 0))
    workers = []
    for row in fast_dispatches:
        reachable = row.get("ssh_reachable")
        worker = _worker_row(
            row,
            node_id=_display(row.get("node"), limit=96),
            sampled_at=sampled_at,
        )
        worker.update(
            {
                "quarantine_reason": _display(row.get("quarantine_reason"), limit=64),
                "ssh_reachable": reachable if isinstance(reachable, bool) else None,
                "may_release": row.get("may_release") if isinstance(row.get("may_release"), bool) else None,
            }
        )
        workers.append(worker)
    nodes = []
    for node in data.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        auth_states = []
        for account in node.get("accounts") or []:
            if not isinstance(account, dict):
                continue
            state = _display(account.get("auth_probe"), limit=32)
            if state and state not in auth_states:
                auth_states.append(state)
        dispatches = node.get("dispatches")
        nodes.append(
            {
                "node_id": _display(node.get("node_id"), limit=96),
                "dispatches": len(dispatches) if isinstance(dispatches, list) else 0,
                "auth_states": auth_states,
            }
        )
    return {
        "available": data.get("available") if isinstance(data.get("available"), bool) else False,
        "history_excluded": history_excluded,
        "nodes": nodes,
        "workers": _sort_worker_rows(workers),
    }


def _session_row(payload: object) -> dict[str, Any]:
    data = payload if isinstance(payload, dict) else {}
    return {
        "available": bool(data),
        "active": data.get("active") if isinstance(data.get("active"), bool) else None,
        "queue_state": _display(data.get("queue_state"), limit=32),
        "queue_last_touched": _iso_timestamp(data.get("queue_last_touched")),
        "active_leases": _number(data.get("active_capacity_leases_in_project")),
    }


def _fast_session_row(machine_status: object, project_root: str) -> dict[str, Any]:
    """Measured lease count without asserting unscanned repository state."""
    status = machine_status if isinstance(machine_status, dict) else {}
    capacity_state = status.get("capacity_state")
    leases = (
        capacity_state.get("leases")
        if isinstance(capacity_state, dict)
        else {}
    )
    leases = leases if isinstance(leases, dict) else {}
    active_leases = sum(
        1
        for lease in leases.values()
        if isinstance(lease, dict)
        and lease.get("state") == "active"
        and _canonical_root(lease.get("project_root")) == project_root
    )
    return {
        "available": False,
        "active": None,
        "queue_state": None,
        "queue_last_touched": None,
        "active_leases": active_leases,
    }


def _milestone_row(payload: object) -> dict[str, Any]:
    data = payload if isinstance(payload, dict) else {}
    return {
        "available": bool(data) and not bool(data.get("error")),
        "active_cadence": data.get("active_cadence") if isinstance(data.get("active_cadence"), bool) else None,
        "commits_since": _number(data.get("commits_since")),
        "cadence": _number(data.get("K")),
        "due": data.get("due") if isinstance(data.get("due"), bool) else None,
    }


def _roots_with_records(machine_status: object) -> set[str]:
    """Canonical roots with a LIVE worker right now.

    Deliberately not "has a dispatch record": the ledger keeps terminal
    records forever, so that set was 282 roots on this machine while only 17
    had anything actually running. Prioritising 282 candidates for 12 slots
    is not prioritising. Liveness is what the operator is watching, and it
    is what makes the deep sample cheap when work is concentrated -- fifty
    queued workers in one project is one project to sample, not twelve.
    """
    status = machine_status if isinstance(machine_status, dict) else {}
    dispatch = status.get("dispatch")
    records = (dispatch or {}).get("records") if isinstance(dispatch, dict) else []
    roots = set()
    for item in records or []:
        if isinstance(item, dict):
            if _record_is_terminal(item):
                continue
            root = _canonical_root(item.get("project_root"))
            if root is not None:
                roots.add(root)
    return roots


def _roots_with_active_leases(machine_status: object) -> set[str]:
    """Canonical roots named by machine-capacity leases that are ACTIVE now."""
    status = machine_status if isinstance(machine_status, dict) else {}
    capacity_state = status.get("capacity_state")
    leases = (
        capacity_state.get("leases")
        if isinstance(capacity_state, dict)
        else {}
    )
    roots: set[str] = set()
    for lease in leases.values() if isinstance(leases, dict) else []:
        if not isinstance(lease, dict) or lease.get("state") != "active":
            continue
        root = _canonical_root(lease.get("project_root"))
        if root is not None:
            roots.add(root)
    return roots


def _fast_project_roots(
    machine_status: object,
    *,
    queue_by_root: dict[str, dict[str, Any]] | None = None,
) -> set[str]:
    """Projects whose state can still change on the short-poll plane."""
    return (
        _roots_with_records(machine_status)
        | _roots_with_active_leases(machine_status)
        | set(queue_by_root or {})
    )


def _without_terminal_history(machine_status: object) -> dict[str, Any]:
    """Copy aggregate status with only non-terminal/unresolved dispatch rows."""
    payload = dict(machine_status) if isinstance(machine_status, dict) else {}
    dispatch = payload.get("dispatch")
    dispatch = dict(dispatch) if isinstance(dispatch, dict) else {}
    dispatch["records"] = [
        record
        for record in dispatch.get("records") or []
        if isinstance(record, dict)
        and _worker_display_verdict(record)["is_terminal"] is not True
    ]
    payload["dispatch"] = dispatch
    return payload


def _queue_summary(machine_status: object) -> tuple[dict[str, dict[str, Any]], int]:
    """Per-project queue depth from the dispatch queue, plus a machine total.

    Queued work is INVISIBLE in dispatch records -- an entry becomes a record
    only once it launches. Without this, fifty queued research workers show as
    an empty fleet, and a queue draining (or stalled) cannot be watched at all,
    which is the single thing an operator most wants to see during a large fan-
    out.

    Read straight from the queue directory rather than through a peer module:
    no existing component projects queue depth, so there is nothing to consume.
    Cost is one readdir plus a small JSON parse per entry.
    """
    status = machine_status if isinstance(machine_status, dict) else {}
    dispatch = status.get("dispatch")
    state_dir = (dispatch or {}).get("state_dir") if isinstance(dispatch, dict) else None
    if not isinstance(state_dir, str) or not state_dir:
        return {}, 0
    queue_dir = Path(state_dir) / "dispatch-queue"
    by_root: dict[str, dict[str, Any]] = {}
    total = 0
    try:
        entries = sorted(queue_dir.iterdir())
    except OSError:
        return {}, 0
    for entry in entries:
        if not entry.is_file():
            continue
        try:
            row = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(row, dict):
            continue
        root = _canonical_root(row.get("project_root"))
        if root is None:
            continue
        total += 1
        bucket = by_root.setdefault(root, {"depth": 0, "lanes": {}, "oldest_created_at": None})
        bucket["depth"] += 1
        agent = _display(row.get("agent"), limit=32) or "unknown"
        bucket["lanes"][agent] = bucket["lanes"].get(agent, 0) + 1
        created = _iso_timestamp(row.get("created_at"))
        if created and (bucket["oldest_created_at"] is None or created < bucket["oldest_created_at"]):
            bucket["oldest_created_at"] = created
    for bucket in by_root.values():
        bucket["lanes"] = [
            {"agent": agent, "count": count}
            for agent, count in sorted(bucket["lanes"].items(), key=lambda kv: (-kv[1], kv[0]))
        ]
    return by_root, total


def _empty_queue_row() -> dict[str, Any]:
    return {"depth": 0, "lanes": [], "oldest_created_at": None}


def _attach_queue_rows(
    projects: list[dict[str, Any]],
    queue_by_root: dict[str, dict[str, Any]],
) -> None:
    """Merge queue depth onto project rows, keyed by the SAME id the rows use.

    Project rows carry ``project_id`` (a hash) rather than the raw root, which
    is on the deny list, so the join has to go through _project_id as well.
    Every row gets a queue key: an absent queue must read as depth 0, never as
    a missing field a renderer has to interpret.
    """
    by_id = {_project_id(root): row for root, row in queue_by_root.items()}
    for project in projects:
        found = by_id.get(project.get("project_id"))
        project["queue"] = dict(found) if found else _empty_queue_row()


def _record_is_terminal(record: object) -> bool:
    """True only when the reconciled worker verdict is terminal.

    Retention is based on terminality and age.  It must not reinterpret a
    liveness class or capacity-lease absence as terminal: detached orphans whose
    exact worker identity still matches remain live fast-plane work.
    """
    if not isinstance(record, dict):
        return True
    return _worker_display_verdict(record)["is_terminal"] is True


def _record_is_running(record: object) -> bool:
    if not isinstance(record, dict):
        return False
    verdict = _worker_display_verdict(record)
    return verdict["is_terminal"] is False


def _all_registered_roots(payload: object) -> set[str]:
    """Every root in the registry, independent of the deep-sample cap.

    Kept separate from _registered_projects, which returns the CAPPED head:
    conflating them made "registered" a statement about this tick's sampling
    rather than about the project.
    """
    roots: set[str] = set()
    for row in payload if isinstance(payload, list) else []:
        if isinstance(row, dict):
            root = _canonical_root(row.get("project_root"))
            if root is not None:
                roots.add(root)
    return roots


def _registered_projects(
    payload: object,
    *,
    active_roots: set[str] | None = None,
    only_roots: set[str] | None = None,
    limit: int = DEFAULT_MAX_PROJECTS,
) -> tuple[list[dict[str, Any]], int, list[dict[str, Any]]]:
    """Return (sampled projects, total registered, omitted identities).

    Sampling every registered project is not viable. The registry accumulates a
    root for every project any dispatch ever ran in, including throwaway
    per-ticket worktrees. Measured on this machine: 1433 registered roots at
    ~1.0s each (one session_status plus one milestone call), so a full serial
    pass costs ~1433s against a ~60s drain tick. That 24x overrun is why this
    projection hung and emitted nothing at all.

    The fast plane supplies ``only_roots`` so 1,954 historical worktree paths
    contribute to the total without filesystem-resolving or projecting each
    one. Legacy callers may still request the recency-ordered bounded head.

    The total is returned alongside so the payload can say how many were NOT
    sampled. A silent cap would read as "these are all your projects" -- the
    field-asserting-an-unmeasured-state failure this projection exists to
    avoid.
    """
    rows = payload if isinstance(payload, list) else []
    result = []
    omitted: list[dict[str, Any]] = []
    seen_total: set[str] = set()
    seen_result: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        supplied = row.get("project_root")
        if not isinstance(supplied, str) or not supplied:
            continue
        # Registry writes are already canonical. abspath/expanduser avoids a
        # stat/readlink walk across thousands of deleted worktrees merely to
        # count them; only live candidates pay canonical_root's filesystem I/O.
        registry_root = os.path.abspath(os.path.expanduser(supplied))
        if registry_root in seen_total:
            continue
        seen_total.add(registry_root)
        if only_roots is not None and registry_root not in only_roots:
            omitted.append(_registry_omitted_identity(row, registry_root))
            continue
        root = _canonical_root(registry_root)
        if (
            root is None
            or root in seen_result
            or (only_roots is not None and root not in only_roots)
        ):
            if root is None or (only_roots is not None and root not in only_roots):
                omitted.append(_registry_omitted_identity(row, registry_root))
            continue
        seen_result.add(root)
        result.append(
            {
                "root": root,
                "last_seen": _iso_timestamp(row.get("last_seen")),
                "skill_version": _display(row.get("skill_version"), limit=32),
                "repo_identity": _repo_identity_scalar(row.get("repo_identity")),
            }
        )
    total = len(seen_total)
    # A project with work in flight outranks a merely-recent one: that is what
    # the operator is actually watching. Recency breaks ties.
    active = active_roots or set()
    result.sort(
        key=lambda item: (
            item["root"] in active,
            item["last_seen"] is not None,
            item["last_seen"] or "",
        ),
        reverse=True,
    )
    if limit is not None and limit >= 0:
        omitted.extend(
            _registry_omitted_identity(item, str(item["root"]))
            for item in result[limit:]
        )
        result = result[:limit]
    omitted.sort(
        key=lambda item: (
            item["last_seen"] is not None,
            item["last_seen"] or "",
            item["name"] or "",
        ),
        reverse=True,
    )
    return result, total, omitted


def _registry_omitted_identity(row: dict[str, Any], root: str) -> dict[str, Any]:
    """Shareable identity for a registered root this tick did not deep-sample.

    Uses the registry path string only: no filesystem resolve, so thousands of
    deleted worktrees stay a name+digest rather than a stat storm. Absolute
    paths never leave this helper -- the allowlist sees a basename and a hash.
    """
    return {
        "name": _omitted_display_name(row, root),
        "project_id": _project_id(root),
        "repo_identity": _repo_identity_scalar(row.get("repo_identity")),
        "last_seen": _iso_timestamp(row.get("last_seen")),
    }


def _omitted_display_name(row: dict[str, Any], root: str) -> str:
    """Prefer cached owner/name; fall back to basename; suffix leftover generics."""
    shown = _repo_display(row.get("repo_identity"))
    if shown:
        return shown
    basename = _display(Path(root).name or "project", limit=64) or "project"
    return _generic_basename_label(basename, _project_id(root))


def _project_rows(
    machine_status: dict[str, Any],
    registered_projects: list[dict[str, Any]],
    errors: list[str],
    all_registered_roots: set[str] | None = None,
    sampled_at: dt.datetime | None = None,
    visible_roots: set[str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int, list[dict[str, Any]]]:
    """Rows only for projects with mutable work or an ACTIVE capacity lease.

    Historical terminal-only roots are counted for the slow blob without path
    resolution, journal opens, status-sidecar reads, or project-row projection.
    Controller panel rows reuse the same ACTIVE lease pass.
    """
    dispatch = machine_status.get("dispatch")
    dispatch = dispatch if isinstance(dispatch, dict) else {}
    records = [item for item in dispatch.get("records") or [] if isinstance(item, dict)]
    active_roots = (
        set(visible_roots)
        if visible_roots is not None
        else _fast_project_roots(machine_status)
    )
    by_root = {
        item["root"]: item
        for item in registered_projects
        if item.get("root") in active_roots
    }
    records_by_root: dict[str, list[dict[str, Any]]] = {}
    rooted_dispatches: set[int] = set()
    hidden_history_excluded = 0
    for record in records:
        supplied = record.get("project_root")
        if not isinstance(supplied, str) or not supplied:
            continue
        rooted_dispatches.add(id(record))
        if _record_is_terminal(record):
            # Ledger project roots are canonical at write time. Avoid resolving
            # thousands of deleted historical worktrees merely to discard them.
            root = os.path.abspath(os.path.expanduser(supplied))
            if root not in active_roots:
                hidden_history_excluded += 1
                continue
        else:
            root = _canonical_root(supplied)
            if root is None:
                rooted_dispatches.discard(id(record))
                continue
            active_roots.add(root)
        records_by_root.setdefault(root, []).append(record)

    for root in active_roots:
        by_root.setdefault(
            root,
            {
                "root": root,
                "last_seen": None,
                "skill_version": None,
                "repo_identity": None,
            },
        )

    projects = []
    controllers: list[dict[str, Any]] = []
    # Deep-sample membership: who gets session/milestone calls this tick.
    sampled_roots = {item["root"] for item in registered_projects}
    # Registry membership: a fact about the project, independent of sampling.
    registered_roots = set(all_registered_roots) if all_registered_roots is not None else sampled_roots
    for root in sorted(by_root):
        metadata = by_root[root]
        scoped_records = records_by_root.get(root, [])
        probe_records = [
            record for record in scoped_records if not _record_is_terminal(record)
        ]
        journal_reader = _project_journal_reader(Path(root))
        lease_root = _git_main_checkout(root)
        lease_reader = (
            journal_reader
            if lease_root == root
            else _project_journal_reader(Path(lease_root))
        )
        all_journal_authority = _journal_authority_by_dispatch(
            Path(root),
            probe_records,
            authority=journal_reader,
            open_if_missing=False,
        )
        fast_records, history_excluded = _fast_plane_records(
            scoped_records,
            sampled_at=sampled_at,
            journal_authority=all_journal_authority,
        )

        controller_contexts = _controller_contexts_by_session(
            Path(lease_root),
            [record for record in fast_records if not _record_is_terminal(record)],
            include_all=True,
            authority=lease_reader or journal_reader,
            open_if_missing=False,
        )
        controllers.extend(
            _controller_panel_row(lease_root, context)
            for context in controller_contexts.values()
            if context.get("label")
        )
        controller_labels = {
            session_id: str(context["label"])
            for session_id, context in controller_contexts.items()
            if context.get("label")
        }
        controller_liveness = {
            session_id: str(context["liveness_state"])
            for session_id, context in controller_contexts.items()
        }
        journal_authority = all_journal_authority
        worker_rows = _sort_worker_rows(
            [
                _worker_row(
                    record,
                    sampled_at=sampled_at,
                    controller_labels=controller_labels,
                    controller_liveness=controller_liveness,
                    journal_authority=journal_authority.get(
                        str(record.get("dispatch_id") or "")
                    ),
                )
                for record in fast_records
            ]
        )
        parent = _parent_fields(root)
        projects.append(
            {
                "project_id": _project_id(root),
                "name": _display(Path(root).name or "project", limit=64),
                "registered": root in registered_roots,
                "last_seen": metadata.get("last_seen"),
                "skill_version": metadata.get("skill_version"),
                "history_excluded": history_excluded,
                "parent_project_id": parent["parent_project_id"],
                "parent_name": parent["parent_name"],
                "worktree_name": parent["worktree_name"],
                "repo_identity": _repo_identity_scalar(metadata.get("repo_identity")),
                # Queue/store/milestone history changes far more slowly than
                # worker liveness. Keep deep repository claims unknown while
                # publishing the machine sample's exact active-lease count.
                "session": _fast_session_row(machine_status, root),
                "milestone": _milestone_row({}),
                "workers": worker_rows,
            }
        )

    # Records with no usable project root remain visible without inventing a
    # controller/project association.  Object identity is stable across the
    # shallow scope_payload filtering used above.
    unassigned_records, _unassigned_excluded = _fast_plane_records(
        [record for record in records if id(record) not in rooted_dispatches],
        sampled_at=sampled_at,
    )
    unassigned = _sort_worker_rows(
        [
            _worker_row(record, sampled_at=sampled_at)
            for record in unassigned_records
        ]
    )
    projects.sort(
        key=lambda project: (
            _worker_sort_key(project["workers"][0])
            if project["workers"]
            else (True, True, 0.0, "", "", "", ""),
            str(project.get("project_id") or ""),
        )
    )
    return projects, unassigned, hidden_history_excluded, controllers


def build_fleet_plane(
    *,
    fleet_dir: Path | None = None,
    readers_dir: Path | None = None,
    usage_timeout_s: float = goalflight_usage.DEFAULT_TIMEOUT_S,
    generation_id: str | None = None,
    cadence_seconds: int | None = None,
) -> dict[str, Any]:
    """Build one machine-wide fleet sample, then group it by registered project."""
    started_at = _utc_now()
    sampled_at = _parse_timestamp(started_at)
    errors: list[str] = []

    empty_machine_status = {
            "capacity": {},
            "capacity_state": {"leases": {}},
            "rate_pressure": {},
            "dispatch": {"records": []},
            "warnings": [],
        }
    resolved_fleet_dir = fleet_dir or goalflight_messages.default_fleet_dir()
    usage_kwargs: dict[str, Any] = {"timeout_s": usage_timeout_s}
    if readers_dir is not None:
        usage_kwargs["readers_dir"] = readers_dir
    # These sources share no mutable transaction and dominate live wall time
    # (status and usage each walk runs.d). Read them concurrently, then consume
    # results in a fixed order so captured errors and the payload stay stable.
    source_specs: list[tuple[str, Callable[[], Any], Any]] = [
        (
            "local_status",
            lambda: goalflight_status.status_payload(
                reconcile_terminal_history=False
            ),
            empty_machine_status,
        ),
        (
            "remote",
            lambda: goalflight_fleet_status_cli.build_fleet_status(
                resolved_fleet_dir,
                live_only=True,
            ),
            {},
        ),
        ("usage", lambda: goalflight_usage.collect_usage(**usage_kwargs), []),
        ("projects", goalflight_task.read_project_registry, []),
    ]
    source_values: dict[str, Any] = {}
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=len(source_specs),
        thread_name_prefix="fleet-console-source",
    ) as pool:
        futures = {
            source: (pool.submit(producer), fallback)
            for source, producer, fallback in source_specs
        }
        for source, _producer, _fallback in source_specs:
            future, fallback = futures[source]
            try:
                source_values[source] = future.result()
            except Exception as exc:  # source failures remain projection data
                errors.append(_safe_error(source, exc))
                source_values[source] = fallback
    machine_status = source_values["local_status"]
    remote_status = source_values["remote"]
    usage_rows = source_values["usage"]
    registered = source_values["projects"]

    queue_by_root, queue_total = _queue_summary(machine_status)
    fast_roots = _fast_project_roots(
        machine_status,
        queue_by_root=queue_by_root,
    )
    sampled_projects, registry_total, registry_omitted = _registered_projects(
        registered,
        active_roots=fast_roots,
        only_roots=fast_roots,
        limit=None,
    )
    projects, unassigned, hidden_history_excluded, controllers = _project_rows(
        machine_status if isinstance(machine_status, dict) else {},
        sampled_projects,
        errors,
        all_registered_roots={item["root"] for item in sampled_projects},
        sampled_at=sampled_at,
        visible_roots=fast_roots,
    )
    seen_roots = set(fast_roots)
    for supplied in _active_controller_roots_from_journals():
        root = _canonical_root(supplied)
        if root is None or root in seen_roots:
            continue
        seen_roots.add(root)
        journal_reader = _project_journal_reader(Path(root))
        extra_contexts = _controller_contexts_by_session(
            Path(root),
            None,
            include_all=True,
            authority=journal_reader,
            open_if_missing=False,
        )
        controllers.extend(
            _controller_panel_row(root, context)
            for context in extra_contexts.values()
            if context.get("label")
        )
    remote = _remote_row(remote_status, sampled_at=sampled_at)
    controllers = _aggregate_controller_rows(
        controllers,
        _owned_live_counts(
            projects,
            unassigned,
            list((remote.get("workers") or [])),
        ),
    )
    # Attach queue depth by the project's own root. Every row gets the key --
    # the allowlist requires it, and an absent queue must read as depth 0, not
    # as a missing field a renderer has to guess about.
    _attach_queue_rows(projects, queue_by_root)
    # Only project history has a disclosure/fetch path in the renderer. Remote
    # and unassigned retention remains bounded, but its omitted rows must not
    # inflate the global '+N in history' claim into unreachable inventory.
    history_excluded = hidden_history_excluded + sum(
        max(0, int(project.get("history_excluded") or 0))
        for project in projects
    )
    finished_at = _utc_now()
    payload = {
        "schema": FLEET_SCHEMA,
        **_metadata(
            "fleet",
            generation_id=_generation_id("fleet", generation_id),
            started_at=started_at,
            finished_at=finished_at,
            errors=errors,
            cadence_seconds=cadence_seconds,
        ),
        # These count the REGISTRY pass, not len(projects): a project with a
        # live dispatch record gets a row even when it is outside the deep
        # sample, so projects[] is legitimately larger. Naming them
        # projects_* invited reading 24 as "you have 24 projects" while 283
        # were listed.
        "registry_total": registry_total,
        "registry_deep_sampled": len(sampled_projects),
        "registry_unsampled": len(registry_omitted),
        # Bound the named omissions the same way attention bounds unprobed
        # generations: a count plus a recency-ordered head, never the full
        # 1,900-row registry dumped into the short-poll mirror.
        "registry_unsampled_projects": registry_omitted[:DEFAULT_MAX_PROJECTS],
        "history_excluded": history_excluded,
        "worker_age_filter": dict(WORKER_AGE_FILTER_POLICY),
        "machine": {
            **_machine_row(machine_status if isinstance(machine_status, dict) else {}),
            "queue_depth": queue_total,
        },
        "vendors": _vendor_rows(usage_rows),
        "remote": remote,
        "projects": projects,
        "controllers": controllers,
        "unassigned_workers": unassigned,
    }
    validate_projection(payload, "fleet")
    return payload


def _attention_kind(value: object) -> str | None:
    """The attention kind, or None when this is not operator attention.

    This used to coerce every unrecognised type to "user_need". The mail
    summary hands over types that are ALREADY correctly self-describing --
    done-suggest, resume-ready, parallel-ready are task-store automation, a
    controller prompting itself ("worker says done: b-663 -> review?", "276
    tasks ready -> continue?"). The fallback relabelled all of them as a
    pending human decision: 137 of 202 rows on this machine. The producer
    said "automation" and the console overrode it, which is this project's
    signature defect -- a field asserting a state it never measured --
    sitting in a fallback branch, with the right answer already in hand.

    Closed by construction rather than by a deny-list of known automation
    kinds: a deny-list has to be updated every time a producer adds a kind,
    and the failure mode of forgetting is silent laundering all over again.
    _ATTENTION_KINDS is the positive authority, matching how this module
    treats every other boundary.
    """
    kind = str(value or "").strip().lower().replace("-", "_")
    if kind in _ATTENTION_KINDS:
        return kind
    # Controller-addressed mail is operator attention too, and it is the
    # channel a human actually writes on. A question needs an answer, so it
    # is a need; the rest are informational. Mapped onto the existing kinds
    # rather than inventing new ones, so the renderer and the allowlist stay
    # closed.
    if kind == "controller_question":
        return "user_need"
    if kind in {"controller_answer", "controller_notice", "coordination", "notice"}:
        return "advisory"
    return None


def _attention_rows(summary: object) -> list[dict[str, Any]]:
    data = summary if isinstance(summary, dict) else {}
    rows = []
    for item in data.get("needs") or []:
        if not isinstance(item, dict):
            continue
        observed_at = _iso_timestamp(item.get("ts"))
        # Anything not recognised as operator attention is dropped, not
        # promoted. Doubly so here: the fleet view is unscoped
        # (task_store_project_root=None), so _filter_task_store_nudges()
        # no-ops and a laundered nudge could never even be retired once its
        # tasks were done.
        kind = _attention_kind(item.get("type"))
        if kind is None:
            continue
        rows.append(
            {
                "dispatch_id": _display(item.get("dispatch_id"), limit=128),
                "seq": int(item["seq"]) if isinstance(item.get("seq"), int) else None,
                "kind": kind,
                "action": "review",
                "observed_at": observed_at,
                # Bounded, path-redacted mail display text; the raw body and raw
                # marker payload never cross the allowlist.
                "headline": _display(item.get("text"), limit=96),
            }
        )
    rows.sort(key=lambda row: (row["observed_at"] is None, row["observed_at"] or "", row["dispatch_id"] or ""))
    return rows


def _controller_attention_rows(
    project_roots: list[Path],
    machine_status: dict[str, Any],
    *,
    probe_metadata: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """Project only HUNG controllers into operator attention."""
    rows: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    seen_roots: set[str] = set()
    for supplied_root in project_roots:
        root = _canonical_root(str(supplied_root))
        if root is None or root in seen_roots:
            continue
        seen_roots.add(root)
        scoped = goalflight_status.scope_payload(machine_status, root)
        dispatch = scoped.get("dispatch") if isinstance(scoped, dict) else {}
        records = (
            [item for item in dispatch.get("records") or [] if isinstance(item, dict)]
            if isinstance(dispatch, dict)
            else []
        )
        journal_reader = _project_journal_reader(Path(root))
        contexts = _controller_contexts_by_session(
            Path(root),
            records,
            include_all=True,
            include_locked_ended=True,
            probe_metadata=probe_metadata,
            authority=journal_reader,
            open_if_missing=False,
        )
        for session_id, context in sorted(contexts.items()):
            if context.get("liveness_state") != "HUNG":
                continue
            raw_label_value = context.get("label")
            raw_label = (
                raw_label_value.strip()
                if isinstance(raw_label_value, str) and raw_label_value.strip()
                else ""
            )
            display_label = _display(raw_label, limit=64)
            if display_label is None:
                display_label = _controller_session_digest(session_id) or "unknown"
            generation = context.get("generation")
            generation_text = (
                f" generation {generation}" if isinstance(generation, int) else ""
            )
            candidates.append(
                {
                    "root": root,
                    "session_id": session_id,
                    "context": context,
                    "raw_label": raw_label,
                    "display_label": display_label,
                    "generation": generation,
                    "generation_text": generation_text,
                }
            )

    supervisor_states = (
        goalflight_wake.supervisor_generation_states(
            (
                (
                    candidate["root"],
                    candidate["raw_label"],
                    candidate["session_id"],
                )
                for candidate in candidates
            ),
            process_timeout_s=HUNG_SUPERVISOR_PROBE_TIMEOUT_S,
        )
        if candidates
        else []
    )
    for candidate, supervisor in zip(candidates, supervisor_states):
        root = str(candidate["root"])
        context = candidate["context"]
        raw_label = str(candidate["raw_label"])
        display_label = str(candidate["display_label"])
        generation = candidate["generation"]
        generation_text = str(candidate["generation_text"])
        component_command = goalflight_wake.listener_start_command(
            root,
            controller_label=raw_label,
        )
        action_policy = goalflight_wake.supervisor_operator_action(
            supervisor,
            component_command=component_command,
        )
        rows.append(
            {
                "dispatch_id": _display(
                    f"{_project_id(root)}:controller:"
                    f"{display_label}:generation-{generation}",
                    limit=128,
                ),
                "seq": None,
                "kind": "controller_hung",
                "action": (
                    action_policy["command"]
                    or action_policy["instruction"]
                ),
                "observed_at": _iso_timestamp(context.get("last_seen")),
                "headline": _display(
                    f"Controller {display_label}{generation_text} is HUNG: "
                    "in-flight work has no live wake waiter",
                    limit=96,
                ),
            }
        )
    # Same key as mail rows and the merged attention plane: dated first,
    # then by observed_at, then dispatch_id. Leaving this on dispatch_id
    # alone would re-sort HUNG rows away from their timestamps the moment
    # a caller used this list without the merge sort below.
    rows.sort(
        key=lambda row: (
            row["observed_at"] is None,
            row["observed_at"] or "",
            row["dispatch_id"] or "",
        )
    )
    return rows


def build_attention_plane(
    *,
    messages_dir: Path | None = None,
    fleet_dir: Path | None = None,
    generation_id: str | None = None,
    project_roots: list[Path] | None = None,
    cadence_seconds: int | None = None,
) -> dict[str, Any]:
    """Build the fast attention sample from mail and HUNG controller facts."""
    started_at = _utc_now()
    errors: list[str] = []
    kwargs: dict[str, Any] = {
        "owned_dispatch_ids": None,
        "task_store_project_root": None,
    }
    if messages_dir is not None:
        kwargs["messages_dir"] = messages_dir
    if fleet_dir is not None:
        kwargs["fleet_dir"] = fleet_dir
    empty_machine_status = {
        "capacity_state": {"leases": {}},
        "dispatch": {"records": []},
        "rate_pressure": {},
    }
    summary = _capture(
        "mail",
        errors,
        lambda: goalflight_messages.controller_mail_summary(**kwargs),
        {},
    )
    machine_status = (
        _capture(
            "local_status",
            errors,
            lambda: goalflight_status.status_payload(
                reconcile_terminal_history=False
            ),
            empty_machine_status,
        )
        if project_roots is None or project_roots
        else empty_machine_status
    )
    machine_status = _without_terminal_history(machine_status)
    if project_roots is None:
        # Machine status already names every root with live work or an ACTIVE
        # capacity lease. Do not re-enumerate the permanent 1,954-row project
        # registry on the fast attention plane.
        resolved_project_roots = [
            Path(root)
            for root in sorted(_fast_project_roots(machine_status))[
                :DEFAULT_MAX_PROJECTS
            ]
        ]
    else:
        resolved_project_roots = list(project_roots)
    controller_probe_metadata = {"controller_history_probes_truncated": 0}
    items = _attention_rows(summary) + _controller_attention_rows(
        resolved_project_roots,
        machine_status if isinstance(machine_status, dict) else {},
        probe_metadata=controller_probe_metadata,
    )
    items.sort(
        key=lambda row: (
            row["observed_at"] is None,
            row["observed_at"] or "",
            row["dispatch_id"] or "",
        )
    )
    finished_at = _utc_now()
    payload = {
        "schema": ATTENTION_SCHEMA,
        **_metadata(
            "attention",
            generation_id=_generation_id("attention", generation_id),
            started_at=started_at,
            finished_at=finished_at,
            errors=errors,
            cadence_seconds=cadence_seconds,
        ),
        **controller_probe_metadata,
        # Renderers derive minute buckets from observed_at at display time.  No
        # marker timestamp and no sample-time age is substituted.
        "age_granularity": "minute",
        "items": items,
    }
    validate_projection(payload, "attention")
    return payload


def build_degraded_plane(
    plane: str,
    *,
    error: str,
    started_at: str | None = None,
    generation_id: str | None = None,
    cadence_seconds: int | None = None,
) -> dict[str, Any]:
    """Build an empty, schema-valid sample for a whole-tick producer failure.

    Source-level failures normally flow through ``_capture``.  A wall-clock
    budget is different: the timed-out sampler is stopped out-of-process, so it
    cannot finish its own payload.  This keeps that stop on the same DEGRADED
    contract instead of leaving an old mirror behind or inventing another
    renderer state.

    A timed-out fleet sample reports zero deep samples and an unknown registry
    total.  That is deliberately not ``0 / 0``: the producer did not finish the
    registry pass, so claiming an empty fleet would turn "I did not look" into
    "nothing exists".
    """
    if plane not in SCRIPT_GLOBALS:
        raise ValueError(f"unknown plane: {plane}")
    began = started_at or _utc_now()
    finished_at = _utc_now()
    bounded_error = _display(error, limit=96) or "producer:RuntimeError"
    metadata = _metadata(
        plane,
        generation_id=_generation_id(plane, generation_id),
        started_at=began,
        finished_at=finished_at,
        errors=[bounded_error],
        cadence_seconds=cadence_seconds,
    )
    if plane == "attention":
        payload = {
            "schema": ATTENTION_SCHEMA,
            **metadata,
            "controller_history_probes_truncated": None,
            "age_granularity": "minute",
            "items": [],
        }
    else:
        payload = {
            "schema": FLEET_SCHEMA,
            **metadata,
            "registry_total": None,
            "registry_deep_sampled": 0,
            "registry_unsampled": None,
            "registry_unsampled_projects": [],
            "history_excluded": None,
            # The age-filter policy is a property of this build, not of the
            # tick, so a degraded sample still declares it. Omitting it made a
            # budget timeout fail its own schema validation and raise instead of
            # publishing the DEGRADED payload the budget exists to produce --
            # the timeout path would have crashed the producer rather than
            # reporting that it ran out of time.
            "worker_age_filter": dict(WORKER_AGE_FILTER_POLICY),
            "machine": {**_machine_row({}), "queue_depth": None},
            "vendors": [],
            "remote": _remote_row({}),
            "projects": [],
            "controllers": [],
            "unassigned_workers": [],
        }
    validate_projection(payload, plane)
    return payload


def _backfill_projection_fields(payload: dict[str, Any], plane: str) -> None:
    """Fill fields added after a prior sample was published.

    A retained last-good payload may predate ``incomplete`` / the unsampled
    registry list. The allowlist requires every key, so a timeout overlay
    cannot republish the old shape verbatim.
    """
    payload.setdefault("incomplete", False)
    if plane == "fleet":
        payload.setdefault("registry_unsampled", None)
        payload.setdefault("registry_unsampled_projects", [])
        for row in payload.get("controllers") or []:
            if isinstance(row, dict):
                row.setdefault("last_error", None)
                row.setdefault("probe_command", None)


def _is_retainable_sample(payload: dict[str, Any], plane: str) -> bool:
    """True when this payload is a last-good sample worth keeping on failure."""
    expected = FLEET_SCHEMA if plane == "fleet" else ATTENTION_SCHEMA
    return (
        isinstance(payload, dict)
        and payload.get("schema") == expected
        and payload.get("last_success_at") is not None
    )


def retain_or_degrade(
    plane: str,
    *,
    prior: dict[str, Any] | None,
    error: str,
    started_at: str | None = None,
    generation_id: str | None = None,
    cadence_seconds: int | None = None,
) -> dict[str, Any]:
    """Keep last-good rows on a failed tick; otherwise publish empty DEGRADED.

    A timeout used to atomically replace the previous sample with emptiness.
    The operator then saw an empty screen plus an error, instead of the last
    real picture marked incomplete. last_success_at stays the prior success
    so freshness still names when the data was good; last_error and
    incomplete mark this publication as a failed tick.
    """
    if prior is not None and _is_retainable_sample(prior, plane):
        payload = json.loads(json.dumps(prior))
        _backfill_projection_fields(payload, plane)
        bounded_error = _display(error, limit=96) or "producer:RuntimeError"
        payload["last_error"] = f"{bounded_error} · {_operator_action(plane)}"
        payload["incomplete"] = True
        payload["generation_id"] = _generation_id(plane, generation_id)
        if cadence_seconds is not None:
            payload["cadence_seconds"] = _current_cadence_seconds(plane, cadence_seconds)
        validate_projection(payload, plane)
        return payload
    return build_degraded_plane(
        plane,
        error=error,
        started_at=started_at,
        generation_id=generation_id,
        cadence_seconds=cadence_seconds,
    )


def publish_plane(path: str | Path, payload: dict[str, Any], plane: str) -> Path:
    """Validate and atomically publish one independently generated plane."""
    validate_projection(payload, plane)
    target = Path(path).expanduser().resolve()
    goalflight_status.write_script_data_js(
        target,
        payload,
        global_name=SCRIPT_GLOBALS[plane],
    )
    return target


def sample_exit_code(payload: dict[str, Any], plane: str) -> int:
    """Report and return the shared healthy/degraded producer exit contract."""
    # A retained last-good sample keeps last_success_at so the renderer can
    # age the data. That stamp is no longer "this tick succeeded": last_error
    # or incomplete means the tick failed even when older rows remain.
    if (
        payload.get("last_error")
        or payload.get("incomplete")
        or payload.get("last_success_at") is None
    ):
        print(
            f"fleet-console {plane} sample DEGRADED: "
            f"{payload.get('last_error') or 'one or more sources failed'}",
            file=sys.stderr,
        )
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Goal Flight fleet-console projection producer")
    subparsers = parser.add_subparsers(dest="plane", required=True)

    fleet = subparsers.add_parser("fleet", help="sample local/remote fleet, usage, and projects")
    fleet.add_argument("--fleet-dir", type=Path)
    fleet.add_argument("--readers-dir", type=Path)
    fleet.add_argument("--usage-timeout-s", type=float, default=goalflight_usage.DEFAULT_TIMEOUT_S)
    fleet.add_argument("--cadence-seconds", type=int)
    fleet.add_argument("--generation-id")
    fleet.add_argument("--output", type=Path, help="atomic GF_FLEET script output; JSON stdout when omitted")

    attention = subparsers.add_parser("attention", help="sample timestamped operator attention mail")
    attention.add_argument("--messages-dir", type=Path)
    attention.add_argument("--fleet-dir", type=Path)
    attention.add_argument("--cadence-seconds", type=int)
    attention.add_argument("--generation-id")
    attention.add_argument("--output", type=Path, help="atomic GF_ATTENTION script output; JSON stdout when omitted")

    probe = subparsers.add_parser(
        "probe-holder",
        help="read-only lease-holder lock probe; prints True, False, or None",
    )
    probe.add_argument("--label", required=True)
    probe.add_argument("--generation", type=int)
    probe.add_argument("--project-root", type=Path, default=Path("."))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.plane == "probe-holder":
        print(
            probe_holder_lock(
                args.project_root,
                controller_label=args.label,
                generation=args.generation,
            )
        )
        return 0
    if args.plane == "fleet":
        payload = build_fleet_plane(
            fleet_dir=args.fleet_dir,
            readers_dir=args.readers_dir,
            usage_timeout_s=args.usage_timeout_s,
            generation_id=args.generation_id,
            cadence_seconds=args.cadence_seconds,
        )
    else:
        payload = build_attention_plane(
            messages_dir=args.messages_dir,
            fleet_dir=args.fleet_dir,
            generation_id=args.generation_id,
            cadence_seconds=args.cadence_seconds,
        )
    if args.output is not None:
        # A missing parent directory is a first-run footgun, not a reason to
        # crash without a payload: create it rather than leaving the operator
        # with a traceback and no mirror.
        Path(args.output).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        publish_plane(args.output, payload, args.plane)
    else:
        print(json.dumps(payload, indent=2, sort_keys=True))

    # A DEGRADED sample must not exit 0. Source failures are captured as data
    # so a partial payload still publishes -- that is deliberate, and it is
    # also exactly how a scheduler learns nothing went wrong when it did. A
    # sample whose sources all failed produces zeros and empty lists, which a
    # page renders as a calm, healthy fleet.
    #
    # last_success_at is None precisely when at least one source failed, so it
    # is the honest signal. Exit 1 there, and say why on stderr, so a cron or
    # drain tick and a human both see the same verdict.
    return sample_exit_code(payload, args.plane)


if __name__ == "__main__":
    raise SystemExit(main())
