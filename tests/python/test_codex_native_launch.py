#!/usr/bin/env python3
"""Focused regressions for direct native Codex launches."""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import importlib
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import time
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_dispatch as dispatch  # noqa: E402
import goalflight_acp_client  # noqa: E402
import goalflight_native_launch as native_launch  # noqa: E402


pytestmark = pytest.mark.skipif(
    os.name == "nt",
    reason="native Codex launch tests use POSIX flock and process signals",
)


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o700)


def _codex_layout(tmp_path: Path, *, arch: str = "arm64") -> tuple[Path, Path]:
    target = {
        "arm64": ("codex-darwin-arm64", "aarch64-apple-darwin"),
        "x64": ("codex-darwin-x64", "x86_64-apple-darwin"),
    }[arch]
    package_root = tmp_path / "node_modules" / "@openai" / "codex"
    wrapper = package_root / "bin" / "codex.js"
    platform_root = (
        package_root
        / "node_modules"
        / "@openai"
        / target[0]
    )
    native = platform_root / "vendor" / target[1] / "bin" / "codex"
    wrapper.parent.mkdir(parents=True)
    native.parent.mkdir(parents=True)
    (platform_root / "package.json").write_text("{}\n", encoding="utf-8")
    native.write_bytes(b"\xcf\xfa\xed\xfe" + b"native\n")
    return wrapper, native


def _lock_held(lock_path: Path) -> bool:
    probe = os.open(lock_path, os.O_RDWR)
    try:
        try:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(probe, fcntl.LOCK_UN)
        return False
    finally:
        os.close(probe)


def _wait_for(path: Path, *, timeout_s: float = 5.0) -> str:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {path}")


