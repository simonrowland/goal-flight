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
    env = {"HOME": str(tmp_path / "selected-home"), "CURSOR_DATA_DIR": str(data)}
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
