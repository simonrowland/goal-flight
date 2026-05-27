#!/usr/bin/env python3
"""Procedural post-compaction resume drill (no LLM).

Mimics ``/goal-flight resume`` + verify tests still pass after handoff from
RESUME-NOTES. Used by hermetic bash tests and as the ground-truth procedure
that live controller scenarios should follow.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

HOST_DIR = Path(__file__).resolve().parent
REPO_ROOT = HOST_DIR.parents[2]
sys.path.insert(0, str(HOST_DIR))

from common import REPO_ROOT, SCHEMA, harness_result, monotonic_elapsed, run_cmd  # noqa: E402

DEFAULT_FIXTURE_RESUME = REPO_ROOT / "tests/fixtures/compaction_handoff/RESUME-NOTES.md"

FAST_TEST_SCRIPTS = [
    REPO_ROOT / "tests/python/test_controller_probe_matrix.py",
    REPO_ROOT / "tests/python/test_compaction_resume_drill.py",
]


def resolve_resume_notes(project_root: Path, explicit: Path | None) -> Path | None:
    if explicit is not None:
        path = explicit.expanduser()
        if not path.is_absolute():
            path = (project_root / path).resolve()
        return path if path.is_file() else None
    docs_private = project_root / "docs-private"
    if docs_private.is_dir():
        matches = sorted(docs_private.rglob("RESUME-NOTES*.md"))
        if matches:
            return matches[-1]
    if DEFAULT_FIXTURE_RESUME.is_file():
        return DEFAULT_FIXTURE_RESUME
    return None


def _check_resume_notes(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    lower = text.lower()
    has_tldr = "TL;DR" in text or "## tldr" in lower
    has_wake_steps = any(
        marker in text
        for marker in (
            "First 5 minutes",
            "Reading order on wake",
            "Reading order",
            "Run commands",
            "Queue / next",
        )
    )
    mentions_status = any(
        token in text
        for token in (
            "goalflight_status",
            "goalflight_status.py",
            "scripts/goalflight_status.py",
        )
    )
    return {
        "id": "resume_notes_readable",
        "ok": has_tldr and has_wake_steps and mentions_status,
        "path": str(path),
        "detail": {
            "has_tldr": has_tldr,
            "has_wake_steps": has_wake_steps,
            "mentions_status": mentions_status,
        },
    }


def _check_status(project_root: Path) -> dict[str, Any]:
    status_script = REPO_ROOT / "scripts/goalflight_status.py"
    result = run_cmd(
        [sys.executable, str(status_script), "--json"],
        cwd=project_root,
        timeout=60,
    )
    check: dict[str, Any] = {
        "id": "goalflight_status",
        "ok": result["ok"],
        "returncode": result["returncode"],
    }
    if result["ok"]:
        try:
            payload = json.loads(result["stdout"])
            check["detail"] = {"has_capacity": "capacity" in payload}
        except json.JSONDecodeError as exc:
            check["ok"] = False
            check["detail"] = {"parse_error": str(exc)}
    else:
        check["detail"] = {"stderr": result["stderr"][:300]}
    return check


def _check_git_snapshot(project_root: Path) -> dict[str, Any]:
    head = run_cmd(["git", "rev-parse", "--short", "HEAD"], cwd=project_root, timeout=15)
    branch = run_cmd(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=project_root, timeout=15)
    return {
        "id": "git_snapshot",
        "ok": head["ok"] and branch["ok"],
        "detail": {
            "head": (head["stdout"] or "").strip(),
            "branch": (branch["stdout"] or "").strip(),
        },
    }


def _run_fast_tests(project_root: Path) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for script in FAST_TEST_SCRIPTS:
        if not script.is_file():
            results.append({"script": str(script), "ok": False, "error": "missing"})
            continue
        proc = run_cmd([sys.executable, str(script)], cwd=project_root, timeout=120)
        results.append(
            {
                "script": str(script.relative_to(REPO_ROOT)),
                "ok": proc["ok"],
                "returncode": proc["returncode"],
            }
        )
    ok = all(r.get("ok") for r in results)
    return {"id": "fast_test_subset", "ok": ok, "detail": {"runs": results}}


def _run_full_tests(project_root: Path) -> dict[str, Any]:
    import os

    env = os.environ.copy()
    env.pop("GOALFLIGHT_CONTROLLER_BEHAVIOR", None)
    proc = run_cmd(["bash", "./tests/run.sh"], cwd=project_root, timeout=900, env=env)
    summary_ok = proc["ok"] and "0 failed" in proc["stdout"]
    return {
        "id": "full_test_suite",
        "ok": summary_ok,
        "returncode": proc["returncode"],
        "detail": {"tail": (proc["stdout"] or "")[-400:]},
    }


def run_compaction_resume_drill(
    *,
    project_root: Path,
    resume_notes: Path | None = None,
    test_mode: str = "none",
) -> dict[str, Any]:
    """test_mode: none | fast | full"""
    started = time.time()
    project_root = project_root.resolve()
    notes_path = resolve_resume_notes(project_root, resume_notes)
    checks: list[dict[str, Any]] = []

    if notes_path is None:
        return harness_result(
            controller="procedural",
            scenario="compaction-resume-drill",
            ok=False,
            skipped=True,
            skip_reason="no RESUME-NOTES found",
            checks=checks,
            elapsed_s=monotonic_elapsed(started),
        )

    checks.append(_check_resume_notes(notes_path))
    checks.append(_check_status(project_root))
    checks.append(_check_git_snapshot(project_root))

    if test_mode == "fast":
        checks.append(_run_fast_tests(project_root))
    elif test_mode == "full":
        checks.append(_run_full_tests(project_root))

    ok = all(c.get("ok") for c in checks)
    return harness_result(
        controller="procedural",
        scenario="compaction-resume-drill",
        ok=ok,
        skipped=False,
        resume_notes=str(notes_path),
        test_mode=test_mode,
        checks=checks,
        elapsed_s=monotonic_elapsed(started),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Post-compaction resume drill")
    parser.add_argument("--directory", "-C", default=str(REPO_ROOT), help="Project root")
    parser.add_argument("--resume-notes", type=Path, help="Explicit RESUME-NOTES path")
    parser.add_argument("--fast-tests", action="store_true", help="Run fast resume test subset")
    parser.add_argument("--full-tests", action="store_true", help="Run ./tests/run.sh (slow)")
    parser.add_argument("--json", action="store_true", help="Emit JSON")
    args = parser.parse_args()

    mode = "none"
    if args.full_tests:
        mode = "full"
    elif args.fast_tests:
        mode = "fast"

    payload = run_compaction_resume_drill(
        project_root=Path(args.directory),
        resume_notes=args.resume_notes,
        test_mode=mode,
    )
    payload.setdefault("schema", SCHEMA)

    if args.json or not sys.stdout.isatty():
        print(json.dumps(payload, indent=2))
    else:
        mark = "OK" if payload.get("ok") else ("SKIP" if payload.get("skipped") else "FAIL")
        print(f"compaction-resume-drill: {mark}")

    if payload.get("skipped"):
        return 0
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
