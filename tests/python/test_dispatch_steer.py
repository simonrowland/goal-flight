#!/usr/bin/env python3
"""Regression tests for goalflight_dispatch.py steer mailbox routing."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from support import skip_case_posix_on_native_windows

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
DISPATCH = SCRIPTS / "goalflight_dispatch.py"
sys.path.insert(0, str(SCRIPTS))

import goalflight_dispatch  # noqa: E402
import goalflight_journal  # noqa: E402
import goalflight_ledger  # noqa: E402
import goalflight_steer_mailbox  # noqa: E402
import goalflight_terminal  # noqa: E402


@contextlib.contextmanager
def _state_dir(tmp: Path):
    isolated = {
        "GOALFLIGHT_STATE_DIR": str(tmp),
        "GOALFLIGHT_DISPATCH_DIR": str(tmp / "dispatch"),
        "GOALFLIGHT_MESSAGES_DIR": str(tmp / "messages"),
        "GOAL_FLIGHT_PIDFILE_DIR": str(tmp / "pids"),
        "GOALFLIGHT_TASK_STORE_DIR": str(tmp / "task-store"),
        "GOALFLIGHT_JOURNAL_DIR": str(tmp / "journal"),
        "GOALFLIGHT_WAKE_LEDGER_DIR": str(tmp / "wake-ledger"),
    }
    old = {key: os.environ.get(key) for key in isolated}
    os.environ.update(isolated)
    try:
        yield
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _env(tmp: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["GOALFLIGHT_STATE_DIR"] = str(tmp)
    env["GOALFLIGHT_DISPATCH_DIR"] = str(tmp / "dispatch")
    env["GOALFLIGHT_MESSAGES_DIR"] = str(tmp / "messages")
    env["GOAL_FLIGHT_PIDFILE_DIR"] = str(tmp / "pids")
    env["GOALFLIGHT_TASK_STORE_DIR"] = str(tmp / "task-store")
    env["GOALFLIGHT_JOURNAL_DIR"] = str(tmp / "journal")
    env["GOALFLIGHT_WAKE_LEDGER_DIR"] = str(tmp / "wake-ledger")
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
        def record_state(state: str) -> int:
            return goalflight_ledger.cmd_record(
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
                state=state,
                json=True,
            )
        )
        assert record_state("waiting_capacity") == 0
        assert record_state("starting") == 0
        authority = goalflight_journal.Journal(ROOT)
        attempt = authority.attempt_for_dispatch(dispatch_id)
        assert attempt is not None
        assert authority.mark_attempt_running(
            attempt.attempt_id,
            attempt.launch_token,
            launch_epoch=attempt.launch_epoch,
            worker_instance={"pid": worker_pid or os.getpid(), "source": "steer-test"},
        ).committed
        assert record_state("running") == 0


def _run_steer(tmp: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(DISPATCH), "steer", *args],
        env=_env(tmp),
        capture_output=True,
        text=True,
        timeout=20,
    )


def _run_worker_wait(tmp: Path, dispatch_id: str, *args: str) -> subprocess.CompletedProcess[str]:
    env = _env(tmp)
    env["GOALFLIGHT_DISPATCH_ID"] = dispatch_id
    env["GOALFLIGHT_STEER_FILE"] = str(_mailbox(tmp, dispatch_id))
    return subprocess.run(
        [sys.executable, str(DISPATCH), "steer", dispatch_id, "--wait", *args],
        env=env,
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
        envelopes = [
            json.loads(line)
            for line in (tmp / "messages" / f"{dispatch_id}.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        assert [envelope["payload"]["text"] for envelope in envelopes] == ["hello one", "hello two"], envelopes

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


def case_prefixed_ack_is_parsed_by_both_call_sites() -> None:
    with tempfile.TemporaryDirectory() as d:
        tail = Path(d) / "tail.log"
        tail.write_text("!STEER-ACK: 7\n", encoding="utf-8")
        record = {"stdout_path": str(tail)}
        mailbox_acks = goalflight_steer_mailbox.acked_steer_seqs(record)
        dispatch_acks = goalflight_dispatch._acked_steer_seqs(record)
        regexes_share_definition = (
            goalflight_dispatch.STEER_ACK_RE
            is goalflight_steer_mailbox.STEER_ACK_RE
            is goalflight_terminal.STEER_ACK_RE
        )
        regex_values = [
            match.group(1) if match else None
            for match in (
                goalflight_dispatch.STEER_ACK_RE.match("!STEER-ACK: 7"),
                goalflight_steer_mailbox.STEER_ACK_RE.match("!STEER-ACK: 7"),
            )
        ]
        assert (
            mailbox_acks == {7}
            and dispatch_acks == {7}
            and regexes_share_definition
            and regex_values == ["7", "7"]
        ), (mailbox_acks, dispatch_acks, regexes_share_definition, regex_values)


def case_dead_worker_records_but_does_not_claim_delivery() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        _record(tmp, "dead-worker")
        proc = _run_steer(tmp, "dead-worker", "halt")
        assert proc.returncode != 0, proc.stdout + proc.stderr
        assert "WARN:" in proc.stderr, proc.stderr
        assert "unknown_no_pid" in proc.stderr, proc.stderr
        entries = _read_mailbox(tmp, "dead-worker")
        assert entries == [], entries
        envelopes = [
            json.loads(line)
            for line in (tmp / "messages" / "dead-worker.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        assert envelopes[0]["payload"]["text"] == "halt", envelopes


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

        assert rc != 0, proc_err.getvalue()
        assert "unknown_no_pid" in proc_err.getvalue(), proc_err.getvalue()
        assert "steer appended:" not in proc_out.getvalue(), proc_out.getvalue()
        entries = _read_mailbox(tmp, dispatch_id)
        assert entries == [], entries
        envelopes = [
            json.loads(line)
            for line in (tmp / "messages" / f"{dispatch_id}.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        assert envelopes[0]["payload"]["text"] == "redirect", envelopes


def case_worker_wait_reports_existing_backlog_without_arming() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        dispatch_id = "wait-backlog"
        _record(tmp, dispatch_id, worker_pid=os.getpid())
        with _state_dir(tmp):
            goalflight_steer_mailbox.append_steer_entry(
                _mailbox(tmp, dispatch_id),
                "answer arrived before arm",
                dispatch_id=dispatch_id,
            )

        started = time.monotonic()
        proc = _run_worker_wait(
            tmp,
            dispatch_id,
            "--question-kind",
            "USER-NEED",
            "need a decision",
            "--timeout-secs",
            "1",
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert time.monotonic() - started < 0.5
        # Generic backlog answers the open-ended need, but it is not a typed
        # reply and must not wear the confirmation-looking receipt label.
        assert "STEER-BACKLOG:" in proc.stdout, proc.stdout
        assert "STEER-REPLY:" not in proc.stdout, proc.stdout
        assert "answer arrived before arm" in proc.stdout, proc.stdout
        assert "USER-NEED:" not in proc.stdout, proc.stdout
        entries = _read_mailbox(tmp, dispatch_id)
        assert not any(
            entry.get("kind") == goalflight_steer_mailbox.WORKER_WAIT_STARTED_KIND
            for entry in entries
        ), entries


def case_worker_confirm_does_not_accept_decision_free_backlog() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        dispatch_id = "confirm-backlog"
        _record(tmp, dispatch_id, worker_pid=os.getpid())
        with _state_dir(tmp):
            goalflight_steer_mailbox.append_steer_entry(
                _mailbox(tmp, dispatch_id),
                "unrelated controller note",
                dispatch_id=dispatch_id,
            )

        proc = _run_worker_wait(
            tmp,
            dispatch_id,
            "--question-kind",
            "USER-CONFIRM",
            "authorize the guarded action?",
            "--timeout-secs",
            "0.2",
            "--poll-secs",
            "1",
        )

        assert proc.returncode == 1, proc.stdout + proc.stderr
        assert "unrelated controller note" in proc.stdout, proc.stdout
        # The decision-free backlog is surfaced while the wait stays live, but
        # never with the label reserved for typed correlated replies.
        assert "STEER-BACKLOG:" in proc.stdout, proc.stdout
        assert "STEER-REPLY:" not in proc.stdout, proc.stdout
        assert f"!USER-CONFIRM: {dispatch_id} — authorize the guarded action?" in proc.stdout
        entries = _read_mailbox(tmp, dispatch_id)
        assert [entry.get("kind") for entry in entries] == [
            goalflight_steer_mailbox.STEERING_KIND,
            goalflight_steer_mailbox.WORKER_WAIT_STARTED_KIND,
        ], entries


def case_worker_wait_atomic_question_has_own_deadline() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        dispatch_id = "wait-deadline"
        _record(tmp, dispatch_id, worker_pid=os.getpid())

        started = time.monotonic()
        proc = _run_worker_wait(
            tmp,
            dispatch_id,
            "--question-kind",
            "USER-CONFIRM",
            "authorize the guarded action?",
            "--timeout-secs",
            "0.2",
            "--poll-secs",
            "1",
        )
        elapsed = time.monotonic() - started
        assert proc.returncode == 1, proc.stdout + proc.stderr
        assert 0.15 <= elapsed < 0.8, elapsed
        lines = proc.stdout.splitlines()
        assert lines[0].startswith(
            f"!USER-CONFIRM: {dispatch_id} — authorize the guarded action? "
            "[wait-id:"
        ) and lines[0].endswith("]"), lines
        assert lines[1].startswith(f"STEER-WAIT: dispatch_id={dispatch_id} armed"), lines
        assert lines[-1] == f"STEER-WAIT: dispatch_id={dispatch_id} deadline reached", lines
        entries = _read_mailbox(tmp, dispatch_id)
        assert [entry.get("kind") for entry in entries] == [
            goalflight_steer_mailbox.WORKER_WAIT_STARTED_KIND,
        ], entries


def case_worker_wait_requires_an_atomic_question() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        dispatch_id = "wait-requires-question"
        _record(tmp, dispatch_id, worker_pid=os.getpid())

        proc = _run_worker_wait(tmp, dispatch_id, "--timeout-secs", "0.1")
        assert proc.returncode == 64, proc.stdout + proc.stderr
        assert "requires question text and --question-kind" in proc.stderr, proc.stderr
        assert not _mailbox(tmp, dispatch_id).exists()


def case_worker_wait_carrier_error_is_reported() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        dispatch_id = "wait-carrier-error"
        _record(tmp, dispatch_id, worker_pid=os.getpid())
        old_wait = goalflight_steer_mailbox.wait_for_worker_entries
        old_dispatch_id = os.environ.get("GOALFLIGHT_DISPATCH_ID")
        old_steer_file = os.environ.get("GOALFLIGHT_STEER_FILE")

        def broken_wait(*_args, **_kwargs):
            raise goalflight_steer_mailbox._carrier_module().MessageError(
                "carrier identity changed"
            )

        try:
            goalflight_steer_mailbox.wait_for_worker_entries = broken_wait
            os.environ["GOALFLIGHT_DISPATCH_ID"] = dispatch_id
            os.environ["GOALFLIGHT_STEER_FILE"] = str(_mailbox(tmp, dispatch_id))
            stdout = io.StringIO()
            stderr = io.StringIO()
            with _state_dir(tmp), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                rc = goalflight_dispatch.main(
                    [
                        "steer",
                        dispatch_id,
                        "--wait",
                        "--question-kind",
                        "USER-NEED",
                        "need a decision",
                    ]
                )
        finally:
            goalflight_steer_mailbox.wait_for_worker_entries = old_wait
            if old_dispatch_id is None:
                os.environ.pop("GOALFLIGHT_DISPATCH_ID", None)
            else:
                os.environ["GOALFLIGHT_DISPATCH_ID"] = old_dispatch_id
            if old_steer_file is None:
                os.environ.pop("GOALFLIGHT_STEER_FILE", None)
            else:
                os.environ["GOALFLIGHT_STEER_FILE"] = old_steer_file

        assert rc == 1, stdout.getvalue() + stderr.getvalue()
        assert "steer --wait failed: carrier identity changed" in stderr.getvalue()


def case_controller_reply_is_typed_and_wait_id_correlated() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        dispatch_id = "typed-wait-reply"
        _record(tmp, dispatch_id, worker_pid=os.getpid())
        with _state_dir(tmp):
            arm = goalflight_steer_mailbox.append_worker_wait_started(
                _mailbox(tmp, dispatch_id),
                dispatch_id=dispatch_id,
                timeout_secs=2,
                question_kind="USER-CONFIRM",
                question_text="approve?",
            )

        proc = _run_steer(
            tmp,
            dispatch_id,
            "approved",
            "--reply-to",
            str(arm["question_id"]),
            "--decision",
            "yes",
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        reply = _read_mailbox(tmp, dispatch_id)[-1]
        assert reply["kind"] == goalflight_steer_mailbox.WORKER_WAIT_REPLY_KIND, reply
        assert reply["reply_to"] == arm["question_id"], reply
        assert reply["decision"] == "yes", reply

        duplicate = _run_steer(
            tmp,
            dispatch_id,
            "second answer",
            "--reply-to",
            str(arm["question_id"]),
            "--decision",
            "no",
        )
        assert duplicate.returncode == 1, duplicate.stdout + duplicate.stderr
        assert "already has a reply" in duplicate.stderr, duplicate.stderr

        missing_correlation = _run_steer(
            tmp,
            dispatch_id,
            "ambiguous answer",
            "--decision",
            "yes",
        )
        assert missing_correlation.returncode == 64
        assert "--decision requires --reply-to" in missing_correlation.stderr


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
            "print(os.environ.get('GOALFLIGHT_DISPATCH_SCRIPT', '')); "
            f"print('COMPLETE: {dispatch_id} — env seen', flush=True)"
        )
        proc = subprocess.run(
            [
                sys.executable,
                str(DISPATCH),
                "--unregistered-forced",
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
        tail_text = tail.read_text(encoding="utf-8")
        assert str(_mailbox(tmp, dispatch_id)) in tail_text, tail_text
        assert str(DISPATCH.resolve()) in tail_text, tail_text


def _run_prompt_env_case(tmp: Path, dispatch_id: str, prompt_args: list[str], seen_path: Path) -> str:
    worker_code = (
        "import os; "
        "from pathlib import Path; "
        f"Path({str(seen_path)!r}).write_text("
        "os.environ.get('GOALFLIGHT_PROMPT_FILE', '') + '\\n' + "
        "os.environ.get('GOALFLIGHT_STEER_FILE', ''), encoding='utf-8'); "
        f"print('COMPLETE: {dispatch_id} — prompt env seen', flush=True)"
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
                        "--unregistered-forced",
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
        repo, _worktree, _orientation = _repo_with_orientation(tmp)
        prompt_dir = repo / "prompts"
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
        body_text = "Do work.\nCOMPLETE: prompt-case — done\n"
        body.write_text(body_text, encoding="utf-8")
        assembled = Path(goalflight_dispatch._materialize_steer_prompt(str(body), tmp / "dispatch", "prompt-case"))
        text = assembled.read_text(encoding="utf-8")
        expected = (
            goalflight_dispatch.STEER_PROMPT_PREAMBLE
            + "\n\n"
            + goalflight_dispatch.PROMPT_FILE_PREAMBLE
            + "\n\n"
            + goalflight_dispatch.SCOPE_GUARD_PREAMBLE
            + "\n\nTerminal evidence identity contract:\n"
            + "- Every terminal marker payload starts with the exact dispatch id `prompt-case`.\n"
            + "- Successful final shape: `!COMPLETE: prompt-case — <summary>`.\n"
            + "- Use the same id prefix for READY, RESULT, FAILED, USER-NEED, "
            + "USER-CONFIRM, or BLOCKED. A generic or foreign marker is ignored."
            + "\n\n"
            + body_text
        )

        assert text == expected, text
        assert text.startswith(goalflight_dispatch.STEER_PROMPT_PREAMBLE + "\n\n"), text
        assert "`!STEER-ACK: <seq>`" in text, text
        assert "$GOALFLIGHT_DISPATCH_SCRIPT" in text, text
        assert "$GOALFLIGHT_PROMPT_FILE" in text, text
        assert "Re-read it after any internal compaction/summarization" in text, text
        assert "disk file is authoritative" in text, text
        assert "COMPLETE: prompt-case — done" in text, text
        assert assembled.name == "prompt-case.assembled.prompt", assembled
        assert stat.S_IMODE(assembled.stat().st_mode) == 0o600
        assert stat.S_IMODE(assembled.parent.stat().st_mode) == 0o700


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
        assert "`!COMPLETE: grok-prompt-case — <summary>`" in text, text
        assert "Legacy unprefixed marker lines remain accepted" in text, text
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
        assert "`!COMPLETE: codex-prompt-case — <summary>`" in text, text


def case_preamble_routing_matrix() -> None:
    # Lock the shared execution-preamble routing across every agent label.
    worker_marker = goalflight_dispatch.WORKER_EXECUTION_PREAMBLE
    for agent in ("grok-code", "grok-research", "moonshot"):
        assert worker_marker in goalflight_dispatch._worker_prompt_preamble(agent), agent
    # "kimi" is the retired moonshot handle: not a preset, no worker preamble.
    for agent in ("codex", "cursor", "claude", "claude-acp", "codex-acp", "opencode", "kimi", None):
        assert worker_marker not in goalflight_dispatch._worker_prompt_preamble(agent), agent
    # The steer preamble is always present regardless of agent.
    for agent in ("grok-code", "grok-research", "codex", None):
        preamble = goalflight_dispatch._worker_prompt_preamble(agent)
        assert goalflight_dispatch.STEER_PROMPT_PREAMBLE in preamble, agent
        assert goalflight_dispatch.PROMPT_FILE_PREAMBLE in preamble, agent
    # The scope guard goes to every dispatch regardless of agent. It binds
    # code CHANGES, not the task in general, so it is inert for a reviewer
    # whose deliverable is findings rather than a diff -- which is why there
    # is no opt-out flag. (--read-only cannot serve as one: it refuses any
    # prompt that writes a review artifact, so no review dispatch sets it.)
    scope_marker = goalflight_dispatch.SCOPE_GUARD_PREAMBLE
    for agent in ("grok-code", "grok-research", "moonshot", "codex", "cursor",
                  "claude-acp", "codex-acp", "opencode", None):
        assert scope_marker in goalflight_dispatch._worker_prompt_preamble(agent), agent
    assert "where this task changes code" in scope_marker
    # Escalation must be named, or an unattended worker expands scope silently.
    assert "reason `scope`" in scope_marker
    # ...but never as a literal marker token: this text lands in every prompt,
    # and worker tails echo the prompt, so a bare "BLOCKED:" here would be
    # scraped back as a real escalation from every dispatch (b-109).
    assert "BLOCKED:" not in scope_marker
    # Scope limits must not read as permission to skip the brief's verification.
    assert "not on VERIFICATION" in scope_marker


def main() -> None:
    case_bash_append_and_list_with_ack()
    case_shape_routing_and_missing_record()
    case_acp_list_reads_status_ack_dict()
    case_prefixed_ack_is_parsed_by_both_call_sites()
    case_dead_worker_records_but_does_not_claim_delivery()
    case_steer_is_no_worker_early_exit()
    case_worker_wait_reports_existing_backlog_without_arming()
    case_worker_confirm_does_not_accept_decision_free_backlog()
    case_worker_wait_atomic_question_has_own_deadline()
    case_worker_wait_requires_an_atomic_question()
    case_worker_wait_carrier_error_is_reported()
    case_controller_reply_is_typed_and_wait_id_correlated()
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
