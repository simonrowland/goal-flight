"""Hermetic account-scoped capacity and Codex account selection tests."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import io
import json
import sys
import textwrap
import time
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


def _args(
    *,
    account: str,
    model: str | None = None,
    max_total: int = 20,
    agent: str = "codex",
):
    return argparse.Namespace(
        agent=agent,
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


def _acquire(
    account: str,
    *,
    model: str | None = None,
    max_total: int = 20,
    agent: str = "codex",
):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = cap.cmd_acquire(
            _args(account=account, model=model, max_total=max_total, agent=agent)
        )
    return rc, json.loads(out.getvalue())


def test_reserved_lease_rebind_moves_one_account_attribution(isolated_capacity):
    rc, payload = _acquire("alpha")
    assert rc == 0, payload
    lease_id = payload["lease"]["lease_id"]

    assert cap.rebind_capacity_lease_account(lease_id, "beta") is True

    lease = cap.load_state()["leases"][lease_id]
    assert lease["account"] == "beta"
    rows = cap.account_capacity_rows(cap.active_leases(cap.load_state()))
    assert rows["codex/alpha"]["active_weight"] == 0.0
    assert rows["codex/beta"]["active_weight"] == 1.0

    assert cap.mark_lease_spawning(lease_id) is True
    assert cap.rebind_capacity_lease_account(lease_id, "alpha") is False


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


def test_grok_accounts_fill_independently_to_default_account_cap(isolated_capacity):
    cap.ACCOUNT_CAPS["grok"] = {"default": 50}
    assert cap.account_cap("grok", "alpha") == 50
    assert cap.account_cap("grok", "beta") == 50
    for account in ("alpha", "beta"):
        for _ in range(50):
            rc, payload = _acquire(account, max_total=200, agent="grok-code")
            assert rc == 0, (account, payload)
    rc, payload = _acquire("alpha", max_total=200, agent="grok-research")
    assert rc == 2 and payload["reason"] == "account_worker_cap", payload


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


def test_legacy_grok_lease_migrates_once_to_ledger_account(isolated_capacity, monkeypatch):
    cap.ACCOUNT_CAPS["grok"] = {"default": 50}
    legacy_lease = {
        "dispatch_id": "legacy-grok-alpha",
        "agent": "grok-code",
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
    assert rows["grok/alpha"]["active"] == 1
    assert rows["grok/alpha"]["active_weight"] == 1.0
    assert rows["grok/alpha"]["cap"] == 50
    assert "grok/default" not in rows


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
        "_codex_usage_probe_rows",
        lambda *_args, **_kwargs: [
            _codex_probe_row("walled", "0%", state="walled"),
            _codex_probe_row("healthy", "80%"),
        ],
    )
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


def _codex_probe_row(
    account: str,
    remaining: str,
    *,
    state: str = "reported",
    weekly_used_percent: float | None = None,
    observed_at: float | None = None,
) -> dict:
    row = {
        "provider": "codex",
        "account": account,
        "remaining": remaining,
        "reset_at": "2030-01-02T03:04:05Z",
        "flags": ["walled"] if state == "walled" else [],
        "evidence": {"probe": {"state": state}},
    }
    row["evidence"]["probe"]["observed_at"] = (
        time.time() if observed_at is None else observed_at
    )
    if weekly_used_percent is not None:
        row["weekly_used_percent"] = weekly_used_percent
    return row


def test_pinned_codex_probe_uses_seat_reader_and_usage_timeout(
    isolated_capacity, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    args_path = tmp_path / "reader-args.json"
    reader = tmp_path / "codex_usage.py"
    reader.write_text(
        textwrap.dedent(
            """\
            import argparse
            import json
            import os
            import sys
            import time

            seats = ["target-seat", "seat-b", "seat-c", "seat-d", "seat-e"]
            parser = argparse.ArgumentParser()
            parser.add_argument("--json", action="store_true")
            parser.add_argument("--seat")
            args = parser.parse_args()
            with open(os.environ["FAKE_CODEX_ARGS_PATH"], "w", encoding="utf-8") as out:
                json.dump(sys.argv[1:], out)
            selected = [args.seat] if args.seat else seats
            records = []
            for seat in selected:
                time.sleep(2)
                records.append({
                    "seat": seat,
                    "used_percent": 57,
                    "reset_at": "2030-01-02T03:04:05Z",
                    "source": "fake",
                    "ok": True,
                })
            print(json.dumps(records))
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("FAKE_CODEX_ARGS_PATH", str(args_path))

    real_collect_usage = usage.collect_usage
    timeouts: list[float] = []

    def collect_from_fake_reader(*, timeout_s, reader_specs, ledger_records):
        timeouts.append(timeout_s)
        return real_collect_usage(
            readers_dir=tmp_path,
            timeout_s=timeout_s,
            reader_specs=reader_specs,
            ledger_records=ledger_records,
        )

    monkeypatch.setattr(usage, "collect_usage", collect_from_fake_reader)
    started = time.monotonic()
    dispatch._refuse_walled_codex_account("target-seat")
    elapsed = time.monotonic() - started

    assert timeouts == [usage.DEFAULT_TIMEOUT_S]
    assert timeouts[0] == 20.0
    assert json.loads(args_path.read_text(encoding="utf-8")) == [
        "--json",
        "--seat",
        "target-seat",
    ]
    assert elapsed < usage.DEFAULT_TIMEOUT_S


