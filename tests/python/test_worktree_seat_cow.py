#!/usr/bin/env python3
"""Copy-on-write population of new sparse worktree seats."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
import tempfile
import threading

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_seat_cow as cow
import goalflight_seat_dedupe as dedupe
import goalflight_worktree_pool as pool

PATTERN_TEXT = "/*\n!/docs-private/build/remote-pytest/\n"
EXCLUDED = Path("docs-private") / "build" / "remote-pytest" / "evidence.txt"


def git(repo: Path, *args: str, input_text: str | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed: {result.stderr or result.stdout}"
        )
    return result.stdout.strip()


def pin_host_git(root: Path) -> None:
    """Point Git config and attribute lookup at empty files under ``root``.

    Fixture creation runs before ``isolated()``, so both call this. A later
    test may point ``XDG_CONFIG_HOME`` at a directory it filled in.
    """
    xdg = root / "xdg"
    xdg.mkdir(parents=True, exist_ok=True)
    global_config = root / "gitconfig.global"
    system_config = root / "gitconfig.system"
    global_config.touch(exist_ok=True)
    system_config.touch(exist_ok=True)
    os.environ["GIT_CONFIG_GLOBAL"] = str(global_config)
    os.environ["GIT_CONFIG_SYSTEM"] = str(system_config)
    os.environ["GIT_ATTR_NOSYSTEM"] = "1"
    os.environ["XDG_CONFIG_HOME"] = str(xdg)


def init_repo(path: Path) -> None:
    git(path, "init", "-b", "main")
    git(path, "config", "user.email", "goalflight-test@example.invalid")
    git(path, "config", "user.name", "Goal Flight Test")
    git(path, "config", "extensions.worktreeConfig", "true")
    git(path, "config", "core.filemode", "true")
    git(path, "config", "core.symlinks", "true")


def make_sparse_repo(root: Path) -> tuple[Path, str, Path]:
    pin_host_git(root)
    repo = root / "repo"
    repo.mkdir()
    init_repo(repo)
    (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
    (repo / "included.txt").write_text("keep me\n", encoding="utf-8")
    excluded = repo / "docs-private" / "build" / "remote-pytest"
    excluded.mkdir(parents=True)
    (excluded / "evidence.txt").write_text("heavy\n", encoding="utf-8")
    git(repo, "add", "--", ".")
    git(repo, "commit", "-m", "sparse fixture")
    pattern = root / "seat-sparse.pattern"
    pattern.write_text(PATTERN_TEXT, encoding="utf-8")
    return repo.resolve(), git(repo, "rev-parse", "HEAD"), pattern


def make_transition_repo(root: Path) -> tuple[Path, str, str, Path]:
    pin_host_git(root)
    repo = root / "repo"
    repo.mkdir()
    init_repo(repo)
    (repo / "included.txt").write_text("v1\n", encoding="utf-8")
    script = repo / "script.sh"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    script.chmod(0o755)
    (repo / "dangling").symlink_to("no-such-target")
    (repo / "widget").write_text("i am a file\n", encoding="utf-8")
    (repo / "sub").mkdir()
    (repo / "sub" / "keep.txt").write_text("keep\n", encoding="utf-8")
    excluded = repo / "docs-private" / "build" / "remote-pytest"
    excluded.mkdir(parents=True)
    (excluded / "evidence.txt").write_text("heavy\n", encoding="utf-8")
    (repo / ".gitignore").write_text("*.ignored\n", encoding="utf-8")
    git(repo, "add", "--", ".")
    git(repo, "commit", "-m", "template")
    template = git(repo, "rev-parse", "HEAD")
    (repo / "included.txt").write_text("v2\n", encoding="utf-8")
    (repo / "widget").unlink()
    (repo / "widget").mkdir()
    (repo / "widget" / "item.txt").write_text("now a dir\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "base")
    pattern = root / "seat-sparse.pattern"
    pattern.write_text(PATTERN_TEXT, encoding="utf-8")
    return repo.resolve(), template, git(repo, "rev-parse", "HEAD"), pattern


def write_config(
    root: Path,
    repo: Path,
    pattern: Path,
    *,
    cow: bool | str | None,
    seats: int = 4,
) -> Path:
    payload: dict = {
        "worktrees_per_repo": seats,
        "seat_sparse_pattern_files": {str(repo): str(pattern)},
    }
    if cow is not None:
        payload["seat_cow_templates"] = {str(repo): cow}
    config = root / "capacity.local.json"
    config.write_text(json.dumps(payload), encoding="utf-8")
    return config


@contextlib.contextmanager
def isolated(root: Path, config: Path):
    pin_host_git(root)
    keys = {
        "GOALFLIGHT_CAPACITY_CONF": str(config),
        "GOALFLIGHT_STATE_DIR": str(root / "state"),
        "GOALFLIGHT_JOURNAL_DIR": str(root / "journal"),
        "GOALFLIGHT_WAKE_LEDGER": str(root / "wake-ledger"),
        "GOALFLIGHT_MESSAGES_DIR": str(root / "messages"),
        "GOALFLIGHT_TASK_STORE": str(root / "task-store"),
        "GOALFLIGHT_TASK_STORE_DIR": str(root / "task-store"),
        "GOALFLIGHT_PIDFILE_DIR": str(root / "pids"),
        "GOAL_FLIGHT_PIDFILE_DIR": str(root / "pids"),
        "GIT_CONFIG_GLOBAL": os.environ["GIT_CONFIG_GLOBAL"],
        "GIT_CONFIG_SYSTEM": os.environ["GIT_CONFIG_SYSTEM"],
        "GIT_ATTR_NOSYSTEM": os.environ["GIT_ATTR_NOSYSTEM"],
        "XDG_CONFIG_HOME": os.environ["XDG_CONFIG_HOME"],
    }
    for key, value in keys.items():
        if key in {
            "GOALFLIGHT_CAPACITY_CONF",
            "GIT_CONFIG_GLOBAL",
            "GIT_CONFIG_SYSTEM",
            "GIT_ATTR_NOSYSTEM",
        }:
            continue
        Path(value).mkdir(parents=True, exist_ok=True)
    prior = {key: os.environ.get(key) for key in keys}
    os.environ.update(keys)
    try:
        yield
    finally:
        for key, value in prior.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def require_clonefile(path: Path) -> None:
    supported, reason, _fstype = cow.clonefile_support(path)
    if not supported:
        pytest.skip(f"clonefile unavailable: {reason}")


def manifest(root: Path) -> list[tuple]:
    rows: list[tuple] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(name for name in dirnames if name != ".git")
        base = Path(dirpath)
        rel_base = base.relative_to(root)
        for name in list(dirnames):
            path = base / name
            if path.is_symlink():
                rel = (rel_base / name).as_posix()
                rows.append(("symlink", rel, os.readlink(path)))
                dirnames.remove(name)
        for name in sorted(filenames):
            if rel_base == Path(".") and name == ".git":
                continue
            path = base / name
            rel = name if rel_base == Path(".") else (rel_base / name).as_posix()
            if path.is_symlink():
                rows.append(("symlink", rel, os.readlink(path)))
            else:
                rows.append(
                    (
                        "file",
                        rel,
                        path.read_bytes(),
                        stat.S_IMODE(path.stat().st_mode),
                    )
                )
    return sorted(rows)


def sparse_tags(worktree: Path) -> list[str]:
    return sorted(git(worktree, "ls-files", "-t").splitlines())


def index_entries(worktree: Path) -> list[str]:
    return sorted(git(worktree, "ls-files", "-s").splitlines())


def listed_worktrees(repo: Path) -> list[str]:
    paths = []
    for line in git(repo, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            paths.append(str(Path(line.split(" ", 1)[1]).resolve()))
    return paths


def template_root(repo: Path) -> Path:
    return cow.templates_root(repo)


def admin_names(repo: Path) -> list[str]:
    common = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    root = common / "worktrees"
    if not root.is_dir():
        return []
    return sorted(path.name for path in root.iterdir())


def plain_sparse_seat(repo: Path, dest: Path, branch: str, commit: str, pattern: Path) -> None:
    git(repo, "worktree", "add", "--no-checkout", "-b", branch, str(dest), commit)
    git(dest, "config", "--worktree", "core.sparseCheckout", "true")
    git(
        dest,
        "sparse-checkout",
        "set",
        "--no-cone",
        "--stdin",
        input_text=pattern.read_text(encoding="utf-8"),
    )
    git(
        dest,
        "-c",
        "submodule.recurse=false",
        "checkout",
        "--no-overwrite-ignore",
        "-f",
        branch,
    )


def install_hook(root: Path, repo: Path, body: str) -> Path:
    hooks = root / "hooks"
    hooks.mkdir(exist_ok=True)
    script = hooks / "post-checkout"
    script.write_text(body, encoding="utf-8")
    script.chmod(0o755)
    git(repo, "config", "core.hooksPath", str(hooks))
    return root / "hook.log"


def hook_count(log: Path, worktree: Path) -> int:
    if not log.is_file():
        return 0
    target = str(worktree.resolve())
    count = 0
    for line in log.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if text and str(Path(text).resolve()) == target:
            count += 1
    return count


def assert_same_seat(actual: Path, expected: Path, commit: str) -> None:
    assert git(actual, "rev-parse", "HEAD") == commit
    assert git(expected, "rev-parse", "HEAD") == commit
    assert manifest(actual) == manifest(expected)
    assert sparse_tags(actual) == sparse_tags(expected)
    assert index_entries(actual) == index_entries(expected)
    assert not (actual / EXCLUDED).exists()
    assert (actual / "included.txt").is_file()


def assert_only_seat(repo: Path, seat: Path) -> None:
    assert listed_worktrees(repo) == sorted(
        [str(repo.resolve()), str(seat.resolve())]
    )
    assert admin_names(repo) == [seat.name]


class _Leases:
    def __init__(self) -> None:
        self._leases = []

    def add(self, lease):
        self._leases.append(lease)
        return lease

    def release(self) -> None:
        while self._leases:
            self._leases.pop().release()


def test_clonefile_detector_distinguishes_copy() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        require_clonefile(root)
        src = root / "src"
        src.mkdir()
        (src / "payload.txt").write_bytes(b"xyz" * 100)
        cloned = root / "cloned"
        copied = root / "copied"
        cloned.mkdir()
        copied.mkdir()
        assert cow.clone_tree_payload(src, cloned) == "clonefile"
        result = subprocess.run(
            ["cp", "-R", "-p", "--", str(src / "payload.txt"), str(copied)],
            check=False,
        )
        assert result.returncode == 0
        assert cow._classify_clone(src, copied) == "full-copy"


def test_clonefile_support_rejects_non_darwin() -> None:
    original = cow.sys.platform
    cow.sys.platform = "linux"
    try:
        supported, reason, fstype = cow.clonefile_support(Path("/"))
    finally:
        cow.sys.platform = original
    assert supported is False
    assert reason == "platform-not-darwin"
    assert fstype is None


@pytest.mark.parametrize("reason", ["base-changed", "tracked-changes"])
def test_intentional_reuse_skip_is_marked_in_population_report(
    tmp_path: Path, reason: str
) -> None:
    repo, base, _pattern = make_sparse_repo(tmp_path)
    seat = tmp_path / "seat"
    git(repo, "worktree", "add", "--detach", str(seat), base)

    pool._report_cow_population(
        seat,
        enabled=True,
        pattern=PATTERN_TEXT,
        operation="reuse",
        cow_result=None,
        reason=reason,
    )

    report = cow.read_seat_cow_report(seat)
    assert report is not None
    assert report["population"] == "plain"
    assert report["cow_skipped"] is True
    assert report["reason"] == reason
    assert report["operation"] == "reuse"


def test_flag_off_matches_manual_sparse_seat() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        repo, base, pattern = make_sparse_repo(root)
        planted = template_root(repo) / "profiles" / "leftover" / "building"
        planted.mkdir(parents=True)
        (planted / "keep.txt").write_text("stay\n", encoding="utf-8")
        config = write_config(root, repo, pattern, cow=None)
        manual = root / "manual"
        entered = {"n": 0}

        def entered_cow(*_args, **_kwargs):
            entered["n"] += 1
            raise AssertionError("flag-off seat creation entered the cow helper")

        originals = (
            cow.prepare_for_new_seat,
            cow.prime_sparse_worktree,
            cow.clone_tree_payload,
        )
        cow.prepare_for_new_seat = entered_cow
        cow.prime_sparse_worktree = entered_cow
        cow.clone_tree_payload = entered_cow
        try:
            with isolated(root, config):
                assert pool.seat_cow_enabled(repo) is False
                lease = pool.acquire_worktree_seat(repo, "flag-off", base=base)
                try:
                    plain_sparse_seat(repo, manual, "manual-off", base, pattern)
                    assert_same_seat(lease.path, manual, base)
                    assert cow.read_seat_cow_report(lease.path) is None
                    assert (planted / "keep.txt").read_text(encoding="utf-8") == "stay\n"
                    assert entered["n"] == 0
                    assert not (template_root(repo) / "locks").exists()
                finally:
                    lease.release()
        finally:
            (
                cow.prepare_for_new_seat,
                cow.prime_sparse_worktree,
                cow.clone_tree_payload,
            ) = originals


def test_invalid_and_false_flags_stay_off() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        repo, base, pattern = make_sparse_repo(root)
        for value in (False, "yes"):
            config = write_config(root, repo, pattern, cow=value)  # type: ignore[arg-type]
            with isolated(root, config):
                assert pool.seat_cow_enabled(repo) is False
                lease = pool.acquire_worktree_seat(repo, f"flag-{value}", base=base)
                try:
                    assert git(lease.path, "rev-parse", "HEAD") == base
                    assert cow.read_seat_cow_report(lease.path) is None
                    assert not (template_root(repo) / "locks").exists()
                finally:
                    lease.release()


def test_cow_seat_matches_plain_seat() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        require_clonefile(root)
        repo, template, base, pattern = make_transition_repo(root)
        log = install_hook(
            root,
            repo,
            "#!/bin/sh\n" + f"printf '%s\\n' \"$PWD\" >> {shlex.quote(str(root / 'hook.log'))}\n",
        )
        config = write_config(root, repo, pattern, cow=True)
        manual = root / "manual"
        leases = _Leases()
        with isolated(root, config):
            try:
                at_template = leases.add(
                    pool.acquire_worktree_seat(repo, "cow-template", base=template)
                )
                at_base = leases.add(
                    pool.acquire_worktree_seat(repo, "cow-base", base=base)
                )
                plain_sparse_seat(repo, manual, "manual-plain", base, pattern)
                assert_same_seat(at_base.path, manual, base)
                assert (at_base.path / "dangling").is_symlink()
                assert os.readlink(at_base.path / "dangling") == "no-such-target"
                assert not (at_base.path / "dangling").exists()
                assert (at_base.path / "widget" / "item.txt").read_text(encoding="utf-8") == (
                    "now a dir\n"
                )
                assert stat.S_IMODE((at_base.path / "script.sh").stat().st_mode) & 0o111
                assert hook_count(log, at_template.path) == 1
                assert hook_count(log, at_base.path) == 1
                assert cow.TEMPLATE_DIRNAME not in log.read_text(encoding="utf-8")
                report = cow.read_seat_cow_report(at_base.path)
                assert report is not None
                assert report["population"] == "cow"
                assert report["clone"] == "clonefile"
                assert report["template_commit"] == template
                assert report["behind"] == 1
                digest = hashlib.sha256(PATTERN_TEXT.encode("utf-8")).hexdigest()
                assert report["profile_digest"] == digest
                generation = cow.generation_payload(repo, digest, report["generation"])
                assert generation.is_dir()
                assert not (generation / ".git").exists()
                assert not (generation / "index").exists()
                assert (generation / "included.txt").read_text(encoding="utf-8") == "v1\n"
                assert (generation / "widget").is_file()
                assert (generation / "dangling").is_symlink()
                assert all(cow.TEMPLATE_DIRNAME not in path for path in listed_worktrees(repo))
            finally:
                leases.release()


def test_cow_checkout_modes_match_umask() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        require_clonefile(root)
        repo, _template, base, pattern = make_transition_repo(root)
        git(repo, "config", "core.filemode", "false")
        config = write_config(root, repo, pattern, cow=True, seats=2)
        manual = root / "manual"
        with isolated(root, config):
            previous = os.umask(0o022)
            try:
                first = pool.acquire_worktree_seat(repo, "cow-mode-022", base=base)
                first_report = cow.read_seat_cow_report(first.path)
                assert first_report is not None and first_report["population"] == "cow"
            finally:
                os.umask(previous)

            previous = os.umask(0o077)
            try:
                plain_sparse_seat(repo, manual, "plain-mode-077", base, pattern)
                lease = pool.acquire_worktree_seat(repo, "cow-mode-077", base=base)
                try:
                    assert_same_seat(lease.path, manual, base)
                    assert stat.S_IMODE((lease.path / "included.txt").stat().st_mode) == 0o600
                    assert stat.S_IMODE((lease.path / "script.sh").stat().st_mode) == 0o700
                    report = cow.read_seat_cow_report(lease.path)
                    assert report is not None and report["population"] == "cow"
                    assert report["generation"] == first_report["generation"]
                finally:
                    lease.release()
            finally:
                os.umask(previous)
                first.release()


def test_cow_normalizes_symlink_and_gitlink_only_directories() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        pin_host_git(root)
        repo = root / "repo"
        repo.mkdir()
        init_repo(repo)
        link_dir = repo / "only-link"
        link_dir.mkdir()
        (link_dir / "link").symlink_to("missing-target")
        module_dir = repo / "only-module"
        module_dir.mkdir()
        (module_dir / "mod").mkdir()
        link_oid = subprocess.run(
            ["git", "hash-object", "-w", "--stdin"],
            cwd=repo,
            input=b"missing-target", stdout=subprocess.PIPE, check=True,
        ).stdout.decode().strip()
        git(
            repo,
            "update-index",
            "--add",
            "--cacheinfo",
            f"120000,{link_oid},only-link/link",
        )
        git(repo, "commit", "--allow-empty", "-m", "gitlink target")
        gitlink_oid = git(repo, "rev-parse", "HEAD")
        git(
            repo,
            "update-index",
            "--add",
            "--cacheinfo",
            f"160000,{gitlink_oid},only-module/mod",
        )
        git(repo, "config", "core.sharedRepository", "group")
        root_mode = repo.stat().st_mode

        previous = os.umask(0o077)
        try:
            cow.normalize_checkout_modes(repo)
        finally:
            os.umask(previous)
        assert stat.S_IMODE(link_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE(module_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE((module_dir / "mod").stat().st_mode) == 0o700
        assert repo.stat().st_mode == root_mode
        if callable(getattr(os, "lchmod", None)):
            assert stat.S_IMODE(os.lstat(link_dir / "link").st_mode) == 0o700


def test_shared_repository_modes_use_plain_checkout_for_extended_values() -> None:
    for shared_mode in ("group", "true", "all", "world", "everybody", "0660", "0640"):
        for umask in (0o022, 0o077):
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                repo = make_sparse_repo(root)[0]
                base = git(repo, "rev-parse", "HEAD")
                git(repo, "config", "core.sharedRepository", shared_mode)
                config = write_config(root, repo, root / "seat-sparse.pattern", cow=True)
                reference = root / "reference"
                with isolated(root, config):
                    previous = os.umask(umask)
                    try:
                        plain = subprocess.run(
                            ["git", "worktree", "add", "--detach", str(reference), base],
                            cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        )
                        assert plain.returncode == 0, plain.stderr.decode()
                        lease = pool.acquire_worktree_seat(
                            repo, f"shared-{shared_mode}-{umask:o}", base=base
                        )
                        try:
                            report = cow.read_seat_cow_report(lease.path)
                            assert report is not None
                            assert report["population"] == "plain"
                            assert report["reason"] == "shared-repository-mode-unsupported"
                            assert lease.path.stat().st_mode == reference.stat().st_mode
                        finally:
                            lease.release()
                    finally:
                        os.umask(previous)
                    subprocess.run(
                        ["git", "worktree", "remove", "--force", str(reference)],
                        cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    )


def test_template_refreshes_after_forty_commits() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        require_clonefile(root)
        repo, template, pattern = make_sparse_repo(root)
        config = write_config(root, repo, pattern, cow=True)
        leases = _Leases()
        with isolated(root, config):
            try:
                first = leases.add(
                    pool.acquire_worktree_seat(repo, "refresh-origin", base=template)
                )
                origin = cow.read_seat_cow_report(first.path)
                assert origin is not None
                assert origin["template_commit"] == template
                assert origin["behind"] == 0
                for index in range(40):
                    git(repo, "commit", "--allow-empty", "-m", f"pad {index}")
                at_forty = git(repo, "rev-parse", "HEAD")
                second = leases.add(
                    pool.acquire_worktree_seat(repo, "refresh-forty", base=at_forty)
                )
                reused = cow.read_seat_cow_report(second.path)
                assert reused is not None
                assert reused["population"] == "cow"
                assert reused["clone"] == "clonefile"
                assert reused["template_commit"] == template
                assert reused["behind"] == 40
                assert reused["generation"] == origin["generation"]
                git(repo, "commit", "--allow-empty", "-m", "past horizon")
                at_forty_one = git(repo, "rev-parse", "HEAD")
                third = leases.add(
                    pool.acquire_worktree_seat(repo, "refresh-forty-one", base=at_forty_one)
                )
                refreshed = cow.read_seat_cow_report(third.path)
                assert refreshed is not None
                assert refreshed["population"] == "cow"
                assert refreshed["template_commit"] == at_forty_one
                assert refreshed["behind"] == 0
                assert refreshed["generation"] != origin["generation"]
                assert git(third.path, "rev-parse", "HEAD") == at_forty_one
                assert not (third.path / EXCLUDED).exists()
                old = cow.generation_payload(
                    repo, origin["profile_digest"], origin["generation"]
                )
                assert not old.exists()
                new = cow.generation_payload(
                    repo, refreshed["profile_digest"], refreshed["generation"]
                )
                assert new.is_dir()
                assert not (new / ".git").exists()
            finally:
                leases.release()


def test_dirty_template_is_never_published() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        require_clonefile(root)
        repo, base, pattern = make_sparse_repo(root)
        log = install_hook(
            root,
            repo,
            "#!/bin/sh\n" + f"printf '%s\\n' \"$PWD\" >> {shlex.quote(str(root / 'hook.log'))}\n",
        )
        config = write_config(root, repo, pattern, cow=True)
        real = cow._git_ok

        def dirty_status(cwd, *args, **kwargs):
            if "status" in args:
                target = Path(cwd)
                (target / "leak.ignored").write_text("x\n", encoding="utf-8")
                (target / "UNTRACKED-LEAK.txt").write_text("y\n", encoding="utf-8")
            return real(cwd, *args, **kwargs)

        cow._git_ok = dirty_status
        try:
            with isolated(root, config):
                lease = pool.acquire_worktree_seat(repo, "dirty-template", base=base)
                try:
                    report = cow.read_seat_cow_report(lease.path)
                    assert report is not None
                    assert report["population"] == "plain"
                    assert str(report["reason"]).startswith("template-not-pristine")
                    assert git(lease.path, "rev-parse", "HEAD") == base
                    assert git(lease.path, "status", "--porcelain") == ""
                    assert not (lease.path / EXCLUDED).exists()
                    assert not (lease.path / "leak.ignored").exists()
                    assert not (lease.path / "UNTRACKED-LEAK.txt").exists()
                    assert hook_count(log, lease.path) == 1
                    assert cow.TEMPLATE_DIRNAME not in log.read_text(encoding="utf-8")
                    profile = template_root(repo)
                    assert not any(profile.rglob("current"))
                    assert not any(profile.rglob("leak.ignored"))
                    assert not any(profile.rglob("UNTRACKED-LEAK.txt"))
                finally:
                    lease.release()
        finally:
            cow._git_ok = real


def _assert_plain_fallback(repo: Path, lease, base: str, reason_prefix: str) -> None:
    report = cow.read_seat_cow_report(lease.path)
    assert report is not None, "fallback did not record evidence"
    assert report["population"] == "plain"
    assert str(report["reason"]).startswith(reason_prefix)
    assert git(lease.path, "rev-parse", "HEAD") == base
    assert git(lease.path, "status", "--porcelain") == ""
    assert (lease.path / "included.txt").is_file()
    assert not (lease.path / EXCLUDED).exists()
    assert not (lease.path / "LEAKED-COW.txt").exists()
    assert_only_seat(repo, lease.path)


def test_flag_off_does_not_reprobe_branch() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        repo, base, pattern = make_sparse_repo(root)
        config = write_config(root, repo, pattern, cow=None)
        calls = {"n": 0}
        real = pool._git_proc

        def counting(cwd, *args, **kwargs):
            if args[:1] == ("show-ref",):
                calls["n"] += 1
            return real(cwd, *args, **kwargs)

        pool._git_proc = counting
        try:
            with isolated(root, config):
                lease = pool.acquire_worktree_seat(repo, "flag-off-probe", base=base)
                try:
                    assert calls["n"] == 1
                    assert git(lease.path, "rev-parse", "HEAD") == base
                    assert cow.read_seat_cow_report(lease.path) is None
                finally:
                    lease.release()
        finally:
            pool._git_proc = real


def test_fallback_keeps_the_seat_without_a_second_probe() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        require_clonefile(root)
        repo, base, pattern = make_sparse_repo(root)
        config = write_config(root, repo, pattern, cow=True)
        calls = {"n": 0}
        real_proc = pool._git_proc
        real_clone = cow.clone_tree_payload

        def counting(cwd, *args, **kwargs):
            if args[:1] == ("show-ref",):
                calls["n"] += 1
            return real_proc(cwd, *args, **kwargs)

        def contaminate(src: Path, dst: Path) -> str:
            (dst / "LEAKED-COW.txt").write_text("leak\n", encoding="utf-8")
            raise OSError("injected clone failure")

        pool._git_proc = counting
        cow.clone_tree_payload = contaminate
        try:
            with isolated(root, config):
                lease = pool.acquire_worktree_seat(repo, "reprobe", base=base)
                try:
                    assert calls["n"] == 1
                    assert lease.path.name == "s-1"
                    _assert_plain_fallback(repo, lease, base, "clone-copy-failed")
                    assert pool._repo_clean_report(repo).clean
                    assert not any(
                        "discard" in path.name for path in lease.path.parent.iterdir()
                    )
                finally:
                    lease.release()
        finally:
            pool._git_proc = real_proc
            cow.clone_tree_payload = real_clone


def test_clone_failure_falls_back_without_residue() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        require_clonefile(root)
        repo, base, pattern = make_sparse_repo(root)
        config = write_config(root, repo, pattern, cow=True)
        real = cow.clone_tree_payload
        stale_path: dict[str, Path] = {}

        def contaminate(src: Path, dst: Path) -> str:
            (dst / "LEAKED-COW.txt").write_text("leak\n", encoding="utf-8")
            common = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir"))
            stale = common / "worktrees" / "stale-foreign"
            stale.mkdir(parents=True, exist_ok=True)
            # A present, already-known checkout stays inside the clean gate.
            # checkout_missing would refuse the plain re-add on this base.
            (stale / "gitdir").write_text(
                str(repo.resolve() / ".git") + "\n", encoding="utf-8"
            )
            (stale / "commondir").write_text("../..\n", encoding="utf-8")
            stale_path["path"] = stale
            raise OSError("injected clone failure")

        removes: list[tuple[str, ...]] = []
        real_proc = pool._git_proc

        def spy(cwd, *args, **kwargs):
            if len(args) >= 2 and args[0] == "worktree" and args[1] == "remove":
                removes.append(tuple(args))
            return real_proc(cwd, *args, **kwargs)

        cow.clone_tree_payload = contaminate
        pool._git_proc = spy
        try:
            with isolated(root, config):
                lease = pool.acquire_worktree_seat(repo, "clone-fail", base=base)
                try:
                    report = cow.read_seat_cow_report(lease.path)
                    assert report is not None
                    assert report["population"] == "plain"
                    assert str(report["reason"]).startswith("clone-copy-failed")
                    assert git(lease.path, "rev-parse", "HEAD") == base
                    assert not (lease.path / "LEAKED-COW.txt").exists()
                    assert lease.path.name == "s-1"
                    assert stale_path["path"].is_dir()
                    assert cow.TEMPLATE_DIRNAME not in " ".join(listed_worktrees(repo))
                    assert removes == []
                    assert pool._repo_clean_report(repo).clean
                    assert not any(
                        "discard" in path.name for path in lease.path.parent.iterdir()
                    )
                finally:
                    lease.release()
        finally:
            cow.clone_tree_payload = real
            pool._git_proc = real_proc


def test_read_tree_failure_falls_back_without_residue() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        require_clonefile(root)
        repo, base, pattern = make_sparse_repo(root)
        config = write_config(root, repo, pattern, cow=True)
        real = pool._git

        def fail_read_tree(cwd: Path, *args: str, **kwargs):
            if args and args[0] == "read-tree":
                raise pool.WorktreeSeatError("injected read-tree failure")
            return real(cwd, *args, **kwargs)

        pool._git = fail_read_tree
        try:
            with isolated(root, config):
                lease = pool.acquire_worktree_seat(repo, "read-tree-fail", base=base)
                try:
                    _assert_plain_fallback(repo, lease, base, "git-error")
                finally:
                    lease.release()
        finally:
            pool._git = real


def test_missing_template_falls_back_without_residue() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        repo, base, pattern = make_sparse_repo(root)
        config = write_config(root, repo, pattern, cow=True)
        real = cow.prepare_for_new_seat

        def fail_build(*_args, **_kwargs):
            return "template-build-failed: injected"

        cow.prepare_for_new_seat = fail_build
        try:
            with isolated(root, config):
                lease = pool.acquire_worktree_seat(repo, "no-template", base=base)
                try:
                    _assert_plain_fallback(repo, lease, base, "template-build-failed")
                    profile = template_root(repo)
                    if profile.exists():
                        assert not any(profile.rglob("current"))
                finally:
                    lease.release()
        finally:
            cow.prepare_for_new_seat = real


def test_non_apfs_falls_back_without_residue() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        repo, base, pattern = make_sparse_repo(root)
        config = write_config(root, repo, pattern, cow=True)
        real = cow.clonefile_support

        def not_apfs(_path: Path):
            return False, "filesystem-not-apfs: nfs", "nfs"

        cow.clonefile_support = not_apfs
        try:
            with isolated(root, config):
                lease = pool.acquire_worktree_seat(repo, "not-apfs", base=base)
                try:
                    _assert_plain_fallback(repo, lease, base, "filesystem-not-apfs")
                    assert not template_root(repo).exists()
                finally:
                    lease.release()
        finally:
            cow.clonefile_support = real


def test_missing_clone_id_is_full_copy() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        require_clonefile(root)
        src = root / "src"
        src.mkdir()
        (src / "small.txt").write_bytes(b"xyz")
        dst = root / "dst"
        dst.mkdir()
        real = cow._clone_id
        cow._clone_id = lambda _path: None
        try:
            assert cow.clone_tree_payload(src, dst) == "full-copy"
        finally:
            cow._clone_id = real


def test_flag_key_uses_resolved_repo_path() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        repo, _base, _pattern = make_sparse_repo(root)
        link = root / "repo-link"
        link.symlink_to(repo)
        config = root / "capacity.local.json"
        config.write_text(
            json.dumps({"worktrees_per_repo": 2, "seat_cow_templates": {str(link): True}}),
            encoding="utf-8",
        )
        with isolated(root, config):
            assert pool.seat_cow_enabled(repo) is True
            assert pool.seat_cow_enabled(link) is True
        config.write_text(
            json.dumps({"seat_cow_templates": {str(repo): True, str(link): False}}),
            encoding="utf-8",
        )
        stderr = io.StringIO()
        with isolated(root, config):
            with contextlib.redirect_stderr(stderr):
                assert pool.seat_cow_enabled(repo) is False
        assert "conflicting values" in stderr.getvalue()


def test_retired_profile_keeps_lock_inode_and_drops_crashed_staging() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        require_clonefile(root)
        repo, base, pattern = make_sparse_repo(root)
        common = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        stale = common / "worktrees" / "stale-foreign"
        stale.mkdir(parents=True)
        (stale / "gitdir").write_text(str(repo.resolve() / ".git") + "\n", encoding="utf-8")
        (stale / "commondir").write_text("../..\n", encoding="utf-8")
        digest = hashlib.sha256(PATTERN_TEXT.encode("utf-8")).hexdigest()
        crashed = cow.profile_dir(repo, digest) / "building" / "gen-crashed"
        crashed.mkdir(parents=True)
        (crashed / "LEAKED-STAGING.txt").write_text("x\n", encoding="utf-8")
        config = write_config(root, repo, pattern, cow=True)
        leases = _Leases()
        try:
            with isolated(root, config):
                first = leases.add(
                    pool.acquire_worktree_seat(repo, "profile-first", base=base)
                )
                report = cow.read_seat_cow_report(first.path)
                assert report is not None
                assert report["population"] == "cow"
                assert report["profile_digest"] == digest
                assert not crashed.exists()
                assert stale.is_dir()
                assert "gen-crashed" not in admin_names(repo)
                lock = cow.profile_lock_path(repo, digest)
                inode = lock.stat().st_ino
                pattern.write_text(PATTERN_TEXT + "!/unconfigured-name\n", encoding="utf-8")
                second = leases.add(
                    pool.acquire_worktree_seat(repo, "profile-second", base=base)
                )
                later = cow.read_seat_cow_report(second.path)
                assert later is not None
                assert later["population"] == "cow"
                assert later["profile_digest"] != digest
                reader_lock = cow.generation_lock_path(
                    repo, digest, report["generation"]
                )
                assert not cow.profile_dir(repo, digest).exists()
                assert not reader_lock.exists()
                assert lock.stat().st_ino == inode
                first_fd = os.open(lock, os.O_RDWR)
                second_fd = os.open(lock, os.O_RDWR)
                try:
                    fcntl.flock(first_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    with pytest.raises(BlockingIOError):
                        fcntl.flock(second_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    os.close(first_fd)
                    os.close(second_fd)
                assert cow.profile_dir(repo, later["profile_digest"]).is_dir()
                assert stale.is_dir()
        finally:
            leases.release()


def test_evidence_write_failure_keeps_verified_seat() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        require_clonefile(root)
        repo, base, pattern = make_sparse_repo(root)
        log = install_hook(
            root,
            repo,
            "#!/bin/sh\n" + f"printf '%s\\n' \"$PWD\" >> {shlex.quote(str(root / 'hook.log'))}\n",
        )
        config = write_config(root, repo, pattern, cow=True)
        real = cow.write_seat_cow_report
        seen: list[str] = []

        def fail_cow_report(worktree: Path, payload: dict) -> None:
            seen.append(str(payload.get("population")))
            if payload.get("population") == "cow":
                raise OSError("injected evidence failure")
            real(worktree, payload)

        cow.write_seat_cow_report = fail_cow_report
        stderr = io.StringIO()
        try:
            with isolated(root, config):
                with contextlib.redirect_stderr(stderr):
                    lease = pool.acquire_worktree_seat(repo, "evidence-fail", base=base)
                try:
                    assert seen == ["cow"]
                    assert git(lease.path, "rev-parse", "HEAD") == base
                    assert git(lease.path, "status", "--porcelain") == ""
                    assert not (lease.path / EXCLUDED).exists()
                    assert cow.read_seat_cow_report(lease.path) is None
                    assert hook_count(log, lease.path) == 1
                    assert "evidence write failed" in stderr.getvalue()
                finally:
                    lease.release()
        finally:
            cow.write_seat_cow_report = real


def _gitdir_admin(worktree: Path) -> Path:
    text = (worktree / ".git").read_text(encoding="utf-8")
    git_dir = Path(text.split(":", 1)[1].strip())
    if not git_dir.is_absolute():
        git_dir = worktree / git_dir
    return git_dir.resolve()


def _chflags(path: Path, flag: str) -> bool:
    result = subprocess.run(
        ["chflags", flag, str(path)],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.returncode == 0


def _skip_reason(lease, seat_name: str) -> str:
    matches = [reason for name, reason in lease.skipped_slots if name == seat_name]
    assert matches, lease.skipped_slots
    return matches[0]


def test_repair_failure_skips_slot_without_removing_it() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        require_clonefile(root)
        repo, base, pattern = make_sparse_repo(root)
        log = install_hook(
            root,
            repo,
            "#!/bin/sh\n" + f"printf '%s\\n' \"$PWD\" >> {shlex.quote(str(root / 'hook.log'))}\n",
        )
        config = write_config(root, repo, pattern, cow=True, seats=4)
        real_clone = cow.clone_tree_payload
        real_clear = pool._clear_fresh_seat_payload
        state = {"clones": 0}

        def contaminate(src: Path, dst: Path) -> str:
            state["clones"] += 1
            (dst / "LEAKED-COW.txt").write_text("leak\n", encoding="utf-8")
            raise OSError("injected clone failure")

        def fail_clear(worktree_path: Path) -> None:
            raise OSError("injected repair failure")

        cow.clone_tree_payload = contaminate
        pool._clear_fresh_seat_payload = fail_clear
        try:
            with isolated(root, config):
                with pytest.raises(pool.WorktreeSeatUnavailable) as caught:
                    pool.acquire_worktree_seat(repo, "repair-fail", base=base)
                stuck = repo / "worktrees" / "s-1"
                skipped = caught.value.skipped_slots
                assert any(
                    name == "s-1" and "operator must inspect" in reason
                    for name, reason in skipped
                ), skipped
                text = str(caught.value)
                assert "seat_preparation_failed" not in text
                assert "repo_not_clean" not in text
                assert stuck.is_dir()
                assert (stuck / ".git").is_file()
                assert (stuck / "LEAKED-COW.txt").is_file()
                admin = _gitdir_admin(stuck)
                assert admin.is_dir()
                assert (admin / "gitdir").is_file()
                assert git(stuck, "rev-parse", "--abbrev-ref", "HEAD") == (
                    pool.seat_branch_name("repair-fail")
                )
                assert hook_count(log, stuck) == 0
                assert pool._repo_clean_report(repo).clean
                assert not any("discard" in path.name for path in stuck.parent.iterdir())
                assert not (repo / "worktrees" / "s-2").exists()
        finally:
            cow.clone_tree_payload = real_clone
            pool._clear_fresh_seat_payload = real_clear


def test_undeletable_checkout_stays_put_and_keeps_the_repo_clean() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        require_clonefile(root)
        repo, base, pattern = make_sparse_repo(root)
        config = write_config(root, repo, pattern, cow=True, seats=4)
        real_clone = cow.clone_tree_payload
        state = {"clones": 0}
        flagged: list[Path] = []

        def contaminate(src: Path, dst: Path) -> str:
            state["clones"] += 1
            if state["clones"] == 1:
                (dst / "LEAKED-COW.txt").write_text("leak\n", encoding="utf-8")
                if not _chflags(dst, "uchg"):
                    pytest.skip("cannot mark a directory undeletable")
                flagged.append(dst)
                raise OSError("injected clone failure")
            return real_clone(src, dst)

        cow.clone_tree_payload = contaminate
        try:
            with isolated(root, config):
                with pytest.raises(pool.WorktreeSeatUnavailable) as caught:
                    pool.acquire_worktree_seat(repo, "uchg-checkout", base=base)
                stuck = repo / "worktrees" / "s-1"
                assert stuck.is_dir()
                assert (stuck / "LEAKED-COW.txt").is_file()
                admin = _gitdir_admin(stuck)
                assert admin.is_dir()
                assert (admin / "gitdir").is_file()
                assert not (repo / "worktrees" / "s-2").exists()
                text = str(caught.value)
                assert "operator must inspect" in text
                assert "repo_not_clean" not in text
                assert "prune" not in text
                assert pool._repo_clean_report(repo).clean
                assert not any("discard" in path.name for path in stuck.parent.iterdir())
        finally:
            cow.clone_tree_payload = real_clone
            for path in flagged:
                _chflags(path, "nouchg")


def test_undeletable_admin_file_repairs_the_same_seat() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        require_clonefile(root)
        repo, base, pattern = make_sparse_repo(root)
        config = write_config(root, repo, pattern, cow=True, seats=4)
        real_clone = cow.clone_tree_payload
        state = {"clones": 0}
        flagged: list[Path] = []

        def contaminate(src: Path, dst: Path) -> str:
            state["clones"] += 1
            if state["clones"] == 1:
                (dst / "LEAKED-COW.txt").write_text("leak\n", encoding="utf-8")
                protected = _gitdir_admin(dst) / "commondir"
                if not _chflags(protected, "uchg"):
                    pytest.skip("cannot mark an admin file undeletable")
                flagged.append(protected)
                raise OSError("injected clone failure")
            return real_clone(src, dst)

        cow.clone_tree_payload = contaminate
        try:
            with isolated(root, config):
                lease = pool.acquire_worktree_seat(repo, "uchg-admin", base=base)
                try:
                    assert lease.path.name == "s-1"
                    _assert_plain_fallback(repo, lease, base, "clone-copy-failed")
                    admin = _gitdir_admin(lease.path)
                    assert admin.is_dir()
                    assert (admin / "gitdir").is_file()
                    assert (admin / "commondir").is_file()
                    assert pool._repo_clean_report(repo).clean
                    assert not any(
                        "discard" in path.name for path in lease.path.parent.iterdir()
                    )
                finally:
                    lease.release()
        finally:
            cow.clone_tree_payload = real_clone
            for path in flagged:
                _chflags(path, "nouchg")


def make_eol_repo(root: Path) -> tuple[Path, str, str, Path]:
    pin_host_git(root)
    repo = root / "repo"
    repo.mkdir()
    init_repo(repo)
    (repo / "included.txt").write_text("line\n", encoding="utf-8")
    excluded = repo / "docs-private" / "build" / "remote-pytest"
    excluded.mkdir(parents=True)
    (excluded / "evidence.txt").write_text("heavy\n", encoding="utf-8")
    git(repo, "add", "--", ".")
    git(repo, "commit", "-m", "template without attributes")
    template = git(repo, "rev-parse", "HEAD")
    (repo / ".gitattributes").write_text("*.txt text eol=crlf\n", encoding="utf-8")
    git(repo, "add", "--", ".gitattributes")
    git(repo, "commit", "-m", "crlf base")
    pattern = root / "seat-sparse.pattern"
    pattern.write_text(PATTERN_TEXT, encoding="utf-8")
    return repo.resolve(), template, git(repo, "rev-parse", "HEAD"), pattern


def test_attributes_change_matches_plain_seat_bytes() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        require_clonefile(root)
        repo, template, base, pattern = make_eol_repo(root)
        assert git(repo, "rev-parse", f"{template}:included.txt") == git(
            repo, "rev-parse", f"{base}:included.txt"
        )
        config = write_config(root, repo, pattern, cow=True)
        manual = root / "manual"
        leases = _Leases()
        with isolated(root, config):
            try:
                leases.add(pool.acquire_worktree_seat(repo, "eol-template", base=template))
                seat = leases.add(pool.acquire_worktree_seat(repo, "eol-base", base=base))
                plain_sparse_seat(repo, manual, "manual-eol", base, pattern)
                assert_same_seat(seat.path, manual, base)
                assert (seat.path / "included.txt").read_bytes() == b"line\r\n"
                assert (manual / "included.txt").read_bytes() == b"line\r\n"
                report = cow.read_seat_cow_report(seat.path)
                assert report is not None
                assert report["population"] == "plain"
                assert str(report["reason"]).startswith("conversion:text")
            finally:
                leases.release()


def test_generation_lookup_failure_falls_back() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        require_clonefile(root)
        repo, template, pattern = make_sparse_repo(root)
        config = write_config(root, repo, pattern, cow=True)
        calls = {"n": 0}
        real = cow._git_ok

        def flaky(cwd, *args, **kwargs):
            if args[:2] == ("rev-list", "--count"):
                calls["n"] += 1
                if calls["n"] >= 2:
                    raise OSError("git rev-list --count failed: injected")
            return real(cwd, *args, **kwargs)

        cow._git_ok = flaky
        leases = _Leases()
        try:
            with isolated(root, config):
                try:
                    leases.add(
                        pool.acquire_worktree_seat(repo, "lookup-origin", base=template)
                    )
                    git(repo, "commit", "--allow-empty", "-m", "ahead")
                    ahead = git(repo, "rev-parse", "HEAD")
                    seat = leases.add(
                        pool.acquire_worktree_seat(repo, "lookup-ahead", base=ahead)
                    )
                    report = cow.read_seat_cow_report(seat.path)
                    assert report is not None
                    assert report["population"] == "plain"
                    assert str(report["reason"]).startswith("git-error")
                    assert git(seat.path, "rev-parse", "HEAD") == ahead
                    assert git(seat.path, "status", "--porcelain") == ""
                    assert not (seat.path / EXCLUDED).exists()
                    assert calls["n"] >= 2
                finally:
                    leases.release()
        finally:
            cow._git_ok = real


def test_concurrent_build_does_not_tear_a_clone() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        require_clonefile(root)
        repo, template, pattern = make_sparse_repo(root)
        config = write_config(root, repo, pattern, cow=True)
        with isolated(root, config):
            assert cow.prepare_for_new_seat(repo, base_commit=template, pattern=PATTERN_TEXT) is None
            generation = cow.open_usable_generation(
                repo, base_commit=template, pattern=PATTERN_TEXT
            )
            assert generation is not None
            snapshot = manifest(generation.payload)
            for index in range(41):
                git(repo, "commit", "--allow-empty", "-m", f"ahead {index}")
            ahead = git(repo, "rev-parse", "HEAD")
            errors: list[BaseException] = []

            def refresh() -> None:
                try:
                    reason = cow.prepare_for_new_seat(
                        repo, base_commit=ahead, pattern=PATTERN_TEXT
                    )
                    if reason:
                        raise AssertionError(reason)
                except BaseException as exc:
                    errors.append(exc)

            worker = threading.Thread(target=refresh)
            worker.start()
            worker.join()
            assert errors == []
            assert generation.payload.is_dir()
            held_lock = cow.generation_lock_path(
                repo, generation.digest, generation.gen_id
            )
            assert held_lock.is_file()
            destination = root / "clone"
            destination.mkdir()
            kind = cow.clone_tree_payload(generation.payload, destination)
            assert kind == "clonefile"
            assert manifest(destination) == snapshot
            old_payload = generation.payload
            generation.release()
            assert cow.prepare_for_new_seat(repo, base_commit=ahead, pattern=PATTERN_TEXT) is None
            assert not old_payload.exists()
            assert not held_lock.exists()


def test_refresh_failure_repairs_in_place() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        require_clonefile(root)
        repo, base, pattern = make_sparse_repo(root)
        config = write_config(root, repo, pattern, cow=True)
        real = pool._git_proc

        def fail_refresh(cwd, *args, **kwargs):
            if args and args[0] == "update-index":
                return subprocess.CompletedProcess(
                    args=["git", *args],
                    returncode=2,
                    stdout="",
                    stderr="injected refresh failure",
                )
            return real(cwd, *args, **kwargs)

        pool._git_proc = fail_refresh
        try:
            with isolated(root, config):
                lease = pool.acquire_worktree_seat(repo, "refresh-fail", base=base)
                try:
                    assert lease.path.name == "s-1"
                    _assert_plain_fallback(repo, lease, base, "git-error")
                    manual = root / "manual"
                    plain_sparse_seat(repo, manual, "manual-refresh", base, pattern)
                    assert_same_seat(lease.path, manual, base)
                    assert pool._repo_clean_report(repo).clean
                finally:
                    lease.release()
        finally:
            pool._git_proc = real


def test_failing_post_checkout_hook_runs_once() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        require_clonefile(root)
        repo, base, pattern = make_sparse_repo(root)
        log = install_hook(
            root,
            repo,
            "#!/bin/sh\n"
            + f"printf '%s\\n' \"$PWD\" >> {shlex.quote(str(root / 'hook.log'))}\n"
            + "exit 1\n",
        )
        config = write_config(root, repo, pattern, cow=True, seats=4)
        with isolated(root, config):
            with pytest.raises(pool.WorktreeSeatUnavailable) as caught:
                pool.acquire_worktree_seat(repo, "hook-fail", base=base)
            stuck = repo / "worktrees" / "s-1"
            assert stuck.is_dir()
            assert (stuck / ".git").is_file()
            assert hook_count(log, stuck) == 1
            assert any(
                name == "s-1" and "operator must inspect" in reason
                for name, reason in caught.value.skipped_slots
            )
            text = str(caught.value)
            assert "seat_preparation_failed" not in text
            assert "repo_not_clean" not in text
            assert pool._repo_clean_report(repo).clean
            assert not any("discard" in path.name for path in stuck.parent.iterdir())


def test_verification_failure_falls_back_to_full_seat() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        require_clonefile(root)
        repo, base, pattern = make_sparse_repo(root)
        log = install_hook(
            root,
            repo,
            "#!/bin/sh\n" + f"printf '%s\\n' \"$PWD\" >> {shlex.quote(str(root / 'hook.log'))}\n",
        )
        config = write_config(root, repo, pattern, cow=True, seats=4)
        real = pool._git
        state = {"status": 0, "checkout": 0}

        def fail_first_status(cwd, *args, **kwargs):
            if (
                len(args) >= 3
                and args[0] == "-c"
                and args[2] == "checkout"
                and "worktrees" in str(cwd)
            ):
                state["checkout"] += 1
            if args[:2] == ("status", "--porcelain") and "worktrees" in str(cwd):
                state["status"] += 1
                if state["status"] == 1:
                    raise pool.WorktreeSeatError("injected verification failure")
            return real(cwd, *args, **kwargs)

        pool._git = fail_first_status
        try:
            with isolated(root, config):
                lease = pool.acquire_worktree_seat(repo, "verify-fail", base=base)
                try:
                    assert git(lease.path, "rev-parse", "HEAD") == base
                    assert not git(lease.path, "status", "--porcelain")
                    assert "S " not in git(lease.path, "ls-files", "-t")
                    assert (
                        lease.path / "docs-private" / "build" / "remote-pytest" / "evidence.txt"
                    ).is_file()
                    report = cow.read_seat_cow_report(lease.path)
                    assert report is not None and report["population"] == "plain"
                    assert state["checkout"] == 2
                    assert hook_count(log, lease.path) == 2
                finally:
                    lease.release()
                assert pool._repo_clean_report(repo).clean
        finally:
            pool._git = real


def test_persistent_checkout_failure_skips_without_removing_the_seat() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        require_clonefile(root)
        repo, base, pattern = make_sparse_repo(root)
        config = write_config(root, repo, pattern, cow=True, seats=4)
        real = pool._git

        def always_fail(cwd, *args, **kwargs):
            if (
                len(args) >= 3
                and args[0] == "-c"
                and args[2] == "checkout"
                and "worktrees" in str(cwd)
            ):
                raise pool.WorktreeSeatError("injected checkout failure")
            return real(cwd, *args, **kwargs)

        pool._git = always_fail
        try:
            with isolated(root, config):
                with pytest.raises(pool.WorktreeSeatUnavailable) as caught:
                    pool.acquire_worktree_seat(repo, "checkout-stuck", base=base)
                stuck = repo / "worktrees" / "s-1"
                assert stuck.is_dir()
                assert (stuck / ".git").is_file()
                admin = _gitdir_admin(stuck)
                assert (admin / "gitdir").is_file()
                text = str(caught.value)
                assert "seat_preparation_failed" not in text
                assert "repo_not_clean" not in text
                assert any(
                    "operator must inspect" in reason
                    for _name, reason in caught.value.skipped_slots
                )
                assert pool._repo_clean_report(repo).clean
                assert not any("discard" in path.name for path in stuck.parent.iterdir())
        finally:
            pool._git = real


def test_info_attributes_filter_uses_plain() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        require_clonefile(root)
        repo, base, pattern = make_sparse_repo(root)
        config = write_config(root, repo, pattern, cow=True)
        common = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        info = common / "info"
        info.mkdir(parents=True, exist_ok=True)
        (info / "attributes").write_text("*.txt filter=mark\n", encoding="utf-8")
        git(repo, "config", "filter.mark.smudge", "sed 's/^/MARK:/'")
        git(repo, "config", "filter.mark.clean", "sed 's/^MARK://'")
        manual = root / "manual"
        leases = _Leases()
        with isolated(root, config):
            try:
                seat = leases.add(
                    pool.acquire_worktree_seat(repo, "info-filter", base=base)
                )
                plain_sparse_seat(repo, manual, "manual-info", base, pattern)
                assert_same_seat(seat.path, manual, base)
                assert (seat.path / "included.txt").read_bytes().startswith(b"MARK:")
                report = cow.read_seat_cow_report(seat.path)
                assert report is not None
                assert report["population"] == "plain"
                assert str(report["reason"]).startswith("conversion:filter")
            finally:
                leases.release()


def test_filter_named_unspecified_uses_plain() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        require_clonefile(root)
        repo, _base, pattern = make_sparse_repo(root)
        (repo / ".gitattributes").write_text("*.txt filter=unspecified\n", encoding="utf-8")
        git(repo, "add", "--", ".gitattributes")
        git(repo, "commit", "-m", "driver named unspecified")
        base = git(repo, "rev-parse", "HEAD")
        git(repo, "config", "filter.unspecified.smudge", "sed 's/^/SMUDGE:/'")
        git(repo, "config", "filter.unspecified.clean", "sed 's/^SMUDGE://'")
        config = write_config(root, repo, pattern, cow=True)
        manual = root / "manual"
        leases = _Leases()
        with isolated(root, config):
            try:
                seat = leases.add(
                    pool.acquire_worktree_seat(repo, "filter-unspecified", base=base)
                )
                plain_sparse_seat(repo, manual, "manual-unspecified", base, pattern)
                assert_same_seat(seat.path, manual, base)
                assert (seat.path / "included.txt").read_bytes().startswith(b"SMUDGE:")
                report = cow.read_seat_cow_report(seat.path)
                assert report is not None
                assert report["population"] == "plain"
                assert str(report["reason"]).startswith("conversion:filter")
            finally:
                leases.release()


def test_global_attributes_eol_uses_plain() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        require_clonefile(root)
        repo, base, pattern = make_sparse_repo(root)
        config = write_config(root, repo, pattern, cow=True)
        xdg = root / "xdg"
        (xdg / "git").mkdir(parents=True)
        (xdg / "git" / "attributes").write_text("*.txt eol=crlf\n", encoding="utf-8")
        prior = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = str(xdg)
        manual = root / "manual"
        leases = _Leases()
        try:
            with isolated(root, config):
                try:
                    seat = leases.add(
                        pool.acquire_worktree_seat(repo, "global-eol", base=base)
                    )
                    plain_sparse_seat(repo, manual, "manual-global", base, pattern)
                    assert_same_seat(seat.path, manual, base)
                    assert (seat.path / "included.txt").read_bytes() == b"keep me\r\n"
                    report = cow.read_seat_cow_report(seat.path)
                    assert report is not None
                    assert report["population"] == "plain"
                    assert str(report["reason"]).startswith("conversion:eol")
                finally:
                    leases.release()
        finally:
            if prior is None:
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = prior


def test_autocrlf_uses_plain() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        require_clonefile(root)
        repo, base, pattern = make_sparse_repo(root)
        git(repo, "config", "core.autocrlf", "true")
        config = write_config(root, repo, pattern, cow=True)
        manual = root / "manual"
        leases = _Leases()
        with isolated(root, config):
            try:
                seat = leases.add(
                    pool.acquire_worktree_seat(repo, "autocrlf", base=base)
                )
                plain_sparse_seat(repo, manual, "manual-autocrlf", base, pattern)
                assert_same_seat(seat.path, manual, base)
                assert (seat.path / "included.txt").read_bytes() == b"keep me\r\n"
                report = cow.read_seat_cow_report(seat.path)
                assert report is not None
                assert report["population"] == "plain"
                assert str(report["reason"]).startswith("conversion:autocrlf")
            finally:
                leases.release()


def test_attr_tree_uses_plain() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        require_clonefile(root)
        repo, _base, pattern = make_sparse_repo(root)
        (repo / ".gitattributes").write_text("*.txt eol=crlf\n", encoding="utf-8")
        git(repo, "add", "--", ".gitattributes")
        git(repo, "commit", "-m", "attr tree")
        attr = git(repo, "rev-parse", "HEAD")
        git(repo, "rm", "-f", "--", ".gitattributes")
        git(repo, "commit", "-m", "base without attributes")
        base = git(repo, "rev-parse", "HEAD")
        git(repo, "config", "attr.tree", attr)
        config = write_config(root, repo, pattern, cow=True)
        manual = root / "manual"
        leases = _Leases()
        with isolated(root, config):
            try:
                seat = leases.add(
                    pool.acquire_worktree_seat(repo, "attr-tree", base=base)
                )
                plain_sparse_seat(repo, manual, "manual-attr-tree", base, pattern)
                assert_same_seat(seat.path, manual, base)
                assert (seat.path / "included.txt").read_bytes() == b"keep me\r\n"
                report = cow.read_seat_cow_report(seat.path)
                assert report is not None
                assert report["population"] == "plain"
                assert str(report["reason"]).startswith("conversion:attr-tree")
            finally:
                leases.release()


def test_quoted_attribute_uses_plain() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        require_clonefile(root)
        repo, _base, pattern = make_sparse_repo(root)
        (repo / ".gitattributes").write_text(
            '* note=" text eol=crlf\n', encoding="utf-8"
        )
        git(repo, "add", "--", ".gitattributes")
        git(repo, "commit", "-m", "quoted attributes")
        base = git(repo, "rev-parse", "HEAD")
        config = write_config(root, repo, pattern, cow=True)
        manual = root / "manual"
        leases = _Leases()
        with isolated(root, config):
            try:
                seat = leases.add(
                    pool.acquire_worktree_seat(repo, "quoted-attr", base=base)
                )
                plain_sparse_seat(repo, manual, "manual-quoted", base, pattern)
                assert_same_seat(seat.path, manual, base)
                assert (seat.path / "included.txt").read_bytes() == b"keep me\r\n"
                report = cow.read_seat_cow_report(seat.path)
                assert report is not None
                assert report["population"] == "plain"
                assert str(report["reason"]).startswith("conversion:")
            finally:
                leases.release()


def test_bare_cr_attribute_separator_uses_plain() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        require_clonefile(root)
        repo, _base, pattern = make_sparse_repo(root)
        (repo / ".gitattributes").write_bytes(b"*.txt\reol=crlf\n")
        git(repo, "add", "--", ".gitattributes")
        git(repo, "commit", "-m", "bare cr separator")
        base = git(repo, "rev-parse", "HEAD")
        config = write_config(root, repo, pattern, cow=True)
        manual = root / "manual"
        leases = _Leases()
        with isolated(root, config):
            try:
                seat = leases.add(
                    pool.acquire_worktree_seat(repo, "bare-cr", base=base)
                )
                plain_sparse_seat(repo, manual, "manual-cr", base, pattern)
                assert_same_seat(seat.path, manual, base)
                assert (seat.path / "included.txt").read_bytes() == b"keep me\r\n"
                report = cow.read_seat_cow_report(seat.path)
                assert report is not None
                assert report["population"] == "plain"
                assert str(report["reason"]).startswith("conversion:eol")
            finally:
                leases.release()


def test_attributes_file_trailing_space_uses_plain() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        require_clonefile(root)
        repo, base, pattern = make_sparse_repo(root)
        real = root / "attributes "
        real.write_text("*.txt eol=crlf\n", encoding="utf-8")
        (root / "attributes").write_text("# no conversion\n", encoding="utf-8")
        git(repo, "config", "core.attributesFile", str(real))
        shown = subprocess.run(
            ["git", "config", "--get", "core.attributesFile"],
            cwd=str(repo),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        assert shown.stdout == str(real).encode() + b"\n"
        config = write_config(root, repo, pattern, cow=True)
        manual = root / "manual"
        leases = _Leases()
        with isolated(root, config):
            try:
                seat = leases.add(
                    pool.acquire_worktree_seat(repo, "attr-space", base=base)
                )
                plain_sparse_seat(repo, manual, "manual-space", base, pattern)
                assert_same_seat(seat.path, manual, base)
                assert (seat.path / "included.txt").read_bytes() == b"keep me\r\n"
                report = cow.read_seat_cow_report(seat.path)
                assert report is not None
                assert report["population"] == "plain"
                assert str(report["reason"]).startswith("conversion:eol")
            finally:
                leases.release()


def test_includeif_symlinks_false_uses_plain() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        require_clonefile(root)
        repo, _base, pattern = make_sparse_repo(root)
        (repo / "link").symlink_to("included.txt")
        git(repo, "add", "--", "link")
        git(repo, "commit", "-m", "symlink")
        base = git(repo, "rev-parse", "HEAD")
        extra = root / "seat-symlinks.gitconfig"
        extra.write_text("[core]\n\tsymlinks = false\n", encoding="utf-8")
        common = git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
        git(repo, "config", f"includeIf.gitdir:{common}/worktrees/.path", str(extra))
        config = write_config(root, repo, pattern, cow=True)
        manual = root / "manual"
        leases = _Leases()
        with isolated(root, config):
            try:
                seat = leases.add(
                    pool.acquire_worktree_seat(repo, "includeif-symlinks", base=base)
                )
                plain_sparse_seat(repo, manual, "manual-symlinks", base, pattern)
                assert_same_seat(seat.path, manual, base)
                link = seat.path / "link"
                assert link.is_file() and not link.is_symlink()
                assert link.read_bytes() == b"included.txt"
                report = cow.read_seat_cow_report(seat.path)
                assert report is not None
                assert report["population"] == "plain"
                assert str(report["reason"]).startswith("conversion:symlinks")
                digest = hashlib.sha256(PATTERN_TEXT.encode("utf-8")).hexdigest()
                gen_id = (cow.profile_dir(repo, digest) / "current").read_text(
                    encoding="utf-8"
                ).strip()
                template_link = cow.generation_payload(repo, digest, gen_id) / "link"
                assert template_link.is_symlink()
                assert os.readlink(template_link) == "included.txt"
            finally:
                leases.release()


def test_smudge_filter_config_uses_plain() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        require_clonefile(root)
        repo, base, pattern = make_sparse_repo(root)
        (repo / ".gitattributes").write_text("*.txt filter=mark\n", encoding="utf-8")
        git(repo, "add", "--", ".gitattributes")
        git(repo, "commit", "-m", "mark filter attribute")
        base = git(repo, "rev-parse", "HEAD")
        config = write_config(root, repo, pattern, cow=True)
        leases = _Leases()
        with isolated(root, config):
            try:
                origin = leases.add(
                    pool.acquire_worktree_seat(repo, "smudge-origin", base=base)
                )
                origin_report = cow.read_seat_cow_report(origin.path)
                assert origin_report is not None
                assert origin_report["population"] == "plain"
                assert str(origin_report["reason"]).startswith("conversion:filter")
                manual_raw = root / "manual-raw"
                plain_sparse_seat(repo, manual_raw, "manual-smudge-raw", base, pattern)
                assert_same_seat(origin.path, manual_raw, base)
                git(repo, "config", "filter.mark.smudge", "sed 's/^/OLD:/'")
                git(repo, "config", "filter.mark.clean", "sed 's/^OLD://'")
                changed = leases.add(
                    pool.acquire_worktree_seat(repo, "smudge-old", base=base)
                )
                manual = root / "manual-old"
                plain_sparse_seat(repo, manual, "manual-smudge-old", base, pattern)
                assert_same_seat(changed.path, manual, base)
                assert (changed.path / "included.txt").read_bytes().startswith(b"OLD:")
                report = cow.read_seat_cow_report(changed.path)
                assert report is not None
                assert report["population"] == "plain"
                assert str(report["reason"]).startswith("conversion:filter")
                git(repo, "config", "filter.mark.smudge", "sed 's/^/NEW:/'")
                git(repo, "config", "filter.mark.clean", "sed 's/^NEW://'")
                renamed = leases.add(
                    pool.acquire_worktree_seat(repo, "smudge-new", base=base)
                )
                manual_new = root / "manual-new"
                plain_sparse_seat(repo, manual_new, "manual-smudge-new", base, pattern)
                assert_same_seat(renamed.path, manual_new, base)
                assert (renamed.path / "included.txt").read_bytes().startswith(b"NEW:")
                later = cow.read_seat_cow_report(renamed.path)
                assert later is not None
                assert later["population"] == "plain"
                assert str(later["reason"]).startswith("conversion:filter")
            finally:
                leases.release()


def test_missing_reader_lock_is_not_recreated() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        require_clonefile(root)
        repo, base, pattern = make_sparse_repo(root)
        config = write_config(root, repo, pattern, cow=True)
        with isolated(root, config):
            assert (
                cow.prepare_for_new_seat(repo, base_commit=base, pattern=PATTERN_TEXT)
                is None
            )
            generation = cow.open_usable_generation(
                repo, base_commit=base, pattern=PATTERN_TEXT
            )
            assert generation is not None
            lock = cow.generation_lock_path(repo, generation.digest, generation.gen_id)
            assert lock.is_file()
            old_payload = generation.payload
            generation.release()
            os.unlink(lock)
            parent = lock.parent
            again = cow.open_usable_generation(
                repo, base_commit=base, pattern=PATTERN_TEXT
            )
            assert again is None
            assert not lock.exists()
            assert old_payload.is_dir()
            cow._rm_real_dir(parent)
            absent = cow.open_usable_generation(
                repo, base_commit=base, pattern=PATTERN_TEXT
            )
            assert absent is None
            assert not parent.exists()
            assert (
                cow.prepare_for_new_seat(repo, base_commit=base, pattern=PATTERN_TEXT)
                is None
            )
            assert not old_payload.exists()
            rebuilt = cow.open_usable_generation(
                repo, base_commit=base, pattern=PATTERN_TEXT
            )
            assert rebuilt is not None
            assert rebuilt.gen_id != generation.gen_id
            assert cow.generation_lock_path(
                repo, rebuilt.digest, rebuilt.gen_id
            ).is_file()
            rebuilt.release()


def test_measured_write_bytes_for_three_thousand_file_seat() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        require_clonefile(root)
        pin_host_git(root)
        plain_repo = root / "plain-repo"
        plain_repo.mkdir()
        init_repo(plain_repo)
        files = plain_repo / "payload"
        files.mkdir()
        for index in range(3_000):
            (files / f"file-{index:04d}.bin").write_bytes(os.urandom(4096))
        git(plain_repo, "add", "--", "payload")
        git(plain_repo, "commit", "-m", "large seat fixture")
        base = git(plain_repo, "rev-parse", "HEAD")
        cow_repo = root / "cow-repo"
        git(root, "clone", "--shared", str(plain_repo), str(cow_repo))
        git(cow_repo, "config", "extensions.worktreeConfig", "true")
        pattern = "/*\n"
        config = root / "capacity.local.json"
        config.write_text(json.dumps({"worktrees_per_repo": 2}), encoding="utf-8")

        def private_payload_bytes(tree: Path) -> int:
            total = 0
            for current, dirs, names in os.walk(tree):
                dirs[:] = [name for name in dirs if name != ".git"]
                for name in names:
                    path = Path(current) / name
                    if path.name != ".git" and path.is_file() and not path.is_symlink():
                        total += dedupe._private_bytes(path)
            return total

        with isolated(root, config):
            plain_bytes = 0
            cold_template_bytes = 0
            first_seat_private_bytes = 0
            warm_seat_private_bytes = 0
            plain_lease = None
            cow_lease = None
            warm_lease = None
            try:
                plain_lease = pool.acquire_worktree_seat(
                    plain_repo, "write-measure-plain", base=base
                )
                plain_bytes = private_payload_bytes(plain_lease.path)
                plain_lease.release()
                git(plain_repo, "worktree", "remove", "--force", str(plain_lease.path))
                plain_lease = None

                config.write_text(
                    json.dumps({
                        "worktrees_per_repo": 2,
                        "seat_cow_templates": {str(cow_repo): True},
                    }),
                    encoding="utf-8",
                )
                assert cow.prepare_for_new_seat(
                    cow_repo, base_commit=base, pattern=pattern
                ) is None
                digest = cow.profile_digest(pattern)
                generation_id = (
                    cow.profile_dir(cow_repo, digest) / "current"
                ).read_text(encoding="utf-8").strip()
                template = cow.generation_payload(cow_repo, digest, generation_id)
                cold_template_bytes = private_payload_bytes(template)
                cow_lease = pool.acquire_worktree_seat(
                    cow_repo, "write-measure-cow", base=base
                )
                report = cow.read_seat_cow_report(cow_lease.path)
                assert report is not None
                assert report["population"] == "cow"
                assert report["operation"] == "new"
                assert report["generation"] == generation_id
                first_seat_private_bytes = private_payload_bytes(cow_lease.path)
                warm_lease = pool.acquire_worktree_seat(
                    cow_repo, "write-measure-warm", base=base
                )
                warm_seat_private_bytes = private_payload_bytes(warm_lease.path)
                cumulative_payload_allocation = (
                    cold_template_bytes
                    + first_seat_private_bytes
                    + warm_seat_private_bytes
                )
                print(
                    f"PRIVATE_BYTES files=3000 plain_payload={plain_bytes} "
                    f"template_cold_before_clone={cold_template_bytes} "
                    f"first_seat={first_seat_private_bytes} "
                    f"warm_seat={warm_seat_private_bytes} "
                    f"cumulative_payload_allocation={cumulative_payload_allocation}"
                )
                assert plain_bytes > 8 * 1024 * 1024
                assert cold_template_bytes >= plain_bytes * 0.8
                assert first_seat_private_bytes < plain_bytes * 0.65
                assert warm_seat_private_bytes < plain_bytes * 0.65
            finally:
                if plain_lease is not None:
                    plain_lease.release()
                if cow_lease is not None:
                    cow_lease.release()
                if warm_lease is not None:
                    warm_lease.release()


def test_check_attr_batches_bound_by_bytes_not_count() -> None:
    """The batcher must split on pathname BYTES and preserve order exactly.

    The bound that matters is the kernel argument block, which is sized in
    bytes, so a count-based split would pass on short paths and blow ARG_MAX on
    long ones.
    """
    cap = cow._CHECK_ATTR_BATCH_BYTES
    short = [f"dir/file{index:04d}.txt".encode() for index in range(500)]
    batches = list(cow._check_attr_batches(short))
    assert batches, "a non-empty path list must yield at least one batch"
    assert [path for batch in batches for path in batch] == short
    for batch in batches:
        assert sum(len(path) + 1 for path in batch) <= cap

    # Each of these is just over half the cap, so no two fit together.
    chunky = [b"x" * (cap // 2 + 1) for _ in range(4)]
    chunky_batches = list(cow._check_attr_batches(chunky))
    assert [path for batch in chunky_batches for path in batch] == chunky
    assert all(len(batch) == 1 for batch in chunky_batches)

    # A single path larger than the whole cap still has to be emitted, alone.
    huge = [b"y" * (cap * 3)]
    assert list(cow._check_attr_batches(huge)) == [huge]

    assert list(cow._check_attr_batches([])) == []


def test_check_attr_never_passes_paths_on_stdin() -> None:
    """Regression guard: the ``--stdin`` form of this call deadlocks.

    Measured 2026-10-24 on a 17,156-path repository with no attribute source of
    any kind: one path through ``git check-attr --stdin`` returned in 0.06 s,
    500 paths never returned, and the hung ``git`` had used 0.01 s of CPU after
    28 minutes. Six such processes were found across the fleet, the oldest over
    five hours, each holding a dispatcher's seat locks. A timeout does not
    recover it -- ``Popen.communicate`` did not honour its own timeout against
    that child, and SIGKILL to the process group followed by a bounded reap did
    not return either -- so the only safe form is paths as arguments with stdin
    closed. This test fails if anyone reintroduces the pipe.
    """
    seen: dict[str, object] = {}
    real_popen = cow.subprocess.Popen

    class Recorder:
        def __init__(self, argv, **kwargs):
            seen["argv"] = list(argv)
            seen["stdin"] = kwargs.get("stdin")
            self._inner = real_popen(
                [cow.sys.executable, "-c", "pass"],
                stdout=cow.subprocess.PIPE,
                stderr=cow.subprocess.PIPE,
            )
            self.pid = self._inner.pid
            self.returncode = None

        def communicate(self, *args, **kwargs):
            out, err = self._inner.communicate()
            self.returncode = self._inner.returncode
            return out, err

    cow.subprocess.Popen = Recorder
    try:
        cow._check_attr_argv(Path("."), "HEAD", [b"one.txt", b"two.txt"])
    finally:
        cow.subprocess.Popen = real_popen

    argv = seen["argv"]
    assert "--stdin" not in argv, f"check-attr must not read paths from a pipe: {argv}"
    assert seen["stdin"] is cow.subprocess.DEVNULL, seen["stdin"]
    assert argv[:2] == ["git", "check-attr"], argv
    # Paths follow a literal "--" so a pathname starting with "-" is never
    # mistaken for an option.
    assert "--" in argv, argv
    assert argv[argv.index("--") + 1 :] == ["one.txt", "two.txt"], argv


def test_check_attr_block_returns_on_a_tree_that_deadlocked_the_pipe() -> None:
    """A path count that was fatal through ``--stdin`` must now return promptly.

    1200 paths is comfortably above the ~500 at which the pipe form stopped
    returning, and cheap to build.

    The probe runs in a SUBPROCESS, with its own session, killed by process
    group on timeout. A thread cannot be used here: the first version of this
    test used ``ThreadPoolExecutor`` and, against the unfixed module, the join
    on executor shutdown hung the whole suite instead of failing this one test
    -- a worker thread blocked in ``communicate`` is not interruptible and also
    blocks interpreter exit. A regression must FAIL this test, not wedge the
    gate, which is the same property this module is about.
    """
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        repo = root / "repo"
        repo.mkdir()
        init_repo(repo)
        for index in range(1200):
            shard = repo / f"shard{index % 24:02d}"
            shard.mkdir(exist_ok=True)
            (shard / f"file{index:05d}.txt").write_text(f"{index}\n", encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-m", "many files")
        commit = git(repo, "rev-parse", "HEAD").strip()

        probe = (
            "import sys; sys.path.insert(0, %r);"
            "import goalflight_seat_cow as cow;"
            "from pathlib import Path;"
            "print(repr(cow._check_attr_block(Path(%r), %r)))"
            % (str(ROOT / "scripts"), str(repo), commit)
        )
        child = subprocess.Popen(
            [sys.executable, "-c", probe],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            text=True,
        )
        try:
            out, err = child.communicate(timeout=120)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(OSError):
                os.killpg(child.pid, 9)
            with contextlib.suppress(subprocess.TimeoutExpired):
                child.communicate(timeout=20)
            raise AssertionError(
                "_check_attr_block did not return for 1200 paths in 120s; "
                "the check-attr call is blocking again"
            ) from None

    assert child.returncode == 0, f"probe failed: {err[-2000:]}"
    # No .gitattributes anywhere in the tree, so no rule can match any path.
    assert out.strip() == "None", f"expected None, got {out.strip()!r}; stderr={err[-500:]}"


def main() -> None:
    tests = [
        test_clonefile_detector_distinguishes_copy,
        test_check_attr_batches_bound_by_bytes_not_count,
        test_check_attr_never_passes_paths_on_stdin,
        test_check_attr_block_returns_on_a_tree_that_deadlocked_the_pipe,
        test_clonefile_support_rejects_non_darwin,
        test_flag_off_matches_manual_sparse_seat,
        test_invalid_and_false_flags_stay_off,
        test_cow_seat_matches_plain_seat,
        test_cow_checkout_modes_match_umask,
        test_cow_normalizes_symlink_and_gitlink_only_directories,
        test_shared_repository_modes_use_plain_checkout_for_extended_values,
        test_template_refreshes_after_forty_commits,
        test_dirty_template_is_never_published,
        test_flag_off_does_not_reprobe_branch,
        test_fallback_keeps_the_seat_without_a_second_probe,
        test_clone_failure_falls_back_without_residue,
        test_read_tree_failure_falls_back_without_residue,
        test_refresh_failure_repairs_in_place,
        test_missing_template_falls_back_without_residue,
        test_non_apfs_falls_back_without_residue,
        test_missing_clone_id_is_full_copy,
        test_flag_key_uses_resolved_repo_path,
        test_retired_profile_keeps_lock_inode_and_drops_crashed_staging,
        test_evidence_write_failure_keeps_verified_seat,
        test_repair_failure_skips_slot_without_removing_it,
        test_undeletable_checkout_stays_put_and_keeps_the_repo_clean,
        test_undeletable_admin_file_repairs_the_same_seat,
        test_attributes_change_matches_plain_seat_bytes,
        test_info_attributes_filter_uses_plain,
        test_filter_named_unspecified_uses_plain,
        test_global_attributes_eol_uses_plain,
        test_autocrlf_uses_plain,
        test_attr_tree_uses_plain,
        test_quoted_attribute_uses_plain,
        test_bare_cr_attribute_separator_uses_plain,
        test_attributes_file_trailing_space_uses_plain,
        test_includeif_symlinks_false_uses_plain,
        test_smudge_filter_config_uses_plain,
        test_failing_post_checkout_hook_runs_once,
        test_verification_failure_falls_back_to_full_seat,
        test_persistent_checkout_failure_skips_without_removing_the_seat,
        test_missing_reader_lock_is_not_recreated,
        test_generation_lookup_failure_falls_back,
        test_concurrent_build_does_not_tear_a_clone,
        test_measured_write_bytes_for_three_thousand_file_seat,
    ]
    for test in tests:
        try:
            test()
        except BaseException as exc:
            if exc.__class__.__name__ == "Skipped":
                print(f"SKIP {test.__name__}: {exc}")
                continue
            raise
        print(f"PASS {test.__name__}")


if __name__ == "__main__":
    main()
