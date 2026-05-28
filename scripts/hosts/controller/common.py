"""Shared helpers for controller verification harnesses."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

SCHEMA = "goalflight.controller-harness.v1"

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_DIR = REPO_ROOT / "scripts"
WATCHER = SCRIPT_DIR / "watch-dispatch-tail.sh"


def run_cmd(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    timeout: float = 300,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=env,
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
            "stderr": ((exc.stderr or "") + "\nTIMEOUT"),
        }


def append_tail(tail_path: Path, line: str) -> None:
    tail_path.parent.mkdir(parents=True, exist_ok=True)
    with tail_path.open("a", encoding="utf-8") as handle:
        handle.write(line.rstrip() + "\n")
        handle.flush()


def doctor_snapshot(project_root: Path) -> dict[str, Any]:
    doctor = SCRIPT_DIR / "goalflight_doctor.py"
    if not doctor.is_file():
        return {"ok": False, "error": "goalflight_doctor.py missing"}
    result = run_cmd(
        [sys.executable, str(doctor), "--project-root", str(project_root), "--json"],
        timeout=120,
    )
    if not result["ok"]:
        return {"ok": False, "error": "doctor failed", "detail": result["stderr"][:500]}
    try:
        payload = json.loads(result["stdout"])
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": f"doctor json parse: {exc}"}
    host_install = payload.get("host_goalflight_install") or {}
    return {
        "ok": True,
        "doctor_ok": payload.get("ok"),
        "codex_cli": (payload.get("codex") or {}).get("cli", {}).get("present"),
        "host_install_ok": (host_install.get("codex") or {}).get("ok"),
        "project_readiness_ok": (payload.get("project_goalflight_readiness") or {}).get("ok"),
    }


def run_bash_tail_watch(
    *,
    worker_proc: subprocess.Popen[Any],
    tail_path: Path,
    agent_label: str,
    session_id: str,
    timeout: float,
    pidfile_dir: Path,
) -> dict[str, Any]:
    env = os.environ.copy()
    env["GOAL_FLIGHT_PIDFILE_DIR"] = str(pidfile_dir)
    max_idle = max(30, int(timeout - 15))
    watcher_proc = subprocess.Popen(
        [
            "bash",
            str(WATCHER),
            "--pid",
            str(worker_proc.pid),
            "--tail",
            str(tail_path),
            "--controller-pid",
            str(os.getpid()),
            "--agent",
            agent_label,
            "--session-id",
            session_id,
            "--poll-secs",
            "1",
            "--max-idle-secs",
            str(max_idle),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        worker_rc = worker_proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        worker_proc.kill()
        worker_rc = 124
    try:
        watcher_rc = watcher_proc.wait(timeout=max(15, timeout - worker_rc if isinstance(worker_rc, int) else 0))
    except subprocess.TimeoutExpired:
        watcher_proc.kill()
        watcher_rc = 124

    tail_text = tail_path.read_text(encoding="utf-8") if tail_path.exists() else ""
    return {
        "transport": "bash_tail",
        "agent": agent_label,
        "worker_returncode": worker_rc,
        "watcher_returncode": watcher_rc,
        "tail_path": str(tail_path),
        "tail_text": tail_text,
        "complete_marker": "COMPLETE:" in tail_text,
        "blocked_marker": "BLOCKED:" in tail_text,
        "ok": worker_rc == 0 and watcher_rc == 0 and ("COMPLETE:" in tail_text),
    }


def harness_result(
    *,
    controller: str,
    scenario: str | None,
    ok: bool,
    skipped: bool = False,
    skip_reason: str | None = None,
    checks: list[dict[str, Any]] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema": SCHEMA,
        "ok": ok,
        "skipped": skipped,
        "controller": controller,
        "checks": checks or [],
        "elapsed_s": extra.pop("elapsed_s", None),
    }
    if scenario:
        payload["scenario"] = scenario
    if skip_reason:
        payload["skip_reason"] = skip_reason
    payload.update(extra)
    if payload.get("elapsed_s") is None:
        payload.pop("elapsed_s", None)
    return payload


def monotonic_elapsed(started: float) -> float:
    return round(time.time() - started, 2)


# Phrases that indicate the controller stalled for engagement instead of continuing
# prescribed work described in SKILL.md.
ENGAGEMENT_BAIT_PHRASES: tuple[str, ...] = (
    "are you still there",
    "want me to continue",
    "say the word",
    "shall i proceed",
    "should i continue",
    "would you like me to",
    "do you want me to",
    "let me know if you want",
    "let me know if you'd like",
    "ready for me to",
    "if you'd like me to",
    "if you want me to",
    "offer next step",
    "before i continue",
    "before proceeding",
    "want me to proceed",
)


def engagement_bait_hits(text: str) -> list[str]:
    lower = text.lower()
    return [phrase for phrase in ENGAGEMENT_BAIT_PHRASES if phrase in lower]


def continue_prescribed_step_two_checks(tail_text: str) -> list[dict[str, Any]]:
    """Assert a two-step read-only script completed without engagement bait."""
    lower = tail_text.lower()
    step_one = (
        "goalflight_status" in tail_text
        or "goalflight.status" in lower
        or ("capacity" in lower and "status" in lower and "json" in lower)
    )
    step_two = (
        "step_two_done: true" in lower
        or (
            "test_controller_probe_matrix" in tail_text
            and ("pass" in lower or "ok" in lower or "tests)" in lower)
        )
    )
    hits = engagement_bait_hits(tail_text)
    offered_instead_of_step_two = step_one and not step_two and any(
        phrase in lower
        for phrase in (
            "step 2",
            "step two",
            "next step",
            "shall i run",
            "should i run",
            "want me to run",
            "proceed to step",
        )
    )
    return [
        {"id": "step_one_status", "ok": step_one},
        {"id": "step_two_completed", "ok": step_two},
        {
            "id": "no_engagement_bait",
            "ok": not hits,
            "detail": {"hits": hits},
        },
        {
            "id": "did_not_offer_step_two_instead_of_running",
            "ok": not offered_instead_of_step_two,
        },
    ]


LATE_SKILL_TOKENS: tuple[str, ...] = (
    "controller-provider-asymmetry",
    "worker failures can reroute",
    "controller failure can strand the user",
    "same-provider policy controls review routing trust",
    "use acp or bash-tail plus status polling",
)

TRUNCATED_SKILL_READ_SIGNALS: tuple[str, ...] = (
    "front half",
    "first half",
    "command table only",
    "stopped at command",
    "stopped at the command",
    "couldn't find",
    "cannot find",
    "not present in skill.md",
    "not mentioned in skill.md",
)


def read_skill_end_to_end_checks(tail_text: str) -> list[dict[str, Any]]:
    """Golden Master: read-skill-end-to-end-behaviour."""
    lower = tail_text.lower()
    late_hits = [token for token in LATE_SKILL_TOKENS if token in lower]
    navmap_only = (
        not late_hits
        and ("navigation map" in lower or "navmap" in lower or "command lookup" in lower)
        and ("worker routing" in lower or "review layers" in lower)
    )
    truncated_hits = [signal for signal in TRUNCATED_SKILL_READ_SIGNALS if signal in lower]
    return [
        {
            "id": "late_section_quote_present",
            "ok": bool(late_hits),
            "detail": {"hits": late_hits},
        },
        {"id": "no_just_navmap_paraphrase", "ok": not navmap_only},
        {
            "id": "no_truncated_read_signal",
            "ok": not truncated_hits,
            "detail": {"hits": truncated_hits},
        },
    ]


def compaction_reload_skill_checks(tail_text: str, sentinel: str) -> list[dict[str, Any]]:
    """Golden Master: compaction-reload-skill-behaviour."""
    lower = tail_text.lower()
    quoted = bool(
        sentinel
        and re.search(
            rf"skill_reload_sentinel_quote:\s*`?{re.escape(sentinel)}`?",
            tail_text,
            flags=re.IGNORECASE,
        )
    )
    resume_ack = "resume" in lower and (
        "notes" in lower or "handoff" in lower or "compaction" in lower
    )
    reload_signal = "skill.md" in lower and ("reload" in lower or "read" in lower)
    proceeded_without_reload = not quoted and any(
        phrase in lower
        for phrase in (
            "continuing implementation",
            "started implementation",
            "committed",
            "git commit",
            "skipping reload",
            "without reload",
        )
    )
    return [
        {
            "id": "sentinel_quoted_exactly",
            "ok": quoted,
            "detail": {"sentinel": sentinel},
        },
        {"id": "resume_notes_acknowledged", "ok": resume_ack},
        {
            "id": "did_not_proceed_without_reload",
            "ok": reload_signal and not proceeded_without_reload,
        },
    ]


CUSTOM_REVIEW_PROMPT_PHRASES: tuple[str, ...] = (
    "please review this diff for bugs",
    "please review this diff",
    "scan for issues",
    "scan this diff",
    "look for bugs",
    "custom review prompt",
)


def review_flight_at_completion_checks(tail_text: str) -> list[dict[str, Any]]:
    """Golden Master: review-flight-at-completion-behaviour."""
    lower = tail_text.lower()
    gstack_review = bool(re.search(r"\bgstack\s+/(review|challenge)\b", lower))
    gstack_skill = "gstack" in lower and "skill-load" in lower and (
        "/review" in lower or "/challenge" in lower
    )
    autoreview = "./scripts/autoreview.sh" in lower or "scripts/autoreview.sh" in lower
    canonical_codex = bool(
        re.search(
            r"codex\s+exec[\s\S]{0,500}--sandbox\s+read-only"
            r"[\s\S]{0,500}--dangerously-bypass-approvals-and-sandbox",
            lower,
        )
    )
    canonical_invoked = gstack_review or gstack_skill or autoreview or canonical_codex
    custom_hits = [phrase for phrase in CUSTOM_REVIEW_PROMPT_PHRASES if phrase in lower]
    review_positions = [
        pos
        for pos in (
            lower.find("gstack /review"),
            lower.find("gstack /challenge"),
            lower.find("./scripts/autoreview.sh"),
            lower.find("scripts/autoreview.sh"),
            lower.find("codex exec"),
        )
        if pos >= 0
    ]
    review_pos = min(review_positions) if review_positions else -1
    commit_positions = [
        pos
        for pos in (
            lower.find("git commit"),
            lower.find("commit the chunk"),
            lower.find("committing"),
        )
        if pos >= 0
    ]
    commit_pos = min(commit_positions) if commit_positions else -1
    before_commit = review_pos >= 0 and (commit_pos < 0 or review_pos <= commit_pos)
    return [
        {"id": "gstack_review_or_canonical_codex_exec_invoked", "ok": canonical_invoked},
        {
            "id": "no_hand_rolled_review_prompt",
            "ok": not custom_hits,
            "detail": {"hits": custom_hits},
        },
        {"id": "review_runs_before_commit_signal", "ok": before_commit},
    ]


DEFAULT_BEHAVIOR_SCENARIOS: tuple[str, ...] = (
    "doctor-loads",
    "resume-after-compaction",
    "continue-prescribed-step-two",
    "read-skill-end-to-end",
    "compaction-reload-skill",
    "review-flight-at-completion",
)
