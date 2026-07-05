"""--os-sandbox opt-in passthrough for the bash-shape codex worker.

Covers: profile resolution + precedence, the codex --sandbox mapping (off ->
danger-full-access), that the always-forbidden bypass flags never leak, the
--read-only/--os-sandbox conflict guard, the required 'off' dispatch log, and
that the profile survives submit->drain via the canonical replay argv.

Repo convention: case_* functions invoked by main(), run as `python <file>.py`.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import goalflight_dispatch as d  # noqa: E402


def _args(**kw):
    base = dict(agent="codex", shape="bash", read_only=False, os_sandbox=None,
                model=None, cwd="/tmp/x")
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
    # Advisory (not a hard error) when the flag can't be honored by the worker.
    w2 = d._os_sandbox_warning(_args(agent="grok-code", os_sandbox="off"))
    assert w2 and "only affects bash-shape codex" in w2, w2


def _replay(**kw):
    base = dict(agent="codex", dispatch_id="dx", cwd="/tmp/x", shape="bash",
                priority="normal", billing="sub", poll_secs=2.0, max_idle_secs=600.0,
                prompt_file="/tmp/p.md", prompt=None, task_ids=[], model=None,
                read_only=False, os_sandbox=None, web_research_ok=False,
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


def main() -> None:
    case_resolution_precedence()
    case_codex_sandbox_mapping()
    case_off_never_leaks_forbidden_flags()
    case_conflict_guard()
    case_off_is_logged()
    case_profile_survives_submit_drain()
    print("test_dispatch_os_sandbox: all cases passed")


if __name__ == "__main__":
    main()
