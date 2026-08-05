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
import time
from datetime import datetime, timedelta, timezone
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


def test_same_session_calling_shell_is_warned_not_refused() -> None:
    """POSIX session membership is suspicious, not proof of doomed lifetime."""
    if os.name == "nt":
        return
    with tempfile.TemporaryDirectory() as td:
        env = os.environ.copy()
        env.pop(S.CONTROLLER_PID_ENV, None)
        completed = subprocess.run(
            [
                "/bin/sh",
                "-c",
                (
                    "GOALFLIGHT_CONTROLLER_PID=$$ GOALFLIGHT_CONTROLLER_LABEL=doomed "
                    '"$1" "$2" --project-root "$3" --controller-startup; '
                    "status=$?; :; exit $status"
                ),
                "sh",
                sys.executable,
                str(ROOT / "scripts" / "goalflight_session_status.py"),
                td,
            ],
            env=env,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        payload = json.loads(completed.stdout)
        assert_true("startup remains fail-open", completed.returncode == 0)
        assert_true("the live parent claim is accepted", payload.get("claimed") is True)
        assert_true(
            "the lifetime uncertainty is explicit",
            payload.get("warnings", [{}])[0].get("reason")
            == "controller_pid_lifetime_suspicious",
        )


def test_ancestry_resolution_uses_surviving_host() -> None:
    """Resolve through a transient shell to a real, still-running host process."""
    if os.name == "nt":
        return
    launcher_source = r'''
import json
import subprocess
import sys
import time
from pathlib import Path

script, root, result_path = sys.argv[1:]
shell = subprocess.Popen(
    [
        "/bin/sh",
        "-c",
        '"$1" "$2" --project-root "$3" --controller-startup '
        '--controller-pid-from-ancestry --session-label ancestry-host; '
        'status=$?; :; exit $status',
        "sh",
        sys.executable,
        script,
        root,
    ],
    start_new_session=True,
    text=True,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
)
stdout, stderr = shell.communicate(timeout=10)
Path(result_path).write_text(
    json.dumps(
        {
            "claim": json.loads(stdout),
            "shell_pid": shell.pid,
            "shell_returncode": shell.returncode,
            "stderr": stderr,
        }
    ),
    encoding="utf-8",
)
time.sleep(120)
'''
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        result_path = root / "ancestry-result.json"
        launcher = subprocess.Popen(
            [
                sys.executable,
                "-c",
                launcher_source,
                str(ROOT / "scripts" / "goalflight_session_status.py"),
                str(root),
                str(result_path),
            ],
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 10
            while not result_path.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert_true("claiming helper returned", result_path.exists())
            payload = json.loads(result_path.read_text(encoding="utf-8"))
            claim = payload["claim"]
            assert_true("ancestry claim succeeded", claim.get("claimed") is True)
            assert_true("transient shell exited", payload["shell_returncode"] == 0)
            assert_true("transient shell is gone", not S._pid_alive(payload["shell_pid"]))
            assert_true("durable launcher still runs", launcher.poll() is None)
            assert_true(
                "the exact durable host was selected",
                claim["session"]["pid"] == launcher.pid,
            )
            live = S.live_session(root, label="ancestry-host", pid=launcher.pid)
            assert_true("beacon remains live after claim helper exits", live is not None)
        finally:
            launcher.terminate()
            launcher.wait(timeout=10)


def test_ancestry_stopping_rule_excludes_shell_and_init() -> None:
    ancestry = (
        {"pid": 44, "ppid": 33, "session_id": 33, "start_token": "helper"},
        {"pid": 33, "ppid": 22, "session_id": 33, "start_token": "shell"},
        {"pid": 22, "ppid": 1, "session_id": 22, "start_token": "host"},
        {"pid": 1, "ppid": 0, "session_id": 1, "start_token": "init"},
    )
    selected = S._select_durable_controller_ancestor(ancestry)
    assert_true("a durable ancestor is selected", selected is not None)
    assert_true("stop at the host session leader", selected["pid"] == 22)


def test_unrelated_live_launcher_pid_still_claims() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        launcher = _beacon()
        try:
            result = S.claim_controller_startup(
                root,
                pid=launcher.pid,
                label="unrelated-launcher",
            )
            assert_true("unrelated live launcher remains valid", result.get("claimed") is True)
            assert_true("launcher pid is preserved", result["session"]["pid"] == launcher.pid)
        finally:
            launcher.terminate()
            launcher.wait(timeout=10)


def test_dead_declared_pid_is_still_refused() -> None:
    with tempfile.TemporaryDirectory() as td:
        dead = _beacon()
        dead.terminate()
        dead.wait(timeout=10)
        result = S.claim_controller_startup(
            Path(td),
            pid=dead.pid,
            label="dead-launcher",
        )
        assert_true("dead pid cannot claim", result.get("claimed") is False)
        assert_true("dead-pid reason is preserved", result.get("reason") == "controller_pid_not_live")


def test_claim_session_cli_uses_the_lifetime_gate() -> None:
    with tempfile.TemporaryDirectory() as td:
        env = os.environ.copy()
        env.pop(S.CONTROLLER_PID_ENV, None)
        completed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "goalflight_session_status.py"),
                "--project-root",
                td,
                "--claim-session",
                "--session-label",
                "one-shot",
            ],
            env=env,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        payload = json.loads(completed.stdout)
        assert_true("claim-session remains fail-open", completed.returncode == 0)
        assert_true("one-shot helper is not claimed", payload.get("claimed") is False)
        assert_true(
            "claim-session reports measured lifetime failure",
            payload.get("reason") == "controller_pid_cannot_outlive_claim",
        )


