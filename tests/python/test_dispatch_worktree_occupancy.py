#!/usr/bin/env python3
"""t-375: a second WRITE dispatch into an occupied worktree must be refused.

The 2026-08-27 incident: two task ids dispatched three times each into the
same worktrees; the occupancy warning never blocked, so three concurrent
workers edited one filesystem tree apiece with no merge discipline. The guard
must REFUSE (non-zero exit) naming the incumbent. Enforcement is an exclusive
non-blocking kernel lock on the target worktree, inherited by the worker so
the claim cannot be raced and is released by the kernel on crash. The ledger
is diagnostic (names the incumbent, fail-closes when unlistable). Exempt
genuinely read-only dispatches.

Every precondition here is built for real (b-235): incumbents are genuine
dispatched workers with genuine ledger records, read by the real reader.
"""

from __future__ import annotations

import contextlib

from support import make_test_git_repo, skip_posix_on_native_windows

skip_posix_on_native_windows("worktree occupancy tests launch POSIX workers")

import json
import os
import pwd
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
DISPATCH = ROOT / "scripts" / "goalflight_dispatch.py"
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_dispatch as dispatch  # noqa: E402
import goalflight_journal  # noqa: E402
import goalflight_ledger as ledger  # noqa: E402
import goalflight_worktree_pool  # noqa: E402

_ASYNC_WAIT_TIMEOUT_S = 30.0


def _env(tmp: Path, *, capacity_max_total: int | None = None) -> dict[str, str]:
    env = os.environ.copy()
    for key in (
        "GOALFLIGHT_DISPATCH_ID",
        "GOALFLIGHT_DISPATCH_SCRIPT",
        "GOALFLIGHT_PROJECT_ROOT",
        "GOALFLIGHT_PROMPT_FILE",
        "GOALFLIGHT_STEER_FILE",
        "GOALFLIGHT_ALLOW_EXTERNAL_STEER_FILE",
        "GOALFLIGHT_CONTROLLER_SESSION_ID",
        "GOALFLIGHT_CONTROLLER_LEASE_NONCE",
        "GOALFLIGHT_CONTROLLER_PID",
        "GOALFLIGHT_CONTROLLER_LABEL",
        "GOALFLIGHT_PROCESS_ROLE",
        "GOALFLIGHT_WORKTREE_LOCK_FD",
        "GOALFLIGHT_OCCUPANCY_LOCK_FD",
    ):
        env.pop(key, None)
    env["GOALFLIGHT_STATE_DIR"] = str(tmp / "state")
    env["GOALFLIGHT_DISPATCH_DIR"] = str(tmp / "state" / "dispatch")
    env["GOALFLIGHT_JOURNAL_DIR"] = str(tmp / "journal")
    env["GOALFLIGHT_MESSAGES_DIR"] = str(tmp / "messages")
    env["GOALFLIGHT_TASK_STORE"] = str(tmp / "task-store")
    env["GOALFLIGHT_TASK_STORE_DIR"] = str(tmp / "task-store")
    env["GOALFLIGHT_WAKE_LEDGER_DIR"] = str(tmp / "wake-ledger")
    env["GOALFLIGHT_WAKE_LEDGER"] = str(tmp / "wake-ledger" / "wake.jsonl")
    env["GOALFLIGHT_PIDFILE_DIR"] = str(tmp / "pids")
    env["GOAL_FLIGHT_PIDFILE_DIR"] = str(tmp / "pids")
    env["GOALFLIGHT_FLEET_DIR"] = str(tmp / "fleet")
    if capacity_max_total is None:
        env["GOALFLIGHT_CAPACITY_CONF"] = "/dev/null"
        env["GOALFLIGHT_CAPACITY_MAX_TOTAL"] = "1"
    else:
        capacity_conf = tmp / "capacity.json"
        capacity_conf.write_text(
            json.dumps(
                {
                    "max_total": capacity_max_total,
                    "agent_caps": {
                        "test": capacity_max_total,
                        "grok": capacity_max_total,
                        "codex-acp": capacity_max_total,
                    },
                }
            ),
            encoding="utf-8",
        )
        capacity_bin = tmp / "capacity-bin"
        capacity_bin.mkdir()
        sysctl = capacity_bin / "sysctl"
        sysctl.write_text("#!/bin/sh\nprintf '%s\\n' 137438953472\n", encoding="utf-8")
        sysctl.chmod(0o755)
        env["GOALFLIGHT_CAPACITY_CONF"] = str(capacity_conf)
        env["PATH"] = f"{capacity_bin}{os.pathsep}{env['PATH']}"
        env.pop("GOALFLIGHT_CAPACITY_MAX_TOTAL", None)
    env["GOALFLIGHT_CAPACITY_WAIT_S"] = "0"
    return env


def _configure_test_accounts(tmp: Path, env: dict[str, str]) -> None:
    home = tmp / "home"
    grok_home = home / ".goal-flight" / "accounts" / "occupancy-test" / "grok"
    (grok_home / ".grok").mkdir(parents=True)
    (grok_home / ".grok" / "auth.json").write_text("{}", encoding="utf-8")
    (grok_home / ".grok" / "config.toml").write_text(
        '[ui]\npermission_mode = "always-approve"\n', encoding="utf-8"
    )
    (home / ".goal-flight" / "accounts" / "occupancy-test" / "codex").mkdir(
        parents=True
    )
    env["HOME"] = str(home)


def _run(
    cmd: list[str],
    env: dict[str, str],
    *,
    timeout: float = 60.0,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd is not None else None,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )


def _wait_for(predicate, *, timeout: float = _ASYNC_WAIT_TIMEOUT_S) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return bool(predicate())


def _dispatch_cmd(
    tmp: Path,
    tree: Path,
    dispatch_id: str,
    worker_code: str,
    *,
    extra: list[str] | None = None,
    foreground: bool = False,
    in_place: bool | None = None,
) -> list[str]:
    cmd = [
        sys.executable,
        str(DISPATCH),
        "--unregistered-forced",
        "--cwd",
        str(tree),
        "--agent",
        "test",
        "--dispatch-id",
        dispatch_id,
        "--tail",
        str(tmp / f"{dispatch_id}.tail"),
        "--status-json",
        str(tmp / f"{dispatch_id}.status.json"),
        "--poll-secs",
        "0.1",
        "--max-idle-secs",
        "20",
    ]
    if in_place is None:
        project_root = dispatch._project_root(SimpleNamespace(cwd=str(tree)))
        in_place = project_root.resolve() == tree.resolve()
    if in_place:
        cmd.append("--in-place")
    if foreground:
        cmd.append("--foreground")
    if extra:
        cmd.extend(extra)
    cmd += ["--", sys.executable, "-c", worker_code]
    return cmd


def _blocking_worker(release: Path, dispatch_id: str) -> str:
    return (
        "from pathlib import Path\n"
        "import time\n"
        f"release = Path({str(release)!r})\n"
        "deadline = time.monotonic() + 25\n"
        "while not release.exists():\n"
        "    if time.monotonic() >= deadline:\n"
        "        raise TimeoutError('test release not received')\n"
        "    time.sleep(0.05)\n"
        f"print('COMPLETE: {dispatch_id} — released', flush=True)\n"
    )


def _quick_writer(dispatch_id: str) -> str:
    return f"print('COMPLETE: {dispatch_id} — done', flush=True)\n"


def _managed_acp_python() -> Path | None:
    """Login-home ACP venv, even when tests remap HOME."""
    login_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    candidate = login_home / ".goal-flight" / "venvs" / "acp-0.10" / "bin" / "python"
    return candidate if candidate.is_file() else None


def _prompt_writer_cmd(
    tmp: Path,
    tree: Path,
    dispatch_id: str,
    *,
    agent: str,
    extra: list[str] | None = None,
) -> list[str]:
    """Preset / ACP launch into --cwd with a prompt (no `--` worker)."""
    cmd = [
        sys.executable,
        str(DISPATCH),
        "--unregistered-forced",
        "--cwd",
        str(tree),
        "--agent",
        agent,
        "--dispatch-id",
        dispatch_id,
        "--prompt",
        "implement the occupied-worktree probe",
        "--ignore-git-warn",
        "--tail",
        str(tmp / f"{dispatch_id}.tail"),
        "--status-json",
        str(tmp / f"{dispatch_id}.status.json"),
        "--poll-secs",
        "0.1",
        "--max-idle-secs",
        "20",
    ]
    if extra:
        cmd.extend(extra)
    return cmd


class _ReapingTempDir:
    """Temp dir that retries rmtree after workers/watchers release files.

    Occupancy tests spawn watchers that can still hold a dirent at assertion
    time. Swallowing every rmtree error hid leaked fds; retry first, and only
    ignore leftover dirents after a second pass.
    """

    def __init__(self, prefix: str | None = None, *, dir: Path | None = None) -> None:
        self.name = tempfile.mkdtemp(
            prefix=prefix or "tmp",
            dir=str(dir) if dir is not None else None,
        )

    def __enter__(self) -> str:
        return self.name

    def __exit__(self, exc_type, exc, tb) -> None:
        path = Path(self.name)
        for attempt in range(3):
            try:
                shutil.rmtree(path)
                return None
            except OSError:
                time.sleep(0.2 * (attempt + 1))
        shutil.rmtree(path, ignore_errors=True)
        return None


def _temp_dir(prefix: str | None = None, *, dir: Path | None = None) -> _ReapingTempDir:
    return _ReapingTempDir(prefix=prefix, dir=dir)


