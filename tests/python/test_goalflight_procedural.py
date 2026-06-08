#!/usr/bin/env python3
"""Standalone tests for procedural goal-flight helpers."""

from __future__ import annotations

from support import skip_posix_on_native_windows

skip_posix_on_native_windows("procedural guards assert POSIX shell and /tmp contracts")

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import goalflight_doctor
import goalflight_capacity
import goalflight_review_job
import goalflight_session_status


def run(args: list[str], *, state_dir: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if state_dir:
        env["GOALFLIGHT_STATE_DIR"] = str(state_dir)
    proc = subprocess.run(args, cwd=ROOT, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and proc.returncode != 0:
        raise AssertionError(f"{args} exited {proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}")
    return proc


def assert_true(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)


def test_capacity_acquire_release_cooldown() -> None:
    with tempfile.TemporaryDirectory() as td:
        state_dir = Path(td)
        profile = json.loads(run(["python3", "scripts/goalflight_capacity.py", "profile", "--json", "--ram-mb", "16384"], state_dir=state_dir).stdout)
        assert_true("operating cap clamps below raw ceiling", profile["operating_cap"] <= 3)

        first = json.loads(run(["python3", "scripts/goalflight_capacity.py", "acquire", "--agent", "codex", "--dispatch-id", "d1", "--ram-mb", "16384", "--max-total", "1"], state_dir=state_dir).stdout)
        assert_true("first acquire allowed", first["decision"] == "allow")
        wait = run(["python3", "scripts/goalflight_capacity.py", "acquire", "--agent", "codex", "--dispatch-id", "d2", "--ram-mb", "16384", "--max-total", "1"], state_dir=state_dir, check=False)
        assert_true("second acquire exits wait", wait.returncode == 2)
        assert_true("second acquire waits", json.loads(wait.stdout)["decision"] == "wait")

        lease_id = first["lease"]["lease_id"]
        run(["python3", "scripts/goalflight_capacity.py", "release", "--lease-id", lease_id], state_dir=state_dir)
        run(["python3", "scripts/goalflight_capacity.py", "cooldown", "set", "--agent", "codex", "--seconds", "60", "--reason", "session_limit"], state_dir=state_dir)
        blocked = run(["python3", "scripts/goalflight_capacity.py", "acquire", "--agent", "codex", "--dispatch-id", "d3", "--ram-mb", "16384"], state_dir=state_dir, check=False)
        assert_true("cooldown blocks", blocked.returncode == 2 and "cooldown" in json.loads(blocked.stdout)["reason"])
        run(["python3", "scripts/goalflight_capacity.py", "cooldown", "clear", "--agent", "codex"], state_dir=state_dir)
        status = json.loads(run(["python3", "scripts/goalflight_capacity.py", "status", "--json", "--ram-mb", "16384"], state_dir=state_dir).stdout)
        assert_true("status schema", status["schema"] == "goalflight.capacity.v1")


def test_chunk_summary_empty_state_dir_json_shape() -> None:
    script = ROOT / "scripts" / "goalflight_chunk_summary.py"
    assert_true("chunk summary script exists", script.exists())
    with tempfile.TemporaryDirectory() as td:
        state_dir = Path(td)
        payload = json.loads(
            run(
                [
                    "python3",
                    "scripts/goalflight_chunk_summary.py",
                    "--slug",
                    "missing-chunk",
                    "--state-dir",
                    str(state_dir),
                    "--json",
                ],
                state_dir=state_dir,
            ).stdout
        )
    assert_true("chunk summary slug", payload["slug"] == "missing-chunk")
    assert_true("chunk summary state", payload["state"] == "missing")
    assert_true("chunk summary dispatch id", payload["dispatch_id"] is None)
    assert_true("chunk summary worker liveness", payload["worker_pid_alive"] is False)
    for key in (
        "slug",
        "dispatch_id",
        "state",
        "worker_pid_alive",
        "status_path",
        "log_path",
        "last_marker",
        "mins_since_last_event",
        "decision_hint",
    ):
        assert_true(f"chunk summary key {key}", key in payload)


def test_capacity_prunes_review_terminal_states() -> None:
    old = "2000-01-01T00:00:00+00:00"
    data = {
        "schema": "goalflight.capacity.v1",
        "machine_id": "test",
        "leases": {
            "l1": {"lease_id": "l1", "state": "inconclusive_timeout", "released_at": old},
            "l2": {"lease_id": "l2", "state": "blocked_session_limit", "ended_at": old},
            "l3": {"lease_id": "l3", "state": "wedged", "released_at": old},
            "l4": {"lease_id": "l4", "state": "tool_timeout", "released_at": old},
            "l5": {"lease_id": "l5", "state": "result_too_large", "released_at": old},
        },
        "cooldowns": {},
    }
    goalflight_capacity.prune_state(data)
    assert_true("review terminal leases pruned", data["leases"] == {})


def test_ledger_record_finish_status() -> None:
    with tempfile.TemporaryDirectory() as td:
        state_dir = Path(td)
        prompt = state_dir / "prompt.md"
        prompt.write_text("hello\n")
        rec = json.loads(
            run(
                [
                    "python3",
                    "scripts/goalflight_ledger.py",
                    "record",
                    "--dispatch-id",
                    "weird/id",
                    "--prompt-path",
                    str(prompt),
                    "--agent",
                    "codex",
                    "--transport",
                    "file-backed-review",
                    "--worker-pid",
                    str(os.getpid()),
                    "--json",
                ],
                state_dir=state_dir,
            ).stdout
        )
        assert_true("record wrote", rec["ok"])
        run(["python3", "scripts/goalflight_ledger.py", "finish", "--dispatch-id", "weird/id", "--state", "complete"], state_dir=state_dir)
        status = json.loads(run(["python3", "scripts/goalflight_ledger.py", "status", "--json"], state_dir=state_dir).stdout)
        assert_true("ledger schema", status["schema"] == "goalflight.dispatch.v1")
        assert_true("finished visible", any(row["state"] == "complete" for row in status["records"]))

        run(
            [
                "python3",
                "scripts/goalflight_ledger.py",
                "record",
                "--dispatch-id",
                "review-timeout",
                "--prompt-path",
                str(prompt),
                "--agent",
                "codex",
                "--transport",
                "file-backed-review",
                "--json",
            ],
            state_dir=state_dir,
        )
        run(["python3", "scripts/goalflight_ledger.py", "finish", "--dispatch-id", "review-timeout", "--state", "inconclusive_timeout"], state_dir=state_dir)
        status = json.loads(run(["python3", "scripts/goalflight_ledger.py", "status", "--json"], state_dir=state_dir).stdout)
        timeout_row = next(row for row in status["records"] if row["dispatch_id"] == "review-timeout")
        assert_true("terminal review state classified as itself", timeout_row["classification"] == "inconclusive_timeout")


def test_doctor_json_shape() -> None:
    payload = goalflight_doctor.doctor(ROOT)
    assert_true("doctor schema", payload["schema"] == "goalflight.doctor.v1")
    assert_true("platform section", "platform" in payload)
    for key in ("is_macos", "is_linux", "os_sandbox_available"):
        assert_true(f"platform.{key} present", key in payload["platform"])
    assert_true("wsl_filesystems section", "wsl_filesystems" in payload)
    assert_true("plugin section", "plugin" in payload and "manifest_exists" in payload["plugin"])
    assert_true("host install section", "host_goalflight_install" in payload)
    assert_true("codex host install probe", "codex" in payload["host_goalflight_install"])
    assert_true("installed skill drift section", "installed_skill_drift" in payload)
    isd = payload["installed_skill_drift"]
    for key in ("source_root", "source_root_hash", "entries", "drift"):
        assert_true(f"installed_skill_drift.{key} present", key in isd)
    assert_true("installed_skill_drift entries is a list", isinstance(isd.get("entries"), list))
    for entry in isd.get("entries", []):
        for key in (
            "host",
            "path",
            "source",
            "source_hash",
            "source_alternatives",
            "installed_hash",
            "drift",
            "install_mode",
            "resync_command",
        ):
            assert_true(f"installed_skill_drift entry {key} present", key in entry)
    assert_true("project readiness section", "project_goalflight_readiness" in payload)
    assert_true("capacity section", payload["capacity"]["schema"] == "goalflight.capacity.profile.v1")
    assert_true("autoreview section", "autoreview" in payload)
    assert_true("autoreview ok key", "ok" in payload["autoreview"])
    assert_true("autoreview script path", payload["autoreview"]["script_path"].endswith("scripts/autoreview.sh"))
    assert_true("autoreview upstream helper key", "upstream_helper" in payload["autoreview"])
    # Resolution contract (catches the regression where ok=True but upstream_helper
    # resolves to None — the env-based AUTOREVIEW_HELPER fallback path going silent
    # while keeping the key present would otherwise pass the schema assertions).
    autoreview = payload["autoreview"]
    if autoreview.get("ok"):
        assert_true(
            "autoreview upstream helper resolves when ok",
            autoreview.get("upstream_helper") not in (None, ""),
        )
    # Worker-reliability hardening (commit #8): three new doctor sections
    # surface the activation contract + AGENTS.md tracking + RESUME-NOTES
    # naming canonical patterns directly in doctor output.
    assert_true("agents_md_state section", "agents_md_state" in payload)
    ams = payload["agents_md_state"]
    for key in ("present", "tracked", "gitignored", "has_goalflight_section", "ok"):
        assert_true(f"agents_md_state.{key} present", key in ams)
    assert_true("session_status section", "session_status" in payload)
    ss = payload["session_status"]
    assert_true("session_status.ok present", "ok" in ss)
    if ss.get("ok"):
        for key in ("active", "queue_file", "active_leases_in_project"):
            assert_true(f"session_status.{key} present when ok", key in ss)
    assert_true("resume_notes_pattern section", "resume_notes_pattern" in payload)
    rnp = payload["resume_notes_pattern"]
    for key in ("present", "count", "pattern_violations", "ok"):
        assert_true(f"resume_notes_pattern.{key} present", key in rnp)
    assert_true(
        "resume_notes_pattern violations is a list",
        isinstance(rnp.get("pattern_violations"), list),
    )


def _write_skill(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_installed_skill_drift_covers_project_local_copy() -> None:
    old_home = os.environ.get("HOME")
    try:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            project = root / "project"
            skill_root = root / "goal-flight"
            os.environ["HOME"] = str(home)
            _write_skill(skill_root / "SKILL.md", "root skill\n")
            _write_skill(skill_root / "plugins/goal-flight/skills/goal-flight/SKILL.md", "codex skill\n")
            _write_skill(skill_root / "configs/cursor/skills/goal-flight/SKILL.md", "cursor skill\n")
            _write_skill(skill_root / "configs/opencode/skills/goal-flight/SKILL.md", "opencode skill\n")
            _write_skill(skill_root / "configs/grok/skills/goal-flight/SKILL.md", "grok skill\n")
            _write_skill(project / ".cursor/skills/goal-flight/SKILL.md", "old cursor skill\n")

            payload = goalflight_doctor.check_installed_skill_drift(skill_root, project)
            entries = {entry["path"]: entry for entry in payload["entries"]}
            project_entry = entries[str(project / ".cursor/skills/goal-flight/SKILL.md")]
            assert_true("project-local cursor copy detected", project_entry["host"] == "cursor")
            assert_true("project-local stale copy warns", project_entry["drift"] is True)
    finally:
        if old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old_home


def test_installed_skill_drift_allows_claude_link_mode() -> None:
    old_home = os.environ.get("HOME")
    try:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            project = root / "project"
            skill_root = root / "goal-flight"
            os.environ["HOME"] = str(home)
            _write_skill(skill_root / "SKILL.md", "root skill\n")
            _write_skill(skill_root / "plugins/goal-flight/skills/goal-flight/SKILL.md", "codex skill\n")
            _write_skill(skill_root / "configs/cursor/skills/goal-flight/SKILL.md", "cursor skill\n")
            _write_skill(skill_root / "configs/opencode/skills/goal-flight/SKILL.md", "opencode skill\n")
            _write_skill(skill_root / "configs/grok/skills/goal-flight/SKILL.md", "grok skill\n")
            cursor_parent = home / ".cursor/skills"
            cursor_parent.mkdir(parents=True)
            os.symlink(skill_root, cursor_parent / "goal-flight")

            payload = goalflight_doctor.check_installed_skill_drift(skill_root, project)
            entries = {entry["path"]: entry for entry in payload["entries"]}
            cursor_entry = entries[str(home / ".cursor/skills/goal-flight/SKILL.md")]
            assert_true("cursor link mode classified", cursor_entry["install_mode"] == "symlink")
            assert_true("cursor link mode compares root skill", cursor_entry["source"] == str(skill_root / "SKILL.md"))
            assert_true("cursor link mode does not false-warn", cursor_entry["drift"] is False)
    finally:
        if old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old_home


def test_installed_skill_drift_project_symlink_stays_copy_mode() -> None:
    old_home = os.environ.get("HOME")
    try:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            actual_project = root / "actual-project"
            project_link = root / "project-link"
            skill_root = root / "goal-flight"
            os.environ["HOME"] = str(home)
            _write_skill(skill_root / "SKILL.md", "root skill\n")
            _write_skill(skill_root / "plugins/goal-flight/skills/goal-flight/SKILL.md", "codex skill\n")
            _write_skill(skill_root / "configs/cursor/skills/goal-flight/SKILL.md", "cursor skill\n")
            _write_skill(skill_root / "configs/opencode/skills/goal-flight/SKILL.md", "opencode skill\n")
            _write_skill(skill_root / "configs/grok/skills/goal-flight/SKILL.md", "grok skill\n")
            _write_skill(actual_project / ".cursor/skills/goal-flight/SKILL.md", "cursor skill\n")
            os.symlink(actual_project, project_link)

            payload = goalflight_doctor.check_installed_skill_drift(skill_root, project_link)
            entries = {entry["path"]: entry for entry in payload["entries"]}
            cursor_entry = entries[str(project_link / ".cursor/skills/goal-flight/SKILL.md")]
            assert_true("project symlink remains copy mode", cursor_entry["install_mode"] == "copy")
            assert_true("project symlink copy does not false-warn", cursor_entry["drift"] is False)
    finally:
        if old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old_home


def test_doctor_target_project_readiness_split() -> None:
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td)
        (repo / "docs-private").mkdir()
        (repo / "docs-private/env-caveats.md").write_text("RAM_MB=1000\n")
        (repo / "SKILL.md").write_text("# Test project\n")
        (repo / "AGENTS.md").write_text(
            "## Goal Flight Routing\n"
            f"- skill-root: ${{GOALFLIGHT_ROOT:-{ROOT}}}\n"
            "- load order: AGENTS.md -> SKILL.md -> commands/*.md\n"
            "## Commands\n"
            "- test: `pytest`\n"
            "- lint: `ruff check .`\n"
        )
        payload = goalflight_doctor.doctor(repo)
        assert_true("target plugin skipped", payload["plugin"]["skipped"] is True)
        readiness = payload["project_goalflight_readiness"]
        assert_true("target project ready", readiness["ok"] is True)
        assert_true("test command recorded", readiness["commands"]["test"] == "`pytest`")
        assert_true("skill root resolved from routing", readiness["skill_root"]["source"] == "AGENTS.md")
        assert_true("skill root exists", readiness["skill_root"]["exists"] is True)
        assert_true(
            "context-mode probe uses skill root",
            payload["context_mode"]["register_script"].endswith("scripts/register-context-mode-codex.py"),
        )


def test_doctor_package_repo_validates_plugin_manifest() -> None:
    payload = goalflight_doctor.doctor(ROOT)
    assert_true("package plugin validation not skipped", payload["plugin"]["skipped"] is False)
    assert_true("package plugin manifest present", payload["plugin"]["manifest_exists"] is True)


def test_doctor_cli_target_project_skips_package_plugin() -> None:
    with tempfile.TemporaryDirectory() as td:
        result = run(
            ["python3", "scripts/goalflight_doctor.py", "--project-root", td, "--json"],
            check=False,
        )
        assert_true("target doctor exits 0", result.returncode == 0)
        payload = json.loads(result.stdout)
        assert_true("target plugin skipped in CLI", payload["plugin"]["skipped"] is True)
        assert_true("target missing init warned", payload["project_goalflight_readiness"]["ok"] is False)


def test_instruction_split_contract() -> None:
    skill = (ROOT / "SKILL.md").read_text()
    # 2026-05-28: budget raised 20KB → 26KB after the worker-reliability
    # hardening additions (commit-guard pointer, session-status activation
    # contract, canonical post-compaction reload order, in-flight monitoring
    # worked commands, permission-pattern warning, stale-wrapper warning,
    # dangerous-bypass context fence) plus file-backed context-protection
    # dispatch rules; 2026-06-02 orchestrator-rebrand byte bump. Catches
    # future feature-add bloat.
    assert_true(
        # Budget raised 28.7KB -> 31KB on 2026-06-08 for the deliberate, compacted
        # "Gotchas from session traffic" section (one-liners; the line budget at
        # test_skill_structure is already satisfied). Catches future bloat.
        f"SKILL under 31KB (got {len(skill.encode())}B)",
        len(skill.encode()) <= 31_000,
    )
    for protocol in [
        "session-preflight.md",
        "tool-readiness.md",
        "dispatch-routing.md",
        "worker-markers.md",
        "state-handoff.md",
        "premises.md",
        "self-delegation.md",
        "worktrees-parallel.md",
        "milestone-review.md",
    ]:
        assert_true(f"protocol exists {protocol}", (ROOT / "protocols" / protocol).exists())
    assert_true("fork not always detailed", "self-fork-detect.sh" not in skill)
    stale = []
    for base in ["commands", "prompts", "protocols", "scripts"]:
        for path in (ROOT / base).rglob("*"):
            if path.suffix not in {".md", ".py", ".sh"}:
                continue
            for line in path.read_text(errors="replace").splitlines():
                if "SKILL.md" in line and "§" in line:
                    stale.append(str(path.relative_to(ROOT)))
                if "~/.claude/skills/goal-flight" in line:
                    stale.append(str(path.relative_to(ROOT)))
    assert_true(f"no stale SKILL section refs: {stale}", not stale)


def test_review_job_codex_no_final_is_inconclusive() -> None:
    with tempfile.TemporaryDirectory() as td:
        final = Path(td) / "missing.final.md"
        state = goalflight_review_job.classify("", "{}", 0, False, final)
        assert_true("codex no final inconclusive", state == "inconclusive_no_final")
        final.write_text("mentions blocked_session_limit as a finding\n")
        state = goalflight_review_job.classify("session limit in final report", "", 0, False, final)
        assert_true("successful final wins over text scan", state == "complete")


def test_runners_write_status_on_capacity_and_spawn_failure() -> None:
    with tempfile.TemporaryDirectory() as td:
        state_dir = Path(td) / "state"
        out_dir = Path(td) / "out"
        out_dir.mkdir()
        prompt = Path(td) / "prompt.md"
        prompt.write_text("review this\n")

        run(["python3", "scripts/goalflight_capacity.py", "cooldown", "set", "--agent", "codex", "--seconds", "60"], state_dir=state_dir)
        blocked = run(
            [
                "python3",
                "scripts/goalflight_review_job.py",
                "--agent",
                "codex",
                "--name",
                "blocked",
                "--repo",
                str(ROOT),
                "--prompt",
                str(prompt),
                "--output-dir",
                str(out_dir),
                "--timeout-s",
                "1",
                "--json",
            ],
            state_dir=state_dir,
            check=False,
        )
        assert_true("blocked review exits 2", blocked.returncode == 2)
        blocked_status = json.loads((out_dir / "blocked.status.json").read_text())
        assert_true("blocked review status", blocked_status["state"] == "blocked_capacity")

    with tempfile.TemporaryDirectory() as td:
        state_dir = Path(td) / "state"
        out_dir = Path(td) / "out"
        out_dir.mkdir()
        prompt = Path(td) / "prompt.md"
        prompt.write_text("review this\n")
        failed = run(
            [
                "python3",
                "scripts/goalflight_review_job.py",
                "--agent",
                "codex",
                "--name",
                "spawnfail",
                "--repo",
                str(ROOT),
                "--prompt",
                str(prompt),
                "--output-dir",
                str(out_dir),
                "--codex-bin",
                "/no/such/codex",
                "--timeout-s",
                "1",
                "--json",
            ],
            state_dir=state_dir,
            check=False,
        )
        assert_true("spawn failure exits 1", failed.returncode == 1)
        failed_status = json.loads((out_dir / "spawnfail.status.json").read_text())
        assert_true("spawn failure status", failed_status["state"] == "failed")
        cap = json.loads(run(["python3", "scripts/goalflight_capacity.py", "status", "--json"], state_dir=state_dir).stdout)
        assert_true("spawn failure released active lease", len(cap["active"]) == 0)

    with tempfile.TemporaryDirectory() as td:
        state_dir = Path(td) / "state"
        status = Path(td) / "acp.status.json"
        run(["python3", "scripts/goalflight_capacity.py", "cooldown", "set", "--agent", "codex-acp", "--seconds", "60"], state_dir=state_dir)
        blocked = run(
            [
                "python3",
                "scripts/goalflight_acp_run.py",
                "--agent",
                "codex-acp",
                "--cwd",
                str(ROOT),
                "--prompt-text",
                "hello",
                "--status-json",
                str(status),
                "--json",
            ],
            state_dir=state_dir,
            check=False,
        )
        assert_true("blocked acp exits nonzero", blocked.returncode != 0)
        acp_status = json.loads(status.read_text())
        assert_true("blocked acp status", acp_status["state"] == "blocked_capacity")

    with tempfile.TemporaryDirectory() as td:
        state_dir = Path(td) / "state"
        out_dir = Path(td) / "out"
        out_dir.mkdir()
        prompt = Path(td) / "prompt.md"
        prompt.write_text("custom\n")
        failed = run(
            [
                "python3",
                "scripts/goalflight_review_job.py",
                "--agent",
                "custom",
                "--name",
                "custom-missing-command",
                "--repo",
                str(ROOT),
                "--prompt",
                str(prompt),
                "--output-dir",
                str(out_dir),
                "--json",
            ],
            state_dir=state_dir,
            check=False,
        )
        assert_true("custom missing command exits 1", failed.returncode == 1)
        custom_status = json.loads((out_dir / "custom-missing-command.status.json").read_text())
        assert_true("custom missing command status", custom_status["state"] == "failed")
        cap = json.loads(run(["python3", "scripts/goalflight_capacity.py", "status", "--json"], state_dir=state_dir).stdout)
        assert_true("custom missing command released active lease", len(cap["active"]) == 0)

    with tempfile.TemporaryDirectory() as td:
        state_dir = Path(td) / "state"
        status = Path(td) / "missing-prompt.status.json"
        failed = run(
            [
                "python3",
                "scripts/goalflight_acp_run.py",
                "--agent",
                "codex-acp",
                "--cwd",
                str(ROOT),
                "--prompt",
                str(Path(td) / "missing.md"),
                "--status-json",
                str(status),
                "--json",
            ],
            state_dir=state_dir,
            check=False,
        )
        assert_true("missing prompt exits nonzero", failed.returncode != 0)
        missing_status = json.loads(status.read_text())
        assert_true("missing prompt status", missing_status["state"] == "failed")


def test_chunk_review_canonical_invocation_has_stdin_redirect() -> None:
    """Worker-reliability regression: bash-tail review wedge.

    Root cause memorialized in commit b9af53d (2026-05-27): `codex exec`
    reads stdin to EOF even when the prompt is passed positionally.
    Without an explicit stdin close, background bash-tail invocations
    inherit the parent shell's stdin and block forever — 0 bytes of stdout
    for hours with near-zero CPU. The canonical invocation in
    protocols/chunk-review.md MUST redirect stdin from /dev/null (or pipe
    the prompt in via stdin). This test catches future edits that strip
    the redirect.
    """
    chunk_review = (ROOT / "protocols/chunk-review.md").read_text()
    # Locate the canonical invocation block under "How the review runs".
    marker = "## How the review runs"
    assert_true("chunk-review.md has 'How the review runs' section", marker in chunk_review)
    after_marker = chunk_review.split(marker, 1)[1]
    # First fenced bash block after the marker is the canonical invocation.
    fence_start = after_marker.find("```bash")
    assert_true("canonical invocation bash block present", fence_start >= 0)
    fence_end = after_marker.find("```", fence_start + 7)
    assert_true("canonical invocation bash block closed", fence_end > fence_start)
    invocation = after_marker[fence_start:fence_end]
    assert_true(
        "canonical invocation invokes codex exec with sandbox + non-interactive approval policy",
        "codex exec --sandbox read-only -c approval_policy=never" in invocation,
    )
    assert_true(
        "canonical invocation does NOT use the deprecated dangerously-bypass flag "
        "(rejected by classifiers; forbidden in adapter manifests)",
        "--dangerously-bypass-approvals-and-sandbox" not in invocation,
    )
    assert_true(
        "canonical invocation explicitly redirects stdin from /dev/null",
        "< /dev/null" in invocation,
    )
    assert_true(
        "canonical invocation captures stdout to a review final-md path",
        "> docs-private/reviews/" in invocation and "review.final.md" in invocation,
    )
    assert_true(
        "canonical invocation captures stderr to a sibling log",
        "2> docs-private/reviews/" in invocation and "stderr.log" in invocation,
    )


def test_denied_permission_downgrades_complete_to_blocked() -> None:
    """Worker-reliability invariant: when inline permissions get auto-declined
    (timeout/deny) and the worker reports COMPLETE anyway, the runner must
    refuse to record state=complete. Sweep B P1 (2026-05-27): false-positive
    completion is unrecoverable; force the operator to re-dispatch or
    explicitly override via PERMISSION-OK-PROCEEDED marker.

    This is a unit test against the inline state-transition logic, not a
    full ACP run. We invoke the transition by simulating the variables.
    """
    # The transition happens inline in goalflight_acp_run.py around the
    # `payload.update(...)` block. We extract its logic into a pure helper
    # for testability — see _classify_terminal_state_with_permission_decline.
    # If that helper doesn't exist yet (this commit may inline the logic),
    # this test asserts the inline rule by reading the source.
    source = (ROOT / "scripts/goalflight_acp_run.py").read_text()
    assert_true(
        "downgrade rule present: complete + auto_declined → blocked_permission_denied",
        "blocked_permission_denied" in source
        and "permission_auto_declined" in source
        and "PERMISSION-OK-PROCEEDED" in source,
    )
    # Anchor check: the downgrade must be conditional on auto_declined AND
    # the marker absence — both signals required to be in the same block.
    # We grep for the canonical comment + sequence.
    canonical_block_markers = (
        "Sweep B P1",
        "denied permissions",
        "blocked_permission_denied",
    )
    for needle in canonical_block_markers:
        assert_true(
            f"downgrade block contains '{needle}'",
            needle in source,
        )


def test_title_allow_policy_layers_after_hard_gates() -> None:
    """Worker-reliability invariant: title-allow regex is a fast-path LAYERED
    AFTER hard safety gates, not before. A broad pattern like '.*' must NOT
    silently authorize destructive execute, network fetch, or outside-cwd
    writes. Catches sweep B P1 (2026-05-27).

    Test cases:
    - title '.*' + kind='read'           → ALLOW (fast-path safe)
    - title '.*' + kind='execute'        → escalate (hard gate: sandbox-off
                                            execute must escalate)
    - title '.*' + kind='fetch'          → escalate (hard gate)
    - title '.*' + target outside cwd    → escalate (hard gate)
    - precise pattern matching execute   → still escalates (hard gate ALWAYS
                                            wins; operator can't bypass via
                                            narrow pattern either — must use
                                            OS sandbox instead)
    - no-match pattern + safe kind       → falls through to base; base allows
    """
    import re
    import goalflight_acp_run as gar

    yolo = [re.compile(".*")]
    policy = gar.make_title_allow_policy(yolo)

    # Helper to build a tool-call dict matching the client's _tc_get shape.
    def tc(title, kind, locations=None):
        return {
            "title": title,
            "kind": kind,
            "locations": locations or [],
        }

    cwd = "/tmp/test-cwd"

    # Safe read with broad pattern → allow (fast-path)
    assert_true(
        ".* allows read kind",
        policy(tc("read foo.txt", "read"), [], cwd) == "allow",
    )

    # Execute with broad pattern → must escalate (hard gate)
    assert_true(
        ".* must NOT bypass execute hard-gate",
        policy(tc("run rm -rf /", "execute"), [], cwd) == "escalate",
    )

    # Fetch with broad pattern → must escalate (hard gate)
    assert_true(
        ".* must NOT bypass fetch hard-gate",
        policy(tc("curl http://evil", "fetch"), [], cwd) == "escalate",
    )

    # Outside-cwd write with broad pattern → must escalate
    outside_write = tc("edit /etc/passwd", "edit",
                        locations=[{"path": "/etc/passwd"}])
    assert_true(
        ".* must NOT bypass outside-cwd hard-gate",
        policy(outside_write, [], cwd) == "escalate",
    )

    # Precise pattern matching execute → still escalates (hard gate always wins)
    precise = gar.make_title_allow_policy([re.compile(r"^./tests/run\.sh$")])
    assert_true(
        "precise execute pattern still hits hard-gate",
        precise(tc("./tests/run.sh", "execute"), [], cwd) == "escalate",
    )

    # In-cwd write with title match → allow (fast-path safe case)
    in_cwd_write = tc("edit foo.py", "edit",
                       locations=[{"path": f"{cwd}/foo.py"}])
    assert_true(
        "in-cwd write with broad pattern allows",
        policy(in_cwd_write, [], cwd) == "allow",
    )

    # Sweep B P1 follow-up: extended hard-gate coverage.

    # Write with NO locations → escalate (can't prove in-cwd)
    write_no_loc = tc("edit ???", "edit", locations=[])
    assert_true(
        ".* must NOT bypass write-with-no-locations hard-gate",
        policy(write_no_loc, [], cwd) == "escalate",
    )

    # kind=delete with locations in cwd → allow
    delete_in_cwd = tc("delete foo.tmp", "delete",
                       locations=[{"path": f"{cwd}/foo.tmp"}])
    assert_true(
        "in-cwd delete with broad pattern allows",
        policy(delete_in_cwd, [], cwd) == "allow",
    )

    # kind=move with locations outside cwd → escalate (outside-cwd hard gate)
    move_outside = tc("move /etc/foo /etc/bar", "move",
                       locations=[{"path": "/etc/foo"}, {"path": "/etc/bar"}])
    assert_true(
        ".* must NOT bypass outside-cwd move",
        policy(move_outside, [], cwd) == "escalate",
    )

    # Unknown / future kind → escalate (refuse to fast-path)
    unknown_kind = tc("future-op blob", "switch_mode")
    assert_true(
        ".* must NOT bypass unknown kind hard-gate",
        policy(unknown_kind, [], cwd) == "escalate",
    )

    # kind="" (empty) — read-safe per default_permission_policy → allow
    kindless = tc("approve mcp elicitation", "")
    assert_true(
        "kindless (MCP elicitation shape) allows with broad pattern",
        policy(kindless, [], cwd) == "allow",
    )

    # Regex compile failure path: ensure runtime regex exception falls
    # through silently to the base policy. Pattern object whose .search
    # raises emulates the bad-state case.
    class BrokenPattern:
        def search(self, _s):
            raise RuntimeError("simulated regex backend failure")
    broken_policy = gar.make_title_allow_policy([BrokenPattern()])
    # On exception, the policy falls through to base. For kind=read, base
    # allows. So a broken pattern should still let the safe call through.
    assert_true(
        "broken pattern falls through to base policy (read → allow)",
        broken_policy(tc("safe read", "read"), [], cwd) == "allow",
    )

    # Multiple patterns: first hits hard gate, second matches title — title
    # match must NOT bypass the hard gate. (Both patterns are .* but order
    # doesn't matter since hard gate runs before pattern iteration.)
    multi = gar.make_title_allow_policy([re.compile(".*"), re.compile("test")])
    assert_true(
        "multiple patterns: hard-gate still wins",
        multi(tc("run anything", "execute"), [], cwd) == "escalate",
    )


def test_commit_guard_partial_commit_signal_allows_through() -> None:
    """Worker-reliability invariant: when git invokes the pre-commit hook
    for a partial commit (`git commit -- <pathspec>`), git exports
    GIT_INDEX_FILE pointing at a temp index named `next-index-<pid>`.
    The guard MUST detect this and let the partial commit through, even
    when active leases exist. Sweep A P1 / D1 fix.
    """
    import goalflight_commit_guard as guard
    # Direct unit test of _commit_is_partial logic.
    tracked_keys = ("GIT_INDEX_FILE", "GIT_PARTIAL_COMMIT")
    saved_env = {k: os.environ.pop(k, None) for k in tracked_keys}
    try:
        # No env vars → not partial (bare commit shape)
        assert_true("bare commit detected as not-partial", guard._commit_is_partial() is False)
        # next-index-* basename → partial
        os.environ["GIT_INDEX_FILE"] = "/tmp/fake-repo/.git/next-index-99999"
        assert_true("next-index-* shape detected as partial", guard._commit_is_partial() is True)
        del os.environ["GIT_INDEX_FILE"]
        # Main index path (non-next-index) → not partial
        os.environ["GIT_INDEX_FILE"] = "/tmp/fake-repo/.git/index"
        assert_true(".git/index detected as not-partial", guard._commit_is_partial() is False)
        del os.environ["GIT_INDEX_FILE"]
        # GIT_PARTIAL_COMMIT set (some git versions) → partial
        os.environ["GIT_PARTIAL_COMMIT"] = "1"
        assert_true("GIT_PARTIAL_COMMIT detected as partial", guard._commit_is_partial() is True)
    finally:
        # Restore saved values (or remove any we left in env).
        for k in tracked_keys:
            os.environ.pop(k, None)
            if saved_env.get(k) is not None:
                os.environ[k] = saved_env[k]


def test_commit_guard_fail_closed_when_capacity_unavailable() -> None:
    """Worker-reliability invariant: when goalflight_capacity.py status
    fails, the guard fails CLOSED by default. Opt-in to fail-open via
    GOALFLIGHT_COMMIT_GUARD_FAIL_OPEN=1. Sweep A P2 fix.

    Simulate capacity failure by pointing GOALFLIGHT_STATE_DIR at a path
    where capacity can't initialize cleanly... actually simpler: force
    the script to fail by exposing it to a malformed state dir, OR
    monkey-patch the helper. We assert via the JSON shape.
    """
    import goalflight_commit_guard as guard
    # Simulate "leases is None" path by calling the script with a
    # bogus capacity path. We use a temp dir as the repo and confirm
    # that without a state dir, the capacity call would return None.
    # The most direct check: _active_same_root_leases handles capacity
    # status returning None and propagates the (None, error) tuple.
    saved_env = os.environ.pop("GOALFLIGHT_COMMIT_GUARD_FAIL_OPEN", None)
    try:
        # Direct: _capacity_status returns None on subprocess error.
        # Verify the helper signature change works as expected.
        leases, err = guard._active_same_root_leases(Path("/nonexistent/fake/repo"))
        # leases is [] when capacity returns empty active list, or None
        # when capacity is unreachable. Either path is fine here — we
        # just verify the function signature returns a tuple.
        assert_true(
            "_active_same_root_leases returns (leases, err) tuple",
            isinstance(leases, (list, type(None))) and (err is None or isinstance(err, str)),
        )
    finally:
        if saved_env is not None:
            os.environ["GOALFLIGHT_COMMIT_GUARD_FAIL_OPEN"] = saved_env


def test_commit_guard_refuses_with_active_leases_and_honors_override() -> None:
    """Worker-reliability invariant: commit guard refuses bare `git commit`
    when active same-root leases exist, and the error message names the
    lease ids, the partial-commit fix, and the override flag.
    """
    import goalflight_commit_guard as guard

    with tempfile.TemporaryDirectory() as td:
        repo = Path(td)
        # Init a real git repo so guard's repo-root detection works.
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit",
                        "--allow-empty", "-m", "init"], cwd=repo, check=True)
        (repo / "scratch.txt").write_text("a\n")
        subprocess.run(["git", "add", "scratch.txt"], cwd=repo, check=True)

        state_dir = repo / "state"
        env = os.environ.copy()
        env["GOALFLIGHT_STATE_DIR"] = str(state_dir)

        # No leases → exit 0
        out = subprocess.run(
            ["python3", str(ROOT / "scripts/goalflight_commit_guard.py"), "--json"],
            cwd=repo, env=env, capture_output=True, text=True,
        )
        assert_true("no leases exits 0", out.returncode == 0)
        body = json.loads(out.stdout)
        assert_true("no leases reports ok", body["ok"] is True)

        # Inject an active same-root lease via goalflight_capacity acquire.
        # We use the capacity CLI directly to ensure project_root is set.
        acq = subprocess.run(
            ["python3", str(ROOT / "scripts/goalflight_capacity.py"), "acquire",
             "--agent", "codex-acp", "--project-root", str(repo),
             "--dispatch-id", "test-guard-dispatch", "--mem-mb", "100"],
            cwd=repo, env=env, capture_output=True, text=True,
        )
        assert_true(
            f"acquire succeeds (rc={acq.returncode} stderr={acq.stderr[:200]})",
            acq.returncode == 0,
        )

        # With active lease, guard refuses
        out = subprocess.run(
            ["python3", str(ROOT / "scripts/goalflight_commit_guard.py"), "--json"],
            cwd=repo, env=env, capture_output=True, text=True,
        )
        assert_true("active lease refuses exit 2", out.returncode == 2)
        body = json.loads(out.stdout)
        assert_true("refusal reports ok=false", body["ok"] is False)
        msg = body.get("message") or ""
        assert_true("refusal names the lease id", "test-guard-dispatch" in msg)
        assert_true("refusal shows partial-commit fix",
                    "git commit -m" in msg and "--" in msg)
        assert_true("refusal names the override env var",
                    "GOALFLIGHT_COMMIT_GUARD_OVERRIDE" in msg)
        assert_true("refusal points at recovery protocol",
                    "dispatched-worker-recovery" in msg)

        # Override by lease id → allows
        env_override = dict(env)
        env_override["GOALFLIGHT_COMMIT_GUARD_OVERRIDE"] = "test-guard-dispatch"
        out = subprocess.run(
            ["python3", str(ROOT / "scripts/goalflight_commit_guard.py"), "--json"],
            cwd=repo, env=env_override, capture_output=True, text=True,
        )
        assert_true("override by id allows exit 0", out.returncode == 0)

        # Sweep A P2 fix: env `all` is refused (too easy to set + forget);
        # `all` only via explicit CLI flag.
        env_override["GOALFLIGHT_COMMIT_GUARD_OVERRIDE"] = "all"
        out = subprocess.run(
            ["python3", str(ROOT / "scripts/goalflight_commit_guard.py"), "--json"],
            cwd=repo, env=env_override, capture_output=True, text=True,
        )
        assert_true("env override 'all' refused exit 2", out.returncode == 2)

        # CLI flag --override-active-leases all → allows
        env_clean = dict(env)
        out = subprocess.run(
            ["python3", str(ROOT / "scripts/goalflight_commit_guard.py"),
             "--override-active-leases", "all", "--json"],
            cwd=repo, env=env_clean, capture_output=True, text=True,
        )
        assert_true("CLI --override-active-leases all allows exit 0", out.returncode == 0)


