"""Focused tests for the bounded worker search tool."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import goalflight_search


SCRIPT = ROOT / "scripts" / "goalflight_search.py"


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=check,
        text=True,
        encoding="utf-8",
        capture_output=True,
    )


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _new_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "--initial-branch=main")
    _git(repo, "config", "user.name", "Search Test")
    _git(repo, "config", "user.email", "search-test@example.invalid")
    return repo


def _run_search(
    repo: Path,
    *args: str,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=repo,
        env=env,
        text=True,
        encoding="utf-8",
        capture_output=True,
    )


def _fixture_repo(tmp_path: Path) -> tuple[Path, str, str]:
    repo = _new_repo(tmp_path)
    files = {
        "core/shared.py": "needle shared\n",
        "core/other.py": "no match\n",
        "build/generated.py": "needle generated\n",
        "vendor/copied.py": "needle vendor\n",
        "data/fixture.txt": "needle data\n",
        "node_modules/package.js": "needle package\n",
        "core/binary.bin": b"prefix\0needle\n",
    }
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
    first = _commit(repo, "first tree")

    (repo / "core/second.py").write_text("needle second\n", encoding="utf-8")
    second = _commit(repo, "second tree")

    for ref in (
        "refs/keep/shared-a",
        "refs/keep/shared-b",
        "refs/remotes/origin/shared",
    ):
        _git(repo, "update-ref", ref, first)
    _git(repo, "update-ref", "refs/heads/branch-second", second)
    return repo, first, second


def _json_result(result: subprocess.CompletedProcess[str]) -> dict:
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _tree_blob_ids(repo: Path, ref: str, path: str) -> set[str]:
    result = _git(repo, "ls-tree", "-r", "-z", ref, "--", path)
    blobs: set[str] = set()
    for record in result.stdout.encode().split(b"\0"):
        if not record:
            continue
        metadata, _ = record.split(b"\t", 1)
        fields = metadata.split()
        if len(fields) == 3 and fields[1] == b"blob":
            blobs.add(fields[2].decode())
    return blobs


def _naive_matching_blobs(repo: Path, refs: list[str], path: str) -> set[str]:
    """Model the old one-ref-at-a-time grep on the small fixture."""

    matching: set[str] = set()
    for ref in refs:
        grep = _git(repo, "grep", "-l", "-E", "needle", ref, "--", path, check=False)
        if grep.returncode != 0:
            continue
        for matched_path in grep.stdout.splitlines():
            matched_path = matched_path.split(":", 1)[-1]
            entries = _git(repo, "ls-tree", "-r", "-z", ref, "--", matched_path)
            for record in entries.stdout.encode().split(b"\0"):
                if not record:
                    continue
                metadata, _ = record.split(b"\t", 1)
                fields = metadata.split()
                if len(fields) == 3 and fields[1] == b"blob":
                    matching.add(fields[2].decode())
    return matching


def test_refs_match_unique_blobs_and_preserve_ref_counts(tmp_path: Path) -> None:
    repo, first, second = _fixture_repo(tmp_path)
    result = _run_search(
        repo,
        "needle",
        "--refs",
        "refs/keep",
        "refs/remotes",
        "refs/heads",
        "--json",
        "--",
        "core",
    )
    document = _json_result(result)
    hits = {item["blob"]: item for item in document["results"]}

    refs = [
        "refs/keep/shared-a",
        "refs/keep/shared-b",
        "refs/remotes/origin/shared",
        "refs/heads/branch-second",
    ]
    binary_blob = _tree_blob_ids(repo, first, "core/binary.bin")
    assert len(binary_blob) == 1
    assert set(hits) == _naive_matching_blobs(repo, refs, "core") - binary_blob
    shared_blob = next(blob for blob, item in hits.items() if "core/shared.py" in item["paths"])
    assert hits[shared_blob]["ref_count"] == 5
    assert set(hits[shared_blob]["refs"]) == {
        "refs/keep/shared-a",
        "refs/keep/shared-b",
        "refs/remotes/origin/shared",
        "refs/heads/main",
        "refs/heads/branch-second",
    }
    assert document["stats"]["trees"] == 2


def test_default_bulk_directories_are_skipped_but_named_bulk_path_is_allowed(
    tmp_path: Path,
) -> None:
    repo, _, _ = _fixture_repo(tmp_path)
    default = _json_result(
        _run_search(
            repo,
            "needle",
            "--refs",
            "refs/keep",
            "--json",
        )
    )
    assert all(not path.startswith("build/") for item in default["results"] for path in item["paths"])
    assert default["stats"]["blobs_skipped_large"] == 0

    named = _json_result(
        _run_search(
            repo,
            "needle",
            "--refs",
            "refs/keep",
            "--json",
            "--",
            "build",
        )
    )
    assert any(path == "build/generated.py" for item in named["results"] for path in item["paths"])


def test_ref_results_honor_max_results_and_report_truncation(tmp_path: Path) -> None:
    repo, _, _ = _fixture_repo(tmp_path)
    document = _json_result(
        _run_search(
            repo,
            "needle",
            "--refs",
            "refs/keep",
            "refs/heads",
            "--max-results",
            "1",
            "--json",
            "--",
            "core",
        )
    )
    assert len(document["results"]) == 1
    assert document["truncated"] is True
    assert "max-results 1" in document["truncation_reason"]


def test_ref_timeout_preserves_matches_found_before_later_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _new_repo(tmp_path)
    files = {
        "core/match-a.py": "needle a\n",
        "core/match-b.py": "needle b\n",
        "core/no-a.py": "other a\n",
        "core/no-b.py": "other b\n",
    }
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    commit = _commit(repo, "timeout fixture")
    _git(repo, "update-ref", "refs/keep/timeout", commit)

    real_match_many = goalflight_search.BlobBatch.match_many
    calls = 0
    first_batch_matches: set[str] = set()

    def match_many(self, blobs, matcher):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise goalflight_search.SearchTimeout("forced timeout after first batch")
        outcomes = real_match_many(self, blobs, matcher)
        first_batch_matches.update(
            blob for blob, (matched, _reason) in zip(blobs, outcomes) if matched
        )
        return outcomes

    monkeypatch.setattr(goalflight_search.BlobBatch, "match_many", match_many)
    document = goalflight_search._search_refs(
        repo,
        "needle",
        ("refs/keep",),
        ("core",),
        True,
        False,
        2,
        goalflight_search.SearchBudget(120),
    )

    assert calls == 2
    assert first_batch_matches
    assert document["truncated"] is True
    assert {item["blob"] for item in document["results"]} == first_batch_matches


def test_each_distinct_blob_is_requested_once(tmp_path: Path) -> None:
    repo, first, second = _fixture_repo(tmp_path)
    real_git = shutil.which("git")
    assert real_git
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_path = tmp_path / "cat-file-requests.log"
    wrapper = bin_dir / "git"
    wrapper.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = cat-file ] && [ \"$2\" = --batch ]; then\n"
        "  tee -a \"$GOALFLIGHT_SEARCH_CAT_FILE\" | \"$GOALFLIGHT_SEARCH_REAL_GIT\" \"$@\"\n"
        "  exit $?\n"
        "fi\n"
        "exec \"$GOALFLIGHT_SEARCH_REAL_GIT\" \"$@\"\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "GOALFLIGHT_SEARCH_CAT_FILE": str(log_path),
            "GOALFLIGHT_SEARCH_REAL_GIT": real_git,
        }
    )
    result = _run_search(
        repo,
        "needle",
        "--refs",
        "refs/keep",
        "refs/heads",
        "--json",
        "--",
        "core",
        env=env,
    )
    _json_result(result)
    requests = [line for line in log_path.read_text(encoding="utf-8").splitlines() if line]
    expected = _tree_blob_ids(repo, first, "core") | _tree_blob_ids(repo, second, "core")
    assert len(requests) == len(expected)
    assert len(requests) == len(set(requests))


def test_refusal_requires_paths_for_large_ref_sets(tmp_path: Path) -> None:
    repo, first, _ = _fixture_repo(tmp_path)
    for index in range(101):
        _git(repo, "update-ref", f"refs/keep/many-{index}", first)
    refused = _run_search(repo, "needle", "--refs", "refs/keep")
    assert refused.returncode == 2
    assert "ref search refused" in refused.stderr
    assert "Add paths after --" in refused.stderr

    allowed = _run_search(repo, "needle", "--refs", "refs/keep", "--json", "--", "core")
    assert allowed.returncode == 0, allowed.stderr


def test_history_is_bounded_and_reports_paths(tmp_path: Path) -> None:
    repo = _new_repo(tmp_path)
    history_file = repo / "core/history.py"
    history_file.parent.mkdir()
    for index in range(3):
        history_file.write_text(f"needle revision {index}\n", encoding="utf-8")
        _commit(repo, f"history {index}")
    result = _run_search(
        repo,
        "needle revision [0-9]",
        "--history",
        "--max-results",
        "2",
        "--json",
        "--",
        "core/history.py",
    )
    document = _json_result(result)
    assert len(document["results"]) == 2
    assert document["truncated"] is True
    assert all(item["paths"] == ["core/history.py"] for item in document["results"])


def test_history_refuses_an_unscoped_log_walk(tmp_path: Path) -> None:
    repo = _new_repo(tmp_path)
    (repo / "source.py").write_text("needle\n", encoding="utf-8")
    _commit(repo, "source")
    result = _run_search(repo, "needle", "--history")
    assert result.returncode == 2
    assert "requires PATH filters" in result.stderr


def test_large_blob_is_skipped_without_loading_or_matching_it(tmp_path: Path) -> None:
    repo = _new_repo(tmp_path)
    large = repo / "core/large.txt"
    large.parent.mkdir()
    large.write_bytes(b"needle\n" + b"x" * (1024 * 1024 + 4096))
    commit = _commit(repo, "large blob")
    _git(repo, "update-ref", "refs/keep/large", commit)
    result = _run_search(
        repo,
        "needle",
        "--fixed",
        "--refs",
        "refs/keep",
        "--json",
        "--",
        "core",
    )
    document = _json_result(result)
    assert document["results"] == []
    assert document["stats"]["blobs_skipped_large"] == 1


def test_regex_match_can_span_more_than_one_stream_chunk(tmp_path: Path) -> None:
    repo = _new_repo(tmp_path)
    source = repo / "core/long.txt"
    source.parent.mkdir()
    source.write_bytes(b"start" + b"x" * (70 * 1024) + b"needle\n")
    commit = _commit(repo, "long regex")
    _git(repo, "update-ref", "refs/keep/long-regex", commit)
    document = _json_result(
        _run_search(
            repo,
            "start.*needle",
            "--refs",
            "refs/keep/long-regex",
            "--json",
            "--",
            "core",
        )
    )
    assert len(document["results"]) == 1


def test_tree_mode_returns_current_checkout_matches(tmp_path: Path) -> None:
    repo = _new_repo(tmp_path)
    (repo / "source.py").write_text("needle\n", encoding="utf-8")
    (repo / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
    (repo / "ignored.py").write_text("needle ignored\n", encoding="utf-8")
    _commit(repo, "source")
    result = _run_search(repo, "needle", "--fixed", "--json", "--max-results", "1")
    document = _json_result(result)
    assert document["results"][0]["path"] == "source.py"


def test_tree_mode_falls_back_to_git_grep_without_rg(tmp_path: Path) -> None:
    repo = _new_repo(tmp_path)
    source = repo / "source.py"
    source.write_text("needle\n", encoding="utf-8")
    weird = repo / "dir:1:source.py"
    weird.write_text("needle\n", encoding="utf-8")
    _commit(repo, "source")
    git_path = shutil.which("git")
    assert git_path
    env = os.environ.copy()
    env["PATH"] = f"{Path(git_path).parent}:/usr/bin:/bin"
    result = _run_search(repo, "needle", "--fixed", "--json", "--", "dir:1:source.py", env=env)
    document = _json_result(result)
    assert document["results"] == [{"line": 1, "path": "dir:1:source.py", "text": "needle"}]


def main() -> None:
    raise SystemExit(pytest.main([__file__, "-q"]))


if __name__ == "__main__":
    main()