def test_stale_codex_probe_is_unknown_and_not_selected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 2_000_000_000.0
    stale = _codex_probe_row(
        "stale", "95%", observed_at=now - dispatch.CODEX_USAGE_PROBE_MAX_AGE_S - 1
    )
    future = _codex_probe_row(
        "future", "90%", observed_at=now + dispatch.CODEX_USAGE_PROBE_FUTURE_SKEW_S + 1
    )
    fresh = _codex_probe_row("fresh", "40%", observed_at=now)
    monkeypatch.setattr(dispatch.time, "time", lambda: now)
    monkeypatch.setattr(
        dispatch, "_configured_account_names", lambda engine: ["stale", "future", "fresh"]
    )
    monkeypatch.setattr(
        dispatch,
        "_codex_usage_probe_rows",
        lambda *_args, **_kwargs: [stale, future, fresh],
    )
    monkeypatch.setattr(dispatch, "_account_quota_blocked", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        dispatch.goalflight_capacity,
        "launch_slot_budget",
        lambda _agent, *, account, **_kwargs: {
            "account_remaining": 30,
            "request_weight": 1.0,
            "by_account": {f"codex/{account}": {"active_weight": 0, "cap": 30}},
        },
    )

    assert dispatch._codex_usage_probe_says_usable("stale", rows=[stale], now=now) is None
    assert dispatch._codex_usage_probe_says_usable("future", rows=[future], now=now) is None
    selected, rejected = dispatch.select_codex_account()

    assert selected == "fresh"
    assert rejected == [
        {"account": "stale", "reason": "health probe unknown"},
        {"account": "future", "reason": "health probe unknown"},
    ]


def test_pinned_codex_dispatch_refuses_walled_account_with_reset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    account_home = tmp_path / ".goal-flight" / "accounts" / "walled" / "codex"
    account_home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(
        dispatch,
        "_codex_usage_probe_rows",
        lambda *_args, **_kwargs: [
            _codex_probe_row("walled", "0%", state="walled")
        ],
    )
    monkeypatch.setattr(dispatch, "_account_quota_blocked", lambda *args, **kwargs: False)

    with pytest.raises(
        dispatch.DispatchUsageError,
        match=(
            r"walled.*condition=walled; elapsed=0\.00s, budget=20\.00s"
            r".*2030-01-02T03:04:05"
        ),
    ):
        dispatch._resolve_account_env(
            SimpleNamespace(agent="codex", account="walled", model=None)
        )


