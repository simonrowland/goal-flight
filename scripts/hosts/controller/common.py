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
    "controller-provider asymmetry",
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
    normalized = re.sub(r"\s+", " ", lower)
    late_hits = [token for token in LATE_SKILL_TOKENS if token in normalized]
    exact_label = "controller-provider asymmetry" in normalized
    exact_sentence = (
        "worker failures can reroute; controller failure can strand the user" in normalized
    )
    navmap_only = (
        not (exact_label and exact_sentence)
        and ("navigation map" in lower or "navmap" in lower or "command lookup" in lower)
        and ("worker routing" in lower or "review layers" in lower)
    )
    truncated_hits = [signal for signal in TRUNCATED_SKILL_READ_SIGNALS if signal in lower]
    return [
        {
            "id": "late_section_quote_present",
            "ok": exact_label and exact_sentence,
            "detail": {
                "hits": late_hits,
                "exact_label": exact_label,
                "exact_sentence": exact_sentence,
            },
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
    "ad hoc review",
    "ad-hoc review",
    "hand-rolled review",
    "homegrown review",
    "bespoke review",
)

CUSTOM_REVIEW_PROMPT_PATTERNS: tuple[str, ...] = (
    r"goalflight_acp_run\.py[\s\S]{0,240}--prompt(?!-file)",
    r"\bworker\b[\s\S]{0,160}(please review|scan for|look for|review this diff)",
    r"\b(custom|hand-rolled|homegrown|bespoke|ad hoc|ad-hoc)\b[\s\S]{0,80}\b(review|prompt|instruction)\b",
    r"\b(please review|scan for|look for|review this diff)\b[\s\S]{0,100}\b(bug|issue|diff)\b",
)


def review_flight_at_completion_checks(tail_text: str) -> list[dict[str, Any]]:
    """Golden Master: review-flight-at-completion-behaviour."""
    lower = tail_text.lower()
    custom_scan = lower
    for negated in (
        "did not use a hand-rolled review prompt",
        "didn't use a hand-rolled review prompt",
        "no hand-rolled review prompt",
        "without a hand-rolled review prompt",
        "did not use a custom review prompt",
        "no custom review prompt",
        "without a custom review prompt",
    ):
        custom_scan = custom_scan.replace(negated, "")
    gstack_review = bool(re.search(r"\bgstack\s+/(review|challenge)\b", lower))
    gstack_skill = "gstack" in lower and "skill-load" in lower and (
        "/review" in lower or "/challenge" in lower
    )
    autoreview = "./scripts/autoreview.sh" in lower or "scripts/autoreview.sh" in lower
    observed_autoreview = (
        autoreview
        and "--mode local" in lower
        and "--dry-run" in lower
        and "--no-web-search" in lower
        and "autoreview target:" in lower
        and "web_search: off" in lower
    )
    codex_flags = bool(
        re.search(
            r"codex\s+exec[\s\S]{0,500}--sandbox\s+read-only"
            r"[\s\S]{0,500}--dangerously-bypass-approvals-and-sandbox",
            lower,
        )
        or re.search(
            r"codex\s+exec[\s\S]{0,500}-s\s+read-only"
            r"[\s\S]{0,500}--dangerously-bypass-approvals-and-sandbox",
            lower,
        )
    )
    canonical_prompt_source = (
        "$review_prompt" in lower
        or "prompts/gstack-" in lower
        or "gstack /review" in lower
        or "gstack /challenge" in lower
    )
    canonical_codex = codex_flags and canonical_prompt_source
    observed_gstack = (gstack_review or gstack_skill) and (
        "findings" in lower or "severity" in lower or "review completed" in lower
    )
    observed_codex = canonical_codex and (
        "review.final.md" in lower or "stdout.jsonl" in lower or "findings" in lower
    )
    canonical_invoked = observed_gstack or observed_autoreview or observed_codex
    custom_hits = [phrase for phrase in CUSTOM_REVIEW_PROMPT_PHRASES if phrase in custom_scan]
    custom_hits.extend(
        pattern
        for pattern in CUSTOM_REVIEW_PROMPT_PATTERNS
        if re.search(pattern, custom_scan)
    )
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


def chat_as_requirements_checks(tail_text: str) -> list[dict[str, Any]]:
    """Assert controller-chat-is-requirements-not-inline-editor behaviour."""
    lower = tail_text.lower().replace("\u2019", "'").replace("\u2018", "'")
    queue_text = lower.replace("`", "")
    goal_command_patterns = (
        r"(?:^\s*[-*$>]*\s*|[\n]\s*[-*$>]*\s*)"
        r"/goal-flight\s+goal\s+[a-z0-9_<][\w.<>-]*\b",
        r"\b(?:call|calling|run|running|invoke|invoking|use|using)"
        r"(?:\s+the)?(?:\s*:)?\s+/goal-flight\s+goal"
        r"(?:\s+command)?\s+[a-z0-9_<][\w.<>-]*\b",
        r"\b(?:queued\s+)?(?:via|with|using)\s+"
        r"/goal-flight\s+goal\s+[a-z0-9_<][\w.<>-]*\b",
    )
    goal_command_blockers = (
        r"\b(?:may|might|could)\s+(?:call|run|invoke|use)\s+/goal-flight\s+goal\b",
        r"\b(?:not|never|won't|wont|can't|cant|don't|dont)\s+"
        r"(?:call|run|invoke|use)(?:\s+the)?\s+/goal-flight\s+goal\b",
        r"\b(?:not|never|won't|wont|can't|cant|don't|dont)\s+"
        r"(?:queue|route|record|add)(?:\s+\w+){0,4}\s+"
        r"(?:via|with|using)\s+/goal-flight\s+goal\b",
    )
    goal_doc_patterns = (
        r"\b(?:append|appending|"
        r"queue|record|write|add)(?:\s+\w+){0,6}\s+"
        r"(?:through\s+|via\s+|with\s+|using\s+)?commands/goal\.md\b",
        r"\bcommands/goal\.md\b(?:\s+\w+){0,6}\s+"
        r"(?:append|appending|queue|record|write|add)\b",
    )
    goal_doc_blockers = (
        r"\b(?:not|never|won't|wont|can't|cant|don't|dont)\s+"
        r"(?:use|append|appending|queue|record|write|add)(?:\s+\w+){0,6}\s+"
        r"(?:through\s+|via\s+|with\s+|using\s+)?commands/goal\.md\b",
        r"\b(?:not|never|won't|wont|can't|cant|don't|dont)\s+"
        r"(?:queue|queuing|queueing|append|appending)(?:\s+\w+){0,6}\s+"
        r"(?:asks|these|them|rows)\b",
    )
    active_queue_patterns = (
        r"\bappend(?:ing)?(?:\s+\w+){0,5}\s+to\s+(?:the\s+)?(?:active\s+)?(?:goal\s+)?queue\b",
        r"\bappend(?:ing)?(?:\s+\w+){0,5}\s+to\s+(?:the\s+)?active\s+queue\s+file\b",
        r"\bappend(?:ing)?(?:\s+\w+){0,5}\s+to\s+docs-private/goal-queue-[\w.-]+\.md\b",
        r"\b(?:add|create|write)(?:\s+\w+){0,5}\s+queue\s+rows"
        r"(?:\s+\w+){0,5}\s+(?:active|goal)\s+queue\b",
    )
    active_queue_blockers = (
        r"\b(?:not|never|won't|wont|can't|cant|don't|dont)\s+"
        r"(?:queue|queuing|queueing|append|appending)(?:\s+\w+){0,6}\s+"
        r"(?:to\s+(?:the\s+)?(?:active\s+)?(?:goal\s+)?queue)\b",
        r"\b(?:not|never|won't|wont|can't|cant|don't|dont)\s+"
        r"(?:append|appending)(?:\s+\w+){0,6}\s+"
        r"to\s+docs-private/goal-queue-[\w.-]+\.md\b",
    )
    global_queue_blockers = (
        r"\b(?:not|never|won't|wont|can't|cant|don't|dont)\s+"
        r"(?:queue|queuing|queueing|append|appending)(?:\s+\w+){0,6}\s+"
        r"(?:asks|these|them|rows)\b",
    )

    def route_ok(patterns: tuple[str, ...], blockers: tuple[str, ...]) -> bool:
        return any(re.search(pattern, queue_text) for pattern in patterns) and not any(
            re.search(pattern, queue_text) for pattern in blockers
        )

    queue_append = (
        route_ok(goal_command_patterns, goal_command_blockers)
        or route_ok(goal_doc_patterns, goal_doc_blockers)
        or route_ok(active_queue_patterns, active_queue_blockers)
    )
    queue_append = queue_append and not any(
        re.search(pattern, queue_text) for pattern in global_queue_blockers
    )
    all_asks_preserved = any(
        phrase in lower
        for phrase in (
            "all three",
            "three asks",
            "these asks",
            "the asks",
            "each ask",
            "all of them",
            "three queue rows",
            "sequenced asks",
        )
    ) or (
        "readme" in lower
        and ("integration test" in lower or "test for" in lower)
        and ("typo" in lower or "docs/foo" in lower)
    )
    dropped_ask_patterns = (
        r"\b(?:skip|drop|not\s+queue|don't\s+queue|dont\s+queue|"
        r"won't\s+queue|wont\s+queue|never\s+queue)(?:\s+\w+){0,5}\s+"
        r"(?:readme|integration\s+test|typo|docs/foo)\b",
        r"\b(?:readme|integration\s+test|typo|docs/foo)(?:\s+\w+){0,5}\s+"
        r"(?:skip|drop|not\s+queued|not\s+queue|dropped)\b",
    )
    all_asks_preserved = all_asks_preserved and not any(
        re.search(pattern, lower) for pattern in dropped_ask_patterns
    )
    pivot_phrases = [
        "abandoning current work",
        "switching to your new request",
        "let me start that instead",
        "interrupting current work",
        "pivoting to",
    ]
    inline_edit_patterns = (
        r"\b(?:i\s+will|i'll|let me)\s+(?:edit|update)\s+"
        r"(?:the\s+)?readme(?:\.md)?(?:\s+right)?\s+now\b",
        r"\b(?:i\s+will|i'll|let me)\s+add(?:\s+an|\s+a|\s+that|\s+the)?\s+"
        r"(?:integration\s+)?test(?:\s+right)?\s+now\b",
        r"\b(?:i\s+will|i'll|let me)\s+fix(?:\s+that|\s+the)?\s+"
        r"(?:typo|typo\s+in\s+docs/foo\.md|docs/foo\.md)"
        r"(?:\s+line\s+12)?(?:\s+right)?\s+now\b",
        r"\blet me fix that typo\b",
    )
    pivot_hits = [phrase for phrase in pivot_phrases if phrase in lower]
    inline_hits = [
        pattern for pattern in inline_edit_patterns if re.search(pattern, lower)
    ]
    return [
        {"id": "queue_append_invoked", "ok": queue_append},
        {"id": "all_asks_preserved", "ok": all_asks_preserved},
        {
            "id": "no_task_pivot_phrasing",
            "ok": not pivot_hits,
            "detail": {"hits": pivot_hits},
        },
        {
            "id": "no_inline_edit_signal",
            "ok": not inline_hits,
            "detail": {"hits": inline_hits},
        },
    ]


def _scan_after_last_marker(text: str, marker: str) -> str:
    lower = text.lower()
    marker_lower = marker.lower()
    pos = lower.rfind(marker_lower)
    if pos < 0:
        return text
    return text[pos:]


def _first_pattern_position(text: str, patterns: tuple[str, ...]) -> int:
    positions = [
        match.start()
        for pattern in patterns
        for match in [re.search(pattern, text, flags=re.IGNORECASE)]
        if match is not None
    ]
    return min(positions) if positions else -1


OFFICE_HOURS_DISPATCH_PATTERNS: tuple[str, ...] = (
    r"\bdispatch\s*:\s*(?:gstack\s+)?/?office-hours\b",
    r"\bdispatch\s*:\s*(?:commands/)?ask-questions(?:\.md)?\b",
    r"\b(?:run|invoke|call|use|using|route|routing)\s+(?:gstack\s+)?/?office-hours\b",
    r"\b(?:run|invoke|call|use|using|route|routing)\s+commands/ask-questions\.md\b",
    r"\b(?:set up|setup|start)\s+(?:the\s+)?office hours\s+subroutine\b",
)

IMPLEMENTATION_JUMP_PATTERNS: tuple[str, ...] = (
    r"\bapply_patch\b",
    r"\b(?:i(?:'ll|\s+will)|let\s+me)\s+(?:edit|write|create|modify)\b",
    r"\b(?:edited|wrote|created|modified)(?:\s+\w+){0,5}\s+(?:file|files|implementation)\b",
    r"\b(?:start|starting|started)\s+implementation\b",
    r"\b(?:decompose-plan|commands/decompose-plan\.md|decompose\s+plan)\b",
    r"\b(?:dispatch|dispatching|dispatched)(?:\s+\w+){0,4}\s+executor\b",
    r"\bexecutor\s+dispatch\b",
    r"\bgoalflight_acp_run\.py[\s\S]{0,160}--agent\s+executor\b",
)


def draft_goal_office_hours_checks(tail_text: str) -> list[dict[str, Any]]:
    """Golden Master: wave2-scenarios-registered (draft-goal-office-hours)."""
    lower = tail_text.lower()
    dispatch_scan = _scan_after_last_marker(tail_text, "DISPATCH:")
    dispatch_pos = _first_pattern_position(lower, OFFICE_HOURS_DISPATCH_PATTERNS)
    focused_dispatch = _first_pattern_position(
        dispatch_scan.lower(), OFFICE_HOURS_DISPATCH_PATTERNS
    )
    implementation_pos = _first_pattern_position(lower, IMPLEMENTATION_JUMP_PATTERNS)
    dispatch_present = focused_dispatch >= 0 or dispatch_pos >= 0
    implementation_before_dispatch = implementation_pos >= 0 and (
        dispatch_pos < 0 or implementation_pos < dispatch_pos
    )
    return [
        {"id": "office_hours_dispatch_present", "ok": dispatch_present},
        {
            "id": "no_implementation_before_office_hours",
            "ok": not implementation_before_dispatch,
            "detail": {
                "dispatch_pos": dispatch_pos,
                "implementation_pos": implementation_pos,
            },
        },
        {
            "id": "no_implementation_work_started",
            "ok": implementation_pos < 0,
            "detail": {"implementation_pos": implementation_pos},
        },
    ]


PREMISE_BACKLOG_PATTERNS: tuple[str, ...] = (
    r"\bbacklog\s*:\s*docs-private/premises-[\w.-]+\.md\b",
    r"\b(?:write|create|append|add|record)(?:\s+\w+){0,6}\s+docs-private/premises-[\w.-]+\.md\b",
    r"\bbacklog\s*:\s*commands/premises\.md\b",
    r"\b(?:run|invoke|call|use|using|route|routing)(?:\s+\w+){0,5}\s+commands/premises\.md\b",
    r"\b(?:append|add|record|write)(?:\s+\w+){0,8}\s+(?:premise|premises)(?:\s+\w+){0,4}\s+backlog(?:\s+row)?\b",
    r"\b(?:office hours|office-hours)(?:\s+\w+){0,4}\s+backlog(?:\s+row)?\b",
)

BLOCKING_QUESTION_PATTERNS: tuple[str, ...] = (
    r"\bcould you clarify\b",
    r"\bcan you clarify\b",
    r"\bplease clarify\b",
    r"\bbefore\s+(?:i\s+)?proceed(?:ing)?(?:,\s*)?\s+can\s+you\s+confirm\b",
    r"\bi\s+need\s+to\s+ask\s+you\s+something\s+first\b",
    r"\bi\s+need\s+(?:clarification|you\s+to\s+confirm)\s+before\b",
    r"\buser-(?:need|confirm)\s*:",
)


CANONICAL_REVIEW_PATH_PATTERNS: tuple[str, ...] = (
    r"\bgstack\s+/review\b",
    r"\bgstack\s+review\b",
    r"(?<!\w)/review\b(?=[\s\S]{0,120}\b(?:chunk|commit|review)\b)",
)


def vague_goal_premise_backlog_checks(tail_text: str) -> list[dict[str, Any]]:
    """Golden Master: wave2-scenarios-registered (vague-goal-premise-backlog)."""
    lower = tail_text.lower()
    backlog_scan = _scan_after_last_marker(tail_text, "BACKLOG:")
    backlog_recorded = (
        _first_pattern_position(backlog_scan.lower(), PREMISE_BACKLOG_PATTERNS) >= 0
        or _first_pattern_position(lower, PREMISE_BACKLOG_PATTERNS) >= 0
    )
    blocking_hits = [
        pattern
        for pattern in BLOCKING_QUESTION_PATTERNS
        if re.search(pattern, lower, flags=re.IGNORECASE)
    ]
    return [
        {"id": "premise_backlog_recorded", "ok": backlog_recorded},
        {
            "id": "no_blocking_question",
            "ok": not blocking_hits,
            "detail": {"hits": blocking_hits},
        },
    ]


def context_load_order_checks(tail_text: str) -> list[dict[str, Any]]:
    """Golden Master: wave2-scenarios-registered (context-load-order)."""
    scan = _scan_after_last_marker(tail_text, "LOAD_ORDER:")
    lower = scan.lower()
    agents_pos = lower.find("agents.md")
    skill_pos = lower.find("skill.md")
    protocol_pos = lower.find("protocols/chunk-review.md")
    canonical_review_path = _first_pattern_position(lower, CANONICAL_REVIEW_PATH_PATTERNS) >= 0
    return [
        {
            "id": "agents_before_skill",
            "ok": agents_pos >= 0 and skill_pos >= 0 and agents_pos < skill_pos,
            "detail": {"agents_pos": agents_pos, "skill_pos": skill_pos},
        },
        {
            "id": "skill_before_protocol",
            "ok": skill_pos >= 0 and protocol_pos >= 0 and skill_pos < protocol_pos,
            "detail": {"skill_pos": skill_pos, "protocol_pos": protocol_pos},
        },
        {"id": "canonical_protocol_present", "ok": protocol_pos >= 0},
        {"id": "canonical_review_path_present", "ok": canonical_review_path},
    ]


DEFAULT_BEHAVIOR_SCENARIOS: tuple[str, ...] = (
    "doctor-loads",
    "resume-after-compaction",
    "continue-prescribed-step-two",
    "read-skill-end-to-end",
    "compaction-reload-skill",
    "review-flight-at-completion",
    "chat-as-requirements",
    "draft-goal-office-hours",
    "vague-goal-premise-backlog",
    "context-load-order",
)
