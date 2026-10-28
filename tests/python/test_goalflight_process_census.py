from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_process_census as census  # noqa: E402


ENTRYPOINT = "/opt/mcp/context-mode/index.js"
NODE = "/usr/bin/node"
NPX_LAUNCHER = "/opt/npm/bin/npx-cli.js"


def _process(
    pid: int,
    ppid: int,
    argv: tuple[str, ...] | None,
    *,
    command: str | None = None,
    executable_path: str | None = None,
    possible_mcp: bool = False,
    start_token: str | None = None,
) -> census.Process:
    if executable_path is None and argv and Path(argv[0]).is_absolute():
        executable_path = census._path_key(argv[0])
    return census.Process(
        pid=pid,
        ppid=ppid,
        command=command or (argv[0] if argv else "node"),
        argv=argv,
        executable_path=executable_path,
        possible_mcp=possible_mcp,
        start_token=start_token,
        identity_verified=True,
    )


def _registrations(
    entrypoint: str = ENTRYPOINT,
    *,
    interpreter: str = NODE,
    launcher: census.Launcher | None = None,
) -> dict[str, tuple[census.RegisteredServer, ...]]:
    return {
        entrypoint: (
            census.RegisteredServer(frozenset({interpreter}), launcher),
        )
    }


def _assert_not_signalled(classified: dict[int, str], processes, pid: int) -> None:
    signalled: list[int] = []
    census.reap_orphans(
        classified,
        processes,
        identity_matches=lambda *_: True,
        signal_process=lambda process_pid, _sig: signalled.append(process_pid),
    )
    assert pid not in signalled


def test_incident_codex_worker_is_never_selected() -> None:
    worker = _process(
        101,
        1,
        (
            "codex",
            "exec",
            "--skip-git-repo-check",
            "-c",
            "mcp_servers.context-mode.enabled=false",
        ),
    )

    classified = census.classify_processes([worker], {101}, _registrations())

    assert classified == {101: census.DISPATCH_WORKER}
    assert census.MCP_ORPHAN not in classified.values()


def test_session_parented_mcp_server_is_not_an_orphan() -> None:
    processes = [
        _process(200, 1, ("/usr/bin/codex",)),
        _process(201, 200, ("/usr/bin/npm", "exec", "context-mode")),
        _process(202, 201, (NODE, ENTRYPOINT), possible_mcp=True, start_token="s202"),
    ]

    classified = census.classify_processes(processes, set(), _registrations())

    assert classified[202] == census.MCP_LIVE_PARENT
    assert census.MCP_ORPHAN not in classified.values()
    _assert_not_signalled(classified, processes, 202)


@pytest.mark.parametrize("parent_argv", [(NODE, "/opt/claude-code/cli.js"), ("/opt/claude.exe",)])
def test_unknown_live_parent_is_not_reaped(parent_argv: tuple[str, ...]) -> None:
    parent = _process(207, 1, parent_argv, command=Path(parent_argv[0]).name)
    server = _process(208, 207, (NODE, ENTRYPOINT), possible_mcp=True, start_token="s208")
    processes = [parent, server]

    classified = census.classify_processes(processes, set(), _registrations())

    assert classified[208] == census.MCP_LIVE_PARENT
    _assert_not_signalled(classified, processes, 208)


def test_registered_launcher_chain_to_launchd_is_an_orphan() -> None:
    launcher = census.Launcher(
        NPX_LAUNCHER,
        ("-y", "context-mode@latest"),
        frozenset({NODE}),
    )
    processes = [
        _process(209, 1, (NODE, NPX_LAUNCHER, "-y", "context-mode@latest")),
        _process(210, 209, (NODE, ENTRYPOINT), possible_mcp=True, start_token="s210"),
    ]

    classified = census.classify_processes(
        processes, set(), _registrations(launcher=launcher)
    )

    assert classified[210] == census.MCP_ORPHAN


def test_launcher_for_a_different_server_is_a_live_parent() -> None:
    other_entrypoint = "/opt/mcp/other/index.js"
    launcher = census.Launcher(
        NPX_LAUNCHER,
        ("-y", "other-mcp@latest"),
        frozenset({NODE}),
    )
    processes = [
        _process(211, 1, (NODE, NPX_LAUNCHER, "-y", "other-mcp@latest")),
        _process(212, 211, (NODE, ENTRYPOINT), possible_mcp=True, start_token="s212"),
    ]
    registrations = _registrations(
        launcher=census.Launcher(
            NPX_LAUNCHER,
            ("-y", "context-mode@latest"),
            frozenset({NODE}),
        )
    )
    registrations.update(_registrations(other_entrypoint, launcher=launcher))

    classified = census.classify_processes(processes, set(), registrations)

    assert classified[212] == census.MCP_LIVE_PARENT
    _assert_not_signalled(classified, processes, 212)


