#!/usr/bin/env python3
"""Fleet remote dispatch MVP (Track A goals 11a–11f).

Explicit dispatch CLI with preview-first flow, thin defaults from steering,
lock-order enforcement, allowlisted remote worktree + detached launch receipt,
and auth/quarantine gates.
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
import goalflight_fleet_tool_smoke as tool_smoke


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
    if command_class == "git_verify_commit":
        # git rev-parse --verify --quiet suppresses its own message, so a
        # well-formed-but-unpushed base-sha fails with a bare non-zero exit. Name
        # the most common cause so the operator does not have to guess.
        details.append(
            "hint: base-sha not found on the node's origin after fetch — push the "
            "controller commit (or pick a pushed base) before dispatch."
        )
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
    base_sha: str
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
            "base_sha": self.base_sha,
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


def _tool_smoke_required(dispatch_mode: str, tool_smoke_policy: str | None) -> bool:
    policy = str(tool_smoke_policy or "auto").lower()
    mode = str(dispatch_mode or "one-shot").lower()
    # `skip` is an explicit operator override and is authoritative even in goal
    # mode: an operator who passes --tool-smoke skip has deliberately opted out of
    # the canary gate (e.g. the worker is known-good, or no canary exists yet for
    # a new agent like claude-acp). Silently re-gating it in goal mode made the
    # flag a no-op and the documented escape misleading.
    if policy == "skip":
        return False
    if policy == "require":
        return True
    if mode == "goal":
        return True
    return False


def assert_dispatch_gates(
    fleet_dir: Path,
    *,
    node_id: str,
    billing_account: str,
    agent: str | None = None,
    base_sha: str | None = None,
    dispatch_mode: str = "one-shot",
    tool_smoke_policy: str | None = "auto",
    tool_smoke_sandbox: str = tool_smoke.DEFAULT_SANDBOX,
) -> None:
    assert_node_not_quarantined(fleet_dir, node_id)
    try:
        billing.assert_dispatch_auth(fleet_dir, node_id, billing_account)
    except billing.DispatchAuthError as exc:
        raise DispatchGateError(str(exc), code="auth") from exc
    if not _tool_smoke_required(dispatch_mode, tool_smoke_policy):
        return
    if not agent or not base_sha:
        raise DispatchGateError(
            "goal-loop dispatch requires agent and base_sha for tool-smoke canary lookup",
            code="tool_smoke",
        )
    try:
        tool_smoke.assert_green_canary(
            fleet_dir,
            node_id=node_id,
            agent=agent,
            base_sha=base_sha,
            sandbox=tool_smoke_sandbox,
        )
    except tool_smoke.ToolSmokeGateError as exc:
        raise DispatchGateError(str(exc), code="tool_smoke") from exc


def remote_status_path_for_dispatch(state_dir: str, dispatch_id: str) -> str:
    base = str(state_dir or "~/.goal-flight").strip().rstrip("/")
    return f"{base}/dispatches/{dispatch_id}/status.json"


BASE_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")


def normalize_base_sha(base_sha: str | None) -> str:
    value = str(base_sha or "").strip()
    if not value:
        raise DispatchError(
            "fleet dispatch worktree creation requires --base-sha <40-hex commit>. "
            "Push or resolve the controller base first; the remote worktree is created detached at that exact SHA."
        )
    if not BASE_SHA_RE.match(value):
        raise DispatchError("--base-sha must be a 40-character hex commit SHA")
    return value.lower()


def build_remote_command_plan(
    node_entry: dict[str, Any],
    *,
    dispatch_id: str,
    agent: str,
    prompt: str,
    worktree_path: str,
    base_sha: str,
    recover_unconfirmed: bool = False,
) -> list[dict[str, Any]]:
    import goalflight_fleet_ssh as fleet_ssh

    repo_root = str(node_entry.get("repo_root") or "")
    state_dir = str(node_entry.get("state_dir") or "~/.goal-flight")
    status_json = remote_status_path_for_dispatch(state_dir, dispatch_id)
    plans: list[dict[str, Any]] = []
    for command_class, extra in (
        ("git_prune_claude_refs", {"python": str(node_entry.get("python") or "python3")}),
        ("git_fetch", {}),
        ("git_verify_commit", {"sha": base_sha}),
        ("git_worktree_add", {"state_dir": state_dir, "worktree_path": worktree_path, "ref": base_sha, "detach": True}),
        (
            "launch_detached",
            {
                "dispatch_id": dispatch_id,
                "node_id": str(node_entry.get("node_id") or ""),
                "agent": agent,
                "prompt": prompt,
                "cwd": worktree_path,
                "state_dir": state_dir,
                "status_json": status_json,
                "python": str(node_entry.get("python") or "python3"),
                "recover_unconfirmed": recover_unconfirmed,
                "base_sha": base_sha,
            },
        ),
    ):
        argv = fleet_ssh.build_remote_command(command_class, repo_root=repo_root, **extra)
        plans.append({"command_class": command_class, "argv": argv})
    return plans


def launch_recovery_allowed(fleet_dir: Path, *, dispatch_id: str, node_id: str) -> bool:
    import goalflight_fleet_watch as fleet_watch

    meta = fleet_watch._read_json_object(fleet_watch.dispatch_meta_path(fleet_dir, dispatch_id))
    return bool(
        meta.get("dispatch_id") == dispatch_id
        and meta.get("node_id") == node_id
        and meta.get("launch_unconfirmed") is True
    )


def preview_dispatch(
    fleet_dir: Path,
    *,
    node_id: str,
    agent: str | None,
    billing_account: str | None,
    prompt: str,
    dispatch_id: str | None = None,
    base_sha: str | None = None,
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
    resolved_base_sha = normalize_base_sha(base_sha)
    worktree = worktree_path_for_dispatch(node_entry, dispatch_id)
    banner = None
    if used_thin or thin_mode:
        banner = "MVP still requires explicit confirmation of billing account before --exec"
    recover_unconfirmed = launch_recovery_allowed(fleet_dir, dispatch_id=dispatch_id, node_id=node_id)
    preview = DispatchPreview(
        dispatch_id=dispatch_id,
        node_id=node_id,
        agent=resolved_agent,
        billing_account=resolved_billing,
        prompt=prompt,
        worktree_path=worktree,
        base_sha=resolved_base_sha,
        lock_steps=build_lock_steps(),
        thin_defaults=used_thin or thin_mode,
        billing_banner=banner,
        remote_commands=build_remote_command_plan(
            node_entry,
            dispatch_id=dispatch_id,
            agent=resolved_agent,
            prompt=prompt,
            worktree_path=worktree,
            base_sha=resolved_base_sha,
            recover_unconfirmed=recover_unconfirmed,
        ),
    )
    return preview


@dataclass
class LockChainResult:
    acquired: list[str] = field(default_factory=list)
    remote_lease_id: str | None = None
    launch_receipt: dict[str, Any] | None = None
    launch_unconfirmed: bool = False
    launch_unconfirmed_error: str | None = None
    fencing_token: str | None = None
    account_key: str | None = None
    account_lock: dict[str, Any] | None = None
    remote_log: list[dict[str, Any]] = field(default_factory=list)


def _account_lock_meta(lock_doc: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(lock_doc, dict):
        return {}
    account_key = lock_doc.get("account_key")
    fencing_token = lock_doc.get("fencing_token")
    owner_dispatch_id = lock_doc.get("owner_dispatch_id")
    meta: dict[str, Any] = {}
    if isinstance(account_key, str) and account_key:
        meta["account_key"] = account_key
    if isinstance(fencing_token, str) and fencing_token:
        meta["account_lock_fencing_token"] = fencing_token
    if isinstance(owner_dispatch_id, str) and owner_dispatch_id:
        meta["account_lock_owner_dispatch_id"] = owner_dispatch_id
    return meta


def persist_dispatch_account_lock_link(
    fleet_dir: Path,
    preview: DispatchPreview,
    lock_doc: dict[str, Any],
) -> None:
    import goalflight_fleet as fleet
    import goalflight_fleet_watch as fleet_watch

    meta_path = fleet_watch.dispatch_meta_path(fleet_dir, preview.dispatch_id)
    try:
        meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    except (OSError, json.JSONDecodeError):
        meta = {}
    meta.setdefault("dispatch_id", preview.dispatch_id)
    meta.setdefault("node_id", preview.node_id)
    meta.setdefault("billing_account", preview.billing_account)
    meta.update(_account_lock_meta(lock_doc))
    fleet._atomic_write_json(meta_path, meta)


def _parse_launch_receipt(
    stdout: str,
    *,
    dispatch_id: str,
    node_id: str,
    base_sha: str | None = None,
) -> dict[str, Any]:
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    if not lines:
        raise DispatchError("launch_detached did not return a launch receipt")
    try:
        payload = json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        raise DispatchError(f"launch_detached returned invalid receipt JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise DispatchError("launch_detached receipt must be a JSON object")
    if payload.get("dispatch_id") != dispatch_id:
        raise DispatchError("launch_detached receipt dispatch_id mismatch")
    if payload.get("node_id") != node_id:
        raise DispatchError("launch_detached receipt node_id mismatch")
    if not isinstance(payload.get("remote_pid"), int):
        raise DispatchError("launch_detached receipt missing remote_pid")
    if not payload.get("remote_status_path"):
        raise DispatchError("launch_detached receipt missing remote_status_path")
    if base_sha and payload.get("worktree_base_sha") != base_sha:
        raise DispatchError("launch_detached receipt base_sha mismatch")
    return payload


def _is_confirmed_launch_refusal(*, exit_code: int, stdout: str, stderr: str) -> bool:
    if exit_code != 17:
        return False
    return "WARN-REFUSE duplicate dispatch-id exists" in f"{stdout}\n{stderr}"


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
    worktree_created = False
    node_entry: dict[str, Any] = {}
    try:
        existing_lock = fleet.load_account_lock(fleet.account_lock_path(fleet_dir, preview.billing_account))
        recovery_allowed = launch_recovery_allowed(
            fleet_dir,
            dispatch_id=preview.dispatch_id,
            node_id=preview.node_id,
        )
        if (
            existing_lock
            and existing_lock.get("state") == "active"
            and existing_lock.get("owner_dispatch_id") == preview.dispatch_id
            and not fleet.account_lock_expired(existing_lock)
            and recovery_allowed
        ):
            lock_doc = existing_lock
        else:
            lock_doc = fleet.acquire_account_lock(
                fleet_dir,
                account_key=preview.billing_account,
                owner_dispatch_id=preview.dispatch_id,
            )
            acquired.append("account")
        result.fencing_token = str(lock_doc.get("fencing_token"))
        result.account_key = str(lock_doc.get("account_key") or preview.billing_account)
        result.account_lock = dict(lock_doc)
        persist_dispatch_account_lock_link(fleet_dir, preview, lock_doc)
        if stop_after == "account":
            raise DispatchError("stop_after account")

        acquired.append("worktree")
        if stop_after == "worktree":
            raise DispatchError("stop_after worktree")

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
                with fleet_ssh.node_ssh_lock(preview.node_id, fleet_dir=fleet_dir):
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
                    failure = _format_remote_failure(
                        str(cmd["command_class"]),
                        exit_code=code,
                        stdout=stdout,
                        stderr=stderr,
                        ssh_argv=ssh_argv,
                        sensitive_values=sensitive_values,
                    )
                    if cmd["command_class"] == "launch_detached":
                        if _is_confirmed_launch_refusal(exit_code=code, stdout=stdout, stderr=stderr):
                            raise DispatchError(failure)
                        result.launch_unconfirmed = True
                        result.launch_unconfirmed_error = failure
                        acquired.append("spawn")
                        result.acquired = acquired
                        return result
                    raise DispatchError(failure)
                if cmd["command_class"] == "git_worktree_add":
                    # Real creation confirmed (code == 0). Track it so a later
                    # mid-chain failure can roll the worktree back, not just the
                    # account lock. Only OUR successful add sets this — a failed
                    # add (e.g. path already exists for a duplicate dispatch) leaves
                    # it False so we never remove a worktree we did not create.
                    worktree_created = True
                if cmd["command_class"] == "launch_detached":
                    try:
                        result.launch_receipt = _parse_launch_receipt(
                            stdout,
                            dispatch_id=preview.dispatch_id,
                            node_id=preview.node_id,
                            base_sha=preview.base_sha,
                        )
                    except DispatchError as exc:
                        result.launch_unconfirmed = True
                        result.launch_unconfirmed_error = str(exc)
                        acquired.append("spawn")
                        result.acquired = acquired
                        return result
            else:
                result.remote_log.append({"command_class": cmd["command_class"], "dry_run": True})
        acquired.append("spawn")
        result.acquired = acquired
        return result
    except Exception:
        if worktree_created and runner is not None:
            _best_effort_remove_worktree(preview, node_entry, runner=runner, fleet_dir=fleet_dir)
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


def _best_effort_remove_worktree(
    preview: DispatchPreview,
    node_entry: dict[str, Any],
    *,
    runner: Callable[[list[str]], tuple[int, str, str]],
    fleet_dir: Path,
) -> None:
    """Remove a remote worktree this dispatch created before it failed mid-chain.

    A `git_worktree_add` that succeeded followed by a later command failure (e.g. a
    confirmed launch refusal) would otherwise strand a detached worktree on the
    node, accumulating until `git worktree prune`/salvage. The freshly-added
    detached worktree is clean (the worker never ran), so a plain remove suffices.
    Best-effort and never raises: rollback cleanup must not mask the original
    dispatch error, and the account-lock release below must still run.
    """
    try:
        import goalflight_fleet_ssh as fleet_ssh

        repo_root = str(node_entry.get("repo_root") or "")
        argv = fleet_ssh.build_remote_command(
            "git_worktree_remove",
            repo_root=repo_root,
            state_dir=str(node_entry.get("state_dir") or "~/.goal-flight"),
            worktree_path=preview.worktree_path,
        )
        host = fleet_ssh.host_from_node_entry(preview.node_id, node_entry)
        ssh_argv = fleet_ssh.build_ssh_command(host, argv, command_class="git_worktree_remove")
        with fleet_ssh.node_ssh_lock(preview.node_id, fleet_dir=fleet_dir):
            runner(ssh_argv)
    except Exception:
        pass


def register_dispatch_meta(
    fleet_dir: Path,
    preview: DispatchPreview,
    *,
    lease_active: bool = True,
    pid_hint: str = "alive",
    launch_receipt: dict[str, Any] | None = None,
    launch_unconfirmed: bool = False,
    launch_unconfirmed_error: str | None = None,
    row_state: str | None = None,
    account_lock: dict[str, Any] | None = None,
) -> None:
    import goalflight_fleet as fleet
    import goalflight_fleet_watch as fleet_watch

    fleet_doc = fleet.read_json(fleet_dir / "fleet.json")
    node_entry = (fleet_doc.get("nodes") or {}).get(preview.node_id) or {}
    state_dir = str(node_entry.get("state_dir") or "~/.goal-flight")
    remote_status_path = remote_status_path_for_dispatch(state_dir, preview.dispatch_id)
    if launch_receipt and launch_receipt.get("remote_status_path"):
        remote_status_path = str(launch_receipt["remote_status_path"])

    receipt_identity = (launch_receipt or {}).get("remote_identity")
    remote_lstart = (launch_receipt or {}).get("remote_lstart")
    if isinstance(receipt_identity, dict) and not remote_lstart:
        remote_lstart = receipt_identity.get("lstart")

    dispatch_dir = fleet_watch.dispatch_register_dir(fleet_dir) / preview.dispatch_id
    dispatch_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "dispatch_id": preview.dispatch_id,
        "node_id": preview.node_id,
        "billing_account": preview.billing_account,
        "lease_active": lease_active,
        "pid_hint": pid_hint,
        "ssh_reachable": True,
        "remote_state_dir": state_dir,
        "remote_status_path": remote_status_path,
        "remote_lease_id_superseded_by": "launch_receipt",
        "launch_unconfirmed": launch_unconfirmed,
        "worktree_path": preview.worktree_path,
        "base_sha": preview.base_sha,
        "worktree_base_sha": preview.base_sha,
    }
    meta.update(_account_lock_meta(account_lock))
    if row_state:
        meta["row_state"] = row_state
    if launch_unconfirmed_error:
        meta["launch_unconfirmed_error"] = launch_unconfirmed_error
    if launch_unconfirmed:
        now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(timespec="seconds")
        meta["launch_issued_at"] = now
        meta["launch_unconfirmed_at"] = now
    if launch_receipt:
        meta.update(
            {
                "launch_receipt": launch_receipt,
                "remote_pid": launch_receipt.get("remote_pid"),
                "remote_pid_lstart": remote_lstart,
                "remote_pid_identity": receipt_identity,
            }
        )
    fleet._atomic_write_json(fleet_watch.dispatch_meta_path(fleet_dir, preview.dispatch_id), meta)
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
        "remote_lease_id_superseded_by": "launch_receipt",
        "remote_launch_receipt": chain.launch_receipt,
        "launch_unconfirmed": chain.launch_unconfirmed,
        "launch_unconfirmed_error": chain.launch_unconfirmed_error,
        "base_sha": preview.base_sha,
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
        def _stub(argv: list[str]) -> tuple[int, str, str]:
            if "goalflight_fleet_launch_detached.py" not in " ".join(argv):
                return 0, "{}", ""
            joined = " ".join(argv)
            dispatch_id = "unknown"
            node_id = "unknown"
            status_json = "/tmp/goal-flight-stub/status.json"
            base_sha = None
            for idx, part in enumerate(argv):
                if part == "--dispatch-id" and idx + 1 < len(argv):
                    dispatch_id = argv[idx + 1]
                elif part == "--node-id" and idx + 1 < len(argv):
                    node_id = argv[idx + 1]
                elif part == "--status-json" and idx + 1 < len(argv):
                    status_json = argv[idx + 1]
                elif part == "--base-sha" and idx + 1 < len(argv):
                    base_sha = argv[idx + 1]
            for key, assign in (
                ("dispatch_id", r"--dispatch-id\s+'?([^'\s]+)'?"),
                ("node_id", r"--node-id\s+'?([^'\s]+)'?"),
                ("status_json", r"--status-json\s+'?([^'\s]+)'?"),
                ("base_sha", r"--base-sha\s+'?([^'\s]+)'?"),
            ):
                match = re.search(assign, joined)
                if match:
                    if key == "dispatch_id":
                        dispatch_id = match.group(1)
                    elif key == "node_id":
                        node_id = match.group(1)
                    elif key == "base_sha":
                        base_sha = match.group(1)
                    else:
                        status_json = match.group(1)
            return 0, json.dumps(
                {
                    "schema": "goalflight.fleet.launch_receipt.v1",
                    "dispatch_id": dispatch_id,
                    "node_id": node_id,
                    "remote_pid": 4242,
                    "remote_lstart": "Thu Jun 11 12:00:00 2026",
                    "remote_identity": {
                        "pid": 4242,
                        "lstart": "Thu Jun 11 12:00:00 2026",
                        "comm": "python3",
                    },
                    "remote_status_path": status_json,
                    "remote_state_dir": "/tmp/goal-flight-stub",
                    "launcher_log_path": "/tmp/goal-flight-stub/dispatcher.log",
                    "started_at": "2026-06-11T12:00:00+00:00",
                    "worktree_base_sha": base_sha,
                },
                sort_keys=True,
            ), ""

        return _stub
    return default_ssh_runner


def execute_dispatch(
    fleet_dir: Path,
    preview: DispatchPreview,
    *,
    runner: Callable[[list[str]], tuple[int, str, str]] | None = None,
    stub_terminal: bool = False,
    dispatch_mode: str = "one-shot",
    tool_smoke_policy: str | None = "auto",
    tool_smoke_sandbox: str = tool_smoke.DEFAULT_SANDBOX,
) -> dict[str, Any]:
    assert_dispatch_gates(
        fleet_dir,
        node_id=preview.node_id,
        billing_account=preview.billing_account,
        agent=preview.agent,
        base_sha=preview.base_sha,
        dispatch_mode=dispatch_mode,
        tool_smoke_policy=tool_smoke_policy,
        tool_smoke_sandbox=tool_smoke_sandbox,
    )
    recovering_unconfirmed = launch_recovery_allowed(
        fleet_dir,
        dispatch_id=preview.dispatch_id,
        node_id=preview.node_id,
    )
    register_dispatch_meta(
        fleet_dir,
        preview,
        pid_hint="unknown",
        launch_unconfirmed=recovering_unconfirmed,
        row_state="launch_pending",
    )
    chain = acquire_lock_chain(fleet_dir, preview, runner=runner)
    register_dispatch_meta(
        fleet_dir,
        preview,
        pid_hint="unknown" if chain.launch_unconfirmed else "alive",
        launch_receipt=chain.launch_receipt,
        launch_unconfirmed=chain.launch_unconfirmed,
        launch_unconfirmed_error=chain.launch_unconfirmed_error,
        row_state="launch_unconfirmed" if chain.launch_unconfirmed else "launch_receipted",
        account_lock=chain.account_lock,
    )
    ledger_info = record_dispatch_ledger(
        preview,
        chain,
        state="launch_unconfirmed" if chain.launch_unconfirmed else "running",
    )
    if stub_terminal:
        import goalflight_ledger as ledger

        write_terminal_mirror(fleet_dir, preview)
        register_dispatch_meta(
            fleet_dir,
            preview,
            pid_hint="dead",
            lease_active=True,
            launch_receipt=chain.launch_receipt,
            launch_unconfirmed=chain.launch_unconfirmed,
            launch_unconfirmed_error=chain.launch_unconfirmed_error,
            row_state="terminal",
            account_lock=chain.account_lock,
        )
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
            "launch_receipt": chain.launch_receipt,
            "launch_unconfirmed": chain.launch_unconfirmed,
            "launch_unconfirmed_error": chain.launch_unconfirmed_error,
            "ledger": ledger_info,
            "remote_log": chain.remote_log,
        }
    return {
        "ok": True,
        "dispatch_id": preview.dispatch_id,
        "remote_lease_id": chain.remote_lease_id,
        "launch_receipt": chain.launch_receipt,
        "launch_unconfirmed": chain.launch_unconfirmed,
        "launch_unconfirmed_error": chain.launch_unconfirmed_error,
        "ledger": ledger_info,
        "remote_log": chain.remote_log,
        "finalize": None,
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
            base_sha=getattr(args, "base_sha", None),
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
        dispatch_mode = getattr(args, "dispatch_mode", "one-shot")
        assert_dispatch_gates(
            args.fleet_dir,
            node_id=preview.node_id,
            billing_account=preview.billing_account,
            agent=preview.agent,
            base_sha=preview.base_sha,
            dispatch_mode=dispatch_mode,
            tool_smoke_policy=getattr(args, "tool_smoke", "auto"),
            tool_smoke_sandbox=getattr(args, "tool_smoke_sandbox", tool_smoke.DEFAULT_SANDBOX),
        )
    except DispatchGateError as exc:
        print(json.dumps({"ok": False, "error": str(exc), "code": exc.code}), file=__import__("sys").stderr)
        return 1

    try:
        assert_live_ssh_opt_in()
    except DispatchError as exc:
        print(str(exc), file=__import__("sys").stderr)
        return 2

    runner = resolve_dispatch_runner(args)

    result = execute_dispatch(
        args.fleet_dir,
        preview,
        runner=runner,
        stub_terminal=getattr(args, "stub_terminal", False),
        dispatch_mode=getattr(args, "dispatch_mode", "one-shot"),
        tool_smoke_policy=getattr(args, "tool_smoke", "auto"),
        tool_smoke_sandbox=getattr(args, "tool_smoke_sandbox", tool_smoke.DEFAULT_SANDBOX),
    )
    print(json.dumps(result, indent=2))
    return 0
