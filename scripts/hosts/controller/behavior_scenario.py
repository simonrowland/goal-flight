#!/usr/bin/env python3
"""Controller behavior scenarios (scripted prompts + deterministic checks).

Wave 1 implements Codex bash-tail ``doctor-loads``. Additional scenarios and
hosts follow the same contract.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable

HOST_DIR = Path(__file__).resolve().parent
REPO_ROOT = HOST_DIR.parents[2]
FIXTURES = REPO_ROOT / "tests/fixtures/controller_scenarios"
sys.path.insert(0, str(HOST_DIR))

from common import (  # noqa: E402
    SCHEMA,
    continue_prescribed_step_two_checks,
    doctor_snapshot,
    harness_result,
    monotonic_elapsed,
)
import probe_matrix  # noqa: E402


def _load_prompt(scenario_id: str, project_root: Path) -> str:
    fixture = FIXTURES / scenario_id / "prompt.md"
    if not fixture.is_file():
        raise FileNotFoundError(f"missing fixture prompt: {fixture}")
    text = fixture.read_text(encoding="utf-8")
    root = str(project_root.resolve())
    text = text.replace("{{PROJECT_ROOT}}", root)
    if "{{RESUME_NOTES_PATH}}" in text:
        from compaction_resume_drill import resolve_resume_notes  # noqa: WPS433

        notes = resolve_resume_notes(project_root, None)
        if notes is None:
            notes = FIXTURES.parent / "compaction_handoff" / "RESUME-NOTES.md"
        text = text.replace("{{RESUME_NOTES_PATH}}", str(notes.resolve()))
    return text


def _assert_doctor_loads(tail_text: str, *, doctor: dict[str, Any]) -> list[dict[str, Any]]:
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


def _assert_continue_prescribed_step_two(tail_text: str, **_: Any) -> list[dict[str, Any]]:
    return continue_prescribed_step_two_checks(tail_text)


SCENARIOS: dict[str, dict[str, Any]] = {
    "doctor-loads": {
        "description": "Controller runs goal-flight doctor and summarizes JSON",
        "assert": _assert_doctor_loads,
    },
    "resume-after-compaction": {
        "description": "Controller resumes from RESUME-NOTES and runs fast test subset",
        "assert": _assert_resume_after_compaction,
    },
    "continue-prescribed-step-two": {
        "description": "Controller runs step 2 without engagement bait when step 1 needs no user decision",
        "assert": _assert_continue_prescribed_step_two,
    },
}


def run_codex_scenario(
    scenario_id: str,
    *,
    project_root: Path,
    timeout: float,
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
    prompt = _load_prompt(scenario_id, project_root)

    codex_dir = REPO_ROOT / "scripts/hosts/codex"
    sys.path.insert(0, str(codex_dir))
    from bash_tail_controller import run_codex_bash_tail  # noqa: WPS433

    long_scenarios = {"resume-after-compaction", "continue-prescribed-step-two"}
    scenario_timeout = max(timeout, 420.0) if scenario_id in long_scenarios else timeout

    session = run_codex_bash_tail(
        project_root=project_root,
        prompt_text=prompt,
        session_id=f"codex-{scenario_id}",
        timeout=scenario_timeout,
    )
    tail_text = session.get("tail_text") or ""
    assert_fn: Callable[..., list[dict[str, Any]]] = spec["assert"]
    checks = assert_fn(tail_text, doctor=doctor)
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


def run_scenario(
    controller: str,
    scenario_id: str,
    *,
    project_root: Path,
    timeout: float,
) -> dict[str, Any]:
    if controller == "codex":
        return run_codex_scenario(scenario_id, project_root=project_root, timeout=timeout)
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
    parser = argparse.ArgumentParser(description="Goal Flight controller behavior scenario runner")
    parser.add_argument("--controller", default="codex", help="Controller host id")
    parser.add_argument("--scenario", default="doctor-loads", help="Scenario id")
    parser.add_argument("--directory", "-C", default=str(REPO_ROOT), help="Project root")
    parser.add_argument("--timeout", type=float, default=300.0, help="Scenario timeout seconds")
    parser.add_argument("--json", action="store_true", help="Emit JSON")
    args = parser.parse_args()

    payload = run_scenario(
        args.controller,
        args.scenario,
        project_root=Path(args.directory).resolve(),
        timeout=args.timeout,
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
