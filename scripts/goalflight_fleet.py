#!/usr/bin/env python3
"""Fleet CLI facade and compatibility exports for fleet store primitives.

Lock order (mutations): registry lock → account lock → worktree lock → remote capacity lease.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import goalflight_fleet_schemas as schemas
from goalflight_fleet_store import (
    FLEET_FILES,
    AccountLockError,
    FleetError,
    RegistryLock,
    RegistryLockError,
    _account_lock_file_lock,
    _atomic_write_json,
    account_lock_expired,
    account_lock_path,
    acquire_account_lock,
    bootstrap,
    controller_id,
    default_fleet_dir,
    export_bundle,
    import_bundle,
    iso,
    load_account_lock,
    lock_snapshot_metadata,
    parse_iso,
    read_json,
    registry_lock_path,
    release_account_lock,
    renew_account_lock,
    sha256_bytes,
    sha256_file,
    stale_account_locks,
    utc_now,
)
from goalflight_fleet_stale import reconcile_fleet, release_stale_account_locks


def cmd_bootstrap(args: argparse.Namespace) -> int:
    result = bootstrap(args.fleet_dir, force=args.force)
    if args.json:
        print(json.dumps({"fleet_dir": str(args.fleet_dir), "paths": result}, indent=2))
    else:
        print(f"fleet_dir={args.fleet_dir}")
        for name, status in sorted(result.items()):
            print(f"{status}\t{name}")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    errors: list[str] = []
    for name in FLEET_FILES:
        path = args.fleet_dir / name
        if not path.exists():
            errors.append(f"{path}: missing")
            continue
        errors.extend(schemas.validate_file(path))
    if errors:
        for line in errors:
            print(line, file=sys.stderr)
        return 1
    print("fleet_valid=1")
    return 0


def _legacy_fleet_status(fleet_dir: Path) -> dict:
    info = {
        "fleet_dir": str(fleet_dir),
        "exists": fleet_dir.is_dir(),
        "files": {},
        "account_locks_active": 0,
    }
    for name in (*FLEET_FILES, "register/aggregate.json"):
        path = fleet_dir / name
        info["files"][name] = path.exists()
    locks_dir = fleet_dir / "locks" / "accounts"
    if locks_dir.is_dir():
        for path in locks_dir.glob("*.json"):
            try:
                doc = load_account_lock(path)
            except schemas.SchemaError:
                continue
            if doc and doc.get("state") == "active":
                info["account_locks_active"] += 1
    fleet_path = fleet_dir / "fleet.json"
    if fleet_path.exists():
        try:
            fleet_doc = read_json(fleet_path)
            nodes = fleet_doc.get("nodes") or {}
            info["nodes"] = {
                "count": len(nodes),
                "ids": sorted(nodes.keys()),
            }
        except (OSError, json.JSONDecodeError, schemas.SchemaError):
            info["nodes"] = {"count": 0, "ids": [], "error": "invalid fleet.json"}
    return info


def cmd_status(args: argparse.Namespace) -> int:
    if args.fleet:
        import goalflight_fleet_status_cli as fleet_status_cli

        info = fleet_status_cli.build_fleet_status(args.fleet_dir)
        if args.json:
            print(json.dumps(info, indent=2))
        else:
            print(fleet_status_cli.format_fleet_status_table(info))
        return 0 if info.get("exists") else 1

    info = _legacy_fleet_status(args.fleet_dir)
    print(json.dumps(info, indent=2))
    return 0 if info["exists"] else 1


def cmd_export(args: argparse.Namespace) -> int:
    manifest = export_bundle(args.fleet_dir, args.out)
    if args.json:
        print(json.dumps({"ok": True, "out": str(args.out), "manifest": manifest}, indent=2))
    else:
        print(f"exported {args.out}")
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    try:
        result = import_bundle(args.fleet_dir, args.input_path, merge=args.merge)
    except (FleetError, schemas.SchemaError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


def cmd_lock_acquire(args: argparse.Namespace) -> int:
    try:
        with RegistryLock(args.fleet_dir):
            doc = acquire_account_lock(
                args.fleet_dir,
                account_key=args.account_key,
                owner_dispatch_id=args.dispatch_id,
                ttl_s=args.ttl_s,
            )
    except (RegistryLockError, AccountLockError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(doc, indent=2))
    return 0


def cmd_lock_release(args: argparse.Namespace) -> int:
    try:
        doc = release_account_lock(
            args.fleet_dir,
            account_key=args.account_key,
            fencing_token=args.fencing_token,
            reason=args.reason,
        )
    except AccountLockError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(doc, indent=2))
    return 0


def cmd_reconcile(args: argparse.Namespace) -> int:
    if getattr(args, "dispatch_id", None) or getattr(args, "all_in_flight", False):
        import goalflight_fleet_reconcile as fleet_reconcile

        mutate = bool(args.release_stale)
        if args.dispatch_id:
            row = fleet_reconcile.reconcile_dispatch(
                args.fleet_dir,
                args.dispatch_id,
                mutate=mutate,
            )
            print(json.dumps(row.to_dict(), indent=2))
            return 0
        summary = fleet_reconcile.reconcile_all_in_flight(args.fleet_dir, mutate=mutate)
        print(json.dumps(summary, indent=2))
        return 0
    result = reconcile_fleet(args.fleet_dir, release_stale=args.release_stale)
    print(json.dumps(result, indent=2))
    return 0


def cmd_dispatch(args: argparse.Namespace) -> int:
    import goalflight_fleet_dispatch as fleet_dispatch

    return fleet_dispatch.cmd_dispatch(args)


def cmd_watch(args: argparse.Namespace) -> int:
    if not args.fleet:
        print("watch requires --fleet", file=sys.stderr)
        return 2
    import goalflight_fleet_watch as fleet_watch

    return fleet_watch.cmd_watch_fleet(args)


def cmd_ferry(args: argparse.Namespace) -> int:
    import goalflight_fleet_ferry as fleet_ferry

    return fleet_ferry.cmd_ferry(args)


def cmd_salvage(args: argparse.Namespace) -> int:
    import goalflight_fleet_ferry as fleet_ferry

    return fleet_ferry.cmd_salvage(args)


def cmd_salvage_complete(args: argparse.Namespace) -> int:
    manifest_path = args.manifest.expanduser()
    if not manifest_path.exists():
        print(f"missing manifest: {manifest_path}", file=sys.stderr)
        return 1
    manifest = read_json(manifest_path)
    account_key = manifest.get("account_key")
    fencing_token = manifest.get("fencing_token")
    if not account_key or not fencing_token:
        print(
            "manifest missing account_key or fencing_token; "
            "re-run salvage with --dispatch-id",
            file=sys.stderr,
        )
        return 1
    try:
        doc = release_account_lock(
            args.fleet_dir,
            account_key=str(account_key),
            fencing_token=str(fencing_token),
            reason=args.reason,
        )
    except AccountLockError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(doc, indent=2))
    return 0


def _steering_actor(args: argparse.Namespace) -> dict:
    return {
        "controller_id": controller_id(),
        "host_adapter": getattr(args, "host_adapter", "cli"),
    }


def cmd_steering_propose(args: argparse.Namespace) -> int:
    import goalflight_fleet_steering as steering

    if args.node:
        patch = steering.build_priority_prefer_patch(args.fleet_dir, args.node)
        reason = args.reason or f"prefer node {args.node}"
    elif args.patch_file:
        patch_doc = json.loads(Path(args.patch_file).read_text())
        patch = patch_doc if isinstance(patch_doc, list) else patch_doc.get("patch", [])
        reason = args.reason or patch_doc.get("reason", "proposed change")
    else:
        print("provide --node or --patch-file", file=sys.stderr)
        return 1
    if not patch:
        print(json.dumps({"ok": True, "noop": True, "reason": "already satisfied"}))
        return 0
    proposal = steering.propose_steering(
        args.fleet_dir,
        patch=patch,
        reason=reason,
        created_by=_steering_actor(args),
        ttl_hours=args.ttl_hours,
    )
    print(json.dumps(proposal, indent=2))
    return 0


def cmd_steering_show(args: argparse.Namespace) -> int:
    import goalflight_fleet_steering as steering

    if args.pending:
        items = steering.list_proposals(args.fleet_dir, pending_only=True)
        print(json.dumps(items, indent=2))
        return 0
    doc = steering.effective_steering(steering.load_steering_doc(args.fleet_dir))
    print(json.dumps(doc, indent=2))
    return 0


def cmd_steering_apply(args: argparse.Namespace) -> int:
    import goalflight_fleet_steering as steering

    try:
        with RegistryLock(args.fleet_dir):
            result = steering.apply_proposal(
                args.fleet_dir,
                args.proposal_id,
                actor=_steering_actor(args),
            )
    except (RegistryLockError, steering.SteeringError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


def cmd_steering_rollback(args: argparse.Namespace) -> int:
    import goalflight_fleet_steering as steering

    try:
        with RegistryLock(args.fleet_dir):
            result = steering.rollback_audit(
                args.fleet_dir,
                args.audit_id,
                actor=_steering_actor(args),
            )
    except (RegistryLockError, steering.SteeringError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


def cmd_steering_explain(args: argparse.Namespace) -> int:
    import goalflight_fleet_steering as steering

    line = steering.explain_steering(args.fleet_dir, chunk_path=args.chunk)
    print(line)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Goal Flight fleet store")
    parser.add_argument("--fleet-dir", type=Path, default=default_fleet_dir())
    sub = parser.add_subparsers(dest="cmd", required=True)

    boot = sub.add_parser("bootstrap")
    boot.add_argument("--force", action="store_true")
    boot.add_argument("--json", action="store_true")
    boot.set_defaults(func=cmd_bootstrap)

    sub.add_parser("validate").set_defaults(func=cmd_validate)
    stat = sub.add_parser("status")
    stat.add_argument("--fleet", action="store_true", help="Aggregate per-node dispatch rows")
    stat.add_argument("--json", action="store_true", help="JSON output (default for legacy status)")
    stat.set_defaults(func=cmd_status)

    export = sub.add_parser("export")
    export.add_argument("--out", "-o", type=Path, required=True)
    export.add_argument("--json", action="store_true")
    export.set_defaults(func=cmd_export)

    imp = sub.add_parser("import")
    imp.add_argument("--in", dest="input_path", type=Path, required=True)
    imp.add_argument("--merge", choices=["strict", "prefer-local"], default="strict")
    imp.set_defaults(func=cmd_import)

    lock_acq = sub.add_parser("lock-acquire")
    lock_acq.add_argument("--account-key", required=True)
    lock_acq.add_argument("--dispatch-id", required=True)
    lock_acq.add_argument("--ttl-s", type=int, default=3600)
    lock_acq.set_defaults(func=cmd_lock_acquire)

    lock_rel = sub.add_parser("lock-release")
    lock_rel.add_argument("--account-key", required=True)
    lock_rel.add_argument("--fencing-token", required=True)
    lock_rel.add_argument("--reason", default="released")
    lock_rel.set_defaults(func=cmd_lock_release)

    recon = sub.add_parser("reconcile")
    recon.add_argument("--release-stale", action="store_true")
    recon.add_argument("--dispatch-id", help="Reconcile one in-flight dispatch row")
    recon.add_argument("--all-in-flight", action="store_true", help="Reconcile all in-flight rows")
    recon.set_defaults(func=cmd_reconcile)

    dispatch = sub.add_parser("dispatch", help="Remote dispatch preview/exec (MVP)")
    dispatch.add_argument("--node", required=True)
    dispatch.add_argument("--prompt", required=True)
    dispatch.add_argument("--agent", help="Explicit agent (required unless --thin-defaults)")
    dispatch.add_argument("--billing-account", help="Explicit billing account")
    dispatch.add_argument("--dispatch-id")
    dispatch.add_argument("--base-sha", help="Required 40-hex controller-resolved base for remote worktree creation")
    dispatch.add_argument("--thin-defaults", action="store_true", help="Fill agent/billing from steering")
    dispatch.add_argument(
        "--dispatch-mode",
        choices=("goal", "one-shot", "tiny"),
        default="goal",
        help="Goal-loop dispatches require a green tool-smoke canary; one-shot/tiny dispatches opt out",
    )
    dispatch.add_argument(
        "--tool-smoke",
        choices=("auto", "require", "skip"),
        default="auto",
        help="Tool-smoke gate policy: auto gates goal mode; require gates every mode; "
        "skip opts out in EVERY mode (authoritative, incl. goal).",
    )
    dispatch.add_argument(
        "--tool-smoke-sandbox",
        default="read-only",
        help="Sandbox/profile identity for tool-smoke cache lookup (default read-only)",
    )
    dispatch.add_argument("--exec", action="store_true", help="Acquire locks and spawn (default preview)")
    dispatch.add_argument("--stub-remote", action="store_true", help="Use stub SSH runner (tests)")
    dispatch.add_argument("--stub-terminal", action="store_true", help="Complete stub dispatch immediately")
    dispatch.add_argument("--json", action="store_true")
    dispatch.set_defaults(func=cmd_dispatch)

    import goalflight_fleet_tool_smoke as fleet_tool_smoke

    smoke = sub.add_parser("tool-smoke", help="Run/read fleet worker native Read canaries")
    smoke_sub = smoke.add_subparsers(dest="tool_smoke_cmd", required=True)
    smoke_run = smoke_sub.add_parser("run", help="Run a live tool-smoke canary (preview unless --exec)")
    smoke_run.add_argument("--node", required=True)
    smoke_run.add_argument("--agent", required=True)
    smoke_run.add_argument("--base-sha", required=True)
    smoke_run.add_argument("--sandbox", default="read-only")
    smoke_run.add_argument("--model-version")
    smoke_run.add_argument("--ttl-s", type=int, default=fleet_tool_smoke.DEFAULT_TTL_S)
    smoke_run.add_argument("--exec", action="store_true")
    smoke_run.set_defaults(func=fleet_tool_smoke.cmd_tool_smoke_run)
    smoke_status = smoke_sub.add_parser("status", help="Read cached tool-smoke verdict")
    smoke_status.add_argument("--node", required=True)
    smoke_status.add_argument("--agent", required=True)
    smoke_status.add_argument("--base-sha", required=True)
    smoke_status.add_argument("--sandbox", default="read-only")
    smoke_status.add_argument("--model-version")
    smoke_status.set_defaults(func=fleet_tool_smoke.cmd_tool_smoke_status)

    watch = sub.add_parser("watch", help="Mirror remote dispatch status into orchestrator register")
    watch.add_argument("--fleet", action="store_true", help="Watch all in-flight fleet dispatches")
    watch.add_argument("--once", action="store_true", help="Single sync pass (default when no --interval)")
    watch.add_argument("--interval", type=float, default=0.0, help="Poll interval seconds (loop until interrupted)")
    watch.add_argument("--until-terminal", help="Poll one dispatch until mirrored state is terminal")
    watch.add_argument("--timeout-s", type=float, default=3600.0, help="Max seconds for --until-terminal")
    watch.add_argument("--stale-s", type=float, default=300.0, help="Seconds without mirror seq progress before pid identity check")
    watch.add_argument("--dry-run", action="store_true", help="Skip real SSH (transport no-op fetch)")
    watch.add_argument("--json", action="store_true")
    watch.set_defaults(func=cmd_watch)

    ferry = sub.add_parser("ferry", help="Fixed-envelope rsync ferry between controller and node")
    ferry.add_argument("--node", required=True)
    ferry.add_argument("--direction", choices=("pull", "push"), required=True)
    ferry.add_argument("--src-root", required=True)
    ferry.add_argument("--dst-root", required=True)
    ferry.add_argument("--path", action="append", required=True, help="Relative file path under src root")
    ferry.add_argument("--purpose", required=True, help="Receipt purpose label")
    ferry.add_argument("--exec", action="store_true", help="Run rsync (default preview)")
    ferry.add_argument("--json", action="store_true")
    ferry.set_defaults(func=cmd_ferry)

    salvage = sub.add_parser("salvage", help="Convergent-rsync salvage of a remote worktree")
    salvage.add_argument("--node", required=True)
    salvage.add_argument("--worktree-path", required=True)
    salvage.add_argument("--out-dir", type=Path, required=True)
    salvage.add_argument("--dispatch-id", help="Owning dispatch id (records lock identity in manifest)")
    salvage.add_argument("--purpose", default="salvage")
    salvage.add_argument("--append-only", action="append", default=None, help="Path/pattern excluded from convergence")
    salvage.add_argument("--max-iterations", type=int, default=10)
    salvage.add_argument("--sleep-s", type=float, default=1.0)
    salvage.add_argument("--exec", action="store_true", help="Run SSH/rsync (default preview)")
    salvage.add_argument("--json", action="store_true")
    salvage.set_defaults(func=cmd_salvage)

    salvage_complete = sub.add_parser(
        "salvage-complete",
        help="Release the account lock recorded in a salvage manifest",
    )
    salvage_complete.add_argument("--manifest", type=Path, required=True)
    salvage_complete.add_argument("--reason", default="salvage_complete")
    salvage_complete.set_defaults(func=cmd_salvage_complete)

    steering = sub.add_parser("steering")
    steering_sub = steering.add_subparsers(dest="steering_cmd", required=True)

    st_prop = steering_sub.add_parser("propose")
    st_prop.add_argument("--node", help="Prefer this node at priority 0")
    st_prop.add_argument("--patch-file", type=Path)
    st_prop.add_argument("--reason", default="")
    st_prop.add_argument("--ttl-hours", type=int, default=24)
    st_prop.add_argument("--host-adapter", default="cli")
    st_prop.set_defaults(func=cmd_steering_propose)

    st_show = steering_sub.add_parser("show")
    st_show.add_argument("--pending", action="store_true")
    st_show.set_defaults(func=cmd_steering_show)

    st_apply = steering_sub.add_parser("apply")
    st_apply.add_argument("--proposal-id", required=True)
    st_apply.add_argument("--host-adapter", default="cli")
    st_apply.set_defaults(func=cmd_steering_apply)

    st_roll = steering_sub.add_parser("rollback")
    st_roll.add_argument("--audit-id", required=True)
    st_roll.add_argument("--host-adapter", default="cli")
    st_roll.set_defaults(func=cmd_steering_rollback)

    st_explain = steering_sub.add_parser("explain")
    st_explain.add_argument("--chunk", type=Path)
    st_explain.set_defaults(func=cmd_steering_explain)

    node = sub.add_parser("node")
    node_sub = node.add_subparsers(dest="node_cmd", required=True)
    import goalflight_fleet_node as fleet_node

    node_add = node_sub.add_parser("add", help="Onboard a remote node from an SSH alias")
    node_add.add_argument("--from-ssh", required=True, help="SSH config Host alias")
    node_add.add_argument("--node-id", help="Fleet node id (defaults to alias)")
    node_add.add_argument("--repo-root", help="Absolute repo path on remote host")
    node_add.add_argument("--state-dir", help="Remote goal-flight state dir")
    node_add.add_argument("--billing-accounts", help="Comma-separated account_key values")
    node_add.add_argument("--ssh-config", type=Path, help="Path to ssh config (default ~/.ssh/config)")
    node_add.add_argument("--dry-run", action="store_true")
    node_add.add_argument("--json", action="store_true")
    node_add.set_defaults(func=fleet_node.cmd_node_add)

    import goalflight_fleet_billing as fleet_billing

    account = sub.add_parser("account")
    account_sub = account.add_subparsers(dest="account_cmd", required=True)
    acct_link = account_sub.add_parser("link", help="Link billing account to node and run auth probe")
    acct_link.add_argument("--account-key", required=True)
    acct_link.add_argument("--node", required=True)
    acct_link.add_argument("--skip-probe", action="store_true")
    acct_link.add_argument("--json", action="store_true")
    acct_link.set_defaults(func=fleet_billing.cmd_account_link)
    acct_unlink = account_sub.add_parser("unlink")
    acct_unlink.add_argument("--account-key", required=True)
    acct_unlink.add_argument("--node", required=True)
    acct_unlink.add_argument("--json", action="store_true")
    acct_unlink.set_defaults(func=fleet_billing.cmd_account_unlink)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
