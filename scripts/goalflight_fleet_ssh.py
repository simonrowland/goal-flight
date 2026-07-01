#!/usr/bin/env python3
"""SSH remote command allowlist for fleet operations (Track A goal 3)."""

from __future__ import annotations

import base64
import contextlib
import json
import posixpath
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

# Non-interactive SSH often omits Homebrew and user-local agent installs.
SYSTEM_PATH_PREFIX = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
# OpenSSH forced-command exec often omits HOME; bootstrap before user-local bins.
REMOTE_PATH_PREFIX = (
    "$HOME/.local/bin:$HOME/.grok/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
)
REMOTE_HOME_BOOTSTRAP = 'HOME=${HOME:-$(eval echo ~${USER:-$(whoami)})}'
NODE_LOCK_RE = re.compile(r"[^A-Za-z0-9_.-]+")
DISPATCH_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
BARE_INTERPRETER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,127}$")
ALLOWED_REMOTE_INTERPRETER_BASENAMES = frozenset({"python", "python3"})
TRUSTED_INTERPRETER_DIRS = frozenset(
    {
        "/bin",
        "/usr/bin",
        "/usr/local/bin",
        "/opt/homebrew/bin",
    }
)


def _quote_remote_argv_part(part: str) -> str:
    """Quote one remote argv element; preserve remote tilde expansion via $HOME."""
    if part == "~":
        return '"${HOME}"'
    if part.startswith("~/"):
        suffix = part[2:]
        escaped = (
            suffix.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("$", "\\$")
            .replace("`", "\\`")
        )
        return f'"${{HOME}}/{escaped}"'
    return shlex.quote(part)


def node_lock_path(node_id: str, *, fleet_dir: Path | None = None) -> Path:
    base = fleet_dir or Path.home() / ".goal-flight" / "fleet"
    safe = NODE_LOCK_RE.sub("_", node_id.strip() or "unknown")
    return base.expanduser() / "locks" / f"{safe}.lock"


@contextlib.contextmanager
def node_ssh_lock(node_id: str, *, fleet_dir: Path | None = None):
    path = node_lock_path(node_id, fleet_dir=fleet_dir)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a+") as handle:
        try:
            import fcntl
        except ImportError:  # pragma: no cover - non-POSIX fallback
            yield path
            return
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield path
        finally:
            with contextlib.suppress(Exception):
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def wrap_remote_argv(remote_argv: list[str]) -> list[str]:
    """Run remote argv under zsh with fleet worker PATH (expands $HOME on the node)."""
    validate_remote_argv(remote_argv)
    inner = " ".join(_quote_remote_argv_part(part) for part in remote_argv)
    script = f"{REMOTE_HOME_BOOTSTRAP}; PATH={REMOTE_PATH_PREFIX}:$PATH; exec {inner}"
    return ["/bin/zsh", "-c", script]

ALLOWED_COMMAND_CLASSES = frozenset(
    {
        "probe_echo",
        "probe_repo_exists",
        "probe_script_exists",
        "doctor",
        "status",
        "capacity",
        "git_prune_claude_refs",
        "acp_run",
        "launch_detached",
        "ferry_preflight",
        "git_fetch",
        "git_status_porcelain",
        "git_verify_commit",
        "git_checkout",
        "git_worktree_add",
        "git_worktree_remove",
        "read_status_file",
        "read_lease_file",
        "pid_identity",
        "auth_probe",
    }
)

UNSAFE_COMMAND_CLASSES = frozenset({"shell", "arbitrary"})

SHELL_METACHAR_RE = re.compile(r"[;&|`<>()$\\]|\$\(")

REMEDIATION_HINTS: dict[str, str] = {
    "host_key_mismatch": "Run: ssh-keygen -R <host> && ssh <alias> true",
    "repo_missing": "Clone the repo on the remote host or fix --repo-root",
    "script_missing": "Ensure goal-flight scripts exist under the remote repo_root",
    "auth_drift": "Run adapter auth probe on the remote (e.g. codex login status)",
    "probe_failed": "Verify SSH alias, identity file, and BatchMode connectivity",
    "allowlist_blocked": "Command class is not on the fleet SSH allowlist",
    "unsafe_remote_blocked": "Set unsafe_remote only after explicit USER-CONFIRM",
}


class SshAllowlistError(Exception):
    def __init__(self, message: str, *, code: str = "allowlist_blocked") -> None:
        self.code = code
        self.remediation = REMEDIATION_HINTS.get(code, REMEDIATION_HINTS["allowlist_blocked"])
        super().__init__(message)


