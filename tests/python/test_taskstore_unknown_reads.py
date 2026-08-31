#!/usr/bin/env python3
"""Task-store reads that cannot complete must refuse, not invent a definite answer.

A failed resolution, scan, import, or schema check used to render as empty /
not-blocked / not-dispatched / success. For a store, losing a requirement is
worse than rejecting the write. These tests induce the real condition.
"""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
TASK = ROOT / "goalflight_task.py"
SCRIPTS = ROOT / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import goalflight_dispatch_states as states  # noqa: E402
import goalflight_ledger as ledger  # noqa: E402
import goalflight_task as task  # noqa: E402


def _run(project: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    return subprocess.run(
        [sys.executable, str(TASK), "--project-root", str(project), *args],
        cwd=str(ROOT),
        env=merged,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )


def _item(
    item_id: str,
    title: str,
    *,
    kind: str = "task",
    lane: str = "now",
    **extra: object,
) -> dict[str, object]:
    row: dict[str, object] = {
        "schema_version": 1,
        "id": item_id,
        "kind": kind,
        "title": title,
        "lane": lane,
        "blocked_by": [],
        "links": [],
        "done": False,
        "created_at": "2026-07-01T00:00:00+00:00",
        "created_by": "test",
    }
    row.update(extra)
    return row


def test_unresolvable_root_capture_refuses_and_writes_nowhere(tmp_path: Path) -> None:
    intended = tmp_path / "intended-repo"
    intended.mkdir()
    (intended / "docs-private").mkdir()
    typo = tmp_path / "intendde-repo"
    assert not typo.exists()

    store_root = Path(os.environ["GOALFLIGHT_TASK_STORE_DIR"])
    before = {p for p in store_root.rglob("*") if p.is_file()} if store_root.exists() else set()

    proc = _run(typo, "capture", "misrouted requirement")
    assert proc.returncode != 0, proc.stderr
    assert "unresolvable project root" in proc.stderr
    assert str(typo) in proc.stderr
    assert "not a directory" in proc.stderr
    assert "Refusing to write to another store" in proc.stderr
    assert not typo.exists(), "typo path must not be created as a new store"
    assert not list(intended.rglob("tasks.jsonl"))

    after = {p for p in store_root.rglob("*") if p.is_file()} if store_root.exists() else set()
    new_files = after - before
    assert not any(path.name == "tasks.jsonl" for path in new_files), new_files


def test_ledger_terminal_record_writable_when_project_root_is_gone(
    tmp_path: Path,
) -> None:
    """A vanished checkout is a ledger label, not a store destination.

    Reverting ``canonicalize_project_root_on_store`` to ``resolve_project_root``
    (the write-refuse helper) raises TaskError here and strands the dispatch:
    the terminal ledger row cannot be written, so reconciliation cannot observe
    it. Capture of the same path must still refuse.
    """
    vanished = tmp_path / "deleted-worktree"
    assert not vanished.exists()

    store_root = Path(os.environ["GOALFLIGHT_TASK_STORE_DIR"])
    before = {p for p in store_root.rglob("*") if p.is_file()} if store_root.exists() else set()

    dispatch_id = "vanished-root-terminal"
    path = ledger.write_record(
        {
            "dispatch_id": dispatch_id,
            "state": "complete",
            "terminal_state": "complete",
            "project_root": str(vanished),
        }
    )
    assert path.is_file()
    stored = ledger.read_record(dispatch_id)
    assert stored is not None
    assert stored["dispatch_id"] == dispatch_id
    assert stored["state"] == "complete"
    assert states.is_terminal_state(stored["state"])
    assert stored["project_root"] == str(vanished)
    assert path == ledger.record_path(dispatch_id)
    assert task.resolve_project_root_for_read(stored["project_root"]) is None

    proc = _run(vanished, "capture", "must not land in another store")
    assert proc.returncode != 0, proc.stderr
    assert "unresolvable project root" in proc.stderr
    assert "Refusing to write to another store" in proc.stderr
    with pytest.raises(task.TaskError, match="Refusing to write to another store"):
        task.resolve_project_root(str(vanished))

    after = {p for p in store_root.rglob("*") if p.is_file()} if store_root.exists() else set()
    assert not any(path.name == "tasks.jsonl" for path in after - before), after - before


def test_canonicalize_on_store_collapses_live_root_and_keeps_missing_raw(
    tmp_path: Path,
) -> None:
    """Live roots still collapse; a missing root keeps the raw label.

    The stored missing value is the unresolved identity, not a collapsed one.
    A reader hashing that identity for a task-store key is not addressing the
    ledger record; ledger lookup is by dispatch_id.
    """
    live = tmp_path / "plain"
    live.mkdir()
    collapsed = task.resolve_project_root(str(live))
    assert ledger.canonicalize_project_root_on_store(str(live)) == str(collapsed)

    missing = tmp_path / "gone-checkout"
    assert not missing.exists()
    assert ledger.canonicalize_project_root_on_store(str(missing)) == str(missing)
    live_store = task.resolve_task_store_dir(collapsed)
    raw_store = task.resolve_task_store_dir(missing)
    assert raw_store != live_store


def test_read_of_unresolvable_root_is_none_not_an_identity(tmp_path: Path) -> None:
    missing = tmp_path / "no-such-checkout"
    assert not missing.exists()
    assert task.resolve_project_root_for_read(str(missing)) is None
    with pytest.raises(task.TaskError, match="Refusing to write to another store"):
        task.resolve_project_root(str(missing))
    plain = tmp_path / "plain"
    plain.mkdir()
    assert task.resolve_project_root_for_read(str(plain)) == plain


def test_read_of_git_oserror_is_none_not_the_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "maybe-git"
    project.mkdir()

    def boom(*_args: object, **_kwargs: object) -> str:
        raise OSError(errno.ENOENT, "No such file or directory", "git")

    monkeypatch.setattr(task.subprocess, "check_output", boom)
    assert task.resolve_project_root_for_read(str(project)) is None
    with pytest.raises(task.TaskError, match="Refusing to write to another store"):
        task.resolve_project_root(str(project))


def test_existing_non_git_directory_still_captures(tmp_path: Path) -> None:
    project = tmp_path / "plain"
    project.mkdir()
    (project / "docs-private").mkdir()
    proc = _run(project, "capture", "plain-dir requirement")
    assert proc.returncode == 0, proc.stderr
    item_id = proc.stdout.strip()
    shown = _run(project, "show", item_id, "--json")
    assert shown.returncode == 0, shown.stderr
    payload = json.loads(shown.stdout)
    assert payload["id"] == item_id
    assert payload["title"] == "plain-dir requirement"


def test_git_oserror_refuses_rather_than_retargeting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "maybe-git"
    project.mkdir()

    def boom(*_args: object, **_kwargs: object) -> str:
        raise OSError(errno.ENOENT, "No such file or directory", "git")

    monkeypatch.setattr(task.subprocess, "check_output", boom)
    with pytest.raises(task.TaskError, match="unresolvable project root"):
        task.resolve_project_root(str(project))
    with pytest.raises(task.TaskError, match="cannot canonicalize via git"):
        task.resolve_project_root(str(project))


def test_snapshot_scan_error_is_not_an_empty_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "legacy-project"
    log = project / "docs-private" / "log"
    log.mkdir(parents=True)
    surviving = _item("t-500", "surviving requirement")
    (log / "tasks-20260101.jsonl").write_text(
        json.dumps(surviving, sort_keys=True) + "\n", encoding="utf-8"
    )
    store = task.TaskStore(project)
    loaded = store.load_items()
    assert [row["id"] for row in loaded] == ["t-500"]

    real_listdir = os.listdir
    log_resolved = log.resolve()

    def boom(path: str | os.PathLike[str]) -> list[str]:
        if Path(path).resolve() == log_resolved:
            raise OSError(errno.EACCES, "permission denied", str(path))
        return real_listdir(path)

    monkeypatch.setattr(os, "listdir", boom)
    with pytest.raises(task.TaskError, match="snapshot scan failed"):
        store.load_items()
    with pytest.raises(task.TaskError, match="empty store"):
        store.load_items()


_SKIP_CHMOD000 = pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="chmod 000 does not bite on Windows or as root",
)


