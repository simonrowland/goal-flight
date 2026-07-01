#!/usr/bin/env python3
"""Fleet store, registry lock, export/import, and account lock primitives.

Lock order (mutations): registry lock -> account lock -> worktree lock -> remote capacity lease.
"""

from __future__ import annotations

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
    return fcntl.resolve_env_path(
        "GOALFLIGHT_FLEET_DIR", Path.home() / ".goal-flight" / "fleet"
    )


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
        "probes/tool-smoke",
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
