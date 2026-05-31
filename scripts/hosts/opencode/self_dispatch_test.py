#!/usr/bin/env python3
"""OpenCode controller self-dispatch test (nondestructive).

Verifies an OpenCode Goal Flight controller can dispatch read-only workers
to itself on both supported transports:

  - ACP: ``goalflight_acp_run.py --agent opencode``
  - bash-tail: ``scripts/hosts/opencode/bash_tail.py`` + ``watch-dispatch-tail.sh``

Also runs doctor + capacity preflight and returns compact JSON evidence for
the controller conversation (no raw worker logs).

Usage:

  source ~/.config/rpp/litellm.env   # when using litellm/* models
  ./scripts/hosts/opencode/self_dispatch_test.py --json
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_DIR = REPO_ROOT / "scripts"
HOST_DIR = SCRIPT_DIR / "hosts" / "opencode"
DEFAULT_MODEL = "litellm/nano"
READONLY_PROMPT = "What is 2+2? Reply with just the number on one line."
SCHEMA = "goalflight.opencode-self-dispatch.v1"


def _load_litellm_env() -> None:
    if os.environ.get("LITELLM_API_KEY") or os.environ.get("LITELLM_MASTER_KEY"):
        return
    env_file = Path.home() / ".config/rpp/litellm.env"
    if not env_file.is_file():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        value = value.strip().strip("'\"")
        os.environ.setdefault(key, value)


def _run(cmd: list[str], *, cwd: Path | None = None, timeout: float = 300) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "returncode": 124,
            "stdout": exc.stdout or "",
            "stderr": (exc.stderr or "") + "\nTIMEOUT",
        }


def _doctor_snapshot(project_root: Path) -> dict[str, Any]:
    doctor = SCRIPT_DIR / "goalflight_doctor.py"
    if not doctor.is_file():
        return {"ok": False, "error": "goalflight_doctor.py missing"}
    result = _run([sys.executable, str(doctor), "--project-root", str(project_root), "--json"], timeout=120)
    if not result["ok"]:
        return {"ok": False, "error": "doctor failed", "detail": result["stderr"][:500]}
    try:
        payload = json.loads(result["stdout"])
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": f"doctor json parse: {exc}"}
    return {
        "ok": True,
        "opencode_present": payload.get("opencode", {}).get("present"),
        "opencode_version": payload.get("opencode", {}).get("version"),
        "host_install_ok": payload.get("host_goalflight_install", {}).get("opencode", {}).get("ok"),
        "acp_sdk_ok": payload.get("acp", {}).get("sdk", {}).get("ok"),
        "project_readiness_ok": payload.get("project_goalflight_readiness", {}).get("ok"),
    }


def _capacity_snapshot() -> dict[str, Any]:
    cap = SCRIPT_DIR / "goalflight_capacity.py"
    result = _run([sys.executable, str(cap), "status", "--json"], timeout=30)
    if not result["ok"]:
        return {"ok": False, "error": "capacity status failed", "detail": result["stderr"][:500]}
    try:
        payload = json.loads(result["stdout"])
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": f"capacity json parse: {exc}"}
    agents = payload.get("agents") or {}
    return {
        "ok": True,
        "opencode_acp": agents.get("opencode-acp") or agents.get("opencode"),
        "opencode_bash_tail": agents.get("opencode-bash-tail"),
    }


def _prepare_workdir(project_root: Path) -> Path:
    workdir = Path(tempfile.mkdtemp(prefix="opencode-self-dispatch-"))
    for candidate in (
        project_root / "opencode.json",
        REPO_ROOT / "configs/opencode/opencode.json",
    ):
        if candidate.is_file():
            shutil.copy2(candidate, workdir / "opencode.json")
            break
    return workdir


def _reply_has_four(text: str) -> bool:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "4" or stripped.endswith(" 4") or stripped.startswith("4"):
            return True
    return "4" in text


def _run_acp(*, workdir: Path, model: str, timeout: float) -> dict[str, Any]:
    runner = SCRIPT_DIR / "goalflight_acp_run.py"
    old_state_dir = os.environ.get("GOALFLIGHT_STATE_DIR")
    try:
        with tempfile.TemporaryDirectory(prefix="gf-opencode-self-dispatch-") as state_tmp:
            os.environ["GOALFLIGHT_STATE_DIR"] = str(Path(state_tmp) / "state")
            result = _run(
                [
                    sys.executable,
                    str(runner),
                    "--agent",
                    "opencode",
                    "--cwd",
                    str(workdir),
                    "--prompt-text",
                    READONLY_PROMPT,
                    "--json",
                ],
                timeout=timeout,
            )
    finally:
        if old_state_dir is None:
            os.environ.pop("GOALFLIGHT_STATE_DIR", None)
        else:
            os.environ["GOALFLIGHT_STATE_DIR"] = old_state_dir
    out: dict[str, Any] = {
        "transport": "acp",
        "agent": "opencode",
        "ok": False,
        "returncode": result["returncode"],
    }
    if not result["stdout"].strip():
        out["error"] = result["stderr"][:500] or "empty stdout"
        return out
    try:
        payload = json.loads(result["stdout"].splitlines()[-1])
    except json.JSONDecodeError:
        out["error"] = "acp json parse failed"
        out["stderr"] = result["stderr"][:500]
        return out
    out.update(
        {
            "state": payload.get("state"),
            "stop_reason": payload.get("stop_reason"),
            "text_excerpt": payload.get("text_excerpt"),
            "dispatch_id": payload.get("dispatch_id"),
        }
    )
    out["ok"] = (
        result["ok"]
        and payload.get("state") == "complete"
        and payload.get("stop_reason") == "end_turn"
        and _reply_has_four(str(payload.get("text_excerpt") or ""))
    )
    if not out["ok"] and "error" not in out:
        out["error"] = f"unexpected acp outcome state={payload.get('state')!r}"
    return out


def _run_bash_tail(*, workdir: Path, model: str, timeout: float) -> dict[str, Any]:
    worker = HOST_DIR / "bash_tail.py"
    watcher = SCRIPT_DIR / "watch-dispatch-tail.sh"
    tail_path = Path(tempfile.mktemp(prefix="opencode-self-dispatch-tail-", suffix=".txt"))
    prompt_path = Path(tempfile.mktemp(prefix="opencode-self-dispatch-prompt-", suffix=".md"))
    prompt_path.write_text(READONLY_PROMPT + "\n", encoding="utf-8")
    pidfile_dir = Path(tempfile.mkdtemp(prefix="goal-flight-opencode-self-dispatch-pids-"))
    env = os.environ.copy()
    env["GOALFLIGHT_PIDFILE_DIR"] = str(pidfile_dir)

    worker_proc = subprocess.Popen(
        [
            sys.executable,
            str(worker),
            "--directory",
            str(workdir),
            "--tail",
            str(tail_path),
            "--prompt-file",
            str(prompt_path),
            "--model",
            model,
            "--timeout",
            str(max(30, int(timeout - 30))),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    watcher_proc = subprocess.Popen(
        [
            "bash",
            str(watcher),
            "--pid",
            str(worker_proc.pid),
            "--tail",
            str(tail_path),
            "--controller-pid",
            str(os.getpid()),
            "--agent",
            "opencode-bash-tail",
            "--session-id",
            "opencode-self-dispatch",
            "--poll-secs",
            "1",
            "--max-idle-secs",
            str(max(30, int(timeout - 15))),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    worker_rc = worker_proc.wait(timeout=timeout)
    watcher_rc = watcher_proc.wait(timeout=timeout)
    tail_text = tail_path.read_text(encoding="utf-8") if tail_path.exists() else ""
    out: dict[str, Any] = {
        "transport": "bash_tail",
        "agent": "opencode-bash-tail",
        "ok": False,
        "worker_returncode": worker_rc,
        "watcher_returncode": watcher_rc,
        "tail_path": str(tail_path),
    }
    out["complete_marker"] = "COMPLETE: true" in tail_text
    out["reply_ok"] = _reply_has_four(tail_text)
    out["ok"] = worker_rc == 0 and watcher_rc == 0 and out["complete_marker"] and out["reply_ok"]
    if not out["ok"]:
        out["error"] = (
            f"worker_rc={worker_rc} watcher_rc={watcher_rc} "
            f"complete={out['complete_marker']} reply={out['reply_ok']}"
        )
    try:
        tail_path.unlink(missing_ok=True)
        prompt_path.unlink(missing_ok=True)
        shutil.rmtree(pidfile_dir, ignore_errors=True)
    except OSError:
        pass
    return out


def run_self_dispatch_test(
    *,
    project_root: Path,
    model: str,
    skip_acp: bool,
    skip_bash_tail: bool,
    timeout: float,
) -> dict[str, Any]:
    started = time.time()
    missing: list[str] = []
    if not shutil.which("opencode"):
        missing.append("opencode binary")
    if not (SCRIPT_DIR / "goalflight_acp_run.py").is_file():
        missing.append("goalflight_acp_run.py")
    if not skip_bash_tail and not (HOST_DIR / "bash_tail.py").is_file():
        missing.append("bash_tail.py")
    if missing:
        return {
            "schema": SCHEMA,
            "ok": False,
            "skipped": True,
            "reason": f"missing: {', '.join(missing)}",
        }

    _load_litellm_env()
    if model.startswith("litellm/") and not (
        os.environ.get("LITELLM_API_KEY") or os.environ.get("LITELLM_MASTER_KEY")
    ):
        return {
            "schema": SCHEMA,
            "ok": False,
            "skipped": True,
            "reason": "LiteLLM credentials missing for litellm/* model",
        }

    doctor = _doctor_snapshot(project_root)
    capacity = _capacity_snapshot()
    workdir = _prepare_workdir(project_root)

    transports: dict[str, Any] = {}
    if not skip_acp:
        transports["acp"] = _run_acp(workdir=workdir, model=model, timeout=timeout)
    if not skip_bash_tail:
        transports["bash_tail"] = _run_bash_tail(workdir=workdir, model=model, timeout=timeout)

    try:
        shutil.rmtree(workdir, ignore_errors=True)
    except OSError:
        pass

    required_ok = [t.get("ok") for t in transports.values()]
    overall_ok = bool(required_ok) and all(required_ok) and doctor.get("ok") and capacity.get("ok")
    return {
        "schema": SCHEMA,
        "ok": overall_ok,
        "controller": "opencode",
        "project_root": str(project_root.resolve()),
        "model": model,
        "doctor": doctor,
        "capacity": capacity,
        "transports": transports,
        "elapsed_s": round(time.time() - started, 2),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="OpenCode controller self-dispatch test")
    parser.add_argument("--directory", "-C", default=str(REPO_ROOT), help="Project root for doctor/capacity")
    parser.add_argument("--model", "-m", default=DEFAULT_MODEL, help=f"LiteLLM/OpenCode model (default: {DEFAULT_MODEL})")
    parser.add_argument("--timeout", type=float, default=180.0, help="Per-transport timeout seconds")
    parser.add_argument("--skip-acp", action="store_true", help="Skip ACP worker dispatch")
    parser.add_argument("--skip-bash-tail", action="store_true", help="Skip bash-tail worker dispatch")
    parser.add_argument("--json", action="store_true", help="Emit JSON summary (default when non-tty)")
    args = parser.parse_args()

    payload = run_self_dispatch_test(
        project_root=Path(args.directory).resolve(),
        model=args.model,
        skip_acp=args.skip_acp,
        skip_bash_tail=args.skip_bash_tail,
        timeout=args.timeout,
    )

    emit_json = args.json or not sys.stdout.isatty()
    if emit_json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"opencode self-dispatch: {'OK' if payload.get('ok') else 'FAIL'}")
        for name, result in (payload.get("transports") or {}).items():
            mark = "OK" if result.get("ok") else "FAIL"
            print(f"  {name}: {mark} — {result.get('error') or result.get('state') or result.get('text_excerpt')}")

    if payload.get("skipped"):
        return 0
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
