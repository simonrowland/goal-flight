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
dispatched workers with genuine ledger records, read by the real reader; the
queued-owner case is a genuine ``--submit`` that never spawns a worker.
"""

from __future__ import annotations

import contextlib

from support import skip_posix_on_native_windows

skip_posix_on_native_windows("worktree occupancy tests launch POSIX workers")

import json
import os
import pwd
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
DISPATCH = ROOT / "scripts" / "goalflight_dispatch.py"
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_dispatch as dispatch  # noqa: E402
import goalflight_ledger as ledger  # noqa: E402

_ASYNC_WAIT_TIMEOUT_S = 30.0


def _env(tmp: Path) -> dict[str, str]:
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
    env["GOALFLIGHT_CAPACITY_CONF"] = "/dev/null"
    env["GOALFLIGHT_CAPACITY_WAIT_S"] = "0"
    return env


def _run(cmd: list[str], env: dict[str, str], *, timeout: float = 60.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
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


def _temp_dir(prefix: str | None = None, *, dir: Path | None = None) -> tempfile.TemporaryDirectory:
    """Temp dir that does not fail the test if a dying watcher still holds a file."""
    kwargs: dict = {"ignore_cleanup_errors": True}
    if prefix:
        kwargs["prefix"] = prefix
    if dir is not None:
        kwargs["dir"] = str(dir)
    return tempfile.TemporaryDirectory(**kwargs)


def _non_temp_tree(prefix: str) -> tempfile.TemporaryDirectory:
    """A worker tree outside the macOS temp root.

    An explicit --read-only dispatch runs the OS-sandbox boundary check, which
    refuses cwds inside the allowed temp root (it cannot separate the workspace
    from everything else a temp root lets the worker write). The repo's
    gitignored docs-private/ is the established scratch location for this.
    """
    scratch = ROOT / "docs-private"
    scratch.mkdir(exist_ok=True)
    return _temp_dir(prefix=prefix, dir=scratch)


def _ledger_record(tmp: Path, dispatch_id: str) -> dict:
    path = tmp / "state" / "runs.d" / f"{dispatch_id}.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


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


def test_second_writer_refused_naming_incumbent_then_override_launches() -> None:
    with _temp_dir() as td:
        tmp = Path(td)
        tree = tmp / "tree"
        tree.mkdir()
        env = _env(tmp)
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
            assert not _ledger_record(tmp, "occ-second"), refused.stderr

            forced = _run(
                _dispatch_cmd(
                    tmp,
                    tree,
                    "occ-second",
                    _quick_writer("occ-second"),
                    extra=["--occupied-worktree-forced"],
                ),
                env,
            )
            assert forced.returncode == 0, (forced.stdout, forced.stderr)
            assert "--occupied-worktree-forced accepted" in forced.stderr, forced.stderr
            assert "occ-incumbent" in forced.stderr, forced.stderr
            _wait_until_terminal(tmp, "occ-second")
        finally:
            release_incumbent.write_text("release", encoding="utf-8")
        _wait_until_terminal(tmp, "occ-incumbent")


def test_declared_read_only_raw_worker_is_refused_into_occupied_worktree() -> None:
    """--read-only on a `--` worker is a declaration; occupancy still applies."""
    with _temp_dir() as td, _non_temp_tree("gf-occ-ro-tree-") as tree_td:
        tmp = Path(td)
        tree = Path(tree_td).resolve()
        env = _env(tmp)
        release_incumbent = tmp / "release-incumbent"
        incumbent = _run(
            _dispatch_cmd(tmp, tree, "ro-incumbent", _blocking_worker(release_incumbent, "ro-incumbent")),
            env,
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
            )
            assert declared.returncode == 64, (declared.returncode, declared.stdout, declared.stderr)
            assert "ro-incumbent" in declared.stderr, declared.stderr
            assert not _ledger_record(tmp, "ro-declared"), declared.stderr
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


def test_queued_incumbent_owns_tree_before_any_worker_spawns() -> None:
    """A queued dispatch owns its tree pre-spawn; only the ledger can see it."""
    with _temp_dir() as td:
        tmp = Path(td)
        tree = tmp / "tree"
        tree.mkdir()
        env = _env(tmp)
        submit = _run(
            [
                sys.executable,
                str(DISPATCH),
                "--unregistered-forced",
                "--cwd",
                str(tree),
                "--agent",
                "test",
                "--dispatch-id",
                "occ-queued",
                "--tail",
                str(tmp / "occ-queued.tail"),
                "--status-json",
                str(tmp / "occ-queued.status.json"),
                "--submit",
                "--no-drain-on-submit",
                "--",
                sys.executable,
                "-c",
                _quick_writer("occ-queued"),
            ],
            env,
        )
        assert submit.returncode == 0, (submit.stdout, submit.stderr)
        queued = _ledger_record(tmp, "occ-queued")
        assert queued.get("state") == "queued", queued
        assert not queued.get("worker_pid"), queued

        refused = _run(_dispatch_cmd(tmp, tree, "occ-writer", _quick_writer("occ-writer")), env)
        assert refused.returncode == 64, (refused.returncode, refused.stdout, refused.stderr)
        assert "occ-queued" in refused.stderr, refused.stderr
        assert not _ledger_record(tmp, "occ-writer"), refused.stderr


def test_unreadable_ledger_record_is_unknown_not_unoccupied() -> None:
    with _temp_dir() as td:
        tmp = Path(td)
        tree = tmp / "tree"
        tree.mkdir()
        env = _env(tmp)
        runs = tmp / "state" / "runs.d"
        runs.mkdir(parents=True)
        (runs / "corrupt-record.json").write_text("{not json", encoding="utf-8")

        refused = _run(_dispatch_cmd(tmp, tree, "unk-writer", _quick_writer("unk-writer")), env)
        assert refused.returncode == 64, (refused.returncode, refused.stdout, refused.stderr)
        assert "occupancy" in refused.stderr and "unknown" in refused.stderr, refused.stderr
        assert "Retry the dispatch" in refused.stderr, refused.stderr
        assert "--occupied-worktree-forced" in refused.stderr, refused.stderr
        assert not _ledger_record(tmp, "unk-writer"), refused.stderr

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
        assert forced.returncode == 0, (forced.stdout, forced.stderr)
        assert "--occupied-worktree-forced accepted" in forced.stderr, forced.stderr


def test_submit_into_occupied_tree_is_refused() -> None:
    """--submit is still a writer claiming the tree; refuse before queueing."""
    with _temp_dir() as td:
        tmp = Path(td)
        tree = tmp / "tree"
        tree.mkdir()
        env = _env(tmp)
        release_incumbent = tmp / "release-incumbent"
        incumbent = _run(
            _dispatch_cmd(tmp, tree, "sub-incumbent", _blocking_worker(release_incumbent, "sub-incumbent")),
            env,
        )
        assert incumbent.returncode == 0, (incumbent.stdout, incumbent.stderr)
        try:
            _wait_until_running(tmp, "sub-incumbent")
            refused = _run(
                _dispatch_cmd(
                    tmp,
                    tree,
                    "sub-second",
                    _quick_writer("sub-second"),
                    extra=["--submit", "--no-drain-on-submit"],
                ),
                env,
            )
            assert refused.returncode == 64, (refused.returncode, refused.stdout, refused.stderr)
            assert "sub-incumbent" in refused.stderr, refused.stderr
            assert not _ledger_record(tmp, "sub-second"), refused.stderr
        finally:
            release_incumbent.write_text("release", encoding="utf-8")
        _wait_until_terminal(tmp, "sub-incumbent")


def test_preset_bash_writer_refused_into_occupied_worktree() -> None:
    """The grok-code / codex preset path shares the bash occupancy gate."""
    with _temp_dir() as td:
        tmp = Path(td)
        tree = tmp / "tree"
        tree.mkdir()
        env = _env(tmp)
        release_incumbent = tmp / "release-incumbent"
        incumbent = _run(
            _dispatch_cmd(tmp, tree, "preset-incumbent", _blocking_worker(release_incumbent, "preset-incumbent")),
            env,
        )
        assert incumbent.returncode == 0, (incumbent.stdout, incumbent.stderr)
        try:
            _wait_until_running(tmp, "preset-incumbent")
            refused = _run(
                _prompt_writer_cmd(tmp, tree, "preset-second", agent="grok-code"),
                env,
            )
            assert refused.returncode == 64, (refused.returncode, refused.stdout, refused.stderr)
            assert "preset-incumbent" in refused.stderr, refused.stderr
            assert "DISPATCH-LAUNCHED" not in refused.stdout, refused.stdout
            assert not _ledger_record(tmp, "preset-second"), refused.stderr
        finally:
            release_incumbent.write_text("release", encoding="utf-8")
        _wait_until_terminal(tmp, "preset-incumbent")


def test_acp_writer_refused_into_occupied_worktree() -> None:
    """ACP --cwd is a separate main() branch; occupancy must refuse there too."""
    source = DISPATCH.read_text(encoding="utf-8")
    assert (
        source.count("occupancy_warning = _prepare_attempt_worktree_occupancy(args)")
        == 2
    ), "occupancy must be wired on both the ACP and bash launch branches"
    acp_py = _managed_acp_python()
    if acp_py is None:
        print("SKIP live ACP occupancy (no managed ACP interpreter); wiring asserted")
        return
    with _temp_dir() as td:
        tmp = Path(td)
        tree = tmp / "tree"
        tree.mkdir()
        env = _env(tmp)
        env["GOALFLIGHT_ACP_PYTHON"] = str(acp_py)
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
                    extra=["--shape", "acp"],
                ),
                env,
            )
            assert refused.returncode == 64, (refused.returncode, refused.stdout, refused.stderr)
            assert "acp-incumbent" in refused.stderr, refused.stderr
            assert "DISPATCH-LAUNCHED" not in refused.stdout, refused.stdout
            assert not _ledger_record(tmp, "acp-second"), refused.stderr
        finally:
            release_incumbent.write_text("release", encoding="utf-8")
        _wait_until_terminal(tmp, "acp-incumbent")


def test_unreadable_ledger_dir_is_unknown_not_unoccupied() -> None:
    """Path.glob on chmod 000 returns []; occupancy must not treat that as free."""
    with _temp_dir() as td:
        tmp = Path(td)
        tree = tmp / "tree"
        tree.mkdir()
        env = _env(tmp)
        runs = tmp / "state" / "runs.d"
        runs.mkdir(parents=True)
        os.chmod(runs, 0o000)
        try:
            refused = _run(_dispatch_cmd(tmp, tree, "unk-dir-writer", _quick_writer("unk-dir-writer")), env)
        finally:
            os.chmod(runs, 0o755)
        assert refused.returncode == 64, (refused.returncode, refused.stdout, refused.stderr)
        assert "occupancy" in refused.stderr and "unknown" in refused.stderr, refused.stderr
        assert "Retry the dispatch" in refused.stderr, refused.stderr
        assert not _ledger_record(tmp, "unk-dir-writer"), refused.stderr


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
        assert not _ledger_record(tmp, "ws-second"), refused.stderr


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
            env = _env(tmp)
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


def test_sigkill_of_worker_releases_occupancy_lock() -> None:
    with _temp_dir() as td:
        tmp = Path(td)
        tree = tmp / "tree"
        tree.mkdir()
        env = _env(tmp)
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
        assert "DISPATCH-END" in second.stdout, second.stdout
        watcher_pid = record.get("watcher_pid")
        if watcher_pid:
            _wait_for(lambda pid=watcher_pid: _pid_gone(pid), timeout=5.0)


def test_concurrent_dispatches_into_different_trees_both_run() -> None:
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
            "print('COMPLETE: tree-a — wrote', flush=True)\n"
        )
        writer_b = (
            "from pathlib import Path\n"
            "import time\n"
            f"Path({str(marker_b)!r}).write_text('b', encoding='utf-8')\n"
            "time.sleep(0.3)\n"
            "print('COMPLETE: tree-b — wrote', flush=True)\n"
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
        assert marker_a.exists() and marker_b.exists(), (out_a[-400:], out_b[-400:])
        # Occupancy must not serialize distinct trees: both writers launched.
        assert "DISPATCH-START" in out_a and "DISPATCH-START" in out_b, (out_a, out_b)


def test_concurrent_submit_does_not_dual_queue() -> None:
    with _temp_dir() as td:
        tmp = Path(td)
        tree = tmp / "tree"
        tree.mkdir()
        env = _env(tmp)
        cmd_a = _dispatch_cmd(
            tmp,
            tree,
            "sub-a",
            _quick_writer("sub-a"),
            extra=["--submit", "--no-drain-on-submit"],
        )
        cmd_b = _dispatch_cmd(
            tmp,
            tree,
            "sub-b",
            _quick_writer("sub-b"),
            extra=["--submit", "--no-drain-on-submit"],
        )
        pa = _popen(cmd_a, env)
        pb = _popen(cmd_b, env)
        try:
            out_a, err_a = _communicate(pa)
            out_b, err_b = _communicate(pb)
        finally:
            _reap(pa)
            _reap(pb)
        queued = [
            did
            for did in ("sub-a", "sub-b")
            if _ledger_record(tmp, did).get("state") == "queued"
        ]
        assert len(queued) == 1, (
            queued,
            pa.returncode,
            pb.returncode,
            err_a[-400:],
            err_b[-400:],
        )
        assert 64 in {pa.returncode, pb.returncode}, (pa.returncode, pb.returncode, err_a, err_b)


def test_declared_read_only_raw_incumbent_occupies_the_tree() -> None:
    """A write-capable `--` worker that declared --read-only still occupies."""
    with _temp_dir() as td, _non_temp_tree("gf-occ-rohold-tree-") as tree_td:
        tmp = Path(td)
        tree = Path(tree_td).resolve()
        env = _env(tmp)
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


if __name__ == "__main__":
    test_second_writer_refused_naming_incumbent_then_override_launches()
    test_declared_read_only_raw_worker_is_refused_into_occupied_worktree()
    test_enforced_read_only_reviewer_skips_occupancy()
    test_terminal_incumbent_vacates_the_tree()
    test_queued_incumbent_owns_tree_before_any_worker_spawns()
    test_unreadable_ledger_record_is_unknown_not_unoccupied()
    test_unreadable_ledger_dir_is_unknown_not_unoccupied()
    test_submit_into_occupied_tree_is_refused()
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
    test_sigkill_of_worker_releases_occupancy_lock()
    test_concurrent_dispatches_into_different_trees_both_run()
    test_concurrent_submit_does_not_dual_queue()
    print("PASS: test_dispatch_worktree_occupancy")
