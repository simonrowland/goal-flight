#!/usr/bin/env python3
"""Fleet watch mirror ingest for controller-visible dispatch status (Track A goal 10a).

Fetches remote dispatch status JSON via an injectable transport, validates monotonic
``seq`` with ``goalflight_fleet_mirror.read_status_mirror``, and writes mirrors under
``register/dispatches/<id>/status.json`` using the same temp+replace pattern as
``goalflight_liveness.write_status``.

On ``seq_regression``: ingest stops for that dispatch, ``meta.json`` is marked stale /
unknown, and the last good mirror file is left untouched.
"""

from __future__ import annotations

import json
import random
import tempfile
import time
from datetime import datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

import goalflight_fleet_mirror as mirror
import goalflight_fleet_status as fleet_status
import goalflight_dispatch_states as dispatch_states
from goalflight_liveness import write_status

DEFAULT_UNTIL_TIMEOUT_S = 3600.0
DEFAULT_UNTIL_INTERVAL_S = 45.0
DEFAULT_UNTIL_JITTER_S = 5.0
DEFAULT_STALE_S = 300.0
LAUNCH_UNCONFIRMED_GRACE_POLLS = 2
LAUNCH_UNCONFIRMED_GRACE_S = 60.0


class FleetWatchTransport(Protocol):
    """Fetch remote status JSON for one dispatch (hermetic tests inject fakes)."""

    def fetch_remote_status(
        self,
        *,
        node_id: str,
        dispatch_id: str,
        remote_status_path: str,
        node_entry: dict[str, Any] | None,
    ) -> RemoteFetchResult:
        ...


@dataclass(frozen=True)
class RemoteFetchResult:
    ok: bool
    content: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class RemoteIdentityResult:
    ok: bool
    alive: bool = False
    identity: dict[str, Any] | None = None
    error: str | None = None


@dataclass(frozen=True)
class WorktreePorcelainResult:
    ok: bool
    dirty: bool = False
    porcelain: str | None = None
    worktree_path: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class DispatchWatchResult:
    dispatch_id: str
    ok: bool
    action: str
    detail: str | None = None
    seq: int | None = None


@dataclass
class FleetWatchResult:
    dispatches: list[DispatchWatchResult] = field(default_factory=list)
    stopped_dispatch_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dispatches": [
                {
                    "dispatch_id": item.dispatch_id,
                    "ok": item.ok,
                    "action": item.action,
                    "detail": item.detail,
                    "seq": item.seq,
                }
                for item in self.dispatches
            ],
            "stopped_dispatch_ids": list(self.stopped_dispatch_ids),
        }


@dataclass(frozen=True)
class UntilTerminalResult:
    dispatch_id: str
    exit_code: int
    state: str
    polls: int
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "dispatch_id": self.dispatch_id,
            "exit_code": self.exit_code,
            "state": self.state,
            "polls": self.polls,
            "detail": self.detail,
        }


def dispatch_register_dir(fleet_dir: Path) -> Path:
    return fleet_dir / "register" / "dispatches"


def dispatch_status_path(fleet_dir: Path, dispatch_id: str) -> Path:
    return dispatch_register_dir(fleet_dir) / dispatch_id / "status.json"


def dispatch_meta_path(fleet_dir: Path, dispatch_id: str) -> Path:
    return dispatch_register_dir(fleet_dir) / dispatch_id / "meta.json"


def _read_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_now_iso() -> str:
    return _utc_now().isoformat(timespec="seconds")


def _parse_iso_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(path)


def local_mirror_last_seq(fleet_dir: Path, dispatch_id: str) -> int | None:
    """Return seq from the last good on-disk mirror, if readable."""
    result = mirror.read_status_mirror(dispatch_status_path(fleet_dir, dispatch_id))
    return result.last_seq if result.ok else None


def resolve_remote_status_path(
    meta: dict[str, Any],
    *,
    node_entry: dict[str, Any] | None,
    dispatch_id: str,
) -> str | None:
    explicit = meta.get("remote_status_path")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    state_dir = meta.get("remote_state_dir") or (node_entry or {}).get("state_dir")
    if isinstance(state_dir, str) and state_dir.strip():
        base = state_dir.strip().rstrip("/")
        return f"{base}/dispatches/{dispatch_id}/status.json"
    return None