@pytest.mark.parametrize("interpreter", ["/bin/sh", "/usr/bin/python3"])
def test_registered_entrypoint_with_wrong_interpreter_is_unknown(
    interpreter: str,
) -> None:
    process = _process(
        213,
        1,
        (interpreter, ENTRYPOINT),
        possible_mcp=True,
        start_token="s213",
    )

    classified = census.classify_processes([process], set(), _registrations())

    assert classified[213] == census.UNKNOWN
    _assert_not_signalled(classified, [process], 213)


@pytest.mark.parametrize(
    "executable_path", ["/different-install/bin/node", None]
)
def test_bare_argv_interpreter_does_not_use_census_path(
    executable_path: str | None,
) -> None:
    registered_interpreter = "/opt/homebrew/Cellar/node/26.11.1/bin/node"
    process = _process(
        214,
        1,
        ("node", "/mcp/server.js"),
        executable_path=executable_path,
        possible_mcp=True,
        start_token="s214",
    )

    classified = census.classify_processes(
        [process],
        set(),
        _registrations("/mcp/server.js", interpreter=registered_interpreter),
    )

    assert classified[214] == census.UNKNOWN
    _assert_not_signalled(classified, [process], 214)


def test_relative_script_argv_is_unknown_and_never_signalled() -> None:
    process = _process(203, 1, (NODE, "server.js"), possible_mcp=True, start_token="s203")

    classified = census.classify_processes([process], set(), _registrations("/app/server.js"))

    assert classified[203] == census.UNKNOWN
    _assert_not_signalled(classified, [process], 203)


def test_missing_ancestor_is_unknown_and_never_signalled() -> None:
    process = _process(204, 999, (NODE, ENTRYPOINT), possible_mcp=True, start_token="s204")

    classified = census.classify_processes([process], set(), _registrations())

    assert classified[204] == census.UNKNOWN
    _assert_not_signalled(classified, [process], 204)


def test_ps_parent_pid_mismatch_is_unknown_and_never_signalled(monkeypatch) -> None:
    monkeypatch.setattr(
        census.goalflight_compat,
        "process_start_identity",
        lambda pid, *, include_ancestry=False: {
            "pid": pid,
            "start_token": "new-generation",
            "ppid": 999,
        },
    )
    monkeypatch.setattr(
        census.goalflight_compat,
        "process_argv",
        lambda *_args, **_kwargs: [NODE, ENTRYPOINT],
    )
    processes = census._capture_processes([(205, 1, "S", "node")], {"node"})

    classified = census.classify_processes(processes, set(), _registrations())

    assert processes[0].ppid == 999
    assert classified[205] == census.UNKNOWN
    _assert_not_signalled(classified, processes, 205)


def test_executable_path_from_reused_pid_is_not_trusted(monkeypatch) -> None:
    identities = iter(
        [
            {"pid": 215, "start_token": "old-generation", "ppid": 1},
            {"pid": 215, "start_token": "new-generation", "ppid": 1},
        ]
    )
    monkeypatch.setattr(
        census.goalflight_compat,
        "process_start_identity",
        lambda *_args, **_kwargs: next(identities),
    )
    monkeypatch.setattr(
        census.goalflight_compat,
        "process_argv",
        lambda *_args, **_kwargs: [NODE, ENTRYPOINT],
    )
    monkeypatch.setattr(census, "_process_executable_path", lambda _pid: NODE)

    processes = census._capture_processes([(215, 1, "S", "node")], {"node"})

    assert not processes[0].identity_verified
    assert processes[0].executable_path is None
    classified = census.classify_processes(processes, set(), _registrations())
    assert classified[215] == census.UNKNOWN
    _assert_not_signalled(classified, processes, 215)


