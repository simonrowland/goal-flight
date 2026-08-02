"""Linked-worktree Codex writable roots stay sufficient and narrow."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import goalflight_acp_run as acp  # noqa: E402
import goalflight_codex_sandbox as sandbox  # noqa: E402
import goalflight_dispatch as dispatch  # noqa: E402
import goalflight_doctor as doctor  # noqa: E402
import goalflight_os_sandbox as os_sandbox  # noqa: E402


def _run(*argv: str, cwd: Path) -> str:
    proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False)
    assert proc.returncode == 0, (argv, proc.stdout, proc.stderr)
    return proc.stdout.strip()


def _codex_args(cwd: Path, profile: str = "workspace-write") -> argparse.Namespace:
    return argparse.Namespace(
        agent="codex",
        shape="bash",
        read_only=False,
        os_sandbox=profile,
        model=None,
        cwd=str(cwd),
        fast=False,
    )


def _configured_roots(argv: list[str]) -> list[str]:
    prefix = "sandbox_workspace_write.writable_roots="
    values = [part[len(prefix):] for part in argv if part.startswith(prefix)]
    assert len(values) == 1, argv
    return json.loads(values[0])


def _is_within(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def _chmod_tree(root: Path, *, writable: bool) -> None:
    for path in [root, *root.rglob("*")]:
        if path.is_symlink():
            continue
        if path.is_dir():
            path.chmod(0o755 if writable else 0o555)
        else:
            path.chmod(0o644 if writable else 0o444)


def case_linked_worktree_argv_is_narrow() -> None:
    scratch = REPO_ROOT / "docs-private"
    scratch.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="gf-linked-worktree-test-", dir=scratch) as tmp:
        base = Path(tmp)
        parent = base / "parent"
        linked = base / "linked"
        parent.mkdir()
        _run("git", "init", cwd=parent)
        (parent / "seed.txt").write_text("seed\n", encoding="utf-8")
        _run("git", "add", "seed.txt", cwd=parent)
        env = os.environ.copy()
        env.update(
            GIT_AUTHOR_NAME="Goal Flight Test",
            GIT_AUTHOR_EMAIL="goal-flight@example.invalid",
            GIT_COMMITTER_NAME="Goal Flight Test",
            GIT_COMMITTER_EMAIL="goal-flight@example.invalid",
        )
        proc = subprocess.run(
            ["git", "commit", "-m", "seed"], cwd=parent, env=env,
            capture_output=True, text=True, check=False,
        )
        assert proc.returncode == 0, (proc.stdout, proc.stderr)
        _run("git", "worktree", "add", "-b", "linked-test", str(linked), cwd=parent)

        dirs = sandbox.linked_worktree_git_dirs(linked)
        assert dirs is not None
        git_dir, common_dir = dirs
        # Worktree roots, then the worker's channel roots. The channel roots are
        # unconditional: without them a worker cannot post mail or start its own
        # mandatory review, both of which failed as bare "Operation not
        # permitted" with no hint that the grant was simply never passed.
        #
        # Kept as EXACT equality on purpose. This case exists to catch
        # over-granting, and the cheap way to make a sandbox bug go away is to
        # widen the grant until it does -- so the list must be enumerated, not
        # merely contain what it needs.
        expected = [
            str(git_dir),
            str(common_dir / "objects"),
            str(common_dir / "refs" / "heads"),
            str(common_dir / "logs" / "refs" / "heads"),
        ] + sandbox.worker_channel_roots()

        bash_argv, _ = dispatch.build_worker(_codex_args(linked), "/tmp/p.md", [])
        assert _configured_roots(bash_argv) == expected

        _command, base_acp_argv = acp.agent_command("codex-acp")
        acp_argv = acp._codex_workspace_write_acp_args(
            "codex-acp", base_acp_argv, cwd=str(linked), os_sandbox="workspace-write"
        )
        assert _configured_roots(acp_argv) == expected

        outer_roots = os_sandbox.macos_write_roots(
            str(linked), "workspace-write", agent="codex-acp", command="codex-acp"
        )
        for root in expected:
            assert root in outer_roots, outer_roots
        misleading_roots = os_sandbox.macos_write_roots(
            str(linked), "workspace-write", agent="notcodex", command="custom-codex-proxy"
        )
        # The protection here is that a LOOK-ALIKE agent does not inherit codex's
        # worktree grants: "custom-codex-proxy" contains the substring "codex",
        # and the worktree roots are gated on an exact label match precisely so
        # that spoofing the name earns nothing.
        #
        # Channel roots are deliberately excluded from this check: every worker
        # needs to post mail regardless of engine, so they are universal by
        # design and their presence for another agent is correct, not a leak.
        worktree_only = [root for root in expected if root not in sandbox.worker_channel_roots()]
        assert worktree_only, "the spoofing check must still have something to protect"
        assert not any(root in misleading_roots for root in worktree_only), misleading_roots

        parent_root = parent.resolve()
        assert str(parent_root) not in expected
        assert str(parent_root) not in _configured_roots(bash_argv)
        # Worktree grants must stay inside the worktree's own git dirs -- the
        # whole point of the linked-worktree narrowing. Channel roots sit outside
        # by definition (they are the shared mailbox and codex's dispatch homes),
        # so they are checked separately below rather than exempted silently.
        for raw in worktree_only:
            path = Path(raw).resolve()
            assert _is_within(path, git_dir) or _is_within(path, common_dir), raw
        # And the channel roots must not smuggle in anything beyond the two
        # directories they exist for: this is the check that would catch someone
        # "fixing" a sandbox denial by widening the channel grant to $HOME.
        for raw in sandbox.worker_channel_roots():
            path = Path(raw).resolve()
            assert path.name in {"messages", "dispatch-homes"} or path.parent.name == "dispatch-homes", raw
            assert _is_within(path, Path.home() / ".goal-flight"), raw

        # Filesystem-permission proof of the same boundary. Pack the current
        # branch first, then make the whole common gitdir read-only except the
        # four granted roots. Git must create a loose ref without touching
        # packed-refs.
        _run("git", "pack-refs", "--all", cwd=parent)
        packed_refs = common_dir / "packed-refs"
        packed_before = packed_refs.read_bytes()
        try:
            _chmod_tree(common_dir, writable=False)
            for raw in expected:
                _chmod_tree(Path(raw), writable=True)
            (linked / "probe.txt").write_text("probe\n", encoding="utf-8")
            _run("git", "add", "--", "probe.txt", cwd=linked)
            commit = subprocess.run(
                ["git", "commit", "-m", "linked probe", "--", "probe.txt"],
                cwd=linked,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            assert commit.returncode == 0, (commit.stdout, commit.stderr)
            assert packed_refs.read_bytes() == packed_before
            assert (common_dir / "refs" / "heads" / "linked-test").is_file()
        finally:
            _chmod_tree(common_dir, writable=True)

        external_objects = base / "external-objects"
        shutil.move(str(common_dir / "objects"), external_objects)
        (common_dir / "objects").symlink_to(external_objects, target_is_directory=True)
        assert sandbox.linked_worktree_writable_roots(linked) == []
        escaped_argv, _ = dispatch.build_worker(_codex_args(linked), "/tmp/p.md", [])
        # When the worktree narrowing cannot be computed safely (objects moved
        # outside via symlink), NO worktree root may be emitted -- a partial
        # narrow grant is worse than none. The channel roots still are: they do
        # not depend on the worktree, and a worker that cannot reach its mailbox
        # cannot report the very breakage that got it here.
        escaped_roots = _configured_roots(escaped_argv)
        assert escaped_roots == sandbox.worker_channel_roots(), escaped_argv
        assert not any(str(common_dir) in root for root in escaped_roots), escaped_argv
        escaped_outer = os_sandbox.macos_write_roots(
            str(linked), "workspace-write", agent="codex", command="codex"
        )
        assert str(external_objects.resolve()) not in escaped_outer, escaped_outer


def case_non_linked_and_non_write_profiles_are_unchanged() -> None:
    with tempfile.TemporaryDirectory(prefix="gf-normal-repo-test-") as tmp:
        repo = Path(tmp) / "repo"
        repo.mkdir()
        _run("git", "init", cwd=repo)

        assert sandbox.linked_worktree_writable_roots(repo) == []
        normal_argv, _ = dispatch.build_worker(_codex_args(repo), "/tmp/p.md", [])
        # A NORMAL (non-linked) repo needs no worktree narrowing -- codex's own
        # workspace-write already covers the checkout. It still gets the channel
        # roots, and only those. This is the case that was silently broken: the
        # grant was gated behind a cwd argument nobody passes, so an ordinary
        # worker in an ordinary repo could neither post mail nor start its
        # review, and the failure surfaced only as "Operation not permitted".
        normal_roots = _configured_roots(normal_argv)
        assert normal_roots == sandbox.worker_channel_roots(), normal_argv

        read_only_argv, _ = dispatch.build_worker(
            _codex_args(repo, "read-only"), "/tmp/p.md", []
        )
        assert not any("writable_roots=" in part for part in read_only_argv), read_only_argv

        _command, base_acp_argv = acp.agent_command("codex-acp")
        acp_argv = acp._codex_workspace_write_acp_args(
            "codex-acp", base_acp_argv, cwd=str(repo), os_sandbox="read-only"
        )
        assert not any("writable_roots=" in part for part in acp_argv), acp_argv

        grok_command, base_grok_argv = acp.agent_command("grok-acp")
        grok_argv = acp._codex_workspace_write_acp_args(
            "grok-acp", base_grok_argv, cwd=str(repo), os_sandbox="workspace-write"
        )
        assert grok_command
        assert not any("writable_roots=" in part for part in grok_argv), grok_argv


def case_doctor_reports_cleanup_failure() -> None:
    original_run = doctor.run
    original_subprocess_run = doctor.subprocess.run

    def result(*, ok: bool, stdout: str = "", stderr: str = "") -> dict:
        return {"ok": ok, "returncode": 0 if ok else 1, "stdout": stdout, "stderr": stderr}

    def fake_run(argv: list[str], timeout: float) -> dict:
        if "worktree" in argv and "add" in argv:
            Path(argv[-2]).mkdir(parents=True)
            return result(ok=True)
        if "worktree" in argv and "remove" in argv:
            return result(ok=False, stderr="cleanup denied")
        if "branch" in argv and "-D" in argv:
            return result(ok=True)
        if "rev-parse" in argv:
            cwd = Path(argv[argv.index("-C") + 1])
            return result(ok=True, stdout=("b" if cwd.name == "worktree" else "a") * 40 + "\n")
        if "show" in argv:
            cwd = Path(argv[argv.index("-C") + 1])
            prompt_text = (cwd.parent / "prompt.md").read_text(encoding="utf-8")
            match = re.search(r"`(goalflight-doctor-worktree-commit:[^`]+)`", prompt_text)
            assert match
            return result(ok=True, stdout=match.group(1) + "\n")
        raise AssertionError(argv)

    def fake_subprocess_run(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 0, "", "")

    doctor.run = fake_run
    doctor.subprocess.run = fake_subprocess_run
    try:
        payload = doctor.worker_linked_worktree_commit_probe(REPO_ROOT, enabled=True)
    finally:
        doctor.run = original_run
        doctor.subprocess.run = original_subprocess_run
    assert payload["ok"] is False, payload
    assert payload["state"] == "cleanup_failed", payload
    assert payload["cleanup_errors"] == ["cleanup denied"], payload


def main() -> None:
    case_linked_worktree_argv_is_narrow()
    case_non_linked_and_non_write_profiles_are_unchanged()
    case_doctor_reports_cleanup_failure()
    print("test_linked_worktree_codex_sandbox: all cases passed")


if __name__ == "__main__":
    main()