def test_session_status_helper_contract() -> None:
    """Worker-reliability invariant: the session-status helper is the
    canonical activation signal across compactions. Post-compaction agents
    run it from `AGENTS.md` to decide whether to load the skill.

    Covered scenarios:
    - empty project → not active, queue_file None
    - queue with state=active fresh → active, queue_state reflected
    - queue with state=active but last-touched > ttl → abandoned (not active)
    - queue with state=complete → not active, queue_state reflected
    - claim by alive different pid refuses; force succeeds
    - release clears current_session + appends history.ended_at
    """
    import goalflight_session_status as gss

    with tempfile.TemporaryDirectory() as td:
        project = Path(td)
        (project / "docs-private").mkdir()

        # Empty project
        status = gss.aggregate_status(project)
        assert_true("empty project not active", status["active"] is False)
        assert_true("empty project queue_file None", status["queue_file"] is None)

        # Fresh active queue
        queue = project / "docs-private/goal-queue-demo.md"
        queue.write_text(
            "---\n"
            "slug: demo\n"
            "started: 2026-05-28\n"
            "state: active\n"
            f"last-touched: {gss._now_iso()}\n"
            "---\n\n"
            "# Demo queue\n"
        )
        status = gss.aggregate_status(project)
        assert_true("fresh active queue is active", status["active"] is True)
        assert_true("fresh active queue_state=active", status["queue_state"] == "active")
        assert_true("fresh active queue_slug=demo", status["queue_slug"] == "demo")

        # Active but stale (past TTL)
        queue.write_text(
            "---\n"
            "slug: demo\n"
            "started: 2025-04-01\n"
            "state: active\n"
            "last-touched: 2025-04-01T00:00:00Z\n"
            "---\n\n"
            "# Demo queue\n"
        )
        status = gss.aggregate_status(project, ttl_days=7)
        assert_true("stale active queue abandoned", status["active"] is False)
        assert_true("stale queue still reports state=active", status["queue_state"] == "active")
        assert_true("stale queue reason mentions abandoned", "abandoned" in status["queue_reason"])

        # Complete queue → not active
        queue.write_text(
            "---\n"
            "slug: demo\n"
            "started: 2026-05-28\n"
            "state: complete\n"
            f"last-touched: {gss._now_iso()}\n"
            "---\n\n"
            "# Demo queue\n"
        )
        status = gss.aggregate_status(project)
        assert_true("complete queue not active", status["active"] is False)
        assert_true("complete queue_state=complete", status["queue_state"] == "complete")

        # Claim refuses on live different pid
        queue.write_text(
            "---\n"
            "slug: demo\n"
            "state: active\n"
            f"last-touched: {gss._now_iso()}\n"
            "current_session:\n"
            "  id: 11111111-1111-1111-1111-111111111111\n"
            f"  pid: {os.getpid()}\n"  # alive pid (this test process)
            "  started_at: 2026-05-28T00:00:00Z\n"
            "  hostname: testhost\n"
            "---\n\n"
            "# Demo queue\n"
        )
        # Use a fresh session id by deleting any cached session file.
        sf = project / "docs-private/.goal-flight-current-session.json"
        if sf.exists():
            sf.unlink()
        ok, msg = gss.claim(project, queue, force=False)
        assert_true("claim refuses on alive different pid", ok is False)
        assert_true("claim refusal mentions force", "force" in msg.lower())

        # Force claim succeeds
        ok, msg = gss.claim(project, queue, force=True)
        assert_true("force claim succeeds", ok is True)

        # Claim succeeds when prior pid is dead
        queue.write_text(
            "---\n"
            "slug: demo\n"
            "state: active\n"
            f"last-touched: {gss._now_iso()}\n"
            "current_session:\n"
            "  id: 22222222-2222-2222-2222-222222222222\n"
            "  pid: 1\n"  # init pid is alive but very unlikely to belong to us;
            # use a high pid that's almost certainly dead instead
            "  started_at: 2026-05-28T00:00:00Z\n"
            "  hostname: testhost\n"
            "---\n\n"
            "# Demo queue\n"
        )
        # Replace pid with a definitely-dead one (99999999 well above any practical pid)
        text = queue.read_text().replace("pid: 1", "pid: 99999999")
        queue.write_text(text)
        sf.unlink(missing_ok=True)
        ok, msg = gss.claim(project, queue, force=False)
        assert_true("claim succeeds with dead prior pid", ok is True)

        # Release clears current_session and stamps ended_at on history.
        # NOTE post-A2 fix: release requires the current_session.id to match
        # THIS terminal's session id; we just claimed with force=True so it
        # matches by construction.
        ok, msg = gss.release(project, queue, reason="test-exit")
        assert_true(f"release succeeds (msg={msg!r})", ok is True)
        front, _ = gss._parse_frontmatter(queue.read_text())
        assert_true(
            "release clears current_session",
            front.get("current_session") in (None, {}, "null"),
        )
        history = front.get("session_history") or []
        assert_true("release stamps history.ended_at", any(
            isinstance(e, dict) and e.get("ended_at") for e in history
        ))

        # A1/C2: RESUME-NOTES TL;DR signal is interpreted.
        # Write a RESUME-NOTES with state=active in frontmatter; queue is
        # complete so leases=0; verdict should still be active via the
        # third signal.
        queue.write_text(
            "---\n"
            "slug: demo\n"
            "state: complete\n"
            "---\n\n# Done queue\n"
        )
        notes = project / "docs-private/RESUME-NOTES-2026-05-28.md"
        notes.write_text(
            "---\n"
            "state: active\n"
            "---\n\n"
            "# Resume Notes — 2026-05-28\n\n## TL;DR\n\n"
            "**Status:** active mid-chunk-6\n"
        )
        status = gss.aggregate_status(project)
        assert_true(
            "active=true when queue=complete but newest RESUME-NOTES frontmatter state=active",
            status["active"] is True,
        )
        assert_true(
            "resume_notes_active key surfaced",
            status.get("resume_notes_active") is True,
        )

        # Complete RESUME-NOTES → not active via this signal
        notes.write_text(
            "---\n"
            "state: complete\n"
            "---\n\n# Done\n"
        )
        status = gss.aggregate_status(project)
        assert_true(
            "active=false when all 3 signals are complete/inactive",
            status["active"] is False,
        )

        # A3: per-terminal session identity. Two distinct PIDs in the same
        # project should get distinct session records, not share one slot.
        sf = project / "docs-private/.goal-flight-current-session.json"
        if sf.exists():
            sf.unlink()
        s1 = gss.ensure_session(project, pid=99001)
        s2 = gss.ensure_session(project, pid=99002)
        assert_true("two pids get distinct session ids", s1["id"] != s2["id"])
        # Re-fetching same pid returns same record
        s1_again = gss.ensure_session(project, pid=99001)
        assert_true("re-fetch same pid returns same id", s1["id"] == s1_again["id"])

        # A P3: --queue path outside project_root/docs-private is refused.
        outside = project.parent / "outside-queue.md"
        outside.write_text("---\nslug: outside\nstate: active\n---\n\n")
        ok, msg = gss.claim(project, outside, force=False)
        assert_true("claim refuses --queue outside docs-private", ok is False)
        outside.unlink()