def test_pinned_codex_dispatch_refuses_unknown_health(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    account_home = tmp_path / ".goal-flight" / "accounts" / "unknown" / "codex"
    account_home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(
        dispatch,
        "_codex_usage_probe_rows",
        lambda *_args, **_kwargs: [_codex_probe_row("unknown", "unknown")],
    )
    monkeypatch.setattr(dispatch, "_account_quota_blocked", lambda *args, **kwargs: False)

    with pytest.raises(
        dispatch.DispatchUsageError,
        match=(
            r"unknown.*health probe unknown or stale "
            r"\(condition=unusable row \(remaining=unknown\); "
            r"elapsed=0\.00s, budget=20\.00s\)"
        ),
    ):
        dispatch._resolve_account_env(
            SimpleNamespace(agent="codex", account="unknown", model=None)
        )


def test_pinned_codex_probe_diagnostic_conditions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 2_000_000_000.0
    monkeypatch.setattr(dispatch.time, "time", lambda: now)
    timeout_row = {
        "provider": "codex",
        "account": None,
        "remaining": "timed out",
        "flags": ["timeout"],
        "evidence": {"probe": {"state": "timed_out"}},
    }
    stale_row = _codex_probe_row(
        "target-seat",
        "43%",
        observed_at=now - dispatch.CODEX_USAGE_PROBE_MAX_AGE_S - 12.5,
    )
    for expected, rows in (
        ("timeout", [timeout_row]),
        ("no row", []),
        ("stale row (age 312.5s)", [stale_row]),
    ):
        with pytest.raises(dispatch.DispatchUsageError) as exc:
            dispatch._refuse_walled_codex_account("target-seat", usage_rows=rows)
        assert f"condition={expected}" in str(exc.value)
        assert "elapsed=0.00s, budget=20.00s" in str(exc.value)


def test_pinned_codex_resume_refuses_walled_account_with_reset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        dispatch,
        "_codex_usage_probe_rows",
        lambda *_args, **_kwargs: [_codex_probe_row("walled", "0%", state="walled")],
    )
    monkeypatch.setattr(dispatch, "_account_quota_blocked", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        dispatch,
        "resolve_codex_home",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("walled resume must refuse before home resolution")
        ),
    )

    with pytest.raises(
        dispatch.DispatchUsageError,
        match=r"walled.*walled.*2030-01-02T03:04:05",
    ):
        dispatch._preflight_codex_resume_account(
            SimpleNamespace(account="walled"),
            project_root=tmp_path,
            dispatch_id="resume-walled",
        )


def test_walled_grok_account_does_not_block_healthy_account(monkeypatch):
    import grok_seats

    calls: list[set[str] | None] = []

    def select_seat(*, exclude=None):
        calls.append(exclude)
        return "healthy" if exclude == {"walled"} else "walled"

    monkeypatch.setattr(grok_seats, "select_seat", select_seat)
    monkeypatch.setattr(
        dispatch, "_account_quota_blocked", lambda *args, **kwargs: False
    )
    monkeypatch.setattr(
        dispatch.goalflight_capacity,
        "launch_slot_budget",
        lambda _agent, *, account, **_kwargs: {
            "unreadable": False,
            "account_remaining": 0 if account == "walled" else 50,
            "request_weight": 1.0,
            "by_account": {},
        },
    )

    selected = dispatch.grok_selected_account(
        SimpleNamespace(agent="grok-code", account=None, model=None)
    )
    assert selected == "healthy"
    assert calls == [None, {"walled"}]


def test_resume_resolution_uses_healthy_account_without_claiming_walled_one(monkeypatch, tmp_path):
    calls: list[str | None] = []
    home = tmp_path / "dispatch-home"
    home.mkdir()
    (home / "sessions").mkdir()
    monkeypatch.setattr(dispatch, "_configured_account_names", lambda engine: ["walled", "healthy"])
    monkeypatch.setattr(
        dispatch,
        "_codex_usage_probe_rows",
        lambda *_args, **_kwargs: [
            _codex_probe_row("walled", "0%", state="walled"),
            _codex_probe_row("healthy", "80%"),
        ],
    )
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
                "evidence": {
                    "probe": {"state": "reported", "observed_at": time.time()}
                },
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


