#!/usr/bin/env python3
"""Standalone tests for procedural goal-flight helpers."""

from __future__ import annotations

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
    assert_true("plugin section", "plugin" in payload and "manifest_exists" in payload["plugin"])
    assert_true("host install section", "host_goalflight_install" in payload)
    assert_true("codex host install probe", "codex" in payload["host_goalflight_install"])
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
    assert_true("SKILL under 20KB", len(skill.encode()) <= 20_000)
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
        "canonical invocation invokes codex exec with sandbox + bypass-approvals",
        "codex exec --sandbox read-only --dangerously-bypass-approvals-and-sandbox" in invocation,
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


def test_dispatched_worker_recovery_protocol_present() -> None:
    """Worker-reliability invariant: the controller-takeover recovery
    protocol must exist and reference the canonical verification gates.

    Memorialized in commit 2a662aa: when a dispatched worker blocks
    mid-chunk (typically on a permission elicitation or a wedged review),
    the controller takes over by reading status JSON, running gates,
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


def main() -> None:
    tests = [
        test_capacity_acquire_release_cooldown,
        test_capacity_prunes_review_terminal_states,
        test_ledger_record_finish_status,
        test_doctor_json_shape,
        test_doctor_target_project_readiness_split,
        test_doctor_package_repo_validates_plugin_manifest,
        test_doctor_cli_target_project_skips_package_plugin,
        test_instruction_split_contract,
        test_review_job_codex_no_final_is_inconclusive,
        test_runners_write_status_on_capacity_and_spawn_failure,
        test_chunk_review_canonical_invocation_has_stdin_redirect,
        test_dispatched_worker_recovery_protocol_present,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")


if __name__ == "__main__":
    main()