@dataclass(frozen=True)
class SshHostSpec:
    alias: str
    hostname: str
    user: str | None = None
    port: int | None = None
    identity_file: str | None = None


def validate_remote_argv(argv: list[str]) -> None:
    if not isinstance(argv, list) or not argv:
        raise SshAllowlistError("remote argv must be a non-empty list")
    for idx, part in enumerate(argv):
        if not isinstance(part, str) or not part:
            raise SshAllowlistError(f"remote argv[{idx}] must be a non-empty string")
        if "\n" in part or "\r" in part:
            raise SshAllowlistError(f"remote argv[{idx}] contains newline")
        if SHELL_METACHAR_RE.search(part):
            raise SshAllowlistError(f"remote argv[{idx}] contains shell metacharacters")


def assert_allowed(command_class: str, *, unsafe_remote: bool = False) -> None:
    if command_class in UNSAFE_COMMAND_CLASSES:
        if not unsafe_remote:
            raise SshAllowlistError(
                f"command class {command_class!r} requires unsafe_remote=true",
                code="unsafe_remote_blocked",
            )
        return
    if command_class not in ALLOWED_COMMAND_CLASSES:
        raise SshAllowlistError(
            f"command class not allowlisted: {command_class}",
            code="allowlist_blocked",
        )


def _require_repo_root(repo_root: str) -> str:
    if not repo_root or not isinstance(repo_root, str):
        raise SshAllowlistError("repo_root is required")
    return _remote_norm(repo_root)


def _require_dispatch_id(dispatch_id: str, *, field: str = "dispatch_id") -> str:
    value = str(dispatch_id or "").strip()
    if not DISPATCH_ID_RE.match(value) or ".." in value:
        raise SshAllowlistError(f"{field} must be a safe dispatch id")
    return value


def _remote_norm(path: str) -> str:
    raw = str(path or "").strip().replace("\\", "/").rstrip("/")
    if not raw or "\n" in raw or "\r" in raw:
        raise SshAllowlistError("remote path must be a non-empty single-line path")
    return posixpath.normpath(re.sub(r"/+", "/", raw))


def _remote_same_or_under(path: str, root: str) -> bool:
    candidate = _remote_norm(path)
    base = _remote_norm(root)
    return candidate == base or candidate.startswith(base.rstrip("/") + "/")


def _remote_path_parts(path: str) -> tuple[str, ...]:
    return tuple(part for part in str(path).replace("\\", "/").split("/") if part)


def _reject_parent_segments(path: str, *, field: str) -> None:
    if any(part == ".." for part in _remote_path_parts(path)):
        raise SshAllowlistError(f"{field} must not contain parent-directory traversal")


def _validate_repo_scoped_remote_path(path: str, repo_root: str, *, field: str) -> str:
    raw = str(path or "").strip()
    if not raw:
        raise SshAllowlistError(f"{field} is required")
    _reject_parent_segments(raw, field=field)
    if posixpath.isabs(raw):
        candidate = _remote_norm(raw)
    else:
        candidate = _remote_norm(posixpath.join(repo_root, raw))
    if not _remote_same_or_under(candidate, repo_root):
        raise SshAllowlistError(f"{field} must resolve under repo_root")
    return candidate


def _validate_relative_remote_path(path: str, *, field: str) -> str:
    raw = str(path or "").strip().replace("\\", "/")
    if not raw or "\n" in raw or "\r" in raw:
        raise SshAllowlistError(f"{field} must be a non-empty single-line relative path")
    normalized = posixpath.normpath(raw)
    if normalized in {"", "."} or posixpath.isabs(normalized):
        raise SshAllowlistError(f"{field} must be a relative file path")
    _reject_parent_segments(normalized, field=field)
    return normalized


def _validate_state_dir(state_dir: str | None, *, field: str = "state_dir") -> str:
    raw = str(state_dir or "").strip().rstrip("/")
    if not raw or "\n" in raw or "\r" in raw:
        raise SshAllowlistError(f"{field} must be a non-empty single-line path")
    _reject_parent_segments(raw, field=field)
    normalized = _remote_norm(raw)
    if not (posixpath.isabs(normalized) or normalized.startswith("~/")):
        raise SshAllowlistError(f"{field} must be absolute or home-relative")
    return normalized


