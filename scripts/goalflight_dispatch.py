#!/usr/bin/env python3
"""goalflight_dispatch.py — crash-safe worker dispatch with a decoupled watcher.

ONE command, run via the host's background-task mechanism, that dispatches a
worker AND reliably wakes the controller on every terminal state. It fixes the
"controller hangs because the worker crashed/hung and never sent a wakeup" class
(observed 2026-05-30).

Easy path (agent preset — the common case):
    python3 goalflight_dispatch.py --agent codex --prompt-file p.md --cwd .
    python3 goalflight_dispatch.py --agent codex --prompt-file p.md --read-only   # review/analysis
    python3 goalflight_dispatch.py --agent grok  --prompt-file p.md --cwd .

Presets bake in the canonical NON-INTERACTIVE + SAFE flags per worker, so you
never spell them out (and cannot fat-finger `--dangerously-bypass`). Paths and a
dispatch id are auto-derived under the state dir; override with --tail /
--status-json / --dispatch-id if you want.

Escape hatch (any worker): pass the raw command after `--`:
    python3 goalflight_dispatch.py --agent custom --tail t --status-json s -- <cmd...>

How it stays crash-safe (validated):
  1. The worker is launched DETACHED (own session/process-group) so its tree is
     not this process's child tree (a lingering worker child can't keep us alive).
  2. The worker is REAPED by a daemon thread so it can't become a POSIX zombie
     (an un-reaped zombie satisfies os.kill(pid,0) and would defeat crash
     detection -> the watcher would only escape via the slow idle-timeout).
  3. The decoupled watcher (goalflight_watch.py) detects finished(0)/crashed(1)/
     hung(2)/controller-dead(3)/blocked(4) and we exit with ITS code UNCHANGED,
     so the host completion notification carries the real terminal state.

Cross-platform: pure stdlib; the watcher uses goalflight_compat.pid_alive, so
this is also the dispatch path on Windows (where the bash watcher is refused).
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import goalflight_compat
import goalflight_capacity
import goalflight_ledger
from goalflight_liveness import process_group_id, write_status

SCRIPT_DIR = Path(__file__).resolve().parent
WATCH_PY = SCRIPT_DIR / "goalflight_watch.py"
PRESET_AGENTS = {"codex", "grok"}
STDIN_PROMPT_AGENTS = {"codex", "grok"}
ACCOUNT_ENGINE_BY_AGENT = {
    "codex": "codex",
    "codex-acp": "codex",
    "grok": "grok",
    "grok-acp": "grok",
    "cursor": "cursor",
    "cursor-agent": "cursor",
}


class DispatchUsageError(Exception):
    pass


def _detached_popen_kwargs() -> dict:
    """Launch the worker in its own session/process-group so its tree is decoupled
    from this dispatcher (a lingering worker child must not keep our group alive)."""
    if os.name == "nt":  # pragma: no cover - Windows only
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
            subprocess, "DETACHED_PROCESS", 0
        )
        return {"creationflags": flags}
    return {"start_new_session": True}


def _resolve_prompt_file(args, base: Path) -> str | None:
    """Normalize --prompt/--prompt-file to a file path (for stdin-fed workers)."""
    if args.prompt_file:
        return str(Path(args.prompt_file).expanduser())
    if args.prompt is not None:
        base.mkdir(parents=True, exist_ok=True)
        pf = base / f"{args.dispatch_id}.prompt"
        pf.write_text(args.prompt, encoding="utf-8")
        return str(pf)
    return None


def _project_root(args) -> Path:
    return Path(args.cwd).resolve() if args.cwd else Path.cwd().resolve()


def _state_dir() -> Path:
    return Path(
        os.environ.get("GOALFLIGHT_STATE_DIR", str(goalflight_compat.default_state_dir()))
    ).expanduser()


def _dispatch_base_dir() -> Path:
    return _state_dir() / "dispatch"


def _raw_worker_args(args) -> list[str]:
    return args.worker[1:] if args.worker and args.worker[0] == "--" else args.worker


def _prompt_requested(args) -> bool:
    return bool(args.prompt_file) or args.prompt is not None


def _validate_before_side_effects(args, raw_argv: list[str]) -> None:
    if raw_argv:
        return
    if args.agent not in PRESET_AGENTS:
        raise DispatchUsageError(
            "no worker preset for --agent "
            f"{args.agent!r} — use --agent codex|grok with --prompt/--prompt-file, "
            "or pass a raw worker after `-- <cmd...>`"
        )
    if args.agent in STDIN_PROMPT_AGENTS and not _prompt_requested(args):
        raise DispatchUsageError(
            f"--agent {args.agent} requires --prompt or --prompt-file; refusing to feed empty stdin"
        )
    if args.prompt_file and not Path(args.prompt_file).expanduser().exists():
        raise DispatchUsageError(f"prompt file not found: {args.prompt_file}")


def _default_dispatch_id(agent: str) -> str:
    return os.environ.get("GOALFLIGHT_DISPATCH_ID_SEED") or f"{agent}-{os.getpid()}-{int(time.time())}"


def _reserve_auto_dispatch_id(agent: str, base: Path) -> str:
    stem = _default_dispatch_id(agent)
    ids_dir = base / ".dispatch-ids"
    ids_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    for attempt in range(1000):
        dispatch_id = stem if attempt == 0 else f"{stem}-{attempt + 1}"
        lock_path = ids_dir / f"{dispatch_id}.json"
        try:
            fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "dispatch_id": dispatch_id,
                    "agent": agent,
                    "reserved_at": int(time.time()),
                    "pid": os.getpid(),
                },
                fh,
                sort_keys=True,
            )
            fh.write("\n")
        return dispatch_id
    raise DispatchUsageError(f"could not reserve a dispatch id for stem {stem!r}")


def _controller_pid(args) -> int:
    return int(args.controller_pid or os.getpid())


def _account_engine(agent: str) -> str | None:
    return ACCOUNT_ENGINE_BY_AGENT.get(agent)


def _account_home(account: str, engine: str) -> Path:
    return Path.home() / ".goal-flight" / "accounts" / account / engine


def _apply_home_env(env: dict[str, str], home: Path) -> None:
    env["HOME"] = str(home)
    env["XDG_CONFIG_HOME"] = str(home / ".config")
    env["XDG_STATE_HOME"] = str(home / ".local" / "state")
    env["XDG_DATA_HOME"] = str(home / ".local" / "share")


def _cursor_account_probe(env: dict[str, str]) -> tuple[bool, str | None]:
    try:
        proc = subprocess.run(
            ["cursor-agent", "status"],
            env=env,
            text=True,
            capture_output=True,
            timeout=10,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    combined = f"{proc.stdout}\n{proc.stderr}".strip()
    negative = ("not logged in", "not authenticated", "login required", "please log in")
    positive = ("logged in", "login successful")
    lowered = combined.lower()
    if proc.returncode == 0 and any(term in lowered for term in positive) and not any(term in lowered for term in negative):
        return True, None
    return False, combined[-400:] or f"cursor-agent status exited {proc.returncode}"


def _resolve_account_env(args) -> dict[str, str]:
    if not args.account:
        return {}
    engine = _account_engine(args.agent)
    if not engine:
        raise DispatchUsageError(
            f"--account is not configured for --agent {args.agent!r}; refusing to bill the wrong account"
        )
    home = _account_home(args.account, engine)
    if engine == "codex":
        if not home.exists():
            raise DispatchUsageError(
                f"--account {args.account} not configured (expected {home}). "
                "Set that account's creds there, or omit --account for the host default. "
                "Refusing to bill the wrong account."
            )
        return {"CODEX_HOME": str(home)}
    if not home.exists():
        raise DispatchUsageError(
            f"--account {args.account} not configured for {engine} (expected HOME {home}). "
            "Refusing to bill the wrong account."
        )
    env = dict(os.environ)
    _apply_home_env(env, home)
    if engine == "grok":
        env.pop("GROK_API_KEY", None)
        env.pop("XAI_API_KEY", None)
        auth = home / ".grok" / "auth.json"
        if not auth.is_file() or auth.stat().st_size == 0:
            raise DispatchUsageError(
                f"--account {args.account} lacks grok creds (expected non-empty {auth}). "
                "Refusing to bill the wrong account."
            )
        return {key: env[key] for key in ("HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME")}
    if engine == "cursor":
        env.pop("CURSOR_API_KEY", None)
        ok, reason = _cursor_account_probe(env)
        if not ok:
            raise DispatchUsageError(
                f"--account {args.account} lacks cursor creds ({reason}). "
                "Refusing to bill the wrong account."
            )
        return {key: env[key] for key in ("HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME")}
    raise DispatchUsageError(f"--account unsupported for engine {engine!r}")


def _acquire_capacity(args, *, project_root: Path, status_json: Path) -> str | None:
    lease_ttl_s = max(int(args.max_idle_secs or 300) * 4, 3600)
    acquire_args = argparse.Namespace(
        agent=args.agent,
        dispatch_id=args.dispatch_id,
        prompt_id=None,
        project_root=str(project_root),
        worktree_path=None,
        worker_cwd=str(project_root),
        controller_pid=_controller_pid(args),
        worker_pid=None,
        lease_id=None,
        mem_mb=None,
        agent_cap=None,
        ttl_s=lease_ttl_s,
        ram_mb=None,
        reserve_mb=goalflight_capacity.DEFAULT_RESERVE_MB,
        worst_worker_mb=goalflight_capacity.DEFAULT_WORST_WORKER_MB,
        hard_cap=goalflight_capacity.DEFAULT_HARD_CAP,
        max_total=None,
    )
    acquire_out = io.StringIO()
    with contextlib.redirect_stdout(acquire_out):
        rc = goalflight_capacity.cmd_acquire(acquire_args)
    try:
        payload = json.loads(acquire_out.getvalue() or "{}")
    except json.JSONDecodeError:
        payload = {"raw": acquire_out.getvalue()}
    if rc != 0:
        write_status(
            status_json,
            {
                "schema": "goalflight.status.v1",
                "dispatch_id": args.dispatch_id,
                "agent": args.agent,
                "state": "blocked_capacity",
                "reason": payload,
                "worker_alive": False,
                "updated_at": int(time.time()),
            },
        )
        print("DISPATCH-BLOCKED " + json.dumps({"state": "blocked_capacity", "reason": payload}, sort_keys=True), flush=True)
        raise SystemExit(rc)
    return payload.get("lease", {}).get("lease_id")


def _record_ledger(args, *, project_root: Path, prompt_path: str | None, status_json: Path,
                   tail: Path, lease_id: str | None, worker_pid: int | None, state: str) -> None:
    with contextlib.redirect_stdout(io.StringIO()):
        goalflight_ledger.cmd_record(
            argparse.Namespace(
                dispatch_id=args.dispatch_id,
                prompt_id=None,
                prompt_path=prompt_path,
                agent=args.agent,
                transport="dispatch",
                project_root=str(project_root),
                controller_pid=_controller_pid(args),
                worker_pid=worker_pid,
                acp_session_id=None,
                logical_session_id=args.dispatch_id,
                lease_id=lease_id,
                stdout_path=str(tail),
                stderr_path=None,
                status_path=str(status_json),
                os_sandbox_json=json.dumps({"shape": "bash", "read_only": bool(args.read_only)}, sort_keys=True),
                state=state,
                json=True,
            )
        )


def _attach_worker_to_lease(lease_id: str | None, worker_pid: int) -> None:
    if not lease_id:
        return
    with goalflight_capacity.StateLock():
        data = goalflight_capacity.load_state()
        lease = data.get("leases", {}).get(lease_id)
        if lease:
            lease["worker_pid"] = worker_pid
            goalflight_capacity.save_state(data)


def _pidfile_dir() -> Path:
    return Path(
        os.environ.get(
            "GOAL_FLIGHT_PIDFILE_DIR",
            goalflight_compat.temp_base() / "goal-flight-acp-pids.d",
        )
    )


def _identity_token(identity: dict | None) -> dict | None:
    if not identity:
        return None
    return {key: identity.get(key) for key in ("pid", "lstart", "comm") if identity.get(key)}


def _process_identity_after_spawn(worker_pid: int) -> dict | None:
    ident = None
    for _ in range(20):
        current = goalflight_ledger.process_identity(worker_pid)
        if current:
            ident = current
        if current and current.get("lstart") and current.get("comm"):
            break
        time.sleep(0.05)
    return ident


def _write_pidfile(args, *, worker_pid: int, pgid: int | None, identity: dict | None = None) -> Path | None:
    ident = identity or _process_identity_after_spawn(worker_pid)
    if not ident:
        return None
    pidfile_dir = _pidfile_dir()
    pidfile_dir.mkdir(parents=True, exist_ok=True)
    controller_pid = _controller_pid(args)
    pidfile = pidfile_dir / f"{controller_pid}.bashtail.{worker_pid}.jsonl"
    entry = {
        "controller_pid": controller_pid,
        "pid": worker_pid,
        "pgid": int(pgid or worker_pid),
        "started_at": ident.get("lstart"),
        "cmd": ident.get("comm"),
        "agent": f"{args.agent}-dispatch",
        "session_id": args.dispatch_id,
    }
    pidfile.write_text(json.dumps(entry, sort_keys=True) + "\n", encoding="utf-8")
    return pidfile


def _cleanup_pidfile_if_worker_dead(pidfile: Path | None, worker_pid: int | None) -> None:
    if not pidfile or not worker_pid:
        return
    if goalflight_compat.pid_alive(worker_pid):
        return
    with contextlib.suppress(OSError):
        pidfile.unlink(missing_ok=True)


def _finish_ledger(dispatch_id: str, state: str, reason: str | None) -> None:
    with contextlib.redirect_stdout(io.StringIO()):
        goalflight_ledger.cmd_finish(argparse.Namespace(dispatch_id=dispatch_id, state=state, reason=reason))


def _release_capacity(lease_id: str | None, state: str, reason: str | None) -> None:
    if not lease_id:
        return
    with contextlib.redirect_stdout(io.StringIO()):
        goalflight_capacity.cmd_release(argparse.Namespace(lease_id=lease_id, state=state, reason=reason, keep=True))


def _registration_error(step: str, exc: Exception) -> dict:
    return {"step": step, "reason": f"{type(exc).__name__}: {exc}"}


def build_worker(args, prompt_path, raw_argv: list[str]):
    """Return (argv, stdin_path). Explicit `-- <cmd>` overrides any preset.
    Presets encode the canonical SAFE, non-interactive invocation per worker.
    `prompt_path` is the already-materialized prompt file (or None for raw)."""
    if raw_argv:
        return raw_argv, None  # raw escape hatch; stdin = DEVNULL
    sandbox = "read-only" if args.read_only else "workspace-write"
    if args.agent == "codex":
        argv = ["codex", "exec", "--skip-git-repo-check", "--sandbox", sandbox,
                "-c", "approval_policy=never"]
        if args.cwd:
            argv += ["-C", args.cwd]
        argv += ["-"]  # codex reads the prompt from stdin
        return argv, prompt_path
    if args.agent == "grok":
        # Read the prompt from a FILE, not argv `-p` — long goal-flight prompts
        # (5-20KB) would hit E2BIG / argv truncation (grok review #5).
        argv = ["grok", "--prompt-file", str(prompt_path), "--permission-mode", "acceptEdits"]
        if args.cwd:
            argv += ["--cwd", args.cwd]
        return argv, None
    return None, None  # unknown preset + no raw command


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Crash-safe worker dispatch: detached worker + decoupled watcher."
    )
    parser.add_argument("--agent", default="worker",
                        help="Preset (codex|grok) OR a label when you pass `-- <cmd>`")
    parser.add_argument("--prompt-file", help="Prompt file (preset path)")
    parser.add_argument("--prompt", help="Inline prompt text (preset path; alternative to --prompt-file)")
    parser.add_argument("--cwd", help="Worker working directory")
    parser.add_argument("--read-only", action="store_true",
                        help="Read-only sandbox (review/analysis dispatches)")
    parser.add_argument("--account",
                        help="Which subscription account/profile to bill the worker to (shared remote "
                             "worker pools). Codex resolves to CODEX_HOME=~/.goal-flight/accounts/<name>/codex; "
                             "grok/cursor resolve to HOME=~/.goal-flight/accounts/<name>/<engine>. "
                             "Default: the host's logged-in account.")
    parser.add_argument("--billing", choices=["sub", "api"], default="sub",
                        help="ALWAYS 'sub' (subscription) in normal use — the default; 'sub' strips "
                             "OPENAI_API_KEY so codex uses the selected account's Pro plan, never the API. "
                             "'api' is a de-emphasized by-request escape hatch (bills the API) — never the "
                             "default, not used by the maintainer; present only for users who explicitly want it.")
    parser.add_argument("--shape", choices=["auto", "bash", "acp"], default="auto",
                        help="Comms shape. 'auto' picks the best per engine (codex/grok->bash, "
                             "cursor/claude->acp). 'acp' unification is pending; use 'bash' or the raw escape hatch.")
    parser.add_argument("--tail", help="Worker output sink (auto: <state>/dispatch/<id>.tail)")
    parser.add_argument("--status-json", help="Watcher status file (auto: <state>/dispatch/<id>.status.json)")
    parser.add_argument("--dispatch-id", help="Slug for auto paths (auto-generated if omitted)")
    parser.add_argument("--poll-secs", type=float, default=2.0)
    parser.add_argument("--max-idle-secs", type=float, default=180.0)
    parser.add_argument("--controller-pid", type=int,
                        help="If set, watcher exits when this pid dies (orphan guard)")
    parser.add_argument("worker", nargs=argparse.REMAINDER,
                        help="Optional `-- <cmd...>` raw worker (overrides the preset)")
    args = parser.parse_args()
    raw = _raw_worker_args(args)

    # Resolve comms shape. 'auto' = best per engine; 'acp' unification is not wired
    # here yet, so fail honestly rather than silently doing bash.
    shape = args.shape
    if shape == "auto":
        shape = "acp" if args.agent in ("cursor", "claude-acp", "claude") else "bash"
    if shape == "acp":
        print("goalflight_dispatch: --shape acp is not yet wired here — use --shape bash, a codex/grok "
              "preset, or scripts/goalflight_acp_run.py directly (acp unification is on the backlog).",
              file=sys.stderr)
        return 64

    try:
        _validate_before_side_effects(args, raw)
        account_env = _resolve_account_env(args)
    except DispatchUsageError as e:
        print(f"goalflight_dispatch: {e}", file=sys.stderr)
        return 64

    # Auto-derive id + paths so the common call is one line.
    base = _dispatch_base_dir()
    if not args.dispatch_id:
        try:
            args.dispatch_id = _reserve_auto_dispatch_id(args.agent, base)
        except DispatchUsageError as e:
            print(f"goalflight_dispatch: {e}", file=sys.stderr)
            return 64
    tail = Path(args.tail) if args.tail else base / f"{args.dispatch_id}.tail"
    status_json = Path(args.status_json) if args.status_json else base / f"{args.dispatch_id}.status.json"

    prompt_path = None if raw else _resolve_prompt_file(args, base)
    worker_argv, stdin_path = build_worker(args, prompt_path, raw)
    if not worker_argv:
        print("goalflight_dispatch: no worker — use `--agent codex --prompt-file X [--cwd .]` "
              "or `-- <cmd...>`", file=sys.stderr)
        return 64

    tail.parent.mkdir(parents=True, exist_ok=True)
    project_root = _project_root(args)
    worker = None
    worker_pid = None
    pidfile = None
    lease_id = None
    ledger_recorded = False
    final_state = "failed"
    final_reason = None
    worker_alive = None
    watch_rc = 1
    summary_head = {
        "dispatch_id": args.dispatch_id,
        "agent": args.agent,
        "worker_pid": None,
        "tail": str(tail),
        "status_json": str(status_json),
    }

    try:
        lease_id = _acquire_capacity(args, project_root=project_root, status_json=status_json)
        _record_ledger(
            args,
            project_root=project_root,
            prompt_path=prompt_path,
            status_json=status_json,
            tail=tail,
            lease_id=lease_id,
            worker_pid=None,
            state="starting",
        )
        ledger_recorded = True

        # 1. Launch the worker DETACHED, output -> tail (prompt -> stdin for codex).
        # Account guards ran before prompt/id/lease side effects; only apply the
        # resolved environment here.
        env = dict(os.environ)
        env.update(account_env)
        if args.account and _account_engine(args.agent) == "grok":
            env.pop("GROK_API_KEY", None)
            env.pop("XAI_API_KEY", None)
        if args.account and _account_engine(args.agent) == "cursor":
            env.pop("CURSOR_API_KEY", None)
        if args.billing == "sub" and _account_engine(args.agent) == "codex":
            env.pop("OPENAI_API_KEY", None)  # subscription billing for the selected account, not the API

        stdin_f = open(stdin_path, "rb") if stdin_path else subprocess.DEVNULL
        try:
            with tail.open("wb") as sink:
                worker = subprocess.Popen(
                    worker_argv,
                    stdout=sink,
                    stderr=subprocess.STDOUT,
                    stdin=stdin_f,
                    env=env,
                    **_detached_popen_kwargs(),
                )
        finally:
            if stdin_path:
                stdin_f.close()

        started = time.time()
        worker_pid = worker.pid
        registration_errors = []
        # Reap the worker when it exits so it does NOT linger as a POSIX zombie. An
        # un-reaped child zombie still satisfies os.kill(pid, 0), which makes the
        # watcher's pid_alive() falsely report the worker alive and defeats crash
        # detection (the watcher would then only escape via the much slower
        # idle-timeout, not prompt pid-death). Daemon thread so it never blocks our
        # own exit. (Windows has no zombies; the reaper is harmless there.)
        try:
            threading.Thread(target=worker.wait, daemon=True).start()
        except Exception as e:
            registration_errors.append(_registration_error("start_reaper", e))

        worker_identity = None
        try:
            worker_identity = _process_identity_after_spawn(worker_pid)
        except Exception as e:
            registration_errors.append(_registration_error("process_identity", e))
        worker_identity_token = _identity_token(worker_identity)

        pgid = worker_pid
        try:
            pgid = process_group_id(worker_pid) or worker_pid
        except Exception as e:
            registration_errors.append(_registration_error("process_group_id", e))
        try:
            _attach_worker_to_lease(lease_id, worker_pid)
        except Exception as e:
            registration_errors.append(_registration_error("attach_worker_to_lease", e))
        try:
            pidfile = _write_pidfile(args, worker_pid=worker_pid, pgid=pgid, identity=worker_identity)
        except Exception as e:
            registration_errors.append(_registration_error("write_pidfile", e))
            pidfile = None
        try:
            _record_ledger(
                args,
                project_root=project_root,
                prompt_path=prompt_path,
                status_json=status_json,
                tail=tail,
                lease_id=lease_id,
                worker_pid=worker_pid,
                state="running",
            )
        except Exception as e:
            registration_errors.append(_registration_error("record_ledger_running", e))

        summary_head.update({"worker_pid": worker_pid, "worker_identity": worker_identity_token})
        if registration_errors:
            summary_head["registration_errors"] = registration_errors
            with contextlib.suppress(Exception):
                print("DISPATCH-REGISTRATION-WARN " + json.dumps({
                    "dispatch_id": args.dispatch_id,
                    "errors": registration_errors,
                }, sort_keys=True), file=sys.stderr, flush=True)
        with contextlib.suppress(Exception):
            print("DISPATCH-START " + json.dumps(summary_head, sort_keys=True), flush=True)

        # 2. Run the decoupled watcher in the FOREGROUND (it is our only real child).
        watch_cmd = [
            sys.executable, str(WATCH_PY),
            "--pid", str(worker_pid),
            "--tail", str(tail),
            "--status-json", str(status_json),
            "--agent", args.agent,
            "--poll-secs", str(args.poll_secs),
            "--max-idle-secs", str(args.max_idle_secs),
            "--dispatch-id", args.dispatch_id,
        ]
        watch_identity_token = (
            worker_identity_token
            if worker_identity_token and worker_identity_token.get("lstart") and worker_identity_token.get("comm")
            else None
        )
        if watch_identity_token:
            watch_cmd += ["--worker-identity-json", json.dumps(watch_identity_token, sort_keys=True)]
        if args.controller_pid:
            watch_cmd += ["--controller-pid", str(args.controller_pid)]
        if prompt_path:
            watch_cmd += ["--ignore-prompt-file", str(prompt_path)]

        watch = subprocess.run(watch_cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        watch_rc = watch.returncode

        # Read the terminal state the watcher recorded (best-effort). worker_still_alive
        # matters: a terminal marker is a NON-DESTRUCTIVE signal (we never kill the
        # worker), so if it is still alive the controller should re-attach a watcher to
        # keep following it, not assume it is finished.
        state = None
        try:
            rec = json.loads(status_json.read_text(encoding="utf-8", errors="replace"))
            state = rec.get("state")
            worker_alive = rec.get("worker_alive")
        except Exception:
            pass
        if watch.stdout:
            try:
                final_reason = json.loads(watch.stdout.strip().splitlines()[-1]).get("reason")
            except Exception:
                final_reason = watch.stdout.strip().splitlines()[-1] if watch.stdout.strip() else None
        final_state = state or ("complete" if watch_rc == 0 else "failed")

        # 3. Emit a summary and propagate the watcher's REAL exit code (no masking).
        print("DISPATCH-END " + json.dumps({
            **summary_head,
            "watcher_exit": watch_rc,
            "terminal_state": state,
            "worker_still_alive": worker_alive,  # True on a marker => signal-to-review, NOT done; re-attach
            "reason": final_reason,
            "elapsed_s": round(time.time() - started, 1),
        }, sort_keys=True), flush=True)
        return watch_rc
    except SystemExit:
        raise
    except Exception as e:
        final_state = "failed"
        final_reason = f"{type(e).__name__}: {e}"
        print("DISPATCH-ERROR " + json.dumps({"state": final_state, "reason": final_reason}, sort_keys=True), file=sys.stderr, flush=True)
        return 1
    finally:
        if ledger_recorded:
            with contextlib.suppress(Exception):
                _finish_ledger(args.dispatch_id, final_state, final_reason)
        with contextlib.suppress(Exception):
            _release_capacity(lease_id, final_state, final_reason)
        _cleanup_pidfile_if_worker_dead(pidfile, worker_pid)


if __name__ == "__main__":
    raise SystemExit(main())