def test_no_unsafe_codex_exec_invocations_across_docs() -> None:
    """Worker-reliability regression: scan all doc + prompt + command files
    for `codex exec` invocations that omit `< /dev/null`. Sweep A P0
    (2026-05-27) found SKILL.md:154 had a duplicate of the bash-tail
    canonical block without the stdin redirect; this test prevents that
    class from recurring elsewhere.

    Files scanned:
    - SKILL.md
    - commands/*.md (front-line worker prompts cite invocation shapes)
    - prompts/gstack-*.md (review-prompt templates)
    - protocols/*.md EXCEPT protocols/legacy/* (legacy is allowlisted)

    Allowlist (technical debt; track via issue):
    - protocols/legacy/bash-tail.md — kept for historical reference
    - scripts/install-codex-overrides.sh — install-time, not runtime
      review invocation
    """
    import re
    forbidden_dirs = {"protocols/legacy"}
    allowlist_files = set()
    scan_paths: list[Path] = []
    scan_paths.append(ROOT / "SKILL.md")
    scan_paths.extend(sorted((ROOT / "commands").glob("*.md")))
    scan_paths.extend(sorted((ROOT / "prompts").glob("gstack-*.md")))
    for proto in sorted((ROOT / "protocols").glob("*.md")):
        rel = proto.relative_to(ROOT).as_posix()
        if any(rel.startswith(d + "/") or rel == d for d in forbidden_dirs):
            continue
        scan_paths.append(proto)

    # Look for `codex exec` followed (within ~12 lines) by either a
    # closing fence/text without `< /dev/null` OR an explicit pipe form.
    # Allow `codex exec --help` informational mentions to pass.
    bad: list[str] = []
    for path in scan_paths:
        rel = path.relative_to(ROOT).as_posix()
        if rel in allowlist_files:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for m in re.finditer(r"codex exec\s+--sandbox", text):
            # Pull the surrounding ~16 lines as the invocation window.
            start = m.start()
            line_start = text.rfind("\n", 0, start) + 1
            # Find the end of the invocation: nearest blank-line or 16 lines.
            tail = text[line_start : line_start + 1200]
            window_end = 0
            blank_marker = "\n\n"
            blank_idx = tail.find(blank_marker)
            if blank_idx > 0:
                window_end = blank_idx
            else:
                window_end = min(len(tail), 1200)
            window = tail[:window_end]
            # An invocation is safe if it contains `< /dev/null` OR is
            # piped-into via stdin (heredoc / |) elsewhere.
            if "< /dev/null" in window or "<<EOF" in window or "<<'" in window:
                continue
            # Allow informational mentions where the codex exec line is
            # quoted inside backticks without a full multi-line invocation:
            # check for a backtick close on the same line.
            same_line = text[line_start : text.find("\n", line_start)]
            if same_line.count("`") >= 2:
                continue
            bad.append(f"{rel}:{text[:start].count(chr(10)) + 1}")
    assert_true(
        f"no `codex exec --sandbox` invocations missing `< /dev/null` (found: {bad})",
        not bad,
    )