def _validate_interpreter_basename(interpreter: str, *, field: str) -> str:
    basename = posixpath.basename(str(interpreter or "").rstrip("/"))
    if basename not in ALLOWED_REMOTE_INTERPRETER_BASENAMES:
        raise SshAllowlistError(f"{field} basename is not an allowlisted Python interpreter")
    return basename


def _validate_remote_interpreter(
    interpreter: str,
    *,
    repo_root: str,
    state_dir: str | None = None,
    field: str = "python",
) -> str:
    raw = str(interpreter or "").strip()
    if not raw or "\n" in raw or "\r" in raw:
        raise SshAllowlistError(f"{field} must be a non-empty single-line command")
    _validate_interpreter_basename(raw, field=field)
    if "/" not in raw:
        if not BARE_INTERPRETER_RE.match(raw) or raw in {".", ".."}:
            raise SshAllowlistError(f"{field} must be a bare command name or trusted path")
        return raw
    _reject_parent_segments(raw, field=field)
    if not (posixpath.isabs(raw) or raw.startswith("~/")):
        raise SshAllowlistError(f"{field} path must be absolute, trusted, or a bare command name")
    candidate = _remote_norm(raw)
    trusted_roots = [repo_root]
    if state_dir:
        trusted_roots.append(_validate_state_dir(state_dir, field=f"{field} state_dir"))
    if posixpath.dirname(candidate) in TRUSTED_INTERPRETER_DIRS:
        return candidate
    if any(_remote_same_or_under(candidate, root) for root in trusted_roots if str(root or "").strip()):
        return candidate
    raise SshAllowlistError(f"{field} path must be an allowlisted Python interpreter under a trusted root")


def _validate_declared_remote_path(path: str, roots: list[str], *, field: str) -> str:
    candidate = _remote_norm(path)
    declared = [_remote_norm(root) for root in roots if str(root or "").strip()]
    # Lexical only: remote symlinks under an allowed root can still point outside.
    # Resolving those requires a remote filesystem check, outside pre-exec validation.
    if declared and not any(_remote_same_or_under(candidate, root) for root in declared):
        raise SshAllowlistError(f"{field} must be under a declared remote root")
    return candidate


def _default_remote_roots(repo_root: str, state_dir: str | None = None) -> list[str]:
    roots = [repo_root]
    if state_dir:
        state_root = _validate_state_dir(state_dir)
        roots.extend(
            [
                state_root,
                f"{state_root}/worktrees",
                f"{state_root}/dispatches",
                f"{state_root}/fleet",
            ]
        )
    deduped: list[str] = []
    for root in roots:
        norm = _remote_norm(root)
        if norm not in deduped:
            deduped.append(norm)
    return deduped


def _validate_scoped_remote_path(
    path: str,
    *,
    repo_root: str,
    state_dir: str | None = None,
    allowed_roots: list[str] | None = None,
    field: str,
) -> str:
    roots = list(allowed_roots or []) or _default_remote_roots(repo_root, state_dir)
    return _validate_declared_remote_path(path, roots, field=field)