def _wait_for_lsof_fd(pid: int, fd: int, lock_path: Path, *, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    expected = str(lock_path.resolve())
    while time.monotonic() < deadline:
        result = subprocess.run(
            ["lsof", "-p", str(pid), "-a", "-d", str(fd), "-Fn"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0 and f"n{expected}" in result.stdout.splitlines():
            return
        time.sleep(0.02)
    raise AssertionError(f"lsof did not show pid={pid} fd={fd} path={expected}")


def _wait_for_pid_exit(pid: int, *, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.02)
    raise AssertionError(f"pid={pid} did not exit within {timeout_s}s")


def test_acp_client_imports_without_fcntl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "fcntl", None)
    monkeypatch.delitem(sys.modules, "goalflight_worktree_pool", raising=False)
    monkeypatch.delitem(sys.modules, "goalflight_acp_client", raising=False)

    imported = importlib.import_module("goalflight_acp_client")

    assert imported.__name__ == "goalflight_acp_client"


@pytest.mark.parametrize("arch", ["arm64", "x64"])
def test_codex_layout_resolves_native_and_preserves_wrapper_contract(
    tmp_path: Path, arch: str
) -> None:
    wrapper, native = _codex_layout(tmp_path, arch=arch)
    launch = native_launch.prepare_codex_launch(
        str(wrapper),
        ["exec", "sandbox", "--"],
        env={"PATH": "/usr/bin", "CODEX_HOME": str(tmp_path / "codex-home")},
        platform_name="darwin",
        arch=arch,
    )

    assert launch.argv == (str(native), "exec", "sandbox", "--")
    assert launch.lock_holder == "native"
    assert launch.warning is None
    assert launch.env["CODEX_MANAGED_PACKAGE_ROOT"] == str(wrapper.parent.parent)
    assert launch.env["CODEX_MANAGED_BY_NPM"] == "1"
    assert launch.env["CODEX_HOME"] == str(tmp_path / "codex-home")


def test_native_shortcut_requires_expected_binary_content_and_layout(
    tmp_path: Path,
) -> None:
    _wrapper, native = _codex_layout(tmp_path)
    launch = native_launch.prepare_codex_launch(
        str(native),
        ["sandbox", "--", "/bin/sleep", "1"],
        env={"PATH": "/usr/bin"},
        platform_name="darwin",
        arch="arm64",
    )

    assert launch.argv[0] == str(native)
    assert launch.lock_holder == "native"
    assert launch.warning is None


def test_native_shortcut_rejects_nonbinary_content(tmp_path: Path) -> None:
    _wrapper, native = _codex_layout(tmp_path)
    native.write_bytes(b"#!/bin/sh\nexec codex \"$@\"\n")
    native_path = native_launch._native_command_path(
        str(native),
        acp=False,
        env={"PATH": "/usr/bin"},
        cwd=None,
        platform_name="darwin",
        arch="arm64",
    )
    assert native_path == native.resolve()
    assert not native_launch._platform_binary_magic(
        native_launch._read_file_prefix(native), "darwin"
    )

    launch = native_launch.prepare_codex_launch(
        str(native),
        ["exec"],
        env={"PATH": "/usr/bin"},
        platform_name="darwin",
        arch="arm64",
    )

    assert launch.argv[0] == str(native)
    assert launch.lock_holder == "wrapper"
    assert "falling back to wrapper" in (launch.warning or "")


def test_dot_codex_resolves_against_worker_cwd_not_controller_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller = tmp_path / "controller"
    worker = tmp_path / "worker"
    controller.mkdir()
    local_wrapper, local_native = _codex_layout(worker / "local")
    global_wrapper, _global_native = _codex_layout(controller / "global")
    (worker / "codex").symlink_to(local_wrapper)
    (controller / "codex").symlink_to(global_wrapper)
    monkeypatch.chdir(controller)

    launch = native_launch.prepare_codex_launch(
        "./codex",
        ["exec"],
        env={"PATH": str(controller)},
        platform_name="darwin",
        arch="arm64",
        cwd=str(worker),
    )

    assert launch.argv[0] == str(local_native)
    assert launch.lock_holder == "native"


def test_vendor_wrapper_is_not_certified_as_native(tmp_path: Path) -> None:
    wrapper = tmp_path / "app" / "vendor" / "node_modules" / ".bin" / "codex"
    wrapper.parent.mkdir(parents=True)
    _write_executable(wrapper, "#!/bin/sh\nexec codex \"$@\"\n")

    launch = native_launch.prepare_codex_launch(
        str(wrapper),
        ["sandbox", "--", "/bin/sleep", "1"],
        env={"PATH": "/usr/bin"},
        platform_name="darwin",
        arch="arm64",
    )

    assert launch.argv[0] == str(wrapper)
    assert launch.lock_holder == "wrapper"
    assert "falling back to wrapper" in (launch.warning or "")


def test_acp_named_wrapper_is_not_certified_as_native(tmp_path: Path) -> None:
    wrapper = tmp_path / "app" / "codex-acp-fake" / "bin" / "codex-acp"
    wrapper.parent.mkdir(parents=True)
    _write_executable(wrapper, "#!/bin/sh\nexec codex-acp \"$@\"\n")

    launch = native_launch.prepare_codex_acp_launch(
        str(wrapper),
        ["--stdio"],
        env={"PATH": "/usr/bin"},
        platform_name="darwin",
        arch="arm64",
    )

    assert launch.argv[0] == str(wrapper)
    assert launch.lock_holder == "wrapper"
    assert "falling back to wrapper" in (launch.warning or "")


def test_node_architecture_probe_reads_only_a_bounded_binary_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = tmp_path / "codex"
    binary.write_bytes(b"\x7fELF" + b"x" * 4096)

    def fail_if_text_read(*_args: object, **_kwargs: object) -> str:
        pytest.fail("native binary must not be decoded as an unbounded text file")

    monkeypatch.setattr(Path, "read_text", fail_if_text_read)
    arch, reason = native_launch._node_process_arch(
        binary, env={"PATH": "/usr/bin"}, cwd=str(tmp_path)
    )

    assert arch is None
    assert reason == "wrapper is not a Node entrypoint"


def test_relative_wrapper_resolution_uses_worker_cwd_and_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    controller = tmp_path / "controller"
    worker = tmp_path / "worker"
    controller.mkdir()
    wrapper, native = _codex_layout(worker)
    monkeypatch.chdir(controller)

    launch = native_launch.prepare_codex_launch(
        "./node_modules/@openai/codex/bin/codex.js",
        ["exec"],
        env={"PATH": "/usr/bin"},
        platform_name="darwin",
        arch="arm64",
        cwd=str(worker),
    )

    assert launch.argv[0] == str(native)
    assert launch.lock_holder == "native"


def test_relative_wrapper_without_worker_cwd_falls_back(tmp_path: Path) -> None:
    worker = tmp_path / "worker"
    wrapper, _native = _codex_layout(worker)
    launch = native_launch.prepare_codex_launch(
        "./node_modules/@openai/codex/bin/codex.js",
        env={"PATH": "/usr/bin"},
        platform_name="darwin",
        arch="arm64",
    )

    assert launch.argv[0] == "./node_modules/@openai/codex/bin/codex.js"
    assert launch.lock_holder == "wrapper"
    assert "requires worker cwd" in (launch.warning or "")


def test_symlinked_wrapper_uses_canonical_node_module_ancestry(tmp_path: Path) -> None:
    real_wrapper, real_native = _codex_layout(tmp_path / "real")
    lexical_wrapper = tmp_path / "linked" / "bin" / "codex.js"
    lexical_wrapper.parent.mkdir(parents=True)
    lexical_wrapper.symlink_to(real_wrapper)
    unrelated = lexical_wrapper.parent / "node_modules" / "@openai" / "codex-darwin-arm64"
    unrelated_native = unrelated / "vendor" / "aarch64-apple-darwin" / "bin" / "codex"
    unrelated_native.parent.mkdir(parents=True)
    (unrelated / "package.json").write_text("{}\n", encoding="utf-8")
    unrelated_native.write_text("unrelated\n", encoding="utf-8")

    launch = native_launch.prepare_codex_launch(
        str(lexical_wrapper),
        env={"PATH": "/usr/bin"},
        platform_name="darwin",
        arch="arm64",
    )

    assert launch.argv[0] == str(real_native)
    assert launch.argv[0] != str(unrelated_native)


def test_architecture_probe_uses_wrapper_node_binary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wrapper, native = _codex_layout(tmp_path)
    node = tmp_path / "node"
    wrapper.write_text(f"#!{node}\n", encoding="utf-8")
    seen: list[list[str]] = []

    def fake_run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, "arm64\n", "")

    monkeypatch.setattr(native_launch.subprocess, "run", fake_run)
    launch = native_launch.prepare_codex_launch(
        str(wrapper),
        env={"PATH": "/usr/bin"},
        platform_name="darwin",
    )

    assert launch.argv[0] == str(native)
    assert seen == [[str(node), "-p", "process.arch"]]


def test_bun_marker_uses_canonical_wrapper_directory(tmp_path: Path) -> None:
    package_root = tmp_path / ".bun" / "install" / "global" / "node_modules" / "@openai" / "codex"
    wrapper = package_root / "bin" / "codex.js"
    native = package_root / "node_modules" / "@openai" / "codex-darwin-arm64" / "vendor" / "aarch64-apple-darwin" / "bin" / "codex"
    wrapper.parent.mkdir(parents=True)
    native.parent.mkdir(parents=True)
    (package_root / "node_modules" / "@openai" / "codex-darwin-arm64" / "package.json").write_text("{}\n", encoding="utf-8")
    native.write_text("native\n", encoding="utf-8")
    lexical_wrapper = tmp_path / "bin" / "codex"
    lexical_wrapper.parent.mkdir()
    lexical_wrapper.symlink_to(wrapper)

    launch = native_launch.prepare_codex_launch(
        str(lexical_wrapper),
        env={"PATH": "/usr/bin"},
        platform_name="darwin",
        arch="arm64",
    )

    assert launch.env["CODEX_MANAGED_BY_BUN"] == "1"
    assert "CODEX_MANAGED_BY_NPM" not in launch.env


def test_command_selector_uses_codex_acp_resolver() -> None:
    launch = native_launch.prepare_codex_launch_for_command(
        "codex-acp",
        ["--stdio"],
        env={"PATH": "/usr/bin"},
    )
    assert launch is not None
    assert launch.lock_holder == "wrapper"
    assert "codex-acp" in (launch.warning or "")


def test_signalled_acp_exit_keeps_goalflight_failure_classification() -> None:
    from goalflight_acp_run import acp_dispatch_exit_code

    assert acp_dispatch_exit_code(
        {
            "state": "failed",
            "error": {"code": -9},
            "terminated_by_signal": "SIGKILL",
        }
    ) == 1
    assert acp_dispatch_exit_code({"state": "complete", "returncode": -9}) == 0


def test_acp_pool_passes_lock_fds_and_retains_native_metadata(tmp_path: Path) -> None:
    lock_path = tmp_path / "pool.lock"
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(lock_fd, fcntl.LOCK_EX)

    class FakeConnection:
        alive = True
        reusable = True
        context_mode = True
        os_sandbox = "off"
        cwd = str(tmp_path)
        proc = type("Proc", (), {"pid": os.getpid()})()
        _started_identity = None
        lock_holder = "native"
        lock_holder_warning = None

        async def initialize(self) -> None:
            return None

        async def new_session(self, _cwd: str) -> None:
            return None

        async def kill(self, **_kwargs: object):
            return goalflight_acp_client.AcpTerminationResult(
                pid=self.proc.pid,
                pgid=self.proc.pid,
                confirmed=True,
                scope_alive=False,
                reason="test_cleanup",
            )

    captured: dict[str, object] = {}

    async def fake_spawn(*_args: object, **kwargs: object) -> FakeConnection:
        captured.update(kwargs)
        return FakeConnection()

    async def run_case() -> None:
        pool = goalflight_acp_client.AcpProcessPool(
            {"codex": {"command": "codex-acp", "acp_args": []}},
            max_processes=1,
            max_per_agent=1,
        )
        with patch.object(goalflight_acp_client, "spawn_acp_connection", fake_spawn):
            conn = await pool.get_or_create(
                "codex",
                "pool-lock",
                cwd=str(tmp_path),
                pass_fds=(lock_fd, lock_fd),
            )
        assert captured["pass_fds"] == (lock_fd,)
        assert conn.pass_fds == (lock_fd,)
        assert pool.stats["lock_holders"] == {
            "codex/pool-lock": {"lock_holder": "native"}
        }
        await pool.shutdown()

    try:
        asyncio.run(run_case())
    finally:
        os.close(lock_fd)


def test_codex_layout_missing_warns_and_falls_back(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    wrapper = tmp_path / "codex.js"
    wrapper.write_text("wrapper\n", encoding="utf-8")
    launch = native_launch.prepare_codex_launch(
        str(wrapper),
        ["exec", "--json"],
        env={"PATH": "/usr/bin"},
        platform_name="darwin",
        arch="arm64",
    )

    assert launch.argv == (str(wrapper), "exec", "--json")
    assert launch.lock_holder == "wrapper"
    assert launch.warning is not None
    assert "WARNING" in capsys.readouterr().err
    assert "lock_holder=wrapper" in launch.warning


def test_codex_acp_layout_resolves_native_without_env_mutation(tmp_path: Path) -> None:
    package_root = tmp_path / "node_modules" / "@zed-industries" / "codex-acp"
    wrapper = package_root / "bin" / "codex-acp.js"
    native = (
        package_root
        / "node_modules"
        / "@zed-industries"
        / "codex-acp-darwin-arm64"
        / "bin"
        / "codex-acp"
    )
    wrapper.parent.mkdir(parents=True)
    native.parent.mkdir(parents=True)
    native.write_text("native\n", encoding="utf-8")

    env = {"PATH": "/usr/bin", "CODEX_HOME": str(tmp_path / "home")}
    launch = native_launch.prepare_codex_acp_launch(
        str(wrapper), ["--stdio"], env=env, platform_name="darwin", arch="arm64"
    )

    assert launch.argv == (str(native), "--stdio")
    assert launch.lock_holder == "native"
    assert launch.warning is None
    assert launch.env == env


def test_dispatch_status_and_watcher_record_wrapper_fallback() -> None:
    args = type(
        "Args",
        (),
        {
            "dispatch_id": "fallback-status",
            "agent": "codex",
            "shape": "bash",
            "_lock_holder": "wrapper",
            "_lock_holder_warning": "native resolution failed",
        },
    )()
    metadata = dispatch._prelaunch_status_metadata(args)
    assert metadata["lock_holder"] == "wrapper"
    assert metadata["lock_holder_warning"] == "native resolution failed"

    watcher_argv = dispatch._watcher_spawn_argv(
        worker_pid=1,
        tail=Path("/tmp/tail"),
        status_json=Path("/tmp/status"),
        agent="codex",
        poll_secs=2.0,
        max_idle_secs=60.0,
        dispatch_id="fallback-status",
        pgid=1,
        lock_holder="wrapper",
    )
    holder_flag = watcher_argv.index("--lock-holder")
    assert watcher_argv[holder_flag + 1] == "wrapper"


def test_native_engine_and_child_keep_flock_after_parent_kill(tmp_path: Path) -> None:
    if shutil.which("lsof") is None:
        pytest.skip("ownership proof requires lsof")
    wrapper, native = _codex_layout(tmp_path)
    native_pid = tmp_path / "native.pid"
    child_pid = tmp_path / "child.pid"
    wrapper_pid = tmp_path / "wrapper.pid"
    _write_executable(
        wrapper,
        """#!/usr/bin/env python3
import os, subprocess, sys, time
from pathlib import Path
Path(os.environ['FAKE_WRAPPER_PID']).write_text(str(os.getpid()))
child = subprocess.Popen([os.environ['FAKE_NATIVE'], *sys.argv[1:]])
child.wait()
""",
    )
    _write_executable(
        native,
        """#!/usr/bin/env python3
import os, subprocess, sys, time
from pathlib import Path
fd = int(os.environ['GOALFLIGHT_WORKTREE_LOCK_FD'])
Path(os.environ['FAKE_NATIVE_PID']).write_text(str(os.getpid()))
child = subprocess.Popen(['/bin/sleep', sys.argv[-1]], pass_fds=(fd,))
Path(os.environ['FAKE_NATIVE_CHILD_PID']).write_text(str(child.pid))
child.wait()
""",
    )

    lock_path = tmp_path / "seat.lock"
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    os.set_inheritable(lock_fd, True)
    lock_number = lock_fd
    try:
        env = {
            "PATH": os.environ.get("PATH", ""),
            "GOALFLIGHT_WORKTREE_LOCK_FD": str(lock_fd),
            "FAKE_NATIVE": str(native),
            "FAKE_NATIVE_PID": str(native_pid),
            "FAKE_NATIVE_CHILD_PID": str(child_pid),
            "FAKE_WRAPPER_PID": str(wrapper_pid),
        }
        launch = native_launch.prepare_codex_launch(
            str(wrapper),
            ["sandbox", "--", "/bin/sleep", "30"],
            env=env,
            platform_name="darwin",
            arch="arm64",
        )
        worker_pid = dispatch._spawn_daemonized_process(
            list(launch.argv),
            env=launch.env,
            label="native-codex-test",
        )
        os.close(lock_fd)
        lock_fd = -1

        assert int(_wait_for(native_pid)) == worker_pid
        child = int(_wait_for(child_pid))
        assert not wrapper_pid.exists()
        _wait_for_lsof_fd(worker_pid, lock_number, lock_path)
        _wait_for_lsof_fd(child, lock_number, lock_path)
        assert _lock_held(lock_path)

        os.kill(worker_pid, signal.SIGKILL)
        _wait_for_pid_exit(worker_pid)
        _wait_for_lsof_fd(child, lock_number, lock_path)
        assert _lock_held(lock_path)

        os.kill(child, signal.SIGKILL)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and _lock_held(lock_path):
            time.sleep(0.02)
        assert not _lock_held(lock_path)
    finally:
        if lock_fd >= 0:
            os.close(lock_fd)
        for pid_path in (native_pid, child_pid, wrapper_pid):
            if pid_path.exists():
                try:
                    pid = int(pid_path.read_text(encoding="utf-8"))
                    os.kill(pid, signal.SIGKILL)
                except (OSError, ValueError):
                    pass


def test_review_job_native_engine_keeps_flock_after_launcher_kill(tmp_path: Path) -> None:
    if shutil.which("lsof") is None or shutil.which("node") is None:
        pytest.skip("ownership proof requires lsof and Node")
    if sys.platform != "darwin":
        pytest.skip("fixture models the Darwin Codex package layout")
    node_arch = subprocess.check_output(
        ["node", "-p", "process.arch"], text=True
    ).strip()
    if node_arch not in {"arm64", "x64"}:
        pytest.skip(f"unsupported local Node architecture: {node_arch}")

    wrapper, native = _codex_layout(tmp_path, arch=node_arch)
    native_pid = tmp_path / "review-native.pid"
    _write_executable(
        wrapper,
        "#!/usr/bin/env node\n",
    )
    _write_executable(
        native,
        """#!/usr/bin/env python3
import json, os, time
from pathlib import Path
Path(os.environ['FAKE_NATIVE_PID']).write_text(str(os.getpid()))
print(json.dumps({'type': 'tick'}), flush=True)
while True:
    time.sleep(1)
""",
    )

    prompt = tmp_path / "review.prompt.md"
    prompt.write_text("stand-in review; no model\n", encoding="utf-8")
    output_dir = tmp_path / "review-output"
    output_dir.mkdir()
    lock_path = tmp_path / "seat.lock"
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    os.set_inheritable(lock_fd, True)
    launcher: subprocess.Popen[str] | None = None
    native_pid_number: int | None = None
    try:
        env = os.environ.copy()
        for name in (
            "GOALFLIGHT_STATE_DIR",
            "GOALFLIGHT_TASK_STORE_DIR",
            "GOALFLIGHT_JOURNAL_DIR",
            "GOALFLIGHT_MESSAGES_DIR",
            "GOAL_FLIGHT_PIDFILE_DIR",
        ):
            env[name] = str(tmp_path / name.lower())
        env["GOALFLIGHT_CAPACITY_CONF"] = "/dev/null"
        env["GOALFLIGHT_WORKTREE_LOCK_FD"] = str(lock_fd)
        env.pop("GOALFLIGHT_OCCUPANCY_LOCK_FD", None)
        env["FAKE_NATIVE_PID"] = str(native_pid)
        launcher = subprocess.Popen(
            [
                sys.executable,
                str(ROOT / "scripts" / "goalflight_review_job.py"),
                "--agent",
                "codex",
                "--name",
                "native-review",
                "--repo",
                str(ROOT),
                "--prompt",
                str(prompt),
                "--output-dir",
                str(output_dir),
                "--codex-bin",
                str(wrapper),
                "--timeout-s",
                "60",
                "--max-quiet-s",
                "60",
                "--heartbeat-interval",
                "0.1",
                "--json",
            ],
            cwd=str(ROOT),
            env=env,
            pass_fds=(lock_fd,),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        native_pid_number = int(_wait_for(native_pid))
        _wait_for_lsof_fd(native_pid_number, lock_fd, lock_path)

        os.kill(launcher.pid, signal.SIGKILL)
        launcher.wait(timeout=5)
        _wait_for_lsof_fd(native_pid_number, lock_fd, lock_path)
    finally:
        if launcher is not None and launcher.poll() is None:
            launcher.kill()
            launcher.wait(timeout=5)
        if native_pid_number is None and native_pid.exists():
            native_pid_number = int(native_pid.read_text(encoding="utf-8"))
        if native_pid_number is not None:
            with contextlib.suppress(OSError):
                os.kill(native_pid_number, signal.SIGKILL)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if subprocess.run(
                    ["lsof", "-p", str(native_pid_number), "-a", "-d", str(lock_fd)],
                    capture_output=True,
                    check=False,
                ).returncode != 0:
                    break
                time.sleep(0.02)
        os.close(lock_fd)
    assert not _lock_held(lock_path)


def test_acp_runner_and_pool_native_engines_keep_flock_after_launcher_kill(
    tmp_path: Path,
) -> None:
    if shutil.which("lsof") is None or shutil.which("node") is None:
        pytest.skip("ownership proof requires lsof and Node")
    if sys.platform != "darwin":
        pytest.skip("fixture models the Darwin Codex ACP package layout")
    if goalflight_acp_client.ACP_IMPORT_ERROR is not None:
        pytest.skip("ACP SDK is not installed in this test interpreter")
    node_arch = subprocess.check_output(
        ["node", "-p", "process.arch"], text=True
    ).strip()
    if node_arch not in {"arm64", "x64"}:
        pytest.skip(f"unsupported local Node architecture: {node_arch}")

    def run_chain(mode: str) -> None:
        chain_root = tmp_path / mode
        package_root = chain_root / "node_modules" / "@zed-industries" / "codex-acp"
        wrapper = package_root / "bin" / "codex-acp.js"
        target_root = (
            package_root
            / "node_modules"
            / "@zed-industries"
            / f"codex-acp-darwin-{node_arch}"
        )
        native = target_root / "bin" / "codex-acp"
        wrapper.parent.mkdir(parents=True)
        native.parent.mkdir(parents=True)
        _write_executable(wrapper, "#!/usr/bin/env node\n")
        _write_executable(
            native,
            """#!/usr/bin/env python3
import json, sys, time
def send(message):
    print(json.dumps(message, separators=(',', ':')), flush=True)
while True:
    line = sys.stdin.readline()
    if not line:
        time.sleep(60)
        continue
    try:
        message = json.loads(line)
    except json.JSONDecodeError:
        continue
    method = message.get('method')
    request_id = message.get('id')
    if method == 'initialize':
        send({'jsonrpc': '2.0', 'id': request_id, 'result': {
            'protocolVersion': 1,
            'agentInfo': {'name': 'stand-in', 'version': '1'},
            'capabilities': {},
        }})
    elif method == 'session/new':
        send({'jsonrpc': '2.0', 'id': request_id, 'result': {'sessionId': 's'}})
""",
        )
        pid_path = chain_root / "native.pid"
        metadata_path = chain_root / "metadata.json"
        lock_path = chain_root / "seat.lock"
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        os.set_inheritable(lock_fd, True)
        launcher: subprocess.Popen[str] | None = None
        native_pid_number: int | None = None
        try:
            if mode == "runner":
                launcher_code = f"""
import asyncio, json, os, sys
from pathlib import Path
sys.path.insert(0, {str(ROOT / 'scripts')!r})
from goalflight_acp_run import spawn_and_handshake_with_retry

async def main():
    proc, conn = await spawn_and_handshake_with_retry(
        {str(wrapper)!r}, [], agent='codex-acp', session_id='s',
        cwd={str(chain_root)!r}, attempts=1, handshake_timeout=5,
        env=os.environ.copy(), pass_fds=({lock_fd},)
    )
    Path({str(pid_path)!r}).write_text(str(proc.pid))
    Path({str(metadata_path)!r}).write_text(json.dumps({{
        'lock_holder': conn.lock_holder,
        'pass_fds': list(conn.pass_fds),
    }}))
    await asyncio.sleep(60)

asyncio.run(main())
"""
            else:
                launcher_code = f"""
import asyncio, json, os
from pathlib import Path
import sys
sys.path.insert(0, {str(ROOT / 'scripts')!r})
from goalflight_acp_client import AcpProcessPool

async def main():
    pool = AcpProcessPool({{
        'codex': {{'command': {str(wrapper)!r}, 'acp_args': [],
                   'working_dir': {str(chain_root)!r}
        }}
    }}, max_processes=1, max_per_agent=1)
    conn = await pool.get_or_create(
        'codex', 's', cwd={str(chain_root)!r}, pass_fds=({lock_fd},)
    )
    Path({str(pid_path)!r}).write_text(str(conn.proc.pid))
    Path({str(metadata_path)!r}).write_text(json.dumps({{
        'lock_holder': conn.lock_holder,
        'pass_fds': list(conn.pass_fds),
        'stats': pool.stats,
    }}))
    await asyncio.sleep(60)

asyncio.run(main())
"""
            env = os.environ.copy()
            env.pop("GOALFLIGHT_WORKTREE_LOCK_FD", None)
            env.pop("GOALFLIGHT_OCCUPANCY_LOCK_FD", None)
            launcher = subprocess.Popen(
                [sys.executable, "-c", launcher_code],
                cwd=str(ROOT),
                env=env,
                pass_fds=(lock_fd,),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            native_pid_number = int(_wait_for(pid_path))
            metadata = json.loads(_wait_for(metadata_path))
            assert metadata["lock_holder"] == "native"
            assert metadata["pass_fds"] == [lock_fd]
            if mode == "pool":
                assert metadata["stats"]["lock_holders"]["codex/s"] == {
                    "lock_holder": "native"
                }
            _wait_for_lsof_fd(native_pid_number, lock_fd, lock_path)

            os.kill(launcher.pid, signal.SIGKILL)
            launcher.wait(timeout=5)
            _wait_for_lsof_fd(native_pid_number, lock_fd, lock_path)
        finally:
            if launcher is not None and launcher.poll() is None:
                launcher.kill()
                launcher.wait(timeout=5)
            if native_pid_number is None and pid_path.exists():
                native_pid_number = int(pid_path.read_text(encoding="utf-8"))
            if native_pid_number is not None:
                with contextlib.suppress(OSError):
                    os.kill(native_pid_number, signal.SIGKILL)
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if subprocess.run(
                        ["lsof", "-p", str(native_pid_number), "-a", "-d", str(lock_fd)],
                        capture_output=True,
                        check=False,
                    ).returncode != 0:
                        break
                    time.sleep(0.02)
            os.close(lock_fd)
        assert not _lock_held(lock_path)

    run_chain("runner")
    run_chain("pool")


def test_real_codex_sandbox_keeps_flock_after_goalflight_launcher_kill(
    tmp_path: Path,
) -> None:
    if shutil.which("lsof") is None:
        pytest.skip("ownership proof requires lsof")
    codex = shutil.which("codex")
    if codex is None:
        pytest.skip("real Codex is not installed")

    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    env = os.environ.copy()
    env["CODEX_HOME"] = str(codex_home)
    sandbox_probe = subprocess.run(
        [codex, "sandbox", "--", "/bin/true"],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    sandbox_output = f"{sandbox_probe.stdout}\n{sandbox_probe.stderr}"
    if sandbox_probe.returncode != 0 and (
        "sandbox_apply" in sandbox_output
        or "Operation not permitted" in sandbox_output
    ):
        pytest.skip("real Codex sandbox is unavailable in this test environment")

    lock_path = tmp_path / "seat.lock"
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    os.set_inheritable(lock_fd, True)
    lock_number = lock_fd
    worker_pid: int | None = None
    try:
        env["GOALFLIGHT_WORKTREE_LOCK_FD"] = str(lock_fd)
        env.pop("GOALFLIGHT_OCCUPANCY_LOCK_FD", None)
        launch = native_launch.prepare_codex_launch(
            codex,
            ["sandbox", "--", "/bin/sleep", "20"],
            env=env,
            cwd=str(tmp_path),
        )
        assert launch.lock_holder == "native"
        worker_pid = dispatch._spawn_daemonized_process(
            list(launch.argv),
            env=launch.env,
            stdout_path=tmp_path / "codex.stdout",
            stderr="stdout",
            label="real-codex-native-ownership",
            cwd=str(tmp_path),
        )
        os.close(lock_fd)
        lock_fd = -1

        _wait_for_lsof_fd(worker_pid, lock_number, lock_path)
        os.kill(worker_pid, signal.SIGKILL)
        _wait_for_pid_exit(worker_pid)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not _lock_held(lock_path):
            time.sleep(0.02)
        assert _lock_held(lock_path)
    finally:
        if worker_pid is not None:
            with contextlib.suppress(OSError):
                os.killpg(worker_pid, signal.SIGKILL)
            with contextlib.suppress(OSError):
                os.kill(worker_pid, signal.SIGKILL)
        if lock_fd >= 0:
            os.close(lock_fd)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and _lock_held(lock_path):
            time.sleep(0.02)
    assert not _lock_held(lock_path)