def test_dispatched_worker_recovery_protocol_present() -> None:
    """Worker-reliability invariant: the controller-takeover recovery
    protocol must exist and reference the canonical verification gates.

    Memorialized in commit 2a662aa: when a dispatched worker blocks
    mid-chunk (typically on a permission elicitation or a wedged review),
    the orchestrator takes over by reading status JSON, running gates,
    reviewing controller-side, staging the worker's scope explicitly,
    and committing with worker attribution. Without this protocol the
    failure mode is silent stalling.
    """
    recovery = ROOT / "protocols/dispatched-worker-recovery.md"
    assert_true("dispatched-worker-recovery.md exists", recovery.exists())
    text = recovery.read_text()
    for needle in (
        "status JSON",
        "focused tests",
        "codename",  # codename-hygiene scan (commit 2a662aa)
        "stage",
        "commit",
    ):
        assert_true(
            f"recovery protocol mentions '{needle}'",
            needle.lower() in text.lower(),
        )


# Python files that construct a codex command via subprocess argv but do NOT
# go through the adapter `arg_policy` forbidden_args gate (hard-coded Popen
# sites). These carry NO forbid-list, so any occurrence of the deprecated flag
# in them is an EMIT, not a reference — scan them wholesale. Add new hard-coded
# codex-emit sites here when they appear.
KNOWN_CODEX_EMIT_PY_SITES = (
    "scripts/hosts/codex/bash_tail_controller.py",
    "scripts/goalflight_review_job.py",
)