def test_only_exact_unparented_registered_entrypoint_is_an_orphan() -> None:
    registered_interpreter = "/opt/homebrew/Cellar/node/26.11.1/bin/node"
    processes = [
        _process(
            301,
            1,
            ("node", ENTRYPOINT),
            executable_path=registered_interpreter,
            possible_mcp=True,
            start_token="s301",
        ),
        _process(302, 1, (NODE, "/opt/mcp/context-mode-wrapper.js"), possible_mcp=True),
    ]

    classified = census.classify_processes(
        processes,
        set(),
        _registrations(interpreter=registered_interpreter),
    )

    assert classified == {301: census.MCP_ORPHAN, 302: census.UNKNOWN}


def test_unreadable_status_file_fails_closed_for_possible_orphan(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    dispatch_dir = state_dir / "dispatch"
    dispatch_dir.mkdir(parents=True, exist_ok=True)
    (dispatch_dir / "unreadable.status.json").mkdir()
    sources = census.worker_pid_sources([state_dir])
    session = _process(400, 1, ("codex", "exec"))
    server = _process(
        401,
        400,
        (NODE, ENTRYPOINT),
        possible_mcp=True,
        start_token="s401",
    )

    classified = census.classify_processes(
        [session, server], set(), _registrations(), worker_sources_complete=sources.complete
    )

    assert not sources.complete
    assert classified == {401: census.UNKNOWN}
    assert 401 not in [pid for pid, kind in classified.items() if kind == census.MCP_ORPHAN]


def test_worker_pids_are_loaded_from_each_state_root(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    (first / "dispatch").mkdir(parents=True)
    (second / "runs.d").mkdir(parents=True)
    (second / "dispatches" / "remote-1").mkdir(parents=True)
    (first / "dispatch" / "local.status.json").write_text(
        json.dumps({"dispatch_id": "local", "worker_pid": 501}), encoding="utf-8"
    )
    (second / "runs.d" / "record.json").write_text(
        json.dumps({"dispatch_id": "run", "worker_pid": 502}), encoding="utf-8"
    )
    (second / "dispatches" / "remote-1" / "status.json").write_text(
        json.dumps({"dispatch_id": "remote-1", "worker_pid": 503}), encoding="utf-8"
    )

    sources = census.worker_pid_sources([first, second])

    assert sources.complete
    assert sources.pids == {501, 502, 503}


def test_npx_registration_resolves_only_invoked_cached_package_script(tmp_path: Path) -> None:
    package = tmp_path / ".npm" / "_npx" / "cache" / "node_modules" / "demo-mcp"
    (package / "dist").mkdir(parents=True)
    (package / "tools").mkdir()
    (package / "dist" / "server.js").write_text("#!/usr/bin/env node\n", encoding="utf-8")
    (package / "tools" / "unrelated.js").write_text("#!/usr/bin/env node\n", encoding="utf-8")
    (package / "package.json").write_text(
        json.dumps(
            {
                "name": "demo-mcp",
                "bin": {
                    "demo-mcp": "dist/server.js",
                    "unrelated-tool": "tools/unrelated.js",
                },
            }
        ),
        encoding="utf-8",
    )
    shim = package.parent / ".bin" / "demo-mcp"
    shim.parent.mkdir(parents=True)
    shim.symlink_to("../demo-mcp/dist/server.js")

    entrypoints, interpreters, errors = census.resolve_mcp_entrypoints(
        [["npx", "-y", "demo-mcp@1.2.3"]], cwd=tmp_path, home=tmp_path
    )

    assert set(entrypoints) == {str((package / "dist" / "server.js").resolve())}
    assert "node" in interpreters
    assert not errors

    unrelated = _process(
        206,
        1,
        (NODE, str(package / "tools" / "unrelated.js")),
        possible_mcp=True,
        start_token="s206",
    )
    classified = census.classify_processes([unrelated], set(), entrypoints)
    assert classified[206] == census.UNKNOWN
    _assert_not_signalled(classified, [unrelated], 206)


def test_pid_reuse_between_census_and_signal_is_skipped() -> None:
    process = _process(601, 1, (NODE, ENTRYPOINT), start_token="old-generation")
    signalled: list[tuple[int, int]] = []

    actions = census.reap_orphans(
        {601: census.MCP_ORPHAN},
        [process],
        identity_matches=lambda _pid, token: token == "new-generation",
        signal_process=lambda pid, sig: signalled.append((pid, sig)),
    )

    assert signalled == []
    assert actions == [
        {
            "pid": 601,
            "action": "skipped",
            "reason": "process identity changed or unavailable",
        }
    ]
