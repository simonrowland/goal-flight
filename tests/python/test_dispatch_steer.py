#!/usr/bin/env python3
"""Regression tests for goalflight_dispatch.py steer mailbox routing."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from support import skip_case_posix_on_native_windows

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
DISPATCH = SCRIPTS / "goalflight_dispatch.py"
sys.path.insert(0, str(SCRIPTS))

import goalflight_dispatch  # noqa: E402
import goalflight_ledger  # noqa: E402


@contextlib.contextmanager
def _state_dir(tmp: Path):
    old = os.environ.get("GOALFLIGHT_STATE_DIR")
    os.environ["GOALFLIGHT_STATE_DIR"] = str(tmp)
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("GOALFLIGHT_STATE_DIR", None)
        else:
            os.environ["GOALFLIGHT_STATE_DIR"] = old


def _env(tmp: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["GOALFLIGHT_STATE_DIR"] = str(tmp)
    env["PYTHONPATH"] = str(SCRIPTS) + os.pathsep + env.get("PYTHONPATH", "")
    return env


def _mailbox(tmp: Path, dispatch_id: str) -> Path:
    return tmp / "dispatch" / f"{dispatch_id}.steer.jsonl"


def _read_mailbox(tmp: Path, dispatch_id: str) -> list[dict]:
    path = _mailbox(tmp, dispatch_id)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _record(
    tmp: Path,
    dispatch_id: str,
    *,
    shape: str = "bash",
    worker_pid: int | None = None,
    stdout_path: Path | None = None,
    status_path: Path | None = None,
) -> None:
    status_path = status_path or (tmp / f"{dispatch_id}.status.json")
    with _state_dir(tmp), contextlib.redirect_stdout(io.StringIO()):
        goalflight_ledger.cmd_record(
            argparse.Namespace(
                dispatch_id=dispatch_id,
                prompt_id=None,
                prompt_path=None,
                agent="test-dispatch",
                engine="test",
                shape=shape,
                account="default",
                transport="dispatch",
                project_root=str(ROOT),
                controller_pid=os.getpid(),
                worker_pid=worker_pid,
                acp_session_id="session-1" if shape == "acp" else None,
                logical_session_id=dispatch_id,
                lease_id=None,
                stdout_path=str(stdout_path) if stdout_path else None,
                stderr_path=None,
                status_path=str(status_path),
                os_sandbox_json=json.dumps({"shape": shape}, sort_keys=True),
                state="running",
                json=True,
            )
        )


def _run_steer(tmp: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(DISPATCH), "steer", *args],
        env=_env(tmp),
        capture_output=True,
        text=True,
        timeout=20,
    )


def case_bash_append_and_list_with_ack() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        dispatch_id = "steer-list"
        tail = tmp / "tail.log"
        tail.write_text("STATUS: running\nSTEER-ACK: 1\n", encoding="utf-8")
        _record(tmp, dispatch_id, worker_pid=os.getpid(), stdout_path=tail)

        first = _run_steer(tmp, dispatch_id, "hello one")
        second = _run_steer(tmp, dispatch_id, "hello two")
        assert first.returncode == 0, first.stderr
        assert second.returncode == 0, second.stderr

        entries = _read_mailbox(tmp, dispatch_id)
        assert [entry["seq"] for entry in entries] == [1, 2], entries
        assert [entry["text"] for entry in entries] == ["hello one", "hello two"], entries

        listed = _run_steer(tmp, dispatch_id, "--list")
        assert listed.returncode == 0, listed.stderr
        assert "seq\tts\tacked\ttext" in listed.stdout, listed.stdout
        assert "\ttrue\thello one" in listed.stdout, listed.stdout
        assert "\tfalse\thello two" in listed.stdout, listed.stdout


def case_shape_routing_and_missing_record() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        _record(tmp, "acp-mailbox", shape="acp", worker_pid=os.getpid())
        acp = _run_steer(tmp, "acp-mailbox", "redirect")
        assert acp.returncode == 0, acp.stdout + acp.stderr
        assert "steer appended:" in acp.stdout, acp.stdout
        entries = _read_mailbox(tmp, "acp-mailbox")
        assert len(entries) == 1 and entries[0]["text"] == "redirect", entries

        missing = _run_steer(tmp, "missing-dispatch", "redirect")
        assert missing.returncode != 0, missing.stdout + missing.stderr
        assert "no ledger record" in missing.stderr


def case_acp_list_reads_status_ack_dict() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        dispatch_id = "acp-status-ack"
        status = tmp / "status.json"
        status.write_text(json.dumps({"markers": {"STEER-ACK": ["1"]}}), encoding="utf-8")
        _record(tmp, dispatch_id, shape="acp", worker_pid=os.getpid(), status_path=status)

        proc = _run_steer(tmp, dispatch_id, "redirect")
        assert proc.returncode == 0, proc.stdout + proc.stderr

        listed = _run_steer(tmp, dispatch_id, "--list")
        assert listed.returncode == 0, listed.stderr
        assert "\ttrue\tredirect" in listed.stdout, listed.stdout


def case_dead_worker_warns_but_appends() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        _record(tmp, "dead-worker")
        proc = _run_steer(tmp, "dead-worker", "halt")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "WARN:" in proc.stderr, proc.stderr
        entries = _read_mailbox(tmp, "dead-worker")
        assert len(entries) == 1 and entries[0]["text"] == "halt", entries


def case_steer_is_no_worker_early_exit() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        dispatch_id = "no-worker"
        _record(tmp, dispatch_id)

        def boom(*_args, **_kwargs):
            raise AssertionError("steer path must not acquire leases, materialize prompts, or spawn workers")

        old_acquire = goalflight_dispatch._acquire_capacity
        old_materialize = goalflight_dispatch._materialize_steer_prompt
        old_popen = goalflight_dispatch.subprocess.Popen
        try:
            goalflight_dispatch._acquire_capacity = boom
            goalflight_dispatch._materialize_steer_prompt = boom
            goalflight_dispatch.subprocess.Popen = boom
            with _state_dir(tmp):
                proc_out = io.StringIO()
                proc_err = io.StringIO()
                with contextlib.redirect_stdout(proc_out), contextlib.redirect_stderr(proc_err):
                    rc = goalflight_dispatch.main(["steer", dispatch_id, "redirect"])
        finally:
            goalflight_dispatch._acquire_capacity = old_acquire
            goalflight_dispatch._materialize_steer_prompt = old_materialize
            goalflight_dispatch.subprocess.Popen = old_popen

        assert rc == 0, proc_err.getvalue()
        assert "steer appended:" in proc_out.getvalue(), proc_out.getvalue()
        entries = _read_mailbox(tmp, dispatch_id)
        assert len(entries) == 1 and entries[0]["text"] == "redirect", entries


def case_concurrent_appends_have_monotonic_unique_seq() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        dispatch_id = "steer-concurrent"
        _record(tmp, dispatch_id, worker_pid=os.getpid())
        procs = [
            subprocess.Popen(
                [sys.executable, str(DISPATCH), "steer", dispatch_id, f"msg-{idx}"],
                env=_env(tmp),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for idx in range(12)
        ]
        outputs = [proc.communicate(timeout=20) + (proc.returncode,) for proc in procs]
        failures = [out for out in outputs if out[2] != 0]
        assert not failures, failures

        entries = _read_mailbox(tmp, dispatch_id)
        seqs = [entry["seq"] for entry in entries]
        assert sorted(seqs) == list(range(1, 13)), entries
        assert len(set(entry["text"] for entry in entries)) == 12, entries


def case_spawn_exports_steer_env() -> None:
    if skip_case_posix_on_native_windows(
        "case_spawn_exports_steer_env",
        "steer env export launches a POSIX/WSL bash-tail dispatch worker",
    ):
        return

    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        dispatch_id = "env-export"
        tail = tmp / "tail.log"
        status = tmp / "status.json"
        worker_code = (
            "import os; "
            "print(os.environ.get('GOALFLIGHT_STEER_FILE', '')); "
            "print('COMPLETE: env seen', flush=True)"
        )
        proc = subprocess.run(
            [
                sys.executable,
                str(DISPATCH),
                "--agent",
                "env-check",
                "--dispatch-id",
                dispatch_id,
                "--tail",
                str(tail),
                "--status-json",
                str(status),
                "--poll-secs",
                "0.1",
                "--max-idle-secs",
                "5",
                "--foreground",
                "--",
                sys.executable,
                "-c",
                worker_code,
            ],
            env=_env(tmp),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert str(_mailbox(tmp, dispatch_id)) in tail.read_text(encoding="utf-8"), tail.read_text(encoding="utf-8")


def _run_prompt_env_case(tmp: Path, dispatch_id: str, prompt_args: list[str], seen_path: Path) -> str:
    worker_code = (
        "import os; "
        "from pathlib import Path; "
        f"Path({str(seen_path)!r}).write_text("
        "os.environ.get('GOALFLIGHT_PROMPT_FILE', '') + '\\n' + "
        "os.environ.get('GOALFLIGHT_STEER_FILE', ''), encoding='utf-8'); "
        "print('COMPLETE: prompt env seen', flush=True)"
    )

    old_build_worker = goalflight_dispatch.build_worker

    def fake_build_worker(_args, _prompt_path, _raw_argv):
        return [sys.executable, "-c", worker_code], None

    try:
        goalflight_dispatch.build_worker = fake_build_worker
        with _state_dir(tmp):
            proc_out = io.StringIO()
            proc_err = io.StringIO()
            with contextlib.redirect_stdout(proc_out), contextlib.redirect_stderr(proc_err):
                rc = goalflight_dispatch.main(
                    [
                        "--agent",
                        "codex",
                        "--dispatch-id",
                        dispatch_id,
                        "--tail",
                        str(tmp / f"{dispatch_id}.tail"),
                        "--status-json",
                        str(tmp / f"{dispatch_id}.status.json"),
                        "--poll-secs",
                        "0.1",
                        "--max-idle-secs",
                        "5",
                        "--capacity-wait-s",
                        "0",
                        "--foreground",
                        "--ignore-git-warn",
                        *prompt_args,
                    ]
                )
    finally:
        goalflight_dispatch.build_worker = old_build_worker

    assert rc == 0, proc_out.getvalue() + proc_err.getvalue()
    return seen_path.read_text(encoding="utf-8")


def _run_git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=True,
    )


def _repo_with_orientation(tmp: Path) -> tuple[Path, Path, Path]:
    repo = tmp / "repo"
    worktree = tmp / "linked-worktree"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
    _run_git(repo, "config", "user.email", "goalflight@example.test")
    _run_git(repo, "config", "user.name", "Goal Flight Test")
    repo.joinpath("README.md").write_text("fixture\n", encoding="utf-8")
    _run_git(repo, "add", "README.md")
    _run_git(repo, "commit", "-m", "fixture")
    orientation = repo / "docs-private" / "rag" / "ORIENTATION.md"
    orientation.parent.mkdir(parents=True)
    orientation.write_text("fixture orientation\n", encoding="utf-8")
    _run_git(repo, "worktree", "add", "--detach", str(worktree), "HEAD")
    return repo, worktree, orientation


def case_inline_prompt_exports_original_prompt_file() -> None:
    if skip_case_posix_on_native_windows(
        "case_inline_prompt_exports_original_prompt_file",
        "prompt env export launches a POSIX/WSL bash-tail dispatch worker",
    ):
        return

    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        dispatch_id = "inline-prompt-env"
        prompt_text = "Line one\n\nLine three\n"
        seen = tmp / "seen-inline.txt"
        seen_text = _run_prompt_env_case(tmp, dispatch_id, ["--prompt", prompt_text], seen)
        prompt_env, steer_env = seen_text.splitlines()
        expected_prompt = tmp / "dispatch" / f"{dispatch_id}.prompt"

        assert Path(prompt_env) == expected_prompt, seen_text
        assert expected_prompt.read_text(encoding="utf-8") == prompt_text
        assert steer_env == str(_mailbox(tmp, dispatch_id)), seen_text


def case_prompt_file_exports_given_path() -> None:
    if skip_case_posix_on_native_windows(
        "case_prompt_file_exports_given_path",
        "prompt env export launches a POSIX/WSL bash-tail dispatch worker",
    ):
        return

    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        dispatch_id = "file-prompt-env"
        prompt_file = tmp / "brief.md"
        prompt_file.write_text("Read the durable brief.\n", encoding="utf-8")
        seen = tmp / "seen-file.txt"
        seen_text = _run_prompt_env_case(tmp, dispatch_id, ["--prompt-file", str(prompt_file)], seen)
        prompt_env, steer_env = seen_text.splitlines()

        # Export contract: resolved absolute path (symlink-canonical), so the
        # worker's re-read works from any cwd.
        assert Path(prompt_env) == prompt_file.resolve(), seen_text
        assert steer_env == str(_mailbox(tmp, dispatch_id)), seen_text


def case_relative_prompt_file_exports_resolved_absolute_path() -> None:
    if skip_case_posix_on_native_windows(
        "case_relative_prompt_file_exports_resolved_absolute_path",
        "prompt env export launches a POSIX/WSL bash-tail dispatch worker",
    ):
        return

    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        dispatch_id = "rel-prompt-env"
        prompt_dir = tmp / "prompts"
        prompt_dir.mkdir()
        brief = prompt_dir / "brief.md"
        brief.write_text("Read the durable brief.\n", encoding="utf-8")
        seen = tmp / "seen-rel.txt"
        prev_cwd = os.getcwd()
        try:
            os.chdir(prompt_dir)
            seen_text = _run_prompt_env_case(
                tmp, dispatch_id, ["--prompt-file", "brief.md"], seen
            )
        finally:
            os.chdir(prev_cwd)
        prompt_env, _steer_env = seen_text.splitlines()

        # A relative --prompt-file must export resolved+absolute: the worker
        # re-reads $GOALFLIGHT_PROMPT_FILE from its OWN cwd, where a relative
        # path resolves against the wrong root.
        assert Path(prompt_env).is_absolute(), seen_text
        assert Path(prompt_env) == brief.resolve(), seen_text


def case_prompt_preamble_is_materialized() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        body = tmp / "body.md"
        body_text = "Do work.\nCOMPLETE: done\n"
        body.write_text(body_text, encoding="utf-8")
        assembled = Path(goalflight_dispatch._materialize_steer_prompt(str(body), tmp / "dispatch", "prompt-case"))
        text = assembled.read_text(encoding="utf-8")
        expected = (
            goalflight_dispatch.STEER_PROMPT_PREAMBLE
            + "\n\n"
            + goalflight_dispatch.PROMPT_FILE_PREAMBLE
            + "\n\n"
            + body_text
        )

        assert text == expected, text
        assert text.startswith(goalflight_dispatch.STEER_PROMPT_PREAMBLE + "\n\n"), text
        assert "`STEER-ACK: <seq>`" in text, text
        assert "$GOALFLIGHT_PROMPT_FILE" in text, text
        assert "Re-read it after any internal compaction/summarization" in text, text
        assert "disk file is authoritative" in text, text
        assert "COMPLETE: done" in text, text
        assert assembled.name == "prompt-case.assembled.prompt", assembled


def case_orientation_preamble_is_materialized_when_present() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        repo, _worktree, orientation = _repo_with_orientation(tmp)
        body = tmp / "body.md"
        body.write_text("Decide and implement.\n", encoding="utf-8")
        orientation_path = goalflight_dispatch._project_orientation_path(repo)
        assert orientation_path == orientation.resolve(), orientation_path

        assembled = Path(
            goalflight_dispatch._materialize_steer_prompt(
                str(body),
                tmp / "dispatch",
                "orientation-case",
                agent="codex",
                orientation_path=orientation_path,
            )
        )
        text = assembled.read_text(encoding="utf-8")

        assert "PROJECT ORIENTATION\n" in text, text
        assert f"Path: {orientation.resolve()}" in text, text
        assert goalflight_dispatch.PROJECT_ORIENTATION_SCOPE_RULE in text, text
        assert "Decide and implement." in text, text


def case_no_orientation_suppresses_orientation_path() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        repo, _worktree, _orientation = _repo_with_orientation(tmp)
        assert goalflight_dispatch._project_orientation_path(repo, disabled=True) is None


def case_orientation_path_resolves_linked_worktree_to_main_root() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        _repo, worktree, orientation = _repo_with_orientation(tmp)
        resolved = goalflight_dispatch._project_orientation_path(worktree)
        assert resolved == orientation.resolve(), resolved


def case_orientation_path_resolves_from_repo_subdirectory() -> None:
    # rE P1: git emits a RELATIVE --git-common-dir from the command cwd, so a
    # dispatch cwd nested inside the checkout must still resolve the repo root
    # (resolving the relative form against toplevel walked out of the tree).
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        repo, worktree, orientation = _repo_with_orientation(tmp)
        subdir = repo / "nested" / "leaf"
        subdir.mkdir(parents=True)
        resolved = goalflight_dispatch._project_orientation_path(subdir)
        assert resolved == orientation.resolve(), resolved
        wt_subdir = worktree / "nested" / "leaf"
        wt_subdir.mkdir(parents=True)
        wt_resolved = goalflight_dispatch._project_orientation_path(wt_subdir)
        assert wt_resolved == orientation.resolve(), wt_resolved


def case_grok_prompt_adds_execution_and_terminal_contract() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        body = tmp / "body.md"
        body.write_text("Write target.txt with ok.\n", encoding="utf-8")
        assembled = Path(
            goalflight_dispatch._materialize_steer_prompt(
                str(body),
                tmp / "dispatch",
                "grok-prompt-case",
                agent="grok-code",
            )
        )
        text = assembled.read_text(encoding="utf-8")

        expected_prefix = (
            goalflight_dispatch.STEER_PROMPT_PREAMBLE
            + "\n\n"
            + goalflight_dispatch.PROMPT_FILE_PREAMBLE
            + "\n\n"
            + goalflight_dispatch.WORKER_EXECUTION_PREAMBLE
            + "\n\n"
        )
        assert text.startswith(expected_prefix), text
        assert "Use your available tools to actually perform" in text, text
        assert "`COMPLETE: <summary>`" in text, text
        assert "last non-empty line" in text, text
        assert "Write target.txt with ok." in text, text


def case_codex_prompt_does_not_add_grok_contract() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        body = tmp / "body.md"
        body.write_text("Review only.\n", encoding="utf-8")
        assembled = Path(
            goalflight_dispatch._materialize_steer_prompt(
                str(body),
                tmp / "dispatch",
                "codex-prompt-case",
                agent="codex",
            )
        )
        text = assembled.read_text(encoding="utf-8")

        expected_prefix = (
            goalflight_dispatch.STEER_PROMPT_PREAMBLE
            + "\n\n"
            + goalflight_dispatch.PROMPT_FILE_PREAMBLE
            + "\n\n"
        )
        assert text.startswith(expected_prefix), text
        assert goalflight_dispatch.WORKER_EXECUTION_PREAMBLE not in text, text


def case_preamble_routing_matrix() -> None:
    # Lock the shared execution-preamble routing across every agent label.
    worker_marker = goalflight_dispatch.WORKER_EXECUTION_PREAMBLE
    for agent in ("grok-code", "grok-research", "kimi"):
        assert worker_marker in goalflight_dispatch._worker_prompt_preamble(agent), agent
    for agent in ("codex", "cursor", "claude", "claude-acp", "codex-acp", "opencode", None):
        assert worker_marker not in goalflight_dispatch._worker_prompt_preamble(agent), agent
    # The steer preamble is always present regardless of agent.
    for agent in ("grok-code", "grok-research", "codex", None):
        preamble = goalflight_dispatch._worker_prompt_preamble(agent)
        assert goalflight_dispatch.STEER_PROMPT_PREAMBLE in preamble, agent
        assert goalflight_dispatch.PROMPT_FILE_PREAMBLE in preamble, agent


def main() -> None:
    case_bash_append_and_list_with_ack()
    case_shape_routing_and_missing_record()
    case_acp_list_reads_status_ack_dict()
    case_dead_worker_warns_but_appends()
    case_steer_is_no_worker_early_exit()
    case_concurrent_appends_have_monotonic_unique_seq()
    case_spawn_exports_steer_env()
    case_inline_prompt_exports_original_prompt_file()
    case_prompt_file_exports_given_path()
    case_relative_prompt_file_exports_resolved_absolute_path()
    case_prompt_preamble_is_materialized()
    case_orientation_preamble_is_materialized_when_present()
    case_no_orientation_suppresses_orientation_path()
    case_orientation_path_resolves_linked_worktree_to_main_root()
    case_orientation_path_resolves_from_repo_subdirectory()
    case_grok_prompt_adds_execution_and_terminal_contract()
    case_codex_prompt_does_not_add_grok_contract()
    case_preamble_routing_matrix()
    print("OK: goalflight_dispatch steer tests pass")


def test_dispatch_steer_cases() -> None:
    main()


if __name__ == "__main__":
    main()
