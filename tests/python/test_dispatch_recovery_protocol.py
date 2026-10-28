"""The documented salvage procedure must validate recorded worktree paths."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PROTOCOL = ROOT / "protocols" / "dispatch-resume.md"


def _recovery_script() -> str:
    text = PROTOCOL.read_text(encoding="utf-8")
    blocks = re.findall(r"```bash\n(.*?)\n```", text, re.DOTALL)
    block = next(
        (candidate for candidate in blocks if 'head=$(git -C "$worktree" ' in candidate),
        None,
    )
    assert block is not None
    lines = block.splitlines()
    worktree_assignment = next(
        index for index, line in enumerate(lines) if line == "worktree=<worktree>"
    )
    git_head = next(
        index
        for index, line in enumerate(lines)
        if line.startswith('head=$(git -C "$worktree" ')
    )
    return "\n".join(
        [
            'worktree="${WORKTREE_PATH:-}"',
            *lines[worktree_assignment + 1 : git_head + 1],
            'printf "%s\\n" "$head"',
        ]
    )


def _git_repo(path: Path) -> None:
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "user.email=test@example.com",
            "-c",
            "user.name=test",
            "commit",
            "--allow-empty",
            "-q",
            "-m",
            "base",
        ],
        check=True,
    )


def _run_protocol(
    script: str, cwd: Path, worktree: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", script],
        cwd=cwd,
        env={**os.environ, "WORKTREE_PATH": worktree},
        capture_output=True,
        text=True,
        check=False,
    )


def test_empty_recorded_worktree_path_stops_before_git_uses_current_directory(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "main-checkout"
    _git_repo(repo)
    result = _run_protocol(_recovery_script(), repo, "")

    assert result.returncode != 0, (
        "an empty worktree_path must fail before git -C can inspect the current checkout"
    )
    assert "worktree_path" in result.stderr, result.stderr


def test_relative_recorded_worktree_path_is_rejected(tmp_path: Path) -> None:
    repo = tmp_path / "main-checkout"
    _git_repo(repo)

    result = _run_protocol(_recovery_script(), repo, ".")

    assert result.returncode != 0, result.stdout
    assert "worktree_path is not absolute" in result.stderr, result.stderr


def test_non_directory_recorded_worktree_path_is_rejected(tmp_path: Path) -> None:
    repo = tmp_path / "main-checkout"
    _git_repo(repo)
    not_a_directory = tmp_path / "not-a-directory"
    not_a_directory.write_text("not a directory", encoding="utf-8")

    result = _run_protocol(_recovery_script(), repo, str(not_a_directory))

    assert result.returncode != 0, result.stdout
    assert "not an existing directory" in result.stderr, result.stderr


def test_recovery_protocol_explains_prelaunch_failure_discriminator() -> None:
    text = PROTOCOL.read_text(encoding="utf-8")
    assert "prelaunch_failure" in text
