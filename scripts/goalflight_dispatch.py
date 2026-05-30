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
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import goalflight_compat

SCRIPT_DIR = Path(__file__).resolve().parent
WATCH_PY = SCRIPT_DIR / "goalflight_watch.py"


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
        return args.prompt_file
    if args.prompt is not None:
        base.mkdir(parents=True, exist_ok=True)
        pf = base / f"{args.dispatch_id}.prompt"
        pf.write_text(args.prompt, encoding="utf-8")
        return str(pf)
    return None


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
        if prompt_path:
            argv = ["grok", "--prompt-file", str(prompt_path), "--permission-mode", "acceptEdits"]
        else:
            argv = ["grok", "-p", args.prompt or "", "--permission-mode", "acceptEdits"]
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
                             "worker pools, e.g. several people's codex Pro on one Mac Studio). Resolves "
                             "to CODEX_HOME=~/.goal-flight/accounts/<name>/codex. Default: the host's "
                             "logged-in account.")
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

    # Auto-derive id + paths so the common call is one line.
    args.dispatch_id = args.dispatch_id or f"{args.agent}-{os.getpid()}-{int(time.time())}"
    base = goalflight_compat.default_state_dir() / "dispatch"
    tail = Path(args.tail) if args.tail else base / f"{args.dispatch_id}.tail"
    status_json = Path(args.status_json) if args.status_json else base / f"{args.dispatch_id}.status.json"

    raw = args.worker[1:] if args.worker and args.worker[0] == "--" else args.worker
    prompt_path = None if raw else _resolve_prompt_file(args, base)
    worker_argv, stdin_path = build_worker(args, prompt_path, raw)
    if not worker_argv:
        print("goalflight_dispatch: no worker — use `--agent codex --prompt-file X [--cwd .]` "
              "or `-- <cmd...>`", file=sys.stderr)
        return 64

    tail.parent.mkdir(parents=True, exist_ok=True)

    # 1. Launch the worker DETACHED, output -> tail (prompt -> stdin for codex).
    # Account + billing -> worker environment. Account selects which subscription
    # (CODEX_HOME) for shared pools; billing stays 'sub' (strip OPENAI_API_KEY) by
    # default so codex uses that account's Pro plan, not the API.
    env = dict(os.environ)
    if args.account:
        codex_home = Path.home() / ".goal-flight" / "accounts" / args.account / "codex"
        if not codex_home.exists():
            print(f"goalflight_dispatch: --account {args.account} not configured (expected "
                  f"{codex_home}). Set that account's creds there, or omit --account for the host "
                  f"default. Refusing to bill the wrong account.", file=sys.stderr)
            return 64
        env["CODEX_HOME"] = str(codex_home)
    if args.billing == "sub" and args.agent == "codex":
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

    # Reap the worker when it exits so it does NOT linger as a POSIX zombie. An
    # un-reaped child zombie still satisfies os.kill(pid, 0), which makes the
    # watcher's pid_alive() falsely report the worker alive and defeats crash
    # detection (the watcher would then only escape via the much slower
    # idle-timeout, not prompt pid-death). Daemon thread so it never blocks our
    # own exit. (Windows has no zombies; the reaper is harmless there.)
    threading.Thread(target=worker.wait, daemon=True).start()

    summary_head = {
        "dispatch_id": args.dispatch_id,
        "agent": args.agent,
        "worker_pid": worker.pid,
        "tail": str(tail),
        "status_json": str(status_json),
    }
    print("DISPATCH-START " + json.dumps(summary_head, sort_keys=True), flush=True)

    # 2. Run the decoupled watcher in the FOREGROUND (it is our only real child).
    watch_cmd = [
        sys.executable, str(WATCH_PY),
        "--pid", str(worker.pid),
        "--tail", str(tail),
        "--status-json", str(status_json),
        "--agent", args.agent,
        "--poll-secs", str(args.poll_secs),
        "--max-idle-secs", str(args.max_idle_secs),
        "--dispatch-id", args.dispatch_id,
    ]
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
    worker_alive = None
    reason = None
    try:
        rec = json.loads(status_json.read_text(encoding="utf-8", errors="replace"))
        state = rec.get("state")
        worker_alive = rec.get("worker_alive")
    except Exception:
        pass
    if watch.stdout:
        try:
            reason = json.loads(watch.stdout.strip().splitlines()[-1]).get("reason")
        except Exception:
            reason = watch.stdout.strip().splitlines()[-1] if watch.stdout.strip() else None

    # 3. Emit a summary and propagate the watcher's REAL exit code (no masking).
    print("DISPATCH-END " + json.dumps({
        **summary_head,
        "watcher_exit": watch_rc,
        "terminal_state": state,
        "worker_still_alive": worker_alive,  # True on a marker => signal-to-review, NOT done; re-attach
        "reason": reason,
        "elapsed_s": round(time.time() - started, 1),
    }, sort_keys=True), flush=True)
    return watch_rc


if __name__ == "__main__":
    raise SystemExit(main())
