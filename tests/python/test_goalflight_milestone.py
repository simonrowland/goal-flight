#!/usr/bin/env python3
"""Tests for goalflight_milestone.py detector surfaces."""
from __future__ import annotations

import io
import json
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_milestone as M
import goalflight_status as S


def run(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return proc.stdout.strip()


def init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    run(repo, "init", "-b", "main")
    run(repo, "config", "user.email", "tests@example.invalid")
    run(repo, "config", "user.name", "Goal Flight Tests")
    (repo / "file.txt").write_text("base\n", encoding="utf-8")
    run(repo, "add", "file.txt")
    run(repo, "commit", "-m", "base")
    return repo


def commit_file(repo: Path, text: str, message: str) -> str:
    path = repo / "file.txt"
    path.write_text(path.read_text(encoding="utf-8") + text + "\n", encoding="utf-8")
    run(repo, "add", "file.txt")
    run(repo, "commit", "-m", message)
    return run(repo, "rev-parse", "HEAD")


def run_mark(repo: Path, state: Path, commit: str, verdict: str) -> str:
    out = io.StringIO()
    err = io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = M.main(
            [
                "mark",
                "--repo",
                str(repo),
                "--state-dir",
                str(state),
                "--commit",
                commit,
                "--verdict",
                verdict,
            ]
        )
    assert rc == 0, err.getvalue()
    return out.getvalue()


def write_queue(
    path: Path,
    *,
    cadence: int = 5,
    milestone_line: str = "",
    frontmatter_lines: list[str] | None = None,
) -> Path:
    frontmatter = frontmatter_lines or ["state: active", f"milestone_cadence: {cadence}"]
    path.write_text(
        "\n".join(
            [
                "---",
                *frontmatter,
                "---",
                "",
                "| Goal | Status | Commit |",
                "|------|--------|--------|",
                milestone_line,
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def test_scalar_and_nested_milestone_cadence_parse_to_configured_k(tmp_path: Path) -> None:
    assert M.cadence_from_config({"milestone": 3}) == 3
    assert M.cadence_from_config({"milestone": {"k": 3}}) == 3
    assert M.cadence_from_config({"milestone": {"cadence": 3}}) == 3

    queue = write_queue(
        tmp_path / "queue.md",
        frontmatter_lines=["state: active", "milestone: {cadence: 3}"],
    )

    assert M.cadence_for_queue(queue) == 3


def test_unparseable_milestone_cadence_warns_and_uses_default(tmp_path: Path) -> None:
    queue = write_queue(
        tmp_path / "queue.md",
        frontmatter_lines=["state: active", "milestone:", "  k: soon"],
    )

    cadence, warnings = M.cadence_details_for_queue(queue)

    assert cadence == M.DEFAULT_K
    assert warnings == ["milestone cadence config present but unparseable; using default 5"]


def test_unknown_nested_milestone_cadence_warns_and_uses_default(tmp_path: Path) -> None:
    assert M.cadence_from_config({"milestone": {"unknown_key": 7}}) is None
    assert M.cadence_from_config({"milestone": {"something_weird": {"k": 1}}}) is None
    assert M.cadence_from_config({"milestone": {"unknown_key": "", "k": 1}}) is None
    assert M.cadence_from_config({"milestone": {"unknown_key": "", "cadence": 1}}) is None
    assert M.cadence_from_config({"milestone": {"k": 3}}) == 3
    assert M.cadence_from_config({"milestone": {"cadence": 3}}) == 3

    queue = write_queue(
        tmp_path / "queue.md",
        frontmatter_lines=["state: active", "milestone:", "  unknown_key: 7"],
    )
    mixed_queue = write_queue(
        tmp_path / "mixed-queue.md",
        frontmatter_lines=["state: active", "milestone:", "  unknown_key:", "    k: 1"],
    )

    cadence, warnings = M.cadence_details_for_queue(queue)
    mixed_cadence, mixed_warnings = M.cadence_details_for_queue(mixed_queue)

    assert cadence == M.DEFAULT_K
    assert warnings == ["milestone cadence config present but unparseable; using default 5"]
    assert mixed_cadence == M.DEFAULT_K
    assert mixed_warnings == ["milestone cadence config present but unparseable; using default 5"]


def test_marker_round_trip(tmp_path: Path) -> None:
    state = tmp_path / "state"
    marker = M.write_marker(
        commit="abc1234",
        verdict="clean",
        root=state,
        now="2026-06-15T00:00:00Z",
    )
    assert marker == state / "milestone-marker.json"
    assert M.read_marker(state) == {
        "commit": "abc1234",
        "ts": "2026-06-15T00:00:00Z",
        "verdict": "clean",
    }


def test_non_clean_marker_round_trip_preserves_verdict(tmp_path: Path) -> None:
    state = tmp_path / "state"
    M.write_marker(
        commit="def5678",
        verdict="inconclusive_timeout",
        root=state,
        now="2026-06-15T00:00:00Z",
    )

    assert M.read_marker(state) == {
        "commit": "def5678",
        "ts": "2026-06-15T00:00:00Z",
        "verdict": "inconclusive_timeout",
    }


def test_malformed_marker_json_is_treated_as_no_marker(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    run(repo, "checkout", "-b", "feature")
    commit_file(repo, "one", "one")
    state = tmp_path / "state"
    state.mkdir()
    (state / M.MARKER_NAME).write_text("{not-json", encoding="utf-8")
    queue = write_queue(tmp_path / "queue.md", cadence=5)

    payload = M.check_status(repo=repo, project_root=repo, root=state, queue=queue)

    assert M.read_marker(state) is None
    assert payload["last_marker"] is None
    assert payload["arc_start"] == run(repo, "merge-base", "main", "HEAD")
    assert payload["commits_since"] == 1

    (state / M.MARKER_NAME).write_bytes(b"\xff\xfe{")
    assert M.read_marker(state) is None


def test_legacy_marker_without_verdict_still_anchors_as_clean(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    base = run(repo, "rev-parse", "HEAD")
    run(repo, "checkout", "-b", "feature")
    commit_file(repo, "one", "one")
    commit_file(repo, "two", "two")
    state = tmp_path / "state"
    state.mkdir()
    (state / M.MARKER_NAME).write_text(
        json.dumps({"commit": base, "ts": "2026-06-15T00:00:00Z"}) + "\n",
        encoding="utf-8",
    )
    queue = write_queue(tmp_path / "queue.md", cadence=2)

    payload = M.check_status(repo=repo, project_root=repo, root=state, queue=queue)

    assert payload["last_marker"]["verdict"] is None
    assert payload["last_clean_marker"] == {
        "commit": base,
        "ts": "2026-06-15T00:00:00Z",
        "verdict": None,
    }
    assert payload["commits_since"] == 2
    assert payload["due"] is True


def test_clean_mark_resets_cadence_anchor(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    run(repo, "checkout", "-b", "feature")
    commit_file(repo, "one", "one")
    clean_commit = commit_file(repo, "two", "two")
    commit_file(repo, "three", "three")
    state = tmp_path / "state"
    queue = write_queue(tmp_path / "queue.md", cadence=3)

    run_mark(repo, state, clean_commit, "clean")
    payload = M.check_status(repo=repo, project_root=repo, root=state, queue=queue)

    assert payload["last_marker"]["verdict"] == "clean"
    assert payload["last_clean_marker"]["commit"] == clean_commit
    assert payload["commits_since"] == 1
    assert payload["due"] is False


def test_converged_mark_resets_cadence_anchor(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    run(repo, "checkout", "-b", "feature")
    commit_file(repo, "one", "one")
    converged_commit = commit_file(repo, "two", "two")
    commit_file(repo, "three", "three")
    state = tmp_path / "state"
    queue = write_queue(tmp_path / "queue.md", cadence=3)

    run_mark(repo, state, converged_commit, "converged")
    payload = M.check_status(repo=repo, project_root=repo, root=state, queue=queue)

    assert payload["last_marker"]["verdict"] == "converged"
    assert payload["last_clean_marker"]["commit"] == converged_commit
    assert payload["last_clean_marker"]["verdict"] == "converged"
    assert payload["commits_since"] == 1
    assert payload["due"] is False


def test_non_clean_mark_does_not_reset_cadence_anchor(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    base = run(repo, "rev-parse", "HEAD")
    run(repo, "checkout", "-b", "feature")
    commit_file(repo, "one", "one")
    non_clean_commit = commit_file(repo, "two", "two")
    commit_file(repo, "three", "three")
    state = tmp_path / "state"
    queue = write_queue(tmp_path / "queue.md", cadence=3)

    run_mark(repo, state, base, "clean")
    run_mark(repo, state, non_clean_commit, "inconclusive_timeout")
    payload = M.check_status(repo=repo, project_root=repo, root=state, queue=queue)

    assert payload["last_marker"]["commit"] == non_clean_commit
    assert payload["last_marker"]["verdict"] == "inconclusive_timeout"
    assert payload["last_clean_marker"]["commit"] == base
    assert payload["commits_since"] == 3
    assert payload["due"] is True


def test_non_clean_mark_without_prior_clean_bootstraps_from_merge_base(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    run(repo, "checkout", "-b", "feature")
    non_clean_commit = commit_file(repo, "one", "one")
    state = tmp_path / "state"
    queue = write_queue(tmp_path / "queue.md", cadence=1)

    run_mark(repo, state, non_clean_commit, "inconclusive_timeout")
    payload = M.check_status(repo=repo, project_root=repo, root=state, queue=queue)

    assert payload["last_marker"]["verdict"] == "inconclusive_timeout"
    assert payload["last_clean_marker"] is None
    assert payload["arc_start"] == run(repo, "merge-base", "main", "HEAD")
    assert payload["commits_since"] == 1
    assert payload["due"] is True


def test_mixed_marker_sequence_anchors_latest_clean_mark(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    base = run(repo, "rev-parse", "HEAD")
    run(repo, "checkout", "-b", "feature")
    non_clean_commit = commit_file(repo, "one", "one")
    latest_clean = commit_file(repo, "two", "two")
    commit_file(repo, "three", "three")
    state = tmp_path / "state"
    queue = write_queue(tmp_path / "queue.md", cadence=2)

    run_mark(repo, state, base, "clean")
    run_mark(repo, state, non_clean_commit, "inconclusive_timeout")
    run_mark(repo, state, latest_clean, "clean")
    payload = M.check_status(repo=repo, project_root=repo, root=state, queue=queue)

    assert payload["last_marker"]["verdict"] == "clean"
    assert payload["last_clean_marker"]["commit"] == latest_clean
    assert payload["commits_since"] == 1
    assert payload["due"] is False


def test_mark_write_failure_is_clean_operator_error(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    state_file = tmp_path / "state-file"
    state_file.write_text("not a directory", encoding="utf-8")
    out = io.StringIO()
    err = io.StringIO()

    with redirect_stdout(out), redirect_stderr(err):
        rc = M.main(["mark", "--repo", str(repo), "--state-dir", str(state_file)])

    assert rc == 2
    assert out.getvalue() == ""
    assert err.getvalue().startswith("goalflight_milestone: mark failed:")
    assert err.getvalue().count("\n") == 1
    assert "Traceback" not in err.getvalue()


def test_commits_since_count_and_due_when_cadence_reached(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    base = run(repo, "rev-parse", "HEAD")
    run(repo, "checkout", "-b", "feature")
    commit_file(repo, "one", "one")
    commit_file(repo, "two", "two")
    commit_file(repo, "three", "three")
    state = tmp_path / "state"
    M.write_marker(commit=base, verdict="clean", root=state, now="2026-06-15T00:00:00Z")
    queue = write_queue(tmp_path / "queue.md", cadence=3)

    payload = M.check_status(repo=repo, project_root=repo, root=state, queue=queue)

    assert payload["commits_since"] == 3
    assert payload["K"] == 3
    assert payload["due"] is True
    assert payload["reason"] == "commit cadence reached"


def test_no_marker_bootstraps_from_merge_base(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    run(repo, "checkout", "-b", "feature")
    commit_file(repo, "one", "one")
    commit_file(repo, "two", "two")
    queue = write_queue(tmp_path / "queue.md", cadence=5)

    payload = M.check_status(repo=repo, project_root=repo, root=tmp_path / "state", queue=queue)

    assert payload["last_marker"] is None
    assert payload["arc_start"] == run(repo, "merge-base", "main", "HEAD")
    assert payload["commits_since"] == 2
    assert payload["due"] is False


def assert_unavailable_probe(payload: dict[str, object], reason_fragment: str) -> None:
    line = M.format_line(payload)
    encoded = json.loads(json.dumps(payload))

    assert "milestone: unavailable" in line
    assert "-> ok" not in line
    assert encoded["active_cadence"] is True
    assert encoded["due"] is None
    assert encoded["error"]
    assert reason_fragment in str(encoded["error"])


def test_no_merge_base_reports_milestone_unavailable(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    run(repo, "checkout", "--orphan", "feature")
    (repo / "orphan.txt").write_text("orphan\n", encoding="utf-8")
    run(repo, "add", ".")
    run(repo, "commit", "-m", "orphan")
    queue = write_queue(tmp_path / "queue.md", cadence=5)

    payload = M.check_status(repo=repo, project_root=repo, root=tmp_path / "state", queue=queue)

    assert_unavailable_probe(payload, "no merge-base main")


def test_unreachable_clean_marker_reports_milestone_unavailable(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    run(repo, "checkout", "-b", "feature")
    commit_file(repo, "one", "one")
    state = tmp_path / "state"
    M.write_marker(commit="deadbeef", verdict="clean", root=state, now="2026-06-15T00:00:00Z")
    queue = write_queue(tmp_path / "queue.md", cadence=5)

    payload = M.check_status(repo=repo, project_root=repo, root=state, queue=queue)

    assert_unavailable_probe(payload, "clean marker commit unreachable")


def test_non_git_repo_reports_milestone_unavailable(tmp_path: Path) -> None:
    repo = tmp_path / "not-git"
    repo.mkdir()
    queue = write_queue(tmp_path / "queue.md", cadence=5)

    payload = M.check_status(repo=repo, project_root=repo, root=tmp_path / "state", queue=queue)

    assert_unavailable_probe(payload, "not a git repository")


def test_shallow_history_count_failure_reports_milestone_unavailable(
    tmp_path: Path, monkeypatch
) -> None:
    repo = init_repo(tmp_path)
    run(repo, "checkout", "-b", "feature")
    commit_file(repo, "one", "one")
    queue = write_queue(tmp_path / "queue.md", cadence=5)

    def fail_commits_since(_repo: Path, _start_commit: str) -> int:
        raise RuntimeError("shallow history prevents rev-list")

    monkeypatch.setattr(M, "commits_since", fail_commits_since)
    payload = M.check_status(repo=repo, project_root=repo, root=tmp_path / "state", queue=queue)

    assert_unavailable_probe(payload, "shallow history")


def test_no_active_queue_still_counts_for_detector_check(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    run(repo, "checkout", "-b", "feature")
    commit_file(repo, "one", "one")

    payload = M.check_status(repo=repo, project_root=repo, root=tmp_path / "state")

    assert payload["active_cadence"] is True
    assert payload["K"] == M.DEFAULT_K
    assert payload["commits_since"] == 1
    assert payload["due"] is False


def test_milestone_tag_trigger_when_landed_since_marker(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    base = run(repo, "rev-parse", "HEAD")
    run(repo, "checkout", "-b", "feature")
    landed = commit_file(repo, "one", "one")
    state = tmp_path / "state"
    M.write_marker(commit=base, verdict="clean", root=state, now="2026-06-15T00:00:00Z")
    queue = write_queue(
        tmp_path / "queue.md",
        cadence=5,
        milestone_line=f"| 1. `sweep-me` [milestone] | DONE | {landed[:8]} |",
    )

    payload = M.check_status(repo=repo, project_root=repo, root=state, queue=queue)

    assert payload["commits_since"] == 1
    assert payload["due"] is True
    assert payload["reason"] == "milestone tag landed since marker"
    assert payload["tagged_milestone"]["goal"].startswith("1.")


def test_no_marker_milestone_tag_at_or_before_merge_base_not_due(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    before_base = run(repo, "rev-parse", "HEAD")
    merge_base_commit = commit_file(repo, "main", "main advance")
    run(repo, "checkout", "-b", "feature")
    commit_file(repo, "one", "one")
    queue = write_queue(
        tmp_path / "queue.md",
        cadence=5,
        milestone_line=(
            f"| 1. `before` [milestone] | DONE | {before_base[:8]} |\n"
            f"| 2. `at-base` [milestone] | DONE | {merge_base_commit[:8]} |"
        ),
    )

    payload = M.check_status(repo=repo, project_root=repo, root=tmp_path / "state", queue=queue)

    assert payload["last_marker"] is None
    assert payload["arc_start"] == merge_base_commit
    assert payload["commits_since"] == 1
    assert payload["due"] is False
    assert payload["tagged_milestone"] is None


def test_no_marker_milestone_tag_after_merge_base_is_due(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    run(repo, "checkout", "-b", "feature")
    landed = commit_file(repo, "one", "one")
    queue = write_queue(
        tmp_path / "queue.md",
        cadence=5,
        milestone_line=f"| 1. `after-base` [milestone] | DONE | {landed[:8]} |",
    )

    payload = M.check_status(repo=repo, project_root=repo, root=tmp_path / "state", queue=queue)

    assert payload["last_marker"] is None
    assert payload["commits_since"] == 1
    assert payload["due"] is True
    assert payload["reason"] == "milestone tag landed"
    assert payload["tagged_milestone"]["goal"].startswith("1.")


def test_milestone_tag_with_blank_commit_does_not_repeat_after_marker(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    base = run(repo, "rev-parse", "HEAD")
    run(repo, "checkout", "-b", "feature")
    commit_file(repo, "one", "one")
    state = tmp_path / "state"
    M.write_marker(commit=base, verdict="clean", root=state, now="2026-06-15T00:00:00Z")
    queue = write_queue(
        tmp_path / "queue.md",
        cadence=5,
        milestone_line="| 1. `swept` [milestone] | DONE | - |",
    )

    payload = M.check_status(repo=repo, project_root=repo, root=state, queue=queue)

    assert payload["commits_since"] == 1
    assert payload["due"] is False
    assert payload["reason"] == "ok"


def test_milestone_tag_with_blank_commit_does_not_trigger_without_marker(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    run(repo, "checkout", "-b", "feature")
    commit_file(repo, "one", "one")
    queue = write_queue(
        tmp_path / "queue.md",
        cadence=5,
        milestone_line="| 1. `blank` [milestone] | DONE | - |",
    )

    payload = M.check_status(repo=repo, project_root=repo, root=tmp_path / "state", queue=queue)

    assert payload["commits_since"] == 1
    assert payload["due"] is False
    assert payload["reason"] == "ok"


def test_milestone_tag_with_unresolvable_commit_does_not_trigger_without_marker(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    run(repo, "checkout", "-b", "feature")
    commit_file(repo, "one", "one")
    queue = write_queue(
        tmp_path / "queue.md",
        cadence=5,
        milestone_line="| 1. `bad-commit` [milestone] | DONE | deadbee |",
    )

    payload = M.check_status(repo=repo, project_root=repo, root=tmp_path / "state", queue=queue)

    assert payload["commits_since"] == 1
    assert payload["due"] is False
    assert payload["reason"] == "ok"


def test_status_surfaces_milestone_text_and_json() -> None:
    payload = {
        "schema": "goalflight.status.aggregate.v1",
        "capacity": {"operating_cap": 16},
        "capacity_state": {"leases": {}, "cooldowns": {}},
        "dispatch": {"records": [], "surplus_processes": []},
    }
    milestone = {
        "schema": M.SCHEMA,
        "active_cadence": True,
        "commits_since": 7,
        "K": 5,
        "last_marker": {"commit": "a1b2c3d4", "ts": "2026-06-15T00:00:00Z", "verdict": "clean"},
        "due": True,
        "reason": "commit cadence reached",
    }
    orig_payload = S.status_payload
    orig_root = S.this_project_root
    orig_check = S.goalflight_milestone.check_status
    S.status_payload = lambda: payload
    S.this_project_root = lambda: "/repo/A"
    S.goalflight_milestone.check_status = lambda **_kwargs: milestone
    try:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = S.main(["--limit", "2"])
        assert rc == 0
        assert "milestone: 7/5 since last sweep @ a1b2c3d -> DUE" in buf.getvalue()

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = S.main(["--json"])
        data = json.loads(buf.getvalue())
        assert rc == 0
        assert data["milestone"]["due"] is True
        assert data["milestone"]["commits_since"] == 7
    finally:
        S.status_payload = orig_payload
        S.this_project_root = orig_root
        S.goalflight_milestone.check_status = orig_check


if __name__ == "__main__":
    try:
        import pytest
    except ModuleNotFoundError:
        fallback = Path("/opt/homebrew/bin/python3")
        if fallback.exists() and fallback.resolve() != Path(sys.executable).resolve():
            raise SystemExit(subprocess.run([str(fallback), "-m", "pytest", __file__]).returncode)
        raise

    raise SystemExit(pytest.main([__file__]))


def test_status_surfaces_milestone_probe_failure_as_unavailable() -> None:
    payload = {
        "schema": "goalflight.status.aggregate.v1",
        "capacity": {"operating_cap": 16},
        "capacity_state": {"leases": {}, "cooldowns": {}},
        "dispatch": {"records": [], "surplus_processes": []},
    }
    orig_payload = S.status_payload
    orig_root = S.this_project_root
    orig_check = S.goalflight_milestone.check_status
    S.status_payload = lambda: payload
    S.this_project_root = lambda: "/repo/A"
    S.goalflight_milestone.check_status = lambda **_kwargs: (_ for _ in ()).throw(
        RuntimeError("boom\nsecond line")
    )
    try:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = S.main(["--limit", "2"])
        assert rc == 0
        assert "milestone: unavailable (RuntimeError: boom second line)" in buf.getvalue()

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = S.main(["--json"])
        data = json.loads(buf.getvalue())
        assert rc == 0
        assert data["milestone"]["active_cadence"] is False
        assert data["milestone"]["reason"] == "milestone unavailable"
        assert data["milestone"]["error"] == "RuntimeError: boom second line"
    finally:
        S.status_payload = orig_payload
        S.this_project_root = orig_root
        S.goalflight_milestone.check_status = orig_check


def test_status_predicate_modes_skip_milestone_probe() -> None:
    payload = {
        "schema": "goalflight.status.aggregate.v1",
        "capacity": {"operating_cap": 16},
        "capacity_state": {"leases": {}, "cooldowns": {}},
        "dispatch": {
            "records": [
                {
                    "dispatch_id": "done1",
                    "project_root": "/repo/A",
                    "classification": "complete",
                    "agent": "codex",
                }
            ],
            "surplus_processes": [],
        },
    }
    orig_payload = S.status_payload
    orig_root = S.this_project_root
    orig_check = S.goalflight_milestone.check_status
    S.status_payload = lambda: payload
    S.this_project_root = lambda: "/repo/A"
    S.goalflight_milestone.check_status = lambda **_kwargs: (_ for _ in ()).throw(
        AssertionError("milestone probe should not run")
    )
    try:
        assert S.main(["--done", "done1"]) == 0
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = S.main(["--dispatch", "done1"])
        assert rc == 0
        assert "done1" in buf.getvalue()
        assert "milestone:" not in buf.getvalue()
    finally:
        S.status_payload = orig_payload
        S.this_project_root = orig_root
        S.goalflight_milestone.check_status = orig_check
