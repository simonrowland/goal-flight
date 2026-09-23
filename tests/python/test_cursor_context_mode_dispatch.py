"""Capture both real dispatch boundaries; never start a Cursor worker."""

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from test_codex_dispatch_seams import (
    _isolated_state,
    _run_acp_to_spawn_failure,
    _stub_bash_launch,
)
import goalflight_cursor as C


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path, _isolated_state):
    monkeypatch.setattr(C.tempfile, "tempdir", str(tmp_path))
    for name in ("CURSOR_DATA_DIR", "GOALFLIGHT_CURSOR_CONTEXT_MODE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GOALFLIGHT_DISPATCH_DIR", str(tmp_path / "dispatch"))
    monkeypatch.setenv("GOALFLIGHT_TASK_STORE", str(tmp_path / "tasks"))
    monkeypatch.setenv("GOALFLIGHT_WAKE_LEDGER", str(tmp_path / "wake.jsonl"))
    monkeypatch.setenv("GOALFLIGHT_PIDFILE_DIR", str(tmp_path / "pids"))


def seed(tmp_path):
    home = Path(os.environ["HOME"])
    servers = {name: {"command": "never-execute"} for name in (
        "context-mode", "hindsight", "deepwiki",
    )}
    global_config = home / ".cursor" / "mcp.json"
    global_config.parent.mkdir(parents=True)
    global_config.write_text(json.dumps({"mcpServers": servers}))
    project_config = tmp_path / ".cursor" / "mcp.json"
    project_config.parent.mkdir()
    project_config.write_text(json.dumps({"mcpServers": servers}))
    plugin = home / ".cursor/plugins/cache/context-mode/.mcp.json"
    plugin.parent.mkdir(parents=True)
    plugin.write_text(json.dumps({"mcpServers": {"context-mode": servers["context-mode"]}}))
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", str(tmp_path.resolve())).strip("-")
    state = home / ".cursor/projects" / slug
    state.mkdir(parents=True)
    for name, value in {
        "mcp-auth.json": {"hindsight": "test-credential"},
        "mcp-approvals.json": ["hindsight", "deepwiki"],
        "mcp-group-selections.json": {"deepwiki": "selected"},
        "mcp-disabled.json": ["unrelated-disabled"],
    }.items():
        (state / name).write_text(json.dumps(value))
    originals = {path: path.read_bytes() for path in (
        global_config, project_config, plugin, *state.iterdir(),
    )}
    return state, originals


@pytest.mark.parametrize("transport", ["text", "acp"])
@pytest.mark.parametrize("opt_in", [False, True])
def test_cursor_spawn_excludes_only_context_mode(monkeypatch, tmp_path, transport, opt_in):
    # Inspect the config at the mocked boundary; lifecycle is tested separately.
    monkeypatch.setattr(C, "cleanup_dispatch_data", lambda *a, **kw: None)
    source, originals = seed(tmp_path)
    if opt_in:
        monkeypatch.setenv("GOALFLIGHT_CURSOR_CONTEXT_MODE", "on")
    if transport == "text":
        spawn, _ = _stub_bash_launch(monkeypatch, tmp_path, resolved=(None, None), agent="cursor")
        env = spawn["env"]
    else:
        _, env, _ = _run_acp_to_spawn_failure(
            monkeypatch, tmp_path, resolved=(None, None), account=None,
            agent="cursor", spawn_base_env=dict(os.environ),
        )
    assert env["HOME"] == os.environ["HOME"]
    if opt_in:
        assert "CURSOR_DATA_DIR" not in env
        state = source
    else:
        data = Path(env["CURSOR_DATA_DIR"])
        assert data.parent == tmp_path
        assert data.stat().st_mode & 0o777 == 0o700
        state = data / "projects" / source.name
    disabled = json.loads((state / "mcp-disabled.json").read_text())
    # Cursor filters identifiers from all three definition sources through the
    # same disabled store; plugins use their namespaced identifier.
    definitions = {"context-mode", "plugin-context-mode-context-mode", "hindsight", "deepwiki"}
    available = definitions - set(disabled)
    assert available == (definitions if opt_in else {"hindsight", "deepwiki"})
    assert "unrelated-disabled" in disabled
    for path, contents in originals.items():
        assert path.read_bytes() == contents
    for name in ("mcp-auth.json", "mcp-approvals.json", "mcp-group-selections.json"):
        assert (state / name).read_bytes() == (source / name).read_bytes()
    assert sorted(p.name for p in (tmp_path / ".cursor").iterdir()) == ["mcp.json"]


