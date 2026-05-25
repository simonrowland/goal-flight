#!/usr/bin/env python3
"""Fleet dispatch reconcile decision table (Track A goals 10b/10c).

Implements plan § Reconcile decision table: classify in-flight dispatch rows,
decide noop / quarantine / release, and append audit entries. Release paths call
``goalflight_fleet_status.may_release_locks`` before mutating account locks.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

import goalflight_fleet_mirror as mirror
import goalflight_fleet_status as status
import goalflight_fleet_status_cli as status_cli

ReconcileAction = Literal["noop", "quarantine", "release_locks", "refresh"]


@dataclass(frozen=True)
class DispatchReconcileContext:
    dispatch_id: str
    meta: dict[str, Any]
    classification: status.DispatchClassification
    may_release: bool
    ssh_reachable: bool
    account_lock: dict[str, Any] | None
    mirror_result: mirror.MirrorReadResult | None


@dataclass(frozen=True)
class ReconcileDecision:
    action: ReconcileAction
    reason: str


@dataclass(frozen=True)
class DispatchReconcileResult:
    dispatch_id: str
    action: ReconcileAction
    reason: str
    classification_state: str
    quarantine_reason: str | None
    may_release: bool
    released: bool
    account_key: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "dispatch_id": self.dispatch_id,
            "action": self.action,
            "reason": self.reason,
            "classification_state": self.classification_state,
            "quarantine_reason": self.quarantine_reason,
            "may_release": self.may_release,
            "released": self.released,
            "account_key": self.account_key,
        }


def reconcile_audit_path(fleet_dir: Path) -> Path:
    path = fleet_dir / "audit" / "reconcile.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def append_reconcile_audit(fleet_dir: Path, entry: dict[str, Any]) -> None:
    from goalflight_fleet import iso

    payload = dict(entry)
    payload.setdefault("ts", iso())
    payload.setdefault("schema", "goalflight.fleet.reconcile.audit.v1")
    with reconcile_audit_path(fleet_dir).open("a") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def find_account_lock_for_dispatch(fleet_dir: Path, dispatch_id: str) -> dict[str, Any] | None:
    import goalflight_fleet as fleet

    locks_dir = fleet_dir / "locks" / "accounts"
    if not locks_dir.is_dir():
        return None
    for path in locks_dir.glob("*.json"):
        try:
            doc = fleet.load_account_lock(path)
        except Exception:
            continue
        if not doc or doc.get("state") != "active":
            continue
        if doc.get("owner_dispatch_id") == dispatch_id:
            return doc
    return None


def account_key_for_dispatch(fleet_dir: Path, dispatch_id: str, meta: dict[str, Any]) -> str | None:
    explicit = meta.get("billing_account") or meta.get("account_key")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    lock = find_account_lock_for_dispatch(fleet_dir, dispatch_id)
    if lock and lock.get("account_key"):
        return str(lock["account_key"])
    return None


def build_dispatch_context(
    fleet_dir: Path,
    dispatch_id: str,
    meta: dict[str, Any] | None = None,
    *,
    ssh_reachable: bool | None = None,
    ssh_runner: Callable[[list[str]], tuple[int, str, str]] | None = None,
) -> DispatchReconcileContext:
    import goalflight_fleet as fleet
    import goalflight_fleet_ssh as fleet_ssh
    import goalflight_fleet_watch as fleet_watch

    meta = dict(meta or status_cli._collect_dispatch_meta(fleet_dir).get(dispatch_id) or {})
    meta.setdefault("dispatch_id", dispatch_id)
    if ssh_reachable is None and "ssh_reachable" not in meta:
        node_id = str(meta.get("node_id") or "")
        fleet_doc = fleet.read_json(fleet_dir / "fleet.json")
        node_entry = (fleet_doc.get("nodes") or {}).get(node_id)
        if isinstance(node_entry, dict):
            host = fleet_ssh.host_from_node_entry(node_id, node_entry)
            ssh_reachable = fleet_ssh.probe_ssh_reachable(host, runner=ssh_runner)
        else:
            ssh_reachable = False
    if ssh_reachable is not None:
        meta["ssh_reachable"] = ssh_reachable

    status_path = fleet_watch.dispatch_status_path(fleet_dir, dispatch_id)
    mirror_result = mirror.read_status_mirror(status_path)
    classification = status_cli._classify_dispatch_row(meta, mirror_result)
    lock = find_account_lock_for_dispatch(fleet_dir, dispatch_id)
    if lock is not None:
        meta.setdefault("lease_active", True)
    return DispatchReconcileContext(
        dispatch_id=dispatch_id,
        meta=meta,
        classification=classification,
        may_release=status.may_release_locks(classification),
        ssh_reachable=bool(meta.get("ssh_reachable", True)),
        account_lock=lock,
        mirror_result=mirror_result,
    )


def decide_reconcile_action(ctx: DispatchReconcileContext) -> ReconcileDecision:
    """Map classification + SSH reachability to reconcile action (plan table)."""
    if ctx.meta.get("row_state") == "released":
        return ReconcileDecision("noop", "already_released")

    if not ctx.ssh_reachable:
        return ReconcileDecision("quarantine", "ssh_partition")

    if ctx.classification.state == "running":
        return ReconcileDecision("refresh", "running_alive")

    if ctx.may_release:
        return ReconcileDecision("release_locks", ctx.classification.state)

    if ctx.classification.state in ("unknown", "quarantined"):
        reason = ctx.classification.quarantine_reason or ctx.classification.state
        return ReconcileDecision("quarantine", str(reason))

    if ctx.classification.state == "terminal":
        return ReconcileDecision("noop", "terminal_no_release_predicate")

    return ReconcileDecision("noop", ctx.classification.state)


def _remove_active_dispatch(fleet_dir: Path, dispatch_id: str) -> None:
    from goalflight_fleet import _atomic_write_json, read_json

    aggregate_path = fleet_dir / "register" / "aggregate.json"
    if not aggregate_path.exists():
        return
    doc = read_json(aggregate_path)
    active = [d for d in doc.get("active_dispatches") or [] if d != dispatch_id]
    doc["active_dispatches"] = active
    _atomic_write_json(aggregate_path, doc)


def _mark_dispatch_released(fleet_dir: Path, dispatch_id: str, *, reason: str) -> None:
    import goalflight_fleet_watch as fleet_watch

    meta_path = fleet_watch.dispatch_meta_path(fleet_dir, dispatch_id)
    if not meta_path.exists():
        return
    try:
        meta = json.loads(meta_path.read_text())
    except (OSError, json.JSONDecodeError):
        meta = {}
    meta["row_state"] = "released"
    meta["released_reason"] = reason
    meta["lease_active"] = False
    from goalflight_fleet import _atomic_write_json

    _atomic_write_json(meta_path, meta)


def release_dispatch_locks(
    fleet_dir: Path,
    ctx: DispatchReconcileContext,
    *,
    reason: str = "reconcile",
) -> tuple[bool, str | None]:
    """Release account lock for dispatch when ``may_release_locks`` is True."""
    import goalflight_fleet as fleet
    import goalflight_fleet_watch as fleet_watch

    if ctx.meta.get("row_state") == "released":
        return False, None
    if not ctx.may_release:
        return False, None
    lock = ctx.account_lock or find_account_lock_for_dispatch(fleet_dir, ctx.dispatch_id)
    if not lock:
        _remove_active_dispatch(fleet_dir, ctx.dispatch_id)
        _mark_dispatch_released(fleet_dir, ctx.dispatch_id, reason=reason)
        return True, None
    account_key = lock.get("account_key")
    fencing_token = lock.get("fencing_token")
    if not account_key or not fencing_token:
        return False, None
    try:
        fleet.release_account_lock(
            fleet_dir,
            account_key=str(account_key),
            fencing_token=str(fencing_token),
            reason=reason,
        )
    except fleet.AccountLockError:
        return False, str(account_key)
    _remove_active_dispatch(fleet_dir, ctx.dispatch_id)
    _mark_dispatch_released(fleet_dir, ctx.dispatch_id, reason=reason)
    return True, str(account_key)


def reconcile_dispatch(
    fleet_dir: Path,
    dispatch_id: str,
    *,
    mutate: bool = False,
    ssh_reachable: bool | None = None,
    ssh_runner: Callable[[list[str]], tuple[int, str, str]] | None = None,
    audit: bool = True,
) -> DispatchReconcileResult:
    ctx = build_dispatch_context(
        fleet_dir,
        dispatch_id,
        ssh_reachable=ssh_reachable,
        ssh_runner=ssh_runner,
    )
    decision = decide_reconcile_action(ctx)
    released = False
    account_key: str | None = None
    before = {
        "classification_state": ctx.classification.state,
        "quarantine_reason": ctx.classification.quarantine_reason,
        "may_release": ctx.may_release,
        "lease_active": bool(ctx.meta.get("lease_active")),
    }
    if mutate and decision.action == "release_locks":
        released, account_key = release_dispatch_locks(fleet_dir, ctx, reason=decision.reason)
    result = DispatchReconcileResult(
        dispatch_id=dispatch_id,
        action=decision.action,
        reason=decision.reason,
        classification_state=ctx.classification.state,
        quarantine_reason=ctx.classification.quarantine_reason,
        may_release=ctx.may_release,
        released=released,
        account_key=account_key,
    )
    if audit and mutate:
        append_reconcile_audit(
            fleet_dir,
            {
                "dispatch_id": dispatch_id,
                "decision": decision.action,
                "reason": decision.reason,
                "before": before,
                "after": result.to_dict(),
                "released": released,
            },
        )
    return result


def reconcile_all_in_flight(
    fleet_dir: Path,
    *,
    mutate: bool = False,
    ssh_reachable_by_dispatch: dict[str, bool] | None = None,
    ssh_runner: Callable[[list[str]], tuple[int, str, str]] | None = None,
) -> dict[str, Any]:
    targets = status_cli._collect_dispatch_meta(fleet_dir)
    rows: list[dict[str, Any]] = []
    released_ids: list[str] = []
    for dispatch_id in sorted(targets.keys()):
        ssh = None
        if ssh_reachable_by_dispatch and dispatch_id in ssh_reachable_by_dispatch:
            ssh = ssh_reachable_by_dispatch[dispatch_id]
        row = reconcile_dispatch(
            fleet_dir,
            dispatch_id,
            mutate=mutate,
            ssh_reachable=ssh,
            ssh_runner=ssh_runner,
            audit=mutate,
        )
        rows.append(row.to_dict())
        if row.released:
            released_ids.append(dispatch_id)
    return {"dispatches": rows, "released_dispatch_ids": released_ids}


def classify_fleet_dispatches(fleet_dir: Path) -> list[dict[str, Any]]:
    """Read-only classification for doctor ``--fleet`` report."""
    payload = status_cli.build_fleet_status(fleet_dir)
    return list(payload.get("dispatches") or [])