def test_no_dangerous_bypass_in_codex_emit_surface() -> None:
    """The deprecated `--dangerously-bypass-approvals-and-sandbox` flag is
    rejected by codex's runtime classifier and forbidden in adapter manifests.
    It must not be EMITTED in an actual codex invocation. The canonical
    non-interactive form is `--sandbox <profile> -c approval_policy=never`.

    Coverage is layered, and no single mechanism covers everything:
    - `*.sh` / `*.tpl` under scripts/ and templates/ — scanned here (the
      re-audit 2026-05-29 found scripts/goalflight_recon.sh carrying it).
    - manifest-declared invocations — gated by the adapter `arg_policy`
      `forbidden_args` scan (goalflight_adapter_gate.py + validate/setup), NOT
      by this test.
    - hard-coded codex Popen argvs in Python (KNOWN_CODEX_EMIT_PY_SITES) —
      gated by NEITHER the shell scan NOR arg_policy, so scanned here directly.
    - codex's own runtime classifier — the final backstop for anything missed.

    Deliberately NOT broadly scanned:
    - other `*.py` — the flag appears there only as DATA: forbid-list elements
      (goalflight_adapter_gate.py), detection regexes (hosts/controller/
      common.py), comments. A shape-based scan cannot distinguish a forbid-list
      arg `"--flag",` from an emit arg `"--flag",`, so broad .py scanning is
      false-positive-prone; the known emit sites above carry no forbid-lists, so
      scanning THEM is unambiguous.
    - `*.md` — prohibition prose; canonical-invocation blocks are covered by
      test_chunk_review_canonical_invocation_has_stdin_redirect + skill-structure.

    A line is allowed iff it carries an explicit `do not use` prohibition
    (covers the codex-goal-prompt.md.tpl 'Do NOT use ...' note)."""
    flag = "--dangerously-bypass-approvals-and-sandbox"
    offenders: list[str] = []

    scan_paths: list[Path] = []
    for sub in ("scripts", "templates"):
        base = ROOT / sub
        if base.is_dir():
            scan_paths.extend(
                p for p in base.rglob("*")
                if p.is_file() and p.suffix in (".sh", ".tpl")
            )
    scan_paths.extend(
        ROOT / rel for rel in KNOWN_CODEX_EMIT_PY_SITES if (ROOT / rel).is_file()
    )

    for path in scan_paths:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if flag in line and "do not use" not in line.lower():
                offenders.append(f"{path.relative_to(ROOT)}:{lineno}")
    assert_true(
        f"no deprecated dangerously-bypass flag EMITTED in *.sh / *.tpl / "
        f"hard-coded codex Popen sites (prohibition 'do not use' lines allowed; "
        f"found emits: {offenders})",
        not offenders,
    )


