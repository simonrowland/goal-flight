#!/usr/bin/env python3
"""Stale lock predicates shared by doctor and fleet reconcile (Track A goal 10c).

Doctor may auto-release stale capacity/account locks when predicates match.
Never release when SSH partition alone, mirror stale alone, or PID still alive.
"""

from __future__ import annotations

from typing import Any

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


def doctor_fleet_stale_release(
    fleet_dir,
    *,
    mutate: bool = False,
) -> dict[str, Any]:
    """Run stale release for capacity TTL locks + dispatch reconcile releases."""
    import goalflight_fleet as fleet
    import goalflight_fleet_reconcile as fleet_reconcile

    summary: dict[str, Any] = {
        "capacity_stale_released": [],
        "account_stale_released": [],
        "dispatch_stale_released": [],
        "dispatch_quarantined": [],
    }
    base = fleet.reconcile_fleet(fleet_dir, release_stale=mutate)
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
