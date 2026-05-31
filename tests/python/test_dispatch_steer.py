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
        _record(tmp, "acp-stub", shape="acp", worker_pid=os.getpid())
        acp = _run_steer(tmp, "acp-stub", "redirect")
        assert acp.returncode != 0, acp.stdout + acp.stderr
        assert "acp inline steer lands with --interactive (#8); use session/prompt once wired" in acp.stderr
        assert not _mailbox(tmp, "acp-stub").exists()

        missing = _run_steer(tmp, "missing-dispatch", "redirect")
        assert missing.returncode != 0, missing.stdout + missing.stderr
        assert "no ledger record" in missing.stderr


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


def case_prompt_preamble_is_materialized() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        body = tmp / "body.md"
        body.write_text("Do work.\nCOMPLETE: done\n", encoding="utf-8")
        assembled = Path(goalflight_dispatch._materialize_steer_prompt(str(body), tmp / "dispatch", "prompt-case"))
        text = assembled.read_text(encoding="utf-8")

        assert text.startswith(goalflight_dispatch.STEER_PROMPT_PREAMBLE + "\n\n"), text
        assert "`STEER-ACK: <seq>`" in text, text
        assert "COMPLETE: done" in text, text
        assert assembled.name == "prompt-case.assembled.prompt", assembled


def main() -> None:
    case_bash_append_and_list_with_ack()
    case_shape_routing_and_missing_record()
    case_dead_worker_warns_but_appends()
    case_steer_is_no_worker_early_exit()
    case_concurrent_appends_have_monotonic_unique_seq()
    case_spawn_exports_steer_env()
    case_prompt_preamble_is_materialized()
    print("OK: goalflight_dispatch steer tests pass")


if __name__ == "__main__":
    main()
