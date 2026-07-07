#!/usr/bin/env python3
"""Orchestrator behavior scenarios (scripted prompts + deterministic checks).

Wave 1 implements Codex bash-tail ``doctor-loads``. Additional scenarios and
hosts follow the same contract.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable

HOST_DIR = Path(__file__).resolve().parent
REPO_ROOT = HOST_DIR.parents[2]
FIXTURES = REPO_ROOT / "tests/fixtures/controller_scenarios"
sys.path.insert(0, str(HOST_DIR))

from common import (  # noqa: E402
    SCHEMA,
    chat_as_requirements_checks,
    compaction_reload_in_skill_continuation_checks,
    compaction_reload_skill_checks,
    context_load_order_checks,
    continue_prescribed_step_two_checks,
    dispatch_cli_worker_crash_safe_checks,
    draft_goal_office_hours_checks,
    goal_loop_default_checks,
    doctor_snapshot,
    harness_result,
    monotonic_elapsed,
    never_pgrep_worker_liveness_checks,
    no_hand_iterate_checks,
    prompt_echo_free_tail,
    read_skill_end_to_end_checks,
    review_flight_at_completion_checks,
    vague_goal_premise_backlog_checks,
)
import probe_matrix  # noqa: E402


def _load_prompt(scenario_id: str, project_root: Path, *, sentinel: str | None = None) -> str:
    fixture = FIXTURES / scenario_id / "prompt.md"
    if not fixture.is_file():
        raise FileNotFoundError(f"missing fixture prompt: {fixture}")
    text = fixture.read_text(encoding="utf-8")
    root = str(project_root.resolve())
    text = text.replace("{{PROJECT_ROOT}}", root)
    if "{{SENTINEL}}" in text:
        text = text.replace("{{SENTINEL}}", sentinel or "")
        text = "\n".join(
            line for line in text.splitlines() if "HARNESS_SENTINEL_PLACEHOLDER" not in line
        )
    if "{{RESUME_NOTES_PATH}}" in text:
        from compaction_resume_drill import resolve_resume_notes  # noqa: WPS433

        notes = resolve_resume_notes(project_root, None)
        if notes is None:
            notes = FIXTURES.parent / "compaction_handoff" / "RESUME-NOTES.md"
        text = text.replace("{{RESUME_NOTES_PATH}}", str(notes.resolve()))
    return text


def _copy_if_present(project_root: Path, temp_root: Path, relative: str) -> None:
    src = project_root / relative
    if not src.is_file():
        return
    dst = temp_root / relative
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)


def _prepare_compaction_reload_project(project_root: Path) -> tuple[Path, str]:
    temp_root = Path(tempfile.mkdtemp(prefix="goalflight-compaction-reload-"))
    sentinel = f"GF-SKILL-RELOAD-SENTINEL-{uuid.uuid4()}"

    for relative in (
        "AGENTS.md",
        "commands/resume.md",
        "protocols/state-handoff.md",
        "docs-private/goal-queue-controller-harness-2026-05-24.md",
    ):
        _copy_if_present(project_root, temp_root, relative)

    skill_text = (project_root / "SKILL.md").read_text(encoding="utf-8")
    sentinel_line = f"\nController behavior reload sentinel: {sentinel}\n"
    marker = "### Controller-provider asymmetry\n"
    if marker in skill_text:
        skill_text = skill_text.replace(marker, marker + sentinel_line, 1)
    else:
        skill_text = skill_text.rstrip() + "\n" + sentinel_line
    (temp_root / "SKILL.md").write_text(skill_text, encoding="utf-8")

    notes_src = project_root / "tests/fixtures/compaction_handoff/RESUME-NOTES.md"
    notes_dst = temp_root / "docs-private/RESUME-NOTES.md"
    notes_dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(notes_src, notes_dst)

    queue_dst = temp_root / "docs-private/goal-queue-controller-behavior.md"
    queue_dst.write_text(
        "# Active Goal Queue Fixture\n\n"
        "- chunk: compaction-reload-skill\n"
        "- status: active after compaction handoff\n",
        encoding="utf-8",
    )
    return temp_root, sentinel


def _assert_doctor_loads(tail_text: str, *, doctor: dict[str, Any], **_: Any) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    lower = tail_text.lower()
    invoked = (
        "goalflight_doctor.py" in tail_text
        or '"schema": "goalflight.doctor' in tail_text
        or "goalflight.doctor" in lower
        or ("doctor" in lower and "host_goalflight_install" in tail_text)
    )
    checks.append({"id": "doctor_invoked_or_cited", "ok": invoked})
    cites_ok = (
        '"ok":' in tail_text
        or '"ok": true' in lower
        or '"ok": false' in lower
        or (doctor.get("doctor_ok") is not None and "doctor" in lower)
    )
    checks.append({"id": "doctor_ok_field_cited", "ok": cites_ok})
    host_install = (
        "host_goalflight_install" in tail_text
        or "host goal-flight install" in lower
        or (doctor.get("host_install_ok") is not None and "codex" in lower)
    )
    checks.append({"id": "host_install_mentioned", "ok": host_install})
    return checks


def _assert_resume_after_compaction(tail_text: str, **_: Any) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    lower = tail_text.lower()
    checks.append(
        {
            "id": "resume_notes_acknowledged",
            "ok": "resume" in lower
            and ("notes" in lower or "handoff" in lower or "compaction" in lower or "tldr" in lower),
        }
    )
    status_ok = (
        "goalflight_status" in tail_text
        or "goalflight.capacity" in tail_text
        or ("capacity" in lower and "status" in lower)
    )
    checks.append({"id": "status_invoked_or_cited", "ok": status_ok})
    tests_ok = (
        "test_controller_probe_matrix" in tail_text
        or "test_compaction_resume_drill" in tail_text
        or ("pass" in lower and "test_controller_probe_matrix" in lower)
    ) and ("fail" not in lower or "0 failed" in lower or "failed)" not in lower)
    checks.append({"id": "fast_tests_run", "ok": tests_ok})
    return checks


def _assert_continue_prescribed_step_two(
    tail_text: str, *, prompt_text: str | None = None, **_: Any
) -> list[dict[str, Any]]:
    return continue_prescribed_step_two_checks(tail_text, prompt_text=prompt_text)


def _assert_read_skill_end_to_end(tail_text: str, **_: Any) -> list[dict[str, Any]]:
    return read_skill_end_to_end_checks(tail_text)


def _assert_compaction_reload_skill(tail_text: str, *, sentinel: str, **_: Any) -> list[dict[str, Any]]:
    return compaction_reload_skill_checks(tail_text, sentinel)


def _assert_compaction_reload_in_skill_continuation(
    tail_text: str, *, sentinel: str, **_: Any
) -> list[dict[str, Any]]:
    return compaction_reload_in_skill_continuation_checks(tail_text, sentinel)


def _assert_review_flight_at_completion(tail_text: str, **_: Any) -> list[dict[str, Any]]:
    return review_flight_at_completion_checks(tail_text)


def _assert_chat_as_requirements(tail_text: str, **_: Any) -> list[dict[str, Any]]:
    return chat_as_requirements_checks(tail_text)


def _assert_draft_goal_office_hours(tail_text: str, **_: Any) -> list[dict[str, Any]]:
    return draft_goal_office_hours_checks(tail_text)


def _assert_vague_goal_premise_backlog(tail_text: str, **_: Any) -> list[dict[str, Any]]:
    return vague_goal_premise_backlog_checks(tail_text)


def _assert_context_load_order(tail_text: str, **_: Any) -> list[dict[str, Any]]:
    return context_load_order_checks(tail_text)


def _assert_goal_loop_default(tail_text: str, **_: Any) -> list[dict[str, Any]]:
    return goal_loop_default_checks(tail_text)


def _assert_dispatch_cli_worker_crash_safe(tail_text: str, **_: Any) -> list[dict[str, Any]]:
    return dispatch_cli_worker_crash_safe_checks(tail_text)


def _assert_never_pgrep_worker_liveness(tail_text: str, **_: Any) -> list[dict[str, Any]]:
    return never_pgrep_worker_liveness_checks(tail_text)


def _assert_no_hand_iterate(tail_text: str, **_: Any) -> list[dict[str, Any]]:
    return no_hand_iterate_checks(tail_text)


SCENARIOS: dict[str, dict[str, Any]] = {
    "doctor-loads": {
        "description": "Orchestrator runs goal-flight doctor and summarizes JSON",
        "assert": _assert_doctor_loads,
    },
    "resume-after-compaction": {
        "description": "Orchestrator resumes from RESUME-NOTES and runs fast test subset",
        "assert": _assert_resume_after_compaction,
    },
    "continue-prescribed-step-two": {
        "description": "Orchestrator runs step 2 without engagement bait when step 1 needs no user decision",
        "assert": _assert_continue_prescribed_step_two,
    },
    "read-skill-end-to-end": {
        "description": "Orchestrator reads back-half SKILL.md routing text, not only command lookup",
        "assert": _assert_read_skill_end_to_end,
    },
    "compaction-reload-skill": {
        "description": "Orchestrator reloads SKILL.md after compaction handoff and quotes rotating sentinel",
        "assert": _assert_compaction_reload_skill,
    },
    "compaction-reload-in-skill-continuation": {
        "description": "Orchestrator stays in-skill after compaction by dispatching workers and gating review",
        "assert": _assert_compaction_reload_in_skill_continuation,
    },
    "review-flight-at-completion": {
        "description": "Orchestrator dispatches canonical review before committing a completed chunk",
        "assert": _assert_review_flight_at_completion,
    },
    "chat-as-requirements": {
        "description": "Orchestrator appends mid-session asks to the active queue without pivoting",
        "assert": _assert_chat_as_requirements,
    },
    "draft-goal-office-hours": {
        "description": "Orchestrator routes fuzzy draft goals to office-hours or ask-questions before implementation",
        "assert": _assert_draft_goal_office_hours,
    },
    "vague-goal-premise-backlog": {
        "description": "Orchestrator records vague premises in a premise backlog instead of blocking on clarification",
        "assert": _assert_vague_goal_premise_backlog,
    },
    "context-load-order": {
        "description": "Orchestrator reads AGENTS.md, SKILL.md, then the relevant protocol in order",
        "assert": _assert_context_load_order,
    },
    "goal-loop-default": {
        "description": "Orchestrator routes convergence-heavy implementation to a goal-loop dispatch",
        "assert": _assert_goal_loop_default,
    },
    "dispatch-cli-worker-via-crash-safe-command": {
        "description": "Orchestrator launches CLI workers through the crash-safe dispatch wrapper",
        "assert": _assert_dispatch_cli_worker_crash_safe,
    },
    "never-pgrep-for-worker-liveness": {
        "description": "Orchestrator checks worker liveness through identity-aware status surfaces",
        "assert": _assert_never_pgrep_worker_liveness,
    },
    "no-hand-iterate": {
        "description": "Orchestrator stops after the edit/test cycle smell and delegates the loop",
        "assert": _assert_no_hand_iterate,
    },
}

IMPLEMENTED_CONTROLLERS: tuple[str, ...] = ("codex", "claude-acp")

CONTROLLER_ALIASES: dict[str, str] = {
    "claude-code-acp": "claude-acp",
    "claude-code-cli-acp": "claude-acp",
}


def build_multi_host_plan(
    requested_text: str,
    *,
    matrix: dict[str, Any] | None = None,
) -> dict[str, Any]:
    requested_raw = [item.strip() for item in requested_text.split(",") if item.strip()]
    requested: list[str] = []
    for item in requested_raw:
        normalized = CONTROLLER_ALIASES.get(item, item)
        if normalized not in requested:
            requested.append(normalized)

    probe_payload = matrix if matrix is not None else probe_matrix.build_probe_matrix()
    controllers = probe_payload.get("controllers") or {}
    available = probe_payload.get("available_controllers") or []
    supported = list(IMPLEMENTED_CONTROLLERS)
    selected = [
        controller
        for controller in available
        if controller in requested and controller in supported
    ]
    unknown = [controller for controller in requested if controller not in controllers]
    unavailable = [
        controller
        for controller in requested
        if controller in controllers and controller not in available
    ]
    available_unsupported = [
        controller
        for controller in requested
        if controller in available and controller not in supported
    ]

    return {
        "schema": "goalflight.controller-behavior.multi-host.plan.v1",
        "requested_raw": requested_raw,
        "requested_controllers": requested,
        "available_controllers": available,
        "supported_controllers": supported,
        "selected_controllers": selected,
        "unknown_controllers": unknown,
        "unavailable_controllers": unavailable,
        "available_unsupported_controllers": available_unsupported,
        "scenarios": list(SCENARIOS),
    }


def run_codex_scenario(
    scenario_id: str,
    *,
    project_root: Path,
    timeout: float,
    transcript_dir: str | None = None,
) -> dict[str, Any]:
    started = time.time()
    row = probe_matrix.probe_controller("codex")
    if not row.get("available"):
        return harness_result(
            controller="codex",
            scenario=scenario_id,
            ok=False,
            skipped=True,
            skip_reason=row.get("skip_reason") or "codex unavailable",
            transport="bash_tail",
            elapsed_s=monotonic_elapsed(started),
        )

    spec = SCENARIOS.get(scenario_id)
    if spec is None:
        return harness_result(
            controller="codex",
            scenario=scenario_id,
            ok=False,
            skipped=True,
            skip_reason=f"unknown scenario: {scenario_id}",
            transport="bash_tail",
            elapsed_s=monotonic_elapsed(started),
        )

    doctor = doctor_snapshot(project_root)
    scenario_root = project_root
    cleanup_root: Path | None = None
    sentinel = ""
    if scenario_id in {"compaction-reload-skill", "compaction-reload-in-skill-continuation"}:
        scenario_root, sentinel = _prepare_compaction_reload_project(project_root)
        cleanup_root = scenario_root
    prompt = _load_prompt(scenario_id, scenario_root, sentinel=sentinel)

    codex_dir = REPO_ROOT / "scripts/hosts/codex"
    sys.path.insert(0, str(codex_dir))
    from bash_tail_controller import run_codex_bash_tail  # noqa: WPS433

    long_scenarios = {
        "resume-after-compaction",
        "continue-prescribed-step-two",
        "compaction-reload-skill",
        "compaction-reload-in-skill-continuation",
        "review-flight-at-completion",
    }
    scenario_timeout = max(timeout, 420.0) if scenario_id in long_scenarios else timeout

    try:
        session = run_codex_bash_tail(
            project_root=scenario_root,
            prompt_text=prompt,
            session_id=f"codex-{scenario_id}",
            timeout=scenario_timeout,
        )
    finally:
        if cleanup_root is not None:
            shutil.rmtree(cleanup_root, ignore_errors=True)
    tail_text = session.get("tail_text") or ""
    model_tail_text = prompt_echo_free_tail(tail_text, prompt)
    assert_fn: Callable[..., list[dict[str, Any]]] = spec["assert"]
    checks = assert_fn(
        model_tail_text,
        doctor=doctor,
        sentinel=sentinel,
        prompt_text=prompt,
    )
    checks.append(
        {
            "id": "bash_tail_complete",
            "ok": bool(session.get("complete_marker")) and session.get("watcher_returncode") == 0,
            "detail": {
                "worker_returncode": session.get("worker_returncode"),
                "watcher_returncode": session.get("watcher_returncode"),
            },
        }
    )
    ok = bool(session.get("ok")) and all(c.get("ok") for c in checks)
    if transcript_dir:
        transcript_path = _transcript_dir(project_root, transcript_dir) / f"{scenario_id}.transcript.log"
        _write_codex_transcript(
            transcript_path=transcript_path,
            scenario_id=scenario_id,
            prompt=prompt,
            session=session,
            checks=checks,
            ok=ok,
        )
        session["transcript_path"] = str(transcript_path)
    return harness_result(
        controller="codex",
        scenario=scenario_id,
        ok=ok,
        skipped=False,
        transport="bash_tail",
        doctor=doctor,
        session=session,
        checks=checks,
        elapsed_s=monotonic_elapsed(started),
    )


def _transcript_dir(default_root: Path, explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    today = _dt.date.today().isoformat()
    return default_root / "docs-private" / "reviews" / f"{today}-chunk-15"


def _write_acp_transcript(
    *,
    transcript_path: Path,
    scenario_id: str,
    prompt: str,
    command: list[str],
    stdout: str,
    stderr: str,
    payload: dict[str, Any],
) -> None:
    transcript_path.parent.mkdir(parents=True, exist_ok=True)
    result_text = str(payload.get("result_text") or payload.get("text_excerpt") or "")
    transcript_path.write_text(
        "\n".join(
            [
                f"controller: claude-acp",
                f"scenario: {scenario_id}",
                f"state: {payload.get('state')}",
                f"ok: {payload.get('ok')}",
                f"command: {json.dumps(command)}",
                "",
                "PROMPT:",
                prompt.rstrip(),
                "",
                "RESULT_TEXT:",
                result_text.rstrip(),
                "",
                "STDOUT:",
                stdout.rstrip(),
                "",
                "STDERR:",
                stderr.rstrip(),
                "",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def _write_codex_transcript(
    *,
    transcript_path: Path,
    scenario_id: str,
    prompt: str,
    session: dict[str, Any],
    checks: list[dict[str, Any]],
    ok: bool,
) -> None:
    transcript_path.parent.mkdir(parents=True, exist_ok=True)
    transcript_path.write_text(
        "\n".join(
            [
                "controller: codex",
                f"scenario: {scenario_id}",
                f"ok: {ok}",
                f"worker_returncode: {session.get('worker_returncode')}",
                f"watcher_returncode: {session.get('watcher_returncode')}",
                f"checks: {json.dumps(checks)}",
                "",
                "PROMPT:",
                prompt.rstrip(),
                "",
                "TAIL:",
                str(session.get("tail_text") or "").rstrip(),
                "",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def run_claude_code_acp_scenario(
    scenario_id: str,
    *,
    project_root: Path,
    timeout: float,
    transcript_dir: str | None = None,
) -> dict[str, Any]:
    started = time.time()
    controller = "claude-acp"
    row = probe_matrix.probe_controller(controller)
    if not row.get("available"):
        return harness_result(
            controller=controller,
            scenario=scenario_id,
            ok=False,
            skipped=True,
            skip_reason=row.get("skip_reason") or "claude-code-cli-acp unavailable",
            transport="acp",
            elapsed_s=monotonic_elapsed(started),
        )

    spec = SCENARIOS.get(scenario_id)
    if spec is None:
        return harness_result(
            controller=controller,
            scenario=scenario_id,
            ok=False,
            skipped=True,
            skip_reason=f"unknown scenario: {scenario_id}",
            transport="acp",
            elapsed_s=monotonic_elapsed(started),
        )

    doctor = doctor_snapshot(project_root)
    scenario_root = project_root
    cleanup_root: Path | None = None
    sentinel = ""
    if scenario_id in {"compaction-reload-skill", "compaction-reload-in-skill-continuation"}:
        scenario_root, sentinel = _prepare_compaction_reload_project(project_root)
        cleanup_root = scenario_root
    prompt = _load_prompt(scenario_id, scenario_root, sentinel=sentinel)

    long_scenarios = {
        "resume-after-compaction",
        "continue-prescribed-step-two",
        "compaction-reload-skill",
        "compaction-reload-in-skill-continuation",
        "review-flight-at-completion",
    }
    scenario_timeout = max(timeout, 420.0) if scenario_id in long_scenarios else timeout
    transcript_root = _transcript_dir(project_root, transcript_dir)
    transcript_path = transcript_root / f"{scenario_id}.transcript.log"

    with tempfile.TemporaryDirectory(prefix="goalflight-controller-acp-") as td:
        temp_root = Path(td)
        prompt_path = temp_root / f"{scenario_id}.prompt.md"
        status_path = temp_root / f"{scenario_id}.status.json"
        prompt_path.write_text(prompt.strip() + "\n", encoding="utf-8")
        cmd = [
            sys.executable,
            str(REPO_ROOT / "scripts" / "goalflight_acp_run.py"),
            "--agent",
            "claude",
            "--cwd",
            str(scenario_root),
            "--dispatch-id",
            f"controller-behavior-claude-acp-{scenario_id}-{uuid.uuid4().hex[:8]}",
            "--prompt-id",
            scenario_id,
            "--prompt",
            str(prompt_path),
            "--status-json",
            str(status_path),
            "--json",
            "--idle-timeout",
            str(scenario_timeout),
            "--progress-stall-s",
            str(scenario_timeout),
            "--max-quiet-s",
            str(scenario_timeout + 60),
        ]
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(scenario_root),
                capture_output=True,
                text=True,
                timeout=scenario_timeout + 120,
                check=False,
            )
            stdout = proc.stdout
            stderr = proc.stderr
            returncode = proc.returncode
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout or ""
            stderr = (exc.stderr or "") + "\nTIMEOUT"
            returncode = 124

        payload: dict[str, Any] = {}
        if stdout.strip():
            try:
                parsed = json.loads(stdout)
                if isinstance(parsed, dict):
                    payload = parsed
            except json.JSONDecodeError:
                payload = {}
        if not payload and status_path.is_file():
            try:
                parsed = json.loads(status_path.read_text(encoding="utf-8"))
                if isinstance(parsed, dict):
                    payload = parsed
            except (OSError, json.JSONDecodeError):
                payload = {}

    if cleanup_root is not None:
        shutil.rmtree(cleanup_root, ignore_errors=True)

    _write_acp_transcript(
        transcript_path=transcript_path,
        scenario_id=scenario_id,
        prompt=prompt,
        command=cmd,
        stdout=stdout,
        stderr=stderr,
        payload=payload,
    )
    tail_text = "\n".join(
        str(part)
        for part in (
            payload.get("result_text"),
            payload.get("text_excerpt"),
            stdout,
            stderr,
        )
        if part
    )
    model_tail_text = prompt_echo_free_tail(tail_text, prompt)
    assert_fn: Callable[..., list[dict[str, Any]]] = spec["assert"]
    checks = assert_fn(
        model_tail_text,
        doctor=doctor,
        sentinel=sentinel,
        prompt_text=prompt,
    )
    checks.append(
        {
            "id": "acp_complete",
            "ok": returncode == 0 and payload.get("state") == "complete",
            "detail": {
                "returncode": returncode,
                "state": payload.get("state"),
                "error": payload.get("error"),
            },
        }
    )
    checks.append(
        {
            "id": "transcript_written",
            "ok": transcript_path.is_file() and transcript_path.stat().st_size > 0,
            "detail": {"path": str(transcript_path)},
        }
    )
    ok = returncode == 0 and payload.get("state") == "complete" and all(c.get("ok") for c in checks)
    return harness_result(
        controller=controller,
        scenario=scenario_id,
        ok=ok,
        skipped=False,
        transport="acp",
        doctor=doctor,
        session={
            "transport": "acp",
            "agent": "claude-code-cli-acp",
            "returncode": returncode,
            "state": payload.get("state"),
            "ok": payload.get("ok"),
            "error": payload.get("error"),
            "transcript_path": str(transcript_path),
            "status_path": str(payload.get("status_path") or ""),
            "text_excerpt": tail_text[-2000:],
        },
        checks=checks,
        elapsed_s=monotonic_elapsed(started),
    )


def run_scenario(
    controller: str,
    scenario_id: str,
    *,
    project_root: Path,
    timeout: float,
    transcript_dir: str | None = None,
) -> dict[str, Any]:
    if controller == "codex":
        return run_codex_scenario(
            scenario_id,
            project_root=project_root,
            timeout=timeout,
            transcript_dir=transcript_dir,
        )
    if controller == "claude-acp":
        return run_claude_code_acp_scenario(
            scenario_id,
            project_root=project_root,
            timeout=timeout,
            transcript_dir=transcript_dir,
        )
    row = probe_matrix.probe_controller(controller)
    return harness_result(
        controller=controller,
        scenario=scenario_id,
        ok=False,
        skipped=True,
        skip_reason=row.get("skip_reason") or f"controller {controller!r} not implemented",
        elapsed_s=0.0,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Goal Flight orchestrator behavior scenario runner")
    parser.add_argument("--controller", default="codex", help="Orchestrator host id")
    parser.add_argument("--scenario", default="doctor-loads", help="Scenario id")
    parser.add_argument("--directory", "-C", default=str(REPO_ROOT), help="Project root")
    parser.add_argument("--timeout", type=float, default=300.0, help="Scenario timeout seconds")
    parser.add_argument("--transcript-dir", help="Directory for live orchestrator transcripts")
    parser.add_argument("--json", action="store_true", help="Emit JSON")
    args = parser.parse_args()

    payload = run_scenario(
        args.controller,
        args.scenario,
        project_root=Path(args.directory).resolve(),
        timeout=args.timeout,
        transcript_dir=args.transcript_dir,
    )
    payload.setdefault("schema", SCHEMA)

    emit_json = args.json or not sys.stdout.isatty()
    if emit_json:
        # Trim large tail text from JSON output
        session = payload.get("session")
        if isinstance(session, dict) and "tail_text" in session:
            excerpt = session["tail_text"][:2000]
            session["tail_excerpt"] = excerpt
            del session["tail_text"]
        print(json.dumps(payload, indent=2))
    else:
        mark = "OK" if payload.get("ok") else ("SKIP" if payload.get("skipped") else "FAIL")
        print(f"{args.controller} {args.scenario}: {mark}")

    if payload.get("skipped"):
        return 0
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
