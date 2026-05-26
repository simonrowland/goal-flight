#!/usr/bin/env python3
"""Fleet remote dispatch MVP (Track A goals 11a–11f).

Explicit dispatch CLI with preview-first flow, thin defaults from steering,
lock-order enforcement, allowlisted remote worktree + spawn stubs, ledger
``remote_lease_id``, and auth/quarantine gates.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import goalflight_fleet_billing as billing
import goalflight_fleet_status as status
import goalflight_fleet_status_cli as status_cli


class DispatchError(Exception):
    pass


class DispatchGateError(DispatchError):
    def __init__(self, message: str, *, code: str = "blocked") -> None:
        self.code = code
        super().__init__(message)


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
            "prompt": self.prompt,
            "worktree_path": self.worktree_path,
            "thin_defaults": self.thin_defaults,
            "billing_banner": self.billing_banner,
            "lock_steps": [
                {"name": step.name, "status": step.status, "detail": step.detail} for step in self.lock_steps
            ],
            "remote_commands": self.remote_commands,
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
    plans: list[dict[str, Any]] = []
    for command_class, extra in (
        ("git_worktree_add", {"worktree_path": worktree_path, "ref": "HEAD"}),
        (
            "acp_run",
            {
                "dispatch_id": dispatch_id,
                "agent": agent,
                "prompt": prompt,
                "cwd": worktree_path,
                "state_dir": state_dir,
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
        for cmd in preview.remote_commands:
            import goalflight_fleet_ssh as fleet_ssh

            host = fleet_ssh.SshHostSpec(
                alias=str((node_entry.get("ssh") or {}).get("alias") or preview.node_id),
                hostname=str((node_entry.get("ssh") or {}).get("hostname") or preview.node_id),
            )
            ssh_argv = fleet_ssh.build_ssh_command(
                host,
                cmd["argv"],
                command_class=str(cmd["command_class"]),
            )
            if runner is not None:
                code, stdout, stderr = runner(ssh_argv)
                result.remote_log.append(
                    {
                        "command_class": cmd["command_class"],
                        "exit_code": code,
                        "stdout": stdout[:200],
                        "stderr": stderr[:200],
                    }
                )
                if code != 0:
                    raise DispatchError(f"remote {cmd['command_class']} failed")
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