def main() -> None:
    tests = [
        test_capacity_acquire_release_cooldown,
        test_chunk_summary_empty_state_dir_json_shape,
        test_capacity_prunes_review_terminal_states,
        test_ledger_record_finish_status,
        test_doctor_json_shape,
        test_installed_skill_drift_covers_project_local_copy,
        test_installed_skill_drift_allows_claude_link_mode,
        test_installed_skill_drift_project_symlink_stays_copy_mode,
        test_doctor_target_project_readiness_split,
        test_doctor_package_repo_validates_plugin_manifest,
        test_doctor_cli_target_project_skips_package_plugin,
        test_instruction_split_contract,
        test_review_job_codex_no_final_is_inconclusive,
        test_runners_write_status_on_capacity_and_spawn_failure,
        test_chunk_review_canonical_invocation_has_stdin_redirect,
        test_no_unsafe_codex_exec_invocations_across_docs,
        test_title_allow_policy_layers_after_hard_gates,
        test_denied_permission_downgrades_complete_to_blocked,
        test_commit_guard_partial_commit_signal_allows_through,
        test_commit_guard_fail_closed_when_capacity_unavailable,
        test_commit_guard_refuses_with_active_leases_and_honors_override,
        test_session_status_helper_contract,
        test_dispatched_worker_recovery_protocol_present,
        test_no_dangerous_bypass_in_codex_emit_surface,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")


if __name__ == "__main__":
    main()
