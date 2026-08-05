#!/usr/bin/env python3
"""The controller session beacon: a stable, externally-answerable identity.

ensure_session() keys a record by the CALLER's pid. That is right for a human
at a long-lived terminal and useless for a controller that reaches the CLI
through one-shot tool calls -- every call is a fresh python3 process, so every
call minted a new id. Measured before this existed: three consecutive
--ensure-session invocations returned three different ids.

The beacon anchors identity to a long-running process instead. Its pid is
stable while the controller works, and it is observable from outside, which is
what makes "is this worker mine?" and "is that controller still alive?"
measurable rather than inferred.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_session_status as S  # noqa: E402


def assert_true(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)


def _beacon() -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])


def test_identity_is_stable_across_separate_resolutions() -> None:
    """The whole point: repeated lookups agree.

    This is the property ensure_session could not provide, and the one most
    likely to rot silently -- nothing else fails loudly if ids start drifting;
    ownership just quietly stops matching.
    """
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        proc = _beacon()
        try:
            claimed = S.claim_session(root, pid=proc.pid, label="controller")
            seen = {S.live_session(root)["id"] for _ in range(5)}
            assert_true("every resolution returns one id", seen == {claimed["id"]})
        finally:
            proc.terminate()
            proc.wait(timeout=10)


def test_claim_is_idempotent_for_the_same_beacon() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        proc = _beacon()
        try:
            first = S.claim_session(root, pid=proc.pid)
            second = S.claim_session(root, pid=proc.pid)
            assert_true("re-claiming does not mint a new id", first["id"] == second["id"])
        finally:
            proc.terminate()
            proc.wait(timeout=10)


def test_reused_pid_replaces_the_stale_process_generation() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        pid = 424242
        old_identity = {"pid": pid, "start_token": "generation-old"}
        new_identity = {"pid": pid, "start_token": "generation-new"}
        with patch.object(
            S,
            "_controller_process_identity",
            side_effect=[old_identity, new_identity, new_identity],
        ):
            old = S.claim_session(
                root,
                pid=pid,
                session_id="old-session",
                label="battery-old",
            )
            new = S.claim_session(
                root,
                pid=pid,
                session_id="new-session",
                label="battery-main",
            )
            resolved = S.live_session(root, label="battery-main")
        assert_true("the reused pid does not keep the old id", old["id"] != new["id"])
        assert_true("the new process generation is live", resolved is not None)
        assert_true("the new generation owns the slot", resolved["id"] == "new-session")


def test_startup_liveness_error_is_fail_open() -> None:
    with tempfile.TemporaryDirectory() as td:
        with patch.object(S, "_pid_alive", side_effect=OSError("probe failed")):
            result = S.claim_controller_startup(
                Path(td),
                pid=12345,
                label="battery-main",
            )
        assert_true("liveness failure does not escape", result.get("claimed") is False)
        assert_true("liveness failure is reported", result.get("reason") == "claim_failed")
        assert_true("failure type stays observable", result.get("error_type") == "OSError")


def test_unavailable_process_generation_preserves_existing_beacon() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        proc = _beacon()
        try:
            first = S.claim_controller_startup(
                root,
                pid=proc.pid,
                label="battery-main",
            )
            assert_true("initial measured claim succeeds", first.get("claimed") is True)
            first_id = first["session"]["id"]
            with patch.object(S, "_controller_process_identity", return_value=None):
                failed = S.claim_controller_startup(
                    root,
                    pid=proc.pid,
                    label="battery-main",
                )
            assert_true("missing generation fails honestly", failed.get("claimed") is False)
            assert_true("missing generation is a claim failure", failed.get("reason") == "claim_failed")
            resolved = S.live_session(root, label="battery-main", pid=proc.pid)
            assert_true("prior measured beacon survives", resolved is not None)
            assert_true("prior session id is preserved", resolved["id"] == first_id)
        finally:
            proc.terminate()
            proc.wait(timeout=10)


def test_linux_identity_requires_measured_boot_generation() -> None:
    stat = "123 (controller) " + " ".join(["S", *(["0"] * 18), "98765"])
    with (
        patch.object(S.sys, "platform", "linux"),
        patch.object(S, "_pid_alive", return_value=True),
        patch.object(S.Path, "read_text", side_effect=[stat, OSError("denied")]),
    ):
        identity = S._controller_process_identity(123)
    assert_true("an unmeasured boot generation is not invented", identity is None)


def test_startup_names_legacy_beacon_once_and_rejects_relabel() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        proc = _beacon()
        try:
            legacy = S.claim_session(root, pid=proc.pid)
            named = S.claim_controller_startup(root, pid=proc.pid, label="battery-main")
            assert_true("legacy beacon adopts its declared label", named.get("claimed") is True)
            assert_true("session id remains stable", named["session"]["id"] == legacy["id"])
            assert_true("declared label is stored", named["session"]["label"] == "battery-main")

            relabel = S.claim_controller_startup(root, pid=proc.pid, label="battery-bugs")
            assert_true("live controller cannot silently relabel", relabel.get("claimed") is False)
            assert_true("relabel cause is explicit", relabel.get("reason") == "controller_label_mismatch")
            resolved = S.live_session(root, label="battery-main", pid=proc.pid)
            assert_true("original label remains live", resolved is not None)
            assert_true("new label was not written", S.live_session(root, label="battery-bugs") is None)
        finally:
            proc.terminate()
            proc.wait(timeout=10)


def test_repo_default_duplicate_surfaces_conflict() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "shared-repo"
        root.mkdir()
        first, second = _beacon(), _beacon()
        try:
            initial = S.claim_controller_startup(root, pid=first.pid, environ={})
            duplicate = S.claim_controller_startup(root, pid=second.pid, environ={})
            assert_true("repo default names the first controller", initial["session"]["label"] == root.name)
            assert_true("first default-named controller claims cleanly", initial.get("claimed") is True)
            assert_true("duplicate repo name is not reported as owned", duplicate.get("claimed") is False)
            assert_true("duplicate cause is explicit", duplicate.get("reason") == "controller_label_conflict")
            resolved = S.live_session(root, label=root.name, pid=first.pid)
            assert_true("pid-scoped lookup still exposes label conflict", resolved is not None)
            assert_true("both same-label beacons are counted", resolved.get("conflicting_beacons") == 2)
        finally:
            for proc in (first, second):
                proc.terminate()
                proc.wait(timeout=10)


def test_controller_startup_cli_defaults_to_repo_name() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "repo-default"
        root.mkdir()
        proc = _beacon()
        try:
            env = os.environ.copy()
            env.pop("GOALFLIGHT_CONTROLLER_LABEL", None)
            env["GOALFLIGHT_CONTROLLER_PID"] = str(proc.pid)
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "goalflight_session_status.py"),
                    "--project-root",
                    str(root),
                    "--controller-startup",
                ],
                env=env,
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
            payload = json.loads(completed.stdout)
            assert_true("startup hook is non-blocking", completed.returncode == 0)
            assert_true("startup hook claims without a label declaration", payload.get("claimed") is True)
            assert_true("startup hook defaults to the repo name", payload["session"]["label"] == root.name)
        finally:
            proc.terminate()
            proc.wait(timeout=10)


def test_worktree_default_matches_main_repo_name() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "shared-repo"
        worktree = root / ".claude" / "worktrees" / "feature"
        worktree.mkdir(parents=True)
        with patch.object(S, "_git_project_root", return_value=root):
            main_label = S.resolve_controller_label(environ={S.CONTROLLER_PID_ENV: "123"})
        with patch.object(S, "_git_project_root", return_value=worktree):
            worktree_label = S.resolve_controller_label(environ={S.CONTROLLER_PID_ENV: "123"})
        assert_true("main checkout uses the repo name", main_label == root.name)
        assert_true("managed worktree keeps the same name", worktree_label == root.name)


def test_controller_startup_cli_explicit_name_overrides_repo_default() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "repo-default"
        root.mkdir()
        proc = _beacon()
        try:
            env = os.environ.copy()
            env.update(
                {
                    "GOALFLIGHT_CONTROLLER_LABEL": "battery-main",
                    "GOALFLIGHT_CONTROLLER_PID": str(proc.pid),
                }
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "goalflight_session_status.py"),
                    "--project-root",
                    str(root),
                    "--controller-startup",
                ],
                env=env,
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
            payload = json.loads(completed.stdout)
            assert_true("startup hook is non-blocking", completed.returncode == 0)
            assert_true("startup hook claims the controller", payload.get("claimed") is True)
            assert_true("declared name overrides the repo default", payload["session"]["label"] == "battery-main")
            assert_true("startup hook stores the declared pid", payload["session"]["pid"] == proc.pid)
        finally:
            proc.terminate()
            proc.wait(timeout=10)


def test_dead_repo_default_does_not_block_reclaim() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "shared-repo"
        root.mkdir()
        stale = _beacon()
        replacement = None
        try:
            first = S.claim_controller_startup(root, pid=stale.pid, environ={})
            assert_true("initial repo-default claim succeeds", first.get("claimed") is True)
            stale.terminate()
            stale.wait(timeout=10)
            replacement = _beacon()
            second = S.claim_controller_startup(root, pid=replacement.pid, environ={})
            assert_true("dead default holder does not block a replacement", second.get("claimed") is True)
            resolved = S.live_session(root, label=root.name, pid=replacement.pid)
            assert_true("replacement owns the repo name", resolved is not None)
            assert_true("dead holder is not a conflict", "conflicting_beacons" not in resolved)
        finally:
            if stale.poll() is None:
                stale.terminate()
                stale.wait(timeout=10)
            if replacement is not None and replacement.poll() is None:
                replacement.terminate()
                replacement.wait(timeout=10)


def test_unresolvable_project_root_stays_unnamed() -> None:
    with patch.object(S.subprocess, "run", side_effect=AssertionError("root probe failed")):
        label = S.resolve_controller_label(environ={S.CONTROLLER_PID_ENV: "123"})
    assert_true("no project root does not become a cwd basename", label is None)


def test_implicit_default_requires_controller_pid() -> None:
    with patch.object(S, "_git_project_root", return_value=Path("/measured/repo")) as probe:
        label = S.resolve_controller_label(environ={})
    assert_true("an undeclared listener does not acquire a repo label", label is None)
    assert_true("undeclared lookup does not probe the repo", not probe.called)


def test_dead_beacon_resolves_to_none_not_a_stale_id() -> None:
    """A dead controller must not keep answering for its workers.

    Returning the last-known id here would be the defect this project keeps
    hitting: a field asserting a state nobody measured. Ownership would survive
    the owner.
    """
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        proc = _beacon()
        S.claim_session(root, pid=proc.pid)
        assert_true("live while the beacon runs", S.live_session(root) is not None)
        proc.terminate()
        proc.wait(timeout=10)
        assert_true("None once the beacon is gone", S.live_session(root) is None)


def test_no_beacon_is_none_rather_than_an_invented_owner() -> None:
    """None means 'nobody has claimed this project', not 'idle'."""
    with tempfile.TemporaryDirectory() as td:
        assert_true("unclaimed project has no session", S.live_session(Path(td)) is None)


def test_second_live_beacon_is_reported_not_silently_arbitrated() -> None:
    """Two controllers in one project is a takeover or a stray. Say so.

    Picking one without a word is how an operator ends up debugging why half
    their workers answer to a session they never started.
    """
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        first, second = _beacon(), _beacon()
        try:
            S.claim_session(root, pid=first.pid, label="battery-main")
            S.claim_session(root, pid=second.pid, label="battery-main")
            live = S.live_session(root, label="battery-main")
            assert_true("a winner is still chosen", live is not None)
            assert_true("the collision is surfaced", live.get("conflicting_beacons") == 2)
        finally:
            for proc in (first, second):
                proc.terminate()
                proc.wait(timeout=10)


def test_environment_declaration_selects_its_own_live_controller() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        first, second = _beacon(), _beacon()
        try:
            S.claim_session(
                root,
                pid=second.pid,
                session_id="battery-bugs-session",
                label="battery-bugs",
            )
            first_claim = S.claim_session(
                root,
                pid=first.pid,
                session_id="battery-main-session",
                label="battery-main",
            )
            with patch.dict(
                S.os.environ,
                {
                    "GOALFLIGHT_CONTROLLER_LABEL": "battery-main",
                    "GOALFLIGHT_CONTROLLER_PID": str(first.pid),
                },
            ):
                resolved = S.live_session(root)
            assert_true("listener resolves its declared controller", resolved is not None)
            assert_true("other named controller is not captured", resolved["id"] == first_claim["id"])
        finally:
            for proc in (first, second):
                proc.terminate()
                proc.wait(timeout=10)


def test_live_same_label_beacon_wins_over_newer_stale_record() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        live_proc = _beacon()
        stale_proc = _beacon()
        try:
            with patch.object(
                S,
                "_now_iso",
                side_effect=["2026-08-03T10:00:00Z", "2026-08-03T11:00:00Z"],
            ):
                live = S.claim_session(
                    root,
                    pid=live_proc.pid,
                    session_id="live-controller",
                    label="battery-main",
                )
                S.claim_session(
                    root,
                    pid=stale_proc.pid,
                    session_id="stale-controller",
                    label="battery-main",
                )
            stale_proc.terminate()
            stale_proc.wait(timeout=10)
            resolved = S.live_session(root, label="battery-main")
            assert_true("live same-label record survives stale peer", resolved is not None)
            assert_true("stale record cannot displace live", resolved["id"] == live["id"])
            assert_true("dead record is not a conflict", "conflicting_beacons" not in resolved)
        finally:
            live_proc.terminate()
            live_proc.wait(timeout=10)
            if stale_proc.poll() is None:
                stale_proc.terminate()
                stale_proc.wait(timeout=10)


def test_beacon_slots_do_not_disturb_per_terminal_sessions() -> None:
    """ensure_session keeps its existing meaning alongside beacons."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        proc = _beacon()
        try:
            terminal = S.ensure_session(root)
            S.claim_session(root, pid=proc.pid)
            again = S.ensure_session(root)
            assert_true("per-terminal record survives a claim", terminal["id"] == again["id"])
            assert_true("and is not mistaken for the beacon",
                        S.live_session(root)["id"] != terminal["id"])
        finally:
            proc.terminate()
            proc.wait(timeout=10)