def test_cursor_alias_uses_selected_data_and_actual_worktree(monkeypatch, tmp_path):
    # Git roots the disabled store at this worktree, not the shared main tree
    # or the cwd's subdirectory. A selected account/data root must survive.
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    source, _ = seed(tmp_path)
    data = tmp_path / "selected-data"
    data.mkdir()
    (data / "projects").mkdir()
    source.rename(data / "projects" / source.name)
    nested = tmp_path / "nested"
    nested.mkdir()
    env = {"HOME": str(tmp_path / "selected-home"), "CURSOR_DATA_DIR": str(data),
           "GOALFLIGHT_DISPATCH_ID": "selected-data"}
    C.isolate_context_mode("cursor-agent", env, cwd=str(nested))
    result = Path(env["CURSOR_DATA_DIR"]) / "projects" / source.name
    assert json.loads((result / "mcp-auth.json").read_text()) == {"hindsight": "test-credential"}
    assert "context-mode" in json.loads((result / "mcp-disabled.json").read_text())


def test_invalid_disabled_state_refuses_launch(tmp_path):
    source, _ = seed(tmp_path)
    (source / "mcp-disabled.json").write_text("{}")
    env = dict(os.environ)
    with pytest.raises(ValueError, match="Invalid Cursor MCP disabled list"):
        C.isolate_context_mode("cursor", env, cwd=str(tmp_path))
    assert "CURSOR_DATA_DIR" not in env
    assert not list(tmp_path.glob("goalflight-cursor-data-*"))


@pytest.mark.parametrize("case", ["nested", "symlink", "git-failure"])
def test_cursor_lexical_workspace_root(monkeypatch, tmp_path, case):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "nested").mkdir()
    root = repo
    if case == "symlink":
        root = tmp_path / "alias"
        root.symlink_to(repo, target_is_directory=True)
    if case == "git-failure":
        monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", "invalid-key")
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", "x")
        assert subprocess.run(["git", "-C", str(repo), "rev-parse", "--show-toplevel"],
                              capture_output=True).returncode != 0
    # Cursor index.js utils/git.ts A walks lexical ancestors to .git; its
    # workspace-paths.js s replaces non-alphanumerics and collapses dashes.
    # Expected root is independent of Git success and realpath of the alias.
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", str(root)).strip("-")
    env = {"HOME": str(tmp_path / "home"), "GOALFLIGHT_DISPATCH_ID": case}
    C.isolate_context_mode("cursor", env, cwd=str(root / "nested"))
    disabled = Path(env["CURSOR_DATA_DIR"]) / "projects" / slug / "mcp-disabled.json"
    assert json.loads(disabled.read_text()) == ["context-mode", "plugin-context-mode-context-mode"]


def test_copy_failure_removes_private_credentials(monkeypatch, tmp_path):
    seed(tmp_path)
    original = Path.write_bytes

    def fail_second_copy(path, content):
        if path.name == "mcp-approvals.json":
            raise OSError("copy failed")
        return original(path, content)

    monkeypatch.setattr(Path, "write_bytes", fail_second_copy)
    env = {**os.environ, "GOALFLIGHT_DISPATCH_ID": "copy-failed"}
    with pytest.raises(OSError, match="copy failed"):
        C.isolate_context_mode("cursor", env, cwd=str(tmp_path))
    assert not list(tmp_path.glob("goalflight-cursor-data-*"))
    assert "CURSOR_DATA_DIR" not in env