def validate_remote_content(
    raw: str,
    *,
    last_seq: int | None,
    last_epoch: object = mirror.LEGACY_EPOCH,
    last_lineage_identity: object = None,
) -> mirror.MirrorReadResult:
    """Validate fetched JSON without mutating the orchestrator mirror path."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
        handle.write(raw)
        staging = Path(handle.name)
    try:
        return mirror.read_status_mirror(
            staging,
            last_seq=last_seq,
            last_epoch=last_epoch,
            last_lineage_identity=last_lineage_identity,
        )
    finally:
        staging.unlink(missing_ok=True)


def _prepare_remote_content_for_dispatch(raw: str, *, dispatch_id: str) -> tuple[str | None, str | None]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return raw, None
    if not isinstance(payload, dict):
        return raw, None
    payload_dispatch_id = payload.get("dispatch_id")
    if payload_dispatch_id is None:
        payload["dispatch_id"] = dispatch_id
        return json.dumps(payload), None
    if str(payload_dispatch_id) != dispatch_id:
        return None, f"payload dispatch_id {payload_dispatch_id!r} does not match expected {dispatch_id!r}"
    return raw, None


def _mark_meta_seq_regression(meta_path: Path, *, detail: str | None) -> None:
    meta = _read_json_object(meta_path)
    meta["mirror_stale"] = True
    meta["mirror_error"] = mirror.ERROR_SEQ_REGRESSION
    meta["mirror_ingest_stopped"] = True
    meta["row_state"] = "unknown"
    if detail:
        meta["mirror_error_detail"] = detail
    _atomic_write_json(meta_path, meta)


def _mark_meta_validation_error(meta_path: Path, *, error: str, detail: str | None) -> None:
    meta = _read_json_object(meta_path)
    meta["mirror_error"] = error
    if detail:
        meta["mirror_error_detail"] = detail
    _atomic_write_json(meta_path, meta)


def _mark_meta_ingested(
    meta_path: Path,
    *,
    seq: int,
    epoch: object = mirror.LEGACY_EPOCH,
    lineage_identity: dict[str, Any] | None = None,
) -> None:
    meta = _read_json_object(meta_path)
    meta["last_mirror_seq"] = seq
    meta["last_mirror_epoch"] = epoch
    if lineage_identity is None:
        meta.pop("last_mirror_lineage_identity", None)
    else:
        meta["last_mirror_lineage_identity"] = lineage_identity
    meta["mirror_stale"] = False
    meta.pop("mirror_error", None)
    meta.pop("mirror_error_detail", None)
    meta["mirror_ingest_stopped"] = False
    meta.pop("row_state", None)
    _atomic_write_json(meta_path, meta)


def _mark_meta_identity(
    meta_path: Path,
    *,
    pid_hint: str,
    identity_result: RemoteIdentityResult,
) -> None:
    meta = _read_json_object(meta_path)
    meta["pid_hint"] = pid_hint
    meta["last_identity_check_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if identity_result.identity is not None:
        meta["remote_pid_last_identity"] = identity_result.identity
    if identity_result.error:
        meta["remote_pid_identity_error"] = identity_result.error
    else:
        meta.pop("remote_pid_identity_error", None)
    _atomic_write_json(meta_path, meta)


def _mark_launch_unconfirmed_miss(
    meta_path: Path,
    *,
    fetch_error: str | None,
) -> dict[str, Any]:
    meta = _read_json_object(meta_path)
    meta["launch_unconfirmed"] = True
    meta["row_state"] = "launch_unconfirmed"
    meta["launch_unconfirmed_status_misses"] = int(meta.get("launch_unconfirmed_status_misses") or 0) + 1
    meta["launch_unconfirmed_last_miss_at"] = _utc_now_iso()
    meta.setdefault("launch_unconfirmed_first_seen_at", meta["launch_unconfirmed_last_miss_at"])
    if fetch_error:
        meta["launch_unconfirmed_last_fetch_error"] = fetch_error
    _atomic_write_json(meta_path, meta)
    return meta


def _launch_unconfirmed_reference_time(meta: dict[str, Any]) -> datetime | None:
    for key in ("launch_issued_at", "launch_unconfirmed_at", "started_at"):
        parsed = _parse_iso_datetime(meta.get(key))
        if parsed is not None:
            return parsed
    receipt = meta.get("launch_receipt")
    if isinstance(receipt, dict):
        parsed = _parse_iso_datetime(receipt.get("started_at"))
        if parsed is not None:
            return parsed
    return _parse_iso_datetime(meta.get("launch_unconfirmed_first_seen_at"))


def _launch_unconfirmed_grace_elapsed(meta: dict[str, Any]) -> bool:
    misses = int(meta.get("launch_unconfirmed_status_misses") or 0)
    if misses < LAUNCH_UNCONFIRMED_GRACE_POLLS:
        return False
    issued_at = _launch_unconfirmed_reference_time(meta)
    if issued_at is None:
        return False
    return (_utc_now() - issued_at).total_seconds() >= LAUNCH_UNCONFIRMED_GRACE_S


def _coerce_pid(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _identity_from_payload(payload: dict[str, Any], *, pid: int) -> dict[str, Any] | None:
    identity = payload.get("worker_identity") or payload.get("expected_worker_identity")
    if isinstance(identity, dict):
        result = dict(identity)
        result.setdefault("pid", pid)
        return result
    return {"pid": pid}


def _receipt_from_status_payload(
    *,
    dispatch_id: str,
    meta: dict[str, Any],
    payload: dict[str, Any],
    remote_status_path: str,
) -> dict[str, Any] | None:
    pid = _coerce_pid(payload.get("worker_pid") or payload.get("pid"))
    if pid is None:
        return None
    identity = _identity_from_payload(payload, pid=pid)
    base_sha = meta.get("worktree_base_sha") or meta.get("base_sha")
    receipt = {
        "schema": "goalflight.fleet.launch_receipt.v1",
        "dispatch_id": dispatch_id,
        "node_id": str(meta.get("node_id") or ""),
        "remote_pid": pid,
        "remote_lstart": identity.get("lstart") if isinstance(identity, dict) else None,
        "remote_identity": identity,
        "remote_status_path": remote_status_path,
        "remote_state_dir": meta.get("remote_state_dir"),
        "started_at": str(payload.get("updated_at") or datetime.now(timezone.utc).isoformat(timespec="seconds")),
        "recovered": True,
        "recovery_source": "poll_status",
    }
    if isinstance(base_sha, str) and base_sha:
        receipt["worktree_base_sha"] = base_sha
    return receipt


def _receipt_from_meta(meta: dict[str, Any], *, dispatch_id: str) -> dict[str, Any] | None:
    existing = meta.get("launch_receipt")
    if isinstance(existing, dict) and existing.get("remote_pid"):
        receipt = dict(existing)
        receipt.setdefault("dispatch_id", dispatch_id)
        return receipt
    pid = _coerce_pid(meta.get("remote_pid"))
    remote_status_path = meta.get("remote_status_path")
    if pid is None or not isinstance(remote_status_path, str) or not remote_status_path:
        return None
    identity = meta.get("remote_pid_identity")
    if not isinstance(identity, dict):
        identity = {"pid": pid}
    base_sha = meta.get("worktree_base_sha") or meta.get("base_sha")
    receipt = {
        "schema": "goalflight.fleet.launch_receipt.v1",
        "dispatch_id": dispatch_id,
        "node_id": str(meta.get("node_id") or ""),
        "remote_pid": pid,
        "remote_lstart": meta.get("remote_pid_lstart") or identity.get("lstart"),
        "remote_identity": identity,
        "remote_status_path": remote_status_path,
        "remote_state_dir": meta.get("remote_state_dir"),
        "recovered": True,
        "recovery_source": "poll_pid_identity",
    }
    if isinstance(base_sha, str) and base_sha:
        receipt["worktree_base_sha"] = base_sha
    return receipt


def _mark_launch_recovered(
    meta_path: Path,
    *,
    receipt: dict[str, Any],
    seq: int | None,
) -> None:
    meta = _read_json_object(meta_path)
    identity = receipt.get("remote_identity") if isinstance(receipt.get("remote_identity"), dict) else None
    remote_lstart = receipt.get("remote_lstart") or (identity or {}).get("lstart")
    meta["launch_unconfirmed"] = False
    meta.pop("launch_unconfirmed_error", None)
    meta["launch_receipt"] = receipt
    meta["launch_recovered"] = True
    meta["launch_recovered_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    meta["row_state"] = "launch_receipted"
    meta["pid_hint"] = "alive"
    meta["remote_pid"] = receipt.get("remote_pid")
    meta["remote_pid_lstart"] = remote_lstart
    meta["remote_pid_identity"] = identity
    meta["remote_status_path"] = receipt.get("remote_status_path") or meta.get("remote_status_path")
    if seq is not None:
        meta["last_mirror_seq"] = seq
    _atomic_write_json(meta_path, meta)


def _write_launch_failed_and_release(
    fleet_dir: Path,
    dispatch_id: str,
    *,
    reason: str,
) -> DispatchWatchResult:
    import goalflight_fleet_reconcile as fleet_reconcile

    prior = _read_local_mirror(fleet_dir, dispatch_id)
    seq = int(prior.last_seq or 0) + 1
    payload = {
        "schema": mirror.STATUS_MIRROR_SCHEMA,
        "seq": seq,
        "dispatch_id": dispatch_id,
        "state": "failed",
        "reason": reason,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    write_status(dispatch_status_path(fleet_dir, dispatch_id), payload)
    meta_path = dispatch_meta_path(fleet_dir, dispatch_id)
    meta = _read_json_object(meta_path)
    meta["launch_unconfirmed"] = False
    meta["row_state"] = "failed"
    meta["pid_hint"] = "dead"
    meta["last_mirror_seq"] = seq
    meta["launch_resolution"] = reason
    _atomic_write_json(meta_path, meta)
    fleet_reconcile.reconcile_dispatch(fleet_dir, dispatch_id, mutate=True, ssh_reachable=True)
    return DispatchWatchResult(
        dispatch_id,
        ok=True,
        action="launch_unconfirmed_failed",
        detail=reason,
        seq=seq,
    )


def _resolve_unconfirmed_without_status(
    fleet_dir: Path,
    dispatch_id: str,
    meta: dict[str, Any],
    transport: FleetWatchTransport,
    *,
    node_entry: dict[str, Any] | None,
    fetch_error: str | None,
) -> DispatchWatchResult:
    meta_path = dispatch_meta_path(fleet_dir, dispatch_id)
    meta = _mark_launch_unconfirmed_miss(meta_path, fetch_error=fetch_error)
    status_absent = not dispatch_status_path(fleet_dir, dispatch_id).exists()
    receipt = _receipt_from_meta(meta, dispatch_id=dispatch_id)
    if receipt:
        identity = _identity_check(
            transport,
            node_id=str(meta.get("node_id") or ""),
            node_entry=node_entry,
            receipt=receipt,
        )
        _mark_meta_identity(
            meta_path,
            pid_hint="alive" if identity.ok and identity.alive else "dead" if identity.ok else "unknown",
            identity_result=identity,
        )
        if identity.ok and identity.alive:
            return DispatchWatchResult(
                dispatch_id,
                ok=True,
                action="launch_unconfirmed",
                detail="launch_unconfirmed live pid waiting for status",
                seq=local_mirror_last_seq(fleet_dir, dispatch_id),
            )
        latest_meta = _read_json_object(meta_path)
        if identity.ok and not identity.alive and status_absent and _launch_unconfirmed_grace_elapsed(latest_meta):
            return _write_launch_failed_and_release(
                fleet_dir,
                dispatch_id,
                reason="launch_unconfirmed_no_status_dead_pid_after_grace",
            )
        if identity.ok and not identity.alive:
            return DispatchWatchResult(
                dispatch_id,
                ok=True,
                action="launch_unconfirmed",
                detail="launch_unconfirmed dead pid proof pending grace",
                seq=local_mirror_last_seq(fleet_dir, dispatch_id),
            )
        if not identity.ok:
            return DispatchWatchResult(
                dispatch_id,
                ok=True,
                action="launch_unconfirmed",
                detail=identity.error or "launch_unconfirmed identity proof unavailable",
                seq=local_mirror_last_seq(fleet_dir, dispatch_id),
            )
    return DispatchWatchResult(
        dispatch_id,
        ok=True,
        action="launch_unconfirmed",
        detail="launch_unconfirmed waiting for status or no-worker proof",
        seq=local_mirror_last_seq(fleet_dir, dispatch_id),
    )


def ingest_dispatch_mirror(
    fleet_dir: Path,
    dispatch_id: str,
    meta: dict[str, Any],
    transport: FleetWatchTransport,
    *,
    node_entry: dict[str, Any] | None = None,
) -> DispatchWatchResult:
    """Fetch, validate, and optionally write one dispatch status mirror."""
    node_id = str(meta.get("node_id") or (node_entry or {}).get("node_id") or "unknown")
    remote_path = resolve_remote_status_path(meta, node_entry=node_entry, dispatch_id=dispatch_id)
    meta_path = dispatch_meta_path(fleet_dir, dispatch_id)
    status_path = dispatch_status_path(fleet_dir, dispatch_id)

    if not remote_path:
        return DispatchWatchResult(
            dispatch_id,
            ok=False,
            action="skipped",
            detail="remote_status_path not configured",
        )

    stopped_after_regression = meta.get("mirror_ingest_stopped") is True

    fetch = transport.fetch_remote_status(
        node_id=node_id,
        dispatch_id=dispatch_id,
        remote_status_path=remote_path,
        node_entry=node_entry,
    )
    if not fetch.ok or fetch.content is None:
        if meta.get("launch_unconfirmed") is True:
            return _resolve_unconfirmed_without_status(
                fleet_dir,
                dispatch_id,
                meta,
                transport,
                node_entry=node_entry,
                fetch_error=fetch.error,
            )
        return DispatchWatchResult(
            dispatch_id,
            ok=False,
            action="fetch_failed",
            detail=fetch.error or "remote fetch failed",
        )

    last_mirror = mirror.read_status_mirror(status_path)
    last_seq = last_mirror.last_seq if last_mirror.ok else None
    last_epoch = last_mirror.epoch if last_mirror.ok else mirror.LEGACY_EPOCH
    last_lineage_identity = (
        last_mirror.lineage_identity
        if last_mirror.ok
        else meta.get("last_mirror_lineage_identity")
    )
    prepared_content, dispatch_id_error = _prepare_remote_content_for_dispatch(
        fetch.content,
        dispatch_id=dispatch_id,
    )
    if dispatch_id_error:
        _mark_meta_validation_error(
            meta_path,
            error="dispatch_id_mismatch",
            detail=dispatch_id_error,
        )
        return DispatchWatchResult(
            dispatch_id,
            ok=False,
            action="dispatch_id_mismatch",
            detail=dispatch_id_error,
            seq=last_seq,
        )
    assert prepared_content is not None
    validated = validate_remote_content(
        prepared_content,
        last_seq=last_seq,
        last_epoch=last_epoch,
        last_lineage_identity=last_lineage_identity,
    )

    if not validated.ok and validated.error == mirror.ERROR_SEQ_REGRESSION:
        if validated.last_seq == last_seq and validated.epoch == last_epoch:
            if stopped_after_regression:
                _mark_meta_ingested(
                    meta_path,
                    seq=int(validated.last_seq or 0),
                    epoch=validated.epoch,
                    lineage_identity=validated.lineage_identity,
                )
            return DispatchWatchResult(
                dispatch_id,
                ok=True,
                action="unchanged",
                detail=validated.detail,
                seq=validated.last_seq,
            )
        _mark_meta_seq_regression(meta_path, detail=validated.detail)
        return DispatchWatchResult(
            dispatch_id,
            ok=False,
            action="seq_regression",
            detail=validated.detail,
            seq=validated.last_seq,
        )

    if not validated.ok:
        _mark_meta_validation_error(meta_path, error=str(validated.error), detail=validated.detail)
        return DispatchWatchResult(
            dispatch_id,
            ok=False,
            action="validation_failed",
            detail=validated.detail,
            seq=validated.last_seq,
        )

    assert validated.payload is not None
    write_status(status_path, validated.payload)
    _mark_meta_ingested(
        meta_path,
        seq=int(validated.last_seq or validated.payload["seq"]),
        epoch=validated.epoch,
        lineage_identity=validated.lineage_identity,
    )
    if meta.get("launch_unconfirmed") is True:
        receipt = _receipt_from_status_payload(
            dispatch_id=dispatch_id,
            meta=meta,
            payload=validated.payload,
            remote_status_path=remote_path,
        )
        if receipt is not None:
            _mark_launch_recovered(meta_path, receipt=receipt, seq=validated.last_seq)
    return DispatchWatchResult(
        dispatch_id,
        ok=True,
        action="ingested",
        seq=validated.last_seq,
    )


def collect_watch_targets(fleet_dir: Path) -> dict[str, dict[str, Any]]:
    """Dispatch id -> meta for rows eligible for mirror watch."""
    import goalflight_fleet_status_cli as status_cli

    return status_cli._collect_dispatch_meta(fleet_dir)


def sync_fleet_mirrors(
    fleet_dir: Path,
    transport: FleetWatchTransport,
    *,
    dispatch_ids: list[str] | None = None,
) -> FleetWatchResult:
    """One-shot sync of orchestrator mirrors from remote status paths."""
    import goalflight_fleet as fleet

    result = FleetWatchResult()
    targets = collect_watch_targets(fleet_dir)
    if dispatch_ids is not None:
        targets = {dispatch_id: targets[dispatch_id] for dispatch_id in dispatch_ids if dispatch_id in targets}

    fleet_doc = _read_json_object(fleet_dir / "fleet.json")
    nodes = fleet_doc.get("nodes") or {}

    for dispatch_id, meta in sorted(targets.items()):
        node_id = str(meta.get("node_id") or "")
        node_entry = nodes.get(node_id) if isinstance(nodes.get(node_id), dict) else None
        row = ingest_dispatch_mirror(
            fleet_dir,
            dispatch_id,
            meta,
            transport,
            node_entry=node_entry,
        )
        result.dispatches.append(row)
        if row.action == "seq_regression":
            result.stopped_dispatch_ids.append(dispatch_id)
    return result


class SshFleetWatchTransport:
    """Default transport: allowlisted SSH ``read_status_file`` + cat."""

    def __init__(
        self,
        *,
        runner: Callable[[list[str]], tuple[int, str, str]] | None = None,
        dry_run: bool = False,
        fleet_dir: Path | None = None,
    ) -> None:
        self._runner = runner
        self._dry_run = dry_run
        self._fleet_dir = fleet_dir

    def fetch_remote_status(
        self,
        *,
        node_id: str,
        dispatch_id: str,
        remote_status_path: str,
        node_entry: dict[str, Any] | None,
    ) -> RemoteFetchResult:
        import goalflight_fleet_ssh as fleet_ssh

        if node_entry is None:
            return RemoteFetchResult(ok=False, error=f"unknown fleet node: {node_id}")

        ssh_info = node_entry.get("ssh") or {}
        alias = ssh_info.get("alias") or node_id
        try:
            host = fleet_ssh.host_from_node_entry(node_id, node_entry)
            repo_root = str(node_entry.get("repo_root") or "")
            remote = fleet_ssh.build_remote_command(
                "read_status_file",
                repo_root=repo_root,
                state_dir=str(node_entry.get("state_dir") or "~/.goal-flight"),
                status_path=remote_status_path,
            )
            ssh_argv = fleet_ssh.build_ssh_command(host, remote, command_class="read_status_file")
            with fleet_ssh.node_ssh_lock(node_id, fleet_dir=self._fleet_dir):
                run = fleet_ssh.run_ssh(ssh_argv, runner=self._runner, dry_run=self._dry_run)
        except fleet_ssh.SshAllowlistError as exc:
            return RemoteFetchResult(ok=False, error=str(exc))

        if not run.get("ok"):
            stderr = (run.get("stderr") or "").strip()
            return RemoteFetchResult(
                ok=False,
                error=stderr or f"ssh exit {run.get('exit_code')}",
            )
        stdout = run.get("stdout") or ""
        if not stdout.strip():
            return RemoteFetchResult(ok=False, error="remote status empty")
        return RemoteFetchResult(ok=True, content=stdout)

    def check_remote_identity(
        self,
        *,
        node_id: str,
        node_entry: dict[str, Any] | None,
        receipt: dict[str, Any],
    ) -> RemoteIdentityResult:
        import goalflight_fleet_ssh as fleet_ssh

        if node_entry is None:
            return RemoteIdentityResult(ok=False, error=f"unknown fleet node: {node_id}")
        try:
            pid = int(receipt.get("remote_pid"))
        except (TypeError, ValueError):
            return RemoteIdentityResult(ok=False, error="launch receipt missing remote_pid")
        try:
            host = fleet_ssh.host_from_node_entry(node_id, node_entry)
            remote = fleet_ssh.build_remote_command(
                "pid_identity",
                repo_root=str(node_entry.get("repo_root") or ""),
                python=str(node_entry.get("python") or "python3"),
                pid=str(pid),
                expected_lstart=str(receipt.get("remote_lstart") or ""),
            )
            ssh_argv = fleet_ssh.build_ssh_command(host, remote, command_class="pid_identity")
            with fleet_ssh.node_ssh_lock(node_id, fleet_dir=self._fleet_dir):
                run = fleet_ssh.run_ssh(ssh_argv, runner=self._runner, dry_run=self._dry_run)
        except fleet_ssh.SshAllowlistError as exc:
            return RemoteIdentityResult(ok=False, error=str(exc))

        if not run.get("ok"):
            stderr = (run.get("stderr") or "").strip()
            return RemoteIdentityResult(ok=False, error=stderr or f"ssh exit {run.get('exit_code')}")
        try:
            payload = json.loads(str(run.get("stdout") or "").strip())
        except json.JSONDecodeError as exc:
            return RemoteIdentityResult(ok=False, error=f"invalid identity JSON: {exc}")
        if not isinstance(payload, dict):
            return RemoteIdentityResult(ok=False, error="identity response must be a JSON object")
        return RemoteIdentityResult(
            ok=True,
            alive=bool(payload.get("alive")),
            identity=payload.get("identity") if isinstance(payload.get("identity"), dict) else None,
        )

    def check_worktree_porcelain(
        self,
        *,
        node_id: str,
        node_entry: dict[str, Any] | None,
        worktree_path: str,
    ) -> WorktreePorcelainResult:
        import goalflight_fleet_ssh as fleet_ssh

        if node_entry is None:
            return WorktreePorcelainResult(
                ok=False,
                worktree_path=worktree_path,
                error=f"unknown fleet node: {node_id}",
            )
        try:
            repo_root = str(node_entry.get("repo_root") or worktree_path)
            allowed_roots = _remote_allowed_roots(node_entry)
            host = fleet_ssh.host_from_node_entry(node_id, node_entry)
            remote = fleet_ssh.build_remote_command(
                "git_status_porcelain",
                repo_root=repo_root,
                worktree_path=worktree_path,
                allowed_roots=allowed_roots,
            )
            ssh_argv = fleet_ssh.build_ssh_command(host, remote, command_class="git_status_porcelain")
            with fleet_ssh.node_ssh_lock(node_id, fleet_dir=self._fleet_dir):
                run = fleet_ssh.run_ssh(ssh_argv, runner=self._runner, dry_run=self._dry_run)
        except fleet_ssh.SshAllowlistError as exc:
            return WorktreePorcelainResult(ok=False, worktree_path=worktree_path, error=str(exc))

        if not run.get("ok"):
            stderr = (run.get("stderr") or "").strip()
            return WorktreePorcelainResult(
                ok=False,
                worktree_path=worktree_path,
                error=stderr or f"ssh exit {run.get('exit_code')}",
            )
        stdout = str(run.get("stdout") or "")
        porcelain = stdout.strip()
        return WorktreePorcelainResult(
            ok=True,
            dirty=bool(porcelain),
            porcelain=porcelain or None,
            worktree_path=worktree_path,
        )


def _remote_allowed_roots(node_entry: dict[str, Any]) -> list[str]:
    repo_root = str(node_entry.get("repo_root") or "").strip()
    state_dir = str(node_entry.get("state_dir") or "").strip()
    return [
        root
        for root in (
            repo_root,
            state_dir,
            f"{state_dir.rstrip('/')}/worktrees" if state_dir else "",
        )
        if root
    ]


def _declared_worktree_path(meta: dict[str, Any]) -> str | None:
    worktree_path = meta.get("worktree_path")
    if isinstance(worktree_path, str) and worktree_path.strip():
        return worktree_path.strip()
    return None


def _worktree_porcelain_check(
    transport: FleetWatchTransport,
    *,
    node_id: str,
    node_entry: dict[str, Any] | None,
    worktree_path: str,
) -> WorktreePorcelainResult:
    checker = getattr(transport, "check_worktree_porcelain", None)
    if checker is None:
        return WorktreePorcelainResult(
            ok=False,
            worktree_path=worktree_path,
            error="transport has no porcelain checker",
        )
    return checker(node_id=node_id, node_entry=node_entry, worktree_path=worktree_path)


def _read_local_mirror(fleet_dir: Path, dispatch_id: str) -> mirror.MirrorReadResult:
    return mirror.read_status_mirror(dispatch_status_path(fleet_dir, dispatch_id))


def _mirror_state(result: mirror.MirrorReadResult) -> str | None:
    if result.ok and result.payload:
        state = result.payload.get("state")
        if isinstance(state, str):
            return state
    return None


def _write_worker_dead_mirror(
    fleet_dir: Path,
    dispatch_id: str,
    *,
    prior: mirror.MirrorReadResult,
    identity_result: RemoteIdentityResult,
) -> None:
    seq = int(prior.last_seq or 0) + 1
    payload = {
        "schema": mirror.STATUS_MIRROR_SCHEMA,
        "seq": seq,
        "dispatch_id": dispatch_id,
        "state": "worker_dead",
        "reason": "remote_launch_pid_dead",
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if prior.ok and prior.payload:
        for key in ("agent", "tail_path", "worker_pid"):
            if key in prior.payload:
                payload[key] = prior.payload[key]
    write_status(dispatch_status_path(fleet_dir, dispatch_id), payload)
    meta_path = dispatch_meta_path(fleet_dir, dispatch_id)
    meta = _read_json_object(meta_path)
    meta["last_mirror_seq"] = seq
    meta["mirror_stale"] = False
    meta["pid_hint"] = "dead"
    meta["row_state"] = "worker_dead"
    meta["worker_dead_reason"] = "remote_launch_pid_dead"
    if identity_result.identity is not None:
        meta["remote_pid_last_identity"] = identity_result.identity
    _atomic_write_json(meta_path, meta)


def _write_salvage_needed_mirror(
    fleet_dir: Path,
    dispatch_id: str,
    *,
    prior: mirror.MirrorReadResult,
    identity_result: RemoteIdentityResult,
    worktree_path: str | None,
    porcelain_result: WorktreePorcelainResult,
    reason: str,
) -> None:
    seq = int(prior.last_seq or 0) + 1
    payload: dict[str, Any] = {
        "schema": mirror.STATUS_MIRROR_SCHEMA,
        "seq": seq,
        "dispatch_id": dispatch_id,
        "state": "salvage_needed",
        "reason": reason,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if worktree_path:
        payload["worktree_path"] = worktree_path
    if porcelain_result.porcelain:
        payload["porcelain"] = porcelain_result.porcelain
    if porcelain_result.error:
        payload["porcelain_error"] = porcelain_result.error
    if prior.ok and prior.payload:
        for key in ("agent", "tail_path", "worker_pid"):
            if key in prior.payload:
                payload[key] = prior.payload[key]
    write_status(dispatch_status_path(fleet_dir, dispatch_id), payload)
    meta_path = dispatch_meta_path(fleet_dir, dispatch_id)
    meta = _read_json_object(meta_path)
    meta["last_mirror_seq"] = seq
    meta["mirror_stale"] = False
    meta["pid_hint"] = "dead"
    meta["row_state"] = "salvage_needed"
    meta["salvage_reason"] = reason
    if worktree_path:
        meta["worktree_path"] = worktree_path
    if porcelain_result.porcelain:
        meta["worktree_porcelain"] = porcelain_result.porcelain
    if porcelain_result.error:
        meta["worktree_porcelain_error"] = porcelain_result.error
    if identity_result.identity is not None:
        meta["remote_pid_last_identity"] = identity_result.identity
    _atomic_write_json(meta_path, meta)


def _failure_terminal_needs_salvage_check(state: str | None) -> bool:
    return bool(state and state in dispatch_states.TERMINAL_FAILURE_STATES)


def _salvage_needed_for_dirty_worktree(
    fleet_dir: Path,
    dispatch_id: str,
    transport: FleetWatchTransport,
    *,
    node_id: str,
    node_entry: dict[str, Any] | None,
    meta: dict[str, Any],
    prior: mirror.MirrorReadResult,
    identity_result: RemoteIdentityResult,
    polls: int,
    reason_prefix: str,
    detail_subject: str,
) -> UntilTerminalResult | None:
    worktree_path = _declared_worktree_path(meta)
    if worktree_path is None:
        porcelain = WorktreePorcelainResult(
            ok=False,
            error="dispatch meta missing declared worktree_path",
        )
        _write_salvage_needed_mirror(
            fleet_dir,
            dispatch_id,
            prior=prior,
            identity_result=identity_result,
            worktree_path=None,
            porcelain_result=porcelain,
            reason=f"{reason_prefix}_worktree_unknown",
        )
        return UntilTerminalResult(
            dispatch_id,
            0,
            "salvage_needed",
            polls,
            porcelain.error,
        )

    porcelain = _worktree_porcelain_check(
        transport,
        node_id=node_id,
        node_entry=node_entry,
        worktree_path=worktree_path,
    )
    if not porcelain.ok or porcelain.dirty:
        salvage_reason = (
            f"{reason_prefix}_dirty_worktree"
            if porcelain.ok and porcelain.dirty
            else f"{reason_prefix}_porcelain_unavailable"
        )
        _write_salvage_needed_mirror(
            fleet_dir,
            dispatch_id,
            prior=prior,
            identity_result=identity_result,
            worktree_path=worktree_path,
            porcelain_result=porcelain,
            reason=salvage_reason,
        )
        detail_msg = (
            f"{detail_subject} with dirty worktree: {worktree_path}"
            if porcelain.ok and porcelain.dirty
            else porcelain.error or "remote worktree porcelain check failed"
        )
        return UntilTerminalResult(
            dispatch_id,
            0,
            "salvage_needed",
            polls,
            detail_msg,
        )
    return None


def _identity_check(
    transport: FleetWatchTransport,
    *,
    node_id: str,
    node_entry: dict[str, Any] | None,
    receipt: dict[str, Any],
) -> RemoteIdentityResult:
    checker = getattr(transport, "check_remote_identity", None)
    if checker is None:
        return RemoteIdentityResult(ok=False, error="transport has no identity checker")
    return checker(node_id=node_id, node_entry=node_entry, receipt=receipt)


def watch_until_terminal(
    fleet_dir: Path,
    dispatch_id: str,
    transport: FleetWatchTransport,
    *,
    timeout_s: float = DEFAULT_UNTIL_TIMEOUT_S,
    interval_s: float = DEFAULT_UNTIL_INTERVAL_S,
    jitter_s: float = DEFAULT_UNTIL_JITTER_S,
    stale_s: float = DEFAULT_STALE_S,
    sleep_fn: Callable[[float], None] = time.sleep,
    monotonic_fn: Callable[[], float] = time.monotonic,
) -> UntilTerminalResult:
    """Poll one remote status mirror until terminal, timeout, or ambiguity."""
    import goalflight_fleet as fleet

    deadline = monotonic_fn() + max(float(timeout_s), 0.0)
    polls = 0
    last_progress_at = monotonic_fn()
    identity_checked_seq: int | None = None
    detail: str | None = None

    while True:
        meta = _read_json_object(dispatch_meta_path(fleet_dir, dispatch_id))
        if not meta:
            return UntilTerminalResult(dispatch_id, 2, "unknown", polls, "dispatch meta not found")
        fleet_doc = fleet.read_json(fleet_dir / "fleet.json")
        node_id = str(meta.get("node_id") or "")
        node_entry = (fleet_doc.get("nodes") or {}).get(node_id)
        if not isinstance(node_entry, dict):
            node_entry = None

        before = _read_local_mirror(fleet_dir, dispatch_id)
        before_seq = before.last_seq if before.ok else None
        row = ingest_dispatch_mirror(
            fleet_dir,
            dispatch_id,
            meta,
            transport,
            node_entry=node_entry,
        )
        polls += 1
        after = _read_local_mirror(fleet_dir, dispatch_id)
        state = _mirror_state(after)
        after_seq = after.last_seq if after.ok else before_seq

        if after.ok and after_seq != before_seq:
            last_progress_at = monotonic_fn()
            identity_checked_seq = None

        if row.action in {"ingested", "unchanged"} and _failure_terminal_needs_salvage_check(state):
            latest = _read_local_mirror(fleet_dir, dispatch_id)
            salvage = _salvage_needed_for_dirty_worktree(
                fleet_dir,
                dispatch_id,
                transport,
                node_id=node_id,
                node_entry=node_entry,
                meta=meta,
                prior=latest,
                identity_result=RemoteIdentityResult(ok=True, alive=False, identity=None),
                polls=polls,
                reason_prefix="self_reported_failure_terminal",
                detail_subject=f"self-reported {state} terminal",
            )
            if salvage is not None:
                return salvage

        if fleet_status.is_terminal_state(state):
            return UntilTerminalResult(dispatch_id, 0, str(state), polls, row.detail)

        if not row.ok and row.action not in {"fetch_failed"}:
            detail = row.detail or row.action

        stale_for = monotonic_fn() - last_progress_at
        receipt = meta.get("launch_receipt") if isinstance(meta.get("launch_receipt"), dict) else {}
        if (
            meta.get("launch_unconfirmed") is not True
            and stale_for >= max(float(stale_s), 0.0)
            and identity_checked_seq != after_seq
            and receipt
        ):
            identity = _identity_check(transport, node_id=node_id, node_entry=node_entry, receipt=receipt)
            _mark_meta_identity(
                dispatch_meta_path(fleet_dir, dispatch_id),
                pid_hint="alive" if identity.ok and identity.alive else "dead" if identity.ok else "unknown",
                identity_result=identity,
            )
            identity_checked_seq = after_seq
            if identity.ok and not identity.alive:
                latest = _read_local_mirror(fleet_dir, dispatch_id)
                if not fleet_status.is_terminal_state(_mirror_state(latest)):
                    salvage = _salvage_needed_for_dirty_worktree(
                        fleet_dir,
                        dispatch_id,
                        transport,
                        node_id=node_id,
                        node_entry=node_entry,
                        meta=meta,
                        prior=latest,
                        identity_result=identity,
                        polls=polls,
                        reason_prefix="remote_launch_pid_dead",
                        detail_subject="remote launch pid dead",
                    )
                    if salvage is not None:
                        return salvage
                    _write_worker_dead_mirror(
                        fleet_dir,
                        dispatch_id,
                        prior=latest,
                        identity_result=identity,
                    )
                    return UntilTerminalResult(
                        dispatch_id,
                        0,
                        "worker_dead",
                        polls,
                        "remote launch pid identity is dead",
                    )
            elif not identity.ok:
                detail = identity.error or "identity check failed"

        now = monotonic_fn()
        if now >= deadline:
            latest = _read_local_mirror(fleet_dir, dispatch_id)
            latest_state = _mirror_state(latest)
            if fleet_status.is_terminal_state(latest_state):
                return UntilTerminalResult(dispatch_id, 0, str(latest_state), polls, detail)
            if latest.ok and fleet_status.is_running_state(latest_state):
                return UntilTerminalResult(dispatch_id, 1, str(latest_state), polls, detail or "timeout")
            latest_meta = _read_json_object(dispatch_meta_path(fleet_dir, dispatch_id))
            if latest_meta.get("launch_unconfirmed") is True:
                return UntilTerminalResult(
                    dispatch_id,
                    1,
                    "launch_unconfirmed",
                    polls,
                    detail or "launch_unconfirmed waiting for status or no-worker proof",
                )
            return UntilTerminalResult(dispatch_id, 2, str(latest_state or "unknown"), polls, detail or "timeout")

        sleep_for = max(float(interval_s), 0.0)
        if jitter_s:
            sleep_for += random.uniform(0, max(float(jitter_s), 0.0))
        sleep_fn(min(sleep_for, max(deadline - now, 0.0)))


def release_lock_on_confirmed_terminal(fleet_dir: Path, dispatch_id: str, state: str) -> bool:
    """Release a dispatch's account lock once it is CONFIRMED terminal. Best-effort; never raises.

    Closes the lock-lifecycle gap where a terminal dispatch stranded its account lock until the
    ~1h TTL because the only release path (reconcile) saw a stale mirror. Here the watcher has
    FRESH terminal confirmation, so releasing is safe — no stale-mirror guessing. Guards:
    - only on a confirmed terminal state (caller passes exit_code==0 results);
    - NEVER for a salvage-needed terminal (a dirty worktree must be salvaged before the lock frees);
    - only OUR own currently-active lock (owner_dispatch_id match) — never another dispatch's.
    Returns True iff a release was performed.
    """
    try:
        # Self-safe: release ONLY a genuinely terminal, non-salvage state, independent of the
        # caller's gating — never release a running/unknown dispatch's lock.
        if not state or not fleet_status.is_terminal_state(state):
            return False
        if state in fleet_status.SALVAGE_NEEDED_STATES:
            return False
        import goalflight_fleet as fleet
        import goalflight_fleet_reconcile as fleet_reconcile

        meta = _read_json_object(dispatch_meta_path(fleet_dir, dispatch_id))
        lock = fleet_reconcile.resolve_account_lock_for_dispatch(fleet_dir, dispatch_id, meta)
        if not isinstance(lock, dict) or lock.get("state") != "active":
            return False
        if lock.get("owner_dispatch_id") != dispatch_id:
            return False  # someone else's lock — never release it
        account_key = lock.get("account_key")
        fencing_token = lock.get("fencing_token")
        if not account_key or not fencing_token:
            return False
        fleet.release_account_lock(
            fleet_dir,
            account_key=str(account_key),
            fencing_token=str(fencing_token),
            reason=f"terminal_confirmed:{state}",
        )
        return True
    except Exception:
        return False


def cmd_watch_fleet(args) -> int:
    import goalflight_fleet as fleet

    fleet.bootstrap(args.fleet_dir)
    transport = SshFleetWatchTransport(
        dry_run=bool(getattr(args, "dry_run", False)),
        fleet_dir=args.fleet_dir,
    )
    until_terminal = getattr(args, "until_terminal", None)
    if until_terminal:
        result = watch_until_terminal(
            args.fleet_dir,
            str(until_terminal),
            transport,
            timeout_s=float(getattr(args, "timeout_s", DEFAULT_UNTIL_TIMEOUT_S)),
            interval_s=float(getattr(args, "interval", 0.0) or DEFAULT_UNTIL_INTERVAL_S),
            stale_s=float(getattr(args, "stale_s", DEFAULT_STALE_S)),
        )
        if result.exit_code == 0:
            # Confirmed terminal -> free the account lock now (don't strand it until TTL
            # waiting on a reconcile that may only ever see a stale mirror).
            release_lock_on_confirmed_terminal(args.fleet_dir, str(until_terminal), result.state)
        if args.json:
            print(json.dumps(result.to_dict(), indent=2))
        else:
            detail = f" ({result.detail})" if result.detail else ""
            print(f"{result.dispatch_id}\t{result.state}\texit={result.exit_code}{detail}")
        return result.exit_code
    if args.interval and args.interval > 0 and not args.once:
        import time

        while True:
            result = sync_fleet_mirrors(args.fleet_dir, transport)
            if args.json:
                print(json.dumps(result.to_dict(), indent=2))
            else:
                _print_watch_summary(result)
            time.sleep(args.interval)
    else:
        result = sync_fleet_mirrors(args.fleet_dir, transport)
        if args.json:
            print(json.dumps(result.to_dict(), indent=2))
        else:
            _print_watch_summary(result)
    return 0


def _print_watch_summary(result: FleetWatchResult) -> None:
    for row in result.dispatches:
        detail = f" ({row.detail})" if row.detail else ""
        print(f"{row.dispatch_id}\t{row.action}\tok={row.ok}{detail}")
    if result.stopped_dispatch_ids:
        print(f"stopped_ingest={','.join(result.stopped_dispatch_ids)}")
