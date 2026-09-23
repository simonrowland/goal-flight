"""Startup-only rotation and direct OpenCode server output."""

import importlib.util
import os
from pathlib import Path
import sys

import pytest


HOST = Path(__file__).resolve().parents[2] / "scripts/hosts/opencode"
spec = importlib.util.spec_from_file_location("opencode_prompt_logs", HOST / "prompt.py")
prompt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prompt)


@pytest.mark.parametrize("port", [4096, 4097])
@pytest.mark.parametrize("entry", ["prompt", "bash_tail"])
@pytest.mark.parametrize("override", [None, "/tmp/custom-server.log"])
def test_cli_log_path(monkeypatch, tmp_path, port, entry, override):
    if entry == "prompt":
        module = prompt
        argv = ["prompt.py", "hello"]
        target = "prompt_once"
    else:
        monkeypatch.syspath_prepend(str(HOST))
        spec = importlib.util.spec_from_file_location("opencode_tail_logs", HOST / "bash_tail.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        argv = ["bash_tail.py", "--prompt-text", "hello", "--tail", str(tmp_path / "tail"), "--dispatch-id", "test"]
        target = "run_bash_tail"
    captured = {}

    def run(**kwargs):
        captured.update(kwargs)
        return "reply" if entry == "prompt" else 0

    monkeypatch.setattr(module, "_load_litellm_env", lambda: None)
    if entry == "prompt":
        monkeypatch.setattr(module, target, lambda message, **kwargs: run(**kwargs))
    else:
        monkeypatch.setattr(module, target, run)
    argv += ["--port", str(port)]
    if override:
        argv += ["--log", override]
    monkeypatch.setattr(sys, "argv", argv)
    assert module.main() == 0
    assert captured["log_path"] == Path(override or f"/tmp/opencode-serve-{port}.log")


@pytest.mark.parametrize("keep", [1, 2, 3])
def test_startup_rotation_bounds_archives(tmp_path, keep):
    path = tmp_path / "logs" / "server.log"
    for generation in range(6):
        with prompt._open_server_log(path, max_bytes=16, keep=keep) as output:
            output.write(bytes([generation]) * 17)
        assert path.read_bytes() == bytes([generation]) * 17
        for index in range(1, min(generation, keep) + 1):
            assert Path(f"{path}.{index}").read_bytes() == bytes([generation - index]) * 17
        assert len(list(path.parent.iterdir())) == 1 + min(generation, keep)


def test_append_without_rotation_at_limit(tmp_path):
    path = tmp_path / "server.log"
    path.write_bytes(b"a" * 16)
    with prompt._open_server_log(path, max_bytes=16) as output:
        output.write(b"b" * 32)
    assert path.read_bytes() == b"a" * 16 + b"b" * 32
    assert not Path(f"{path}.1").exists()


@pytest.mark.parametrize("suffix", ["", ".1", ".2"])
@pytest.mark.parametrize("dangling", [False, True])
def test_symlink_refused_before_rotation(tmp_path, suffix, dangling):
    path = tmp_path / "server.log"
    outside = tmp_path / "outside"
    if not dangling:
        outside.write_bytes(b"untouched")
    if suffix:
        path.write_bytes(b"a" * 17)
    link = Path(f"{path}{suffix}")
    link.symlink_to(outside)
    with pytest.raises(OSError, match="symlinked log path"):
        prompt._open_server_log(path, max_bytes=16)
    assert link.is_symlink()
    if suffix:
        assert path.read_bytes() == b"a" * 17
    assert outside.read_bytes() == b"untouched" if not dangling else not outside.exists()


def test_boot_failure_keeps_all_direct_output(monkeypatch, tmp_path):
    executable = tmp_path / "opencode"
    executable.write_text(
        f"#!{sys.executable}\nimport os\n"
        "os.write(1, b'starting\\n')\n"
        "os.write(2, b'failure:' + b'x' * 262144 + b'\\n')\n"
        "raise SystemExit(1)\n"
    )
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    monkeypatch.setattr(prompt, "_health_ok", lambda base: False)
    processes = []
    real_popen = prompt.subprocess.Popen

    def launch(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    def failed_health(*args):
        assert processes[0].wait(timeout=10) == 1
        raise RuntimeError("boot failed")

    monkeypatch.setattr(prompt.subprocess, "Popen", launch)
    monkeypatch.setattr(prompt, "_wait_for_health", failed_health)
    path = tmp_path / "server.log"
    path.write_bytes(b"previous\n")
    with pytest.raises(RuntimeError, match="boot failed"):
        prompt.prompt_once(
            "hello", directory=tmp_path, model="provider/model", port=4096,
            boot_timeout_s=1, reply_timeout_s=1, keep_server=True, log_path=path,
        )
    assert len(processes) == 1
    assert path.read_bytes() == b"previous\nstarting\nfailure:" + b"x" * 262144 + b"\n"
