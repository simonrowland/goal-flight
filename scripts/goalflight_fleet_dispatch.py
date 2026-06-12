#!/usr/bin/env python3
"""Fleet remote dispatch MVP (Track A goals 11a–11f).

Explicit dispatch CLI with preview-first flow, thin defaults from steering,
lock-order enforcement, allowlisted remote worktree + spawn stubs, ledger
``remote_lease_id``, and auth/quarantine gates.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shlex
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import goalflight_fleet_billing as billing
import goalflight_fleet_status as status
import goalflight_fleet_status_cli as status_cli


class DispatchError(Exception):
    pass


_SENSITIVE_ARGV_FLAGS = frozenset({"--prompt-b64", "--prompt"})
_EMBEDDED_SECRET_RE = re.compile(
    r"(--prompt(?:-b64)?(?:=|\s+))(?:'[^']*'|\"[^\"]*\"|\S+)"
)


def _redact_argv(argv: list[str]) -> list[str]:
    """Mask sensitive argv values (e.g. ``--prompt-b64 <base64 prompt>``) before
    logging or returning them. The base64 prompt is trivially reversible and may
    carry private task context or pasted credentials."""
    redacted: list[str] = []
    mask_next = False
    for part in argv:
        if mask_next:
            redacted.append("<redacted>")
            mask_next = False
            continue
        flag = part.split("=", 1)[0]
        if flag in _SENSITIVE_ARGV_FLAGS:
            if "=" in part:
                redacted.append(f"{flag}=<redacted>")
            else:
                redacted.append(part)
                mask_next = True
            continue
        redacted.append(_EMBEDDED_SECRET_RE.sub(r"\1<redacted>", part))
    return redacted


def _prompt_sensitive_values(prompt: str) -> list[str]:
    values = [prompt]
    if prompt:
        values.append(base64.b64encode(prompt.encode("utf-8")).decode("ascii"))
    return values


def _redact_text(text: str, *, sensitive_values: list[str] | None = None) -> str:
    redacted = _EMBEDDED_SECRET_RE.sub(r"\1<redacted>", text)
    for value in sensitive_values or []:
        if value:
            redacted = redacted.replace(value, "<redacted>")
    return redacted


def _format_remote_failure(
    command_class: str,
    *,
    exit_code: int,
    stdout: str,
    stderr: str,
    ssh_argv: list[str],
    sensitive_values: list[str] | None = None,
) -> str:
    details = [
        f"remote {command_class} failed (exit {exit_code})",
        f"ssh argv: {shlex.join(_redact_argv(ssh_argv))}",
    ]
    if stderr:
        details.append(f"stderr:\n{_redact_text(stderr, sensitive_values=sensitive_values).rstrip()}")
    if stdout:
        details.append(f"stdout:\n{_redact_text(stdout, sensitive_values=sensitive_values).rstrip()}")
    return "\n".join(details)


class DispatchGateError(DispatchError):
    def __init__(self, message: str, *, code: str = "blocked") -> None:
        self.code = code
        super().__init__(message)


def assert_live_ssh_opt_in() -> None:
    if os.environ.get("GOALFLIGHT_LIVE_SSH") == "1":
        return
    raise DispatchError(
        "--exec refused: set GOALFLIGHT_LIVE_SSH=1 to allow live SSH. "
        "CI/test safety: hermetic suites must never live-SSH; preview mode "
        "works without this opt-in."
    )


@dataclass
class LockStep:
    name: str
    status: str
    detail: str | None = None


@dataclass
class DispatchPreview:
    dispatch_id: str
    node_id: str
    agent: str
    billing_account: str
    prompt: str
    worktree_path: str
    lock_steps: list[LockStep] = field(default_factory=list)
    thin_defaults: bool = False
    billing_banner: str | None = None
    remote_commands: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dispatch_id": self.dispatch_id,
            "node": self.node_id,
            "agent": self.agent,
            "billing_account": self.billing_account,
            "prompt": "<redacted>",
            "worktree_path": self.worktree_path,
            "thin_defaults": self.thin_defaults,
            "billing_banner": self.billing_banner,
            "lock_steps": [
                {"name": step.name, "status": step.status, "detail": step.detail} for step in self.lock_steps
            ],
            "remote_commands": [
                {**cmd, "argv": _redact_argv(cmd["argv"])}
                if isinstance(cmd, dict) and "argv" in cmd
                else cmd
                for cmd in self.remote_commands
            ],
            "dry_run": True,
        }


def default_dispatch_id() -> str:
    return f"acp-{uuid.uuid4().hex[:12]}"


def resolve_thin_defaults(
    fleet_dir: Path,
    *,
    node_id: str,
    agent: str | None,
    billing_account: str | None,
) -> tuple[str, str, bool]:
    """Fill agent/billing from steering + billing doc when omitted."""
    import goalflight_fleet_steering as steering

    used_thin = agent is None or billing_account is None
    resolved_agent = agent or "codex-acp"
    resolved_billing = billing_account
    if not resolved_billing:
        steering_doc = steering.effective_steering(steering.load_steering_doc(fleet_dir))
        priority = steering_doc.get("node_policy", {}).get("priority") or []
        if node_id not in priority and priority:
            pass
        billing_path = fleet_dir / "billing-accounts.json"
        if billing_path.exists():
            doc = json.loads(billing_path.read_text())
            accounts = doc.get("accounts") or []
            if accounts:
                resolved_billing = str(accounts[0].get("account_key") or "openai/default")
        resolved_billing = resolved_billing or "openai/default"
    return resolved_agent, str(resolved_billing), used_thin


def worktree_path_for_dispatch(node_entry: dict[str, Any], dispatch_id: str) -> str:
    state_dir = str(node_entry.get("state_dir") or "~/.goal-flight").rstrip("/")
    if state_dir.startswith("~"):
        state_dir = str(Path(state_dir).expanduser())
    return f"{state_dir}/worktrees/{dispatch_id}"


def build_lock_steps(*, acquired: list[str] | None = None) -> list[LockStep]:
    order = ["registry", "account", "worktree", "remote_capacity", "spawn"]
    acquired_set = set(acquired or [])
    steps: list[LockStep] = []
    for name in order:
        if name in acquired_set:
            steps.append(LockStep(name, "acquired"))
        else:
            steps.append(LockStep(name, "pending"))
    return steps


def assert_node_not_quarantined(fleet_dir: Path, node_id: str) -> None:
    payload = status_cli.build_fleet_status(fleet_dir)
    for row in payload.get("dispatches") or []:
        if row.get("node") != node_id:
            continue
        if row.get("state") == "quarantined":
            raise DispatchGateError(
                f"node {node_id} has quarantined dispatch {row.get('dispatch_id')}",
                code="quarantine",
            )


def assert_dispatch_gates(fleet_dir: Path, *, node_id: str, billing_account: str) -> None:
    assert_node_not_quarantined(fleet_dir, node_id)
    try:
        billing.assert_dispatch_auth(fleet_dir, node_id, billing_account)
    except billing.DispatchAuthError as exc:
        raise DispatchGateError(str(exc), code="auth") from exc


def remote_status_path_for_dispatch(state_dir: str, dispatch_id: str) -> str:
    base = str(state_dir or "~/.goal-flight").strip().rstrip("/")
    return f"{base}/dispatches/{dispatch_id}/status.json"


def build_remote_command_plan(
    node_entry: dict[str, Any],
    *,
    dispatch_id: str,
    agent: str,
    prompt: str,
    worktree_path: str,
) -> list[dict[str, Any]]:
    import goalflight_fleet_ssh as fleet_ssh

    repo_root = str(node_entry.get("repo_root") or "")
    state_dir = str(node_entry.get("state_dir") or "~/.goal-flight")
    status_json = remote_status_path_for_dispatch(state_dir, dispatch_id)
    plans: list[dict[str, Any]] = []
    for command_class, extra in (
        ("git_prune_claude_refs", {"python": str(node_entry.get("python") or "python3")}),
        ("git_fetch", {}),
        ("git_worktree_add", {"worktree_path": worktree_path, "ref": "origin/main"}),
        (
            "acp_run",
            {
                "dispatch_id": dispatch_id,
                "agent": agent,
                "prompt": prompt,
                "cwd": worktree_path,
                "state_dir": state_dir,
                "status_json": status_json,
            },
        ),
    ):
        argv = fleet_ssh.build_remote_command(command_class, repo_root=repo_root, **extra)
        plans.append({"command_class": command_class, "argv": argv})
    return plans


def preview_dispatch(
    fleet_dir: Path,
    *,
    node_id: str,
    agent: str | None,
    billing_account: str | None,
    prompt: str,
    dispatch_id: str | None = None,
    thin_mode: bool = False,
) -> DispatchPreview:
    import goalflight_fleet as fleet

    fleet.bootstrap(fleet_dir)
    fleet_doc = fleet.read_json(fleet_dir / "fleet.json")
    nodes = fleet_doc.get("nodes") or {}
    node_entry = nodes.get(node_id)
    if not isinstance(node_entry, dict):
        raise DispatchError(f"unknown node: {node_id}")

    resolved_agent, resolved_billing, used_thin = resolve_thin_defaults(
        fleet_dir,
        node_id=node_id,
        agent=agent,
        billing_account=billing_account,
    )
    if not thin_mode and (agent is None or billing_account is None):
        raise DispatchError("explicit dispatch requires --agent and --billing-account (or --thin-defaults)")

    dispatch_id = dispatch_id or default_dispatch_id()
    worktree = worktree_path_for_dispatch(node_entry, dispatch_id)
    banner = None
    if used_thin or thin_mode:
        banner = "MVP still requires explicit confirmation of billing account before --exec"
    preview = DispatchPreview(
        dispatch_id=dispatch_id,
        node_id=node_id,
        agent=resolved_agent,
        billing_account=resolved_billing,
        prompt=prompt,
        worktree_path=worktree,
        lock_steps=build_lock_steps(),
        thin_defaults=used_thin or thin_mode,
        billing_banner=banner,
        remote_commands=build_remote_command_plan(
            node_entry,
            dispatch_id=dispatch_id,
            agent=resolved_agent,
            prompt=prompt,
            worktree_path=worktree,
        ),
    )
    return preview


@dataclass
class LockChainResult:
    acquired: list[str] = field(default_factory=list)
    remote_lease_id: str | None = None
    fencing_token: str | None = None
    account_key: str | None = None
    remote_log: list[dict[str, Any]] = field(default_factory=list)


def acquire_lock_chain(
    fleet_dir: Path,
    preview: DispatchPreview,
    *,
    runner: Callable[[list[str]], tuple[int, str, str]] | None = None,
    stop_after: str | None = None,
) -> LockChainResult:
    """Acquire locks in plan order; rollback on failure."""
    import goalflight_fleet as fleet

    result = LockChainResult(account_key=preview.billing_account)
    acquired: list[str] = []
    try:
        lock_doc = fleet.acquire_account_lock(
            fleet_dir,
            account_key=preview.billing_account,
            owner_dispatch_id=preview.dispatch_id,
        )
        acquired.append("account")
        result.fencing_token = str(lock_doc.get("fencing_token"))
        if stop_after == "account":
            raise DispatchError("stop_after account")

        acquired.append("worktree")
        if stop_after == "worktree":
            raise DispatchError("stop_after worktree")

        result.remote_lease_id = str(uuid.uuid4())
        acquired.append("remote_capacity")
        if stop_after == "remote_capacity":
            raise DispatchError("stop_after remote_capacity")

        fleet_doc = fleet.read_json(fleet_dir / "fleet.json")
        node_entry = (fleet_doc.get("nodes") or {}).get(preview.node_id) or {}
        sensitive_values = _prompt_sensitive_values(preview.prompt)
        for cmd in preview.remote_commands:
            import goalflight_fleet_ssh as fleet_ssh

            host = fleet_ssh.host_from_node_entry(preview.node_id, node_entry)
            ssh_argv = fleet_ssh.build_ssh_command(
                host,
                cmd["argv"],
                command_class=str(cmd["command_class"]),
            )
            if runner is not None:
                code, stdout, stderr = runner(ssh_argv)
                redacted_stdout = _redact_text(stdout, sensitive_values=sensitive_values)
                redacted_stderr = _redact_text(stderr, sensitive_values=sensitive_values)
                result.remote_log.append(
                    {
                        "command_class": cmd["command_class"],
                        "ssh_argv": _redact_argv(ssh_argv),
                        "exit_code": code,
                        "stdout": redacted_stdout[:200],
                        "stderr": redacted_stderr[:200],
                    }
                )
                if code != 0:
                    raise DispatchError(
                        _format_remote_failure(
                            str(cmd["command_class"]),
                            exit_code=code,
                            stdout=stdout,
                            stderr=stderr,
                            ssh_argv=ssh_argv,
                            sensitive_values=sensitive_values,
                        )
                    )
            else:
                result.remote_log.append({"command_class": cmd["command_class"], "dry_run": True})
        acquired.append("spawn")
        result.acquired = acquired
        return result
    except Exception:
        release_lock_chain(fleet_dir, preview, acquired=acquired, fencing_token=result.fencing_token)
        raise


def release_lock_chain(
    fleet_dir: Path,
    preview: DispatchPreview,
    *,
    acquired: list[str],
    fencing_token: str | None,
) -> None:
    import goalflight_fleet as fleet

    for step in reversed(acquired):
        if step == "account" and fencing_token:
            try:
                fleet.release_account_lock(
                    fleet_dir,
                    account_key=preview.billing_account,
                    fencing_token=fencing_token,
                    reason="dispatch_rollback",
                )
            except fleet.AccountLockError:
                pass


def register_dispatch_meta(
    fleet_dir: Path,
    preview: DispatchPreview,
    *,
    lease_active: bool = True,
    pid_hint: str = "alive",
) -> None:
    import goalflight_fleet as fleet
    import goalflight_fleet_watch as fleet_watch

    fleet_doc = fleet.read_json(fleet_dir / "fleet.json")
    node_entry = (fleet_doc.get("nodes") or {}).get(preview.node_id) or {}
    state_dir = str(node_entry.get("state_dir") or "~/.goal-flight")
    remote_status_path = remote_status_path_for_dispatch(state_dir, preview.dispatch_id)

    dispatch_dir = fleet_watch.dispatch_register_dir(fleet_dir) / preview.dispatch_id
    dispatch_dir.mkdir(parents=True, exist_ok=True)
    fleet._atomic_write_json(
        fleet_watch.dispatch_meta_path(fleet_dir, preview.dispatch_id),
        {
            "dispatch_id": preview.dispatch_id,
            "node_id": preview.node_id,
            "billing_account": preview.billing_account,
            "lease_active": lease_active,
            "pid_hint": pid_hint,
            "ssh_reachable": True,
            "remote_state_dir": state_dir,
            "remote_status_path": remote_status_path,
        },
    )
    aggregate_path = fleet_dir / "register" / "aggregate.json"
    if aggregate_path.exists():
        doc = fleet.read_json(aggregate_path)
    else:
        doc = {
            "schema": "goalflight.fleet.register.aggregate.v1",
            "schema_version": 1,
            "min_reader_version": 1,
            "open_user_needs": [],
            "active_dispatches": [],
            "last_steering": None,
        }
    active = list(doc.get("active_dispatches") or [])
    if preview.dispatch_id not in active:
        active.append(preview.dispatch_id)
    doc["active_dispatches"] = active
    fleet._atomic_write_json(aggregate_path, doc)


def write_terminal_mirror(fleet_dir: Path, preview: DispatchPreview) -> None:
    import goalflight_fleet_watch as fleet_watch
    from goalflight_liveness import write_status

    write_status(
        fleet_watch.dispatch_status_path(fleet_dir, preview.dispatch_id),
        {
            "schema": "goalflight.acp-run.v1",
            "seq": 1,
            "dispatch_id": preview.dispatch_id,
            "state": "complete",
            "agent": preview.agent,
            "updated_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(
                timespec="seconds"
            ),
        },
    )


def record_dispatch_ledger(
    preview: DispatchPreview,
    chain: LockChainResult,
    *,
    state: str = "running",
) -> dict[str, Any]:
    import goalflight_ledger as ledger

    record = {
        "schema": ledger.SCHEMA,
        "dispatch_id": preview.dispatch_id,
        "prompt_path": preview.prompt if preview.prompt.endswith(".md") else None,
        "agent": preview.agent,
        "transport": "fleet-ssh",
        "project_root": str(Path.cwd()),
        "state": state,
        "remote_lease_id": chain.remote_lease_id,
        "lease_id": chain.remote_lease_id,
        "started_at": ledger.utc_now(),
        "hostname": __import__("socket").gethostname(),
    }
    with ledger.StateLock():
        path = ledger.write_record(record)
    return {"ok": True, "path": str(path), "record": record}


def default_ssh_runner(argv: list[str]) -> tuple[int, str, str]:
    import goalflight_fleet_ssh as fleet_ssh

    run = fleet_ssh.run_ssh(argv)
    return int(run.get("exit_code", 1)), str(run.get("stdout") or ""), str(run.get("stderr") or "")


def _remote_acp_run_succeeded(chain: LockChainResult) -> bool:
    for entry in reversed(chain.remote_log):
        if entry.get("dry_run"):
            continue
        if entry.get("command_class") != "acp_run":
            continue
        return int(entry.get("exit_code", 1)) == 0
    return False


def finalize_live_sync_dispatch(
    fleet_dir: Path,
    preview: DispatchPreview,
    chain: LockChainResult,
    ledger_info: dict[str, Any],
    *,
    runner: Callable[[list[str]], tuple[int, str, str]] | None = None,
) -> dict[str, Any] | None:
    """After synchronous live acp_run, ingest remote mirror and release fleet locks."""
    if not _remote_acp_run_succeeded(chain):
        return None

    import goalflight_fleet as fleet
    import goalflight_fleet_reconcile as fleet_reconcile
    import goalflight_fleet_watch as fleet_watch
    import goalflight_ledger as ledger

    fleet_doc = fleet.read_json(fleet_dir / "fleet.json")
    node_entry = (fleet_doc.get("nodes") or {}).get(preview.node_id)
    if not isinstance(node_entry, dict):
        return {"ok": False, "error": f"unknown node {preview.node_id}"}

    meta_path = fleet_watch.dispatch_meta_path(fleet_dir, preview.dispatch_id)
    meta = fleet_watch._read_json_object(meta_path)
    transport = fleet_watch.SshFleetWatchTransport(runner=runner or default_ssh_runner)
    ingest = fleet_watch.ingest_dispatch_mirror(
        fleet_dir,
        preview.dispatch_id,
        meta,
        transport,
        node_entry=node_entry,
    )
    if not ingest.ok:
        return {"ok": False, "ingest": ingest.to_dict() if hasattr(ingest, "to_dict") else ingest.__dict__}

    register_dispatch_meta(fleet_dir, preview, pid_hint="dead", lease_active=False)
    with ledger.StateLock():
        record = json.loads(Path(ledger_info["path"]).read_text())
        mirror_payload = json.loads(
            fleet_watch.dispatch_status_path(fleet_dir, preview.dispatch_id).read_text()
        )
        record["state"] = str(mirror_payload.get("state") or "complete")
        record["ended_at"] = ledger.utc_now()
        ledger.write_record(record)
    reconcile = fleet_reconcile.reconcile_dispatch(fleet_dir, preview.dispatch_id, mutate=True)
    return {"ok": True, "ingest": ingest.action, "reconcile": reconcile.to_dict()}


def resolve_dispatch_runner(args) -> Callable[[list[str]], tuple[int, str, str]] | None:
    if getattr(args, "stub_runner", None):
        return args.stub_runner
    if getattr(args, "stub_remote", False):
        return lambda _argv: (0, "{}", "")
    return default_ssh_runner


def execute_dispatch(
    fleet_dir: Path,
    preview: DispatchPreview,
    *,
    runner: Callable[[list[str]], tuple[int, str, str]] | None = None,
    stub_terminal: bool = False,
) -> dict[str, Any]:
    assert_dispatch_gates(fleet_dir, node_id=preview.node_id, billing_account=preview.billing_account)
    chain = acquire_lock_chain(fleet_dir, preview, runner=runner)
    register_dispatch_meta(fleet_dir, preview)
    ledger_info = record_dispatch_ledger(preview, chain)
    if stub_terminal:
        import goalflight_ledger as ledger

        write_terminal_mirror(fleet_dir, preview)
        register_dispatch_meta(fleet_dir, preview, pid_hint="dead", lease_active=True)
        with ledger.StateLock():
            record = json.loads(Path(ledger_info["path"]).read_text())
            record["state"] = "complete"
            record["ended_at"] = ledger.utc_now()
            ledger.write_record(record)
        import goalflight_fleet_reconcile as fleet_reconcile

        fleet_reconcile.reconcile_dispatch(fleet_dir, preview.dispatch_id, mutate=True)
        return {
            "ok": True,
            "dispatch_id": preview.dispatch_id,
            "remote_lease_id": chain.remote_lease_id,
            "ledger": ledger_info,
            "remote_log": chain.remote_log,
        }
    finalize = finalize_live_sync_dispatch(
        fleet_dir,
        preview,
        chain,
        ledger_info,
        runner=runner,
    )
    return {
        "ok": True,
        "dispatch_id": preview.dispatch_id,
        "remote_lease_id": chain.remote_lease_id,
        "ledger": ledger_info,
        "remote_log": chain.remote_log,
        "finalize": finalize,
    }


def cmd_dispatch(args) -> int:
    import goalflight_fleet as fleet

    fleet.bootstrap(args.fleet_dir)
    thin = bool(getattr(args, "thin_defaults", False))
    try:
        preview = preview_dispatch(
            args.fleet_dir,
            node_id=args.node,
            agent=getattr(args, "agent", None),
            billing_account=getattr(args, "billing_account", None),
            prompt=args.prompt,
            dispatch_id=getattr(args, "dispatch_id", None),
            thin_mode=thin,
        )
    except DispatchError as exc:
        print(str(exc), file=__import__("sys").stderr)
        return 2

    if not getattr(args, "exec", False):
        payload = preview.to_dict()
        if preview.billing_banner:
            payload["banner"] = preview.billing_banner
        print(json.dumps(payload, indent=2))
        return 0

    try:
        assert_live_ssh_opt_in()
    except DispatchError as exc:
        print(str(exc), file=__import__("sys").stderr)
        return 2

    try:
        assert_dispatch_gates(args.fleet_dir, node_id=preview.node_id, billing_account=preview.billing_account)
    except DispatchGateError as exc:
        print(json.dumps({"ok": False, "error": str(exc), "code": exc.code}), file=__import__("sys").stderr)
        return 1

    runner = resolve_dispatch_runner(args)

    result = execute_dispatch(
        args.fleet_dir,
        preview,
        runner=runner,
        stub_terminal=getattr(args, "stub_terminal", False),
    )
    print(json.dumps(result, indent=2))
    return 0