@_SKIP_CHMOD000
def test_unlistable_snapshot_dir_is_unknown_not_empty(tmp_path: Path) -> None:
    """Path.glob on chmod 000 returns [] without raising; the reader must not."""
    project = tmp_path / "legacy-project"
    log = project / "docs-private" / "log"
    log.mkdir(parents=True)
    surviving = _item("t-500", "surviving requirement")
    (log / "tasks-20260101.jsonl").write_text(
        json.dumps(surviving, sort_keys=True) + "\n", encoding="utf-8"
    )
    store = task.TaskStore(project)
    assert [row["id"] for row in store.load_items()] == ["t-500"]

    os.chmod(log, 0o000)
    try:
        with pytest.raises(task.TaskError, match="snapshot scan failed"):
            store.load_items()
        listed = _run(project, "list", "--json")
        assert listed.returncode != 0, listed.stdout
        assert "snapshot scan failed" in listed.stderr
        assert listed.stdout.strip() != "[]"
        captured = _run(project, "capture", "new while snapshots unreadable")
        assert captured.returncode != 0, captured.stdout
        assert "snapshot scan failed" in captured.stderr
    finally:
        os.chmod(log, 0o700)

    restored = task.TaskStore(project).load_items()
    assert [row["id"] for row in restored] == ["t-500"]
    assert all(row["title"] != "new while snapshots unreadable" for row in restored)