def build_remote_command(command_class: str, **params: Any) -> list[str]:
    assert_allowed(command_class, unsafe_remote=bool(params.get("unsafe_remote")))
    python = str(params.get("python") or "python")

    if command_class == "probe_echo":
        argv = ["echo", "goal-flight-probe-ok"]
        validate_remote_argv(argv)
        return argv

    repo_root = _require_repo_root(str(params.get("repo_root") or ""))
    if command_class == "probe_repo_exists":
        argv = ["test", "-d", repo_root]
    elif command_class == "probe_script_exists":
        script = str(params.get("script") or "scripts/goalflight_doctor.py")
        script_path = _validate_repo_scoped_remote_path(script, repo_root, field="probe_script_exists script")
        argv = ["test", "-x", script_path]
    elif command_class == "doctor":
        python = _validate_remote_interpreter(python, repo_root=repo_root, field="doctor python")
        argv = [python, f"{repo_root}/scripts/goalflight_doctor.py", "--json"]
    elif command_class == "status":
        python = _validate_remote_interpreter(python, repo_root=repo_root, field="status python")
        argv = [python, f"{repo_root}/scripts/goalflight_capacity.py", "status", "--json"]
    elif command_class == "capacity":
        python = _validate_remote_interpreter(python, repo_root=repo_root, field="capacity python")
        argv = [python, f"{repo_root}/scripts/goalflight_capacity.py", "status", "--json"]
    elif command_class == "git_prune_claude_refs":
        cleanup_python = _validate_remote_interpreter(
            str(params.get("python") or "python3"),
            repo_root=repo_root,
            field="git_prune_claude_refs python",
        )
        argv = [
            cleanup_python,
            f"{repo_root}/scripts/goalflight_cleanup_dispatch_refs.py",
            "--repo-root",
            repo_root,
            "--json",
        ]
    elif command_class == "acp_run":
        dispatch_id = _require_dispatch_id(str(params.get("dispatch_id") or ""))
        agent = str(params.get("agent") or "")
        prompt = str(params.get("prompt") or "")
        cwd = str(params.get("cwd") or repo_root)
        state_dir = _validate_state_dir(str(params.get("state_dir") or "~/.goal-flight"), field="acp_run state_dir")
        if not dispatch_id or not agent:
            raise SshAllowlistError("acp_run requires dispatch_id and agent")
        cwd = _validate_scoped_remote_path(
            cwd,
            repo_root=repo_root,
            state_dir=state_dir,
            allowed_roots=list(params.get("allowed_roots") or []),
            field="acp_run cwd",
        )
        # Remote SSH on some hosts splits argv on spaces; base64 keeps prompts intact.
        prompt_b64 = base64.b64encode(prompt.encode("utf-8")).decode("ascii")
        acp_python = _validate_remote_interpreter(
            str(params.get("python") or f"{state_dir}/venvs/acp-0.10/bin/python"),
            repo_root=repo_root,
            state_dir=state_dir,
            field="acp_run python",
        )
        argv = [
            acp_python,
            f"{repo_root}/scripts/goalflight_acp_run.py",
            "--agent",
            agent,
            "--cwd",
            cwd,
            "--dispatch-id",
            dispatch_id,
            "--prompt-b64",
            prompt_b64,
            "--json",
        ]
        model = str(params.get("model") or "").strip()
        if model:
            argv.extend(["--model", model])
        status_json = str(params.get("status_json") or "").strip()
        if status_json:
            status_json = _validate_declared_remote_path(
                status_json,
                [f"{state_dir}/dispatches"],
                field="acp_run status_json",
            )
            argv.extend(["--status-json", status_json])
        mode = str(params.get("mode") or "").strip()
        if mode:
            argv.extend(["--mode", mode])
        os_sandbox = str(params.get("os_sandbox") or "").strip()
        if os_sandbox:
            argv.extend(["--os-sandbox", os_sandbox])
        idle_timeout = params.get("idle_timeout")
        if idle_timeout is not None:
            argv.extend(["--idle-timeout", str(idle_timeout)])
        max_tool_errors = params.get("max_consecutive_tool_errors")
        if max_tool_errors is not None:
            argv.extend(["--max-consecutive-tool-errors", str(max_tool_errors)])
        max_events = params.get("max_acp_events")
        if max_events is not None:
            argv.extend(["--max-acp-events", str(max_events)])
        if bool(params.get("live_matrix")):
            argv = ["env", "GOALFLIGHT_ACP_LIVE_MATRIX=1", *argv]
    elif command_class == "launch_detached":
        dispatch_id = _require_dispatch_id(str(params.get("dispatch_id") or ""))
        agent = str(params.get("agent") or "")
        prompt = str(params.get("prompt") or "")
        cwd = str(params.get("cwd") or repo_root)
        node_id = str(params.get("node_id") or "")
        state_dir = _validate_state_dir(
            str(params.get("state_dir") or "~/.goal-flight"),
            field="launch_detached state_dir",
        )
        status_json = str(params.get("status_json") or "").strip()
        base_sha = str(params.get("base_sha") or "").strip()
        if not dispatch_id or not agent or not node_id or not status_json:
            raise SshAllowlistError("launch_detached requires dispatch_id, node_id, agent, and status_json")
        if not base_sha:
            raise SshAllowlistError("launch_detached requires base_sha")
        cwd = _validate_scoped_remote_path(
            cwd,
            repo_root=repo_root,
            state_dir=state_dir,
            allowed_roots=list(params.get("allowed_roots") or []),
            field="launch_detached cwd",
        )
        status_json = _validate_declared_remote_path(
            status_json,
            [f"{state_dir}/dispatches"],
            field="launch_detached status_json",
        )
        prompt_b64 = base64.b64encode(prompt.encode("utf-8")).decode("ascii")
        launch_python = _validate_remote_interpreter(
            str(params.get("python") or "python3"),
            repo_root=repo_root,
            state_dir=state_dir,
            field="launch_detached python",
        )
        argv = [
            launch_python,
            f"{repo_root}/scripts/goalflight_fleet_launch_detached.py",
            "launch",
            "--repo-root",
            repo_root,
            "--node-id",
            node_id,
            "--dispatch-id",
            dispatch_id,
            "--agent",
            agent,
            "--prompt-b64",
            prompt_b64,
            "--cwd",
            cwd,
            "--state-dir",
            state_dir,
            "--status-json",
            status_json,
            "--json",
        ]
        if bool(params.get("recover_unconfirmed")):
            argv.append("--recover-unconfirmed")
        argv.extend(["--base-sha", base_sha])
    elif command_class == "ferry_preflight":
        python = _validate_remote_interpreter(python, repo_root=repo_root, field="ferry_preflight python")
        allowed_roots = [_remote_norm(root) for root in list(params.get("allowed_roots") or []) if str(root or "").strip()]
        root = _validate_declared_remote_path(
            str(params.get("root") or ""),
            allowed_roots,
            field="ferry_preflight root",
        )
        files = [_validate_relative_remote_path(path, field="ferry_preflight file") for path in list(params.get("files") or [])]
        for rel in files:
            _validate_declared_remote_path(
                posixpath.join(root, rel),
                allowed_roots,
                field="ferry_preflight file",
            )
        payload = {
            "root": root,
            "files": files,
            "allowed_roots": allowed_roots,
        }
        if not payload["root"] or not payload["files"] or not payload["allowed_roots"]:
            raise SshAllowlistError("ferry_preflight requires root, files, and allowed_roots")
        payload_b64 = base64.b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8")).decode("ascii")
        argv = [
            python,
            f"{repo_root}/scripts/goalflight_fleet_ferry.py",
            "remote-preflight",
            "--payload-b64",
            payload_b64,
        ]
    elif command_class == "git_fetch":
        argv = ["git", "-C", repo_root, "fetch", "--quiet", "origin"]
    elif command_class == "git_status_porcelain":
        worktree_path = str(params.get("worktree_path") or repo_root)
        if not worktree_path:
            raise SshAllowlistError("git_status_porcelain requires worktree_path")
        state_dir_param = str(params.get("state_dir") or "").rstrip("/")
        state_dir = _validate_state_dir(
            state_dir_param,
            field="git_status_porcelain state_dir",
        ) if state_dir_param else None
        worktree_path = _validate_scoped_remote_path(
            worktree_path,
            repo_root=repo_root,
            state_dir=state_dir,
            allowed_roots=list(params.get("allowed_roots") or []),
            field="git_status_porcelain worktree_path",
        )
        argv = ["git", "-C", worktree_path, "status", "--porcelain", "--untracked-files=all"]
    elif command_class == "git_verify_commit":
        sha = str(params.get("sha") or "").strip()
        if not sha:
            raise SshAllowlistError("git_verify_commit requires sha")
        argv = ["git", "-C", repo_root, "rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}"]
    elif command_class == "git_checkout":
        ref = str(params.get("ref") or "HEAD")
        argv = ["git", "-C", repo_root, "checkout", ref]
    elif command_class == "git_worktree_add":
        path = str(params.get("worktree_path") or "")
        ref = str(params.get("ref") or "HEAD")
        if not path:
            raise SshAllowlistError("git_worktree_add requires worktree_path")
        state_dir_param = str(params.get("state_dir") or "").rstrip("/")
        state_dir = _validate_state_dir(
            state_dir_param,
            field="git_worktree_add state_dir",
        ) if state_dir_param else None
        path = _validate_scoped_remote_path(
            path,
            repo_root=repo_root,
            state_dir=state_dir,
            allowed_roots=list(params.get("allowed_roots") or []),
            field="git_worktree_add worktree_path",
        )
        argv = ["git", "-C", repo_root, "worktree", "add"]
        if bool(params.get("detach")):
            argv.append("--detach")
        argv.extend([path, ref])
    elif command_class == "git_worktree_remove":
        path = str(params.get("worktree_path") or "")
        if not path:
            raise SshAllowlistError("git_worktree_remove requires worktree_path")
        state_dir_param = str(params.get("state_dir") or "").rstrip("/")
        state_dir = _validate_state_dir(
            state_dir_param,
            field="git_worktree_remove state_dir",
        ) if state_dir_param else None
        path = _validate_scoped_remote_path(
            path,
            repo_root=repo_root,
            state_dir=state_dir,
            allowed_roots=list(params.get("allowed_roots") or []),
            field="git_worktree_remove worktree_path",
        )
        argv = ["git", "-C", repo_root, "worktree", "remove", path]
    elif command_class == "read_status_file":
        status_path = str(params.get("status_path") or "")
        if not status_path:
            raise SshAllowlistError("read_status_file requires status_path")
        state_dir = _validate_state_dir(
            str(params.get("state_dir") or "~/.goal-flight"),
            field="read_status_file state_dir",
        )
        status_path = _validate_declared_remote_path(
            status_path,
            [f"{state_dir}/dispatches"],
            field="status_path",
        )
        argv = ["cat", status_path]
    elif command_class == "read_lease_file":
        lease_path = str(params.get("lease_path") or "")
        if not lease_path:
            raise SshAllowlistError("read_lease_file requires lease_path")
        state_dir = _validate_state_dir(
            str(params.get("state_dir") or "~/.goal-flight"),
            field="read_lease_file state_dir",
        )
        lease_path = _validate_scoped_remote_path(
            lease_path,
            repo_root=repo_root,
            state_dir=state_dir,
            allowed_roots=list(params.get("allowed_roots") or []),
            field="read_lease_file lease_path",
        )
        argv = ["cat", lease_path]
    elif command_class == "pid_identity":
        pid_raw = str(params.get("pid") or "")
        if not pid_raw.isdigit():
            raise SshAllowlistError("pid_identity requires numeric pid")
        expected_lstart = str(params.get("expected_lstart") or "")
        ident_python = _validate_remote_interpreter(
            str(params.get("python") or "python3"),
            repo_root=repo_root,
            field="pid_identity python",
        )
        argv = [
            ident_python,
            f"{repo_root}/scripts/goalflight_fleet_launch_detached.py",
            "pid-identity",
            "--pid",
            pid_raw,
            "--json",
        ]
        if expected_lstart:
            lstart_b64 = base64.b64encode(expected_lstart.encode("utf-8")).decode("ascii")
            argv.extend(["--expected-lstart-b64", lstart_b64])
    elif command_class == "auth_probe":
        account_key = str(params.get("account_key") or "")
        if not account_key:
            raise SshAllowlistError("auth_probe requires account_key")
        state_dir = _validate_state_dir(
            str(params.get("state_dir") or "~/.goal-flight"),
            field="auth_probe state_dir",
        )
        auth_python = _validate_remote_interpreter(
            str(params.get("python") or f"{state_dir}/venvs/acp-0.10/bin/python"),
            repo_root=repo_root,
            state_dir=state_dir,
            field="auth_probe python",
        )
        argv = [
            auth_python,
            f"{repo_root}/scripts/goalflight_fleet_billing.py",
            "--fleet-dir",
            "/dev/null",
            "probe",
            "--account-key",
            account_key,
        ]
    else:
        raise SshAllowlistError(f"unsupported command class: {command_class}")

    validate_remote_argv(argv)
    return argv


