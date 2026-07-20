#!/usr/bin/env python3
"""Hermetic tests for provider quota-stuck detection/advice/reap/reconcile."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import io
import json
import os
import signal
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_acp_client as acp  # noqa: E402
import goalflight_capacity as cap  # noqa: E402
import goalflight_ledger as ledger  # noqa: E402
import goalflight_messages as messages  # noqa: E402
import goalflight_quota_stuck as quota  # noqa: E402
import goalflight_status as status  # noqa: E402


@contextlib.contextmanager
def temp_env(**updates: str):
    old = {key: os.environ.get(key) for key in updates}
    os.environ.update({key: value for key, value in updates.items() if value is not None})
    try:
        yield
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def assert_eq(name: str, got: object, expected: object) -> None:
    if got != expected:
        raise AssertionError(f"{name}: got {got!r}, expected {expected!r}")


def assert_true(name: str, cond: bool) -> None:
    if not cond:
        raise AssertionError(name)


def iso_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def write_tail(path: Path, text: str, *, old_s: float = 600.0) -> None:
    path.write_text(text, encoding="utf-8")
    ts = time.time() - old_s
    os.utime(path, (ts, ts))


def worker_identity(pid: int, *, lstart: str = "Wed Jul  1 12:00:00 2026", comm: str = "grok") -> dict:
    return {
        "pid": pid,
        "ppid": "1",
        "pgid": str(pid),
        "lstart": lstart,
        "comm": comm,
        "args": comm,
    }


def process_row(
    pid: int,
    *,
    ppid: int = 1,
    comm: str = "grok",
    age_s: float = 600.0,
    lstart: str = "Wed Jul  1 12:00:00 2026",
) -> dict:
    return {"pid": pid, "ppid": ppid, "comm": comm, "lstart": lstart, "age_s": age_s}


def quota_record(tmp: Path, *, dispatch_id: str = "q1", pid: int = 101, state: str = "running_quiet") -> dict:
    tail = tmp / f"{dispatch_id}.tail"
    write_tail(tail, "API error (status 402 Payment Required): Grok Build usage balance exhausted\n")
    return {
        "schema": "goalflight.dispatch.v1",
        "dispatch_id": dispatch_id,
        "agent": "grok-code",
        "state": state,
        "worker_pid": pid,
        "worker_identity": worker_identity(pid),
        "worker_pgid": pid,
        "stdout_path": str(tail),
        "project_root": str(tmp),
        "started_at": iso_now(),
        "updated_at": iso_now(),
    }


def test_tail_signature_classifies_rate_limited_provider() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        tail = tmp / "quota.tail"
        write_tail(tail, "ERROR: insufficient_quota; got 429\n")
        payload = {"state": "worker_dead"}
        changed = quota.apply_rate_limited_status(
            payload,
            agent="codex",
            tail=tail,
            previous_state="worker_dead",
            previous_reason="worker_dead_no_terminal_marker",
        )
        assert_true("quota status changed", changed)
        assert_eq("state", payload["state"], "rate_limited")
        assert_eq("provider", payload["rate_limit_provider"], "openai")


def test_capacity_hard_stops_provider_launches() -> None:
    with tempfile.TemporaryDirectory() as td, temp_env(GOALFLIGHT_STATE_DIR=str(Path(td) / "state")):
        state_dir = Path(td) / "state"
        runs = state_dir / "runs.d"
        runs.mkdir(parents=True)
        for idx in range(3):
            record = quota_record(Path(td), dispatch_id=f"quota-{idx}", pid=100 + idx, state="rate_limited")
            (runs / f"quota-{idx}.json").write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
        out = io.StringIO()
        args = argparse.Namespace(
            agent="grok-code",
            dispatch_id="new-grok",
            prompt_id=None,
            project_root=str(Path(td)),
            worker_cwd=str(Path(td)),
            worktree_path=None,
            controller_pid=os.getpid(),
            worker_pid=None,
            lease_id=None,
            mem_mb=1,
            agent_cap=5,
            priority="normal",
            ttl_s=3600,
            ram_mb=65536,
            reserve_mb=1024,
            worst_worker_mb=1,
            hard_cap=10,
            max_total=10,
            rate_pressure_threshold=3,
            rate_pressure_window_s=600,
        )
        with contextlib.redirect_stdout(out):
            rc = cap.cmd_acquire(args)
        payload = json.loads(out.getvalue())
        assert_eq("acquire blocked", rc, 2)
        assert_eq("reason", payload["reason"], "adaptive_rate_pressure")
        assert_eq("hard stop cap", payload["lane_agent_cap"], 0)
        assert_eq("stuck count", payload["adaptive_rate_pressure"]["stuck_worker_count"], 3)


def test_status_banner_and_advisory_mail() -> None:
    with tempfile.TemporaryDirectory() as td, temp_env(GOALFLIGHT_MESSAGES_DIR=str(Path(td) / "messages")):
        record = quota_record(Path(td), dispatch_id="quota-mail", pid=201, state="rate_limited")
        pressure = {
            "providers_under_pressure": [
                {
                    "scope": "provider",
                    "provider": "xai",
                    "budget_key": "provider:xai",
                    "labels": ["grok-code", "grok-research"],
                    "count": 3,
                    "threshold": 3,
                    "quota_hard_stop": True,
                    "effective_caps": {"grok-code": 0, "grok-research": 0},
                    "stuck_worker_count": 1,
                    "stuck_workers": [
                        {"dispatch_id": "quota-mail", "agent": "grok-code", "signature": "usage balance exhausted"}
                    ],
                }
            ],
            "window_seconds": 600,
        }
        payload = {
            "schema": "goalflight.status.aggregate.v1",
            "scope": {"project_root": str(Path(td)), "machine_active_leases": 0},
            "capacity": {"operating_cap": 10},
            "capacity_state": {"leases": {}, "cooldowns": {}},
            "dispatch": {"records": [record]},
            "rate_pressure": pressure,
        }
        lines = status.render_text(payload, 5)
        assert_true("status quota banner", any("quota: xai quota exhausted: 1 agent(s) stuck" in line for line in lines))
        status._post_quota_advisories(payload)
        status._post_quota_advisories(payload)
        inbox = messages.inbox_path(Path(td) / "messages", quota.QUOTA_STUCK_CONTROLLER_DISPATCH_ID)
        envelopes = messages.read_envelopes(inbox)
        assert_eq("deduped advisory count", len(envelopes), 1)
        assert_eq("advisory type", envelopes[0]["type"], quota.QUOTA_STUCK_ADVISORY_TYPE)


def test_quota_reaper_default_deny_guards_and_release() -> None:
    with tempfile.TemporaryDirectory() as td, temp_env(GOALFLIGHT_STATE_DIR=str(Path(td) / "state")):
        tmp = Path(td)
        record = quota_record(tmp, dispatch_id="qkill", pid=301, state="running_quiet")
        ledger.write_record(record)
        cap.save_state(
            {
                "schema": cap.SCHEMA,
                "machine_id": "test",
                "leases": {
                    "lease-qkill": {
                        "lease_id": "lease-qkill",
                        "dispatch_id": "qkill",
                        "agent": "grok-code",
                        "state": "active",
                        "worker_pid": 301,
                        "controller_pid": os.getpid(),
                        "mem_mb": 1,
                        "started_at": cap.iso(),
                        "expires_at": cap.iso(cap.utc_now() + dt.timedelta(hours=1)),
                    }
                },
                "cooldowns": {},
            }
        )
        killed: list[int] = []

        def kill_group(pgid: int) -> str:
            killed.append(pgid)
            return "SIGTERM"

        rows = [
            process_row(301),
            process_row(302),  # no ledger/tail
            process_row(303, age_s=10.0),  # young
            process_row(304, comm="claude-code-cli-acp"),
        ]
        result = acp.reap_quota_stuck_workers(
            process_rows=rows,
            records=ledger.read_records(),
            ttl_s=180.0,
            getpgid=lambda pid: 999 if pid == 302 else pid,
            terminate_group=kill_group,
            now_ts=time.time(),
            enabled=True,
        )
        assert_eq("only quota pid killed", killed, [301])
        assert_eq("reaped dispatch", result["reaped"][0]["dispatch_id"], "qkill")
        state = cap.load_state()
        assert_eq("lease released rate_limited", state["leases"]["lease-qkill"]["state"], "rate_limited")
        persisted = json.loads(ledger.record_path("qkill").read_text(encoding="utf-8"))
        assert_eq("ledger state rate_limited", persisted["state"], "rate_limited")


def test_kimi_quota_reaper_and_surplus_discovery() -> None:
    with tempfile.TemporaryDirectory() as td, temp_env(GOALFLIGHT_STATE_DIR=str(Path(td) / "state")):
        tmp = Path(td)
        record = quota_record(tmp, dispatch_id="kimi-quota", pid=42, state="running_quiet")
        kimi_comm = "/Users/x/.kimi-code/bin/kimi"
        record["agent"] = "kimi"
        record["worker_identity"] = worker_identity(42, comm=kimi_comm)
        write_tail(Path(record["stdout_path"]), "ERROR: reached account max rpm 60\n")
        ledger.write_record(record)
        cap.save_state(
            {
                "schema": cap.SCHEMA,
                "machine_id": "test",
                "leases": {
                    "lease-kimi-quota": {
                        "lease_id": "lease-kimi-quota",
                        "dispatch_id": "kimi-quota",
                        "agent": "kimi",
                        "state": "active",
                        "worker_pid": 42,
                        "controller_pid": os.getpid(),
                        "mem_mb": 1,
                        "started_at": cap.iso(),
                        "expires_at": cap.iso(cap.utc_now() + dt.timedelta(hours=1)),
                    }
                },
                "cooldowns": {},
            }
        )
        killed: list[int] = []
        result = acp.reap_quota_stuck_workers(
            process_rows=[process_row(42, comm=kimi_comm)],
            records=[record],
            ttl_s=180.0,
            getpgid=lambda pid: pid,
            terminate_group=lambda pgid: killed.append(pgid) or "SIGTERM",
            now_ts=time.time(),
            enabled=True,
        )
        assert_eq("kimi quota worker reaped", killed, [42])
        assert_eq("kimi quota candidate recorded", result["reaped"][0]["dispatch_id"], "kimi-quota")

        ps = (
            "42 /Users/x/.kimi-code/bin/kimi /Users/x/.kimi-code/bin/kimi -p work\n"
            "43 python python docs mention kimi\n"
        )
        with mock.patch.object(ledger.subprocess, "check_output", return_value=ps):
            surplus = ledger.scan_surplus([])
        assert_eq("only Kimi executable is surplus", [item["pid"] for item in surplus], [42])


def test_quota_reaper_escalates_sigkill_when_sigterm_does_not_exit() -> None:
    calls: list[tuple[int, int]] = []
    original_killpg = acp.os.killpg
    original_alive = acp._pgid_alive
    original_monotonic = acp.time.monotonic
    try:
        acp.os.killpg = lambda pgid, sig: calls.append((pgid, sig))  # type: ignore[assignment]
        acp._pgid_alive = lambda pgid: True  # type: ignore[assignment]
        ticks = iter([100.0, 106.0])
        acp.time.monotonic = lambda: next(ticks, 106.0)  # type: ignore[assignment]
        action = acp._terminate_quota_process_group(777)
    finally:
        acp.os.killpg = original_killpg  # type: ignore[assignment]
        acp._pgid_alive = original_alive  # type: ignore[assignment]
        acp.time.monotonic = original_monotonic  # type: ignore[assignment]
    assert_eq("quota reap escalation action", action, "SIGTERM+SIGKILL")
    assert_eq("quota reap escalation signals", calls, [(777, signal.SIGTERM), (777, signal.SIGKILL)])


def test_quota_reaper_partial_failure_not_counted_as_reaped() -> None:
    with tempfile.TemporaryDirectory() as td, temp_env(GOALFLIGHT_STATE_DIR=str(Path(td) / "state")):
        tmp = Path(td)
        record = quota_record(tmp, dispatch_id="qpartial", pid=321, state="running_quiet")
        cap.save_state(
            {
                "schema": cap.SCHEMA,
                "machine_id": "test",
                "leases": {
                    "lease-qpartial": {
                        "lease_id": "lease-qpartial",
                        "dispatch_id": "qpartial",
                        "agent": "grok-code",
                        "state": "active",
                        "worker_pid": 321,
                        "controller_pid": os.getpid(),
                        "mem_mb": 1,
                        "started_at": cap.iso(),
                        "expires_at": cap.iso(cap.utc_now() + dt.timedelta(hours=1)),
                    }
                },
                "cooldowns": {},
            }
        )
        result = acp.reap_quota_stuck_workers(
            process_rows=[process_row(321)],
            records=[record],
            ttl_s=180.0,
            getpgid=lambda pid: pid,
            terminate_group=lambda pgid: "SIGTERM+SIGKILL",
            now_ts=time.time(),
            enabled=True,
        )
        assert_eq("partial failure not reaped", result["reaped"], [])
        assert_eq("partial failure count", len(result["partial_failures"]), 1)
        assert_true("partial failure reports ledger", "ledger_not_updated" in result["partial_failures"][0]["bookkeeping_errors"])


def test_quota_reaper_refuses_dispatch_lease_when_worker_pid_mismatches() -> None:
    with tempfile.TemporaryDirectory() as td, temp_env(GOALFLIGHT_STATE_DIR=str(Path(td) / "state")):
        tmp = Path(td)
        record = quota_record(tmp, dispatch_id="qpidlease", pid=331, state="running_quiet")
        ledger.write_record(record)
        cap.save_state(
            {
                "schema": cap.SCHEMA,
                "machine_id": "test",
                "leases": {
                    "lease-qpidlease": {
                        "lease_id": "lease-qpidlease",
                        "dispatch_id": "qpidlease",
                        "agent": "grok-code",
                        "state": "active",
                        "worker_pid": 999,
                        "controller_pid": os.getpid(),
                        "mem_mb": 1,
                        "started_at": cap.iso(),
                        "expires_at": cap.iso(cap.utc_now() + dt.timedelta(hours=1)),
                    }
                },
                "cooldowns": {},
            }
        )
        result = acp.reap_quota_stuck_workers(
            process_rows=[process_row(331)],
            records=ledger.read_records(),
            ttl_s=180.0,
            getpgid=lambda pid: pid,
            terminate_group=lambda pgid: "SIGTERM+SIGKILL",
            now_ts=time.time(),
            enabled=True,
        )
        assert_eq("pid mismatch not fully reaped", result["reaped"], [])
        assert_true("pid mismatch reports lease", "lease_not_released" in result["partial_failures"][0]["bookkeeping_errors"])
        assert_eq("mismatched lease remains active", cap.load_state()["leases"]["lease-qpidlease"]["state"], "active")


def test_quota_reaper_rejects_no_signature_young_pgid_mismatch_and_acp_shim() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        no_sig_tail = tmp / "nosig.tail"
        write_tail(no_sig_tail, "still doing long research\n")
        sig_tail = tmp / "sig.tail"
        write_tail(sig_tail, "ERROR: insufficient_quota\n")
        records = [
            {
                "dispatch_id": "nosig",
                "agent": "codex",
                "state": "running_quiet",
                "worker_pid": 401,
                "worker_identity": worker_identity(401, comm="codex"),
                "stdout_path": str(no_sig_tail),
            },
            {
                "dispatch_id": "young",
                "agent": "codex",
                "state": "running_quiet",
                "worker_pid": 402,
                "worker_identity": worker_identity(402, comm="codex"),
                "stdout_path": str(sig_tail),
            },
            {
                "dispatch_id": "pgid",
                "agent": "codex",
                "state": "running_quiet",
                "worker_pid": 403,
                "worker_identity": worker_identity(403, comm="codex"),
                "stdout_path": str(sig_tail),
            },
            {
                "dispatch_id": "shim",
                "agent": "claude",
                "state": "running_quiet",
                "worker_pid": 404,
                "worker_identity": worker_identity(404, comm="claude-code-cli-acp"),
                "stdout_path": str(sig_tail),
            },
        ]
        killed: list[int] = []
        result = acp.reap_quota_stuck_workers(
            process_rows=[
                process_row(401, comm="codex"),
                process_row(402, comm="codex", age_s=10.0),
                process_row(403, comm="codex"),
                process_row(404, comm="claude-code-cli-acp"),
            ],
            records=records,
            ttl_s=180.0,
            getpgid=lambda pid: 999 if pid == 403 else pid,
            terminate_group=lambda pgid: killed.append(pgid) or "SIGTERM",
            now_ts=time.time(),
            enabled=True,
        )
        assert_eq("no negative guard kills", killed, [])
        assert_eq("no negative reaped", result["reaped"], [])


def test_quota_reaper_default_off_requires_explicit_enable() -> None:
    with tempfile.TemporaryDirectory() as td, temp_env(GOALFLIGHT_QUOTA_STUCK_REAP=""):
        tmp = Path(td)
        record = quota_record(tmp, dispatch_id="default-off", pid=451, state="running_quiet")
        killed: list[int] = []
        result = acp.reap_quota_stuck_workers(
            process_rows=[process_row(451)],
            records=[record],
            ttl_s=180.0,
            getpgid=lambda pid: pid,
            terminate_group=lambda pgid: killed.append(pgid) or "SIGTERM",
            now_ts=time.time(),
        )
        assert_eq("default off skip", result["skipped"], quota.QUOTA_STUCK_REAP_ENABLE_ENV)
        assert_eq("default off no kill", killed, [])


def test_prompt_echo_quota_text_is_not_counted_or_hard_stopped() -> None:
    with tempfile.TemporaryDirectory() as td, temp_env(GOALFLIGHT_STATE_DIR=str(Path(td) / "state")):
        state_dir = Path(td) / "state"
        runs = state_dir / "runs.d"
        runs.mkdir(parents=True)
        for idx in range(3):
            record = quota_record(Path(td), dispatch_id=f"echo-{idx}", pid=460 + idx, state="running_quiet")
            tail = Path(str(record["stdout_path"]))
            write_tail(
                tail,
                "\n".join(
                    [
                        "Research brief:",
                        "> provider may say usage balance exhausted",
                        "```",
                        "insufficient_quota",
                        "payment required",
                        "```",
                        "No provider error happened; continuing work.",
                    ]
                ),
            )
            (runs / f"echo-{idx}.json").write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
        records = ledger.read_records()
        assert_eq(
            "prompt echo not quota pressure",
            quota.quota_pressure_per_provider(records, window_seconds=600),
            {},
        )
        pressure = cap.current_rate_pressure(argparse.Namespace(rate_pressure_threshold=3, rate_pressure_window_s=600))
        assert_eq("no quota hard-stop entries", pressure["providers_under_pressure"], [])
        killed: list[int] = []
        result = acp.reap_quota_stuck_workers(
            process_rows=[process_row(460), process_row(461), process_row(462)],
            records=records,
            ttl_s=180.0,
            getpgid=lambda pid: pid,
            terminate_group=lambda pgid: killed.append(pgid) or "SIGTERM",
            now_ts=time.time(),
            enabled=True,
        )
        assert_eq("prompt echo no reap", result["reaped"], [])
        assert_eq("prompt echo no kill", killed, [])


def test_decorated_pressure_without_stuck_tail_does_not_hard_stop() -> None:
    pressure = {
        "providers_under_pressure": [
            {
                "scope": "provider",
                "provider": "xai",
                "budget_key": "provider:xai",
                "labels": ["grok-code", "grok-research"],
                "count": 3,
                "threshold": 3,
                "recommended_caps": {"grok-code": 1, "grok-research": 1},
            }
        ]
    }
    decorated = quota.decorate_pressure_payload(pressure, [], window_seconds=600)
    entry = decorated["providers_under_pressure"][0]
    assert_eq("no stuck tail count", entry["stuck_worker_count"], 0)
    assert_eq("no quota hard-stop without stuck tail", entry["quota_hard_stop"], False)
    assert_true("no zero effective caps", "effective_caps" not in entry)


def test_quota_reaper_hard_denies_acp_shaped_record_with_allowed_comm() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        record = quota_record(tmp, dispatch_id="acp-shaped", pid=471, state="running_quiet")
        record.update(
            {
                "agent": "grok-acp",
                "shape": "acp",
                "transport": "acp",
                "acp_session_id": "session-1",
                "worker_identity": worker_identity(471, comm="grok"),
            }
        )
        killed: list[int] = []
        result = acp.reap_quota_stuck_workers(
            process_rows=[process_row(471, comm="grok")],
            records=[record],
            ttl_s=180.0,
            getpgid=lambda pid: pid,
            terminate_group=lambda pgid: killed.append(pgid) or "SIGTERM",
            now_ts=time.time(),
            enabled=True,
        )
        assert_eq("acp shaped no kill", killed, [])
        assert_eq("acp shaped no reaped", result["reaped"], [])
        assert_eq("acp shaped no candidate", result["candidates"], [])


def test_quota_reaper_rejects_pid_reuse_identity_mismatch() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        record = quota_record(tmp, dispatch_id="pid-reuse", pid=501, state="rate_limited")
        killed: list[int] = []
        result = acp.reap_quota_stuck_workers(
            process_rows=[
                process_row(
                    501,
                    comm="grok",
                    age_s=600.0,
                    lstart="Wed Jul  1 12:30:00 2026",
                )
            ],
            records=[record],
            ttl_s=180.0,
            getpgid=lambda pid: pid,
            terminate_group=lambda pgid: killed.append(pgid) or "SIGTERM",
            now_ts=time.time(),
            enabled=True,
        )
        assert_eq("pid reuse must not kill", killed, [])
        assert_eq("pid reuse no reaped", result["reaped"], [])
        assert_eq("pid reuse no candidate", result["candidates"], [])


def test_draft_artifact_reconcile_requires_finality_before_complete() -> None:
    with tempfile.TemporaryDirectory() as td, temp_env(GOALFLIGHT_STATE_DIR=str(Path(td) / "state")):
        tmp = Path(td)
        artifact = tmp / "docs-private" / "research" / "draft.md"
        artifact.parent.mkdir(parents=True)
        artifact.write_text("finished draft\n", encoding="utf-8")
        started = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5)).isoformat(timespec="seconds")
        record = {
            "schema": "goalflight.dispatch.v1",
            "dispatch_id": "draft-done",
            "agent": "codex",
            "state": "worker_dead",
            "terminal_state": "worker_dead",
            "project_root": str(tmp),
            "draft_path": "docs-private/research/draft.md",
            "started_at": started,
        }
        ledger.write_record(record)
        row = ledger.status_payload()["records"][0]
        out = status._reconcile_output_tail_record(row)
        assert_eq("draft without finality stays worker_dead", out["state"], "worker_dead")
        assert_eq("draft without finality not promoted", out["draft_artifact_reconciliation"]["promoted"], False)
        persisted = json.loads(ledger.record_path("draft-done").read_text(encoding="utf-8"))
        assert_eq("ledger not promoted from draft alone", persisted["state"], "worker_dead")

        final_record = dict(record, dispatch_id="draft-final", draft_complete=True)
        ledger.write_record(final_record)
        rows = {r["dispatch_id"]: r for r in ledger.status_payload()["records"]}
        out_final = status._reconcile_output_tail_record(rows["draft-final"])
        assert_eq("explicit final draft promoted", out_final["state"], "complete")
        persisted_final = json.loads(ledger.record_path("draft-final").read_text(encoding="utf-8"))
        assert_eq("ledger persisted explicit final draft", persisted_final["state"], "complete")

        missing = dict(record, dispatch_id="draft-missing", draft_path="docs-private/research/missing.md")
        ledger.write_record(missing)
        rows = {r["dispatch_id"]: r for r in ledger.status_payload()["records"]}
        out_missing = status._reconcile_output_tail_record(rows["draft-missing"])
        assert_eq("missing artifact stays worker_dead", out_missing["state"], "worker_dead")


def test_draft_artifact_rejects_paths_outside_project_root() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        outside = tmp.parent / f"{tmp.name}-outside.md"
        outside.write_text("outside artifact\n", encoding="utf-8")
        try:
            record = {
                "schema": "goalflight.dispatch.v1",
                "dispatch_id": "draft-escape",
                "agent": "codex",
                "state": "worker_dead",
                "project_root": str(tmp),
                "draft_path": str(outside),
                "started_at": iso_now(),
            }
            assert_eq("outside absolute artifact rejected", quota.draft_artifact_for_record(record), None)
            relative_escape = dict(record, dispatch_id="draft-relative-escape", draft_path=f"../{outside.name}")
            assert_eq("outside relative artifact rejected", quota.draft_artifact_for_record(relative_escape), None)
        finally:
            outside.unlink(missing_ok=True)


def test_tail_quota_signature_preserves_float_mtime() -> None:
    with tempfile.TemporaryDirectory() as td:
        tail = Path(td) / "quota.tail"
        tail.write_text("ERROR: insufficient_quota\n", encoding="utf-8")
        ts = time.time() - 240.375
        os.utime(tail, (ts, ts))
        info = quota.tail_quota_signature(tail)
        assert_true("signature found", info is not None)
        assert_true("mtime remains float", isinstance(info["tail_mtime"], float))
        assert_true("mtime keeps subsecond precision", abs(float(info["tail_mtime"]) - ts) < 0.01)


def main() -> None:
    tests = [
        test_tail_signature_classifies_rate_limited_provider,
        test_capacity_hard_stops_provider_launches,
        test_status_banner_and_advisory_mail,
        test_quota_reaper_default_deny_guards_and_release,
        test_kimi_quota_reaper_and_surplus_discovery,
        test_quota_reaper_escalates_sigkill_when_sigterm_does_not_exit,
        test_quota_reaper_partial_failure_not_counted_as_reaped,
        test_quota_reaper_refuses_dispatch_lease_when_worker_pid_mismatches,
        test_quota_reaper_rejects_no_signature_young_pgid_mismatch_and_acp_shim,
        test_quota_reaper_default_off_requires_explicit_enable,
        test_prompt_echo_quota_text_is_not_counted_or_hard_stopped,
        test_decorated_pressure_without_stuck_tail_does_not_hard_stop,
        test_quota_reaper_hard_denies_acp_shaped_record_with_allowed_comm,
        test_quota_reaper_rejects_pid_reuse_identity_mismatch,
        test_draft_artifact_reconcile_requires_finality_before_complete,
        test_draft_artifact_rejects_paths_outside_project_root,
        test_tail_quota_signature_preserves_float_mtime,
    ]
    for test in tests:
        test()
    print(f"OK: quota stuck tests pass ({len(tests)} tests)")


if __name__ == "__main__":
    main()
