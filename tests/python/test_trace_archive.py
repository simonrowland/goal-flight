#!/usr/bin/env python3
"""Selective dispatch-trace archive: keep marked runs, cap tails, never git-add."""

from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import goalflight_output_redact as redact  # noqa: E402
import goalflight_trace_archive as archive  # noqa: E402

# Obviously fake. Matches public shapes; must never be a live credential.
_FAKE_XAI = "xai-" + "a" * 24
_FAKE_OPENAI = "sk-" + "b" * 24
_FAKE_GITHUB = "ghp_" + "c" * 24
_FAKE_JWT = "eyJhbGciOiJub25lIn0.eyJ0ZXN0IjoiZmFrZS1wYXlsb2FkIn0.fakesignatureonly"
_FAKE_BEARER = "Authorization: Bearer " + _FAKE_JWT
_FAKE_B64 = "A" * 80


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
    manifest = json.loads((traces[0] / "MANIFEST.json").read_text(encoding="utf-8"))
    assert "never git-adds" in manifest["git"]
    assert "unreviewed worker output" in manifest["git"]
    assert "steer mailbox" in manifest["drop_list"]
    assert "historical /tmp backlog (unless --source-dir --apply)" in manifest["drop_list"]


def test_archive_redacts_credential_shaped_text_and_reports_count(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    source = tmp_path / "dispatch"
    source.mkdir()
    dispatch_id = "redact-tail"
    body = "\n".join(
        [
            "working",
            _FAKE_BEARER,
            _FAKE_XAI,
            _FAKE_OPENAI,
            _FAKE_GITHUB,
            '{"token": "not-a-short-label"}',
            _FAKE_B64,
            "COMPLETE: redact-tail — done",
            "",
        ]
    )
    (source / f"{dispatch_id}.tail").write_text(body, encoding="utf-8")
    _status(source / f"{dispatch_id}.status.json", dispatch_id)
    result = archive.archive_finished_dispatch(
        {
            "dispatch_id": dispatch_id,
            "project_root": str(project),
            "stdout_path": str(source / f"{dispatch_id}.tail"),
            "status_path": str(source / f"{dispatch_id}.status.json"),
            "state": "complete",
            "worker_pid": 11,
        },
        apply=True,
        project_root=project,
    )
    assert result["keep"] is True
    assert result["redactions"] >= 1
    stored = Path(result["dest"], "tail.log").read_text(encoding="utf-8")
    leaked = any(
        token in stored
        for token in (_FAKE_XAI, _FAKE_OPENAI, _FAKE_GITHUB, _FAKE_JWT, _FAKE_B64)
    )
    assert leaked is False
    assert "redaction(s) applied" in stored
    assert "this file is not verbatim" in stored
    assert "redacted" in stored
    kinds = result["redaction_kinds"]
    assert "xai api key" in kinds or "openai-style key" in kinds or "github token" in kinds
    manifest = json.loads(Path(result["dest"], "MANIFEST.json").read_text(encoding="utf-8"))
    assert manifest["redactions"] == result["redactions"]
    assert "unreviewed worker output" in manifest["git"]


def test_archive_redactor_names_the_removed_shape() -> None:
    text, count, kinds = redact.redact_archive_text(
        f"pre {_FAKE_XAI} mid {_FAKE_GITHUB} post"
    )
    assert count >= 2
    assert "xai api key" in kinds
    assert "github token" in kinds
    assert "redacted xai api key" in text
    assert "redacted github token" in text
    leaked = _FAKE_XAI in text or _FAKE_GITHUB in text
    assert leaked is False
    short = redact.redact_archive_text("account xai-0 stays")
    assert short[0] == "account xai-0 stays"
    assert short[1] == 0


def test_archive_refuses_git_add_and_source_has_no_git_add_argv() -> None:
    with pytest.raises(RuntimeError, match="never git-adds"):
        archive.refuse_git_add(Path("/tmp/traces/example"))
    assert archive.git_add_is_forbidden(["git", "add", "docs-private/traces"])
    assert archive.git_add_is_forbidden(["git", "-C", "/tmp/repo", "add", "tail.log"])
    assert not archive.git_add_is_forbidden(["git", "status"])
    source = Path(archive.__file__).read_text(encoding="utf-8")
    for line in source.splitlines():
        code = line.split("#", 1)[0]
        if re.search(r"""['\"]git['\"].*['\"]add['\"]""", code):
            raise AssertionError("trace archive source must not construct git add")
        if "subprocess" in code and "git" in code and "add" in code:
            raise AssertionError("trace archive source must not subprocess git add")


def test_drop_list_is_visible_in_cli_help() -> None:
    help_text = archive.build_parser().format_help()
    for needle in (
        "unreviewed",
        "Never git-adds",
        "steer mailboxes",
        "watcher logs",
        "caffeinate logs",
        "pidfiles",
        "prompt copies",
        "historical /tmp backlog",
    ):
        assert needle in help_text, needle
    module_doc = archive.__doc__ or ""
    for needle in (
        "steer mailboxes",
        "watcher logs",
        "caffeinate logs",
        "pidfiles",
        "prompt copies",
        "7.1 GB",
        "--source-dir",
        "unreviewed",
    ):
        assert needle in module_doc, needle
