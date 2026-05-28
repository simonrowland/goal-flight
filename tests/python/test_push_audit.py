#!/usr/bin/env python3
"""Hermetic tests for push provenance audit helper."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "goalflight_push_audit.py"


def assert_true(label: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(label)


def run(cmd: list[str], *, cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=check,
    )


def git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run(["git", *args], cwd=cwd, check=check)


def configure_user(repo: Path) -> None:
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Test User")


def make_commit(repo: Path, name: str, body: str) -> str:
    target = repo / name
    target.write_text(body, encoding="utf-8")
    git(repo, "add", name)
    git(repo, "commit", "-m", f"commit {name}")
    return git(repo, "rev-parse", "HEAD").stdout.strip()


class Fixture:
    def __init__(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.origin = self.root / "origin.git"
        self.clone = self.root / "clone"

        git(self.root, "init", "--bare", "--initial-branch=main", str(self.origin))
        git(self.root, "clone", str(self.origin), str(self.clone))
        configure_user(self.clone)

    def cleanup(self) -> None:
        self._td.cleanup()


def run_audit(repo: Path, *extra: str) -> tuple[int, dict]:
    proc = run([sys.executable, str(SCRIPT), "--json", *extra], cwd=repo, check=False)
    assert_true(f"audit emitted json stderr={proc.stderr[:200]}", bool(proc.stdout.strip()))
    return proc.returncode, json.loads(proc.stdout)


def test_clean_push_is_aligned() -> None:
    fixture = Fixture()
    try:
        sha = make_commit(fixture.clone, "clean.txt", "clean\n")
        git(fixture.clone, "push", "-u", "origin", "main")

        rc, payload = run_audit(fixture.clone)

        assert_true("clean audit exits zero", rc == 0)
        assert_true("clean audit aligned", payload["aligned"] is True)
        assert_true("clean remote sha recorded", payload["remote_sha"] == sha)
        assert_true("clean has no unauthorized advances", payload["unauthorized_advances"] == [])
        assert_true("clean has no out-of-band commits", payload["out_of_band_commits"] == [])
    finally:
        fixture.cleanup()


def test_remote_update_ref_without_local_push_is_flagged() -> None:
    fixture = Fixture()
    try:
        make_commit(fixture.clone, "base.txt", "base\n")
        git(fixture.clone, "push", "-u", "origin", "main")

        tree = git(fixture.origin, "show", "-s", "--format=%T", "refs/heads/main").stdout.strip()
        new_sha = git(
            fixture.origin,
            "commit-tree",
            tree,
            "-p",
            "refs/heads/main",
            "-m",
            "remote direct update",
        ).stdout.strip()
        git(fixture.origin, "update-ref", "refs/heads/main", new_sha)

        rc, payload = run_audit(fixture.clone)

        assert_true("unauthorized audit exits nonzero", rc == 2)
        assert_true("unauthorized audit not aligned", payload["aligned"] is False)
        shas = [item["sha"] for item in payload["unauthorized_advances"]]
        assert_true("unauthorized sha reported", new_sha in shas)
        assert_true("warning names missing push reflog", "without local push reflog entry" in payload["warning"])
    finally:
        fixture.cleanup()


def test_local_update_ref_commit_without_commit_reflog_is_flagged() -> None:
    fixture = Fixture()
    try:
        make_commit(fixture.clone, "base.txt", "base\n")
        git(fixture.clone, "push", "-u", "origin", "main")

        tree = git(fixture.clone, "rev-parse", "HEAD^{tree}").stdout.strip()
        parent = git(fixture.clone, "rev-parse", "HEAD").stdout.strip()
        stamp = int(time.time())
        commit_payload = (
            f"tree {tree}\n"
            f"parent {parent}\n"
            f"author Test User <test@example.invalid> {stamp} +0000\n"
            f"committer Test User <test@example.invalid> {stamp} +0000\n"
            "\n"
            "manual local object\n"
        )
        object_file = fixture.root / "manual.commit"
        object_file.write_text(commit_payload, encoding="utf-8")
        new_sha = git(fixture.clone, "hash-object", "-t", "commit", "-w", str(object_file)).stdout.strip()
        git(fixture.clone, "update-ref", "refs/heads/main", new_sha)

        rc, payload = run_audit(fixture.clone, "--no-network")

        assert_true("out-of-band audit exits nonzero", rc == 2)
        assert_true("out-of-band audit not aligned", payload["aligned"] is False)
        shas = [item["sha"] for item in payload["out_of_band_commits"]]
        assert_true("out-of-band sha reported", new_sha in shas)
    finally:
        fixture.cleanup()


def main() -> None:
    test_clean_push_is_aligned()
    test_remote_update_ref_without_local_push_is_flagged()
    test_local_update_ref_commit_without_commit_reflog_is_flagged()


if __name__ == "__main__":
    main()
