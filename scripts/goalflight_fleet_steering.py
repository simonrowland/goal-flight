#!/usr/bin/env python3
"""Steering propose / apply / rollback with audit (Track A goal 5)."""

from __future__ import annotations

import copy
import datetime as dt
import json
import uuid
from pathlib import Path
from typing import Any

import goalflight_fleet_schemas as schemas

STEERING_PROPOSAL_SCHEMA = "goalflight.fleet.steering.proposal.v1"
AUDIT_SCHEMA = "goalflight.fleet.steering.audit.v1"


class SteeringError(Exception):
    pass


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso(ts: dt.datetime | None = None) -> str:
    return (ts or utc_now()).isoformat(timespec="seconds")


def proposals_dir(fleet_dir: Path) -> Path:
    path = fleet_dir / "proposals"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def audit_path(fleet_dir: Path) -> Path:
    path = fleet_dir / "audit" / "steering.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def steering_path(fleet_dir: Path) -> Path:
    return fleet_dir / "steering.json"


def load_steering_doc(fleet_dir: Path) -> dict:
    path = steering_path(fleet_dir)
    if not path.exists():
        raise SteeringError(f"missing {path}")
    doc = json.loads(path.read_text())
    schemas.validate_steering(doc)
    return doc


def steering_hash(fleet_dir: Path) -> str:
    from goalflight_fleet_store import sha256_file

    return f"sha256:{sha256_file(steering_path(fleet_dir))}"


def _atomic_write_json(path: Path, data: dict) -> None:
    from goalflight_fleet_store import _atomic_write_json as write

    write(path, data)


def _get_path(doc: dict, path: str) -> Any:
    parts = [p for p in path.strip("/").split("/") if p]
    cur: Any = doc
    for part in parts:
        if isinstance(cur, list):
            cur = cur[int(part)]
        else:
            cur = cur[part]
    return cur


def _set_path(doc: dict, path: str, value: Any) -> None:
    parts = [p for p in path.strip("/").split("/") if p]
    cur: Any = doc
    for part in parts[:-1]:
        key = int(part) if isinstance(cur, list) else part
        cur = cur[key]
    last = parts[-1]
    if isinstance(cur, list):
        cur[int(last)] = value
    else:
        cur[last] = value


def _remove_path(doc: dict, path: str) -> Any:
    parts = [p for p in path.strip("/").split("/") if p]
    cur: Any = doc
    for part in parts[:-1]:
        key = int(part) if isinstance(cur, list) else part
        cur = cur[key]
    last = parts[-1]
    if isinstance(cur, list):
        idx = int(last)
        return cur.pop(idx)
    removed = cur[last]
    del cur[last]
    return removed


def apply_patch(doc: dict, patch: list[dict]) -> dict:
    """Apply a minimal JSON Patch subset (replace, add, remove, move)."""
    out = copy.deepcopy(doc)
    for op in patch:
        kind = op.get("op")
        path = op.get("path", "")
        if not path.startswith("/"):
            raise SteeringError(f"patch path must be absolute: {path!r}")
        if kind == "replace":
            _set_path(out, path, op["value"])
        elif kind == "add":
            parts = [p for p in path.strip("/").split("/") if p]
            cur: Any = out
            for part in parts[:-1]:
                key = int(part) if isinstance(cur, list) else part
                cur = cur[key]
            last = parts[-1]
            if isinstance(cur, list):
                if last == "-":
                    cur.append(op["value"])
                else:
                    cur.insert(int(last), op["value"])
            else:
                cur[last] = op["value"]
        elif kind == "remove":
            _remove_path(out, path)
        elif kind == "move":
            value = _remove_path(out, op["from"])
            _set_path(out, path, value)
        else:
            raise SteeringError(f"unsupported patch op: {kind!r}")
    schemas.validate_steering(out)
    return out


