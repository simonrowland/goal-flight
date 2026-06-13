#!/usr/bin/env python3
"""Tests for context-discipline hook, wrappers, and audit script."""

from __future__ import annotations

from support import skip_posix_on_native_windows

skip_posix_on_native_windows("context-discipline hooks are POSIX/Git-Bash-only")

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / "scripts/hooks/goalflight-context-discipline.sh"


def run_hook(
    payload: dict, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    hook_env = os.environ.copy()
    if env:
        hook_env.update(env)
    return subprocess.run(
        [str(HOOK), "--dry-run"],
        cwd=ROOT,
        env=hook_env,
        input=json.dumps(payload),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def decision(payload: dict, env: dict[str, str] | None = None) -> tuple[dict, str]:
    proc = run_hook(payload, env=env)
    assert proc.returncode == 0, proc
    return json.loads(proc.stdout), proc.stderr


def heredoc_command(prefix: str, marker: str, lines: int) -> str:
    body = "\n".join(f"line {idx}" for idx in range(lines))
    return f"{prefix} <<{marker}\n{body}\n{marker}\n"


def spawn_task_payload() -> dict:
    return {
        "tool_name": "mcp__ccd_session__spawn_task",
        "tool_input": {"description": "fixture"},
    }


def fresh_stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def isolated_hook_env(
    project_root: Path, state_dir: Path, extra: dict[str, str] | None = None
) -> dict[str, str]:
    state_dir.mkdir(parents=True, exist_ok=True)
    env = {
        "GOALFLIGHT_HOOK_PROJECT_ROOT": str(project_root),
        "GOALFLIGHT_STATE_DIR": str(state_dir),
    }
    if extra:
        env.update(extra)
    return env


def write_queue(project_root: Path, *, state: str, last_touched: str | None = None) -> Path:
    docs_private = project_root / "docs-private"
    docs_private.mkdir(parents=True, exist_ok=True)
    lines = [
        "---",
        "slug: hook-fixture",
        f"state: {state}",
    ]
    if last_touched:
        lines.append(f"last-touched: {last_touched}")
    lines.extend(["---", "", "# Hook Fixture", ""])
    path = docs_private / "goal-queue-hook-fixture.md"
    path.write_text("\n".join(lines))
    return path


def write_terminal_capacity_lease(state_dir: Path, project_root: Path) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": "goalflight.capacity.v1",
        "machine_id": "test",
        "leases": {
            "lease-terminal": {
                "lease_id": "lease-terminal",
                "agent": "codex",
                "dispatch_id": "dispatch-terminal",
                "project_root": str(project_root),
                "state": "released",
            }
        },
        "cooldowns": {},
    }
    (state_dir / "capacity.json").write_text(json.dumps(payload))


def test_hook_script_exists_and_is_executable() -> None:
    assert HOOK.is_file()
    assert os.access(HOOK, os.X_OK)


def test_hook_read_size_blocks_only_large_reads() -> None:
    blocked, _ = decision({
        "tool_name": "Read",
        "tool_input": {"file_path": str(ROOT / "SKILL.md")},
    })
    allowed, _ = decision({
        "tool_name": "Read",
        "tool_input": {"file_path": str(ROOT / "commands/resume.md")},
    })
    assert blocked["block"] is True
    assert "Read of file >5KB" in blocked["message"]
    assert allowed["block"] is False


def test_hook_heredoc_line_thresholds() -> None:
    blocked, _ = decision({
        "tool_name": "Bash",
        "tool_input": {"command": heredoc_command("cat", "EOF", 60)},
    })
    allowed, _ = decision({
        "tool_name": "Bash",
        "tool_input": {"command": heredoc_command("cat", "EOF", 30)},
    })
    assert blocked["block"] is True
    assert "heredoc body >50 lines" in blocked["message"]
    assert allowed["block"] is False


def test_hook_python_heredoc_blocks_at_30_lines() -> None:
    blocked, _ = decision({
        "tool_name": "Bash",
        "tool_input": {"command": heredoc_command("python3", "PYEOF", 30)},
    })
    assert blocked["block"] is True
    assert "python heredoc" in blocked["message"]


def test_hook_agent_without_ready_warns_not_blocks() -> None:
    result, stderr = decision({
        "tool_name": "Agent",
        "tool_input": {"prompt": "Inspect the repo and summarize."},
    })
    assert result["block"] is False
    assert "READY:" in stderr


def test_hook_spawn_task_blocks_during_active_goalflight_run(tmp_path: Path) -> None:
    project_root = tmp_path / "active-project"
    project_root.mkdir()
    write_queue(project_root, state="active", last_touched=fresh_stamp())

    result, _ = decision(
        spawn_task_payload(),
        env=isolated_hook_env(project_root, tmp_path / "active-state"),
    )

    assert result["block"] is True
    assert "spawn_task chips are disabled during an active goal-flight run" in result["message"]
    assert "docs-private/goal-queue-*.md via /goal-flight goal" in result["message"]


def test_hook_spawn_task_allows_when_no_active_goalflight_run(tmp_path: Path) -> None:
    no_queue_root = tmp_path / "no-queue"
    no_queue_root.mkdir()
    allowed, _ = decision(
        spawn_task_payload(),
        env=isolated_hook_env(no_queue_root, tmp_path / "no-queue-state"),
    )
    assert allowed["block"] is False

    stale_queue_root = tmp_path / "stale-queue"
    stale_queue_root.mkdir()
    write_queue(stale_queue_root, state="active", last_touched="2000-01-01T00:00:00Z")
    stale_allowed, _ = decision(
        spawn_task_payload(),
        env=isolated_hook_env(stale_queue_root, tmp_path / "stale-state"),
    )
    assert stale_allowed["block"] is False

    terminal_lease_root = tmp_path / "terminal-lease"
    terminal_lease_root.mkdir()
    terminal_state = tmp_path / "terminal-state"
    write_terminal_capacity_lease(terminal_state, terminal_lease_root)
    terminal_allowed, _ = decision(
        spawn_task_payload(),
        env=isolated_hook_env(terminal_lease_root, terminal_state),
    )
    assert terminal_allowed["block"] is False


def test_hook_spawn_task_override_allows_during_active_goalflight_run(tmp_path: Path) -> None:
    project_root = tmp_path / "override-project"
    project_root.mkdir()
    write_queue(project_root, state="active", last_touched=fresh_stamp())

    result, _ = decision(
        spawn_task_payload(),
        env=isolated_hook_env(
            project_root,
            tmp_path / "override-state",
            {"GOALFLIGHT_CHIP_OK": "1"},
        ),
    )

    assert result["block"] is False


def test_hook_spawn_task_status_check_error_allows(tmp_path: Path) -> None:
    project_root = tmp_path / "error-project"
    project_root.mkdir()
    write_queue(project_root, state="active", last_touched=fresh_stamp())
    bad_status = tmp_path / "bad_status.py"
    bad_status.write_text("import sys\nsys.exit(3)\n")

    result, _ = decision(
        spawn_task_payload(),
        env=isolated_hook_env(
            project_root,
            tmp_path / "error-state",
            {"GOALFLIGHT_SESSION_STATUS_SCRIPT": str(bad_status)},
        ),
    )

    assert result["block"] is False


def test_hook_config_routes_spawn_task_to_context_discipline_hook() -> None:
    config = json.loads((ROOT / "hooks/hooks.json").read_text())
    entries = config["hooks"].get("PreToolUse", [])
    routed_matchers = []
    for entry in entries:
        commands = [hook.get("command", "") for hook in entry.get("hooks", [])]
        if any("goalflight-context-discipline.sh" in command for command in commands):
            routed_matchers.append(entry.get("matcher", ""))
    assert any(
        "mcp__ccd_session__spawn_task" in matcher
        or matcher in {"", "*", ".*"}
        for matcher in routed_matchers
    )


def test_tier_two_wrappers_exist_and_are_executable() -> None:
    for rel in (
        "scripts/goalflight_recon.sh",
        "scripts/goalflight_dispatch.sh",
        "scripts/goalflight_commit.sh",
    ):
        path = ROOT / rel
        assert path.is_file(), rel
        assert os.access(path, os.X_OK), rel


def test_audit_script_fixture_json_shape() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            "scripts/goalflight_context_audit.py",
            "--session-log",
            "tests/fixtures/context_audit/session.jsonl",
        ],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert proc.returncode == 0, proc
    data = json.loads(proc.stdout)
    assert data["session_id"] == "fixture-session"
    assert data["since_last_audit_turns"] == 4
    assert data["bytes_read"] > 0
    assert data["bytes_bashed_in"] > 0
    assert data["agents_dispatched"] == 1
    assert isinstance(data["bash_to_agent_ratio"], float)
    assert isinstance(data["read_without_edit_fraction"], float)
    assert "warning" in data
