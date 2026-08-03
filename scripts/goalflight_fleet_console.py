#!/usr/bin/env python3
"""Backend-only, shareable projections for the Goal Flight fleet console.

This module is a consumer of the existing status authorities.  It does not
read dispatch ledgers, status sidecars, tails, marker files, or process tables,
and it deliberately does not classify workers.  Fleet and attention samples
are independent so a fast mailbox refresh never pretends to refresh worker
liveness.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import math
import re
import sys
import uuid
from pathlib import Path
from typing import Any, Callable

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(ROOT))

import goalflight_dispatch_states
import goalflight_fleet_status_cli
import goalflight_messages
import goalflight_session_status
import goalflight_status
import goalflight_task
import goalflight_usage


FLEET_SCHEMA = "goalflight.fleet-console.fleet.v2"
ATTENTION_SCHEMA = "goalflight.fleet-console.attention.v1"
PRODUCER_NAME = "goalflight_fleet_console.py"
SCRIPT_GLOBALS = {"fleet": "GF_FLEET", "attention": "GF_ATTENTION"}

# Head of the recency-ordered project registry to sample per tick. See
# _registered_projects for the measurement that sets this: the per-project
# cost is ~1.0s, and the drain tick this rides is ~60s, so the whole
# per-project pass has to fit in a fraction of that alongside everything else.
DEFAULT_MAX_PROJECTS = 12

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
    "registry_total": None,
    "registry_deep_sampled": None,
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
                "controller_session_id": None,
                "controller_pid": None,
                "controller_label": None,
                "controller_display": None,
                "controller_state": None,
                "age_filter_match": None,
                "age_filter_reason": None,
                "observed_live": None,
                "observed_live_source": None,
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
                    "controller_session_id": None,
                    "controller_pid": None,
                    "controller_label": None,
                    "controller_display": None,
                    "controller_state": None,
                    "age_filter_match": None,
                    "age_filter_reason": None,
                    "observed_live": None,
                    "observed_live_source": None,
                }
            ],
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
            "controller_session_id": None,
            "controller_pid": None,
            "controller_label": None,
            "controller_display": None,
            "controller_state": None,
            "age_filter_match": None,
            "age_filter_reason": None,
            "observed_live": None,
            "observed_live_source": None,
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


def _validate_no_absolute_paths(value: Any, *, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            _validate_no_absolute_paths(item, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _validate_no_absolute_paths(item, path=f"{path}[{index}]")
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


def _safe_error(source: str, exc: BaseException) -> str:
    return f"{source}:{type(exc).__name__}"


def _generation_id(plane: str, supplied: str | None) -> str:
    value = supplied or f"{plane}-{uuid.uuid4()}"
    return _display(value, limit=128) or f"{plane}-unknown"


def _metadata(
    plane: str,
    *,
    generation_id: str,
    started_at: str,
    finished_at: str,
    errors: list[str],
) -> dict[str, Any]:
    return {
        "generation_id": generation_id,
        "sample_started_at": started_at,
        "sample_finished_at": finished_at,
        "last_success_at": finished_at if not errors else None,
        "producer": {"name": PRODUCER_NAME, "plane": plane},
        "last_error": errors[-1] if errors else None,
    }


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


def _worker_display_verdict(record: dict[str, Any]) -> dict[str, Any]:
    """Resolve one presentation verdict from the reconciled authority fields."""
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

    if conflict:
        return {
            "display_state": "unknown",
            "is_terminal": None,
            "classification_conflict": True,
        }
    if terminal_values:
        normalized = goalflight_dispatch_states.normalize_dispatch_state(terminal_values[0])
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


def _controller_labels_by_session(
    project_root: Path,
    records: list[dict[str, Any]],
) -> dict[str, str]:
    """Return only unambiguous beacon labels for stamped controller sessions."""
    wanted = {
        str(record.get("controller_session_id"))
        for record in records
        if record.get("controller_session_id")
    }
    if not wanted:
        return {}
    session_map = goalflight_session_status._read_session_map(  # noqa: SLF001
        goalflight_session_status._session_file(project_root)  # noqa: SLF001
    )
    labels: dict[str, set[str]] = {}
    for session in session_map.values():
        session_id = _display(session.get("id"), limit=128)
        label = _display(session.get("label"), limit=64)
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
) -> dict[str, Any]:
    session_id = _display(record.get("controller_session_id"), limit=128)
    raw_pid = record.get("controller_pid")
    controller_pid = raw_pid if isinstance(raw_pid, int) and not isinstance(raw_pid, bool) and raw_pid > 0 else None
    label = controller_labels.get(session_id) if session_id else None
    if label:
        display, state = label, "label"
    elif session_id:
        display, state = session_id, "session"
    elif controller_pid is not None:
        display, state = "owned · identity unknown", "owned_unknown"
    else:
        display, state = "unowned", "unowned"
    return {
        "controller_session_id": session_id,
        "controller_pid": controller_pid,
        "controller_label": label,
        "controller_display": display,
        "controller_state": state,
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
) -> dict[str, Any]:
    alive = record.get("worker_still_alive")
    if isinstance(alive, bool):
        return {"observed_live": alive, "observed_live_source": "identity_recheck"}
    status = goalflight_status._status_json_payload(record)  # noqa: SLF001
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
) -> dict[str, Any]:
    # Raw authority fields remain available for diagnosis, while renderers use
    # only the canonical verdict below for filtering and presentation.
    alive = record.get("worker_still_alive")
    started_at = _iso_timestamp(record.get("started_at"))
    verdict = _worker_display_verdict(record)
    observed_live = _worker_observed_live_fields(record, sampled_at=sampled_at)
    return {
        "dispatch_id": _display(record.get("dispatch_id"), limit=128),
        "node_id": _display(node_id, limit=96),
        "agent": _display(record.get("agent"), limit=64),
        "engine": _display(record.get("engine"), limit=64),
        "shape": _display(record.get("shape"), limit=64),
        "transport": _display(record.get("transport"), limit=64),
        "os_sandbox": _display(record.get("os_sandbox"), limit=32),
        "state": _display(record.get("state"), limit=64),
        "classification": _display(record.get("classification"), limit=64),
        "terminal_state": _display(record.get("terminal_state"), limit=64),
        "liveness_state": _display(record.get("liveness_state"), limit=64),
        "worker_alive": alive if isinstance(alive, bool) else None,
        "started_at": started_at,
        "ended_at": _iso_timestamp(record.get("ended_at")),
        **verdict,
        **_controller_fields(record, controller_labels or {}),
        **observed_live,
        **_worker_age_filter_fields(
            started_at=started_at,
            is_terminal=verdict["is_terminal"],
            sampled_at=sampled_at,
            observed_live=observed_live["observed_live"],
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
    workers = []
    for row in data.get("dispatches") or []:
        if not isinstance(row, dict):
            continue
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
        "active_leases": _number(data.get("active_leases_in_project")),
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
    """True when a dispatch record is finished by ANY of its own accounts.

    Checks classification and terminal_state as well as state. A record can read
    state="running" while its classification says worker_dead; trusting the
    state string alone let a dead root win scarce deep-sample priority over a
    live one.
    """
    if not isinstance(record, dict):
        return True
    for key in ("state", "terminal_state", "classification"):
        if goalflight_dispatch_states.is_terminal_state(record.get(key)):
            return True
    return False


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
    limit: int = DEFAULT_MAX_PROJECTS,
) -> tuple[list[dict[str, Any]], int]:
    """Return (sampled projects, total registered).

    Sampling every registered project is not viable. The registry accumulates a
    root for every project any dispatch ever ran in, including throwaway
    per-ticket worktrees. Measured on this machine: 1433 registered roots at
    ~1.0s each (one session_status plus one milestone call), so a full serial
    pass costs ~1433s against a ~60s drain tick. That 24x overrun is why this
    projection hung and emitted nothing at all.

    Bounded by RECENCY, because the operator cares about projects with current
    activity and the registry already records ``last_seen``. ISO-8601 sorts
    lexicographically, so newest-first is a plain reverse string sort; entries
    with no timestamp sort last rather than being dropped, since a missing
    timestamp is unknown recency, not proven staleness.

    The total is returned alongside so the payload can say how many were NOT
    sampled. A silent cap would read as "these are all your projects" -- the
    field-asserting-an-unmeasured-state failure this projection exists to
    avoid.
    """
    rows = payload if isinstance(payload, list) else []
    result = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        root = _canonical_root(row.get("project_root"))
        if root is None or root in seen:
            continue
        seen.add(root)
        result.append(
            {
                "root": root,
                "last_seen": _iso_timestamp(row.get("last_seen")),
                "skill_version": _display(row.get("skill_version"), limit=32),
            }
        )
    total = len(result)
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
        result = result[:limit]
    return result, total


def _project_rows(
    machine_status: dict[str, Any],
    registered_projects: list[dict[str, Any]],
    errors: list[str],
    all_registered_roots: set[str] | None = None,
    sampled_at: dt.datetime | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Rows for every project with a record, deep-sampling only the head.

    ``registered_projects`` is the CAPPED sample; ``all_registered_roots`` is the
    whole registry. They are different questions and were previously the same
    set: "registered" answered from the sample, so the 13th registered project
    was emitted as registered=false purely because this tick did not sample it.
    Whether a project is registered has nothing to do with which head we chose.
    """
    dispatch = machine_status.get("dispatch")
    dispatch = dispatch if isinstance(dispatch, dict) else {}
    records = [item for item in dispatch.get("records") or [] if isinstance(item, dict)]
    by_root = {item["root"]: item for item in registered_projects}
    for record in records:
        root = _canonical_root(record.get("project_root"))
        if root is not None:
            by_root.setdefault(root, {"root": root, "last_seen": None, "skill_version": None})

    projects = []
    assigned_dispatches: set[int] = set()
    # Deep-sample membership: who gets session/milestone calls this tick.
    sampled_roots = {item["root"] for item in registered_projects}
    # Registry membership: a fact about the project, independent of sampling.
    registered_roots = set(all_registered_roots) if all_registered_roots is not None else sampled_roots
    for root in sorted(by_root):
        metadata = by_root[root]
        scoped = goalflight_status.scope_payload(machine_status, root)
        scoped_dispatch = scoped.get("dispatch") if isinstance(scoped, dict) else {}
        scoped_records = (
            [item for item in scoped_dispatch.get("records") or [] if isinstance(item, dict)]
            if isinstance(scoped_dispatch, dict)
            else []
        )
        for record in scoped_records:
            assigned_dispatches.add(id(record))

        if root in sampled_roots:
            session = _capture(
                "session",
                errors,
                lambda root=root: goalflight_session_status.aggregate_status(Path(root)),
                {},
            )
            milestone = _capture(
                "milestone",
                errors,
                lambda root=root: goalflight_status.milestone_status_payload(root),
                {},
            )
        else:
            session = {}
            milestone = {}

        controller_labels = _controller_labels_by_session(Path(root), scoped_records)
        worker_rows = _sort_worker_rows(
            [
                _worker_row(
                    record,
                    sampled_at=sampled_at,
                    controller_labels=controller_labels,
                )
                for record in scoped_records
            ]
        )
        projects.append(
            {
                "project_id": _project_id(root),
                "name": _display(Path(root).name or "project", limit=64),
                "registered": root in registered_roots,
                "last_seen": metadata.get("last_seen"),
                "skill_version": metadata.get("skill_version"),
                "session": _session_row(session),
                "milestone": _milestone_row(milestone),
                "workers": worker_rows,
            }
        )

    # Records with no usable project root remain visible without inventing a
    # controller/project association.  Object identity is stable across the
    # shallow scope_payload filtering used above.
    unassigned = _sort_worker_rows(
        [
            _worker_row(record, sampled_at=sampled_at)
            for record in records
            if id(record) not in assigned_dispatches
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
    return projects, unassigned


def build_fleet_plane(
    *,
    fleet_dir: Path | None = None,
    readers_dir: Path | None = None,
    usage_timeout_s: float = goalflight_usage.DEFAULT_TIMEOUT_S,
    generation_id: str | None = None,
) -> dict[str, Any]:
    """Build one machine-wide fleet sample, then group it by registered project."""
    started_at = _utc_now()
    sampled_at = _parse_timestamp(started_at)
    errors: list[str] = []

    # Machine-wide facts first.  The unscoped local authority is invoked once,
    # and its reconciled records are only filtered afterward via scope_payload.
    machine_status = _capture(
        "local_status",
        errors,
        goalflight_status.status_payload,
        {
            "capacity": {},
            "capacity_state": {"leases": {}},
            "rate_pressure": {},
            "dispatch": {"records": []},
            "warnings": [],
        },
    )
    resolved_fleet_dir = fleet_dir or goalflight_messages.default_fleet_dir()
    remote_status = _capture(
        "remote",
        errors,
        lambda: goalflight_fleet_status_cli.build_fleet_status(resolved_fleet_dir),
        {},
    )
    usage_kwargs: dict[str, Any] = {"timeout_s": usage_timeout_s}
    if readers_dir is not None:
        usage_kwargs["readers_dir"] = readers_dir
    usage_rows = _capture(
        "usage",
        errors,
        lambda: goalflight_usage.collect_usage(**usage_kwargs),
        [],
    )
    registered = _capture(
        "projects",
        errors,
        goalflight_task.read_project_registry,
        [],
    )

    queue_by_root, queue_total = _queue_summary(machine_status)
    sampled_projects, registry_total = _registered_projects(
        registered,
        active_roots=_roots_with_records(machine_status),
    )
    projects, unassigned = _project_rows(
        machine_status if isinstance(machine_status, dict) else {},
        sampled_projects,
        errors,
        all_registered_roots=_all_registered_roots(registered),
        sampled_at=sampled_at,
    )
    # Attach queue depth by the project's own root. Every row gets the key --
    # the allowlist requires it, and an absent queue must read as depth 0, not
    # as a missing field a renderer has to guess about.
    _attach_queue_rows(projects, queue_by_root)
    finished_at = _utc_now()
    payload = {
        "schema": FLEET_SCHEMA,
        **_metadata(
            "fleet",
            generation_id=_generation_id("fleet", generation_id),
            started_at=started_at,
            finished_at=finished_at,
            errors=errors,
        ),
        # These count the REGISTRY pass, not len(projects): a project with a
        # live dispatch record gets a row even when it is outside the deep
        # sample, so projects[] is legitimately larger. Naming them
        # projects_* invited reading 24 as "you have 24 projects" while 283
        # were listed.
        "registry_total": registry_total,
        "registry_deep_sampled": len(sampled_projects),
        "worker_age_filter": dict(WORKER_AGE_FILTER_POLICY),
        "machine": {
            **_machine_row(machine_status if isinstance(machine_status, dict) else {}),
            "queue_depth": queue_total,
        },
        "vendors": _vendor_rows(usage_rows),
        "remote": _remote_row(remote_status, sampled_at=sampled_at),
        "projects": projects,
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


def build_attention_plane(
    *,
    messages_dir: Path | None = None,
    fleet_dir: Path | None = None,
    generation_id: str | None = None,
) -> dict[str, Any]:
    """Build the fast attention sample from timestamped mail envelopes only."""
    started_at = _utc_now()
    errors: list[str] = []
    kwargs: dict[str, Any] = {
        "owned_dispatch_ids": None,
        "task_store_project_root": None,
        "unread_only": True,
    }
    if messages_dir is not None:
        kwargs["messages_dir"] = messages_dir
    if fleet_dir is not None:
        kwargs["fleet_dir"] = fleet_dir
    summary = _capture(
        "mail",
        errors,
        lambda: goalflight_messages.controller_mail_summary(**kwargs),
        {},
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
        ),
        # Renderers derive minute buckets from observed_at at display time.  No
        # marker timestamp and no sample-time age is substituted.
        "age_granularity": "minute",
        "items": _attention_rows(summary),
    }
    validate_projection(payload, "attention")
    return payload


def build_degraded_plane(
    plane: str,
    *,
    error: str,
    started_at: str | None = None,
    generation_id: str | None = None,
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
    )
    if plane == "attention":
        payload = {
            "schema": ATTENTION_SCHEMA,
            **metadata,
            "age_granularity": "minute",
            "items": [],
        }
    else:
        payload = {
            "schema": FLEET_SCHEMA,
            **metadata,
            "registry_total": None,
            "registry_deep_sampled": 0,
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
            "unassigned_workers": [],
        }
    validate_projection(payload, plane)
    return payload


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
    if payload.get("last_success_at") is not None:
        return 0
    print(
        f"fleet-console {plane} sample DEGRADED: "
        f"{payload.get('last_error') or 'one or more sources failed'}",
        file=sys.stderr,
    )
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Goal Flight fleet-console projection producer")
    subparsers = parser.add_subparsers(dest="plane", required=True)

    fleet = subparsers.add_parser("fleet", help="sample local/remote fleet, usage, and projects")
    fleet.add_argument("--fleet-dir", type=Path)
    fleet.add_argument("--readers-dir", type=Path)
    fleet.add_argument("--usage-timeout-s", type=float, default=goalflight_usage.DEFAULT_TIMEOUT_S)
    fleet.add_argument("--output", type=Path, help="atomic GF_FLEET script output; JSON stdout when omitted")

    attention = subparsers.add_parser("attention", help="sample timestamped operator attention mail")
    attention.add_argument("--messages-dir", type=Path)
    attention.add_argument("--fleet-dir", type=Path)
    attention.add_argument("--output", type=Path, help="atomic GF_ATTENTION script output; JSON stdout when omitted")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.plane == "fleet":
        payload = build_fleet_plane(
            fleet_dir=args.fleet_dir,
            readers_dir=args.readers_dir,
            usage_timeout_s=args.usage_timeout_s,
        )
    else:
        payload = build_attention_plane(
            messages_dir=args.messages_dir,
            fleet_dir=args.fleet_dir,
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