def build_ssh_command(
    host: SshHostSpec,
    remote_argv: list[str],
    *,
    command_class: str | None = None,
    unsafe_remote: bool = False,
) -> list[str]:
    if command_class is not None:
        assert_allowed(command_class, unsafe_remote=unsafe_remote)
    validate_remote_argv(remote_argv)
    target = host.alias
    if host.user:
        target = f"{host.user}@{host.hostname}"
    else:
        target = host.hostname
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new"]
    if host.port:
        cmd.extend(["-p", str(host.port)])
    if host.identity_file:
        cmd.extend(["-i", str(Path(host.identity_file).expanduser())])
    cmd.append(target)
    cmd.append("--")
    cmd.extend(wrap_remote_argv(remote_argv))
    return cmd


def parse_ssh_config(alias: str, config_path: Path | None = None) -> SshHostSpec:
    path = config_path or Path.home() / ".ssh" / "config"
    if not path.exists():
        raise SshAllowlistError(f"ssh config not found: {path}", code="probe_failed")
    blocks: dict[str, dict[str, str]] = {}
    current: str | None = None
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        lower = line.lower()
        if lower.startswith("host "):
            current = line.split(None, 1)[1].strip()
            blocks.setdefault(current, {})
            continue
        if current is None:
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        key, value = parts[0].lower(), parts[1].strip()
        blocks[current][key] = value
    if alias not in blocks:
        import getpass

        if alias in {"localhost", "127.0.0.1", "::1"}:
            return SshHostSpec(
                alias=alias,
                hostname="127.0.0.1",
                user=getpass.getuser(),
            )
        raise SshAllowlistError(f"ssh Host alias not found: {alias}", code="probe_failed")
    stanza = blocks[alias]
    hostname = stanza.get("hostname", alias)
    user = stanza.get("user")
    port_raw = stanza.get("port")
    port = int(port_raw) if port_raw and port_raw.isdigit() else None
    identity = stanza.get("identityfile")
    return SshHostSpec(alias=alias, hostname=hostname, user=user, port=port, identity_file=identity)