@contextlib.contextmanager
def _non_temp_tree(prefix: str):
    """A worker tree outside the macOS temp root.

    An explicit --read-only dispatch runs the OS-sandbox boundary check, which
    refuses cwds inside the allowed temp root (it cannot separate the workspace
    from everything else a temp root lets the worker write). The repo's
    gitignored docs-private/ is the established scratch location for this.

    The tree is its own git toplevel so ``--cwd`` is in-place, not a nested
    path under this repository (captive-ring lock).
    """
    scratch = ROOT / "docs-private"
    scratch.mkdir(exist_ok=True)
    with _temp_dir(prefix=prefix, dir=scratch) as td:
        tree = Path(td)
        init = subprocess.run(
            ["git", "init", "-b", "main"],
            cwd=str(tree),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if init.returncode != 0:
            raise AssertionError(f"git init failed: {init.stderr or init.stdout}")
        yield td


def _read_only_sandbox_available() -> bool:
    repo = os.path.realpath(str(ROOT))
    temp_root = os.path.realpath(tempfile.gettempdir())
    return repo != temp_root and not repo.startswith(temp_root + os.sep)


def _skip_read_only_sandbox_case(name: str) -> bool:
    if _read_only_sandbox_available():
        return False
    print(
        f"SKIP {name}: repository clone is under the temp root; "
        "macOS read-only sandbox cannot enforce workspace boundaries there"
    )
    return True


def _make_runs_dir_unreadable_after_record(
    runs: Path, dispatch_id: str
) -> tuple[threading.Thread, threading.Event]:
    recorded = runs / f"{dispatch_id}.json"
    changed = threading.Event()

    def change_mode() -> None:
        deadline = time.monotonic() + _ASYNC_WAIT_TIMEOUT_S
        while time.monotonic() < deadline and not recorded.exists():
            time.sleep(0.01)
        if recorded.exists():
            os.chmod(runs, 0o000)
            changed.set()

    thread = threading.Thread(target=change_mode, daemon=True)
    thread.start()
    return thread, changed


def _ledger_record(tmp: Path, dispatch_id: str) -> dict:
    path = tmp / "state" / "runs.d" / f"{dispatch_id}.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _assert_failed_worktree_record(tmp: Path, dispatch_id: str) -> dict:
    record = _ledger_record(tmp, dispatch_id)
    assert record, dispatch_id
    assert record.get("state") == "failed_worktree", record
    assert record.get("terminal_state") == "error", record
    assert record.get("prelaunch_failure") is True, record
    assert record.get("worker_still_alive") is False, record
    assert record.get("worker_pid") is None, record
    assert record.get("worker_cwd") is None, record
    seat_fields = (
        "worktree_id",
        "worktree_seat",
        "worktree_path",
        "worktree_base",
        "worktree_head",
        "worktree_branch",
        "worktree_keep_ref",
        "worktree_quarantine_ref",
        "worktree_reclaimed_dispatch_id",
    )
    assert not any(record.get(key) for key in seat_fields), record
    status = json.loads(Path(record["status_path"]).read_text(encoding="utf-8"))
    assert status.get("state") == "failed_worktree", status
    assert status.get("terminal_state") == "error", status
    assert status.get("prelaunch_failure") is True, status
    assert status.get("worker_alive") is False, status
    assert not any(status.get(key) for key in seat_fields), status
    return record


def _write_runs_record(tmp: Path, dispatch_id: str, **fields: object) -> dict:
    """Write a real ledger JSON file under the isolated runs.d."""
    runs = tmp / "state" / "runs.d"
    runs.mkdir(parents=True, exist_ok=True)
    record = {
        "schema": ledger.SCHEMA,
        "dispatch_id": dispatch_id,
        "agent": "test",
        "shape": "bash",
        "state": "running",
        **fields,
    }
    (runs / f"{dispatch_id}.json").write_text(json.dumps(record), encoding="utf-8")
    return record


def _wait_until_running(tmp: Path, dispatch_id: str) -> dict:
    def running() -> bool:
        record = _ledger_record(tmp, dispatch_id)
        return record.get("state") == "running" and bool(record.get("worker_pid"))

    assert _wait_for(running), f"{dispatch_id} never reached running: {_ledger_record(tmp, dispatch_id)!r}"
    return _ledger_record(tmp, dispatch_id)


def _pid_gone(pid: object) -> bool:
    try:
        os.kill(int(pid), 0)
    except (TypeError, ValueError, ProcessLookupError):
        return True
    except PermissionError:
        return False
    except OSError:
        return True
    return False


def _wait_until_terminal(tmp: Path, dispatch_id: str) -> dict:
    status_path = tmp / f"{dispatch_id}.status.json"

    def terminal() -> bool:
        record = _ledger_record(tmp, dispatch_id)
        if not record:
            return False
        if ledger.terminal_state_for(
            record.get("state"), record.get("reason") or record.get("error")
        ) != "unknown":
            return True
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return status.get("worker_alive") is False and bool(status.get("terminal_state"))

    assert _wait_for(terminal), f"{dispatch_id} never reached a terminal state"
    record = _ledger_record(tmp, dispatch_id)
    for key in ("worker_pid", "watcher_pid"):
        pid = record.get(key)
        if pid:
            _wait_for(lambda pid=pid: _pid_gone(pid), timeout=5.0)
    return record


def test_second_writer_refused_and_ordinary_force_rejected() -> None:
    with _temp_dir() as td:
        tmp = Path(td)
        tree = tmp / "tree"
        tree.mkdir()
        env = _env(tmp, capacity_max_total=2)
        release_incumbent = tmp / "release-incumbent"
        incumbent = _run(
            _dispatch_cmd(tmp, tree, "occ-incumbent", _blocking_worker(release_incumbent, "occ-incumbent")),
            env,
        )
        assert incumbent.returncode == 0, (incumbent.stdout, incumbent.stderr)
        try:
            record = _wait_until_running(tmp, "occ-incumbent")
            assert record.get("worker_cwd") == str(tree.resolve()), record

            refused = _run(
                _dispatch_cmd(tmp, tree, "occ-second", _quick_writer("occ-second")),
                env,
            )
            assert refused.returncode == 64, (refused.returncode, refused.stdout, refused.stderr)
            assert "occ-incumbent" in refused.stderr, refused.stderr
            assert "--occupied-worktree-forced" in refused.stderr, refused.stderr
            _assert_failed_worktree_record(tmp, "occ-second")

            forced = _run(
                _dispatch_cmd(
                    tmp,
                    tree,
                    "occ-second",
                    _quick_writer("occ-second"),
                    extra=["--occupied-worktree-forced", "--capacity-wait-s", "0"],
                    foreground=True,
                ),
                env,
            )
            assert forced.returncode == 64, (forced.stdout, forced.stderr)
            assert "deprecated and rejected for ordinary dispatch" in forced.stderr
            _assert_failed_worktree_record(tmp, "occ-second")
        finally:
            release_incumbent.write_text("release", encoding="utf-8")
        _wait_until_terminal(tmp, "occ-incumbent")


@pytest.mark.parametrize("holder_liveness", ("live", "unknown"))
def test_fresh_pinned_writer_refuses_before_wait_publication(
    holder_liveness: str, monkeypatch
) -> None:
    with _temp_dir() as td:
        tmp = Path(td)
        repo = make_test_git_repo(tmp)
        env = _env(tmp, capacity_max_total=2)
        monkeypatch.setenv("GOALFLIGHT_JOURNAL_DIR", env["GOALFLIGHT_JOURNAL_DIR"])
        monkeypatch.setenv("GOALFLIGHT_TASK_STORE", env["GOALFLIGHT_TASK_STORE"])
        monkeypatch.setenv("GOALFLIGHT_TASK_STORE_DIR", env["GOALFLIGHT_TASK_STORE_DIR"])

        def pooled_dispatch_cmd(
            dispatch_id: str, worker_code: str, *, foreground: bool = False
        ) -> list[str]:
            cmd = [
                sys.executable,
                str(DISPATCH),
                "--unregistered-forced",
                "--agent",
                "test",
                "--dispatch-id",
                dispatch_id,
                "--tail",
                str(tmp / f"{dispatch_id}.tail"),
                "--status-json",
                str(tmp / f"{dispatch_id}.status.json"),
                "--poll-secs",
                "0.1",
                "--max-idle-secs",
                "20",
            ]
            if foreground:
                cmd.append("--foreground")
            return [*cmd, "--", sys.executable, "-c", worker_code]

        release_holder = tmp / "release-holder"
        holder_process = None
        occupancy_holder_process = None
        release_occupancy_lock = tmp / "release-occupancy-lock"
        occupancy_lock_ready = tmp / "occupancy-lock-ready.json"
        foreign_id = f"fresh-{holder_liveness}-holder"
        try:
            if holder_liveness == "live":
                holder_process = subprocess.Popen(
                    pooled_dispatch_cmd(
                        foreign_id,
                        _blocking_worker(release_holder, foreign_id),
                        foreground=True,
                    ),
                    cwd=str(repo),
                    env=env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                holder_record = _wait_until_running(tmp, foreign_id)
                seat = Path(holder_record["worktree_path"])
            else:
                seeded = _run(
                    pooled_dispatch_cmd(
                        "fresh-unknown-seat-seed",
                        _quick_writer("fresh-unknown-seat-seed"),
                        foreground=True,
                    ),
                    env,
                    cwd=repo,
                )
                assert seeded.returncode == 0, (seeded.stdout, seeded.stderr)
                seed_record = _ledger_record(tmp, "fresh-unknown-seat-seed")
                seat = Path(seed_record["worktree_path"])
                assert not _ledger_record(tmp, foreign_id)

            occupancy_holder_code = (
                "import json, sys, time\n"
                f"sys.path.insert(0, {str(ROOT / 'scripts')!r})\n"
                "from pathlib import Path\n"
                "import goalflight_worktree_pool as pool\n"
                f"seat = Path({str(seat)!r})\n"
                f"dispatch_id = {foreign_id!r}\n"
                f"ready = Path({str(occupancy_lock_ready)!r})\n"
                f"release = Path({str(release_occupancy_lock)!r})\n"
                "try:\n"
                "    lock = pool.try_acquire_worktree_path_lock(seat, dispatch_id)\n"
                "except pool.WorktreePathLockBusy as exc:\n"
                "    ready.write_text(json.dumps({'held': False, 'holder': exc.occupant_id}), encoding='utf-8')\n"
                "else:\n"
                "    ready.write_text(json.dumps({'held': True}), encoding='utf-8')\n"
                "    while not release.exists(): time.sleep(0.01)\n"
                "    lock.release()\n"
            )
            occupancy_holder_process = subprocess.Popen(
                [sys.executable, "-c", occupancy_holder_code],
                cwd=str(repo),
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            assert _wait_for(lambda: occupancy_lock_ready.exists()), (
                occupancy_holder_process.poll(),
                occupancy_holder_process.stderr,
            )
            occupancy_state = json.loads(
                occupancy_lock_ready.read_text(encoding="utf-8")
            )
            if holder_liveness == "live" and not occupancy_state["held"]:
                assert occupancy_state.get("holder") == foreign_id, occupancy_state
            else:
                assert occupancy_state["held"] is True, occupancy_state

            refused_id = f"fresh-{holder_liveness}-contender"
            journal = goalflight_journal.open_or_create_journal(repo)
            wait_records = tmp / "waiting-records.jsonl"
            observer_code = (
                "import json, sys\n"
                f"sys.path.insert(0, {str(ROOT / 'scripts')!r})\n"
                "from pathlib import Path\n"
                "import goalflight_dispatch as observed_dispatch\n"
                f"wait_records = Path({str(wait_records)!r})\n"
                "record_ledger = observed_dispatch._record_ledger\n"
                "def record_ledger_with_observation(args, *positional, **keywords):\n"
                "    if keywords.get('state') == 'waiting_capacity':\n"
                "        with wait_records.open('a', encoding='utf-8') as stream:\n"
                "            stream.write(json.dumps({'cwd': getattr(args, 'cwd', None)}) + '\\n')\n"
                "    return record_ledger(args, *positional, **keywords)\n"
                "observed_dispatch._record_ledger = record_ledger_with_observation\n"
                "raise SystemExit(observed_dispatch._main_impl(sys.argv[1:]))\n"
            )
            dispatch_cmd = _dispatch_cmd(
                tmp,
                seat,
                refused_id,
                _quick_writer(refused_id),
                extra=["--capacity-wait-s", "2"],
                foreground=True,
                in_place=False,
            )
            pending = subprocess.Popen(
                [dispatch_cmd[0], "-c", observer_code, *dispatch_cmd[2:]],
                cwd=str(repo),
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            out, err = _communicate(pending, timeout=20)
            published_cwds = []
            if wait_records.exists():
                published_cwds = [
                    row.get("cwd")
                    for line in wait_records.read_text(encoding="utf-8").splitlines()
                    if (row := json.loads(line)).get("cwd")
                ]
            assert pending.returncode == 64, (pending.returncode, out, err)
            assert "worktree seat" in err and "flock_liveness=held" in err, err
            assert f"holder_liveness={holder_liveness}" in err, err
            assert not any(
                Path(raw_cwd).resolve(strict=False) == seat.resolve()
                for raw_cwd in published_cwds
            ), published_cwds
            assert _ledger_record(tmp, refused_id).get("state") != "waiting_capacity"
            attempt = journal.attempt_for_dispatch(refused_id)
            assert attempt is None or attempt.lifecycle_state == goalflight_journal.ATTEMPT_TERMINAL
        finally:
            release_holder.write_text("release", encoding="utf-8")
            release_occupancy_lock.write_text("release", encoding="utf-8")
            if occupancy_holder_process is not None:
                lock_out, lock_err = _communicate(
                    occupancy_holder_process, timeout=30
                )
                assert occupancy_holder_process.returncode == 0, (
                    occupancy_holder_process.returncode,
                    lock_out,
                    lock_err,
                )
            if holder_process is not None:
                holder_out, holder_err = _communicate(holder_process, timeout=30)
                assert holder_process.returncode == 0, (
                    holder_process.returncode,
                    holder_out,
                    holder_err,
                )
                _wait_until_terminal(tmp, foreign_id)


def test_declared_read_only_raw_worker_is_refused_into_occupied_worktree() -> None:
    """--read-only on a `--` worker is a declaration; occupancy still applies."""
    if _skip_read_only_sandbox_case("declared read-only raw worker"):
        return
    with _temp_dir() as td, _non_temp_tree("gf-occ-ro-tree-") as tree_td:
        tmp = Path(td)
        tree = Path(tree_td).resolve()
        env = _env(tmp, capacity_max_total=2)
        release_incumbent = tmp / "release-incumbent"
        incumbent = _run(
            _dispatch_cmd(tmp, tree, "ro-incumbent", _blocking_worker(release_incumbent, "ro-incumbent")),
            env,
            cwd=Path(tree_td),
        )
        assert incumbent.returncode == 0, (incumbent.stdout, incumbent.stderr)
        try:
            _wait_until_running(tmp, "ro-incumbent")
            declared = _run(
                _dispatch_cmd(
                    tmp,
                    tree,
                    "ro-declared",
                    _quick_writer("ro-declared"),
                    extra=["--read-only"],
                    foreground=True,
                ),
                env,
                cwd=Path(tree_td),
            )
            assert declared.returncode == 64, (declared.returncode, declared.stdout, declared.stderr)
            assert "ro-incumbent" in declared.stderr, declared.stderr
            _assert_failed_worktree_record(tmp, "ro-declared")
        finally:
            release_incumbent.write_text("release", encoding="utf-8")
        _wait_until_terminal(tmp, "ro-incumbent")


def test_enforced_read_only_reviewer_skips_occupancy() -> None:
    grok = SimpleNamespace(
        read_only=True,
        agent="grok-code",
        worker=[],
        shape="bash",
        os_sandbox=None,
        cwd=".",
        dispatch_id="ro-grok",
        occupied_worktree_forced=False,
        from_queue=False,
    )
    assert dispatch._occupancy_exempt_read_only(grok) is True
    assert dispatch._prepare_attempt_worktree_occupancy(grok) is None
    raw = SimpleNamespace(
        read_only=True,
        agent="test",
        worker=["--", sys.executable, "-c", "pass"],
        shape="bash",
        os_sandbox=None,
        cwd=".",
        dispatch_id="ro-raw",
        occupied_worktree_forced=False,
        from_queue=False,
    )
    assert dispatch._occupancy_exempt_read_only(raw) is False


def test_terminal_incumbent_vacates_the_tree() -> None:
    with _temp_dir() as td:
        tmp = Path(td)
        tree = tmp / "tree"
        tree.mkdir()
        env = _env(tmp)
        first = _run(
            _dispatch_cmd(tmp, tree, "term-first", _quick_writer("term-first"), foreground=True),
            env,
        )
        assert first.returncode == 0, (first.stdout, first.stderr)
        _wait_until_terminal(tmp, "term-first")
        second = _run(
            _dispatch_cmd(tmp, tree, "term-second", _quick_writer("term-second"), foreground=True),
            env,
        )
        assert second.returncode == 0, (second.stdout, second.stderr)
        assert "DISPATCH-END" in second.stdout, second.stdout


def test_unreadable_ledger_record_is_unknown_not_unoccupied() -> None:
    with _temp_dir() as td:
        tmp = Path(td)
        tree = tmp / "tree"
        tree.mkdir()
        env = _env(tmp, capacity_max_total=2)
        runs = tmp / "state" / "runs.d"
        runs.mkdir(parents=True)
        (runs / "corrupt-record.json").write_text("{not json", encoding="utf-8")

        refused = _run(_dispatch_cmd(tmp, tree, "unk-writer", _quick_writer("unk-writer")), env)
        assert refused.returncode == 64, (refused.returncode, refused.stdout, refused.stderr)
        assert "occupancy" in refused.stderr and "unknown" in refused.stderr, refused.stderr
        assert "Retry the dispatch" in refused.stderr, refused.stderr
        assert "--occupied-worktree-forced" in refused.stderr, refused.stderr
        _assert_failed_worktree_record(tmp, "unk-writer")

        forced = _run(
            _dispatch_cmd(
                tmp,
                tree,
                "unk-writer",
                _quick_writer("unk-writer"),
                extra=["--occupied-worktree-forced"],
                foreground=True,
            ),
            env,
        )
        assert forced.returncode == 64, (forced.stdout, forced.stderr)
        assert "deprecated and rejected for ordinary dispatch" in forced.stderr
        _assert_failed_worktree_record(tmp, "unk-writer")


def test_preset_bash_writer_refused_into_occupied_worktree() -> None:
    """The grok-code / codex preset path shares the bash occupancy gate."""
    with _temp_dir() as td:
        tmp = Path(td)
        tree = tmp / "tree"
        tree.mkdir()
        env = _env(tmp, capacity_max_total=2)
        _configure_test_accounts(tmp, env)
        release_incumbent = tmp / "release-incumbent"
        incumbent = _run(
            _dispatch_cmd(tmp, tree, "preset-incumbent", _blocking_worker(release_incumbent, "preset-incumbent")),
            env,
        )
        assert incumbent.returncode == 0, (incumbent.stdout, incumbent.stderr)
        try:
            _wait_until_running(tmp, "preset-incumbent")
            refused = _run(
                _prompt_writer_cmd(
                    tmp,
                    tree,
                    "preset-second",
                    agent="grok-code",
                    extra=["--account", "occupancy-test", "--in-place"],
                ),
                env,
            )
            assert refused.returncode == 64, (refused.returncode, refused.stdout, refused.stderr)
            assert "preset-incumbent" in refused.stderr, refused.stderr
            assert "DISPATCH-LAUNCHED" not in refused.stdout, refused.stdout
            _assert_failed_worktree_record(tmp, "preset-second")
        finally:
            release_incumbent.write_text("release", encoding="utf-8")
        _wait_until_terminal(tmp, "preset-incumbent")


def test_acp_writer_refused_into_occupied_worktree() -> None:
    """ACP --cwd is a separate main() branch; occupancy must refuse there too."""
    import pytest

    acp_py = _managed_acp_python()
    if acp_py is None:
        pytest.skip("live ACP occupancy requires a managed ACP interpreter")
    from test_acp_dispatch_sigterm import _write_fake_codex_acp_manifest
    with _temp_dir() as td:
        tmp = Path(td)
        tree = tmp / "tree"
        tree.mkdir()
        env = _env(tmp, capacity_max_total=2)
        _configure_test_accounts(tmp, env)
        env["GOALFLIGHT_ACP_PYTHON"] = str(acp_py)
        _write_fake_codex_acp_manifest(tmp / "adapters")
        env["GOALFLIGHT_ADAPTERS_DIR"] = str(tmp / "adapters")
        env["GOALFLIGHT_ALLOW_ADAPTERS_DIR_OVERRIDE"] = "1"
        env["GOALFLIGHT_FAKE_ACP_SCENARIO"] = "echo"
        release_incumbent = tmp / "release-incumbent"
        incumbent = _run(
            _dispatch_cmd(tmp, tree, "acp-incumbent", _blocking_worker(release_incumbent, "acp-incumbent")),
            env,
        )
        assert incumbent.returncode == 0, (incumbent.stdout, incumbent.stderr)
        try:
            _wait_until_running(tmp, "acp-incumbent")
            refused = _run(
                _prompt_writer_cmd(
                    tmp,
                    tree,
                    "acp-second",
                    agent="codex-acp",
                    extra=["--shape", "acp", "--account", "occupancy-test", "--foreground"],
                ),
                env,
            )
            assert refused.returncode == 64, (refused.returncode, refused.stdout, refused.stderr)
            refusal = json.loads((tmp / "acp-second.status.json").read_text(encoding="utf-8"))
            assert refusal["state"] == "failed_worktree", refusal
            assert refusal["reason"] == "worktree_occupied", refusal
            assert "acp-incumbent" in refusal["error"], refusal
            assert "already owned" in refusal["error"], refusal
            assert "acp-incumbent" in refused.stdout, refused.stdout
            assert "acp-incumbent" in refused.stderr, refused.stderr
            assert "DISPATCH-LAUNCHED" not in refused.stdout, refused.stdout
            _assert_failed_worktree_record(tmp, "acp-second")
            from unittest.mock import patch
            from test_dispatch_registration_gate import _register_controller

            try:
                ps_probe = _run(["ps", "-p", str(os.getpid()), "-o", "pid="], env)
            except PermissionError:
                print("SKIP: ACP refusal verified; forced ACP launch requires sandbox-denied ps")
                return
            if ps_probe.returncode and any(
                reason in ps_probe.stderr.lower()
                for reason in ("operation not permitted", "permission denied")
            ):
                print("SKIP: ACP refusal verified; forced ACP launch requires sandbox-denied ps")
                return
            assert ps_probe.returncode == 0, ps_probe.stderr
            env["GOALFLIGHT_FAKE_ACP_SCENARIO"] = "goal"
            with patch.dict(os.environ, env, clear=True):
                _authority, holder, _nonce = _register_controller(tree, env)
                with contextlib.closing(holder):
                    root_refused = _run(
                        _prompt_writer_cmd(
                            tmp,
                            tree,
                            "acp-root-refused",
                            agent="codex-acp",
                            extra=[
                                "--shape", "acp", "--account", "occupancy-test",
                                "--occupied-worktree-forced", "--foreground",
                            ],
                        ),
                        env,
                    )
                    assert root_refused.returncode == 64, (
                        root_refused.stdout,
                        root_refused.stderr,
                    )
                    assert "DISPATCH-REFUSED " in root_refused.stdout
                    root_refusal_status = json.loads(
                        (tmp / "acp-root-refused.status.json").read_text(
                            encoding="utf-8"
                        )
                    )
                    assert root_refusal_status["state"] == "failed_worktree", (
                        root_refusal_status
                    )
                    assert "project checkout" in root_refusal_status["error"], (
                        root_refusal_status
                    )
                    forced = _run(
                        _prompt_writer_cmd(
                            tmp,
                            tree,
                            "acp-forced",
                            agent="codex-acp",
                            extra=[
                                "--shape", "acp", "--account", "occupancy-test",
                                "--occupied-worktree-forced", "--in-place",
                                "--foreground",
                            ],
                        ),
                        env,
                    )
            forced_status = json.loads((tmp / "acp-forced.status.json").read_text(encoding="utf-8"))
            assert forced.returncode == 0, (forced.stdout, forced.stderr, forced_status)
            assert forced_status["state"] == "complete", forced_status
            assert forced_status["controller_label"] == "registered-test", forced_status
            assert "--occupied-worktree-forced accepted" in forced.stderr, forced.stderr
            tail = (tmp / "acp-forced.tail").read_text(encoding="utf-8")
            assert "--occupied-worktree-forced accepted" in tail, tail
            assert "acp-incumbent" in tail, tail
            assert "acp-incumbent" in forced.stderr, forced.stderr
            assert "DISPATCH-END" in forced.stdout, forced.stdout
        finally:
            release_incumbent.write_text("release", encoding="utf-8")
        _wait_until_terminal(tmp, "acp-incumbent")


def test_forced_pooled_acp_does_not_pass_rejected_occupancy_fd_marker() -> None:
    import pytest

    acp_py = _managed_acp_python()
    if acp_py is None:
        pytest.skip("pooled ACP occupancy requires a managed ACP interpreter")
    import fcntl

    from test_acp_dispatch_sigterm import _write_fake_codex_acp_manifest

    with _temp_dir() as td:
        tmp = Path(td)
        repo = make_test_git_repo(tmp)
        env = _env(tmp, capacity_max_total=2)
        _configure_test_accounts(tmp, env)
        env["GOALFLIGHT_ACP_PYTHON"] = str(acp_py)
        env["GOALFLIGHT_FAKE_ACP_SCENARIO"] = "goal"
        snapshot_path = tmp / "worker-env.json"
        env["GOALFLIGHT_TEST_ACP_WORKER_ENV"] = str(snapshot_path)

        adapters = tmp / "adapters"
        _write_fake_codex_acp_manifest(adapters)
        manifest_path = adapters / "codex-acp.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        wrapper = tmp / "capture-acp-env"
        wrapper.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "from pathlib import Path\n"
            f"name = {goalflight_worktree_pool.OCCUPANCY_LOCK_FD_ENV!r}\n"
            "Path(os.environ['GOALFLIGHT_TEST_ACP_WORKER_ENV']).write_text(\n"
            "    json.dumps({name: name in os.environ}), encoding='utf-8'\n"
            ")\n"
            f"os.execv(sys.executable, [sys.executable, {str(ROOT / 'tests/fixtures/acp_fake_agent.py')!r}, *sys.argv[1:]])\n",
            encoding="utf-8",
        )
        wrapper.chmod(0o755)
        manifest["invocation"]["exec"]["binary"] = str(wrapper)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        env["GOALFLIGHT_ADAPTERS_DIR"] = str(adapters)
        env["GOALFLIGHT_ALLOW_ADAPTERS_DIR_OVERRIDE"] = "1"

        def pooled_command(dispatch_id: str, cwd: Path = repo) -> list[str]:
            return [
                sys.executable,
                str(ROOT / "scripts/goalflight_acp_run.py"),
                "--unregistered-forced",
                "--worktree",
                "create",
                "--agent",
                "codex-acp",
                "--cwd",
                str(cwd),
                "--prompt-text",
                "hello",
                "--status-json",
                str(tmp / f"{dispatch_id}.status.json"),
                "--heartbeat-interval",
                "0.05",
                "--dispatch-id",
                dispatch_id,
                "--json",
            ]

        warmed = _run(pooled_command("acp-warm-seat"), env)
        assert warmed.returncode == 0, (warmed.stdout, warmed.stderr)
        warmed_status = json.loads(
            (tmp / "acp-warm-seat.status.json").read_text(encoding="utf-8")
        )
        warmed_seat = Path(warmed_status["worktree_path"])
        unrelated_path = tmp / "unrelated-open-file"
        with unrelated_path.open("w+") as unrelated_file:
            # Keep setup subprocesses from reusing the descriptor number before
            # the dispatcher validates the inherited file identity.
            inherited_fd = fcntl.fcntl(
                unrelated_file.fileno(), fcntl.F_DUPFD, 100
            )
            unrelated_stat = os.fstat(inherited_fd)
            forced_env = env.copy()
            forced_env[goalflight_worktree_pool.OCCUPANCY_LOCK_FD_ENV] = str(
                inherited_fd
            )
            try:
                forced = subprocess.run(
                    [
                        *pooled_command("acp-forced-mismatch"),
                        "--occupied-worktree-forced",
                    ],
                    cwd=ROOT,
                    env=forced_env,
                    pass_fds=(inherited_fd,),
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=60,
                    check=False,
                )
            finally:
                os.close(inherited_fd)

        status = json.loads(
            (tmp / "acp-forced-mismatch.status.json").read_text(encoding="utf-8")
        )
        assert forced.returncode == 0, (
            forced.returncode,
            forced.stdout,
            forced.stderr,
            status.get("error"),
            status.get("reason"),
            status.get("state"),
        )
        assert status["state"] == "complete", status
        forced_seat = Path(status["worktree_path"])
        assert forced_seat != warmed_seat, status
        occupancy_lock_path = goalflight_worktree_pool.worktree_path_lock_path(
            forced_seat
        )
        expected_stat = occupancy_lock_path.stat()
        assert (unrelated_stat.st_dev, unrelated_stat.st_ino) != (
            expected_stat.st_dev,
            expected_stat.st_ino,
        )
        assert (
            "does not identify the registered worktree occupancy lock"
            in forced.stderr
        ), forced.stderr
        assert "--occupied-worktree-forced accepted" in forced.stderr, forced.stderr
        worker_env = json.loads(snapshot_path.read_text(encoding="utf-8"))
        assert worker_env[goalflight_worktree_pool.OCCUPANCY_LOCK_FD_ENV] is False, (
            worker_env,
            forced.stdout,
            forced.stderr,
        )


def test_forced_in_place_acp_does_not_pass_unconfirmed_occupancy_fd_marker() -> None:
    import pytest

    acp_py = _managed_acp_python()
    if acp_py is None:
        pytest.skip("forced ACP occupancy requires a managed ACP interpreter")
    import fcntl

    from test_acp_dispatch_sigterm import _write_fake_codex_acp_manifest

    with _temp_dir() as td:
        tmp = Path(td)
        repo = make_test_git_repo(tmp)
        env = _env(tmp)
        _configure_test_accounts(tmp, env)
        env["GOALFLIGHT_ACP_PYTHON"] = str(acp_py)
        env["GOALFLIGHT_FAKE_ACP_SCENARIO"] = "goal"
        snapshot_path = tmp / "worker-env.json"
        env["GOALFLIGHT_TEST_ACP_WORKER_ENV"] = str(snapshot_path)

        adapters = tmp / "adapters"
        _write_fake_codex_acp_manifest(adapters)
        manifest_path = adapters / "codex-acp.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        wrapper = tmp / "capture-acp-env"
        wrapper.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "from pathlib import Path\n"
            f"name = {goalflight_worktree_pool.OCCUPANCY_LOCK_FD_ENV!r}\n"
            "Path(os.environ['GOALFLIGHT_TEST_ACP_WORKER_ENV']).write_text(\n"
            "    json.dumps({name: name in os.environ}), encoding='utf-8'\n"
            ")\n"
            f"os.execv(sys.executable, [sys.executable, {str(ROOT / 'tests/fixtures/acp_fake_agent.py')!r}, *sys.argv[1:]])\n",
            encoding="utf-8",
        )
        wrapper.chmod(0o755)
        manifest["invocation"]["exec"]["binary"] = str(wrapper)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        env["GOALFLIGHT_ADAPTERS_DIR"] = str(adapters)
        env["GOALFLIGHT_ALLOW_ADAPTERS_DIR_OVERRIDE"] = "1"

        ready = tmp / "lock-holder-ready"
        release = tmp / "release-lock-holder"
        holder_code = (
            "import sys, time\n"
            f"sys.path.insert(0, {str(ROOT / 'scripts')!r})\n"
            "from pathlib import Path\n"
            "import goalflight_worktree_pool as pool\n"
            f"lock = pool.try_acquire_worktree_path_lock(Path({str(repo)!r}), 'foreign-lock-holder')\n"
            f"Path({str(ready)!r}).write_text('ready', encoding='utf-8')\n"
            f"release = Path({str(release)!r})\n"
            "while not release.exists(): time.sleep(0.01)\n"
            "lock.release()\n"
        )
        holder = subprocess.Popen(
            [sys.executable, "-c", holder_code],
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            assert _wait_for(ready.exists), (holder.poll(), holder.stderr)
            lock_path = goalflight_worktree_pool.worktree_path_lock_path(repo)
            with lock_path.open("r+") as separate_file:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(
                        separate_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB
                    )
                inherited_fd = os.dup(separate_file.fileno())
                try:
                    forced_env = env.copy()
                    forced_env[goalflight_worktree_pool.OCCUPANCY_LOCK_FD_ENV] = str(
                        inherited_fd
                    )
                    forced = subprocess.run(
                        _prompt_writer_cmd(
                            tmp,
                            repo,
                            "acp-forced-unconfirmed",
                            agent="codex-acp",
                            extra=[
                                "--shape",
                                "acp",
                                "--account",
                                "occupancy-test",
                                "--occupied-worktree-forced",
                                "--in-place",
                                "--foreground",
                            ],
                        ),
                        cwd=ROOT,
                        env=forced_env,
                        pass_fds=(inherited_fd,),
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        timeout=60,
                        check=False,
                    )
                finally:
                    os.close(inherited_fd)
        finally:
            release.write_text("release", encoding="utf-8")
            holder.communicate(timeout=10)

        status = json.loads(
            (tmp / "acp-forced-unconfirmed.status.json").read_text(encoding="utf-8")
        )
        assert forced.returncode == 0, (forced.stdout, forced.stderr, status)
        assert status["state"] == "complete", status
        worker_env = json.loads(snapshot_path.read_text(encoding="utf-8"))
        assert worker_env[goalflight_worktree_pool.OCCUPANCY_LOCK_FD_ENV] is False, (
            worker_env,
            forced.stdout,
            forced.stderr,
        )


def test_unreadable_ledger_dir_is_unknown_not_unoccupied() -> None:
    """Path.glob on chmod 000 returns []; occupancy must not treat that as free."""
    with _temp_dir() as td:
        tmp = Path(td)
        tree = tmp / "tree"
        tree.mkdir()
        env = _env(tmp)
        runs = tmp / "state" / "runs.d"
        runs.mkdir(parents=True)
        release_holder = tmp / "release-holder"
        holder_tree = tmp / "holder-tree"
        holder_tree.mkdir()
        holder = _run(
            _dispatch_cmd(
                tmp,
                holder_tree,
                "unk-dir-holder",
                _blocking_worker(release_holder, "unk-dir-holder"),
            ),
            env,
        )
        assert holder.returncode == 0, (holder.stdout, holder.stderr)
        holder_record = _wait_until_running(tmp, "unk-dir-holder")
        target_id = "unk-dir-writer"
        chmod_thread, changed = _make_runs_dir_unreadable_after_record(runs, target_id)
        try:
            pending = _popen(
                _dispatch_cmd(
                    tmp,
                    tree,
                    target_id,
                    _quick_writer(target_id),
                    extra=["--capacity-wait-s", "20"],
                ),
                env,
            )
            assert _wait_for(changed.is_set), "ledger directory was not made unreadable"
            release_holder.write_text("release", encoding="utf-8")
            out, err = _communicate(pending)
            refused = subprocess.CompletedProcess(
                pending.args, pending.returncode, out, err
            )
        finally:
            os.chmod(runs, 0o755)
            chmod_thread.join(timeout=5)
            release_holder.write_text("release", encoding="utf-8")
        assert _wait_for(lambda: _pid_gone(holder_record.get("worker_pid"))), holder_record
        assert refused.returncode == 64, (refused.returncode, refused.stdout, refused.stderr)
        assert "occupancy" in refused.stderr and "unknown" in refused.stderr, refused.stderr
        assert "Retry the dispatch" in refused.stderr, refused.stderr
        assert "already has a non-terminal ledger record" not in refused.stderr, refused.stderr
        assert "unique --dispatch-id" not in refused.stderr, refused.stderr
        assert "runs.d" in refused.stderr, refused.stderr
        refused_record = _ledger_record(tmp, target_id)
        assert refused_record, refused.stderr
        assert refused_record.get("worker_cwd") is None, refused_record
        assert refused_record.get("state") in {
            "waiting_capacity",
            "failed_worktree",
        }, refused_record
        if refused_record["state"] == "failed_worktree":
            assert refused_record.get("prelaunch_failure") is True, refused_record
        status = json.loads(
            Path(refused_record["status_path"]).read_text(encoding="utf-8")
        )
        assert status.get("state") == "failed_worktree", status
        assert status.get("terminal_state") == "error", status
        assert status.get("prelaunch_failure") is True, status
        if refused_record["state"] == "waiting_capacity":
            assert status.get("ledger_record_warning"), status


def test_genuine_duplicate_dispatch_id_is_still_refused() -> None:
    """A readable non-terminal row for this id is still a duplicate, not occupancy."""
    with _temp_dir() as td:
        tmp = Path(td)
        tree = tmp / "tree"
        other = tmp / "other-tree"
        tree.mkdir()
        other.mkdir()
        env = _env(tmp)
        _write_runs_record(
            tmp,
            "dup-id",
            state="running",
            worker_cwd=str(other.resolve()),
            project_root=str(other.resolve()),
        )
        refused = _run(_dispatch_cmd(tmp, tree, "dup-id", _quick_writer("dup-id")), env)
        assert refused.returncode == 64, (refused.returncode, refused.stdout, refused.stderr)
        assert "already has a non-terminal ledger record" in refused.stderr, refused.stderr
        assert "dup-id" in refused.stderr, refused.stderr
        assert "DISPATCH-LAUNCHED" not in refused.stdout, refused.stdout
        planted = _ledger_record(tmp, "dup-id")
        assert planted.get("state") == "running", planted


def test_cwdless_nonterminal_record_does_not_block_unrelated_worktree() -> None:
    """A readable running row with worker_cwd=None names no path.

    Live pid on a different project_root must not fail-close every worktree:
    missing cwd is not occupancy-unknown of the target.
    """
    with _temp_dir() as td:
        tmp = Path(td)
        tree = tmp / "tree"
        tree.mkdir()
        env = _env(tmp)
        _write_runs_record(
            tmp,
            "cwdless-live",
            worker_cwd=None,
            worker_pid=os.getpid(),
            worker_identity=ledger.process_identity(os.getpid()),
            project_root="/unrelated/other-project",
        )
        launched = _run(
            _dispatch_cmd(
                tmp, tree, "cwdless-writer", _quick_writer("cwdless-writer"), foreground=True
            ),
            env,
        )
        assert launched.returncode == 0, (launched.stdout, launched.stderr)
        assert "DISPATCH-END" in launched.stdout, launched.stdout
        assert "occupancy" not in launched.stderr or "unknown" not in launched.stderr, launched.stderr
        assert "cwdless-live" in launched.stderr, launched.stderr
        assert "names no worker cwd" in launched.stderr, launched.stderr
        assert "occupancy skip" in launched.stderr, launched.stderr
        assert "(state=running)" in launched.stderr, launched.stderr


def test_cwdless_dead_identity_does_not_block() -> None:
    """Running + recorded pid gone + no cwd is not evidence about any tree."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        identity = ledger.process_identity(proc.pid)
        pid = proc.pid
        assert identity is not None, pid
    finally:
        proc.kill()
        proc.wait(timeout=5)
    liveness, reason = ledger.worker_identity_liveness(
        {"worker_pid": pid, "worker_identity": identity}
    )
    assert liveness == "dead", (liveness, reason)
    with _temp_dir() as td:
        tmp = Path(td)
        tree = tmp / "tree"
        tree.mkdir()
        env = _env(tmp)
        _write_runs_record(
            tmp,
            "cwdless-dead",
            worker_cwd=None,
            worker_pid=pid,
            worker_identity=identity,
            project_root="/unrelated/other-project",
        )
        launched = _run(
            _dispatch_cmd(
                tmp, tree, "after-cwdless-dead", _quick_writer("after-cwdless-dead"), foreground=True
            ),
            env,
        )
        assert launched.returncode == 0, (launched.stdout, launched.stderr)
        assert "DISPATCH-END" in launched.stdout, launched.stdout
        assert "occupancy" not in launched.stderr or "unknown" not in launched.stderr, launched.stderr
        assert "cwdless-dead" in launched.stderr, launched.stderr
        assert "names no worker cwd" in launched.stderr, launched.stderr


def test_matching_project_root_writer_without_in_place_is_refused() -> None:
    """A matching project root without explicit in-place is refused early."""
    with _temp_dir() as td:
        tmp = Path(td)
        tree = tmp / "tree"
        tree.mkdir()
        env = _env(tmp, capacity_max_total=2)
        identity = ledger.process_identity(os.getpid())
        assert identity is not None, os.getpid()
        _write_runs_record(
            tmp,
            "root-only",
            worker_cwd=None,
            worker_pid=os.getpid(),
            worker_identity=identity,
            project_root=str(tree.resolve()),
        )
        refused = _run(
            _dispatch_cmd(
                tmp,
                tree,
                "root-only-writer",
                _quick_writer("root-only-writer"),
                in_place=False,
            ),
            env,
        )
        assert refused.returncode == 64, (refused.returncode, refused.stdout, refused.stderr)
        assert "project_root" in refused.stderr or "project root" in refused.stderr
        assert "Pass --in-place explicitly to write into the project root." in refused.stderr, refused.stderr
        _assert_failed_worktree_record(tmp, "root-only-writer")


def test_matching_project_root_without_cwd_skips_when_identity_not_live() -> None:
    """No live identity + matching project_root is still a skip, not a refuse.

    Proven-dead / missing identity is the fleet-outage class: reconcile
    closes those. project_root alone is not a path claim.
    """
    with _temp_dir() as td:
        tmp = Path(td)
        tree = tmp / "tree"
        tree.mkdir()
        env = _env(tmp)
        _write_runs_record(
            tmp,
            "root-only-nopid",
            worker_cwd=None,
            project_root=str(tree.resolve()),
        )
        launched = _run(
            _dispatch_cmd(
                tmp,
                tree,
                "root-only-nopid-writer",
                _quick_writer("root-only-nopid-writer"),
                foreground=True,
            ),
            env,
        )
        assert launched.returncode == 0, (launched.stdout, launched.stderr)
        assert "DISPATCH-END" in launched.stdout, launched.stdout
        assert "occupancy" not in launched.stderr or "unknown" not in launched.stderr, launched.stderr
        assert "root-only-nopid" in launched.stderr, launched.stderr
        assert "names no worker cwd" in launched.stderr, launched.stderr
        assert "occupancy skip" in launched.stderr, launched.stderr


def test_different_project_root_with_matching_cwd_still_occupies() -> None:
    """Linked/shared worktrees: matching worker_cwd occupies even if root differs.

    Starting (post-admission, pre-spawn) rather than running: a running row whose
    worker is gone yields the kernel lock, and the SIGKILL recovery path
    then proceeds. Occupancy of this path is the starting row.
    """
    with _temp_dir() as td:
        tmp = Path(td)
        tree = tmp / "tree"
        tree.mkdir()
        env = _env(tmp)
        _write_runs_record(
            tmp,
            "linked-cwd",
            state="starting",
            worker_cwd=str(tree.resolve()),
            project_root="/unrelated/other-project",
        )
        refused = _run(_dispatch_cmd(tmp, tree, "linked-second", _quick_writer("linked-second")), env)
        assert refused.returncode == 64, (refused.returncode, refused.stdout, refused.stderr)
        assert "linked-cwd" in refused.stderr, refused.stderr
        _assert_failed_worktree_record(tmp, "linked-second")


def test_cwdless_field_with_argv_cwd_matching_target_still_blocks() -> None:
    """dispatch_argv --cwd is path evidence even when worker_cwd is unset."""
    with _temp_dir() as td:
        tmp = Path(td)
        tree = tmp / "tree"
        tree.mkdir()
        env = _env(tmp)
        _write_runs_record(
            tmp,
            "argv-cwd",
            state="starting",
            worker_cwd=None,
            dispatch_argv=["--cwd", str(tree.resolve()), "--agent", "test"],
        )
        refused = _run(_dispatch_cmd(tmp, tree, "argv-second", _quick_writer("argv-second")), env)
        assert refused.returncode == 64, (refused.returncode, refused.stdout, refused.stderr)
        assert "argv-cwd" in refused.stderr, refused.stderr
        _assert_failed_worktree_record(tmp, "argv-second")
        assert "occupancy skip" not in refused.stderr, refused.stderr


def test_argv_cwd_and_request_cwd_disagree_occupy_both_trees() -> None:
    """No top-level worker_cwd, but argv --cwd and request.cwd both exist.

    Live measured shape: missing worker_cwd is not nameless when argv or
    request still names a path. Disagreeing sources that both exist occupy
    every named tree (refuse on a superset).
    """
    with _temp_dir() as td:
        tmp = Path(td)
        tree_a = tmp / "tree-a"
        tree_b = tmp / "tree-b"
        tree_a.mkdir()
        tree_b.mkdir()
        env = _env(tmp)
        _write_runs_record(
            tmp,
            "split-cwd",
            state="starting",
            worker_cwd=None,
            dispatch_argv=["--cwd", str(tree_a.resolve()), "--agent", "test"],
            request={"cwd": str(tree_b.resolve())},
            project_root=str(tree_b.resolve()),
        )
        refused_a = _run(
            _dispatch_cmd(tmp, tree_a, "split-a", _quick_writer("split-a")), env
        )
        assert refused_a.returncode == 64, (
            refused_a.returncode, refused_a.stdout, refused_a.stderr
        )
        assert "split-cwd" in refused_a.stderr, refused_a.stderr
        _assert_failed_worktree_record(tmp, "split-a")

        refused_b = _run(
            _dispatch_cmd(tmp, tree_b, "split-b", _quick_writer("split-b")), env
        )
        assert refused_b.returncode == 64, (
            refused_b.returncode, refused_b.stdout, refused_b.stderr
        )
        assert "split-cwd" in refused_b.stderr, refused_b.stderr
        _assert_failed_worktree_record(tmp, "split-b")


def test_relative_worker_cwd_resolved_against_project_root_occupies() -> None:
    """Relative worker_cwd is not nameless when project_root can place it."""
    with _temp_dir() as td:
        tmp = Path(td)
        parent = tmp / "repo"
        tree = parent / "tree"
        parent.mkdir()
        tree.mkdir()
        env = _env(tmp)
        _write_runs_record(
            tmp,
            "rel-cwd",
            state="starting",
            worker_cwd="tree",
            project_root=str(parent.resolve()),
        )
        refused = _run(
            _dispatch_cmd(tmp, tree, "rel-second", _quick_writer("rel-second")), env
        )
        assert refused.returncode == 64, (refused.returncode, refused.stdout, refused.stderr)
        assert "rel-cwd" in refused.stderr, refused.stderr
        _assert_failed_worktree_record(tmp, "rel-second")


def test_cwd_after_double_dash_in_argv_is_path_evidence() -> None:
    """--cwd recorded after -- still names the tree; the row is not nameless."""
    with _temp_dir() as td:
        tmp = Path(td)
        tree = tmp / "tree"
        tree.mkdir()
        env = _env(tmp)
        _write_runs_record(
            tmp,
            "argv-after",
            state="starting",
            worker_cwd=None,
            dispatch_argv=["--agent", "test", "--", "--cwd", str(tree.resolve())],
        )
        refused = _run(
            _dispatch_cmd(tmp, tree, "argv-after-second", _quick_writer("argv-after-second")),
            env,
        )
        assert refused.returncode == 64, (refused.returncode, refused.stdout, refused.stderr)
        assert "argv-after" in refused.stderr, refused.stderr
        _assert_failed_worktree_record(tmp, "argv-after-second")


def test_live_cwdless_matching_project_root_second_writer_is_refused() -> None:
    """Item-4 hole: planted live nameless matching-root row refuses a writer.

    Concurrent empty-ledger dual-launch (0 extra writes) does not cover a
    pre-existing cwd-less live identity in this project.
    """
    with _temp_dir() as td:
        tmp = Path(td)
        tree = tmp / "tree"
        tree.mkdir()
        env = _env(tmp)
        identity = ledger.process_identity(os.getpid())
        assert identity is not None, os.getpid()
        _write_runs_record(
            tmp,
            "live-cwdless",
            worker_cwd=None,
            worker_pid=os.getpid(),
            worker_identity=identity,
            project_root=str(tree.resolve()),
        )
        refused = _run(
            _dispatch_cmd(
                tmp,
                tree,
                "after-live-cwdless",
                _quick_writer("after-live-cwdless"),
                in_place=False,
            ),
            env,
        )
        assert refused.returncode == 64, (refused.returncode, refused.stdout, refused.stderr)
        assert "project_root" in refused.stderr or "project root" in refused.stderr
        assert "Pass --in-place explicitly to write into the project root." in refused.stderr, refused.stderr
        _assert_failed_worktree_record(tmp, "after-live-cwdless")


def test_synthetic_starting_record_with_target_cwd_still_blocks() -> None:
    """A post-admission starting row whose worker_cwd is the target occupies it."""
    with _temp_dir() as td:
        tmp = Path(td)
        tree = tmp / "tree"
        tree.mkdir()
        env = _env(tmp)
        _write_runs_record(
            tmp,
            "synth-cwd",
            state="starting",
            worker_cwd=str(tree.resolve()),
        )
        refused = _run(_dispatch_cmd(tmp, tree, "synth-second", _quick_writer("synth-second")), env)
        assert refused.returncode == 64, (refused.returncode, refused.stdout, refused.stderr)
        assert "synth-cwd" in refused.stderr, refused.stderr
        _assert_failed_worktree_record(tmp, "synth-second")


def test_live_watcher_stopped_incumbent_still_occupies() -> None:
    with _temp_dir() as td:
        tmp = Path(td)
        tree = tmp / "tree"
        tree.mkdir()
        env = _env(tmp)
        runs = tmp / "state" / "runs.d"
        runs.mkdir(parents=True)
        record = {
            "schema": ledger.SCHEMA,
            "dispatch_id": "ws-live",
            "agent": "test",
            "shape": "bash",
            "state": "watcher_stopped",
            "worker_alive": True,
            "worker_cwd": str(tree.resolve()),
            "os_sandbox": {"requested_profile": "workspace-write"},
        }
        (runs / "ws-live.json").write_text(json.dumps(record), encoding="utf-8")
        refused = _run(_dispatch_cmd(tmp, tree, "ws-second", _quick_writer("ws-second")), env)
        assert refused.returncode == 64, (refused.returncode, refused.stdout, refused.stderr)
        assert "ws-live" in refused.stderr, refused.stderr
        _assert_failed_worktree_record(tmp, "ws-second")


def test_dead_watcher_stopped_incumbent_vacates_the_tree() -> None:
    with _temp_dir() as td:
        tmp = Path(td)
        tree = tmp / "tree"
        tree.mkdir()
        env = _env(tmp)
        runs = tmp / "state" / "runs.d"
        runs.mkdir(parents=True)
        record = {
            "schema": ledger.SCHEMA,
            "dispatch_id": "ws-dead",
            "agent": "test",
            "shape": "bash",
            "state": "watcher_stopped",
            "worker_alive": False,
            "worker_cwd": str(tree.resolve()),
            "os_sandbox": {"requested_profile": "workspace-write"},
        }
        (runs / "ws-dead.json").write_text(json.dumps(record), encoding="utf-8")
        launched = _run(
            _dispatch_cmd(tmp, tree, "ws-after-dead", _quick_writer("ws-after-dead"), foreground=True),
            env,
        )
        assert launched.returncode == 0, (launched.stdout, launched.stderr)
        assert "DISPATCH-END" in launched.stdout, launched.stdout


def test_fleet_ssh_incumbent_does_not_occupy_local_cwd() -> None:
    """A remote worker's cwd is a path on another node's disk."""
    with _temp_dir() as td:
        tmp = Path(td)
        tree = tmp / "tree"
        tree.mkdir()
        env = _env(tmp)
        runs = tmp / "state" / "runs.d"
        runs.mkdir(parents=True)
        record = {
            "schema": ledger.SCHEMA,
            "dispatch_id": "fleet-remote",
            "state": "running",
            "transport": "fleet-ssh",
            "hostname": "other-node.example",
            "worker_cwd": str(tree.resolve()),
        }
        (runs / "fleet-remote.json").write_text(json.dumps(record), encoding="utf-8")
        launched = _run(
            _dispatch_cmd(tmp, tree, "local-writer", _quick_writer("local-writer"), foreground=True),
            env,
        )
        assert launched.returncode == 0, (launched.stdout, launched.stderr)
        assert "DISPATCH-END" in launched.stdout, launched.stdout


def test_supported_profile_alone_does_not_mark_a_writer_read_only() -> None:
    assert (
        dispatch._record_declared_read_only(
            {
                "os_sandbox": {
                    "requested_profile": "workspace-write",
                    "supported_profile": "read-only",
                    "enforced_profile": "workspace-write",
                }
            }
        )
        is False
    )
    assert (
        dispatch._record_declared_read_only(
            {
                "read_only": True,
                "os_sandbox": {"requested_profile": "workspace-write"},
            }
        )
        is False
    )
    assert (
        dispatch._record_declared_read_only(
            {"os_sandbox": {"requested_profile": "read-only"}, "agent": "test"}
        )
        is False
    )
    assert (
        dispatch._record_declared_read_only(
            {"os_sandbox": {"requested_profile": "read-only"}, "agent": "grok-code"}
        )
        is True
    )


def test_help_documents_occupied_worktree_override() -> None:
    proc = _run([sys.executable, str(DISPATCH), "--help"], os.environ.copy())
    assert proc.returncode == 0, proc.stderr
    assert "--occupied-worktree-forced" in proc.stdout, proc.stdout


def _reap(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is None:
        with contextlib.suppress(OSError):
            proc.kill()
    with contextlib.suppress(Exception):
        proc.wait(timeout=5)


def _popen(cmd: list[str], env: dict[str, str]) -> subprocess.Popen[str]:
    return subprocess.Popen(
        cmd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _communicate(
    proc: subprocess.Popen[str], *, timeout: float = 60.0
) -> tuple[str, str]:
    try:
        return proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _reap(proc)
        raise


def test_concurrent_second_writer_is_refused_on_every_trial() -> None:
    """The occupancy lock, not the ledger read, serializes two overlapping launches."""
    trials = 8
    both_wrote = 0
    refused_one = 0
    for i in range(trials):
        with _temp_dir(prefix=f"gf-occ-conc-{i}-") as td:
            tmp = Path(td)
            tree = tmp / "tree"
            tree.mkdir()
            env = _env(tmp, capacity_max_total=2)
            marker_a = tree / "wrote-by-w-a"
            marker_b = tree / "wrote-by-w-b"
            writer_a = (
                "from pathlib import Path\n"
                f"Path({str(marker_a)!r}).write_text('wrote-by-w-a', encoding='utf-8')\n"
                "print('COMPLETE: conc-a — wrote', flush=True)\n"
            )
            writer_b = (
                "from pathlib import Path\n"
                f"Path({str(marker_b)!r}).write_text('wrote-by-w-b', encoding='utf-8')\n"
                "print('COMPLETE: conc-b — wrote', flush=True)\n"
            )
            pa = _popen(
                _dispatch_cmd(tmp, tree, f"conc-a-{i}", writer_a, foreground=True),
                env,
            )
            pb = _popen(
                _dispatch_cmd(tmp, tree, f"conc-b-{i}", writer_b, foreground=True),
                env,
            )
            try:
                out_a, err_a = _communicate(pa)
                out_b, err_b = _communicate(pb)
            finally:
                _reap(pa)
                _reap(pb)
            wrote_pair = marker_a.exists() and marker_b.exists()
            if wrote_pair:
                both_wrote += 1
            rcs = {pa.returncode, pb.returncode}
            if 64 in rcs:
                refused_one += 1
            assert not wrote_pair, (
                f"trial {i}: concurrent dual-write "
                f"rc=({pa.returncode},{pb.returncode}) err_a={err_a[-400:]!r} "
                f"err_b={err_b[-400:]!r}"
            )
            assert 64 in rcs, (
                f"trial {i}: expected one rc 64, got ({pa.returncode},{pb.returncode}) "
                f"err_a={err_a[-400:]!r} err_b={err_b[-400:]!r}"
            )
    assert both_wrote == 0, f"concurrent dual-write on {both_wrote}/{trials} trials"
    assert refused_one == trials, f"rc 64 on {refused_one}/{trials} trials"


def test_worker_inherits_occupancy_lock_fd() -> None:
    with _temp_dir() as td:
        tmp = Path(td)
        tree = tmp / "tree"
        tree.mkdir()
        env = _env(tmp)
        held = tmp / "held-fd"
        worker = (
            "import os\n"
            "from pathlib import Path\n"
            "fd = int(os.environ['GOALFLIGHT_OCCUPANCY_LOCK_FD'])\n"
            "os.fstat(fd)\n"
            f"Path({str(held)!r}).write_text(str(fd), encoding='utf-8')\n"
            "print('COMPLETE: occ-inherit — held', flush=True)\n"
        )
        launched = _run(
            _dispatch_cmd(tmp, tree, "occ-inherit", worker, foreground=True),
            env,
        )
        assert launched.returncode == 0, (launched.stdout, launched.stderr)
        assert held.exists(), launched.stderr
        assert int(held.read_text(encoding="utf-8")) >= 0


def test_sigkill_releases_occupancy_before_capacity_lease_cleanup() -> None:
    with _temp_dir() as td:
        tmp = Path(td)
        tree = tmp / "tree"
        tree.mkdir()
        env = _env(tmp, capacity_max_total=2)
        release = tmp / "release-incumbent"
        incumbent = _run(
            _dispatch_cmd(tmp, tree, "occ-kill", _blocking_worker(release, "occ-kill")),
            env,
        )
        assert incumbent.returncode == 0, (incumbent.stdout, incumbent.stderr)
        record = _wait_until_running(tmp, "occ-kill")
        worker_pid = int(record["worker_pid"])
        os.kill(worker_pid, signal.SIGKILL)
        assert _wait_for(lambda: _pid_gone(worker_pid), timeout=10.0), worker_pid
        # Kernel release is immediate; watcher must not keep the claim.
        second = _run(
            _dispatch_cmd(
                tmp, tree, "occ-after-kill", _quick_writer("occ-after-kill"), foreground=True
            ),
            env,
        )
        assert second.returncode == 0, (second.stdout, second.stderr)
        assert "already owned" not in second.stderr, second.stderr
        assert "DISPATCH-END" in second.stdout, second.stdout
        watcher_pid = record.get("watcher_pid")
        if watcher_pid:
            _wait_for(lambda pid=watcher_pid: _pid_gone(pid), timeout=5.0)


def test_sigkill_running_row_survives_first_check_and_admission_recheck() -> None:
    with _temp_dir() as td:
        tmp = Path(td)
        tree = tmp / "tree"
        tree.mkdir()
        env = _env(tmp)
        _write_runs_record(
            tmp,
            "stale-running",
            state="running",
            worker_cwd=str(tree.resolve()),
            worker_pid=99999999,
        )
        args = SimpleNamespace(
            agent="test",
            shape="bash",
            project_root=str(tree),
            cwd=str(tree),
            in_place=True,
            dispatch_id="sigkill-taker",
            occupied_worktree_forced=False,
            from_queue=False,
            capacity_wait_s=0,
            dispatch_warnings=[],
        )
        record_path = tmp / "state" / "runs.d" / "stale-running.json"

        with patch.dict(os.environ, env, clear=True):
            try:
                dispatch._prepare_explicit_worktree_occupancy(args)
                lock = args._worktree_occupancy_lock
                lock_fd = lock.fileno()
                assert json.loads(
                    record_path.read_text(encoding="utf-8")
                )["state"] == "running"

                with patch.object(
                    dispatch, "_bind_dispatch_worktree", return_value=None
                ):
                    assert dispatch._admit_dispatch_worktree(args) is None

                assert json.loads(
                    record_path.read_text(encoding="utf-8")
                )["state"] == "running"
                assert args._worktree_occupancy_lock is lock
                assert lock.fileno() == lock_fd
                with contextlib.suppress(goalflight_worktree_pool.WorktreePathLockBusy):
                    probe = goalflight_worktree_pool.try_acquire_worktree_path_lock(
                        tree, "third-writer"
                    )
                    probe.release()
                    raise AssertionError("admission dropped the SIGKILL reclaim lock")
            finally:
                dispatch._release_worktree_occupancy_lock(args)


def test_unrelated_inherited_fd_does_not_claim_running_worktree() -> None:
    with _temp_dir() as td:
        tmp = Path(td)
        tree = tmp / "tree"
        tree.mkdir()
        env = _env(tmp)
        _write_runs_record(
            tmp,
            "live-holder",
            state="running",
            worker_cwd=str(tree.resolve()),
            worker_pid=os.getpid(),
        )
        args = SimpleNamespace(
            agent="test",
            shape="bash",
            project_root=str(tree),
            cwd=str(tree),
            in_place=True,
            dispatch_id="unrelated-fd-taker",
            occupied_worktree_forced=False,
            from_queue=False,
            capacity_wait_s=0,
            dispatch_warnings=[],
        )
        unrelated_path = tmp / "unrelated-open-file"
        expected_lock_path = goalflight_worktree_pool.worktree_path_lock_path(tree)

        with patch.dict(os.environ, env, clear=True):
            live_lock = goalflight_worktree_pool.try_acquire_worktree_path_lock(
                tree, "live-holder"
            )
            try:
                with unrelated_path.open("w+") as unrelated_file:
                    os.environ[
                        goalflight_worktree_pool.OCCUPANCY_LOCK_FD_ENV
                    ] = str(unrelated_file.fileno())
                    assert (
                        os.fstat(unrelated_file.fileno()).st_dev,
                        os.fstat(unrelated_file.fileno()).st_ino,
                    ) != (
                        expected_lock_path.stat().st_dev,
                        expected_lock_path.stat().st_ino,
                    )
                    assert not hasattr(args, "_worktree_occupancy_lock")
                    try:
                        dispatch._prepare_attempt_worktree_occupancy(args)
                    except dispatch.DispatchUsageError:
                        assert not hasattr(args, "_worktree_occupancy_lock")
                        assert (
                            goalflight_worktree_pool.OCCUPANCY_LOCK_FD_ENV
                            not in os.environ
                        )
                    else:
                        dispatch._release_worktree_occupancy_lock(args)
                        raise AssertionError(
                            "unrelated inherited fd was accepted as worktree ownership"
                        )
                    args.occupied_worktree_forced = True
                    os.environ[
                        goalflight_worktree_pool.OCCUPANCY_LOCK_FD_ENV
                    ] = str(unrelated_file.fileno())
                    warning = dispatch._prepare_attempt_worktree_occupancy(args)
                    assert warning is not None and (
                        "--occupied-worktree-forced accepted" in warning
                    )
                    assert not hasattr(args, "_worktree_occupancy_lock")
                    assert (
                        goalflight_worktree_pool.OCCUPANCY_LOCK_FD_ENV
                        not in os.environ
                    )
            finally:
                live_lock.release()


def test_concurrent_different_trees_reach_capacity_independently() -> None:
    with _temp_dir() as td:
        tmp = Path(td)
        tree_a = tmp / "tree-a"
        tree_b = tmp / "tree-b"
        tree_a.mkdir()
        tree_b.mkdir()
        env = _env(tmp)
        marker_a = tree_a / "wrote-a"
        marker_b = tree_b / "wrote-b"
        writer_a = (
            "from pathlib import Path\n"
            "import time\n"
            f"Path({str(marker_a)!r}).write_text('a', encoding='utf-8')\n"
            "time.sleep(0.3)\n"
            "print('COMPLETE: diff-a — wrote', flush=True)\n"
        )
        writer_b = (
            "from pathlib import Path\n"
            "import time\n"
            f"Path({str(marker_b)!r}).write_text('b', encoding='utf-8')\n"
            "time.sleep(0.3)\n"
            "print('COMPLETE: diff-b — wrote', flush=True)\n"
        )
        pa = _popen(
            _dispatch_cmd(tmp, tree_a, "diff-a", writer_a, foreground=True),
            env,
        )
        pb = _popen(
            _dispatch_cmd(tmp, tree_b, "diff-b", writer_b, foreground=True),
            env,
        )
        try:
            out_a, err_a = _communicate(pa)
            out_b, err_b = _communicate(pb)
        finally:
            _reap(pa)
            _reap(pb)
        assert 64 not in {pa.returncode, pb.returncode}, (
            pa.returncode, pb.returncode, err_a[-400:], err_b[-400:]
        )
        assert sorted((pa.returncode, pb.returncode)) == [0, 2], (
            pa.returncode, pb.returncode, out_a[-400:], out_b[-400:]
        )
        assert sum((marker_a.exists(), marker_b.exists())) == 1, (out_a[-400:], out_b[-400:])
        blocked_out = out_a if pa.returncode == 2 else out_b
        blocked_err = err_a if pa.returncode == 2 else err_b
        assert "DISPATCH-BLOCKED" in blocked_out, blocked_out
        assert "already owned" not in blocked_err, blocked_err


def test_declared_read_only_raw_incumbent_occupies_the_tree() -> None:
    """A write-capable `--` worker that declared --read-only still occupies."""
    if _skip_read_only_sandbox_case("declared read-only raw incumbent"):
        return
    with _temp_dir() as td, _non_temp_tree("gf-occ-rohold-tree-") as tree_td:
        tmp = Path(td)
        tree = Path(tree_td).resolve()
        env = _env(tmp, capacity_max_total=2)
        release_holder = tmp / "release-holder"
        holder = _run(
            _dispatch_cmd(
                tmp,
                tree,
                "ro-holder",
                _blocking_worker(release_holder, "ro-holder"),
                extra=["--read-only"],
            ),
            env,
            cwd=Path(tree_td),
        )
        assert holder.returncode == 0, (holder.stdout, holder.stderr)
        try:
            record = _wait_until_running(tmp, "ro-holder")
            posture = record.get("os_sandbox") or {}
            assert posture.get("requested_profile") == "read-only", record
            writer = _run(
                _dispatch_cmd(tmp, tree, "rw-writer", _quick_writer("rw-writer"), foreground=True),
                env,
            )
            assert writer.returncode == 64, (writer.returncode, writer.stdout, writer.stderr)
            assert "ro-holder" in writer.stderr, writer.stderr
        finally:
            release_holder.write_text("release", encoding="utf-8")
        _wait_until_terminal(tmp, "ro-holder")


def test_enforced_read_only_incumbent_does_not_block_a_writer() -> None:
    with _temp_dir() as td:
        tmp = Path(td)
        tree = tmp / "tree"
        tree.mkdir()
        env = _env(tmp)
        runs = tmp / "state" / "runs.d"
        runs.mkdir(parents=True)
        record = {
            "schema": ledger.SCHEMA,
            "dispatch_id": "ro-grok-holder",
            "agent": "grok-code",
            "shape": "bash",
            "state": "running",
            "worker_cwd": str(tree.resolve()),
            "os_sandbox": {"requested_profile": "read-only"},
        }
        (runs / "ro-grok-holder.json").write_text(json.dumps(record), encoding="utf-8")
        writer = _run(
            _dispatch_cmd(tmp, tree, "rw-after-reviewer", _quick_writer("rw-after-reviewer"), foreground=True),
            env,
        )
        assert writer.returncode == 0, (writer.stdout, writer.stderr)
        assert "DISPATCH-END" in writer.stdout, writer.stdout


def _launched_json(stdout: str) -> dict:
    for prefix in ("DISPATCH-LAUNCHED ", "DISPATCH-START "):
        for line in stdout.splitlines():
            if line.startswith(prefix):
                try:
                    payload = json.loads(line[len(prefix) :])
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict):
                    return payload
    return {}


def _open_paths_for_pid(pid: int) -> list[str]:
    proc = subprocess.run(
        ["lsof", "-p", str(pid), "-Fn"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    return [line[1:] for line in proc.stdout.splitlines() if line.startswith("n")]


def _pid_holds_path(pid: int, path: Path) -> bool:
    try:
        target = str(path.resolve())
    except OSError:
        target = str(path)
    for raw in _open_paths_for_pid(pid):
        try:
            if str(Path(raw).resolve()) == target:
                return True
        except OSError:
            if raw == target or raw.endswith(path.name):
                return True
    return False


def _command_for_pid(pid: int) -> str:
    proc = subprocess.run(
        ["ps", "-p", str(pid), "-o", "command="],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    return (proc.stdout or "").strip()


def test_occupancy_lock_fd_stays_out_of_sidecar_fd_tables() -> None:
    """Inspect fd tables: worker holds occupancy; watcher/caffeinate/redact do not."""
    with _temp_dir() as td:
        tmp = Path(td)
        tree = tmp / "tree"
        tree.mkdir()
        env = _env(tmp)
        release = tmp / "release-fd-table"
        launched = _run(
            _dispatch_cmd(
                tmp, tree, "occ-fd-table", _blocking_worker(release, "occ-fd-table")
            ),
            env,
        )
        assert launched.returncode == 0, (launched.stdout, launched.stderr)
        record = _wait_until_running(tmp, "occ-fd-table")
        worker_pid = int(record["worker_pid"])
        watcher_pid = record.get("watcher_pid")
        payload = _launched_json(launched.stdout)
        caffeinate_pid = payload.get("caffeinate_pid") or record.get("caffeinate_pid")
        lock_path = goalflight_worktree_pool.worktree_path_lock_path(tree)
        assert lock_path.exists(), lock_path
        assert _pid_holds_path(worker_pid, lock_path), (
            worker_pid,
            _open_paths_for_pid(worker_pid),
            lock_path,
        )
        sidecar_pids = []
        if watcher_pid:
            sidecar_pids.append(int(watcher_pid))
        if caffeinate_pid:
            sidecar_pids.append(int(caffeinate_pid))
        for pid in sidecar_pids:
            assert not _pid_holds_path(pid, lock_path), (
                pid,
                _command_for_pid(pid),
                _open_paths_for_pid(pid),
                lock_path,
            )
        redact_holders = []
        lsof = subprocess.run(
            ["lsof", "-t", str(lock_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        for line in lsof.stdout.splitlines():
            if not line.strip().isdigit():
                continue
            pid = int(line.strip())
            if pid == worker_pid or pid in sidecar_pids:
                continue
            command = _command_for_pid(pid)
            if "goalflight_output_redact.py" in command or "goalflight_watch.py" in command:
                redact_holders.append((pid, command))
        assert redact_holders == [], redact_holders
        release.write_text("release", encoding="utf-8")
        _wait_until_terminal(tmp, "occ-fd-table")


def _n_way_occupancy_trials(n: int, trials: int) -> None:
    extra_wrote = 0
    refused_enough = 0
    for i in range(trials):
        with _temp_dir(prefix=f"gf-occ-{n}way-{i}-") as td:
            tmp = Path(td)
            tree = tmp / "tree"
            tree.mkdir()
            env = _env(tmp, capacity_max_total=n)
            markers = [tree / f"wrote-{j}" for j in range(n)]
            procs = []
            for j, marker in enumerate(markers):
                writer = (
                    "from pathlib import Path\n"
                    f"Path({str(marker)!r}).write_text('wrote-{j}', encoding='utf-8')\n"
                    f"print('COMPLETE: {n}way-{j} — wrote', flush=True)\n"
                )
                procs.append(
                    _popen(
                        _dispatch_cmd(
                            tmp, tree, f"{n}way-{j}-{i}", writer, foreground=True
                        ),
                        env,
                    )
                )
            try:
                for proc in procs:
                    _communicate(proc)
            finally:
                for proc in procs:
                    _reap(proc)
            wrote = sum(1 for marker in markers if marker.exists())
            if wrote > 1:
                extra_wrote += 1
            rcs = [proc.returncode for proc in procs]
            if rcs.count(64) >= n - 1:
                refused_enough += 1
            assert wrote <= 1, (
                f"{n}-way trial {i}: {wrote} writers wrote rcs={rcs}"
            )
            assert rcs.count(64) >= n - 1, (
                f"{n}-way trial {i}: expected >= {n - 1} rc 64, got {rcs}"
            )
    assert extra_wrote == 0, f"{n}-way extra writers on {extra_wrote}/{trials} trials"
    assert refused_enough == trials, (
        f"{n}-way rc 64 on {refused_enough}/{trials} trials"
    )


def test_concurrent_three_writers_are_refused_on_every_trial() -> None:
    _n_way_occupancy_trials(3, 8)


def test_concurrent_four_writers_are_refused_on_every_trial() -> None:
    _n_way_occupancy_trials(4, 8)


if __name__ == "__main__":
    test_second_writer_refused_and_ordinary_force_rejected()
    test_declared_read_only_raw_worker_is_refused_into_occupied_worktree()
    test_enforced_read_only_reviewer_skips_occupancy()
    test_terminal_incumbent_vacates_the_tree()
    test_unreadable_ledger_record_is_unknown_not_unoccupied()
    test_unreadable_ledger_dir_is_unknown_not_unoccupied()
    test_genuine_duplicate_dispatch_id_is_still_refused()
    test_cwdless_nonterminal_record_does_not_block_unrelated_worktree()
    test_cwdless_dead_identity_does_not_block()
    test_matching_project_root_without_cwd_does_not_occupy()
    test_matching_project_root_without_cwd_skips_when_identity_not_live()
    test_different_project_root_with_matching_cwd_still_occupies()
    test_cwdless_field_with_argv_cwd_matching_target_still_blocks()
    test_argv_cwd_and_request_cwd_disagree_occupy_both_trees()
    test_relative_worker_cwd_resolved_against_project_root_occupies()
    test_cwd_after_double_dash_in_argv_is_path_evidence()
    test_live_cwdless_matching_project_root_second_writer_is_refused()
    test_synthetic_starting_record_with_target_cwd_still_blocks()
    test_preset_bash_writer_refused_into_occupied_worktree()
    test_acp_writer_refused_into_occupied_worktree()
    test_live_watcher_stopped_incumbent_still_occupies()
    test_dead_watcher_stopped_incumbent_vacates_the_tree()
    test_fleet_ssh_incumbent_does_not_occupy_local_cwd()
    test_supported_profile_alone_does_not_mark_a_writer_read_only()
    test_help_documents_occupied_worktree_override()
    test_declared_read_only_raw_incumbent_occupies_the_tree()
    test_enforced_read_only_incumbent_does_not_block_a_writer()
    test_concurrent_second_writer_is_refused_on_every_trial()
    test_worker_inherits_occupancy_lock_fd()
    test_sigkill_releases_occupancy_before_capacity_lease_cleanup()
    test_sigkill_running_row_survives_first_check_and_admission_recheck()
    test_unrelated_inherited_fd_does_not_claim_running_worktree()
    test_occupancy_lock_fd_stays_out_of_sidecar_fd_tables()
    test_concurrent_different_trees_reach_capacity_independently()
    test_concurrent_three_writers_are_refused_on_every_trial()
    test_concurrent_four_writers_are_refused_on_every_trial()
    print("PASS: test_dispatch_worktree_occupancy")