def effective_steering(doc: dict, *, now: dt.datetime | None = None) -> dict:
    """Persistent policy with non-expired conversation overrides applied."""
    now = now or utc_now()
    out = copy.deepcopy(doc)
    overrides = out.get("conversation_overrides") or []
    active: list[dict] = []
    for item in overrides:
        expires = item.get("expires_at")
        if expires:
            try:
                exp = dt.datetime.fromisoformat(expires.replace("Z", "+00:00"))
            except ValueError:
                continue
            if exp < now:
                continue
        active.append(item)
    for item in active:
        patch = item.get("patch")
        if isinstance(patch, list):
            out = apply_patch(out, patch)
    return out


def append_audit(fleet_dir: Path, entry: dict) -> dict:
    entry = dict(entry)
    entry.setdefault("schema", AUDIT_SCHEMA)
    entry.setdefault("audit_id", str(uuid.uuid4()))
    entry.setdefault("ts", iso())
    with audit_path(fleet_dir).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, separators=(",", ":")) + "\n")
    return entry


def read_audit_entries(fleet_dir: Path) -> list[dict]:
    path = audit_path(fleet_dir)
    if not path.exists():
        return []
    entries: list[dict] = []
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        entries.append(json.loads(stripped))
    return entries


def proposal_path(fleet_dir: Path, proposal_id: str) -> Path:
    return proposals_dir(fleet_dir) / f"{proposal_id}.json"


def load_proposal(fleet_dir: Path, proposal_id: str) -> dict:
    path = proposal_path(fleet_dir, proposal_id)
    if not path.exists():
        raise SteeringError(f"unknown proposal_id: {proposal_id}")
    doc = json.loads(path.read_text())
    if doc.get("schema") != STEERING_PROPOSAL_SCHEMA:
        raise SteeringError("invalid proposal schema")
    return doc


def list_proposals(fleet_dir: Path, *, pending_only: bool = False) -> list[dict]:
    out: list[dict] = []
    now = utc_now()
    for path in sorted(proposals_dir(fleet_dir).glob("*.json")):
        doc = json.loads(path.read_text())
        if doc.get("schema") != STEERING_PROPOSAL_SCHEMA:
            continue
        if pending_only:
            if doc.get("state") != "pending":
                continue
            expires = doc.get("expires_at")
            if expires:
                try:
                    exp = dt.datetime.fromisoformat(expires.replace("Z", "+00:00"))
                except ValueError:
                    pass
                else:
                    if exp < now:
                        continue
        out.append(doc)
    return out


def propose_steering(
    fleet_dir: Path,
    *,
    patch: list[dict],
    reason: str,
    created_by: dict,
    ttl_hours: int = 24,
) -> dict:
    base_hash = steering_hash(fleet_dir)
    proposal_id = str(uuid.uuid4())
    expires = utc_now() + dt.timedelta(hours=ttl_hours)
    proposal = {
        "schema": STEERING_PROPOSAL_SCHEMA,
        "schema_version": schemas.SCHEMA_VERSION,
        "proposal_id": proposal_id,
        "state": "pending",
        "base_hash": base_hash,
        "patch": patch,
        "reason": reason,
        "created_by": created_by,
        "created_at": iso(),
        "expires_at": iso(expires),
    }
    _atomic_write_json(proposal_path(fleet_dir, proposal_id), proposal)
    return proposal


def build_priority_prefer_patch(fleet_dir: Path, node_name: str) -> list[dict]:
    doc = load_steering_doc(fleet_dir)
    priority = list(doc.get("node_policy", {}).get("priority") or [])
    if node_name not in priority:
        return [{"op": "add", "path": "/node_policy/priority/0", "value": node_name}]
    idx = priority.index(node_name)
    if idx == 0:
        return []
    return [{"op": "move", "from": f"/node_policy/priority/{idx}", "path": "/node_policy/priority/0"}]


