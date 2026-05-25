#!/usr/bin/env python3
"""Fleet status aggregation for `goalflight_fleet.py status --fleet` (Track A goal 9c)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import goalflight_fleet_billing as billing
import goalflight_fleet_mirror as mirror
import goalflight_fleet_status as status


def _read_json_object(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _dispatch_register_dir(fleet_dir: Path) -> Path:
    return fleet_dir / "register" / "dispatches"


def _collect_dispatch_meta(fleet_dir: Path) -> dict[str, dict[str, Any]]:
    """Map dispatch_id -> meta dict from aggregate.json and register/dispatches/*."""
    entries: dict[str, dict[str, Any]] = {}

    aggregate_path = fleet_dir / "register" / "aggregate.json"
    aggregate = _read_json_object(aggregate_path) or {}
    for dispatch_id in aggregate.get("active_dispatches") or []:
        if isinstance(dispatch_id, str) and dispatch_id:
            entries.setdefault(dispatch_id, {"dispatch_id": dispatch_id})

    register_dir = _dispatch_register_dir(fleet_dir)
    if register_dir.is_dir():
        for path in sorted(register_dir.iterdir()):
            if path.is_dir():
                dispatch_id = path.name
                meta = _read_json_object(path / "meta.json") or {}
                meta.setdefault("dispatch_id", dispatch_id)
                entries[dispatch_id] = {**entries.get(dispatch_id, {}), **meta}
            elif path.suffix == ".jsonl":
                dispatch_id = path.stem
                entries.setdefault(dispatch_id, {"dispatch_id": dispatch_id})

    return entries


def _pid_hint(meta: dict[str, Any], mirror_payload: dict[str, Any] | None) -> status.PidHint:
    hint = meta.get("pid_hint")
    if hint in ("alive", "dead", "unknown"):
        return hint  # type: ignore[return-value]
    if mirror_payload and mirror_payload.get("worker_pid"):
        return "alive"
    return "unknown"


def _mirror_stale(meta: dict[str, Any], mirror_result: mirror.MirrorReadResult | None) -> bool:
    if meta.get("mirror_stale") is True:
        return True
    last_observed = meta.get("last_mirror_seq")
    if last_observed is None or mirror_result is None or not mirror_result.ok:
        return False
    try:
        observed = int(last_observed)
    except (TypeError, ValueError):
        return False
    if mirror_result.last_seq is None:
        return False
    return mirror_result.last_seq < observed


def _classify_dispatch_row(
    meta: dict[str, Any],
    mirror_result: mirror.MirrorReadResult | None,
) -> status.DispatchClassification:
    mirror_payload = mirror_result.payload if mirror_result and mirror_result.ok else None
    return status.classify_dispatch_row(
        ssh_reachable=bool(meta.get("ssh_reachable", True)),
        mirror=mirror_result,
        mirror_stale=_mirror_stale(meta, mirror_result),
        lease_active=bool(meta.get("lease_active", False)),
        pid_hint=_pid_hint(meta, mirror_payload),
    )


def build_fleet_status(fleet_dir: Path) -> dict[str, Any]:
    """Aggregate per-node auth probes and dispatch rows for fleet status."""
    import goalflight_fleet as fleet

    fleet_path = fleet_dir / "fleet.json"
    result: dict[str, Any] = {
        "fleet_dir": str(fleet_dir),
        "exists": fleet_dir.is_dir(),
        "nodes": [],
        "dispatches": [],
    }
    if not fleet_path.exists():
        result["available"] = False
        result["reason"] = "fleet store missing"
        return result

    auth_summary = billing.fleet_auth_doctor(fleet_dir, refresh=False)
    result["auth"] = auth_summary

    fleet_doc = fleet.read_json(fleet_path)
    node_ids = sorted((fleet_doc.get("nodes") or {}).keys())
    auth_by_node = {
        str(entry.get("node_id")): entry for entry in auth_summary.get("nodes") or [] if isinstance(entry, dict)
    }

    dispatch_meta = _collect_dispatch_meta(fleet_dir)
    rows_by_node: dict[str, list[dict[str, Any]]] = {node_id: [] for node_id in node_ids}

    for dispatch_id, meta in sorted(dispatch_meta.items()):
        meta = dict(meta)
        meta.setdefault("dispatch_id", dispatch_id)
        node_id = str(meta.get("node_id") or (node_ids[0] if len(node_ids) == 1 else "unknown"))

        status_path = _dispatch_register_dir(fleet_dir) / dispatch_id / "status.json"
        mirror_result = mirror.read_status_mirror(status_path)
        classification = _classify_dispatch_row(meta, mirror_result)

        row = {
            "node": node_id,
            "dispatch_id": dispatch_id,
            "state": classification.state,
            "quarantine_reason": classification.quarantine_reason,
            "may_release": status.may_release_locks(classification),
        }
        result["dispatches"].append(row)
        rows_by_node.setdefault(node_id, []).append(row)

    for node_id in node_ids:
        node_entry = dict(auth_by_node.get(node_id) or {"node_id": node_id, "accounts": []})
        node_entry["dispatches"] = rows_by_node.get(node_id, [])
        result["nodes"].append(node_entry)

    unknown_rows = rows_by_node.get("unknown") or []
    if unknown_rows and not any(node.get("node_id") == "unknown" for node in result["nodes"]):
        result["nodes"].append({"node_id": "unknown", "accounts": [], "dispatches": unknown_rows})

    result["available"] = True
    return result


def format_fleet_status_table(payload: dict[str, Any]) -> str:
    headers = ("node", "dispatch", "state", "quarantine_reason")
    rows = [
        (
            str(row.get("node") or ""),
            str(row.get("dispatch_id") or ""),
            str(row.get("state") or ""),
            str(row.get("quarantine_reason") or ""),
        )
        for row in payload.get("dispatches") or []
    ]
    widths = [len(header) for header in headers]
    for row in rows:
        widths = [max(width, len(cell)) for width, cell in zip(widths, row)]

    def _fmt(cells: tuple[str, ...]) -> str:
        return "  ".join(cell.ljust(width) for cell, width in zip(cells, widths))

    lines = [_fmt(headers)]
    if rows:
        lines.extend(_fmt(row) for row in rows)
    else:
        lines.append("(no active dispatches)")
    return "\n".join(lines)