@pytest.mark.parametrize("transport", ["text", "acp", "text-validation"])
def test_failed_spawn_cleanup_requires_confirmed_absence(monkeypatch, tmp_path, transport):
    seed(tmp_path)
    if transport in {"text", "text-validation"}:
        _stub_bash_launch(monkeypatch, tmp_path, resolved=(None, None),
                          agent="cursor", failure_phase="spawn" if transport == "text" else "after_isolation")
    else:
        _run_acp_to_spawn_failure(monkeypatch, tmp_path, resolved=(None, None),
                                 agent="cursor", account=None, spawn_base_env=dict(os.environ))
    # Text daemon errors can lose the PID receipt after the child started.
    # ACP's retry helper accounts for unconfirmed termination explicitly.
    assert bool(list(tmp_path.glob("goalflight-cursor-data-*"))) is (transport == "text")


@pytest.mark.parametrize("state,launcher_live,worker_live,group_live,probe_error,removed", [
    ("complete", False, False, False, False, True),
    ("failed", False, False, False, False, True),
    ("running", False, False, False, False, False),
    ("complete", True, False, False, False, False),
    ("complete", False, True, False, False, False),
    ("complete", False, False, True, False, False),
    ("complete", False, False, False, True, False),
])
def test_terminal_sweep_preserves_live_and_uncertain_scopes(
    monkeypatch, tmp_path, state, launcher_live, worker_live, group_live, probe_error, removed,
):
    import goalflight_ledger as L
    env = {"HOME": str(tmp_path / "home"), "GOALFLIGHT_DISPATCH_ID": "cleanup-test"}
    C.isolate_context_mode("cursor", env, cwd=str(tmp_path))
    data = Path(env["CURSOR_DATA_DIR"])
    record = L.record_path("cleanup-test")
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(json.dumps({"state": state, "worker_pid": 42001, "worker_pgid": 42002}))

    def kill(pid, sig):
        if probe_error:
            raise PermissionError("unknown")
        if (pid == os.getpid() and launcher_live) or (pid == 42001 and worker_live):
            return
        raise ProcessLookupError()

    def killpg(pgid, sig):
        assert pgid == 42002
        if not group_live:
            raise ProcessLookupError()

    monkeypatch.setattr(C.os, "kill", kill)
    monkeypatch.setattr(C.os, "killpg", killpg)
    C.cleanup_dispatch_data()
    assert data.exists() is not removed


@pytest.mark.parametrize("entrypoint", ["watcher", "reconcile", "dry-run"])
def test_terminal_owners_invoke_cleanup(monkeypatch, tmp_path, entrypoint):
    from test_codex_dispatch_seams import D, L, W
    env = {"HOME": str(tmp_path / "home"), "GOALFLIGHT_DISPATCH_ID": "terminal-owner"}
    C.isolate_context_mode("cursor", env, cwd=str(tmp_path))
    data = Path(env["CURSOR_DATA_DIR"])
    record = L.record_path("terminal-owner")
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(json.dumps({"dispatch_id": "terminal-owner", "state": "complete", "worker_pid": 42001}))

    def gone(*args):
        raise ProcessLookupError()

    monkeypatch.setattr(C.os, "kill", gone)
    monkeypatch.setattr(C.os, "killpg", gone)
    monkeypatch.setattr(L, "cmd_finish", lambda *a, **kw: 0)
    if entrypoint == "watcher":
        W._finish_existing_ledger("terminal-owner", "complete", "test", agent="cursor", detached=True,
                                  worker_still_alive=False)
    else:
        D.reconcile_abandoned_dispatches(queue_dir=tmp_path / "queue", dry_run=entrypoint == "dry-run")
    assert data.exists() is (entrypoint == "dry-run")


def test_terminal_sweep_keeps_unowned_data(tmp_path):
    legacy = tmp_path / "goalflight-cursor-data-legacy"
    legacy.mkdir()
    C.cleanup_dispatch_data()
    assert legacy.exists()