def apply_proposal(
    fleet_dir: Path,
    proposal_id: str,
    *,
    actor: dict | None = None,
) -> dict:
    proposal = load_proposal(fleet_dir, proposal_id)
    if proposal.get("state") != "pending":
        raise SteeringError(f"proposal not pending: {proposal_id}")
    expires = proposal.get("expires_at")
    if expires:
        exp = dt.datetime.fromisoformat(expires.replace("Z", "+00:00"))
        if exp < utc_now():
            raise SteeringError(f"proposal expired: {proposal_id}")

    current_hash = steering_hash(fleet_dir)
    if proposal.get("base_hash") != current_hash:
        raise SteeringError(
            f"base_hash mismatch: expected {proposal.get('base_hash')}, got {current_hash}"
        )

    before_doc = load_steering_doc(fleet_dir)
    after_doc = apply_patch(before_doc, proposal["patch"])
    _atomic_write_json(steering_path(fleet_dir), after_doc)
    after_hash = steering_hash(fleet_dir)

    audit = append_audit(
        fleet_dir,
        {
            "actor": actor or proposal.get("created_by") or {},
            "proposal_id": proposal_id,
            "before_hash": proposal["base_hash"],
            "after_hash": after_hash,
            "patch": proposal["patch"],
            "before_doc": before_doc,
            "result": "applied",
        },
    )

    proposal["state"] = "applied"
    proposal["applied_at"] = iso()
    proposal["audit_id"] = audit["audit_id"]
    _atomic_write_json(proposal_path(fleet_dir, proposal_id), proposal)

    try:
        import goalflight_messages as messages

        messages.write_steering_envelope(
            fleet_dir,
            audit_id=audit["audit_id"],
            proposal_id=proposal_id,
            patch=proposal["patch"],
            after_hash=after_hash,
        )
    except Exception:
        pass

    return {"ok": True, "audit_id": audit["audit_id"], "after_hash": after_hash}


def rollback_audit(fleet_dir: Path, audit_id: str, *, actor: dict | None = None) -> dict:
    entry = None
    for item in read_audit_entries(fleet_dir):
        if item.get("audit_id") == audit_id:
            entry = item
            break
    if entry is None:
        raise SteeringError(f"unknown audit_id: {audit_id}")
    before_doc = entry.get("before_doc")
    if not isinstance(before_doc, dict):
        raise SteeringError("audit entry missing before_doc snapshot")
    current_doc = load_steering_doc(fleet_dir)
    before_hash = steering_hash(fleet_dir)
    _atomic_write_json(steering_path(fleet_dir), before_doc)
    after_hash = steering_hash(fleet_dir)
    rollback_audit_entry = append_audit(
        fleet_dir,
        {
            "actor": actor or {},
            "proposal_id": entry.get("proposal_id"),
            "before_hash": before_hash,
            "after_hash": after_hash,
            "patch": [],
            "before_doc": current_doc,
            "result": "rollback",
            "rollback_of": audit_id,
        },
    )
    return {"ok": True, "audit_id": rollback_audit_entry["audit_id"], "after_hash": after_hash}


def explain_steering(fleet_dir: Path, *, chunk_path: Path | None = None) -> str:
    doc = effective_steering(load_steering_doc(fleet_dir))
    priority = doc.get("node_policy", {}).get("priority") or []
    prefer_node = priority[0] if priority else "(none)"
    billing_hint = "openai/default"
    billing_path = fleet_dir / "billing-accounts.json"
    if billing_path.exists():
        billing = json.loads(billing_path.read_text())
        accounts = billing.get("accounts") or []
        if accounts:
            billing_hint = accounts[0].get("account_key", billing_hint)
    chunk_note = ""
    if chunk_path is not None:
        chunk_note = f" chunk={chunk_path.name};"
    return (
        f"would prefer node {prefer_node} + billing-account {billing_hint};"
        f"{chunk_note} MVP still requires explicit --node and --billing-account on dispatch."
    )
