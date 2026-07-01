#!/usr/bin/env python3
"""Stale lock predicates shared by doctor and fleet reconcile (Track A goal 10c).

Doctor may auto-release stale capacity/account locks when predicates match.
Never release when SSH partition alone, mirror stale alone, or PID still alive.
"""

from __future__ import annotations

import argparse
from typing import Any

import goalflight_fleet_store as fleet
import goalflight_fleet_status as status


def account_lock_stale_for_doctor(
    lock_doc: dict[str, Any],
    *,
    classification: status.DispatchClassification | None = None,
    ttl_expired: bool = False,
    owner_in_flight: bool = True,
) -> bool:
    """Return True when doctor may auto-release an account lock as stale."""
    if lock_doc.get("state") != "active":
        return False
    if classification is not None:
        if classification.quarantine_reason == status.QUARANTINE_SSH_PARTITION:
            return False
        if classification.quarantine_reason == status.QUARANTINE_MIRROR_STALE:
            return False
        if classification.state == "running":
            return False
        if classification.state in ("unknown", "quarantined") and not status.may_release_locks(classification):
            return False
    if ttl_expired:
        return True
    if classification is not None and status.may_release_locks(classification):
        return True
    if not owner_in_flight:
        return True
    return False


def doctor_may_release_dispatch_locks(classification: status.DispatchClassification) -> bool:
    """Dispatch-row stale release gate for doctor --fleet-reconcile-stale."""
    if classification.quarantine_reason == status.QUARANTINE_SSH_PARTITION:
        return False
    if classification.quarantine_reason == status.QUARANTINE_MIRROR_STALE:
        return False
    return status.may_release_locks(classification)


def account_lock_ttl_reapable(fleet_dir, lock_doc: dict[str, Any]) -> bool:
    """Return whether the TTL stale reaper may release an expired account lock.

    Salvage-classified dispatches are exempt until an explicit salvage/cleanup
    release. When an owner dispatch id is present but cannot be resolved, prefer
    safe (hold) over silent release of possible salvage-held locks.
    """
    import goalflight_fleet_reconcile as fleet_reconcile

    owner_dispatch_id = lock_doc.get("owner_dispatch_id")
    if not isinstance(owner_dispatch_id, str) or not owner_dispatch_id.strip():
        return True

    dispatch_dir = fleet_dir / "register" / "dispatches" / owner_dispatch_id
    if not dispatch_dir.is_dir():
        return False

    try:
        ctx = fleet_reconcile.build_dispatch_context(fleet_dir, owner_dispatch_id)
    except Exception:
        return False

    if ctx.classification.state == "salvage":
        return False
    remote_state = status.remote_state_from_mirror(ctx.mirror_result)
    if remote_state in status.SALVAGE_NEEDED_STATES:
        return False
    return True


def release_stale_account_locks(fleet_dir) -> list[str]:
    released: list[str] = []
    for doc in fleet.stale_account_locks(fleet_dir):
        account_key = doc.get("account_key")
        fencing_token = doc.get("fencing_token")
        if not account_key or not fencing_token:
            continue
        if not account_lock_ttl_reapable(fleet_dir, doc):
            continue
        try:
            fleet.release_account_lock(
                fleet_dir,
                account_key=str(account_key),
                fencing_token=str(fencing_token),
                reason="stale_ttl",
            )
            released.append(str(account_key))
        except fleet.AccountLockError:
            continue
    return released


def reconcile_fleet(fleet_dir, *, release_stale: bool = False) -> dict:
    result: dict = {"capacity_stale_released": [], "account_stale_released": []}
    try:
        import goalflight_capacity
    except ImportError:
        result["capacity_error"] = "goalflight_capacity unavailable"
    else:
        with goalflight_capacity.StateLock():
            data = goalflight_capacity.load_state()
            stale = goalflight_capacity.stale_active_leases(data)
        result["capacity_stale"] = len(stale)
        if release_stale and stale:
            ns = argparse.Namespace(state="expired", reason="stale_controller", keep=False)
            goalflight_capacity.cmd_release_stale(ns)
            result["capacity_stale_released"] = [
                lease.get("lease_id") for lease in stale if lease.get("lease_id")
            ]

    result["account_stale"] = len(fleet.stale_account_locks(fleet_dir))
    if release_stale:
        result["account_stale_released"] = release_stale_account_locks(fleet_dir)
    return result


def doctor_fleet_stale_release(
    fleet_dir,
    *,
    mutate: bool = False,
) -> dict[str, Any]:
    """Run stale release for capacity TTL locks + dispatch reconcile releases."""
    import goalflight_fleet_reconcile as fleet_reconcile

    summary: dict[str, Any] = {
        "capacity_stale_released": [],
        "account_stale_released": [],
        "dispatch_stale_released": [],
        "dispatch_quarantined": [],
    }
    base = reconcile_fleet(fleet_dir, release_stale=mutate)
    summary.update(base)

    import goalflight_fleet_status_cli as status_cli

    targets = fleet_reconcile.classify_fleet_dispatches(fleet_dir)
    meta_by_id = status_cli._collect_dispatch_meta(fleet_dir)
    for row in targets:
        dispatch_id = str(row.get("dispatch_id") or "")
        if not dispatch_id:
            continue
        meta = meta_by_id.get(dispatch_id) or {}
        ctx = fleet_reconcile.build_dispatch_context(
            fleet_dir,
            dispatch_id,
            meta,
            ssh_reachable=meta.get("ssh_reachable"),
        )
        if not doctor_may_release_dispatch_locks(ctx.classification):
            if ctx.classification.state in ("unknown", "quarantined"):
                summary["dispatch_quarantined"].append(
                    {
                        "dispatch_id": dispatch_id,
                        "reason": ctx.classification.quarantine_reason,
                    }
                )
            continue
        lock = fleet_reconcile.find_account_lock_for_dispatch(fleet_dir, dispatch_id)
        ttl_expired = False
        if lock:
            ttl_expired = fleet.account_lock_expired(lock)
        if not account_lock_stale_for_doctor(
            lock or {},
            classification=ctx.classification,
            ttl_expired=ttl_expired,
            owner_in_flight=True,
        ):
            continue
        if mutate:
            result = fleet_reconcile.reconcile_dispatch(fleet_dir, dispatch_id, mutate=True)
            if result.released:
                summary["dispatch_stale_released"].append(dispatch_id)
        else:
            summary.setdefault("dispatch_stale_candidates", []).append(dispatch_id)
    return summary