def _init_git_repo(path: Path) -> None:
    subprocess.run(
        ["git", "init", "-q"],
        cwd=str(path),
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    (path / "f.txt").write_text("x", encoding="utf-8")
    env = os.environ.copy()
    env.update(
        {
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        }
    )
    subprocess.run(
        ["git", "add", "f.txt"],
        cwd=str(path),
        check=True,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    subprocess.run(
        ["git", "commit", "-qm", "init"],
        cwd=str(path),
        check=True,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )


def test_subdirectory_capture_is_visible_from_repo_root(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    src = repo / "src"
    src.mkdir()
    _init_git_repo(repo)

    captured = _run(src, "capture", "from-subdir-healthy")
    assert captured.returncode == 0, captured.stderr
    item_id = captured.stdout.strip()
    assert item_id.startswith("t-")

    shown_src = _run(src, "show", item_id, "--json")
    shown_root = _run(repo, "show", item_id, "--json")
    listed_root = _run(repo, "list", "--json")
    listed_src = _run(src, "list", "--json")
    assert shown_src.returncode == 0, shown_src.stderr
    assert shown_root.returncode == 0, shown_root.stderr
    assert listed_root.returncode == 0, listed_root.stderr
    assert listed_src.returncode == 0, listed_src.stderr
    assert json.loads(shown_src.stdout)["title"] == "from-subdir-healthy"
    assert json.loads(shown_root.stdout)["title"] == "from-subdir-healthy"
    root_ids = [row["id"] for row in json.loads(listed_root.stdout)]
    src_ids = [row["id"] for row in json.loads(listed_src.stdout)]
    assert item_id in root_ids
    assert src_ids == root_ids

    root_store = task.resolve_task_store_dir(task.resolve_project_root(str(repo)))
    src_store = task.resolve_task_store_dir(task.resolve_project_root(str(src)))
    assert root_store == src_store


def test_empty_git_dir_from_subdirectory_refuses(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    src = repo / "src"
    src.mkdir()
    _init_git_repo(repo)

    store_root = Path(os.environ["GOALFLIGHT_TASK_STORE_DIR"])
    before = {p for p in store_root.rglob("*") if p.is_file()} if store_root.exists() else set()
    proc = _run(src, "capture", "from-subdir-broken-gitdir", env={"GIT_DIR": ""})
    assert proc.returncode != 0, proc.stdout
    assert "unresolvable project root" in proc.stderr
    assert "Refusing to write to another store" in proc.stderr
    after = {p for p in store_root.rglob("*") if p.is_file()} if store_root.exists() else set()
    assert not any(path.name == "tasks.jsonl" for path in after - before)

    listed = _run(repo, "list", "--json")
    assert listed.returncode == 0, listed.stderr
    assert json.loads(listed.stdout) == []


@_SKIP_CHMOD000
def test_unreadable_dot_git_from_subdirectory_refuses(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    src = repo / "src"
    src.mkdir()
    _init_git_repo(repo)
    git_dir = repo / ".git"

    store_root = Path(os.environ["GOALFLIGHT_TASK_STORE_DIR"])
    before = {p for p in store_root.rglob("*") if p.is_file()} if store_root.exists() else set()
    os.chmod(git_dir, 0o000)
    try:
        proc = _run(src, "capture", "captured-while-git-unreadable")
        assert proc.returncode != 0, proc.stdout
        assert "unresolvable project root" in proc.stderr
        assert "Refusing to write to another store" in proc.stderr
    finally:
        os.chmod(git_dir, 0o700)
    after = {p for p in store_root.rglob("*") if p.is_file()} if store_root.exists() else set()
    assert not any(path.name == "tasks.jsonl" for path in after - before)


def test_empty_git_dir_in_non_git_directory_still_captures(tmp_path: Path) -> None:
    project = tmp_path / "plain"
    project.mkdir()
    (project / "docs-private").mkdir()
    proc = _run(project, "capture", "plain-with-empty-git-dir", env={"GIT_DIR": ""})
    assert proc.returncode == 0, proc.stderr
    item_id = proc.stdout.strip()
    shown = _run(project, "show", item_id, "--json")
    assert shown.returncode == 0, shown.stderr
    assert json.loads(shown.stdout)["title"] == "plain-with-empty-git-dir"


def test_git_repo_root_capture_show_list_round_trip(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "docs-private").mkdir()
    _init_git_repo(repo)
    captured = _run(repo, "capture", "from-repo-root")
    assert captured.returncode == 0, captured.stderr
    item_id = captured.stdout.strip()
    shown = _run(repo, "show", item_id, "--json")
    listed = _run(repo, "list", "--json")
    assert shown.returncode == 0, shown.stderr
    assert listed.returncode == 0, listed.stderr
    assert json.loads(shown.stdout)["title"] == "from-repo-root"
    assert item_id in [row["id"] for row in json.loads(listed.stdout)]


def test_genuine_empty_store_still_reads_empty(tmp_path: Path) -> None:
    project = tmp_path / "empty-project"
    project.mkdir()
    (project / "docs-private").mkdir()
    store = task.TaskStore(project)
    assert store.load_items() == []
    proc = _run(project, "list", "--json")
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == []


def test_ledger_import_failure_is_not_no_dispatch_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "ledger-project"
    project.mkdir()
    (project / "docs-private").mkdir()
    minted = _run(project, "capture", "live work", "--lane", "now")
    assert minted.returncode == 0, minted.stderr
    item_id = minted.stdout.strip()

    store = task.TaskStore(project)
    empty_frontier = store.next_frontier()
    assert [row["id"] for row in empty_frontier] == [item_id]

    class LiveLedger:
        @staticmethod
        def read_records() -> list[dict[str, object]]:
            return [
                {
                    "dispatch_id": "d-live",
                    "task_id": item_id,
                    "state": "working",
                    "project_root": str(store.project_root),
                    "started_at": "2026-07-01T00:01:00+00:00",
                }
            ]

    monkeypatch.setattr(task, "goalflight_ledger", LiveLedger)
    live_frontier = store.next_frontier()
    assert [row["id"] for row in live_frontier] == []

    monkeypatch.setattr(task, "goalflight_ledger", None)
    with pytest.raises(task.TaskError, match="ledger module failed to import"):
        store.project_ledger_records()
    with pytest.raises(task.TaskError, match="not idle"):
        store.next_frontier()


def test_malformed_blocked_by_is_not_unblocked(tmp_path: Path) -> None:
    project = tmp_path / "blocked-project"
    project.mkdir()
    docs = project / "docs-private"
    docs.mkdir()
    docs.joinpath("tasks.jsonl").write_text(
        json.dumps(
            _item("t-802", "open blocker", kind="task")
        )
        + "\n"
        + json.dumps(_item("t-801", "gated", blocked_by="t-802"))
        + "\n",
        encoding="utf-8",
    )
    store = task.TaskStore(project)
    with pytest.raises(task.TaskError, match="blocked_by must be an array"):
        store.load_items()
    proc = _run(project, "next")
    assert proc.returncode != 0, proc.stdout
    assert "blocked_by must be an array" in proc.stderr
    assert "t-801" not in proc.stdout

    with pytest.raises(task.TaskError, match="malformed is not unblocked"):
        task.unsatisfied_blockers({"id": "t-801", "blocked_by": "t-802"}, {})
    assert task.unsatisfied_blockers({"id": "t-1", "blocked_by": []}, {}) == []
    assert task.unsatisfied_blockers({"id": "t-2"}, {}) == []


def test_malformed_dispatches_is_not_undispatched(tmp_path: Path) -> None:
    project = tmp_path / "dispatch-project"
    project.mkdir()
    docs = project / "docs-private"
    docs.mkdir()
    docs.joinpath("tasks.jsonl").write_text(
        json.dumps(
            _item(
                "t-900",
                "looks idle",
                dispatches={"dispatch_id": "d1", "state": "working"},
            )
        )
        + "\n",
        encoding="utf-8",
    )
    store = task.TaskStore(project)
    with pytest.raises(task.TaskError, match="dispatches must be an array"):
        store.load_items()
    proc = _run(project, "next")
    assert proc.returncode != 0, proc.stdout
    assert "dispatches must be an array" in proc.stderr
    assert "t-900" not in proc.stdout

    with pytest.raises(task.TaskError, match="malformed is not undispatched"):
        task._latest_dispatch_breadcrumb(
            {"id": "t-900", "dispatches": {"dispatch_id": "d1", "state": "working"}}
        )
    assert task._latest_dispatch_breadcrumb({"id": "t-1"}) is None
    assert task._latest_dispatch_breadcrumb({"id": "t-2", "dispatches": []}) is None


def test_fsync_dir_open_failure_fails_the_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "fsync-project"
    project.mkdir()
    (project / "docs-private").mkdir()
    real_open = os.open

    def boom(path: int | str | bytes | os.PathLike[str], flags: int, *args: object, **kwargs: object) -> int:
        if flags == os.O_RDONLY:
            try:
                is_dir = Path(path).is_dir()
            except (TypeError, OSError, ValueError):
                is_dir = False
            if is_dir:
                raise OSError(errno.EIO, "injected directory open failure")
        return real_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "open", boom)
    rc = task.main(["--project-root", str(project), "capture", "must not claim durability"])
    assert rc != 0
    listed = _run(project, "list", "--json")
    # Either the write never landed, or it rolled back. It must not report
    # success while remaining unreadable; after a failed mutation the store
    # is still empty (no requirement silently claimed).
    assert listed.returncode == 0, listed.stderr
    assert json.loads(listed.stdout) == []
