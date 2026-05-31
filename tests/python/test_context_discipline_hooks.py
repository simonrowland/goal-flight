#!/usr/bin/env python3
"""Tests for context-discipline hook, wrappers, and audit script."""

from __future__ import annotations

from support import skip_posix_on_native_windows

skip_posix_on_native_windows("context-discipline hooks are POSIX/Git-Bash-only")

import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / "scripts/hooks/goalflight-context-discipline.sh"


def run_hook(payload: dict) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(HOOK), "--dry-run"],
        cwd=ROOT,
        input=json.dumps(payload),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def decision(payload: dict) -> tuple[dict, str]:
    proc = run_hook(payload)
    assert proc.returncode == 0, proc
    return json.loads(proc.stdout), proc.stderr


def heredoc_command(prefix: str, marker: str, lines: int) -> str:
    body = "\n".join(f"line {idx}" for idx in range(lines))
    return f"{prefix} <<{marker}\n{body}\n{marker}\n"


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