def test_codex_selection_uses_most_measured_headroom(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        dispatch,
        "_configured_account_names",
        lambda engine: ["unknown", "alpha", "beta"],
    )
    monkeypatch.setattr(
        dispatch,
        "_codex_usage_probe_rows",
        lambda *_args, **_kwargs: [
            _codex_probe_row("unknown", "unknown"),
            _codex_probe_row("alpha", "20%"),
            _codex_probe_row("beta", "80%"),
        ],
    )
    monkeypatch.setattr(dispatch, "_account_quota_blocked", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        dispatch.goalflight_capacity,
        "launch_slot_budget",
        lambda *args, **kwargs: {
            "account_remaining": 30,
            "request_weight": 1.0,
            "by_account": {f"codex/{kwargs['account']}": {"active_weight": 0, "cap": 30}},
        },
    )

    selected, rejected = dispatch.select_codex_account()

    assert selected == "beta"
    assert rejected == [{"account": "unknown", "reason": "health probe unknown"}]


def test_codex_selection_preserves_weekly_resume_reserve(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        dispatch,
        "_configured_account_names",
        lambda engine: ["reserved", "available"],
    )
    monkeypatch.setattr(
        dispatch,
        "_codex_usage_probe_rows",
        lambda *_args, **_kwargs: [
            _codex_probe_row("reserved", "95%", weekly_used_percent=95),
            _codex_probe_row("available", "80%", weekly_used_percent=40),
        ],
    )
    monkeypatch.setattr(dispatch, "_account_quota_blocked", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        dispatch.goalflight_capacity,
        "launch_slot_budget",
        lambda *args, **kwargs: {
            "account_remaining": 30,
            "request_weight": 1.0,
            "by_account": {f"codex/{kwargs['account']}": {"active_weight": 0, "cap": 30}},
        },
    )

    selected, rejected = dispatch.select_codex_account()

    assert selected == "available"
    assert rejected == [{"account": "reserved", "reason": "weekly resume reserve"}]


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


def test_resolver_only_account_without_numeric_headroom_is_not_promoted(
    monkeypatch, tmp_path
):
    home = tmp_path / "unmeasured-home"
    monkeypatch.setattr(dispatch, "_configured_account_names", lambda engine: [])
    monkeypatch.setattr(
        dispatch,
        "_codex_seat_api",
        lambda: SimpleNamespace(
            resolve_codex_seat=lambda *_args: (str(home), "mystery")
        ),
    )
    monkeypatch.setattr(
        dispatch, "_codex_usage_probe_rows", lambda *_args, **_kwargs: []
    )
    monkeypatch.setattr(
        dispatch, "_codex_usage_probe_says_usable", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(dispatch, "_account_quota_blocked", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        dispatch.goalflight_capacity,
        "launch_slot_budget",
        lambda *args, **kwargs: {
            "account_remaining": 30,
            "request_weight": 1.0,
            "by_account": {"codex/mystery": {"active_weight": 0, "cap": 30}},
        },
    )

    assert dispatch.resolve_codex_home(tmp_path, None, "unmeasured-health") == (
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


def test_status_and_usage_render_grok_account_active_cap(isolated_capacity):
    cap.ACCOUNT_CAPS["grok"] = {"default": 50}
    assert _acquire("alpha", agent="grok-code")[0] == 0
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        rc = cap.main(["status", "--json", "--ram-mb", "65536"])
    assert rc == 0
    status = json.loads(output.getvalue())
    row = status["by_account"]["grok/alpha"]
    assert row["active"] == 1
    assert row["cap"] == 50
    rendered = usage.render_table(
        [{
            "provider": "grok",
            "account": "alpha",
            "used": "10%",
            "remaining": "90%",
            "reset_at": None,
            "flags": [],
            "evidence": {},
        }],
        now=0,
        capacity_rows=status["by_account"],
    )
    assert "grok/alpha: active=1" in rendered
    assert "cap=50" in rendered
