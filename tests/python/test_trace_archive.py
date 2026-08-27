#!/usr/bin/env python3
"""Selective dispatch-trace archive: keep marked runs, cap tails, never git-add."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import goalflight_trace_archive as archive  # noqa: E402


def _status(path: Path, dispatch_id: str, **fields: object) -> None:
    payload = {
        "dispatch_id": dispatch_id,
        "state": "complete",
        "worker_pid": 4242,
        **fields,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_marker_run_is_archived_and_noise_is_dropped(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    source = tmp_path / "dispatch"
    source.mkdir()
    keep_id = "keep-complete"
    skip_id = "skip-capacity"
    (source / f"{keep_id}.tail").write_text(
        "working\nCOMPLETE: keep-complete — done\n", encoding="utf-8"
    )
    _status(source / f"{keep_id}.status.json", keep_id, state="complete")
    (source / f"{skip_id}.tail").write_text("waiting for capacity\n", encoding="utf-8")
    _status(
        source / f"{skip_id}.status.json",
        skip_id,
        state="blocked_capacity",
        worker_pid=None,
    )
    (source / f"{keep_id}.steer.jsonl").write_text("secret steer\n", encoding="utf-8")

    rc = archive.main(
        [
            "--project-root",
            str(project),
            "--source-dir",
            str(source),
            "--apply",
            "--json",
        ]
    )
    assert rc == 0
    dest_root = project / "docs-private" / "traces"
    kept_dirs = list(dest_root.glob(f"*/{keep_id}"))
    assert len(kept_dirs) == 1, list(dest_root.rglob("*"))
    dest = kept_dirs[0]
    tail = (dest / "tail.log").read_text(encoding="utf-8")
    assert "COMPLETE: keep-complete" in tail
    manifest = json.loads((dest / "MANIFEST.json").read_text(encoding="utf-8"))
    assert "steer mailbox" in manifest["dropped"]
    assert "never git-adds" in manifest["git"]
    assert not list(dest_root.glob(f"*/{skip_id}"))
    assert not (dest / "steer.jsonl").exists()


def test_oversized_tail_is_capped_and_dropped_bytes_are_named(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    source = tmp_path / "dispatch"
    source.mkdir()
    dispatch_id = "fat-tail"
    body = b"A" * (archive.HEAD_BYTES + archive.TAIL_BYTES + 4096)
    tail = b"start\n" + body + b"\nCOMPLETE: fat-tail -- done\n"
    (source / f"{dispatch_id}.tail").write_bytes(tail)
    _status(source / f"{dispatch_id}.status.json", dispatch_id)
    result = archive.archive_finished_dispatch(
        {
            "dispatch_id": dispatch_id,
            "project_root": str(project),
            "stdout_path": str(source / f"{dispatch_id}.tail"),
            "status_path": str(source / f"{dispatch_id}.status.json"),
            "state": "complete",
            "worker_pid": 7,
        },
        apply=True,
        project_root=project,
    )
    assert result["keep"] is True
    expected_dropped = len(tail) - archive.HEAD_BYTES - archive.TAIL_BYTES
    assert result["dropped_bytes"] == expected_dropped
    assert expected_dropped > 4000
    dest = Path(result["dest"])
    stored = (dest / "tail.log").read_bytes()
    assert b"COMPLETE: fat-tail -- done" in stored
    assert f"dropped {expected_dropped} bytes".encode("ascii") in stored
    assert len(stored) < len(tail)
    manifest = json.loads((dest / "MANIFEST.json").read_text(encoding="utf-8"))
    assert "tail middle bytes" in manifest["dropped"]


def test_archive_does_not_git_add(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=project, check=True, capture_output=True)
    source = tmp_path / "dispatch"
    source.mkdir()
    dispatch_id = "no-git"
    (source / f"{dispatch_id}.tail").write_text(
        "COMPLETE: no-git — done\n", encoding="utf-8"
    )
    _status(source / f"{dispatch_id}.status.json", dispatch_id)
    archive.archive_finished_dispatch(
        {
            "dispatch_id": dispatch_id,
            "project_root": str(project),
            "stdout_path": str(source / f"{dispatch_id}.tail"),
            "status_path": str(source / f"{dispatch_id}.status.json"),
            "state": "complete",
            "worker_pid": 9,
        },
        apply=True,
        project_root=project,
    )
    cached = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=project,
        check=True,
        capture_output=True,
        text=True,
    )
    assert cached.stdout.strip() == "", cached.stdout
    traces = list((project / "docs-private" / "traces").glob("*/no-git"))
    assert traces, "archive should write under docs-private/traces"
    assert (traces[0] / "tail.log").is_file()