def test_selected_ancestry_start_token_cannot_be_replaced_before_claim() -> None:
    ancestry = (
        {"pid": 44, "ppid": 33, "session_id": 33, "start_token": "helper"},
        {"pid": 33, "ppid": 22, "session_id": 33, "start_token": "shell"},
        {"pid": 22, "ppid": 1, "session_id": 22, "start_token": "old"},
    )
    replacement = {"pid": 22, "start_token": "new"}
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        with (
            patch.object(S, "_controller_process_ancestry", return_value=ancestry),
            patch.object(S, "_controller_process_identity", return_value=replacement),
        ):
            startup = S.claim_controller_startup(
                root,
                label="ancestry",
                pid_from_ancestry=True,
                environ={},
            )
        assert_true("reused selected pid is not claimed", startup.get("claimed") is False)
        assert_true("generation race is explicit", startup.get("reason") == "claim_failed")

        selected_identity = {"pid": 22, "start_token": "old"}
        with patch.object(S, "_controller_process_identity", return_value=replacement):
            registered = S.register_controller(
                root,
                "register-race",
                pid=22,
                process_identity=selected_identity,
            )
        assert_true(
            "register preserves selected generation",
            registered.get("reason") == "controller_process_generation_changed",
        )

        S.register_controller(root, "join-race")
        with patch.object(S, "_controller_process_identity", return_value=replacement):
            joined = S.join_controller(
                root,
                "join-race",
                pid=22,
                process_identity=selected_identity,
                acknowledge_conflict=True,
            )
        assert_true(
            "join preserves selected generation",
            joined.get("reason") == "controller_process_generation_changed",
        )


def test_identity_capture_does_not_require_ancestry_metadata() -> None:
    if os.name == "nt":
        return
    with patch.object(S.os, "getsid", side_effect=AssertionError("ancestry probe")):
        identity = S._controller_process_identity(os.getpid())
    assert_true("ordinary identity capture remains independent", identity is not None)


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
            assert_true(
                "missing generation is reported precisely",
                failed.get("reason") == "controller_process_generation_unavailable",
            )
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
            measured_now = datetime.now(timezone.utc)
            with patch.object(
                S,
                "_now_iso",
                side_effect=[
                    (measured_now - timedelta(seconds=2)).isoformat(),
                    (measured_now - timedelta(seconds=1)).isoformat(),
                ],
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
    test_same_session_calling_shell_is_warned_not_refused()
    test_ancestry_resolution_uses_surviving_host()
    test_ancestry_stopping_rule_excludes_shell_and_init()
    test_unrelated_live_launcher_pid_still_claims()
    test_dead_declared_pid_is_still_refused()
    test_claim_session_cli_uses_the_lifetime_gate()
    test_selected_ancestry_start_token_cannot_be_replaced_before_claim()
    test_identity_capture_does_not_require_ancestry_metadata()
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
