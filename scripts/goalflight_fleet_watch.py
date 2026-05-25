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
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

import goalflight_fleet_mirror as mirror
from goalflight_liveness import write_status


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


def validate_remote_content(raw: str, *, last_seq: int | None) -> mirror.MirrorReadResult:
    """Validate fetched JSON without mutating the controller mirror path."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
        handle.write(raw)
        staging = Path(handle.name)
    try:
        return mirror.read_status_mirror(staging, last_seq=last_seq)
    finally:
        staging.unlink(missing_ok=True)


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


def _mark_meta_ingested(meta_path: Path, *, seq: int) -> None:
    meta = _read_json_object(meta_path)
    meta["last_mirror_seq"] = seq
    meta["mirror_stale"] = False
    meta.pop("mirror_error", None)
    meta.pop("mirror_error_detail", None)
    meta["mirror_ingest_stopped"] = False
    meta.pop("row_state", None)
    _atomic_write_json(meta_path, meta)


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

    if meta.get("mirror_ingest_stopped") is True:
        return DispatchWatchResult(
            dispatch_id,
            ok=False,
            action="skipped",
            detail="mirror ingest stopped after prior seq regression",
        )

    fetch = transport.fetch_remote_status(
        node_id=node_id,
        dispatch_id=dispatch_id,
        remote_status_path=remote_path,
        node_entry=node_entry,
    )
    if not fetch.ok or fetch.content is None:
        return DispatchWatchResult(
            dispatch_id,
            ok=False,
            action="fetch_failed",
            detail=fetch.error or "remote fetch failed",
        )

    last_seq = local_mirror_last_seq(fleet_dir, dispatch_id)
    validated = validate_remote_content(fetch.content, last_seq=last_seq)

    if not validated.ok and validated.error == mirror.ERROR_SEQ_REGRESSION:
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
    _mark_meta_ingested(meta_path, seq=int(validated.last_seq or validated.payload["seq"]))
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
    """One-shot sync of controller mirrors from remote status paths."""
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
    ) -> None:
        self._runner = runner
        self._dry_run = dry_run

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
                status_path=remote_status_path,
            )
            ssh_argv = fleet_ssh.build_ssh_command(host, remote, command_class="read_status_file")
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


def cmd_watch_fleet(args) -> int:
    import goalflight_fleet as fleet

    fleet.bootstrap(args.fleet_dir)
    transport = SshFleetWatchTransport(dry_run=bool(getattr(args, "dry_run", False)))
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
