"""t-272 + t-273: terse controller-startup JSON, and arming over pending mail.

t-272: ``--controller-startup`` is a machine surface. Before e333cc5 it was
JSON-only. The floor work put the numbered human list on that surface three
times (stderr + commands[] + hint), repeating a long project path twelve
times. The numbered list stays on listen-exit, where it is pedagogical.
Startup JSON carries one command template and no rendered prose.

t-273 design (argued, not picked by habit):

(a) Floor hint says "drain first" when mail is pending. Rejected as the
    primary fix: it documents the footgun and still lets a caller arm into
    a zero-depth pool.

(b) ``--report-pending`` reports on the first listener only; the rest keep
    waiting. Chosen. One tracked task per slot, no new drain-then-arm
    ordering rule. Pending mail becomes one report, not four exits.

    Two arms racing: an atomic per-lease claim fixes one immutable high-water.
    Exactly one live owner emits, then fsyncs the provisional ``reported``
    phase. Losers discard their later local high-water and adopt the claim
    boundary, so mail arriving between peeks still rings. If the owner dies
    before cursor acknowledgement, exactly one peer takes over that boundary.
    Depth stays at target until the genuinely new event wins one ring.

(c) An arm-to-depth helper that drains once then forks N waiters. Rejected:
    one tracked parent of N children is the untracked-stray shape the
    floor exists to prevent, and a silent cursor advance would mark mail
    read before anyone processed it.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from machine_isolation import AMBIENT_IDENTITY_ENV, isolated_machine_env, wait_until

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import goalflight_journal as journal  # noqa: E402
import goalflight_session_status as sessions  # noqa: E402
import goalflight_wake as wake  # noqa: E402

# Frozen listen-exit numbered list. Changing this string is the regression
# the operator asked us not to trade for the terse-startup fix.
LISTEN_EXIT_HINT_SNAPSHOT = (
    "listener floor: work in flight and live=0/4 — 4 slots missing; "
    "issue each as its own tracked background task; "
    "a shell `&` loop is one untracked call and those wakes reach nobody:\n"
    "1. CMD\n"
    "2. CMD\n"
    "3. CMD\n"
    "4. CMD"
)
LISTEN_EXIT_THIN_HINT_SNAPSHOT = (
    "listener pool n=1/4 — 3 slots missing; "
    "issue each as its own tracked background task; "
    "a shell `&` loop is one untracked call and those wakes reach nobody:\n"
    "1. CMD\n"
    "2. CMD\n"
    "3. CMD"
)

# t-272 excludes rendered prose and repeated path-bearing commands from this
# every-startup machine surface.  Compact wake state is different: the later
# persistent-listener contract needs ``wake_mode`` to choose pool versus
# stream/backup/watchdog arming, and ``reason`` distinguishes healthy, missing,
# stale, faulted, and unavailable coverage. ``supervisor`` is a compact enum
# (running/absent/unknown) with no extra path. Keep this an exact set so that
# operational additions remain deliberate rather than turning into key sprawl.
LISTENER_DEPTH_KEYS = {
    "live",
    "target",
    "missing",
    "work_in_flight",
    "command",
    "separate_tracked_tasks",
    "wake_mode",
    "reason",
    "supervisor",
}


@pytest.fixture()
def isolated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[Path, dict[str, str]]:
    env = isolated_machine_env(tmp_path)
    env["GOALFLIGHT_TEST_MODE"] = "1"
    env["GOALFLIGHT_PROCESS_ROLE"] = "controller"
    # Hints name the ADVERTISED install, not the copy that generated them. Pin
    # the advertised root to the code under test so these expectations do not
    # depend on whether the host has ~/.goal-flight/skill installed.
    env["GOALFLIGHT_ROOT"] = str(SCRIPTS.parent)
    for key in AMBIENT_IDENTITY_ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("GOALFLIGHT_WAKE_LEDGER", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    project = tmp_path / "project"
    project.mkdir()
    return project, {**os.environ, **env}


def _claim(project: Path, label: str = "terse-ctl") -> journal.LeaseIdentity:
    authority = journal.open_or_create_journal(project)
    claimed = authority.claim_or_renew_lease(
        label, principal={"principal_id": f"{label}-principal"}
    )
    assert claimed.committed and claimed.value is not None
    return claimed.value


def _env_with_empty_process_listing(
    env: dict[str, str],
    directory: Path,
) -> dict[str, str]:
    shim_dir = directory / "empty-process-listing"
    shim_dir.mkdir(exist_ok=True)
    ps_shim = shim_dir / "ps"
    ps_shim.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    ps_shim.chmod(0o755)
    return {**env, "PATH": f"{shim_dir}:{env.get('PATH', '')}"}


def _post(env: dict[str, str], project: Path, label: str, text: str) -> None:
    subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "goalflight_messages.py"),
            "post",
            "--to-controller",
            label,
            "--dispatch-id",
            "terse-mail",
            "--type",
            "controller-notice",
            "--text",
            text,
        ],
        env=env,
        cwd=project,
        check=True,
        capture_output=True,
    )


def _listen_cmd(
    project: Path,
    *,
    label: str,
    nonce: str,
    timeout_s: float = 20,
    json_out: bool = False,
) -> list[str]:
    cmd = [
        sys.executable,
        str(SCRIPTS / "goalflight_messages.py"),
        "listen",
        "--project-root",
        str(project),
        "--controller-label",
        label,
        "--lease-nonce",
        nonce,
        "--poll-secs",
        "0.05",
        "--timeout-s",
        str(timeout_s),
        "--report-pending",
    ]
    if json_out:
        cmd.append("--json")
    return cmd


def _wait_live(project: Path, label: str, count: int, *, timeout_s: float = 60) -> None:
    def _at_count() -> bool:
        waiters = (
            wake.live_waiters(project, controller_label=label, kinds={"listener"}) or []
        )
        return len(waiters) == count

    wait_until(
        _at_count,
        timeout_s=timeout_s,
        interval_s=0.02,
        message=f"live listeners for {label} n={count}",
    )


def test_listen_exit_numbered_hint_is_unchanged() -> None:
    assert (
        wake.listener_floor_hint(
            0,
            4,
            "CMD",
            work_in_flight=True,
            supervisor=wake.SUPERVISOR_ABSENT,
        )
        == LISTEN_EXIT_HINT_SNAPSHOT
    )
    assert (
        wake.listener_floor_hint(
            1,
            4,
            "CMD",
            work_in_flight=True,
            supervisor=wake.SUPERVISOR_ABSENT,
        )
        == LISTEN_EXIT_THIN_HINT_SNAPSHOT
    )
    assert wake.listener_floor_hint(0, 4, "CMD", work_in_flight=False) == ""
    assert wake.listener_floor_hint(4, 4, "CMD", work_in_flight=True) == ""


def test_controller_startup_stdout_is_json_without_preprocessing(
    isolated: tuple[Path, dict[str, str]],
) -> None:
    project, env = isolated
    env = _env_with_empty_process_listing(env, project.parent)
    authority = journal.open_or_create_journal(project)
    assert authority.prepare_attempt("terse-startup-work").committed
    host = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        proc = subprocess.Popen(
            [
                sys.executable,
                str(SCRIPTS / "goalflight_session_status.py"),
                "--project-root",
                str(project),
                "--controller-startup",
                "--session-pid",
                str(host.pid),
                "--session-label",
                "terse-ctl",
            ],
            cwd=project,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert proc.stdout is not None
        # Mechanical t-272 proof: no strip, no line skip, no 2>&1.
        payload = json.load(proc.stdout)
        stderr = proc.stderr.read() if proc.stderr is not None else ""
        assert proc.wait(timeout=8) == 0
    finally:
        host.kill()
        host.wait()

    assert payload["claimed"] is True
    depth = payload["listener_depth"]
    assert set(depth) == LISTENER_DEPTH_KEYS
    assert isinstance(depth["command"], str) and depth["command"]
    assert depth["command"].count(str(project.resolve())) == 1
    stdout_dump = json.dumps(payload)
    assert stdout_dump.count(str(project.resolve())) == 1
    assert "listener floor:" not in stdout_dump
    assert "listener floor:" not in stderr
    assert "1. " not in stderr


def test_controller_startup_json_is_terse_with_work_in_flight(
    isolated: tuple[Path, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, _env = isolated
    authority = journal.open_or_create_journal(project)
    assert authority.prepare_attempt("terse-claim-work").committed
    monkeypatch.setattr(
        sessions,
        "_controller_process_identity",
        lambda pid: {"pid": pid, "start_token": "terse-claim-token"},
    )
    monkeypatch.setattr(wake, "_process_listing", lambda: [])
    result = sessions.claim_controller_startup(
        project, pid=81001, label="terse-ctl", role="controller"
    )
    assert result["claimed"] is True
    depth = result["listener_depth"]
    assert set(depth) == LISTENER_DEPTH_KEYS
    assert depth["work_in_flight"] is True
    assert depth["live"] == 0
    assert depth["missing"] == wake.DEFAULT_LISTENER_SLOTS
    assert depth["separate_tracked_tasks"] is True
    encoded = json.dumps(result)
    assert encoded.count(str(project.resolve())) == 1


def test_listen_exit_still_prints_the_numbered_hint(
    isolated: tuple[Path, dict[str, str]],
) -> None:
    project, env = isolated
    authority = journal.open_or_create_journal(project)
    lease = _claim(project)
    assert authority.prepare_attempt("terse-exit-work").committed
    with wake.register_lease_holder(
        project, controller_label="terse-ctl", lease_nonce=lease.nonce
    ):
        cmd = _listen_cmd(project, label="terse-ctl", nonce=lease.nonce)
        proc = subprocess.Popen(
            cmd,
            cwd=project,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            _wait_live(project, "terse-ctl", 1)
            _post(env, project, "terse-ctl", "ring me")
            stdout, stderr = proc.communicate(timeout=30)
            assert proc.returncode == 0, stderr
            assert stdout.splitlines()[0] == (
                "[controller-notice] terse-mail seq=1 — ring me"
            )
            assert stdout.splitlines()[1].startswith("advance: ")
            expected = wake.listener_floor_hint(
                0,
                wake.DEFAULT_LISTENER_SLOTS,
                shlex.join(cmd),
                work_in_flight=True,
                supervisor=wake.SUPERVISOR_ABSENT,
            )
            assert expected
            assert expected in stderr
            assert "1. " in expected
            assert f"{wake.DEFAULT_LISTENER_SLOTS}. " in expected
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()


def test_claim_pending_report_first_writer_wins(
    isolated: tuple[Path, dict[str, str]],
) -> None:
    project, _env = isolated
    assert (
        wake.claim_pending_report(
            project,
            controller_label="terse-ctl",
            lease_nonce="nonce-a",
            positions={"arm-backlog": 2},
        )
        is True
    )
    assert wake.pending_report_high_water(
        project, controller_label="terse-ctl", lease_nonce="nonce-a"
    ) == {"arm-backlog": 2}
    claimed = wake.pending_report_state(
        project, controller_label="terse-ctl", lease_nonce="nonce-a"
    )
    assert claimed is not None
    assert claimed.phase == "claimed"
    assert claimed.claim_token
    assert (
        wake.claim_pending_report(
            project,
            controller_label="terse-ctl",
            lease_nonce="nonce-a",
            positions={"arm-backlog": 99},
        )
        is False
    )
    assert wake.pending_report_high_water(
        project, controller_label="terse-ctl", lease_nonce="nonce-a"
    ) == {"arm-backlog": 2}
    assert wake.mark_pending_report_reported(
        project,
        controller_label="terse-ctl",
        lease_nonce="nonce-a",
        claim_token=claimed.claim_token,
    )
    reported = wake.pending_report_state(
        project, controller_label="terse-ctl", lease_nonce="nonce-a"
    )
    assert reported is not None
    assert reported.phase == "reported"
    assert not wake.acknowledge_pending_report(
        project,
        controller_label="terse-ctl",
        lease_nonce="nonce-a",
        positions={"arm-backlog": 1},
    )
    assert wake.pending_report_state(
        project, controller_label="terse-ctl", lease_nonce="nonce-a"
    ) == reported
    assert wake.acknowledge_pending_report(
        project,
        controller_label="terse-ctl",
        lease_nonce="nonce-a",
        positions={"arm-backlog": 2},
    )
    acknowledged = wake.pending_report_state(
        project, controller_label="terse-ctl", lease_nonce="nonce-a"
    )
    assert acknowledged is not None
    assert acknowledged.phase == "acknowledged"
    assert wake.mark_pending_report_reported(
        project,
        controller_label="terse-ctl",
        lease_nonce="nonce-a",
        claim_token=claimed.claim_token,
    )
    assert wake.pending_report_state(
        project, controller_label="terse-ctl", lease_nonce="nonce-a"
    ) == acknowledged
    assert (
        wake.claim_pending_report(
            project, controller_label="terse-ctl", lease_nonce="nonce-b"
        )
        is True
    )
    assert (
        wake.pending_report_high_water(
            project, controller_label="terse-ctl", lease_nonce="nonce-b"
        )
        == {}
    )


def test_acknowledge_pending_report_shrinks_to_unconsumed_remainder(
    isolated: tuple[Path, dict[str, str]],
) -> None:
    project, _env = isolated
    snapshot_a = "aa" * 32
    snapshot_b = "bb" * 32
    claimed = wake.acquire_pending_report(
        project,
        controller_label="terse-ctl",
        lease_nonce="nonce-subset",
        positions={"stream-a": 1, "stream-b": 1},
        cursor_version=0,
        stream_snapshots={"stream-a": snapshot_a, "stream-b": snapshot_b},
    )
    assert claimed is not None
    assert wake.mark_pending_report_reported(
        project,
        controller_label="terse-ctl",
        lease_nonce="nonce-subset",
        claim_token=claimed.claim_token,
    )
    assert not wake.acknowledge_pending_report(
        project,
        controller_label="terse-ctl",
        lease_nonce="nonce-subset",
        positions={"stream-a": 1},
    )
    reduced = wake.pending_report_state(
        project, controller_label="terse-ctl", lease_nonce="nonce-subset"
    )
    assert reduced is not None
    assert reduced.phase == "reported"
    assert reduced.positions == {"stream-b": 1}
    assert reduced.stream_snapshots == {"stream-b": snapshot_b}
    assert wake.acknowledge_pending_report(
        project,
        controller_label="terse-ctl",
        lease_nonce="nonce-subset",
        positions={"stream-a": 1, "stream-b": 1},
    )
    acknowledged = wake.pending_report_state(
        project, controller_label="terse-ctl", lease_nonce="nonce-subset"
    )
    assert acknowledged is not None
    assert acknowledged.phase == "acknowledged"
    assert acknowledged.positions == {"stream-b": 1}


def test_partial_pending_report_state_fails_closed(
    isolated: tuple[Path, dict[str, str]],
) -> None:
    project, _env = isolated
    path = wake._pending_report_path(
        project,
        controller_label="terse-ctl",
        lease_nonce="partial-state",
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"schema":"goalflight.pending-report.v3",', encoding="utf-8")

    with pytest.raises(wake.PendingReportStateError, match="incomplete"):
        wake.pending_report_high_water(
            project,
            controller_label="terse-ctl",
            lease_nonce="partial-state",
        )
    with pytest.raises(wake.PendingReportStateError, match="incomplete"):
        wake.acquire_pending_report(
            project,
            controller_label="terse-ctl",
            lease_nonce="partial-state",
            positions={"newer-local-snapshot": 99},
        )
    assert path.read_text(encoding="utf-8").endswith(",")


def test_corrupt_pending_report_quarantine_is_bounded(
    isolated: tuple[Path, dict[str, str]],
) -> None:
    project, _env = isolated
    path = wake._pending_report_path(
        project,
        controller_label="terse-ctl",
        lease_nonce="bounded-quarantine",
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(wake.MAX_PENDING_REPORT_QUARANTINES + 5):
        path.write_text('{"schema":"goalflight.pending-report.v3",', encoding="utf-8")
        assert (
            wake.recover_pending_report_state(
                project,
                controller_label="terse-ctl",
                lease_nonce="bounded-quarantine",
            )
            is None
        )
        assert not path.exists()
    quarantines = list(path.parent.glob(f".{path.name}.*.corrupt"))
    assert len(quarantines) == wake.MAX_PENDING_REPORT_QUARANTINES


def test_listen_recovers_corrupt_pending_report_and_stays_armed(
    isolated: tuple[Path, dict[str, str]],
) -> None:
    project, env = isolated
    lease = _claim(project)
    _post(env, project, lease.label, "recover corrupt listen state")
    path = wake._pending_report_path(
        project,
        controller_label=lease.label,
        lease_nonce=lease.nonce,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"schema":"goalflight.pending-report.v3",', encoding="utf-8")

    with wake.register_lease_holder(
        project, controller_label=lease.label, lease_nonce=lease.nonce
    ):
        proc = subprocess.Popen(
            _listen_cmd(
                project,
                label=lease.label,
                nonce=lease.nonce,
                timeout_s=10,
                json_out=True,
            ),
            cwd=project,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            _wait_live(project, lease.label, 1)
            assert proc.poll() is None
            assert proc.stdout is not None
            report = json.loads(proc.stdout.readline())
            assert report["kind"] == "pending-at-arm"
            assert proc.poll() is None
            coverage = journal.Journal(project).active_coverage(lease.label)
            assert coverage is not None
            assert coverage["state"] == "ARMED"
            assert list(path.parent.glob(f".{path.name}.*.corrupt"))
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)


def test_ambiguous_v2_report_cannot_suppress_v3_delivery(
    isolated: tuple[Path, dict[str, str]],
) -> None:
    project, _env = isolated
    v3_path = wake._pending_report_path(
        project,
        controller_label="terse-ctl",
        lease_nonce="upgrade-state",
    )
    v2_path = v3_path.with_name(
        v3_path.name.replace("pending-report-v3", "pending-report-v2")
    )
    v2_path.parent.mkdir(parents=True, exist_ok=True)
    v2_path.write_text(
        '{"schema":"goalflight.pending-report.v2","phase":"reported",'
        '"positions":{"old-boundary":77}}\n',
        encoding="utf-8",
    )

    # v2 recorded only a local flush, so its high-water is deliberately not trusted.
    assert wake.pending_report_high_water(
        project,
        controller_label="terse-ctl",
        lease_nonce="upgrade-state",
    ) is None
    assert wake.claim_pending_report(
        project,
        controller_label="terse-ctl",
        lease_nonce="upgrade-state",
        positions={"current-backlog": 1},
    )
    assert wake.pending_report_high_water(
        project,
        controller_label="terse-ctl",
        lease_nonce="upgrade-state",
    ) == {"current-backlog": 1}


def test_arming_over_pending_mail_reaches_target_depth(
    isolated: tuple[Path, dict[str, str]],
) -> None:
    """The defect: pending mail used to satisfy every arm, leaving 0/N.

    Assert on the resulting depth, not on the launches.
    """
    project, env = isolated
    lease = _claim(project)
    _post(env, project, "terse-ctl", "already waiting")
    target = wake.DEFAULT_LISTENER_SLOTS
    procs: list[subprocess.Popen[str]] = []
    out_paths = [project.parent / f"arm-{index}.stdout" for index in range(target)]
    err_paths = [project.parent / f"arm-{index}.stderr" for index in range(target)]
    with wake.register_lease_holder(
        project, controller_label="terse-ctl", lease_nonce=lease.nonce
    ):
        try:
            for index in range(target):
                out_handle = out_paths[index].open("w", encoding="utf-8")
                err_handle = err_paths[index].open("w", encoding="utf-8")
                procs.append(
                    subprocess.Popen(
                        _listen_cmd(
                            project,
                            label="terse-ctl",
                            nonce=lease.nonce,
                            timeout_s=60,
                            json_out=True,
                        ),
                        cwd=project,
                        env=env,
                        stdout=out_handle,
                        stderr=err_handle,
                    )
                )
                out_handle.close()
                err_handle.close()
            _wait_live(project, "terse-ctl", target)
            live = (
                wake.live_waiters(
                    project, controller_label="terse-ctl", kinds={"listener"}
                )
                or []
            )
            assert len(live) == target
            assert all(proc.poll() is None for proc in procs)

            def _count_reports() -> int:
                reports = 0
                for path in out_paths:
                    text = path.read_text(encoding="utf-8")
                    if not text.strip():
                        continue
                    for line in text.splitlines():
                        if not line.strip():
                            continue
                        payload = json.loads(line)
                        if payload.get("kind") == "pending-at-arm":
                            reports += 1
                        else:
                            raise AssertionError(
                                f"non-report output while pool should stay armed: {payload}"
                            )
                return reports

            wait_until(
                lambda: _count_reports() == 1,
                timeout_s=30,
                message="exactly one pending-at-arm report at target depth",
            )
            assert _count_reports() == 1
            assert (
                len(
                    wake.live_waiters(
                        project, controller_label="terse-ctl", kinds={"listener"}
                    )
                    or []
                )
                == target
            )
        finally:
            for proc in procs:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()


def test_two_racing_arms_yield_one_report_and_stay_live(
    isolated: tuple[Path, dict[str, str]],
) -> None:
    project, env = isolated
    lease = _claim(project)
    _post(env, project, "terse-ctl", "race backlog")
    procs: list[subprocess.Popen[str]] = []
    out_paths = [project.parent / "race-0.stdout", project.parent / "race-1.stdout"]
    with wake.register_lease_holder(
        project, controller_label="terse-ctl", lease_nonce=lease.nonce
    ):
        try:
            for path in out_paths:
                handle = path.open("w", encoding="utf-8")
                procs.append(
                    subprocess.Popen(
                        _listen_cmd(
                            project,
                            label="terse-ctl",
                            nonce=lease.nonce,
                            json_out=True,
                        ),
                        cwd=project,
                        env=env,
                        stdout=handle,
                        stderr=subprocess.DEVNULL,
                    )
                )
                handle.close()
            _wait_live(project, "terse-ctl", 2)
            assert len(wake.live_waiters(project, controller_label="terse-ctl") or []) == 2
            assert all(proc.poll() is None for proc in procs)

            def _count_reports() -> int:
                reports = 0
                for path in out_paths:
                    for line in path.read_text(encoding="utf-8").splitlines():
                        if line.strip() and json.loads(line).get("kind") == "pending-at-arm":
                            reports += 1
                return reports

            wait_until(
                lambda: _count_reports() >= 1,
                timeout_s=15,
                message="exactly one pending-at-arm report from racing arms",
            )
            assert _count_reports() == 1
            assert len(wake.live_waiters(project, controller_label="terse-ctl") or []) == 2
        finally:
            for proc in procs:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