def run_ssh(
    argv: list[str],
    *,
    runner: Callable[[list[str]], tuple[int, str, str]] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    if dry_run:
        return {"ok": True, "dry_run": True, "argv": argv, "stdout": "", "stderr": "", "exit_code": 0}
    if runner is None:
        import subprocess

        def _default_runner(cmd: list[str]) -> tuple[int, str, str]:
            proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
            return proc.returncode, proc.stdout, proc.stderr

        runner = _default_runner
    code, stdout, stderr = runner(argv)
    return {
        "ok": code == 0,
        "argv": argv,
        "stdout": stdout,
        "stderr": stderr,
        "exit_code": code,
    }


def host_from_node_entry(node_id: str, node_entry: dict[str, Any]) -> SshHostSpec:
    ssh_info = node_entry.get("ssh") or {}
    alias = str(ssh_info.get("alias") or node_id)
    port_raw = ssh_info.get("port")
    port = int(port_raw) if port_raw is not None and str(port_raw).isdigit() else None
    return SshHostSpec(
        alias=alias,
        hostname=str(ssh_info.get("hostname") or alias),
        user=ssh_info.get("user"),
        port=port,
        identity_file=ssh_info.get("identity_file"),
    )


def probe_ssh_reachable(
    host: SshHostSpec,
    *,
    runner: Callable[[list[str]], tuple[int, str, str]] | None = None,
    dry_run: bool = False,
) -> bool:
    remote = build_remote_command("probe_echo")
    ssh_argv = build_ssh_command(host, remote, command_class="probe_echo")
    run = run_ssh(ssh_argv, runner=runner, dry_run=dry_run)
    return bool(run.get("ok"))


def format_remediation(error: SshAllowlistError) -> str:
    return f"{error} | hint: {error.remediation}"


def cmd_validate(args) -> int:
    import argparse
    import json
    import sys

    if not isinstance(args, argparse.Namespace):
        raise TypeError("expected argparse.Namespace")
    try:
        assert_allowed(args.command_class, unsafe_remote=args.unsafe_remote)
        remote = build_remote_command(
            args.command_class,
            repo_root=args.repo_root,
            dispatch_id=args.dispatch_id,
            agent=args.agent,
            unsafe_remote=args.unsafe_remote,
        )
    except SshAllowlistError as exc:
        print(format_remediation(exc), file=sys.stderr)
        return 1
    if args.alias:
        host = parse_ssh_config(args.alias, args.ssh_config)
    else:
        host = SshHostSpec(alias="local", hostname="localhost")
    ssh_cmd = build_ssh_command(
        host,
        remote,
        command_class=args.command_class,
        unsafe_remote=args.unsafe_remote,
    )
    print(json.dumps({"remote": remote, "ssh": ssh_cmd}, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Goal Flight fleet SSH allowlist")
    sub = parser.add_subparsers(dest="cmd", required=True)

    validate = sub.add_parser("validate")
    validate.add_argument("--command-class", required=True)
    validate.add_argument("--repo-root", default="/tmp/goal-flight")
    validate.add_argument("--alias")
    validate.add_argument("--ssh-config", type=Path)
    validate.add_argument("--dispatch-id")
    validate.add_argument("--agent")
    validate.add_argument("--unsafe-remote", action="store_true")
    validate.set_defaults(func=cmd_validate)

    explain = sub.add_parser("remediation")
    explain.add_argument("--code", default="allowlist_blocked")
    explain.set_defaults(
        func=lambda args: print(REMEDIATION_HINTS.get(args.code, REMEDIATION_HINTS["allowlist_blocked"])) or 0
    )

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
