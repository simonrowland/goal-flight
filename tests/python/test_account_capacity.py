"""Hermetic account-scoped capacity and Codex account selection tests."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import io
import json
import sys
from types import SimpleNamespace

import pytest

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_capacity as cap  # noqa: E402
import goalflight_dispatch as dispatch  # noqa: E402
import goalflight_usage as usage  # noqa: E402


@pytest.fixture()
def isolated_capacity(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("GOALFLIGHT_CAPACITY_CONF", "/dev/null")
    monkeypatch.setattr(cap, "ACCOUNT_CAPS", {"codex": {"alpha": 2, "beta": 2}})
    monkeypatch.setattr(cap, "current_rate_pressure", lambda args=None: None)
    cap.save_state(
        {
            "schema": cap.SCHEMA,
            "machine_id": cap.machine_id(),
            "leases": {},
            "cooldowns": {},
        }
    )
    return tmp_path


def _args(*, account: str, model: str | None = None, max_total: int = 20):
    return argparse.Namespace(
        agent="codex",
        account=account,
        model=model,
        dispatch_id=f"dispatch-{account}",
        prompt_id=None,
        project_root="/tmp/project",
        worker_cwd="/tmp/project",
        worktree_path=None,
        controller_pid=None,
        worker_pid=None,
        lease_id=None,
        mem_mb=1,
        agent_cap=None,
        priority="normal",
        ttl_s=600,
        ram_mb=65536,
        reserve_mb=cap.DEFAULT_RESERVE_MB,
        worst_worker_mb=cap.DEFAULT_WORST_WORKER_MB,
        hard_cap=40,
        max_total=max_total,
        rate_pressure_window_s=1,
        rate_pressure_threshold=99,
    )


def _acquire(account: str, *, model: str | None = None, max_total: int = 20):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = cap.cmd_acquire(_args(account=account, model=model, max_total=max_total))
    return rc, json.loads(out.getvalue())


def test_accounts_fill_independently_and_machine_ceiling_still_binds(isolated_capacity):
    cap.ACCOUNT_CAPS["codex"] = {"alpha": 30, "beta": 30}
    for index in range(30):
        rc, payload = _acquire("alpha", max_total=100)
        assert rc == 0, payload
    for index in range(30):
        rc, payload = _acquire("beta", max_total=100)
        assert rc == 0, payload
    rc, payload = _acquire("alpha", max_total=100)
    assert rc == 2 and payload["reason"] == "account_worker_cap", payload

    cap.ACCOUNT_CAPS["codex"] = {"alpha": 30, "beta": 30}
    cap.save_state(
        {
            "schema": cap.SCHEMA,
            "machine_id": cap.machine_id(),
            "leases": {},
            "cooldowns": {},
        }
    )
    assert _acquire("alpha", max_total=2)[0] == 0
    assert _acquire("beta", max_total=2)[0] == 0
    rc, payload = _acquire("alpha", max_total=2)
    assert rc == 2 and payload["reason"] == "machine_worker_cap", payload


def test_model_weights_sum_against_account_cap(isolated_capacity, monkeypatch):
    cap.ACCOUNT_CAPS["codex"] = {"alpha": 1}
    monkeypatch.setattr(
        cap,
        "model_weight",
        lambda vendor, model: 0.25 if model == "gpt-5.6-luna" else 1.0,
    )
    for index in range(4):
        rc, payload = _acquire("alpha", model="gpt-5.6-luna")
        assert rc == 0, (index, payload)
    rc, payload = _acquire("alpha", model="gpt-5.6-luna")
    assert rc == 2 and payload["active_weight"] == 1.0, payload


def test_non_finite_lease_weight_defaults_to_one(isolated_capacity):
    assert cap._lease_weight({"capacity_weight": float("inf")}) == 1.0
    assert cap._lease_weight({"capacity_weight": "NaN"}) == 1.0


def test_legacy_vendor_lease_uses_ledger_account_for_capacity(isolated_capacity, monkeypatch):
    legacy_lease = {
        "dispatch_id": "legacy-alpha",
        "agent": "codex",
        "state": "active",
        "capacity_weight": 1.0,
    }
    monkeypatch.setattr(
        cap,
        "_dispatch_record_for_lease",
        lambda lease: {"effective_account": "alpha"}
        if lease is legacy_lease
        else None,
    )
    rows = cap.account_capacity_rows([legacy_lease])
    assert rows["codex/alpha"]["active"] == 1
    assert rows["codex/alpha"]["active_weight"] == 1.0
    assert "codex/default" not in rows


def test_account_cooldown_does_not_block_sibling(isolated_capacity):
    with cap.StateLock():
        state = cap.load_state()
        state["cooldowns"]["codex/alpha"] = {
            "agent": "codex",
            "account": "alpha",
            "reason": "walled",
            "until": cap.iso(cap.utc_now() + dt.timedelta(hours=1)),
        }
        cap.save_state(state)
    rc, payload = _acquire("alpha")
    assert rc == 2 and payload["reason"] == "cooldown:walled", payload
    rc, payload = _acquire("beta")
    assert rc == 0, payload


def test_walled_account_does_not_block_healthy_account(monkeypatch):
    monkeypatch.setattr(dispatch, "_configured_account_names", lambda engine: ["walled", "healthy"])
    monkeypatch.setattr(
        dispatch,
        "_codex_usage_probe_says_usable",
        lambda account, **kwargs: account == "healthy",
    )
    monkeypatch.setattr(dispatch, "_account_quota_blocked", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        dispatch.goalflight_capacity,
        "launch_slot_budget",
        lambda *args, **kwargs: {
            "account_remaining": 30,
            "request_weight": 1.0,
            "by_account": {"codex/healthy": {"active_weight": 0, "cap": 30}},
        },
    )
    selected, rejected = dispatch.select_codex_account()
    assert selected == "healthy"
    assert rejected == [{"account": "walled", "reason": "walled or quota-blocked"}]


def test_resume_resolution_uses_healthy_account_without_claiming_walled_one(monkeypatch, tmp_path):
    calls: list[str | None] = []
    home = tmp_path / "dispatch-home"
    home.mkdir()
    (home / "sessions").mkdir()
    monkeypatch.setattr(dispatch, "_configured_account_names", lambda engine: ["walled", "healthy"])
    monkeypatch.setattr(
        dispatch,
        "_codex_usage_probe_says_usable",
        lambda account, **kwargs: account == "healthy",
    )
    monkeypatch.setattr(dispatch, "_account_quota_blocked", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        dispatch.goalflight_capacity,
        "launch_slot_budget",
        lambda *args, **kwargs: {
            "account_remaining": 30,
            "request_weight": 1.0,
            "by_account": {"codex/healthy": {"active_weight": 0, "cap": 30}},
        },
    )

    def resolve(_project_root, account, _dispatch_id):
        calls.append(account)
        return (str(home), account) if account == "healthy" else (None, None)

    monkeypatch.setattr(
        dispatch,
        "_codex_seat_api",
        lambda: type("API", (), {"resolve_codex_seat": staticmethod(resolve)})(),
    )
    resolved = dispatch.resolve_codex_home(tmp_path, None, "resume-child")
    assert resolved == (str(home), "healthy")
    assert calls == ["healthy"]


def test_selection_uses_usage_health_over_seat_state(monkeypatch):
    monkeypatch.setattr(dispatch, "_configured_account_names", lambda engine: ["alpha"])
    monkeypatch.setattr(dispatch, "_seat_probe_says_usable", lambda *args: False)
    monkeypatch.setattr(dispatch, "_account_quota_blocked", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        usage,
        "collect_usage",
        lambda **kwargs: [
            {
                "provider": "codex",
                "account": "alpha",
                "remaining": "80%",
                "flags": [],
                "evidence": {"probe": {"state": "reported"}},
            }
        ],
    )
    monkeypatch.setattr(
        dispatch.goalflight_capacity,
        "launch_slot_budget",
        lambda *args, **kwargs: {
            "account_remaining": 30,
            "request_weight": 1.0,
            "by_account": {"codex/alpha": {"active_weight": 0, "cap": 30}},
        },
    )

    selected, rejected = dispatch.select_codex_account()
    assert selected == "alpha"
    assert rejected == []


def test_unverified_resolver_account_is_not_promoted(monkeypatch, tmp_path):
    home = tmp_path / "unverified-home"
    monkeypatch.setattr(dispatch, "_configured_account_names", lambda engine: [])
    monkeypatch.setattr(dispatch, "_codex_seat_api", lambda: SimpleNamespace(
        resolve_codex_seat=lambda *_args: (str(home), "mystery")
    ))
    monkeypatch.setattr(usage, "collect_usage", lambda **kwargs: [])

    assert dispatch.resolve_codex_home(tmp_path, None, "unknown-health") == (
        None,
        "host",
    )


def test_status_and_usage_render_account_active_cap(isolated_capacity):
    cap.ACCOUNT_CAPS["codex"] = {"alpha": 2, "beta": 2}
    assert _acquire("alpha")[0] == 0
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        rc = cap.main(["status", "--json", "--ram-mb", "65536"])
    assert rc == 0
    status = json.loads(output.getvalue())
    assert status["by_account"]["codex/alpha"]["active"] == 1
    assert status["by_account"]["codex/alpha"]["cap"] == 2
    rendered = usage.render_table(
        [{"provider": "codex", "account": "alpha", "used": "10%", "remaining": "90%", "reset_at": None, "flags": [], "evidence": {}}],
        now=0,
        capacity_rows=status["by_account"],
    )
    assert "codex/alpha: active=1" in rendered
    assert "cap=2" in rendered