def test_release_drops_the_beacon() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        proc = _beacon()
        try:
            S.claim_session(root, pid=proc.pid)
            assert_true("release reports removal", S.release_session(root, pid=proc.pid) is True)
            assert_true("session is gone", S.live_session(root) is None)
            assert_true("releasing twice is False", S.release_session(root, pid=proc.pid) is False)
        finally:
            proc.terminate()
            proc.wait(timeout=10)


def main() -> None:
    test_identity_is_stable_across_separate_resolutions()
    test_claim_is_idempotent_for_the_same_beacon()
    test_reused_pid_replaces_the_stale_process_generation()
    test_startup_liveness_error_is_fail_open()
    test_unavailable_process_generation_preserves_existing_beacon()
    test_linux_identity_requires_measured_boot_generation()
    test_startup_names_legacy_beacon_once_and_rejects_relabel()
    test_repo_default_duplicate_surfaces_conflict()
    test_controller_startup_cli_defaults_to_repo_name()
    test_worktree_default_matches_main_repo_name()
    test_controller_startup_cli_explicit_name_overrides_repo_default()
    test_dead_repo_default_does_not_block_reclaim()
    test_unresolvable_project_root_stays_unnamed()
    test_implicit_default_requires_controller_pid()
    test_dead_beacon_resolves_to_none_not_a_stale_id()
    test_no_beacon_is_none_rather_than_an_invented_owner()
    test_second_live_beacon_is_reported_not_silently_arbitrated()
    test_environment_declaration_selects_its_own_live_controller()
    test_live_same_label_beacon_wins_over_newer_stale_record()
    test_beacon_slots_do_not_disturb_per_terminal_sessions()
    test_release_drops_the_beacon()
    print("OK: session beacon tests pass")


if __name__ == "__main__":
    main()
