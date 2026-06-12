#!/usr/bin/env python3
"""Fleet store bootstrap, registry lock, export/import, and account locks.

Lock order (mutations): registry lock → account lock → worktree lock → remote capacity lease.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import socket
import sys
import tarfile
import tempfile
import uuid
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import goalflight_compat as fcntl
import goalflight_fleet_schemas as schemas


def default_fleet_dir() -> Path:
    return Path(os.environ.get("GOALFLIGHT_FLEET_DIR", Path.home() / ".goal-flight" / "fleet")).expanduser()


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso(ts: dt.datetime | None = None) -> str:
    return (ts or utc_now()).isoformat(timespec="seconds")


def controller_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def parse_iso(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(path)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text())


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def registry_lock_path(fleet_dir: Path) -> Path:
    path = fleet_dir / "locks" / "fleet-registry.lock"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


class RegistryLock:
    """Exclusive fleet registry lock with optional non-blocking acquire."""

    def __init__(self, fleet_dir: Path, *, blocking: bool = True) -> None:
        self.fleet_dir = fleet_dir
        self.blocking = blocking
        self._fh = None
        self.fencing_token = str(uuid.uuid4())

    def __enter__(self) -> RegistryLock:
        self._fh = registry_lock_path(self.fleet_dir).open("a+")
        flags = fcntl.LOCK_EX
        if not self.blocking:
            flags |= fcntl.LOCK_NB
        try:
            fcntl.flock(self._fh, flags)
        except BlockingIOError as exc:
            self._fh.close()
            self._fh = None
            raise RegistryLockError("registry lock held by another writer") from exc
        payload = {
            "controller_id": controller_id(),
            "fencing_token": self.fencing_token,
            "acquired_at": iso(),
        }
        self._fh.seek(0)
        self._fh.truncate(0)
        self._fh.write(json.dumps(payload) + "\n")
        self._fh.flush()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._fh is not None:
            fcntl.flock(self._fh, fcntl.LOCK_UN)
            self._fh.close()
            self._fh = None


class FleetError(Exception):
    pass


class RegistryLockError(FleetError):
    pass


class AccountLockError(FleetError):
    pass


FLEET_FILES = ("fleet.json", "billing-accounts.json", "steering.json")


def bootstrap(fleet_dir: Path, *, force: bool = False) -> dict[str, str]:
    layout = {
        "fleet.json": schemas.default_fleet_doc(controller_id()),
        "billing-accounts.json": schemas.default_billing_doc(),
        "steering.json": schemas.default_steering_doc(),
    }
    subdirs = (
        "locks",
        "locks/accounts",
        "audit",
        "probes",
        "register",
        "register/dispatches",
    )
    created: dict[str, str] = {}
    fleet_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name in subdirs:
        (fleet_dir / name).mkdir(parents=True, exist_ok=True, mode=0o700)
        created[name + "/"] = "dir"
    for filename, payload in layout.items():
        path = fleet_dir / filename
        if path.exists() and not force:
            created[filename] = "exists"
            continue
        _atomic_write_json(path, payload)
        created[filename] = "created"
    aggregate = fleet_dir / "register" / "aggregate.json"
    if not aggregate.exists() or force:
        _atomic_write_json(
            aggregate,
            {
                "schema": schemas.AGGREGATE_SCHEMA,
                "schema_version": schemas.SCHEMA_VERSION,
                "min_reader_version": schemas.MIN_READER_VERSION,
                "open_user_needs": [],
                "active_dispatches": [],
                "last_steering": None,
            },
        )
        created["register/aggregate.json"] = "created"
    else:
        created["register/aggregate.json"] = "exists"
    return created


def account_lock_path(fleet_dir: Path, account_key: str) -> Path:
    safe = account_key.replace("/", "__")
    return fleet_dir / "locks" / "accounts" / f"{safe}.json"


def _account_lock_file_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fh = path.open("a+")
    fcntl.flock(fh, fcntl.LOCK_EX)
    return fh


def load_account_lock(path: Path) -> dict | None:
    if not path.exists() or path.stat().st_size == 0:
        return None
    doc = read_json(path)
    schemas.validate_account_lock(doc)
    return doc


def account_lock_expired(doc: dict) -> bool:
    expires_at = parse_iso(doc.get("expires_at"))
    return bool(expires_at and expires_at < utc_now())


def acquire_account_lock(
    fleet_dir: Path,
    *,
    account_key: str,
    owner_dispatch_id: str,
    ttl_s: int = 3600,
) -> dict:
    path = account_lock_path(fleet_dir, account_key)
    fh = _account_lock_file_lock(path)
    try:
        existing = load_account_lock(path)
        if existing and existing.get("state") == "active" and not account_lock_expired(existing):
            raise AccountLockError(f"account lock held by {existing.get('owner_dispatch_id')}")
        fencing_token = str(uuid.uuid4())
        now = utc_now()
        doc = {
            "schema": schemas.ACCOUNT_LOCK_SCHEMA,
            "schema_version": schemas.SCHEMA_VERSION,
            "min_reader_version": schemas.MIN_READER_VERSION,
            "account_key": account_key,
            "owner_dispatch_id": owner_dispatch_id,
            "owner_controller_id": controller_id(),
            "fencing_token": fencing_token,
            "remote_lease_id": None,
            "state": "active",
            "expires_at": iso(now + dt.timedelta(seconds=ttl_s)),
            "renewed_at": iso(now),
        }
        _atomic_write_json(path, doc)
        return doc
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


def renew_account_lock(
    fleet_dir: Path,
    *,
    account_key: str,
    owner_dispatch_id: str,
    fencing_token: str,
    ttl_s: int = 3600,
) -> dict:
    path = account_lock_path(fleet_dir, account_key)
    fh = _account_lock_file_lock(path)
    try:
        doc = load_account_lock(path)
        if not doc or doc.get("state") != "active":
            raise AccountLockError("no active account lock")
        if doc.get("owner_dispatch_id") != owner_dispatch_id:
            raise AccountLockError("owner mismatch")
        if doc.get("fencing_token") != fencing_token:
            raise AccountLockError("fencing token mismatch")
        now = utc_now()
        doc["expires_at"] = iso(now + dt.timedelta(seconds=ttl_s))
        doc["renewed_at"] = iso(now)
        _atomic_write_json(path, doc)
        return doc
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


def release_account_lock(
    fleet_dir: Path,
    *,
    account_key: str,
    fencing_token: str,
    reason: str = "released",
) -> dict:
    path = account_lock_path(fleet_dir, account_key)
    fh = _account_lock_file_lock(path)
    try:
        doc = load_account_lock(path)
        if not doc:
            raise AccountLockError("missing account lock")
        if doc.get("fencing_token") != fencing_token:
            raise AccountLockError("fencing token mismatch")
        doc["state"] = "released"
        doc["released_at"] = iso()
        doc["reason"] = reason
        _atomic_write_json(path, doc)
        return doc
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


def stale_account_locks(fleet_dir: Path) -> list[dict]:
    locks_dir = fleet_dir / "locks" / "accounts"
    if not locks_dir.is_dir():
        return []
    stale: list[dict] = []
    for path in locks_dir.glob("*.json"):
        try:
            doc = load_account_lock(path)
        except schemas.SchemaError:
            continue
        if not doc or doc.get("state") != "active":
            continue
        if account_lock_expired(doc):
            stale.append(doc)
    return stale


def release_stale_account_locks(fleet_dir: Path) -> list[str]:
    released: list[str] = []
    for doc in stale_account_locks(fleet_dir):
        account_key = doc.get("account_key")
        fencing_token = doc.get("fencing_token")
        if not account_key or not fencing_token:
            continue
        try:
            release_account_lock(
                fleet_dir,
                account_key=str(account_key),
                fencing_token=str(fencing_token),
                reason="stale_ttl",
            )
            released.append(str(account_key))
        except AccountLockError:
            continue
    return released


def lock_snapshot_metadata(fleet_dir: Path) -> list[dict]:
    locks_dir = fleet_dir / "locks" / "accounts"
    snapshot: list[dict] = []
    if not locks_dir.is_dir():
        return snapshot
    for path in sorted(locks_dir.glob("*.json")):
        try:
            doc = read_json(path)
        except (OSError, json.JSONDecodeError):
            continue
        snapshot.append(
            {
                "path": path.name,
                "account_key": doc.get("account_key"),
                "state": doc.get("state"),
                "owner_dispatch_id": doc.get("owner_dispatch_id"),
                "expires_at": doc.get("expires_at"),
                "fencing_token_prefix": (doc.get("fencing_token") or "")[:8],
            }
        )
    return snapshot


def export_bundle(fleet_dir: Path, out_path: Path) -> dict:
    bootstrap(fleet_dir)
    manifest = {
        "schema": "goalflight.fleet.export.v1",
        "schema_version": 1,
        "exported_at": iso(),
        "controller_id": controller_id(),
        "files": {},
        "lock_snapshots": lock_snapshot_metadata(fleet_dir),
    }
    for name in FLEET_FILES:
        path = fleet_dir / name
        if not path.exists():
            raise FleetError(f"missing fleet file: {path}")
        manifest["files"][name] = sha256_file(path)

    out_path = out_path.expanduser()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        staging = Path(td)
        (staging / "manifest.json").write_bytes(json.dumps(manifest, indent=2).encode() + b"\n")
        for name in FLEET_FILES:
            src = fleet_dir / name
            (staging / name).write_bytes(src.read_bytes())
        with tarfile.open(out_path, "w:gz") as tar:
            for item in ("manifest.json", *FLEET_FILES):
                tar.add(staging / item, arcname=item)
    return manifest


def import_bundle(
    fleet_dir: Path,
    in_path: Path,
    *,
    merge: str = "strict",
) -> dict:
    if merge not in {"strict", "prefer-local"}:
        raise FleetError(f"unsupported merge mode: {merge}")
    bootstrap(fleet_dir)
    with tempfile.TemporaryDirectory() as td:
        staging = Path(td)
        with tarfile.open(in_path.expanduser(), "r:gz") as tar:
            tar.extractall(staging)
        manifest_path = staging / "manifest.json"
        if not manifest_path.exists():
            raise FleetError("export missing manifest.json")
        manifest = read_json(manifest_path)
        incoming_hashes = manifest.get("files") or {}
        local_hashes = {name: sha256_file(fleet_dir / name) for name in FLEET_FILES}
        drift = [
            name
            for name in FLEET_FILES
            if name in incoming_hashes and local_hashes.get(name) != incoming_hashes[name]
        ]
        if merge == "strict" and drift:
            raise FleetError(f"strict merge rejected drift: {', '.join(drift)}")

        with RegistryLock(fleet_dir):
            for name in FLEET_FILES:
                src = staging / name
                if not src.exists():
                    continue
                dest = fleet_dir / name
                if merge == "prefer-local" and name in drift and dest.exists():
                    continue
                incoming = read_json(src)
                schemas.validate_document(incoming)
                _atomic_write_json(dest, incoming)
    return {"ok": True, "merge": merge, "drift": drift}


def reconcile_fleet(fleet_dir: Path, *, release_stale: bool = False) -> dict:
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
            result["capacity_stale_released"] = [lease.get("lease_id") for lease in stale if lease.get("lease_id")]

    result["account_stale"] = len(stale_account_locks(fleet_dir))
    if release_stale:
        result["account_stale_released"] = release_stale_account_locks(fleet_dir)
    return result


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
    dispatch.add_argument("--exec", action="store_true", help="Acquire locks and spawn (default preview)")
    dispatch.add_argument("--stub-remote", action="store_true", help="Use stub SSH runner (tests)")
    dispatch.add_argument("--stub-terminal", action="store_true", help="Complete stub dispatch immediately")
    dispatch.add_argument("--json", action="store_true")
    dispatch.set_defaults(func=cmd_dispatch)

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
