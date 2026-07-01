#!/usr/bin/env python3
"""Fleet tool-smoke canary cache and gate.

The canary is intentionally cheap: one ACP turn that forces native file reads
inside the same node/agent/sandbox/worktree shape used by fleet dispatch.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any, Callable

import goalflight_fleet_ssh as fleet_ssh

TOOL_SMOKE_SCHEMA = "goalflight.fleet.tool-smoke.v1"
DEFAULT_TTL_S = 24 * 60 * 60
DEFAULT_SANDBOX = "read-only"
DEFAULT_WORKTREE_SHAPE = "detached-base"
DEFAULT_AGENT_MODEL_VERSION = {
    "grok-acp": "grok-composer-2.5-fast",
}

ProbeRunner = Callable[[list[str]], tuple[int, str, str]]

_SAFE_PART_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_TOOL_ERROR_RE = re.compile(r"\b(tool_output_error|tool_error|tool_result_error)\b", re.I)
_READ_TOOL_RE = re.compile(
    r"(?:tool_name|effective_tool_name)[=:\s\"']+Read\b|\bRead\b",
    re.I,
)
_MODEL_ID_RE = re.compile(r"model_id[=:\s\"']+([A-Za-z0-9_.:/@+-]+)")


class ToolSmokeGateError(Exception):
    def __init__(
        self,
        message: str,
        *,
        cache_state: str,
        record: dict[str, Any] | None = None,
    ) -> None:
        self.cache_state = cache_state
        self.record = record
        super().__init__(message)


class ToolSmokeRunError(Exception):
    pass


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso(ts: dt.datetime | None = None) -> str:
    return (ts or utc_now()).isoformat(timespec="seconds")


def parse_iso(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _safe_part(value: str) -> str:
    safe = _SAFE_PART_RE.sub("_", value.strip()).strip("._")
    return safe[:80] or "unknown"


def _canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def build_identity(
    *,
    node_id: str,
    agent: str,
    base_sha: str,
    sandbox: str = DEFAULT_SANDBOX,
    model_version: str | None = None,
    worktree_identity: str | None = None,
    worktree_shape: str = DEFAULT_WORKTREE_SHAPE,
) -> dict[str, Any]:
    base = str(base_sha or "").strip().lower()
    return {
        "node_id": str(node_id),
        "agent": str(agent),
        "model_version": str(model_version or "") or None,
        "base_sha": base,
        "worktree_identity": worktree_identity or f"{worktree_shape}:{base}",
        "worktree_shape": worktree_shape,
        "sandbox": str(sandbox or DEFAULT_SANDBOX),
    }


def resolve_model_version(agent: str, model_version: str | None = None) -> str | None:
    if model_version:
        return str(model_version)
    return DEFAULT_AGENT_MODEL_VERSION.get(str(agent or "").strip().lower())


def cache_key_for_identity(identity: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(identity).encode("utf-8")).hexdigest()


def tool_smoke_artifact_path(fleet_dir: Path, identity: dict[str, Any]) -> Path:
    key = cache_key_for_identity(identity)
    node = _safe_part(str(identity.get("node_id") or "unknown"))
    agent = _safe_part(str(identity.get("agent") or "unknown"))
    return fleet_dir / "probes" / "tool-smoke" / node / f"{agent}__{key[:20]}.json"


def write_tool_smoke_artifact(fleet_dir: Path, payload: dict[str, Any]) -> Path:
    identity = payload.get("identity") if isinstance(payload.get("identity"), dict) else None
    if identity is None:
        identity = build_identity(
            node_id=str(payload.get("node_id") or ""),
            agent=str(payload.get("agent") or ""),
            base_sha=str(payload.get("base_sha") or ""),
            sandbox=str(payload.get("sandbox") or DEFAULT_SANDBOX),
            model_version=payload.get("model_version"),
            worktree_identity=payload.get("worktree_identity"),
            worktree_shape=str(payload.get("worktree_shape") or DEFAULT_WORKTREE_SHAPE),
        )
        payload = {**payload, "identity": identity}
    path = tool_smoke_artifact_path(fleet_dir, identity)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return path


def read_tool_smoke_artifact(fleet_dir: Path, identity: dict[str, Any]) -> dict[str, Any] | None:
    path = tool_smoke_artifact_path(fleet_dir, identity)
    if not path.exists():
        return None
    return _read_artifact_path(path)


def _read_artifact_path(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _expires_at(updated_at: str, ttl_s: int) -> str:
    ts = parse_iso(updated_at) or utc_now()
    return iso(ts + dt.timedelta(seconds=max(1, int(ttl_s))))


def cache_state(payload: dict[str, Any] | None, *, now: dt.datetime | None = None) -> str:
    if payload is None:
        return "missing"
    if str(payload.get("status") or "red") != "green":
        return str(payload.get("status") or "red")
    expires = parse_iso(str(payload.get("expires_at") or ""))
    if expires is None:
        return "stale"
    if expires <= (now or utc_now()):
        return "stale"
    return "green"


def build_tool_smoke_prompt(*, worktree_path: str) -> str:
    abs_readme = f"{worktree_path.rstrip('/')}/README.md"
    return (
        "Tool-smoke canary. Use your native Read/file tool, not shell commands. "
        "First read relative path VERSION. Then read absolute path "
        f"{abs_readme}. Reply exactly with three lines:\n"
        "TOOL-SMOKE-READY\n"
        "RELATIVE_OK: <first line of VERSION>\n"
        "ABSOLUTE_OK: <first line of README.md>\n"
        "Stop after that."
    )


def _json_from_stdout(stdout: str) -> dict[str, Any] | None:
    text = (stdout or "").strip()
    if not text:
        return None
    candidates = [text]
    candidates.extend(line.strip() for line in text.splitlines() if line.strip().startswith("{"))
    for candidate in reversed(candidates):
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _extract_model_version(text: str, fallback: str | None) -> str | None:
    if fallback:
        return str(fallback)
    match = _MODEL_ID_RE.search(text or "")
    return match.group(1) if match else None


def _read_tool_error_seen(text: str, status_payload: dict[str, Any] | None) -> bool:
    if status_payload:
        repeated = str(status_payload.get("repeated_tool_error_tool") or "")
        last = str(status_payload.get("last_tool_error") or "")
        if repeated == "Read" and (_TOOL_ERROR_RE.search(last) or last):
            return True
    return bool(_TOOL_ERROR_RE.search(text or "") and _READ_TOOL_RE.search(text or ""))


_TOOL_SUCCESS_STATUSES = {"complete", "completed", "success", "succeeded", "ok"}
_TOOL_SUCCESS_KINDS = {"tool_output", "tool_result"}


def _tool_call_success(call: dict[str, Any]) -> bool:
    status = str(call.get("status") or "").lower()
    kind = str(call.get("kind") or "").lower()
    if status:
        return status in _TOOL_SUCCESS_STATUSES
    return kind in _TOOL_SUCCESS_KINDS


def _tool_call_text(call: dict[str, Any]) -> str:
    try:
        return json.dumps(call, sort_keys=True, separators=(",", ":")).lower()
    except (TypeError, ValueError):
        return str(call).lower()


def _read_tool_successes(
    status_payload: dict[str, Any] | None,
    *,
    expected_absolute_path: str | None,
) -> tuple[bool, bool]:
    if not status_payload:
        return False, False
    tool_calls = status_payload.get("tool_calls")
    if not isinstance(tool_calls, list):
        return False, False
    relative = False
    absolute = False
    for item in tool_calls:
        if not isinstance(item, dict) or not _tool_call_success(item):
            continue
        text = _tool_call_text(item)
        title = str(item.get("title") or "").lower()
        if "read" not in title and '"read"' not in text:
            continue
        if "version" in text:
            relative = True
        expected_abs = str(expected_absolute_path or "").rstrip("/").lower()
        if expected_abs and expected_abs in text:
            absolute = True
    return relative, absolute


def build_result_record(
    *,
    identity: dict[str, Any],
    ttl_s: int = DEFAULT_TTL_S,
    exit_code: int = 0,
    stdout: str = "",
    stderr: str = "",
    status_payload: dict[str, Any] | None = None,
    status_path: str | None = None,
    tail_path: str | None = None,
    updated_at: str | None = None,
    expected_absolute_path: str | None = None,
) -> dict[str, Any]:
    status_payload = status_payload or _json_from_stdout(stdout) or {}
    status_text = json.dumps(status_payload, sort_keys=True) if status_payload else ""
    combined = "\n".join(part for part in (stdout, stderr, status_text) if part)
    result_text = str(
        status_payload.get("result_text")
        or status_payload.get("text_excerpt")
        or status_payload.get("last_tool_error")
        or stdout
    )
    read_relative_ok = "RELATIVE_OK:" in result_text
    read_absolute_ok = "ABSOLUTE_OK:" in result_text
    model_version = _extract_model_version(combined, identity.get("model_version"))
    record_identity = dict(identity)
    keyed_model = str(record_identity.get("model_version") or "")
    model_key_ok = not model_version or keyed_model == str(model_version)
    read_relative_tool_ok, read_absolute_tool_ok = _read_tool_successes(
        status_payload,
        expected_absolute_path=expected_absolute_path,
    )
    read_tool_error_seen = _read_tool_error_seen(combined, status_payload)
    acp_state = str(status_payload.get("state") or "")
    ok = (
        exit_code == 0
        and not read_tool_error_seen
        and read_relative_ok
        and read_absolute_ok
        and read_relative_tool_ok
        and read_absolute_tool_ok
        and model_key_ok
        and (not acp_state or acp_state == "complete")
    )
    now = updated_at or iso()
    diagnosis = None
    if read_tool_error_seen:
        diagnosis = (
            f"worker {identity.get('agent')} on node {identity.get('node_id')} failed a tool-smoke: "
            "native Read returned tool_output_error; tools are broken in this environment; "
            "do not commit a goal-loop"
        )
    elif not ok:
        missing = []
        if not read_relative_ok:
            missing.append("relative result label")
        if not read_absolute_ok:
            missing.append("absolute result label")
        if not read_relative_tool_ok:
            missing.append("relative native Read evidence")
        if not read_absolute_tool_ok:
            missing.append("absolute native Read evidence")
        if not model_key_ok:
            missing.append("keyed model version")
        reason = ", ".join(missing) or f"ACP state={acp_state or 'unknown'} exit={exit_code}"
        diagnosis = (
            f"worker {identity.get('agent')} on node {identity.get('node_id')} failed a tool-smoke: "
            f"did not prove {reason}"
        )
    return {
        "schema": TOOL_SMOKE_SCHEMA,
        "cache_key": cache_key_for_identity(identity),
        "identity": record_identity,
        "status": "green" if ok else "red",
        "ok": ok,
        "agent": record_identity.get("agent"),
        "model_version": model_version,
        "node_id": record_identity.get("node_id"),
        "base_sha": record_identity.get("base_sha"),
        "worktree_identity": record_identity.get("worktree_identity"),
        "worktree_shape": record_identity.get("worktree_shape"),
        "sandbox": record_identity.get("sandbox"),
        "read_relative_ok": read_relative_ok,
        "read_absolute_ok": read_absolute_ok,
        "read_relative_tool_ok": read_relative_tool_ok,
        "read_absolute_tool_ok": read_absolute_tool_ok,
        "read_tool_error_seen": read_tool_error_seen,
        "expected_absolute_path": expected_absolute_path,
        "status_path": status_path or status_payload.get("status_path"),
        "tail_path": tail_path or status_payload.get("agent_stderr_path"),
        "stderr_excerpt": (stderr or str(status_payload.get("last_tool_error") or ""))[-800:] or None,
        "diagnosis": diagnosis,
        "updated_at": now,
        "ttl_s": int(ttl_s),
        "expires_at": _expires_at(now, int(ttl_s)),
    }


def gate_message(
    fleet_dir: Path,
    identity: dict[str, Any],
    state: str,
    record: dict[str, Any] | None,
) -> str:
    if record and record.get("diagnosis"):
        return str(record["diagnosis"])
    run_hint = (
        "python3 scripts/goalflight_fleet.py "
        f"--fleet-dir {fleet_dir} tool-smoke run --node {identity.get('node_id')} "
        f"--agent {identity.get('agent')} --base-sha {identity.get('base_sha')} "
        f"--sandbox {identity.get('sandbox') or DEFAULT_SANDBOX} --exec"
    )
    return (
        f"missing/stale tool-smoke canary ({state}) for goal-loop dispatch "
        f"node={identity.get('node_id')} agent={identity.get('agent')} "
        f"base={identity.get('base_sha')} sandbox={identity.get('sandbox')}; run: {run_hint}"
    )


def assert_green_canary(
    fleet_dir: Path,
    *,
    node_id: str,
    agent: str,
    base_sha: str,
    sandbox: str = DEFAULT_SANDBOX,
    model_version: str | None = None,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    model_version = resolve_model_version(agent, model_version)
    identity = build_identity(
        node_id=node_id,
        agent=agent,
        base_sha=base_sha,
        sandbox=sandbox,
        model_version=model_version,
    )
    record = read_tool_smoke_artifact(fleet_dir, identity)
    state = cache_state(record, now=now)
    if state != "green":
        raise ToolSmokeGateError(
            gate_message(fleet_dir, identity, state, record),
            cache_state=state,
            record=record,
        )
    return record or {}


def _node_entry(fleet_dir: Path, node_id: str) -> dict[str, Any]:
    import goalflight_fleet_store as fleet

    fleet_doc = fleet.read_json(fleet_dir / "fleet.json")
    node = (fleet_doc.get("nodes") or {}).get(node_id)
    if not isinstance(node, dict):
        raise ToolSmokeRunError(f"unknown node: {node_id}")
    return node


def _remote_path(state_dir: str, *parts: str) -> str:
    return "/".join([state_dir.rstrip("/"), *[p.strip("/") for p in parts]])


def _ssh_result(
    node_id: str,
    node_entry: dict[str, Any],
    command_class: str,
    *,
    runner: ProbeRunner | None,
    **params: Any,
) -> dict[str, Any]:
    repo_root = str(node_entry.get("repo_root") or "")
    params.setdefault("state_dir", str(node_entry.get("state_dir") or "~/.goal-flight"))
    host = fleet_ssh.host_from_node_entry(node_id, node_entry)
    remote_argv = fleet_ssh.build_remote_command(command_class, repo_root=repo_root, **params)
    ssh_argv = fleet_ssh.build_ssh_command(host, remote_argv, command_class=command_class)
    return fleet_ssh.run_ssh(ssh_argv, runner=runner, dry_run=False)


def assert_live_ssh_opt_in(*, runner: ProbeRunner | None = None) -> None:
    if runner is not None:
        return
    value = os.environ.get("GOALFLIGHT_LIVE_SSH", "").lower()
    if value not in {"1", "true", "yes"}:
        raise ToolSmokeRunError(
            "tool-smoke live run requires GOALFLIGHT_LIVE_SSH=1; "
            "status reads cached canaries without SSH"
        )


def preview_tool_smoke_canary(
    fleet_dir: Path,
    *,
    node_id: str,
    agent: str,
    base_sha: str,
    sandbox: str = DEFAULT_SANDBOX,
    model_version: str | None = None,
    ttl_s: int = DEFAULT_TTL_S,
) -> dict[str, Any]:
    node = _node_entry(fleet_dir, node_id)
    state_dir = str(node.get("state_dir") or "~/.goal-flight").rstrip("/")
    model_version = resolve_model_version(agent, model_version)
    identity = build_identity(
        node_id=node_id,
        agent=agent,
        base_sha=base_sha,
        sandbox=sandbox,
        model_version=model_version,
    )
    dispatch_id = f"tool-smoke-{_safe_part(agent)}-{cache_key_for_identity(identity)[:12]}"
    worktree_path = _remote_path(state_dir, "worktrees", dispatch_id)
    status_path = _remote_path(state_dir, "dispatches", dispatch_id, "status.json")
    expected_absolute_path = f"{worktree_path.rstrip('/')}/README.md"
    return {
        "schema": TOOL_SMOKE_SCHEMA,
        "dry_run": True,
        "identity": identity,
        "cache_path": str(tool_smoke_artifact_path(fleet_dir, identity)),
        "dispatch_id": dispatch_id,
        "worktree_path": worktree_path,
        "status_path": status_path,
        "expected_absolute_path": expected_absolute_path,
        "prompt": build_tool_smoke_prompt(worktree_path=worktree_path),
        "ttl_s": int(ttl_s),
    }


def run_tool_smoke_canary(
    fleet_dir: Path,
    *,
    node_id: str,
    agent: str,
    base_sha: str,
    sandbox: str = DEFAULT_SANDBOX,
    model_version: str | None = None,
    ttl_s: int = DEFAULT_TTL_S,
    runner: ProbeRunner | None = None,
    iso_now: str | None = None,
) -> dict[str, Any]:
    assert_live_ssh_opt_in(runner=runner)
    node = _node_entry(fleet_dir, node_id)
    state_dir = str(node.get("state_dir") or "~/.goal-flight").rstrip("/")
    preview = preview_tool_smoke_canary(
        fleet_dir,
        node_id=node_id,
        agent=agent,
        base_sha=base_sha,
        sandbox=sandbox,
        model_version=model_version,
        ttl_s=ttl_s,
    )
    identity = dict(preview["identity"])
    dispatch_id = str(preview["dispatch_id"])
    worktree_path = str(preview["worktree_path"])
    status_path = str(preview["status_path"])
    expected_absolute_path = str(preview["expected_absolute_path"])
    prompt = str(preview["prompt"])

    # Deterministic smoke worktree. Remove a stale prior copy first; ignore
    # absence/non-removable paths and let worktree add produce the real verdict.
    _ssh_result(
        node_id,
        node,
        "git_worktree_remove",
        runner=runner,
        worktree_path=worktree_path,
    )
    preflight_steps = [
        ("git_fetch", {}),
        ("git_verify_commit", {"sha": base_sha}),
        ("git_worktree_add", {"worktree_path": worktree_path, "ref": base_sha, "detach": True}),
    ]
    for command_class, params in preflight_steps:
        result = _ssh_result(node_id, node, command_class, runner=runner, **params)
        if not result.get("ok"):
            record = build_result_record(
                identity=identity,
                ttl_s=ttl_s,
                exit_code=int(result.get("exit_code") or 1),
                stdout=str(result.get("stdout") or ""),
                stderr=str(result.get("stderr") or ""),
                status_path=status_path,
                updated_at=iso_now,
            )
            record["diagnosis"] = (
                f"worker {agent} on node {node_id} failed tool-smoke preflight "
                f"{command_class}: {record.get('stderr_excerpt') or 'no stderr'}"
            )
            write_tool_smoke_artifact(fleet_dir, record)
            return record

    acp = _ssh_result(
        node_id,
        node,
        "acp_run",
        runner=runner,
        state_dir=state_dir,
        dispatch_id=dispatch_id,
        agent=agent,
        model=identity.get("model_version"),
        prompt=prompt,
        cwd=worktree_path,
        status_json=status_path,
        mode="one-shot",
        os_sandbox=sandbox,
        max_consecutive_tool_errors=1,
        max_acp_events=120,
        idle_timeout=180,
        live_matrix=True,
    )
    status_payload: dict[str, Any] | None = None
    status_read = _ssh_result(
        node_id,
        node,
        "read_status_file",
        runner=runner,
        state_dir=state_dir,
        status_path=status_path,
    )
    if status_read.get("ok"):
        status_payload = _json_from_stdout(str(status_read.get("stdout") or ""))
    if status_payload is None:
        status_payload = _json_from_stdout(str(acp.get("stdout") or ""))
    record = build_result_record(
        identity=identity,
        ttl_s=ttl_s,
        exit_code=int(acp.get("exit_code") or 0),
        stdout=str(acp.get("stdout") or ""),
        stderr=str(acp.get("stderr") or ""),
        status_payload=status_payload,
        status_path=status_path,
        tail_path=(status_payload or {}).get("agent_stderr_path") if status_payload else None,
        updated_at=iso_now,
        expected_absolute_path=expected_absolute_path,
    )
    record["canary_worktree_path"] = worktree_path
    write_tool_smoke_artifact(fleet_dir, record)
    return record


def fleet_tool_smoke_doctor(fleet_dir: Path) -> dict[str, Any]:
    root = fleet_dir / "probes" / "tool-smoke"
    canaries: list[dict[str, Any]] = []
    if root.exists():
        for path in sorted(root.rglob("*.json")):
            try:
                payload = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            canaries.append(
                {
                    "node_id": payload.get("node_id"),
                    "agent": payload.get("agent"),
                    "model_version": payload.get("model_version"),
                    "base_sha": payload.get("base_sha"),
                    "sandbox": payload.get("sandbox"),
                    "status": payload.get("status"),
                    "cache_state": cache_state(payload),
                    "updated_at": payload.get("updated_at"),
                    "expires_at": payload.get("expires_at"),
                    "path": str(path),
                }
            )
    return {"available": True, "fleet_dir": str(fleet_dir), "canaries": canaries}


def cmd_tool_smoke_status(args) -> int:
    model_version = resolve_model_version(args.agent, getattr(args, "model_version", None))
    identity = build_identity(
        node_id=args.node,
        agent=args.agent,
        base_sha=args.base_sha,
        sandbox=args.sandbox,
        model_version=model_version,
    )
    record = read_tool_smoke_artifact(args.fleet_dir, identity)
    payload = record or {
        "schema": TOOL_SMOKE_SCHEMA,
        "identity": identity,
        "status": "missing",
        "cache_path": str(tool_smoke_artifact_path(args.fleet_dir, identity)),
    }
    payload = dict(payload)
    payload["cache_state"] = cache_state(record)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["cache_state"] == "green" else 1


def cmd_tool_smoke_run(args) -> int:
    if not getattr(args, "exec", False):
        payload = preview_tool_smoke_canary(
            args.fleet_dir,
            node_id=args.node,
            agent=args.agent,
            base_sha=args.base_sha,
            sandbox=args.sandbox,
            model_version=getattr(args, "model_version", None),
            ttl_s=args.ttl_s,
        )
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    try:
        payload = run_tool_smoke_canary(
            args.fleet_dir,
            node_id=args.node,
            agent=args.agent,
            base_sha=args.base_sha,
            sandbox=args.sandbox,
            model_version=getattr(args, "model_version", None),
            ttl_s=args.ttl_s,
        )
    except ToolSmokeRunError as exc:
        print(str(exc), file=__import__("sys").stderr)
        return 2
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload.get("status") == "green" else 1
