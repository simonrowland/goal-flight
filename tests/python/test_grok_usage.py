"""Hermetic tests for the bundled grok credit reader.

No network, no real auth document, no token bytes anywhere.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "scripts" / "grok_usage.py"
SPEC = importlib.util.spec_from_file_location("test_target_grok_usage", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
grok = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = grok
SPEC.loader.exec_module(grok)

SECRET = "session-token-must-never-appear"


def _auth(tmp_path: Path, token: str = SECRET) -> Path:
    path = tmp_path / "auth.json"
    path.write_text(json.dumps({"https://auth.x.ai::abc": {"key": token}}))
    return path


def test_healthy_payload_yields_percent_period_end_and_decision_details(
    tmp_path: Path,
) -> None:
    payload = {
        "config": {
            "creditUsagePercent": 41.0,
            "billingPeriodEnd": "2026-07-30T22:18:55.563213+00:00",
            "prepaidBalance": {"val": 25.0},
            "onDemandCap": {"val": 50.0},
            "onDemandUsed": {"val": 4.0},
            "currentPeriod": {"type": "USAGE_PERIOD_TYPE_WEEKLY"},
            "productUsage": [
                {"product": "GrokBuild", "usagePercent": 95.0},
                {"product": "GrokVoice", "usagePercent": 3.0},
                {"product": "GrokChat", "usagePercent": 2.0},
            ],
        }
    }
    record = grok.read_usage(
        auth_path=_auth(tmp_path), fetcher=lambda url, timeout: payload
    )
    assert record["ok"] is True
    assert record["used_percent"] == 41.0
    assert record["reset_at"] == pytest.approx(1785450000, abs=100_000)
    assert record["prepaid_balance"] == 25.0
    assert record["on_demand_cap"] == 50.0
    assert record["on_demand_used"] == 4.0
    assert record["period_type"] == "USAGE_PERIOD_TYPE_WEEKLY"
    assert record["product_usage"] == {
        "GrokBuild": 95.0,
        "GrokVoice": 3.0,
        "GrokChat": 2.0,
    }


@pytest.mark.parametrize(
    ("payload", "marker"),
    [
        ({}, "config"),
        ({"config": []}, "config"),
        ({"config": {}}, "creditUsagePercent"),
        ({"config": {"creditUsagePercent": None}}, "creditUsagePercent"),
        ({"config": {"creditUsagePercent": "41"}}, "creditUsagePercent"),
        ({"config": {"creditUsagePercent": True}}, "creditUsagePercent"),
    ],
)
def test_contract_drift_is_not_measured_headroom(
    tmp_path: Path, payload: object, marker: str
) -> None:
    """The endpoint is undocumented. A missing or re-typed field must report
    'could not measure' - never full headroom, never 0%."""
    record = grok.read_usage(
        auth_path=_auth(tmp_path), fetcher=lambda url, timeout: payload
    )
    assert record["ok"] is False
    assert marker in record["error"]
    assert "used_percent" not in record


def test_missing_login_is_reported_not_raised(tmp_path: Path) -> None:
    record = grok.read_usage(auth_path=tmp_path / "absent.json")
    assert record["ok"] is False and "no grok login" in record["error"]


def test_empty_and_tokenless_auth_documents_are_reported(tmp_path: Path) -> None:
    empty = tmp_path / "empty.json"
    empty.write_text("{}")
    assert grok.read_usage(auth_path=empty)["ok"] is False

    tokenless = tmp_path / "tokenless.json"
    tokenless.write_text(json.dumps({"issuer::x": {"email": "a@b.c"}}))
    record = grok.read_usage(auth_path=tokenless)
    assert record["ok"] is False and "session token" in record["error"]


def test_transport_failures_are_reported_without_a_percentage(tmp_path: Path) -> None:
    def boom(url, timeout):
        raise grok.GrokUsageError("billing endpoint returned HTTP 401")

    record = grok.read_usage(auth_path=_auth(tmp_path), fetcher=boom)
    assert record["ok"] is False
    assert record["error"] == "billing endpoint returned HTTP 401"
    assert "used_percent" not in record


def test_token_never_reaches_the_emitted_record_or_stdout(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = {}

    def fetcher(url, timeout):
        return {"config": {"creditUsagePercent": 7.0, "billingPeriodEnd": None}}

    record = grok.read_usage(auth_path=_auth(tmp_path), fetcher=fetcher)
    seen["record"] = json.dumps(record)
    assert SECRET not in seen["record"]

    monkeypatch.setattr(grok, "read_usage", lambda **kwargs: record)
    grok.main(["--json"])
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err


def test_unparseable_period_end_keeps_the_percentage(tmp_path: Path) -> None:
    """A bad timestamp costs the reset column, not the whole reading."""
    payload = {
        "config": {
            "creditUsagePercent": 12.0,
            "billingPeriodEnd": "not-a-date",
        }
    }
    record = grok.read_usage(
        auth_path=_auth(tmp_path), fetcher=lambda url, timeout: payload
    )
    assert record["ok"] is True
    assert record["used_percent"] == 12.0
    assert record["reset_at"] is None
