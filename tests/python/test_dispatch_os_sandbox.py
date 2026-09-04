"""--os-sandbox opt-in passthrough for the bash-shape codex worker.

Covers: profile resolution + precedence, the codex --sandbox mapping (off ->
danger-full-access), that the always-forbidden bypass flags never leak, the
--read-only/--os-sandbox conflict guard, the required 'off' dispatch log, and
that the profile survives backlog drain via the canonical replay argv.

Repo convention: case_* functions invoked by main(), run as `python <file>.py`.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import sys
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import goalflight_adapter_readiness as readiness  # noqa: E402
import goalflight_dispatch as d  # noqa: E402
import goalflight_os_sandbox as sandbox  # noqa: E402

_CURSOR_REFUSED_SANDBOX_FLAGS = (
    "--sandbox",
    "--os-sandbox",
    "--no-sandbox",
    "--disable-sandbox",
    "--sandbox-disable",
)


def _args(**kw):
    base = dict(agent="codex", shape="bash", read_only=False, os_sandbox=None,
                model=None, cwd="/tmp/x", prompt=None, prompt_file=None,
                ignore_git_warn=True)
    base.update(kw)
    return argparse.Namespace(**base)


def _sandbox_value(**kw) -> str:
    argv, _ = d.build_worker(_args(**kw), "/tmp/p.md", [])
    assert argv[0:2] == ["codex", "exec"], argv
    return argv[argv.index("--sandbox") + 1]


def case_resolution_precedence() -> None:
    assert d._effective_os_sandbox(_args()) == "workspace-write"
    assert d._effective_os_sandbox(_args(read_only=True)) == "read-only"
    assert d._effective_os_sandbox(_args(os_sandbox="off")) == "off"
    assert d._effective_os_sandbox(_args(os_sandbox="read-only")) == "read-only"
    assert d._effective_os_sandbox(_args(os_sandbox="workspace-write")) == "workspace-write"
    assert d._effective_read_only(_args(os_sandbox="read-only")) is True
    assert d._effective_read_only(_args(os_sandbox="off")) is False


def case_codex_sandbox_mapping() -> None:
    # Default is unchanged; every existing dispatch stays workspace-write.
    assert _sandbox_value() == "workspace-write"
    assert _sandbox_value(read_only=True) == "read-only"
    assert _sandbox_value(os_sandbox="read-only") == "read-only"
    assert _sandbox_value(os_sandbox="workspace-write") == "workspace-write"
    # off -> Seatbelt disabled via codex's sanctioned value.
    assert _sandbox_value(os_sandbox="off") == "danger-full-access"


def case_off_never_leaks_forbidden_flags() -> None:
    argv, _ = d.build_worker(_args(os_sandbox="off"), "/tmp/p.md", [])
    assert not any("dangerously" in a for a in argv), argv
    assert "--no-sandbox" not in argv, argv
    # approval posture stays intact (off is a sandbox profile, not a bypass).
    assert "approval_policy=never" in argv, argv


def case_conflict_guard() -> None:
    for bad in ("off", "workspace-write"):
        try:
            d._validate_os_sandbox_conflict(_args(read_only=True, os_sandbox=bad))
        except d.DispatchUsageError:
            pass
        else:
            raise AssertionError(f"--read-only + --os-sandbox {bad} should conflict")
    # Agreement and single-flag forms are fine.
    d._validate_os_sandbox_conflict(_args(read_only=True, os_sandbox="read-only"))
    d._validate_os_sandbox_conflict(_args(os_sandbox="off"))
    d._validate_os_sandbox_conflict(_args())


def case_off_is_logged() -> None:
    w = d._os_sandbox_warning(_args(os_sandbox="off"))
    assert w and "DISABLED" in w and "danger-full-access" in w, w
    assert d._os_sandbox_warning(_args()) is None
    # Inert --os-sandbox is a refusal, not an advisory that still launches.
    assert d._os_sandbox_warning(_args(agent="grok-code", os_sandbox="off")) is None
    try:
        d._validate_agent_os_sandbox(_args(agent="grok-code", os_sandbox="off"))
    except d.DispatchUsageError as exc:
        message = str(exc)
        assert "--os-sandbox" in message, message
        assert "--read-only" in message, message
    else:
        raise AssertionError("inert --os-sandbox for grok-code must refuse")


def case_claude_acp_read_only_fallback_notice_is_pinned() -> None:
    args = _args(agent="claude", shape="acp", read_only=True)
    warning = d._os_sandbox_warning(args)
    assert warning == (
        "SANDBOX FALLBACK: requested=read-only -> applied=off -> "
        "enforcement=acp-permissions"
    ), warning
    d._validate_agent_os_sandbox(args)
    explicit = _args(
        agent="claude", shape="acp", read_only=False, os_sandbox="read-only"
    )
    try:
        d._validate_agent_os_sandbox(explicit)
    except d.DispatchUsageError as exc:
        message = str(exc)
        assert "--os-sandbox" in message, message
        assert "--read-only" in message, message
    else:
        raise AssertionError("inert --os-sandbox on claude ACP must refuse")


def case_acp_supported_and_unrequested_warning_paths_are_unchanged() -> None:
    supported = _args(agent="codex-acp", shape="acp", read_only=True)
    unrequested = _args(agent="claude", shape="acp", read_only=False)
    assert json.dumps(d._dispatch_warnings(supported, []), sort_keys=True) == "[]"
    assert json.dumps(d._dispatch_warnings(unrequested, []), sort_keys=True) == "[]"


def _replay(**kw):
    base = dict(agent="codex", dispatch_id="dx", cwd="/tmp/x", shape="bash",
                priority="normal", billing="sub", poll_secs=2.0, max_idle_secs=600.0,
                prompt_file="/tmp/p.md", prompt=None, task_ids=[], model=None,
                read_only=False, os_sandbox=None, web_research_ok=False,
                web_qa=False,
                ignore_git_warn=False, capacity_wait_s=None, account=None,
                interactive=False, permission_mode="auto", permission_dir=None,
                permission_inline_timeout_s=None, permission_user_timeout_s=None,
                controller_pid=None)
    base.update(kw)
    return d._canonical_replay_argv(argparse.Namespace(**base), [],
                                    tail=Path("/tmp/t"), status_json=Path("/tmp/s"))


def case_profile_survives_submit_drain() -> None:
    # off must reconstruct as --os-sandbox off so a queued dispatch drains with off.
    r_off = _replay(os_sandbox="off")
    assert r_off[r_off.index("--os-sandbox") + 1] == "off", r_off
    assert "--read-only" not in r_off, r_off
    # Legacy --read-only still reconstructs as --read-only (back-compat).
    r_ro = _replay(read_only=True)
    assert "--read-only" in r_ro and "--os-sandbox" not in r_ro, r_ro
    # Default emits neither.
    r_def = _replay()
    assert "--os-sandbox" not in r_def and "--read-only" not in r_def, r_def


@contextmanager
def _force_darwin_sandbox_exec():
    """Hermetic Darwin + sandbox-exec so argv wrap is testable on Linux CI."""

    def _which(name: str) -> str | None:
        if name == "sandbox-exec":
            return "/usr/bin/sandbox-exec"
        return None

    with (
        mock.patch.object(sandbox, "os_sandbox_platform_key", return_value="darwin"),
        mock.patch.object(
            sandbox,
            "platform_supported_os_sandbox_profiles",
            return_value=["off", "read-only", "workspace-write"],
        ),
        mock.patch.object(sandbox.goalflight_compat, "is_windows", return_value=False),
        mock.patch.object(sandbox.shutil, "which", side_effect=_which),
        mock.patch.object(readiness, "os_sandbox_platform_key", return_value="darwin"),
        mock.patch.object(
            readiness,
            "platform_supported_os_sandbox_profiles",
            return_value=["off", "read-only", "workspace-write"],
        ),
    ):
        yield


def _cursor_agent_tail(argv: list[str]) -> list[str]:
    try:
        return argv[argv.index("cursor-agent") :]
    except ValueError as exc:
        raise AssertionError(f"cursor-agent missing from argv: {argv}") from exc


def case_cursor_read_only_argv_wraps_sandbox_exec_on_darwin() -> None:
    """Controllers keep --os-sandbox read-only; runner wraps, cursor-cli does not."""
    with _force_darwin_sandbox_exec():
        for agent, model in (
            ("cursor", None),
            ("cursor", "kimi-k3-high"),
            ("cursor-agent", "kimi-k3-high"),
        ):
            argv, stdin_path = d.build_worker(
                _args(
                    agent=agent,
                    os_sandbox="read-only",
                    cwd=str(REPO_ROOT),
                    model=model,
                ),
                "/tmp/p.md",
                [],
            )
            assert Path(argv[0]).name == "sandbox-exec", (agent, model, argv)
            assert argv[1] == "-p", (agent, model, argv)
            tail = _cursor_agent_tail(argv)
            assert tail[0] == "cursor-agent", (agent, model, argv)
            for flag in _CURSOR_REFUSED_SANDBOX_FLAGS:
                assert flag not in tail, (agent, model, flag, tail)
            assert not any("dangerously" in part for part in tail), (agent, model, tail)
            if model:
                assert tail[tail.index("--model") + 1] == model, (agent, model, tail)
            assert stdin_path == "/tmp/p.md", stdin_path

        workspace_argv, _ = d.build_worker(
            _args(agent="cursor", os_sandbox="workspace-write", cwd=str(REPO_ROOT)),
            "/tmp/p.md",
            [],
        )
        assert Path(workspace_argv[0]).name == "sandbox-exec", workspace_argv
        assert "--sandbox" not in _cursor_agent_tail(workspace_argv), workspace_argv

        off_argv, _ = d.build_worker(
            _args(agent="cursor", os_sandbox="off", cwd=str(REPO_ROOT)),
            "/tmp/p.md",
            [],
        )
        assert off_argv[0] == "cursor-agent", off_argv
        assert "sandbox-exec" not in off_argv[0], off_argv

        read_only_alias, _ = d.build_worker(
            _args(agent="cursor", read_only=True, os_sandbox=None, cwd=str(REPO_ROOT)),
            "/tmp/p.md",
            [],
        )
        assert Path(read_only_alias[0]).name == "sandbox-exec", read_only_alias
        assert "--sandbox" not in _cursor_agent_tail(read_only_alias), read_only_alias

        d._validate_agent_os_sandbox(
            _args(agent="cursor", shape="bash", os_sandbox="read-only", cwd=str(REPO_ROOT))
        )

        raw = ["cursor-agent", "-p", "--force", "--trust", "--output-format", "text"]
        raw_wrapped, raw_stdin = d.build_worker(
            _args(agent="cursor", os_sandbox="read-only", cwd=str(REPO_ROOT)),
            "/tmp/p.md",
            raw,
        )
        assert Path(raw_wrapped[0]).name == "sandbox-exec", raw_wrapped
        assert raw_stdin is None, raw_stdin
        assert "--sandbox" not in _cursor_agent_tail(raw_wrapped), raw_wrapped


def case_cursor_read_only_argv_stays_unwrapped_off_darwin() -> None:
    """Linux/other hosts keep documented off-only behavior; no invented CLI flag."""
    if sandbox.os_sandbox_platform_key() == "darwin":
        return
    argv, _ = d.build_worker(
        _args(agent="cursor", os_sandbox="read-only", cwd=str(REPO_ROOT), model="kimi-k3-high"),
        "/tmp/p.md",
        [],
    )
    assert argv[0] == "cursor-agent", argv
    assert Path(argv[0]).name != "sandbox-exec", argv
    for flag in _CURSOR_REFUSED_SANDBOX_FLAGS:
        assert flag not in argv, (flag, argv)
    assert argv[argv.index("--model") + 1] == "kimi-k3-high", argv
    try:
        d._validate_agent_os_sandbox(
            _args(agent="cursor", shape="bash", os_sandbox="read-only", cwd=str(REPO_ROOT))
        )
    except d.UnsupportedAgentSandboxRequest as exc:
        message = str(exc)
        assert "--os-sandbox" in message, message
        assert "agent=cursor" in message, message
    else:
        raise AssertionError("cursor bash --os-sandbox read-only must refuse off Darwin")


def case_moonshot_argv_stays_unwrapped_on_darwin() -> None:
    """Kimi-via-cursor is a cursor model; moonshot agent_id stays its own path."""
    import tempfile

    with tempfile.TemporaryDirectory(prefix="gf-moonshot-argv-") as tmp:
        prompt = Path(tmp) / "p.md"
        prompt.write_text("Inspect the tree.\n", encoding="utf-8")
        with _force_darwin_sandbox_exec():
            argv, stdin_path = d.build_worker(
                _args(agent="moonshot", os_sandbox=None, cwd=str(REPO_ROOT)),
                str(prompt),
                [],
            )
        assert argv[:2] == ["/bin/sh", "-lc"], argv
        assert not any(Path(part).name == "sandbox-exec" for part in argv), argv
        assert "kimi" in argv[2], argv
        assert stdin_path is None, stdin_path
        d._validate_agent_os_sandbox(
            _args(agent="moonshot", shape="bash", os_sandbox="off", cwd=str(REPO_ROOT))
        )
        try:
            d._validate_agent_os_sandbox(
                _args(
                    agent="moonshot",
                    shape="bash",
                    os_sandbox="read-only",
                    cwd=str(REPO_ROOT),
                )
            )
        except d.UnsupportedAgentSandboxRequest as exc:
            assert "supports only --os-sandbox off" in str(exc), exc
        else:
            raise AssertionError("moonshot --os-sandbox read-only must stay refused")


def case_boundary_rejected_early_names_the_real_cause() -> None:
    """The sandbox refuses when cwd sits in a temp/agent-state root. Say so up front.

    That refusal is correct -- inside those roots it cannot separate "the
    workspace" from "everywhere the worker may already write". But it used to
    surface only after a detached worker had launched and died as
    `blocked_os_sandbox`, with the reason buried in a status file.

    Two controllers independently misread that as "--os-sandbox / --read-only is
    broken for ACP shapes" and proposed rejecting those flags for ACP outright,
    which would break sandboxed ACP dispatches from ordinary worktrees. The
    trigger is the cwd LOCATION -- not the shape, not the flag. Hence the
    non-temp cases below: they are the ones a shape-based rejection would have
    broken.
    """
    repo_cwd = str(Path(__file__).resolve().parents[2])
    for shape in ("acp", "bash"):
        for profile in ("read-only", "workspace-write"):
            try:
                d._validate_os_sandbox_boundary(
                    _args(agent="cursor", shape=shape, os_sandbox=profile, cwd="/tmp/gf-probe")
                )
            except d.DispatchUsageError as exc:
                message = str(exc)
                assert "working directory" in message, message
                # Must name the cause and a way out, not just fail.
                assert "temp" in message or "root" in message, message
                assert "--os-sandbox off" in message, message
            else:
                raise AssertionError(f"cwd inside a temp root must reject early ({shape}/{profile})")

            # A real worktree is fine for BOTH shapes -- the case a shape-based
            # rejection would have wrongly blocked.
            d._validate_os_sandbox_boundary(
                _args(agent="cursor", shape=shape, os_sandbox=profile, cwd=repo_cwd)
            )
    # `off` asks for no boundary, so it is never rejected, even from /tmp.
    d._validate_os_sandbox_boundary(
        _args(agent="cursor", shape="acp", os_sandbox="off", cwd="/tmp/gf-probe")
    )

    # THE REGRESSION THIS GUARD ALREADY CAUSED ONCE. The first version gated on
    # the EFFECTIVE profile, which falls back to workspace-write when no flag is
    # passed -- so it rejected every dispatch from a temp cwd, including the many
    # that never asked for a sandbox and run fine without one. Two existing test
    # modules went red and it was reverted.
    #
    # Without these cases the test passes even with that regression reinstated
    # (verified: the check was blind to it). An unflagged dispatch must be
    # allowed from anywhere -- that is the common case, and refusing it is far
    # worse than the late failure this guard exists to fix.
    for shape in ("acp", "bash"):
        for cwd in ("/tmp/gf-probe", repo_cwd):
            d._validate_os_sandbox_boundary(
                _args(agent="cursor", shape=shape, os_sandbox=None,
                      read_only=False, cwd=cwd)
            )


def main() -> None:
    case_resolution_precedence()
    case_codex_sandbox_mapping()
    case_off_never_leaks_forbidden_flags()
    case_conflict_guard()
    case_off_is_logged()
    case_claude_acp_read_only_fallback_notice_is_pinned()
    case_acp_supported_and_unrequested_warning_paths_are_unchanged()
    case_profile_survives_submit_drain()
    case_cursor_read_only_argv_wraps_sandbox_exec_on_darwin()
    case_cursor_read_only_argv_stays_unwrapped_off_darwin()
    case_moonshot_argv_stays_unwrapped_on_darwin()
    case_boundary_rejected_early_names_the_real_cause()
    print("test_dispatch_os_sandbox: all cases passed")


if __name__ == "__main__":
    main()
