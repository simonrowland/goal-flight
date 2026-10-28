"""Hermetic contracts for submitted remote-CI tickets and the resident admitter."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
import os
import errno
from dataclasses import replace
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import time

import pytest
import support
from support import launchctl_unavailable_reason

import goalflight_ledger
import goalflight_remote_ci as ci
import goalflight_remote_ci_node as ci_node
import goalflight_subprocess
from test_remote_ci import RemoteRunner, _git, _init_repo, _test_source, enqueue, key, start
from test_remote_ci_transport import _independent_deadline, node_env


def _waitpid_bounded(pid, *, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            reaped, status = os.waitpid(pid, os.WNOHANG)
        except InterruptedError:
            continue
        if reaped:
            return status
        time.sleep(0.01)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    reap_deadline = time.monotonic() + 5
    while time.monotonic() < reap_deadline:
        try:
            reaped, _status = os.waitpid(pid, os.WNOHANG)
        except InterruptedError:
            continue
        if reaped:
            break
        time.sleep(0.01)
    raise AssertionError(f"child {pid} did not exit within {timeout}s")


def _run_admit_loop_once(managed, monkeypatch, *, admit_once=None):
    class StopLoop(Exception):
        pass

    with monkeypatch.context() as loop_patch:
        loop_patch.setattr(
            ci_node.builtins, "_GOALFLIGHT_REMOTE_CI_AUTHORITY",
            str(managed.parent / "authority.json"), raising=False,
        )
        loop_patch.setattr(
            ci_node, "_admit_once",
            admit_once if admit_once is not None else lambda *_args, **_kwargs: [],
        )
        loop_patch.setattr(ci_node, "awake_monotonic", lambda: 1.0)

        def stop_sleep(_seconds):
            raise StopLoop

        loop_patch.setattr(ci_node.time, "sleep", stop_sleep)
        with pytest.raises(StopLoop):
            ci_node.admit_loop(managed, interval=1.0)


def _append_pin_release_event(admission, lease_id, *, source="tokenless-holder-release"):
    ci_node.audit(admission, {
        "action": "bundle-basis-pin-release", "result": "unknown",
        "lease_id": lease_id, "source": source, "at": 1.0,
    })


def _candidate_bundle(*, request_id: str) -> tuple[str, Path]:
    source, sha, temporary = _test_source()
    bundle = temporary / f"{request_id}-{time.time_ns()}.bundle"
    _git(source, "bundle", "create", str(bundle), "HEAD")
    return sha, bundle


def _submission_payload(config, request_id, *, max_seats=1, basis_commit=None, code="pass"):
    sha, bundle = _candidate_bundle(request_id=request_id)
    command = {
        "argv": [sys.executable, "-c", code],
        "env": {
            "GOALFLIGHT_REMOTE_CI_SHA": sha,
            "GOALFLIGHT_REMOTE_CI_SELF_CAP": "2",
        },
        "timeout": 20,
    }
    values = {
        "request_id": request_id,
        "arm": "candidate",
        "owner": RemoteRunner._owner(),
        "repo": config.repo,
        "max_concurrent_seats": max_seats,
        "checkout_basename": config.checkout_basename,
        "command": command,
        "candidate_bundle_path": bundle,
    }
    if basis_commit is not None:
        values["basis_commit"] = basis_commit
    return values, sha


def _submit(node, config, request_id, *, max_seats=1, basis_commit=None, code="pass"):
    values, sha = _submission_payload(
        config, request_id, max_seats=max_seats,
        basis_commit=basis_commit, code=code,
    )
    try:
        return node.call("submit", **values), sha
    finally:
        values["candidate_bundle_path"].unlink(missing_ok=True)


def _require_launchctl():
    reason = launchctl_unavailable_reason()
    if reason is not None:
        pytest.skip(reason)


def test_launchctl_probe_skips_refusal_but_fails_on_other_errors(monkeypatch):
    with monkeypatch.context() as isolated:
        isolated.delenv("GOALFLIGHT_DISPATCH_ID", raising=False)
        isolated.setattr(support, "_RECORDED_DISPATCH_ID", None)
        isolated.setattr(support, "_RECORDED_ENFORCED_PROFILE", None)
        isolated.setattr(support, "_RECORDED_SANDBOX_ERROR", None)
        isolated.setattr(
            subprocess,
            "run",
            lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "", ""),
        )
        assert launchctl_unavailable_reason() is None

        def denied(*_args, **_kwargs):
            raise PermissionError("denied")

        isolated.setattr(subprocess, "run", denied)
        assert launchctl_unavailable_reason() == "sandbox denies exec of launchctl"

        isolated.setattr(
            subprocess,
            "run",
            lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, "", ""),
        )
        with pytest.raises(AssertionError, match="without a sandbox refusal"):
            launchctl_unavailable_reason()

        isolated.setattr(
            subprocess,
            "run",
            lambda *args, **kwargs: subprocess.CompletedProcess(
                args[0], 1, "", "launchctl: Operation not permitted",
            ),
        )
        assert launchctl_unavailable_reason() == "sandbox denies launchctl submit"

        isolated.setattr(
            subprocess,
            "run",
            lambda *args, **kwargs: subprocess.CompletedProcess(
                args[0], 2, "", "unsupported launchctl operation",
            ),
        )
        with pytest.raises(AssertionError, match="without a sandbox refusal"):
            launchctl_unavailable_reason()


def test_launchctl_guard_ignores_test_dispatch_id_without_recapturing(monkeypatch):
    recorded = (
        support._RECORDED_DISPATCH_ID,
        support._RECORDED_ENFORCED_PROFILE,
        support._RECORDED_SANDBOX_ERROR,
    )
    try:
        monkeypatch.setenv("GOALFLIGHT_DISPATCH_ID", "sandboxed-dispatch")
        support._RECORDED_DISPATCH_ID = None
        support._RECORDED_ENFORCED_PROFILE = None
        support._RECORDED_SANDBOX_ERROR = None

        def unexpected_record_read(_dispatch_id):
            raise AssertionError("a test dispatch id must not be recaptured")

        monkeypatch.setattr(goalflight_ledger, "read_record", unexpected_record_read)
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, "", ""),
        )
        with pytest.raises(AssertionError, match="without a sandbox refusal"):
            launchctl_unavailable_reason()
        assert (
            support._RECORDED_DISPATCH_ID,
            support._RECORDED_ENFORCED_PROFILE,
            support._RECORDED_SANDBOX_ERROR,
        ) == (None, None, None)
    finally:
        (
            support._RECORDED_DISPATCH_ID,
            support._RECORDED_ENFORCED_PROFILE,
            support._RECORDED_SANDBOX_ERROR,
        ) = recorded
    assert (
        support._RECORDED_DISPATCH_ID,
        support._RECORDED_ENFORCED_PROFILE,
        support._RECORDED_SANDBOX_ERROR,
    ) == recorded


def test_launchctl_guard_normalizes_dispatcher_grok_and_acp_records(monkeypatch):
    records = (
        (
            "dispatcher",
            {
                "agent": "codex",
                "shape": "bash",
                "os_sandbox": {
                    "requested_profile": None,
                    "supported_profile": "workspace-write",
                    "enforced_profile": "workspace-write",
                },
            },
        ),
        (
            "grok",
            {
                "agent": "grok-code",
                "shape": "bash",
                "os_sandbox": {
                    "requested_profile": "read-only",
                    "supported_profile": "read-only",
                    "enforced_profile": None,
                },
            },
        ),
        (
            "grok-enforced",
            {
                "agent": "grok-code",
                "shape": "bash",
                "os_sandbox": {
                    "requested_profile": "read-only",
                    "supported_profile": "read-only",
                    "enforced_profile": "read-only",
                },
            },
        ),
        (
            "acp",
            {
                "agent": "grok-acp",
                "shape": "acp",
                "os_sandbox": {
                    "requested": "read-only",
                    "profile": "read-only",
                    "enabled": True,
                },
            },
        ),
    )
    recorded = (
        support._RECORDED_DISPATCH_ID,
        support._RECORDED_ENFORCED_PROFILE,
        support._RECORDED_SANDBOX_ERROR,
    )
    for name, record in records:
        try:
            with monkeypatch.context() as isolated:
                isolated.setenv("GOALFLIGHT_DISPATCH_ID", f"recorded-{name}")
                isolated.setattr(
                    goalflight_ledger,
                    "read_record",
                    lambda _dispatch_id, record=record: record,
                )

                def unexpected_probe(*_args, **_kwargs):
                    raise AssertionError(
                        "recorded sandbox should avoid the launchctl probe"
                    )

                isolated.setattr(subprocess, "run", unexpected_probe)
                support.capture_dispatch_sandbox_posture()
                profile = support._RECORDED_ENFORCED_PROFILE
                assert profile is not None, name
                with pytest.raises(
                    pytest.skip.Exception,
                    match=f"enforced {profile} sandbox prevents launchctl submit",
                ):
                    _require_launchctl()
        finally:
            (
                support._RECORDED_DISPATCH_ID,
                support._RECORDED_ENFORCED_PROFILE,
                support._RECORDED_SANDBOX_ERROR,
            ) = recorded
    assert (
        support._RECORDED_DISPATCH_ID,
        support._RECORDED_ENFORCED_PROFILE,
        support._RECORDED_SANDBOX_ERROR,
    ) == recorded


@pytest.mark.parametrize(
    "record",
    [None, {"state": "unreadable"}],
    ids=["absent", "unreadable"],
)
def test_launchctl_guard_fails_closed_without_a_usable_record(monkeypatch, record):
    recorded = (
        support._RECORDED_DISPATCH_ID,
        support._RECORDED_ENFORCED_PROFILE,
        support._RECORDED_SANDBOX_ERROR,
    )
    try:
        with monkeypatch.context() as isolated:
            isolated.setenv("GOALFLIGHT_DISPATCH_ID", "posture-record")
            isolated.setattr(
                goalflight_ledger,
                "read_record",
                lambda _dispatch_id: record,
            )

            def unexpected_probe(*_args, **_kwargs):
                raise AssertionError(
                    "unknown posture must fail before probing launchctl"
                )

            isolated.setattr(subprocess, "run", unexpected_probe)
            support.capture_dispatch_sandbox_posture()
            assert support._RECORDED_SANDBOX_ERROR is not None
            with pytest.raises(AssertionError, match="sandbox posture"):
                _require_launchctl()
    finally:
        (
            support._RECORDED_DISPATCH_ID,
            support._RECORDED_ENFORCED_PROFILE,
            support._RECORDED_SANDBOX_ERROR,
        ) = recorded


@pytest.mark.parametrize(
    "record",
    [
        {
            "agent": "grok-acp",
            "shape": "acp",
            "os_sandbox": {
                "requested": "unrecognised-profile",
                "profile": "unrecognised-profile",
                "enabled": True,
            },
        },
        {
            "agent": "codex",
            "shape": "bash",
            "os_sandbox": {
                "requested_profile": "unrecognised-profile",
                "supported_profile": "unrecognised-profile",
                "enforced_profile": "unrecognised-profile",
            },
        },
    ],
    ids=["acp", "dispatcher"],
)
def test_launchctl_guard_rejects_unknown_profile_records(monkeypatch, record):
    recorded = (
        support._RECORDED_DISPATCH_ID,
        support._RECORDED_ENFORCED_PROFILE,
        support._RECORDED_SANDBOX_ERROR,
    )
    probe_calls = []
    try:
        monkeypatch.setenv("GOALFLIGHT_DISPATCH_ID", "unknown-profile-dispatch")
        monkeypatch.setattr(
            goalflight_ledger,
            "read_record",
            lambda _dispatch_id: record,
        )

        def broken_probe(*args, **kwargs):
            probe_calls.append((args, kwargs))
            return subprocess.CompletedProcess(args[0], 1, "", "")

        monkeypatch.setattr(subprocess, "run", broken_probe)
        support.capture_dispatch_sandbox_posture()
        error = support._RECORDED_SANDBOX_ERROR
        try:
            _require_launchctl()
        except pytest.skip.Exception as exc:
            pytest.fail(f"unknown sandbox profile incorrectly skipped: {exc}")
        except AssertionError as exc:
            assert error is not None
            assert str(exc) == error
        else:
            pytest.fail("unknown sandbox profile did not fail closed")
        assert probe_calls == []
    finally:
        (
            support._RECORDED_DISPATCH_ID,
            support._RECORDED_ENFORCED_PROFILE,
            support._RECORDED_SANDBOX_ERROR,
        ) = recorded


def test_submitted_ticket_uses_configured_admission_window(node_env):
    config, _executor, _runner, node = node_env
    node.admission = replace(config.admission, max_wait_seconds=0.2)

    record, _sha = _submit(
        node, config, "configured-admission-window", max_seats=0,
    )
    request = ci_node.read_json(Path(record["run_directory"]) / "request.json")
    assert request["queue_wait_seconds"] == pytest.approx(0.2)

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        current = node.call("status", **key(record))
        completed = current.get("result")
        if isinstance(completed, dict) and completed.get("status") == "expired":
            break
        time.sleep(0.05)
    else:
        pytest.fail("resident ticket was not expired at the configured admission deadline")


def _expected_owner(record):
    return {name: record.get(name) for name in
            ("owner_host", "owner_pid", "owner_start_token", "owner_identity")}


def _wait_for_state(node, record, states, *, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = node.call("status", **key(record))
        if current["state"] in states:
            return current
        time.sleep(0.05)
    raise AssertionError(f"never reached {states}: {current}")


def _file_sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(64 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _ticket_ref(managed, repo, lease_id):
    return (managed / "repos" / repo / "mirror.git" / "refs" /
            "goalflight" / "remote-ci" / "resident-bundle-bases" / lease_id)


def _upload_request(stream):
    return {
        "box": "box-a", "request_id": "interrupted-upload", "arm": "candidate",
        "repo": "test-repo", "owner": {"owner_identity": "upload-test"},
        "command": {"argv": [sys.executable, "-c", "pass"],
                    "env": {"GOALFLIGHT_REMOTE_CI_SHA": "a" * 40}, "timeout": 20},
        "candidate_bundle_stream": True, "candidate_bundle_size": 4,
        "candidate_bundle_sha256": hashlib.sha256(b"test").hexdigest(),
        "_candidate_bundle_stream": stream,
    }


def _deployed_v4_node():
    repo = Path(__file__).resolve().parents[2]
    legacy = {"__name__": "_legacy_remote_ci_node", "__file__": "v4-node.py"}
    exec(compile(_git(repo, "show", "afc14b57:scripts/goalflight_remote_ci_node.py"),
                 "v4-node.py", "exec"), legacy)
    legacy["cwd_intruders"] = lambda _paths: []
    return legacy


def test_cwd_snapshot_requires_complete_lsof_result(monkeypatch):
    rows = "p123\nn/elsewhere\n"
    cases = (
        (1, rows, ""),
        (0, rows, "lsof: no pwd entry for UID 501\n"),
        (0, "", ""),
    )
    for returncode, stdout, stderr in cases:
        monkeypatch.setattr(
            ci_node, "_run_command",
            lambda _argv, rc=returncode, out=stdout, err=stderr:
                subprocess.CompletedProcess([], rc, out, err),
        )
        assert ci_node.cwd_snapshot() is None


def test_cwd_snapshot_accepts_only_mount_stat_warnings(monkeypatch):
    rows = "p123\nn/elsewhere\n"
    warnings = (
        "lsof: WARNING: can't stat() smbfs file system /Volumes/data\n"
        "      Output information may be incomplete.\n"
        '      assuming "dev=36000007" from mount table\n'
    )
    monkeypatch.setattr(
        ci_node, "_run_command",
        lambda _argv: subprocess.CompletedProcess([], 0, rows, warnings),
    )
    assert ci_node.cwd_snapshot() == [(123, "/elsewhere")]


def test_cwd_guard_accepts_a_slow_known_clear_census(tmp_path, monkeypatch):
    run = tmp_path / "run"
    run.mkdir()
    clock = [100.0]
    monkeypatch.setattr(ci_node.time, "monotonic", lambda: clock[0])

    def slow_census():
        clock[0] += 10.0
        return []

    monkeypatch.setattr(ci_node, "cwd_snapshot", slow_census)
    assert ci_node._CwdSnapshotGuard().intruders([run]) == []


def test_release_rechecks_after_slow_result_preparation(tmp_path, monkeypatch):
    managed = tmp_path / "managed"
    run = managed / "runs" / "lease-one"
    run.mkdir(parents=True)
    ci_node.write_json(run / "lease.json", {
        "lease_id": run.name, "state": "draining", "token_index": 0,
    })
    outside = tmp_path / "outside"
    outside.mkdir()
    clock = [100.0]
    monkeypatch.setattr(ci_node.time, "monotonic", lambda: clock[0])
    census_calls = []

    def census():
        census_calls.append(None)
        cwd = outside if len(census_calls) == 1 else run
        return [(os.getpid(), str(cwd))]

    monkeypatch.setattr(ci_node, "cwd_snapshot", census)
    guard = ci_node._CwdSnapshotGuard()
    assert guard.for_pass()

    def slow_append(*_args, **_kwargs):
        clock[0] += 10.0

    monkeypatch.setattr(ci_node, "append_result", slow_append)
    released = ci_node._mark_released(
        managed, run, ci_node.read_json(run / "lease.json"), "completed",
        cwd_guard=guard,
    )

    assert released is None
    assert ci_node.read_json(run / "lease.json")["state"] == "draining"
    assert len(census_calls) == 2


def test_immediate_scratch_cleanup_refuses_a_live_cwd(tmp_path, monkeypatch):
    managed = tmp_path / "managed"
    scratch = managed / "tmp" / "lease-one"
    scratch.mkdir(parents=True)
    run = managed / "runs" / "lease-one"
    run.mkdir(parents=True)
    lease = {"lease_id": run.name, "state": "draining", "token_index": 0}
    ci_node.write_json(run / "lease.json", lease)
    outside = tmp_path / "outside"
    outside.mkdir()
    current = scratch

    monkeypatch.setattr(
        ci_node, "cwd_snapshot",
        lambda: [(os.getpid(), str(current))],
    )
    monkeypatch.setattr(ci_node, "append_result", lambda *_args, **_kwargs: None)
    assert ci_node._mark_released(managed, run, lease, "completed") is None
    assert scratch.is_dir()
    assert ci_node.read_json(run / "lease.json")["state"] == "draining"

    current = outside
    assert ci_node._remove_run_scratch(managed, "lease-one")
    assert not scratch.exists()


def test_release_keeps_scratch_when_the_seat_cwd_is_live(tmp_path, monkeypatch):
    managed = tmp_path / "managed"
    run = managed / "runs" / "lease-one"
    slot = managed / "repos" / "test-repo" / "seats" / "s-01"
    scratch = managed / "tmp" / "lease-one"
    run.mkdir(parents=True)
    slot.mkdir(parents=True)
    scratch.mkdir(parents=True)
    lease = {
        "lease_id": run.name, "state": "draining", "slot": str(slot),
    }
    ci_node.write_json(run / "lease.json", lease)
    monkeypatch.setattr(
        ci_node, "cwd_snapshot", lambda: [(os.getpid(), str(slot))],
    )

    assert ci_node._mark_released(managed, run, lease, "completed") is None
    assert scratch.is_dir()
    assert ci_node.read_json(run / "lease.json")["state"] == "draining"


def test_tokenless_release_rechecks_seat_before_post_release_scratch_cleanup(
    tmp_path, monkeypatch,
):
    managed = tmp_path / "managed"
    admission = managed / "admission"
    run = managed / "runs" / "lease-one"
    slot = managed / "repos" / "test-repo" / "seats" / "s-01"
    scratch = managed / "tmp" / "lease-one"
    admission.mkdir(parents=True)
    run.mkdir(parents=True)
    slot.mkdir(parents=True)
    scratch.mkdir(parents=True)
    ci_node.write_json(run / "lease.json", {
        "lease_id": run.name, "state": "released", "slot": str(slot),
    })
    monkeypatch.setattr(
        ci_node, "cwd_snapshot", lambda: [(os.getpid(), str(slot))],
    )
    monkeypatch.setattr(ci_node, "_release_bundle_basis_pin", lambda *_args: True)

    ci_node._release_holder(admission, run, {"lease_id": run.name}, None)

    assert scratch.is_dir()


def test_never_admitted_holder_release_does_not_census(tmp_path, monkeypatch):
    managed = tmp_path / "managed"
    admission = managed / "admission"
    run = managed / "runs" / "lease-queued"
    scratch = managed / "tmp" / "lease-queued"
    admission.mkdir(parents=True)
    run.mkdir(parents=True)
    scratch.mkdir(parents=True)
    ci_node.write_json(run / "lease.json", {
        "lease_id": run.name, "state": "queued",
    })
    monkeypatch.setattr(ci_node, "append_result", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ci_node, "_release_bundle_basis_pin", lambda *_args: True)
    monkeypatch.setattr(
        ci_node, "cwd_snapshot",
        lambda: pytest.fail("a never-admitted holder must not run a cwd census"),
    )

    ci_node._release_holder(admission, run, {"lease_id": run.name}, None)

    assert ci_node.read_json(run / "lease.json")["state"] == "released"
    assert not scratch.exists()


def test_node_lsof_stderr_allowlist_matches_shared_helper():
    mount_warnings = (
        "lsof: WARNING: can't stat() smbfs file system /Volumes/data\n"
        "      Output information may be incomplete.\n"
        '      assuming "dev=36000007" from mount table\n'
    )
    corpus = (
        "",
        mount_warnings,
        mount_warnings.encode(),
        "lsof: no pwd entry for UID 501\n",
        "lsof: WARNING: can't stat() process 424242: Permission denied\n",
        "      Output information may be incomplete.\n",
        '      assuming "dev=36000007" from mount table\n',
        mount_warnings + "lsof: unexpected diagnostic\n",
    )
    for stderr in corpus:
        assert ci_node._lsof_stderr_is_mount_stat_warnings_only(stderr) == (
            goalflight_subprocess.lsof_stderr_is_mount_stat_warnings_only(stderr)
        )


def test_gc_never_splits_a_resident_pair_at_the_budget_boundary(
    tmp_path, monkeypatch,
):
    """runs/<id> and resident-results/<id> are one deletion: the per-pass cap
    either removes both or neither, so the retained body is never stranded."""
    managed = tmp_path / "managed"
    runs = managed / "runs"
    retained = managed / "resident-results"
    runs.mkdir(parents=True)
    retained.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    old = time.time() - 30 * 24 * 60 * 60
    indexed = {}
    ordinary = runs / "lease-a"
    ordinary.mkdir()
    ci_node.write_json(ordinary / "lease.json", {
        "lease_id": "lease-a", "state": "released", "run_directory": str(ordinary),
    })
    indexed["lease-a"] = old - 10
    original = runs / "lease-b"
    original.mkdir()
    body = retained / "lease-b"
    body.mkdir()
    for target in (original, body):
        ci_node.write_json(target / "lease.json", {
            "lease_id": "lease-b", "state": "released", "run_directory": str(original),
            "resident_submission": True,
        })
    indexed["lease-b"] = old
    monkeypatch.setattr(ci_node, "result_ids", lambda _managed: set(indexed))
    monkeypatch.setattr(ci_node, "result_finish_times", lambda _managed: indexed)
    monkeypatch.setattr(ci_node, "prunable", lambda *_args: True)
    monkeypatch.setattr(
        ci_node, "_run_command",
        lambda argv: subprocess.CompletedProcess(argv, 0, f"p{os.getpid()}\nn{outside}\n", ""),
    )
    # One ordinary body ahead of the pair, cap 2: the old code spent the
    # second unit on runs/<id> and stopped before resident-results/<id>.
    monkeypatch.setattr(ci_node, "MAX_GC_DELETIONS_PER_PASS", 2)
    ci_node.gc_run_bodies(managed, {}, True, cap_deletions=True)
    assert not ordinary.exists()
    assert original.exists() == body.exists(), "resident pair split by the cap"


def test_gc_retries_a_resident_pair_when_retained_body_removal_fails(
    tmp_path, monkeypatch,
):
    managed = tmp_path / "managed"
    original = managed / "runs" / "lease-pair"
    body = managed / "resident-results" / "lease-pair"
    original.mkdir(parents=True)
    body.mkdir(parents=True)
    old = time.time() - 30 * 24 * 60 * 60
    for path in (original, body):
        ci_node.write_json(path / "lease.json", {
            "lease_id": "lease-pair", "state": "released",
            "run_directory": str(original), "resident_submission": True,
        })
    monkeypatch.setattr(ci_node, "result_ids", lambda _managed: {"lease-pair"})
    monkeypatch.setattr(
        ci_node, "result_finish_times", lambda _managed: {"lease-pair": old},
    )
    monkeypatch.setattr(ci_node, "prunable", lambda *_args: True)
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setattr(
        ci_node, "_run_command",
        lambda argv: subprocess.CompletedProcess(
            argv, 0, f"p{os.getpid()}\nn{outside}\n", "",
        ),
    )
    real_rmtree = ci_node.shutil.rmtree
    removal_order = []
    fail_retained_once = [True]

    def fail_first_retained_removal(path, *args, **kwargs):
        path = Path(path)
        removal_order.append(path)
        if path == body and fail_retained_once[0]:
            fail_retained_once[0] = False
            raise PermissionError("retained body is temporarily busy")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(ci_node.shutil, "rmtree", fail_first_retained_removal)
    ci_node.gc_run_bodies(managed, {}, True, cap_deletions=True)
    assert original.is_dir() and body.is_dir()
    assert removal_order == [body]

    ci_node.gc_run_bodies(managed, {}, True, cap_deletions=True)
    assert not original.exists() and not body.exists()
    assert removal_order[-2:] == [body, original]


def test_gc_recovers_a_resident_original_when_its_removal_fails_after_the_retained_body(
    tmp_path, monkeypatch,
):
    """Retained-first deletion can leave runs/<id> if its removal fails after
    resident-results/<id> is gone. That original must not be skipped forever as
    a resident submission; the next pass collects it as an ordinary body."""
    managed = tmp_path / "managed"
    original = managed / "runs" / "lease-pair"
    body = managed / "resident-results" / "lease-pair"
    original.mkdir(parents=True)
    body.mkdir(parents=True)
    old = time.time() - 30 * 24 * 60 * 60
    for path in (original, body):
        ci_node.write_json(path / "lease.json", {
            "lease_id": "lease-pair", "state": "released",
            "run_directory": str(original), "resident_submission": True,
        })
    monkeypatch.setattr(ci_node, "result_ids", lambda _managed: {"lease-pair"})
    monkeypatch.setattr(
        ci_node, "result_finish_times", lambda _managed: {"lease-pair": old},
    )
    monkeypatch.setattr(ci_node, "prunable", lambda *_args: True)
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setattr(
        ci_node, "_run_command",
        lambda argv: subprocess.CompletedProcess(
            argv, 0, f"p{os.getpid()}\nn{outside}\n", "",
        ),
    )
    real_rmtree = ci_node.shutil.rmtree
    fail_original_once = [True]

    def fail_first_original_removal(path, *args, **kwargs):
        path = Path(path)
        if path == original and fail_original_once[0]:
            fail_original_once[0] = False
            raise PermissionError("original run directory is temporarily busy")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(ci_node.shutil, "rmtree", fail_first_original_removal)
    try:
        ci_node.gc_run_bodies(managed, {}, True, cap_deletions=True)
    except PermissionError:
        pass
    assert not body.exists() and original.is_dir()

    ci_node.gc_run_bodies(managed, {}, True, cap_deletions=True)
    assert not original.exists()


def test_gc_refused_fresh_checks_still_spend_the_census_budget(
    tmp_path, monkeypatch,
):
    """Refusals must consume the per-pass budget, or a pass where every fresh
    check finds a live cwd would census once per body under the admission lock
    (the outage shape)."""
    managed = tmp_path / "managed"
    runs = managed / "runs"
    runs.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    indexed = {}
    for index in range(6):
        lease_id = f"lease-{index}"
        run = runs / lease_id
        run.mkdir()
        ci_node.write_json(run / "lease.json", {
            "lease_id": lease_id, "state": "released", "run_directory": str(run),
        })
        indexed[lease_id] = time.time() - 30 * 24 * 60 * 60
    monkeypatch.setattr(ci_node, "result_ids", lambda _managed: set(indexed))
    monkeypatch.setattr(ci_node, "result_finish_times", lambda _managed: indexed)
    monkeypatch.setattr(ci_node, "prunable", lambda *_args: True)
    calls = []

    def run_lsof(argv):
        calls.append(argv)
        if len(calls) == 1:
            body = f"p{os.getpid()}\nn{outside}\n"
        else:
            body = "".join(f"p{os.getpid()}\nn{run}\n" for run in runs.iterdir())
        return subprocess.CompletedProcess(argv, 0, body, "")

    monkeypatch.setattr(ci_node, "_run_command", run_lsof)
    ci_node.gc_run_bodies(managed, {}, True, cap_deletions=True)
    assert len(calls) <= 1 + ci_node.MAX_GC_DELETIONS_PER_PASS
    assert len(list(runs.iterdir())) == 6


def test_gc_uses_one_filter_census_and_caps_total_deletions_at_two(
    tmp_path, monkeypatch,
):
    managed = tmp_path / "managed"
    runs = managed / "runs"
    runs.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    indexed = {}
    for index in range(5):
        lease_id = f"lease-{index}"
        run = runs / lease_id
        run.mkdir()
        ci_node.write_json(run / "lease.json", {
            "lease_id": lease_id, "state": "released", "run_directory": str(run),
        })
        indexed[lease_id] = time.time() - 30 * 24 * 60 * 60
    scratch_root = managed / "tmp"
    scratch_root.mkdir()
    old = time.time() - ci_node.DEFAULT_REMOTE_RUN_TIMEOUT_SECONDS - 10
    for index in range(3):
        scratch = scratch_root / f"scratch-{index}"
        scratch.mkdir()
        os.utime(scratch, (old, old))
    monkeypatch.setattr(ci_node, "result_ids", lambda _managed: set(indexed))
    monkeypatch.setattr(ci_node, "result_finish_times", lambda _managed: indexed)
    monkeypatch.setattr(ci_node, "prunable", lambda *_args: True)
    calls = []

    def run_lsof(argv):
        calls.append(argv)
        return subprocess.CompletedProcess(
            argv, 0, f"p{os.getpid()}\nn{outside}\n", "",
        )

    monkeypatch.setattr(ci_node, "_run_command", run_lsof)
    ci_node.gc_run_bodies(managed, {}, True, cap_deletions=True)
    assert len(calls) == 1 + ci_node.MAX_GC_DELETIONS_PER_PASS
    assert len(list(runs.iterdir())) == 5 - ci_node.MAX_GC_DELETIONS_PER_PASS
    assert len(list(scratch_root.iterdir())) == 3


def test_gc_rechecks_before_deleting_when_a_process_enters_after_the_pass_census(
    tmp_path, monkeypatch,
):
    managed = tmp_path / "managed"
    runs = managed / "runs"
    runs.mkdir(parents=True)
    first = runs / "lease-a"
    second = runs / "lease-b"
    first.mkdir()
    second.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    indexed = {}
    for run in (first, second):
        ci_node.write_json(run / "lease.json", {
            "lease_id": run.name, "state": "released", "run_directory": str(run),
        })
        indexed[run.name] = time.time() - 30 * 24 * 60 * 60
    monkeypatch.setattr(ci_node, "result_ids", lambda _managed: set(indexed))
    monkeypatch.setattr(ci_node, "result_finish_times", lambda _managed: indexed)
    monkeypatch.setattr(ci_node, "prunable", lambda *_args, **_kwargs: True)
    calls = []

    def run_lsof(argv):
        calls.append(argv)
        # One filtering census, then one fresh proof per candidate deletion.
        cwd = second if len(calls) == 3 else outside
        return subprocess.CompletedProcess(
            argv, 0, f"p{os.getpid()}\nn{cwd}\n", "",
        )

    monkeypatch.setattr(ci_node, "_run_command", run_lsof)
    ci_node.gc_run_bodies(managed, {}, True, cap_deletions=True)

    assert not first.exists()
    assert second.is_dir()
    assert len(calls) == 3


def test_gc_scratch_rechecks_before_deleting_when_a_process_enters(
    tmp_path, monkeypatch,
):
    managed = tmp_path / "managed"
    scratch = managed / "tmp" / "orphan"
    scratch.mkdir(parents=True)
    old = time.time() - ci_node.DEFAULT_REMOTE_RUN_TIMEOUT_SECONDS - 10
    os.utime(scratch, (old, old))
    outside = tmp_path / "outside"
    outside.mkdir()
    calls = []

    def run_lsof(argv):
        calls.append(argv)
        cwd = scratch if len(calls) == 2 else outside
        return subprocess.CompletedProcess(
            argv, 0, f"p{os.getpid()}\nn{cwd}\n", "",
        )

    monkeypatch.setattr(ci_node, "_run_command", run_lsof)
    rows = ci_node.gc_run_bodies(managed, {}, True)

    assert scratch.is_dir()
    assert any(row.get("class") == "LIVE" and row.get("path") == str(scratch)
               for row in rows)
    assert len(calls) == 2


def test_reclaim_dead_prestart_caps_fresh_census_attempts_per_pass(
    tmp_path, monkeypatch,
):
    managed = tmp_path / "managed"
    runs = managed / "runs"
    runs.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    for index in range(5):
        run = runs / f"lease-{index}"
        slot = managed / "slots" / f"s-{index}"
        run.mkdir()
        slot.mkdir(parents=True)
        ci_node.write_json(run / "lease.json", {
            "lease_id": run.name, "state": "admitted", "slot": str(slot),
        })
    calls = []

    def run_lsof(argv):
        calls.append(argv)
        if len(calls) > 1:
            return subprocess.CompletedProcess(argv, 1, "", "temporary lsof failure")
        return subprocess.CompletedProcess(
            argv, 0, f"p{os.getpid()}\nn{outside}\n", "",
        )

    monkeypatch.setattr(ci_node, "_run_command", run_lsof)
    released = ci_node.reclaim_dead_prestart(managed)
    assert released == []
    assert len(calls) == 1 + ci_node.MAX_OWNERSHIP_CENSUS_ATTEMPTS_PER_PASS
    assert all(ci_node.read_json(run / "lease.json")["state"] == "admitted"
               for run in runs.iterdir())


def test_refused_admitted_release_is_retried_on_the_next_pass(tmp_path, monkeypatch):
    managed = tmp_path / "managed"
    admission = managed / "admission"
    runs = managed / "runs"
    admission.mkdir(parents=True)
    runs.mkdir()
    run = runs / "lease-retry"
    slot = managed / "repos" / "test-repo" / "seats" / "s-01"
    run.mkdir()
    slot.mkdir(parents=True)
    holder_lock = ci_node.try_lock(run / "holder.lock")
    assert holder_lock is not None
    ci_node.write_json(run / "lease.json", {
        "lease_id": run.name, "state": "admitted", "token_index": 0,
        "slot": str(slot),
    })
    outside = tmp_path / "outside"
    outside.mkdir()
    calls = []

    def run_lsof(argv):
        calls.append(argv)
        cwd = run if len(calls) == 1 else outside
        return subprocess.CompletedProcess(
            argv, 0, f"p{os.getpid()}\nn{cwd}\n", "",
        )

    monkeypatch.setattr(ci_node, "_run_command", run_lsof)
    monkeypatch.setattr(ci_node, "append_result", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ci_node, "_release_bundle_basis_pin", lambda *_args: True)

    class Token:
        def close(self):
            pass

    ci_node._release_holder(admission, run, {"token_index": 0}, Token())
    assert ci_node.read_json(run / "lease.json")["state"] == "admitted"
    holder_lock.close()
    released = ci_node.reclaim_dead_prestart(managed)

    assert len(released) == 1
    assert ci_node.read_json(run / "lease.json")["state"] == "released"
    assert len(calls) == 3


def test_admit_once_releases_many_never_admitted_tickets_without_census(
    tmp_path, monkeypatch,
):
    managed = tmp_path / "managed"
    admission = managed / "admission"
    tickets = admission / "tickets"
    tickets.mkdir(parents=True)
    runs = managed / "runs"
    runs.mkdir()
    monkeypatch.setattr(
        ci_node, "awake_monotonic",
        lambda: ci_node.SUBMITTED_TICKET_TTL_SECONDS + 10,
    )
    monkeypatch.setattr(ci_node, "_release_bundle_basis_pin", lambda *_args: True)
    census_calls = []
    monkeypatch.setattr(ci_node, "cwd_snapshot", lambda: census_calls.append(None))

    count = 5
    for number in range(1, count + 1):
        run = runs / f"lease-{number}"
        run.mkdir()
        for name in ("stdout", "stderr"):
            (run / name).touch()
        ci_node.write_json(run / "owner.json", {"owner_identity": f"owner-{number}"})
        ci_node.write_json(run / "lease.json", {
            "lease_id": run.name, "lease_token": f"token-{number}",
            "state": "queued", "resident_submission": True,
            "ticket": number, "ticket_awake_started": 1,
            "ticket_boot_session": "test-boot", "repo": None,
        })
        (tickets / f"{number:020d}").touch()

    events = ci_node._admit_once(managed, boot_session="test-boot")

    assert [event for event in events if event[1] == "expired"] == [
        (number, "expired") for number in range(1, count + 1)
    ]
    assert all(ci_node.read_json(run / "lease.json")["state"] == "released"
               for run in runs.iterdir())
    assert census_calls == []


def test_run_holder_refreshes_production_census_before_reclaiming(
    tmp_path, monkeypatch,
):
    managed = tmp_path / "managed"
    root = managed / "admission"
    tickets = root / "tickets"
    runs = managed / "runs"
    tickets.mkdir(parents=True)
    runs.mkdir(parents=True)
    current = runs / "current"
    current.mkdir()
    current_state = {
        "lease_id": current.name, "lease_token": "token", "state": "queued",
        "ticket": 1, "destination_address": "127.0.0.1",
    }
    ci_node.write_json(current / "lease.json", current_state)
    ticket = tickets / "0001"
    ticket.touch()
    ticket_lock = ci_node.try_lock(ticket)
    holder_lock = ci_node.try_lock(current / "holder.lock")
    assert ticket_lock is not None and holder_lock is not None
    stale = runs / "stale"
    slot = managed / "slots" / "s-01"
    stale.mkdir()
    slot.mkdir(parents=True)
    ci_node.write_json(stale / "lease.json", {
        "lease_id": stale.name, "state": "admitted", "slot": str(slot),
    })
    outside = tmp_path / "outside"
    outside.mkdir()
    census_calls = []

    def run_lsof(argv):
        census_calls.append(argv)
        cwd = outside if len(census_calls) == 1 else slot
        return subprocess.CompletedProcess(
            argv, 0, f"p{os.getpid()}\nn{cwd}\n", "",
        )

    monkeypatch.setattr(ci_node, "_run_command", run_lsof)
    monkeypatch.setattr(ci_node.os, "setsid", lambda: None)
    monkeypatch.setattr(ci_node, "_submitted_ticket_runs", lambda _managed: {})
    monkeypatch.setattr(ci_node, "admission_queue_state", lambda _managed: None)
    monkeypatch.setattr(ci_node, "_release_bundle_basis_pin", lambda *_args, **_kwargs: True)
    try:
        ci_node.run_holder(
            root, current, ticket, ticket_lock, holder_lock,
            {"queue_wait_seconds": 0, "poll_seconds": 0.01},
        )
    finally:
        ticket_lock.close()
        holder_lock.close()

    assert ci_node.read_json(stale / "lease.json")["state"] == "admitted"
    assert len(census_calls) >= 2


def test_holder_release_rechecks_under_lock_before_releasing(tmp_path, monkeypatch):
    managed = tmp_path / "managed"
    admission = managed / "admission"
    run = managed / "runs" / "lease-one"
    slot = managed / "slots" / "s-01"
    admission.mkdir(parents=True)
    run.mkdir(parents=True)
    slot.mkdir(parents=True)
    ci_node.write_json(run / "lease.json", {
        "lease_id": run.name, "state": "admitted", "token_index": 0,
        "slot": str(slot),
    })
    real_with_queue_lock = ci_node._with_queue_lock
    lock_calls = []

    def delayed_queue_lock(root, **kwargs):
        handle = real_with_queue_lock(root, **kwargs)
        lock_calls.append(root)
        return handle

    monkeypatch.setattr(ci_node, "_with_queue_lock", delayed_queue_lock)
    census_calls = []

    def run_lsof(argv):
        census_calls.append(argv)
        assert len(lock_calls) == 2
        cwd = run
        return subprocess.CompletedProcess(
            argv, 0, f"p{os.getpid()}\nn{cwd}\n", "",
        )

    monkeypatch.setattr(ci_node, "_run_command", run_lsof)
    class Token:
        def close(self):
            pass

    ci_node._release_holder(admission, run, {"token_index": 0}, Token())

    assert ci_node.read_json(run / "lease.json")["state"] == "admitted"
    assert len(census_calls) == 1


def test_finish_dead_workload_rechecks_prestart_after_lock_wait(tmp_path, monkeypatch):
    managed = tmp_path / "managed"
    admission = managed / "admission"
    run = managed / "runs" / "lease-one"
    slot = managed / "slots" / "s-01"
    admission.mkdir(parents=True)
    run.mkdir(parents=True)
    slot.mkdir(parents=True)
    ci_node.write_json(run / "lease.json", {
        "lease_id": run.name, "state": "admitted", "slot": str(slot),
    })
    real_with_queue_lock = ci_node._with_queue_lock
    lock_calls = []

    def delayed_queue_lock(root, **kwargs):
        handle = real_with_queue_lock(root, **kwargs)
        lock_calls.append(root)
        return handle

    monkeypatch.setattr(ci_node, "_with_queue_lock", delayed_queue_lock)
    census_calls = []

    def run_lsof(argv):
        census_calls.append(argv)
        assert len(lock_calls) == 2
        cwd = run
        return subprocess.CompletedProcess(
            argv, 0, f"p{os.getpid()}\nn{cwd}\n", "",
        )

    monkeypatch.setattr(ci_node, "_run_command", run_lsof)
    result = ci_node.finish_dead_workload(admission, run, {}, "operator-clear")

    assert result == {"status": "unknown"}
    assert ci_node.read_json(run / "lease.json")["state"] == "draining"
    assert len(census_calls) == 1


def test_holder_admission_pass_reuses_one_cwd_census_for_many_runs(tmp_path, monkeypatch):
    managed = tmp_path / "managed"
    root = managed / "admission"
    tickets = root / "tickets"
    runs = managed / "runs"
    tickets.mkdir(parents=True)
    runs.mkdir(parents=True)
    current = runs / "current"
    current.mkdir()
    ci_node.write_json(current / "lease.json", {
        "lease_id": "current", "lease_token": "token", "state": "queued",
        "ticket": 1, "destination_address": "127.0.0.1",
    })
    ticket = tickets / "0001"
    ticket.touch()
    ticket_lock = ci_node.try_lock(ticket)
    holder_lock = ci_node.try_lock(current / "holder.lock")
    assert ticket_lock is not None and holder_lock is not None
    for index in range(5):
        run = runs / f"stale-{index}"
        slot = managed / "slots" / f"s-{index}"
        run.mkdir()
        slot.mkdir(parents=True)
        ci_node.write_json(run / "lease.json", {
            "lease_id": run.name, "state": "admitted", "slot": str(slot),
        })
    monkeypatch.setattr(ci_node.os, "setsid", lambda: None)
    monkeypatch.setattr(ci_node, "_submitted_ticket_runs", lambda _managed: {})
    monkeypatch.setattr(ci_node, "admission_queue_state", lambda _managed: None)
    monkeypatch.setattr(
        ci_node, "_mark_released",
        lambda _managed, _run, lease, _reason, **_kwargs: lease,
    )
    monkeypatch.setattr(ci_node, "_release_bundle_basis_pin", lambda *_args, **_kwargs: True)
    calls = []

    def run_lsof(argv):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "p123\nn/elsewhere\n", "")

    monkeypatch.setattr(ci_node, "_run_command", run_lsof)
    try:
        ci_node.run_holder(
            root, current, ticket, ticket_lock, holder_lock,
            {"queue_wait_seconds": 0, "poll_seconds": 0.01},
        )
    finally:
        ticket_lock.close()
        holder_lock.close()
    assert len(calls) == 1


def test_slot_candidates_share_one_cwd_census(tmp_path, monkeypatch):
    managed = tmp_path / "managed"
    seats = managed / "repos" / "repo" / "seats"
    candidate_paths = [seats / f"s-{index:02d}" for index in range(1, 6)]
    output = "".join(f"p{os.getpid()}\nn{path}\n" for path in candidate_paths)
    calls = []

    def run_lsof(argv):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, output, "")

    monkeypatch.setattr(ci_node, "_run_command", run_lsof)
    slot, lock = ci_node.lease_slot(
        managed, "repo", len(candidate_paths), {}, token_index=0,
    )
    assert slot is None
    assert lock is None
    assert len(calls) == 1


def test_slot_lease_rechecks_when_process_enters_after_candidate_census(
    tmp_path, monkeypatch,
):
    managed = tmp_path / "managed"
    slot = managed / "repos" / "repo" / "seats" / "s-01"
    outside = tmp_path / "outside"
    outside.mkdir()
    calls = []

    def run_lsof(argv):
        calls.append(argv)
        cwd = outside if len(calls) == 1 else slot
        return subprocess.CompletedProcess(
            argv, 0, f"p{os.getpid()}\nn{cwd}\n", "",
        )

    monkeypatch.setattr(ci_node, "_run_command", run_lsof)
    leased, lock = ci_node.lease_slot(
        managed, "repo", 1, {"lease_id": "lease-one"}, token_index=0,
    )
    assert leased is None
    assert lock is None
    assert len(calls) == 2
    marker = managed / "repos" / "repo" / "slot-meta" / "s-01.json"
    assert not marker.exists()


@pytest.mark.parametrize("kill_point", ["before-upload", "mid-upload", "before-publish"])
def test_killed_upload_releases_provisional_basis_and_never_reserves_tokens(
    tmp_path, monkeypatch, kill_point,
):
    managed = tmp_path / "managed"
    root = managed / "admission"
    (root / "tickets").mkdir(parents=True)
    source, basis, _temporary = _test_source()
    mirror = managed / "repos" / "test-repo" / "mirror.git"
    mirror.parent.mkdir(parents=True)
    _git(source, "clone", "--mirror", str(source), str(mirror))
    checkpoint = tmp_path / "upload-checkpoint"
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(write_fd)

        def pause():
            checkpoint.touch()
            os.read(read_fd, 1)
            raise AssertionError("parent should kill the submitter at its checkpoint")

        class InterruptedStream:
            first = True

            def read(self, _size):
                if kill_point == "before-upload" or not self.first:
                    pause()
                self.first = False
                return b"te"

        stream = io.BytesIO(b"test") if kill_point == "before-publish" else InterruptedStream()
        if kill_point == "before-publish":
            rename = ci_node.Path.rename

            def pause_publish(path, target):
                if path.parent == managed / "uploads":
                    pause()
                return rename(path, target)

            ci_node.Path.rename = pause_publish
        request = {**_upload_request(stream), "basis_commit": basis}
        ci_node._submit_ticket(managed, root, request, 1, "")
        os._exit(0)
    os.close(read_fd)
    try:
        deadline = time.monotonic() + 30
        while not checkpoint.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert checkpoint.exists(), f"submitter never reached {kill_point}"
        uploads = list(ci_node.iter_upload_dirs(managed))
        assert len(uploads) == 1
        upload = uploads[0]
        lease = ci_node.read_json(upload / "lease.json")
        hold = _ticket_ref(managed, "test-repo", upload.name)
        assert hold.is_file()
        if kill_point == "before-publish":
            assert ci_node.locked(mirror.parent / "pool.lock")
        else:
            with ci_node._repo_pool_lock(managed, "test-repo") as (repo_root, lock_fd):
                result = ci_node._repo_gc_locked(
                    managed, "test-repo", repo_root, mirror, lock_fd,
                )
            assert result["status"] == "complete"
            assert hold.is_file()
        assert list(ci_node.iter_run_dirs(managed)) == []
        assert list((root / "tickets").iterdir()) == []
        assert ci_node.reserved_token_indexes(managed, token_pool_size=1) == (set(), None)
        monkeypatch.setattr(ci_node, "awake_monotonic", lambda:
                            lease["ticket_awake_started"] + ci_node.UPLOAD_TTL_SECONDS + 1)
        assert ci_node._gc_uploads(managed) == []  # A live upload owns its flock.
        assert upload.exists()
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
        pid = None
        assert hold.is_file() and upload.exists()  # The provisional hold has a durable owner.
        with monkeypatch.context() as failure:
            failure.setattr(ci_node, "_release_bundle_basis_pin", lambda *_args: False)
            assert ci_node._gc_uploads(managed) == [(None, "upload-cleanup-deferred")]
            assert hold.is_file() and upload.exists()
        assert ci_node._gc_uploads(managed) == [(None, "upload-expired")]
        assert not upload.exists()
        assert not hold.exists()
        assert ci_node.reserved_token_indexes(managed, token_pool_size=1) == (set(), None)
    finally:
        os.close(write_fd)
        if pid is not None:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)


def test_invalid_upload_digest_never_publishes_a_ticket(tmp_path):
    managed = tmp_path / "managed"
    root = managed / "admission"
    (root / "tickets").mkdir(parents=True)
    with pytest.raises(ValueError, match="digest mismatch"):
        ci_node._submit_ticket(managed, root, _upload_request(io.BytesIO(b"nope")), 1, "")
    assert list(ci_node.iter_run_dirs(managed)) == []
    assert list(ci_node.iter_upload_dirs(managed)) == []
    assert not (root / "sequence.json").exists()


def test_lease_less_private_upload_expires_by_awake_time(tmp_path, monkeypatch):
    managed = tmp_path / "managed"
    upload = managed / "uploads" / ("9" * 32)
    upload.mkdir(parents=True)
    clock = {"awake": 100.0}
    monkeypatch.setattr(ci_node, "awake_monotonic", lambda: clock["awake"])
    monkeypatch.setattr(ci_node.time, "time", lambda: 1_000_000.0)

    assert ci_node._gc_uploads(managed, boot_session="boot-one") == []
    marker = ci_node.read_json(upload / "upload-start.json")
    assert marker["ticket_awake_started"] == 100.0
    clock["awake"] = 100.5
    assert ci_node._gc_uploads(managed, boot_session="boot-one") == []
    clock["awake"] = 100.0 + ci_node.UPLOAD_TTL_SECONDS + 1
    assert ci_node._gc_uploads(managed, boot_session="boot-one") == [
        (None, "upload-expired"),
    ]
    assert not upload.exists()


def test_submit_returns_before_execution_and_finishes_without_a_followup_node_call(
    node_env, tmp_path,
):
    _require_launchctl()
    config, executor, _runner, node = node_env
    node.call("list")  # Finish the node-helper preflight before measuring submit.
    before = len(executor.calls)
    result_file = tmp_path / "submitted.json"
    pid = os.fork()
    if pid == 0:
        try:
            values, sha = _submission_payload(
                config, "resident-offline",
                code="import time; time.sleep(3); print('2')",
            )
            try:
                started = time.monotonic()
                record = node.call("submit", **values)
                submit_seconds = time.monotonic() - started
            finally:
                values["candidate_bundle_path"].unlink(missing_ok=True)
            result_file.write_text(json.dumps({
                "record": record,
                "sha": sha,
                "submit_seconds": submit_seconds,
                "calls": executor.calls[before:],
            }), encoding="utf-8")
            os.kill(os.getpid(), signal.SIGKILL)
        except BaseException as exc:
            result_file.write_text(json.dumps({"error": str(exc)}), encoding="utf-8")
            os._exit(1)
    status = _waitpid_bounded(pid)
    assert os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL
    submitted = json.loads(result_file.read_text(encoding="utf-8"))
    assert "error" not in submitted, submitted.get("error")
    record = submitted["record"]

    assert submitted["submit_seconds"] < 2.5
    assert submitted["calls"] == ["submit"]
    assert executor.calls[before:] == []
    stored = json.loads((Path(record["run_directory"]) / "command.json").read_text())
    assert stored["env"]["GOALFLIGHT_REMOTE_CI_SELF_CAP"] == "2"
    assert Path(record["run_directory"], "source.bundle").is_file()

    result_path = Path(record["run_directory"]) / "result.json"
    deadline = time.monotonic() + 30
    while not result_path.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert result_path.is_file(), "ticket did not run after the submit transport ended"
    result = json.loads(result_path.read_text())
    assert result["status"] == "completed"
    assert result["returncode"] == 0
    lease_path = Path(record["run_directory"]) / "lease.json"
    release_deadline = time.monotonic() + 30
    lease = ci_node.read_json(lease_path)
    while lease["state"] != "released" and time.monotonic() < release_deadline:
        time.sleep(0.05)
        lease = ci_node.read_json(lease_path)
    assert lease["state"] == "released"


def test_submit_retries_are_idempotent_across_active_and_retained_tickets(node_env):
    _require_launchctl()
    config, executor, _runner, node = node_env
    values, _sha = _submission_payload(
        config, "resident-idempotent", code="import time; time.sleep(0.2)",
    )
    try:
        first = node.call("submit", **values)
        second = node.call("submit", **values)
        assert second["lease_id"] == first["lease_id"]
        assert second["ticket"] == first["ticket"]
        assert len([path for path in ci_node.iter_run_dirs(executor.root)
                    if ci_node.read_json(path / "lease.json").get("request_id")
                    == "resident-idempotent"]) == 1
        original_command = values["command"]
        values["command"] = {
            **original_command,
            "argv": [sys.executable, "-c", "print('different')"],
        }
        with pytest.raises(ci.RemoteSubmitRejectedError, match="different submitted ticket"):
            node.call("submit", **values)
        values["command"] = original_command
        _wait_for_state(node, first, {"released"})
        third = node.call("submit", **values)
        assert third["lease_id"] == first["lease_id"]
        assert third["ticket"] == first["ticket"]
    finally:
        values["candidate_bundle_path"].unlink(missing_ok=True)


@pytest.mark.parametrize("submitted_cap", ["0", "-1", "1.5", "many"])
def test_submit_rejects_invalid_worker_caps(node_env, submitted_cap):
    config, executor, _runner, node = node_env
    values, _sha = _submission_payload(config, f"bad-submit-cap-{submitted_cap}")
    values["command"]["env"]["GOALFLIGHT_REMOTE_CI_SELF_CAP"] = submitted_cap
    try:
        with pytest.raises(ci.RemoteSubmitRejectedError, match="positive integer"):
            node.call("submit", **values)
        assert not [path for path in ci_node.iter_run_dirs(executor.root)
                    if ci_node.read_json(path / "lease.json").get("request_id")
                    == values["request_id"]]
    finally:
        values["candidate_bundle_path"].unlink(missing_ok=True)


def test_missing_sample_self_cap_clamps_resident_workload_to_one(node_env):
    _require_launchctl()
    config, executor, _runner, node = node_env
    values, _sha = _submission_payload(
        config, "resident-missing-sample-cap",
        code=("import os; print('workload-cap=' + "
              "os.environ['GOALFLIGHT_REMOTE_CI_SELF_CAP'])"),
    )
    try:
        record = node.call("submit", **values)
    finally:
        values["candidate_bundle_path"].unlink(missing_ok=True)
    released = _wait_for_state(node, record, {"released"})
    assert "self_cap" not in released["sample"]
    run = Path(record["run_directory"])
    assert "workload-cap=1\n" in (run / "stdout").read_text()
    assert ci_node.read_json(run / "command.json")["env"][
        "GOALFLIGHT_REMOTE_CI_SELF_CAP"
    ] == "1"


def test_submit_streams_multi_megabyte_git_bundle(node_env, tmp_path):
    config, executor, _runner, node = node_env
    source = tmp_path / "multi-meg-source"
    _init_repo(source)
    with (source / "payload.bin").open("wb") as output:
        for _ in range(16):
            output.write(os.urandom(256 * 1024))
    _git(source, "add", "payload.bin")
    _git(source, "commit", "-m", "multi megabyte source")
    sha = _git(source, "rev-parse", "HEAD")
    bundle = tmp_path / "multi-meg.bundle"
    _git(source, "bundle", "create", str(bundle), "HEAD")
    assert bundle.stat().st_size > 2 * 1024 * 1024

    result = node.call(
        "submit",
        request_id="multi-meg-stream",
        arm="candidate",
        owner=RemoteRunner._owner(),
        repo=config.repo,
        max_concurrent_seats=1,
        checkout_basename=config.checkout_basename,
        command={
            "argv": [sys.executable, "-c", "pass"],
            "env": {"GOALFLIGHT_REMOTE_CI_SHA": sha},
            "timeout": 20,
        },
        candidate_bundle_path=bundle,
    )
    stored_bundle = Path(result["run_directory"]) / "source.bundle"
    assert stored_bundle.stat().st_size == bundle.stat().st_size
    assert _file_sha256(stored_bundle) == _file_sha256(bundle)
    assert "--stream-submit" in executor.remote_scripts[-1]
    assert str(bundle) not in executor.remote_scripts[-1]
    _wait_for_state(node, result, {"admitted", "released"})


@pytest.mark.parametrize(("submitted_cap", "expected_cap"), [(8, 2), (1, 1)])
def test_resident_workload_clamps_self_cap_to_admission_sample(
    node_env, submitted_cap, expected_cap,
):
    _require_launchctl()
    config, executor, _runner, node = node_env
    admission = executor.root / "admission"
    admission.mkdir(parents=True, exist_ok=True)
    ci_node.write_json(admission / "caps.json", {"self_cap": 2})
    values, _sha = _submission_payload(
        config, f"resident-cap-{submitted_cap}",
        code=("import os; print('workload-cap=' + "
              "os.environ['GOALFLIGHT_REMOTE_CI_SELF_CAP'])"),
    )
    values["command"]["env"]["GOALFLIGHT_REMOTE_CI_SELF_CAP"] = str(submitted_cap)
    try:
        record = node.call("submit", **values)
    finally:
        values["candidate_bundle_path"].unlink(missing_ok=True)
    released = _wait_for_state(node, record, {"released"})
    assert released["sample"]["self_cap"] == 2
    run = Path(record["run_directory"])
    assert ci_node.read_json(run / "result.json")["returncode"] == 0
    assert f"workload-cap={expected_cap}\n" in (run / "stdout").read_text()
    assert ci_node.read_json(run / "command.json")["env"][
        "GOALFLIGHT_REMOTE_CI_SELF_CAP"
    ] == str(expected_cap)


def test_submit_bundle_limit_raises_typed_error_before_transport(
    node_env, tmp_path, monkeypatch,
):
    config, executor, _runner, node = node_env
    values, _sha = _submission_payload(config, "oversized-stream")
    bundle_path = values["candidate_bundle_path"]
    try:
        monkeypatch.setattr(ci, "MAX_SUBMIT_BUNDLE_BYTES", 1)
        before = len(executor.calls)
        with pytest.raises(ci.RemoteCIBundleTooLargeError, match="candidate bundle exceeds"):
            node.call("submit", **values)
        assert len(executor.calls) == before
    finally:
        bundle_path.unlink(missing_ok=True)


@pytest.mark.parametrize(
    ("load1", "busy", "max_seats", "decision"),
    [
        (0, 0.0, 0, "project-cap"),
        (40, 0.96, 1, "saturated"),
    ],
)
def test_submitted_ticket_waits_for_project_cap_and_saturation_gate(
    node_env, load1, busy, max_seats, decision,
):
    config, executor, _runner, node = node_env
    executor.load1 = load1
    executor.cpu_busy_fraction = busy
    record, _sha = _submit(node, config, f"resident-{decision}", max_seats=max_seats)

    audit = Path(executor.root) / "admission" / "audit.log"
    deadline = time.monotonic() + 5
    while (not audit.exists() or decision not in audit.read_text()) and time.monotonic() < deadline:
        time.sleep(0.05)
    status = node.call("status", **key(record))
    assert status["state"] == "queued"
    assert decision in audit.read_text()
    assert (Path(record["run_directory"]) / "command.json").is_file()

    cancelled = node.call("cancel", **key(record), expected_owner=_expected_owner(record))
    assert cancelled["status"] == "cancelled"


def test_mixed_v4_v5_ticket_queue_stays_numeric_and_admitter_does_not_double_start(
    node_env, tmp_path,
):
    config, _executor, _runner, node = node_env
    managed = node.box.managed_run_directory
    admission = managed / "admission"
    tickets = admission / "tickets"
    tickets.mkdir(parents=True, exist_ok=True)
    ci_node.write_json(admission / "policy.json", {
        "p_cores": node.box.p_cores,
        "token_pool_size": node.box.token_pool_size,
    })
    ci_node.write_json(admission / "sequence.json", 1)

    legacy_id = "1" * 32
    legacy_run = managed / "runs" / legacy_id
    legacy_run.mkdir(parents=True)
    for name in ("stdout", "stderr"):
        (legacy_run / name).touch()
    owner = RemoteRunner._owner()
    ci_node.write_json(legacy_run / "owner.json", owner)
    ci_node.write_json(legacy_run / "lease.json", {
        "schema": "goalflight.remote-ci.lease.v1",
        "lease_id": legacy_id,
        "lease_token": "v4-lease-token",
        "box": node.box.name,
        "managed_run_directory": str(managed),
        "run_directory": str(legacy_run),
        "state": "queued",
        "ticket": 1,
        "request_id": "old-v4-request",
        "arm": "candidate",
        "repo": config.repo,
        "max_concurrent_seats": 1,
    })
    legacy_ticket = tickets / f"{1:020d}"
    legacy_lock = ci_node.try_lock(legacy_ticket)
    assert legacy_lock is not None

    record, _sha = _submit(node, config, "new-v5-request", max_seats=1)
    v5_ticket = tickets / f"{record['ticket']:020d}"
    assert sorted(path.name for path in tickets.iterdir()) == [
        f"{1:020d}", f"{record['ticket']:020d}",
    ]
    assert all(path.name.isdecimal() for path in tickets.iterdir())
    assert ci_node.locked(legacy_ticket)
    assert ci_node.locked(v5_ticket), "v4 queue scanning could mistake the v5 ticket as stale"

    # The deployed v4 scan parses each live queue name as an integer; a locked
    # v5 ticket remains a normal numbered ticket and is left in FIFO order.
    v4_waiting = [path for path in sorted(tickets.iterdir()) if ci_node.locked(path)]
    assert [int(path.name) for path in v4_waiting] == [1, record["ticket"]]

    status = node.call("status", **key(record))
    pid = int(status["remote_run"]["pid"])
    os.kill(pid, signal.SIGKILL)
    holder_lock = Path(record["run_directory"]) / "holder.lock"
    deadline = time.monotonic() + 5
    while ci_node.locked(holder_lock) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not ci_node.locked(holder_lock)

    outcomes = tmp_path / "admitter-outcomes"
    outcomes.mkdir()
    start_gate = tmp_path / "start-admitter-race"
    pids = []
    start_deadline = time.monotonic() + 5.0
    cancel_attempted = False
    try:
        for attempt in range(2):
            pid = os.fork()
            if pid == 0:
                try:
                    while not start_gate.exists():
                        remaining = start_deadline - time.monotonic()
                        if remaining <= 0:
                            os._exit(2)
                        time.sleep(min(0.005, remaining))
                    result = ci_node.admit_once(managed)
                    (outcomes / str(attempt)).write_text(json.dumps(result), encoding="utf-8")
                    os._exit(0)
                except BaseException:
                    os._exit(1)
            pids.append(pid)
        start_gate.touch()
        for pid in pids:
            status = _waitpid_bounded(pid)
            assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
        passes = [
            json.loads((outcomes / str(attempt)).read_text(encoding="utf-8"))
            for attempt in range(2)
        ]
        started = sum(
            1 for events in passes for ticket, result in events
            if ticket == record["ticket"] and result == "holder-started"
        )
        assert started == 1
        assert ci_node.locked(v5_ticket)

        cancelled = node.call("cancel", **key(record), expected_owner=_expected_owner(record))
        cancel_attempted = True
        assert cancelled["status"] == "cancelled"
        assert legacy_ticket.exists() and ci_node.locked(legacy_ticket)
    finally:
        cleanup_errors = []
        for pid in pids:
            # The test owns each direct fork child. Remove it even when an
            # assertion or the gate wait fails; ticket cancellation above
            # owns any detached run-holder process it may have started.
            while True:
                try:
                    reaped, _status = os.waitpid(pid, os.WNOHANG)
                    break
                except InterruptedError:
                    continue
                except (ProcessLookupError, ChildProcessError):
                    reaped = pid
                    break
                except OSError as exc:
                    cleanup_errors.append(f"child wait probe for {pid}: {exc}")
                    reaped = 0
                    break
            if reaped:
                continue
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError as exc:
                if not isinstance(exc, ProcessLookupError):
                    cleanup_errors.append(f"child termination for {pid}: {exc}")
            try:
                _waitpid_bounded(pid, timeout=5)
            except AssertionError as exc:
                cleanup_errors.append(str(exc))
        if not cancel_attempted:
            try:
                node.call("cancel", **key(record), expected_owner=_expected_owner(record))
            except Exception as exc:
                cleanup_errors.append(f"ticket cancellation: {exc}")
        legacy_lock.close()
        legacy_ticket.unlink(missing_ok=True)
        shutil.rmtree(legacy_run, ignore_errors=True)
        if cleanup_errors:
            raise AssertionError("forked admitter cleanup failed: " + "; ".join(cleanup_errors))


def test_ticket_basis_hold_survives_repo_gc_and_releases_on_cancel_and_finish(node_env):
    _require_launchctl()
    config, executor, _runner, node = node_env
    seeded = _wait_for_state(node, enqueue(node, "basis-seed"), {"admitted"})
    start(node, seeded, "pass")
    _wait_for_state(node, seeded, {"released"})
    basis = node.call("bundle-basis", repo=config.repo)["commit"]
    assert basis

    queued, _sha = _submit(
        node, config, "basis-cancel", max_seats=0, basis_commit=basis,
    )
    ref = _ticket_ref(executor.root, config.repo, queued["lease_id"])
    assert ref.is_file()
    repo_root = executor.root / "repos" / config.repo
    mirror = repo_root / "mirror.git"
    with ci_node._repo_pool_lock(executor.root, config.repo) as (_repo_root, lock_fd):
        gc_result = ci_node._repo_gc_locked(
            executor.root, config.repo, repo_root, mirror, lock_fd,
        )
    assert gc_result["status"] == "complete"
    assert ref.is_file()
    assert subprocess.run(
        ["git", "--git-dir", str(mirror), "cat-file", "-e", f"{basis}^{{commit}}"],
        check=False,
    ).returncode == 0

    cancelled = node.call("cancel", **key(queued), expected_owner=_expected_owner(queued))
    assert cancelled["status"] == "cancelled"
    assert not ref.exists()

    finished, _sha = _submit(
        node, config, "basis-finish", max_seats=1, basis_commit=basis,
    )
    finished = _wait_for_state(node, finished, {"released"})
    finished_ref = _ticket_ref(executor.root, config.repo, finished["lease_id"])
    # _mark_released publishes state=released, drops the queue lock, then
    # _release_bundle_basis_pin takes the pool lock and deletes the ref.
    pin_deadline = time.monotonic() + ci_node.POOL_LOCK_OPERATION_TIMEOUT_SECONDS
    while finished_ref.exists() and time.monotonic() < pin_deadline:
        time.sleep(0.02)
    assert not finished_ref.exists(), (
        "resident bundle-basis pin still present "
        f"{ci_node.POOL_LOCK_OPERATION_TIMEOUT_SECONDS:g}s after the ticket reached released"
    )


def test_basis_pin_is_present_at_released_until_pin_release_runs(tmp_path, monkeypatch):
    managed = tmp_path / "managed"
    admission = managed / "admission"
    lease_id = "f" * 32
    run = managed / "runs" / lease_id
    admission.mkdir(parents=True)
    run.mkdir(parents=True)
    source, basis, _temporary = _test_source()
    repo = "test-repo"
    mirror = managed / "repos" / repo / "mirror.git"
    mirror.parent.mkdir(parents=True)
    _git(source, "clone", "--mirror", str(source), str(mirror))
    ci_node.write_json(run / "lease.json", {
        "lease_id": lease_id, "lease_token": "lease-token",
        "repo": repo, "state": "running", "resident_submission": True,
        "pending_result": {"status": "completed", "returncode": 0},
    })
    ci_node.write_json(run / "owner.json", {"owner_identity": "pin-race"})
    assert ci_node.pin_bundle_basis(
        managed, repo, lease_id, "lease-token", basis_commit=basis,
    ) == basis.lower()
    pin = _ticket_ref(managed, repo, lease_id)
    assert pin.is_file()

    release_entered = threading.Event()
    continue_release = threading.Event()
    real_release = ci_node._release_bundle_basis_pin

    def delayed_pin_release(managed_root, lease):
        release_entered.set()
        assert continue_release.wait(20), (
            "the injection timed out: pin-release hold gave up before the test "
            "signalled continuation"
        )
        return real_release(managed_root, lease)

    monkeypatch.setattr(ci_node, "clear_tree", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(ci_node, "cwd_intruders", lambda _paths, **_kwargs: [])
    monkeypatch.setattr(ci_node, "cwd_snapshot", lambda: [])
    monkeypatch.setattr(ci_node, "_release_bundle_basis_pin", delayed_pin_release)
    with ThreadPoolExecutor(max_workers=1) as pool:
        finishing = pool.submit(
            ci_node.finish_dead_workload, admission, run, {}, "operator-clear",
        )
        try:
            assert release_entered.wait(2), "pin release never started"
            assert ci_node.read_json(run / "lease.json")["state"] == "released"
            assert pin.is_file()
        finally:
            continue_release.set()
        pin_deadline = time.monotonic() + ci_node.POOL_LOCK_OPERATION_TIMEOUT_SECONDS
        while pin.exists() and time.monotonic() < pin_deadline:
            time.sleep(0.02)
        assert not pin.exists(), (
            "resident bundle-basis pin still present "
            f"{ci_node.POOL_LOCK_OPERATION_TIMEOUT_SECONDS:g}s after the ticket reached released"
        )
        assert finishing.result(timeout=5)["status"] == "cleared"


@pytest.mark.parametrize("pre_admission_state", ["queued", "submitting", "uploading"])
def test_ticket_expiration_and_wall_clock_jump_use_awake_time(tmp_path, monkeypatch, pre_admission_state):
    state = {
        "ticket_awake_started": 100.0,
        "ticket_boot_session": "boot-one",
    }
    monkeypatch.setattr(ci_node.time, "time", lambda: 100.0 + 3600)
    monkeypatch.setattr(ci_node, "awake_monotonic", lambda: 101.0)
    expired, reset = ci_node.submitted_ticket_expired(state, boot_session="boot-one")
    assert not expired and not reset

    managed = tmp_path / "managed"
    admission = managed / "admission"
    tickets = admission / "tickets"
    run = managed / "runs" / ("2" * 32)
    tickets.mkdir(parents=True)
    run.mkdir(parents=True)
    for name in ("stdout", "stderr"):
        (run / name).touch()
    ci_node.write_json(admission / "policy.json", {"p_cores": 4, "token_pool_size": 1})
    ci_node.write_json(run / "owner.json", {"owner_identity": "test-owner"})
    ci_node.write_json(run / "lease.json", {
        "schema": "goalflight.remote-ci.lease.v1",
        "lease_id": run.name,
        "lease_token": "lease-token",
        "box": "box-a",
        "managed_run_directory": str(managed),
        "run_directory": str(run),
        "state": pre_admission_state,
        "ticket": 1,
        "request_id": "abandoned",
        "arm": "candidate",
        "repo": "test-repo",
        "max_concurrent_seats": 1,
        "resident_submission": True,
        "ticket_awake_started": 1.0,
        "ticket_boot_session": "boot-one",
    })
    ci_node.write_json(run / "request.json", {"resident_submission": True})
    ci_node.write_json(run / "command.json", {})
    (tickets / f"{1:020d}").touch()

    assert ci_node.reserved_token_indexes(managed, token_pool_size=1) == (set(), None)

    monkeypatch.setattr(ci_node, "awake_monotonic", lambda: 1.0 + ci_node.SUBMITTED_TICKET_TTL_SECONDS + 1)
    events = ci_node._admit_once(managed, boot_session="boot-one")
    assert (1, "expired") in events
    assert ci_node.read_json(run / "lease.json")["state"] == "released"
    assert ci_node.read_json(run / "result.json")["status"] == "expired"
    assert not (tickets / f"{1:020d}").exists()


def test_admitter_single_instance_flock(tmp_path):
    managed = tmp_path / "managed"
    admission = managed / "admission"
    admission.mkdir(parents=True)
    first = ci_node.try_lock(admission / "admit-loop.lock")
    assert first is not None
    assert ci_node.admit_once(managed) == [(None, "already-running")]
    first.close()
    assert ci_node.admit_once(managed) == []


def test_admitter_queue_lock_wait_is_bounded(tmp_path, monkeypatch):
    managed = tmp_path / "managed"
    admission = managed / "admission"
    admission.mkdir(parents=True)
    queue_lock = (admission / "queue.lock").open("a+")
    ci_node.fcntl.flock(queue_lock, ci_node.fcntl.LOCK_EX)
    monkeypatch.setattr(ci_node, "POOL_LOCK_OPERATION_TIMEOUT_SECONDS", 0.05)
    started = time.monotonic()
    try:
        with pytest.raises(ci_node.SetupDeadlineExceeded, match="queue lock"):
            ci_node._admit_once(managed)
    finally:
        queue_lock.close()

    assert time.monotonic() - started < 1


def test_recovered_holder_does_not_keep_admitter_singleton_lock(tmp_path, monkeypatch):
    managed = tmp_path / "managed"
    admission = managed / "admission"
    (admission / "tickets").mkdir(parents=True)
    run = managed / "runs" / ("4" * 32)
    run.mkdir(parents=True)
    ci_node.write_json(run / "lease.json", {
        "state": "queued", "resident_submission": True, "ticket": 1,
        "ticket_awake_started": ci_node.awake_monotonic(),
        "ticket_boot_session": ci_node._boot_session_id(),
    })
    ci_node.write_json(run / "request.json", {"resident_submission": True})
    ci_node.write_json(run / "command.json", {})
    child_file = tmp_path / "holder-pid"

    def holder(*_args):
        ci_node.write_json(child_file, os.getpid())
        while True:
            time.sleep(0.05)

    monkeypatch.setattr(ci_node, "run_holder", holder)
    assert (1, "holder-started") in ci_node.admit_once(managed)
    deadline = time.monotonic() + 10
    while not child_file.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert child_file.exists()
    pid = ci_node.read_json(child_file)
    try:
        assert ci_node.locked(run / "holder.lock")
        replacement = ci_node.try_lock(admission / "admit-loop.lock")
        assert replacement is not None, "surviving holder inherited the singleton flock"
        replacement.close()
        assert ci_node.admit_once(managed) == []
    finally:
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)


def test_admit_loop_reaps_finished_recovery_holders(tmp_path, monkeypatch):
    managed = tmp_path / "managed"
    admission = managed / "admission"
    (admission / "tickets").mkdir(parents=True)
    run = managed / "runs" / ("5" * 32)
    run.mkdir(parents=True)
    ci_node.write_json(run / "lease.json", {
        "state": "queued", "resident_submission": True, "ticket": 1,
        "ticket_awake_started": ci_node.awake_monotonic(),
        "ticket_boot_session": ci_node._boot_session_id(),
    })
    ci_node.write_json(run / "request.json", {"resident_submission": True})
    ci_node.write_json(run / "command.json", {})
    monkeypatch.setattr(ci_node, "pin_managed_root", lambda path: path)
    monkeypatch.setattr(ci_node, "run_holder", lambda *_args: None)
    admit_once = ci_node._admit_once
    pause = time.sleep
    children = []
    remaining = []

    class StopLoop(BaseException):
        pass

    def admission_pass(*args, **kwargs):
        if children:
            remaining.extend(kwargs["holder_pids"])
            raise StopLoop
        events = admit_once(*args, **kwargs)
        assert events == [(1, "holder-started")]
        children.extend(kwargs["holder_pids"])
        assert len(children) == 1
        return events

    def await_child_exit(_seconds):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                result = subprocess.run(
                    ["ps", "-o", "stat=", "-p", str(children[0])],
                    capture_output=True, text=True, check=True,
                )
            except PermissionError:
                pytest.skip("sandbox denies exec of ps")
            except FileNotFoundError:
                pytest.skip("ps is not installed on this host")
            except subprocess.CalledProcessError as exc:
                output = f"{exc.stdout or ''}\n{exc.stderr or ''}".lower()
                if "operation not permitted" in output or "permission denied" in output:
                    pytest.skip("sandbox denies ps process inspection")
                raise
            status = result.stdout.strip()
            if status.startswith("Z"):
                return
            pause(0.01)
        raise AssertionError("recovery holder did not exit")

    monkeypatch.setattr(ci_node, "_admit_once", admission_pass)
    monkeypatch.setattr(ci_node.time, "sleep", await_child_exit)
    try:
        with _independent_deadline(), pytest.raises(StopLoop):
            ci_node.admit_loop(managed, interval=60)
        assert remaining == []
        with pytest.raises(ChildProcessError):
            os.waitpid(children[0], os.WNOHANG)
    finally:
        for pid in children:
            try:
                reaped, _status = os.waitpid(pid, os.WNOHANG)
                if not reaped:
                    os.kill(pid, signal.SIGKILL)
                    os.waitpid(pid, 0)
            except ChildProcessError:
                pass


def test_admit_loop_retries_non_echild_waitpid_errors(tmp_path, monkeypatch):
    managed = tmp_path / "managed"
    calls = []

    class StopLoop(BaseException):
        pass

    def interrupted_waitpid(_pid, _options):
        raise OSError(errno.EINTR, "interrupted")

    def admission_pass(_managed, *, holder_pids, **_kwargs):
        calls.append(tuple(holder_pids))
        if len(calls) == 1:
            holder_pids.add(12345)
            return []
        assert holder_pids == {12345}
        raise StopLoop

    # ci_node.os is the shared os module; isolate the boot lookup before
    # replacing waitpid so subprocess internals cannot inherit this stub.
    monkeypatch.setattr(ci_node, "_boot_session_id", lambda: "test-boot-session")
    monkeypatch.setattr(ci_node.os, "waitpid", interrupted_waitpid)
    monkeypatch.setattr(ci_node, "_admit_once", admission_pass)
    monkeypatch.setattr(ci_node, "pin_managed_root", lambda path: Path(path))
    monkeypatch.setattr(ci_node.time, "sleep", lambda _seconds: None)
    with _independent_deadline(), pytest.raises(StopLoop):
        ci_node.admit_loop(managed, interval=0)
    assert calls == [(), (12345,)]


def test_v4_gc_cannot_remove_resident_result_or_streams(node_env):
    _require_launchctl()
    config, executor, _runner, node = node_env
    record, _sha = _submit(node, config, "v4-retention", code="print('resident-retained')")
    _wait_for_state(node, record, {"released"})
    original = Path(record["run_directory"])
    retained = executor.root / "resident-results" / record["lease_id"]
    assert (original / "stdout").samefile(retained / "stdout")
    deadline = time.monotonic() + 10
    while ci_node.locked(original / "holder.lock") and time.monotonic() < deadline:
        time.sleep(0.02)
    original_lease = (original / "lease.json").read_bytes()
    (original / "lease.json").unlink()  # A legacy GC can be interrupted between unlinks.
    try:
        assert not (retained / "holder.lock").exists()
        assert node.call("status", **key(record))["state"] == "released"
        assert not (retained / "holder.lock").exists()
        assert any(row.get("lease_id") == record["lease_id"] for row in node.call("list"))
    finally:
        (original / "lease.json").write_bytes(original_lease)
    _deployed_v4_node()["gc_run_bodies"](
        executor.root,
        {"retain_max_count": 0, "retain_max_age_seconds": 0,
         "retain_failure_count": 0, "retain_failure_age_seconds": 0}, True,
    )
    assert not original.exists()
    capture = (retained / "stdout").read_text()
    assert "resident-retained\n" in capture
    status = node.call("status", **key(record))
    assert status["command_present"] is True
    assert status["result"]["streams"]["stdout"]["tail"]["text"] == capture
    fetched = node.call("fetch", **key(record), stream="stdout", last_kb=1)
    assert fetched["size_bytes"] == min(1024, (retained / "stdout").stat().st_size)
    assert any(row["lease_id"] == record["lease_id"] for row in node.call("list"))


def test_gc_keeps_results_and_streams_for_24_hours_after_finish(tmp_path):
    managed = tmp_path / "managed"
    run = managed / "resident-results" / ("3" * 32)
    run.mkdir(parents=True)
    now = time.time()
    state = {
        "lease_id": run.name,
        "state": "released",
        "acquired_at": now - 3600,
        "resident_submission": True,
        "run_directory": str(managed / "runs" / run.name),
    }
    ci_node.write_json(run / "lease.json", state)
    ci_node.write_json(run / "result.json", {"returncode": 0, "status": "completed"})
    (run / "stdout").write_text("retained output", encoding="utf-8")
    (run / "stderr").write_text("", encoding="utf-8")
    index = managed / "results" / "index.jsonl"
    index.parent.mkdir(parents=True)
    index.write_text(json.dumps({
        "run_id": run.name,
        "finish": now - 3600,
    }) + "\n", encoding="utf-8")

    # Execute the actual deployed v4 GC, which has no 24-hour count floor.
    # It must prune legacy bodies while being unable to see resident bodies.
    legacy = _deployed_v4_node()
    with index.open("a") as stream:
        for number in range(21):
            old = managed / "runs" / f"{number:032x}"
            old.mkdir(parents=True)
            ci_node.write_json(old / "lease.json", {
                "lease_id": old.name, "state": "released", "acquired_at": now - 100 + number,
            })
            ci_node.write_json(old / "result.json", {"returncode": 0})
            stream.write(json.dumps({"run_id": old.name, "finish": now - 100 + number}) + "\n")
    legacy_rows = legacy["gc_run_bodies"](managed, {}, True)
    assert any(row["class"] == "ELIGIBLE" for row in legacy_rows)
    assert (run / "stdout").read_text() == "retained output"
    assert (run / "result.json").is_file()

    young = ci_node.gc_run_bodies(
        managed,
        {"retain_max_count": 0, "retain_max_age_seconds": 0,
         "retain_failure_count": 0, "retain_failure_age_seconds": 0},
        True,
    )
    assert not any(row.get("path") == str(run) and row["class"] == "ELIGIBLE" for row in young)
    assert (run / "stdout").read_text(encoding="utf-8") == "retained output"

    index.write_text(json.dumps({
        "run_id": run.name,
        "finish": now - ci_node.MIN_RESULT_RETENTION_SECONDS - 1,
    }) + "\n", encoding="utf-8")
    original = Path(state["run_directory"])
    original.mkdir(parents=True)
    ci_node.write_json(original / "lease.json", {**state, "state": "draining"})
    ci_node.gc_run_bodies(
        managed, {"retain_max_count": 0, "retain_max_age_seconds": 0}, True,
    )
    assert run.exists(), "GC removed the snapshot before release was published"
    ci_node.write_json(original / "lease.json", state)
    old = ci_node.gc_run_bodies(
        managed,
        {"retain_max_count": 0, "retain_max_age_seconds": 0,
         "retain_failure_count": 0, "retain_failure_age_seconds": 0},
        True,
    )
    assert any(row.get("path") == str(run) and row["class"] == "ELIGIBLE" for row in old)
    assert not run.exists()
    assert not original.exists()


def test_node_agent_dry_run_prints_steps_without_touching_files(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    script = repo / "scripts" / "install-remote-ci-node-agent.sh"
    home = tmp_path / "home"
    home.mkdir()
    config = tmp_path / "remote-ci.json"
    config.write_text(json.dumps({
        "boxes": {
            "studio-dry-run": {
                "host": "studio-dry-run",
                "expected_hostname": "studio-dry-run-host",
                "python_interpreter": "/opt/homebrew/bin/python3",
                "managed_run_directory": "/Users/test/.goal-flight/remote-ci-managed",
            },
        },
    }), encoding="utf-8")
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["TMPDIR"] = str(tmp_path)
    env["GOALFLIGHT_REMOTE_CI_CONFIG"] = str(config)

    result = subprocess.run(
        [str(script), "studio-dry-run", "--dry-run"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "Pinned commit:" in result.stdout
    assert "ssh studio-dry-run hostname" in result.stdout
    assert "git -C " in result.stdout and "archive --format=tar" in result.stdout
    assert "launchctl" in result.stdout
    assert "~/.goal-flight/remote-ci-managed/logs/remote-ci-node.log" in result.stdout
    assert list(home.iterdir()) == []


def test_node_agent_upgrade_replaces_current_symlink_and_verifies_target(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    home = tmp_path / "home"
    node_root = home / ".goal-flight" / "remote-ci-node"
    old = node_root / "old-commit"
    old.mkdir(parents=True)
    current = node_root / "current"
    current.symlink_to(old, target_is_directory=True)
    commands = tmp_path / "bin"
    commands.mkdir()
    # Run the installer end-to-end against a local fake SSH host. Nothing can
    # contact a box or invoke launchctl outside this temporary HOME.
    (commands / "ssh").write_text('#!/bin/sh\nshift\nexec /bin/sh -c "$*"\n')
    (commands / "scp").write_text(
        "#!" + sys.executable + "\nimport shutil,sys\n"
        "shutil.copyfile(sys.argv[1],sys.argv[2].split(':',1)[1])\n"
    )
    for name in ("launchctl", "plutil"):
        (commands / name).write_text("#!/bin/sh\nexit 0\n")
    for path in commands.iterdir():
        path.chmod(0o755)
    config = tmp_path / "remote-ci.json"
    config.write_text(json.dumps({"boxes": {"local-fake": {
        "host": "local-fake", "python_interpreter": sys.executable,
        "managed_run_directory": str(home / "managed"),
    }}}))
    env = {**os.environ, "HOME": str(home), "TMPDIR": str(tmp_path),
           "PATH": str(commands) + os.pathsep + os.environ["PATH"],
           "GOALFLIGHT_REMOTE_CI_CONFIG": str(config)}
    commit = _git(repo, "rev-parse", "HEAD")
    for _ in range(2):  # An upgrade, then an idempotent reinstall.
        result = subprocess.run(
            [str(repo / "scripts" / "install-remote-ci-node-agent.sh"), "local-fake"],
            env=env, capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, result.stderr
        assert current.is_symlink() and current.resolve() == node_root / commit
        assert f"Current checkout resolves to: {node_root / commit}" in result.stdout
        assert list(old.iterdir()) == [], "temporary link was moved inside the old directory"


@pytest.mark.parametrize("release_ok", [False, True])
def test_finish_dead_workload_reports_unknown_basis_pin_release(
    tmp_path, monkeypatch, capfd, release_ok,
):
    managed = tmp_path / "managed"
    admission = managed / "admission"
    run = managed / "runs" / ("a" * 32)
    admission.mkdir(parents=True)
    run.mkdir(parents=True)
    lease = {"lease_id": run.name, "repo": "test-repo", "state": "running"}
    ci_node.write_json(run / "lease.json", lease)
    monkeypatch.setattr(ci_node, "clear_tree", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(ci_node, "cwd_intruders", lambda _paths, **_kwargs: [])
    monkeypatch.setattr(ci_node, "cwd_snapshot", lambda: [])
    monkeypatch.setattr(
        ci_node, "_mark_released",
        lambda _managed, _run, current, _reason, **_kwargs: current,
    )
    monkeypatch.setattr(ci_node, "_release_bundle_basis_pin", lambda *_args: release_ok)

    _run_admit_loop_once(managed, monkeypatch)
    assert capfd.readouterr() == ("", "")

    result = ci_node.finish_dead_workload(admission, run, {}, "operator-clear")

    assert result == {"status": "cleared", "killed": True}
    audit_path = admission / "audit.log"
    audit_rows = (
        [json.loads(row) for row in audit_path.read_text().splitlines()]
        if audit_path.exists() else []
    )
    pin_rows = [row for row in audit_rows if row.get("action") == "bundle-basis-pin-release"]
    if release_ok:
        assert pin_rows == []
    else:
        assert len(pin_rows) == 1
        assert pin_rows[0] == {
            "action": "bundle-basis-pin-release", "result": "unknown",
            "lease_id": run.name, "source": "finish-dead-workload",
            "at": pin_rows[0]["at"],
        }
    assert capfd.readouterr() == ("", "")
    _run_admit_loop_once(managed, monkeypatch)
    stdout, stderr = capfd.readouterr()
    if release_ok:
        assert "bundle-basis-pin-release" not in stdout + stderr
    else:
        message = f"bundle-basis-pin-release=unknown lease_id={run.name}"
        assert f"admit-loop {message} source=finish-dead-workload" in stdout
        assert message not in stderr
        assert (stdout + stderr).count(message) == 1
    _run_admit_loop_once(managed, monkeypatch)
    assert capfd.readouterr() == ("", "")


@pytest.mark.parametrize("release_ok", [False, True])
def test_release_holder_reports_unknown_basis_pin_release(
    tmp_path, monkeypatch, capfd, release_ok,
):
    managed = tmp_path / "managed"
    admission = managed / "admission"
    run = managed / "runs" / ("b" * 32)
    admission.mkdir(parents=True)
    run.mkdir(parents=True)
    lease = {"lease_id": run.name, "repo": "test-repo", "state": "draining"}
    ci_node.write_json(run / "lease.json", lease)
    monkeypatch.setattr(ci_node, "cwd_intruders", lambda _paths, **_kwargs: [])
    monkeypatch.setattr(ci_node, "cwd_snapshot", lambda: [])
    monkeypatch.setattr(ci_node, "_publish_pending", lambda *_args: None)
    monkeypatch.setattr(
        ci_node, "_mark_released",
        lambda _managed, _run, current, _reason, **_kwargs: current,
    )
    monkeypatch.setattr(ci_node, "_release_bundle_basis_pin", lambda *_args: release_ok)

    class Token:
        def close(self):
            pass

    _run_admit_loop_once(managed, monkeypatch)
    assert capfd.readouterr() == ("", "")

    ci_node._release_holder(admission, run, {}, Token())

    audit_path = admission / "audit.log"
    audit_rows = (
        [json.loads(row) for row in audit_path.read_text().splitlines()]
        if audit_path.exists() else []
    )
    pin_rows = [row for row in audit_rows if row.get("action") == "bundle-basis-pin-release"]
    if release_ok:
        assert pin_rows == []
    else:
        assert len(pin_rows) == 1
        assert pin_rows[0]["result"] == "unknown"
        assert pin_rows[0]["lease_id"] == run.name
        assert pin_rows[0]["source"] == "holder-release"
    assert capfd.readouterr() == ("", "")
    _run_admit_loop_once(managed, monkeypatch)
    stdout, stderr = capfd.readouterr()
    if release_ok:
        assert "bundle-basis-pin-release" not in stdout + stderr
    else:
        message = f"bundle-basis-pin-release=unknown lease_id={run.name}"
        assert f"admit-loop {message} source=holder-release" in stdout
        assert message not in stderr
        assert (stdout + stderr).count(message) == 1
    _run_admit_loop_once(managed, monkeypatch)
    assert capfd.readouterr() == ("", "")


@pytest.mark.parametrize("source", ["finish-dead-workload", "holder-release"])
def test_existing_pin_release_reporter_stderr_is_only_audit_failure_fallback(
    tmp_path, monkeypatch, capfd, source,
):
    lease = {"lease_id": "2" * 32}
    monkeypatch.setattr(ci_node, "audit", lambda *_args: None)
    ci_node._report_bundle_basis_pin_release_unknown(tmp_path, lease, source=source)
    assert capfd.readouterr() == ("", "")

    def fail_audit(*_args):
        raise OSError("audit unavailable")

    monkeypatch.setattr(ci_node, "audit", fail_audit)
    ci_node._report_bundle_basis_pin_release_unknown(tmp_path, lease, source=source)
    stdout, stderr = capfd.readouterr()
    assert stdout == ""
    assert stderr == (
        "remote-ci bundle-basis-pin-release=unknown "
        f"lease_id={lease['lease_id']} source={source}\n"
    )


@pytest.mark.parametrize("release_ok", [False, True])
def test_tokenless_release_holder_reports_unknown_basis_pin_release(
    tmp_path, monkeypatch, capfd, release_ok,
):
    managed = tmp_path / "managed"
    admission = managed / "admission"
    run = managed / "runs" / ("d" * 32)
    admission.mkdir(parents=True)
    run.mkdir(parents=True)
    lease = {"lease_id": run.name, "repo": "test-repo", "state": "queued"}
    ci_node.write_json(run / "lease.json", lease)
    ci_node.write_json(run / "result.json", {"status": "cancelled"})
    monkeypatch.setattr(
        ci_node, "_mark_released",
        lambda _managed, _run, current, _reason, **_kwargs: current,
    )
    monkeypatch.setattr(ci_node, "_release_bundle_basis_pin", lambda *_args: release_ok)

    _run_admit_loop_once(managed, monkeypatch)
    assert capfd.readouterr() == ("", "")
    ci_node._release_holder(
        admission, run, {"lease_id": run.name, "token_index": None}, None,
    )

    assert ci_node.read_json(run / "result.json") == {"status": "cancelled"}
    audit_path = admission / "audit.log"
    audit_rows = (
        [json.loads(row) for row in audit_path.read_text().splitlines()]
        if audit_path.exists() else []
    )
    pin_rows = [row for row in audit_rows if row.get("action") == "bundle-basis-pin-release"]
    if release_ok:
        assert pin_rows == []
    else:
        assert len(pin_rows) == 1
        assert pin_rows[0]["result"] == "unknown"
        assert pin_rows[0]["lease_id"] == run.name
        assert pin_rows[0]["source"] == "tokenless-holder-release"
    _run_admit_loop_once(managed, monkeypatch)
    stdout, stderr = capfd.readouterr()
    if release_ok:
        assert "bundle-basis-pin-release" not in stdout + stderr
    else:
        assert f"admit-loop bundle-basis-pin-release=unknown lease_id={run.name}" in stdout
        assert f"remote-ci bundle-basis-pin-release=unknown lease_id={run.name}" not in stderr
        assert (stdout + stderr).count(
            f"bundle-basis-pin-release=unknown lease_id={run.name}"
        ) == 1
    _run_admit_loop_once(managed, monkeypatch)
    assert capfd.readouterr() == ("", "")


@pytest.mark.parametrize("release_ok", [False, True])
def test_run_holder_reports_unknown_for_reclaimed_basis_pin_release(
    tmp_path, monkeypatch, capfd, release_ok,
):
    managed = tmp_path / "managed"
    root = managed / "admission"
    run = managed / "runs" / ("e" * 32)
    (root / "tickets").mkdir(parents=True)
    run.mkdir(parents=True)
    ci_node.write_json(run / "lease.json", {
        "lease_id": run.name, "lease_token": "current-token", "repo": "test-repo",
        "state": "queued", "destination_address": "192.0.2.10",
    })
    reclaimed = {"lease_id": "f" * 32, "repo": "test-repo"}
    monkeypatch.setattr(ci_node.os, "setsid", lambda: None)
    monkeypatch.setattr(ci_node, "reclaim_dead_prestart", lambda *_args: [reclaimed])
    monkeypatch.setattr(ci_node, "_release_holder", lambda *_args: None)
    monkeypatch.setattr(
        ci_node, "_release_bundle_basis_pin", lambda *_args: release_ok,
    )
    _run_admit_loop_once(managed, monkeypatch)
    assert capfd.readouterr() == ("", "")

    class Handle:
        def close(self):
            pass

    saved_fds = [os.dup(fd) for fd in range(3)]
    try:
        with open(os.devnull, "r+") as null, monkeypatch.context() as hidden_stdio:
            hidden_stdio.setattr(ci_node.sys, "stdin", null)
            hidden_stdio.setattr(ci_node.sys, "stdout", null)
            hidden_stdio.setattr(ci_node.sys, "stderr", null)
            for fd in range(3):
                os.dup2(null.fileno(), fd)
            ci_node.run_holder(
                root, run, root / "tickets" / "00000000000000000001",
                Handle(), Handle(),
                {"queue_wait_seconds": 0, "resident_submission": False},
            )
    finally:
        for fd, saved in enumerate(saved_fds):
            os.dup2(saved, fd)
            os.close(saved)

    assert ci_node.read_json(run / "result.json")["status"] == "capacity-refused"
    audit_path = root / "audit.log"
    audit_rows = (
        [json.loads(row) for row in audit_path.read_text().splitlines()]
        if audit_path.exists() else []
    )
    pin_rows = [row for row in audit_rows if row.get("action") == "bundle-basis-pin-release"]
    if release_ok:
        assert pin_rows == []
    else:
        assert len(pin_rows) == 1
        assert pin_rows[0]["result"] == "unknown"
        assert pin_rows[0]["lease_id"] == reclaimed["lease_id"]
        assert pin_rows[0]["source"] == "run-holder-reclaimed"
    _run_admit_loop_once(managed, monkeypatch)
    stdout, stderr = capfd.readouterr()
    if release_ok:
        assert "bundle-basis-pin-release" not in stdout + stderr
    else:
        assert (
            f"admit-loop bundle-basis-pin-release=unknown "
            f"lease_id={reclaimed['lease_id']}"
        ) in stdout
        assert "remote-ci bundle-basis-pin-release=unknown" not in stderr
        assert (stdout + stderr).count(
            f"bundle-basis-pin-release=unknown lease_id={reclaimed['lease_id']}"
        ) == 1
    _run_admit_loop_once(managed, monkeypatch)
    assert capfd.readouterr() == ("", "")


@pytest.mark.parametrize("release_ok", [False, True])
def test_queued_cancel_reports_unknown_basis_pin_release(
    tmp_path, monkeypatch, capfd, release_ok,
):
    managed = tmp_path / "managed"
    monkeypatch.setattr(
        ci_node.builtins, "_GOALFLIGHT_REMOTE_CI_AUTHORITY",
        str(tmp_path / "authority.json"), raising=False,
    )
    run = managed / "runs" / ("1" * 32)
    run.mkdir(parents=True)
    owner = {"owner_identity": "test-owner"}
    ci_node.write_json(run / "owner.json", owner)
    ci_node.write_json(run / "lease.json", {
        "lease_id": run.name, "lease_token": "cancel-token", "repo": "test-repo",
        "state": "queued", "resident_submission": True, "ticket": 1,
    })
    monkeypatch.setattr(ci_node, "_release_bundle_basis_pin", lambda *_args: release_ok)
    _run_admit_loop_once(managed, monkeypatch)
    assert capfd.readouterr() == ("", "")

    result = ci_node.dispatch({
        "managed_root": str(managed), "p_cores": 2, "token_pool_size": 1,
        "operation": "cancel", "run_dir": str(run),
        "lease_token": "cancel-token", "expected_owner": owner,
    })

    assert result == {"status": "cancelled"}
    assert ci_node.read_json(run / "result.json")["status"] == "cancelled"
    _run_admit_loop_once(managed, monkeypatch)
    stdout, stderr = capfd.readouterr()
    if release_ok:
        assert "bundle-basis-pin-release" not in stdout + stderr
    else:
        assert f"admit-loop bundle-basis-pin-release=unknown lease_id={run.name}" in stdout
        assert f"remote-ci bundle-basis-pin-release=unknown lease_id={run.name}" not in stderr
        message = f"bundle-basis-pin-release=unknown lease_id={run.name}"
        assert (stdout + stderr).count(message) == 1
    audit_path = managed / "admission" / "audit.log"
    audit_rows = (
        [json.loads(row) for row in audit_path.read_text().splitlines()]
        if audit_path.exists() else []
    )
    pin_rows = [row for row in audit_rows if row.get("action") == "bundle-basis-pin-release"]
    if release_ok:
        assert pin_rows == []
    else:
        assert len(pin_rows) == 1
        assert pin_rows[0]["result"] == "unknown"
        assert pin_rows[0]["lease_id"] == run.name
        assert pin_rows[0]["source"] == "queued-cancel"
    _run_admit_loop_once(managed, monkeypatch)
    assert capfd.readouterr() == ("", "")


def test_admit_loop_cursor_survives_restart_and_forwards_new_rows(
    tmp_path, monkeypatch, capfd,
):
    managed = tmp_path / "managed"
    admission = managed / "admission"
    _run_admit_loop_once(managed, monkeypatch)
    assert capfd.readouterr() == ("", "")

    _append_pin_release_event(admission, "old-event")
    _run_admit_loop_once(managed, monkeypatch)
    stdout, stderr = capfd.readouterr()
    assert stderr == ""
    assert stdout.count("lease_id=old-event") == 1

    # A new admit_loop invocation is a daemon restart: the old row stays consumed.
    _run_admit_loop_once(managed, monkeypatch)
    assert capfd.readouterr() == ("", "")

    _append_pin_release_event(admission, "new-event")
    _run_admit_loop_once(managed, monkeypatch)
    stdout, stderr = capfd.readouterr()
    assert stderr == ""
    assert "lease_id=old-event" not in stdout
    assert stdout.count("lease_id=new-event") == 1


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        pytest.param(
            {
                "action": "admission-decision", "result": "project-cap",
                "lease_id": "admission-refusal",
            },
            "action=admission-decision result=project-cap",
            id="admission-refusal",
        ),
        pytest.param(
            {
                "action": "token-reservation", "result": "capacity-refused",
                "reason": "token-ownership-unknown", "lease_id": "reservation-refusal",
            },
            "action=token-reservation result=capacity-refused",
            id="token-reservation-refusal",
        ),
        pytest.param(
            {"action": "future-audit", "lease_id": "missing-result"},
            "action=future-audit result=unknown",
            id="missing-result-is-unknown",
        ),
    ],
)
def test_admit_loop_forwards_non_success_audit_outcomes_once(
    tmp_path, monkeypatch, capfd, event, expected,
):
    managed = tmp_path / "managed"
    admission = managed / "admission"
    monkeypatch.setattr(ci_node, "_boot_session_id", lambda: "test-boot")
    _run_admit_loop_once(managed, monkeypatch)
    assert capfd.readouterr() == ("", "")

    ci_node.audit(admission, event)
    _run_admit_loop_once(managed, monkeypatch)
    stdout, stderr = capfd.readouterr()
    assert expected in stdout
    assert stdout.count("admit-loop audit ") == 1
    assert stderr == ""

    _run_admit_loop_once(managed, monkeypatch)
    stdout, stderr = capfd.readouterr()
    assert expected not in stdout
    assert stderr == ""


def test_admit_loop_does_not_forward_admitted_audit_row(tmp_path, monkeypatch, capfd):
    managed = tmp_path / "managed"
    admission = managed / "admission"
    monkeypatch.setattr(ci_node, "_boot_session_id", lambda: "test-boot")
    _run_admit_loop_once(managed, monkeypatch)
    capfd.readouterr()

    ci_node.audit(admission, {
        "action": "admission-decision", "result": "admitted",
        "lease_id": "accepted-admission",
    })
    _run_admit_loop_once(managed, monkeypatch)
    stdout, stderr = capfd.readouterr()
    assert "accepted-admission" not in stdout
    assert "admit-loop audit " not in stdout
    assert stderr == ""


def test_admit_loop_cursor_resets_for_replaced_audit_log(tmp_path, monkeypatch, capfd):
    managed = tmp_path / "managed"
    admission = managed / "admission"
    _run_admit_loop_once(managed, monkeypatch)
    _append_pin_release_event(admission, "old-event")
    _run_admit_loop_once(managed, monkeypatch)
    capfd.readouterr()
    old_cursor = ci_node.read_json(admission / "audit-forward.cursor.json")

    replacement_root = tmp_path / "replacement"
    replacement_root.mkdir()
    _append_pin_release_event(replacement_root, "replacement-event")
    replacement = replacement_root / "audit.log"
    audit_path = admission / "audit.log"
    old_identity = (old_cursor["device"], old_cursor["inode"])
    os.replace(replacement, audit_path)
    new_stat = audit_path.stat()
    assert (new_stat.st_dev, new_stat.st_ino) != old_identity

    _run_admit_loop_once(managed, monkeypatch)
    stdout, stderr = capfd.readouterr()
    assert stderr == ""
    assert "lease_id=old-event" not in stdout
    assert stdout.count("lease_id=replacement-event") == 1
    cursor = ci_node.read_json(admission / "audit-forward.cursor.json")
    assert (cursor["device"], cursor["inode"]) == (new_stat.st_dev, new_stat.st_ino)
    assert cursor["offset"] == audit_path.stat().st_size


def test_admit_loop_cursor_resets_after_truncation(tmp_path, monkeypatch, capfd):
    managed = tmp_path / "managed"
    admission = managed / "admission"
    _run_admit_loop_once(managed, monkeypatch)
    _append_pin_release_event(admission, "old-event-with-a-long-lease-identifier")
    _run_admit_loop_once(managed, monkeypatch)
    capfd.readouterr()
    old_cursor = ci_node.read_json(admission / "audit-forward.cursor.json")

    new_event = {
        "action": "bundle-basis-pin-release", "result": "unknown",
        "lease_id": "new", "source": "tokenless-holder-release", "at": 2.0,
    }
    new_line = (json.dumps(new_event, sort_keys=True) + "\n").encode()
    assert len(new_line) < old_cursor["offset"]
    audit_path = admission / "audit.log"
    with audit_path.open("wb") as handle:
        handle.write(new_line)

    _run_admit_loop_once(managed, monkeypatch)
    stdout, stderr = capfd.readouterr()
    assert stderr == ""
    assert "lease_id=old-event-with-a-long-lease-identifier" not in stdout
    assert stdout.count("lease_id=new") == 1
    cursor = ci_node.read_json(admission / "audit-forward.cursor.json")
    assert cursor["offset"] == len(new_line)


def test_admit_loop_cursor_past_eof_does_not_skip_new_rows(tmp_path, monkeypatch, capfd):
    managed = tmp_path / "managed"
    admission = managed / "admission"
    _run_admit_loop_once(managed, monkeypatch)
    _append_pin_release_event(admission, "before-past-eof")
    _run_admit_loop_once(managed, monkeypatch)
    capfd.readouterr()

    cursor_path = admission / "audit-forward.cursor.json"
    cursor = ci_node.read_json(cursor_path)
    cursor["offset"] = (admission / "audit.log").stat().st_size + 100
    ci_node.write_json(cursor_path, cursor)

    def append_during_pass(*_args, **_kwargs):
        _append_pin_release_event(admission, "after-past-eof")
        return []

    _run_admit_loop_once(managed, monkeypatch, admit_once=append_during_pass)
    stdout, stderr = capfd.readouterr()
    assert stderr == ""
    assert "lease_id=after-past-eof" in stdout
    saved = ci_node.read_json(cursor_path)
    assert saved["offset"] == (admission / "audit.log").stat().st_size


def test_admit_loop_unreadable_cursor_forwards_from_now_and_warns_once(
    tmp_path, monkeypatch, capfd,
):
    managed = tmp_path / "managed"
    admission = managed / "admission"
    _run_admit_loop_once(managed, monkeypatch)
    _append_pin_release_event(admission, "already-consumed")
    _run_admit_loop_once(managed, monkeypatch)
    capfd.readouterr()

    cursor_path = admission / "audit-forward.cursor.json"
    read_json = ci_node.read_json

    def unreadable_cursor(path):
        if Path(path) == cursor_path:
            raise PermissionError("cursor unreadable")
        return read_json(path)

    monkeypatch.setattr(ci_node, "read_json", unreadable_cursor)

    def append_during_pass(*_args, **_kwargs):
        _append_pin_release_event(admission, "after-unreadable-cursor")
        return []

    _run_admit_loop_once(managed, monkeypatch, admit_once=append_during_pass)
    monkeypatch.setattr(ci_node, "read_json", read_json)
    stdout, stderr = capfd.readouterr()
    assert stdout.count("audit-cursor-unreadable=1") == 1
    assert "forwarding_from=end" in stdout
    assert "lease_id=already-consumed" not in stdout
    assert stdout.count("lease_id=after-unreadable-cursor") == 1
    assert stderr == ""
    repaired = ci_node.read_json(cursor_path)
    assert repaired["offset"] == (admission / "audit.log").stat().st_size

    _run_admit_loop_once(managed, monkeypatch)
    assert capfd.readouterr() == ("", "")


@pytest.mark.parametrize("release_ok", [False, True])
def test_admit_once_surfaces_unknown_basis_pin_release(
    tmp_path, monkeypatch, release_ok,
):
    managed = tmp_path / "managed"
    admission = managed / "admission"
    tickets = admission / "tickets"
    run = managed / "runs" / ("c" * 32)
    tickets.mkdir(parents=True)
    run.mkdir(parents=True)
    lease = {
        "lease_id": run.name, "repo": "test-repo", "state": "queued",
        "resident_submission": True, "ticket": 1, "token_index": None,
    }
    ci_node.write_json(run / "lease.json", lease)
    (run / "release.json").touch()
    (tickets / f"{1:020d}").touch()
    monkeypatch.setattr(
        ci_node, "_finish_queued_submission",
        lambda *_args, **_kwargs: {
            "lease_id": run.name, "repo": "test-repo", "ticket": 1,
        },
    )
    monkeypatch.setattr(ci_node, "_release_bundle_basis_pin", lambda *_args: release_ok)

    events = ci_node._admit_once(managed, boot_session="test-boot")

    assert (1, "cancelled") in events
    pin_events = [event for event in events if "basis-pin-release" in event[1]]
    if release_ok:
        assert pin_events == []
    else:
        assert pin_events == [(
            1, f"bundle-basis-pin-release-unknown lease_id={run.name}",
        )]
