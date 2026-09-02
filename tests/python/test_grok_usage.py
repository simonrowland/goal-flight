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
    assert record["probe_state"] == "usable"
    assert record["auth_state"] == "valid"
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
        ({"config": {"creditUsagePercent": None}}, "creditUsagePercent"),
        ({"config": {"creditUsagePercent": "41"}}, "creditUsagePercent"),
        ({"config": {"creditUsagePercent": True}}, "creditUsagePercent"),
    ],
)
def test_contract_drift_is_not_measured_headroom(
    tmp_path: Path, payload: object, marker: str
) -> None:
    """The endpoint is undocumented. A RE-TYPED field must report 'could not
    measure' - never full headroom, never 0%. (An absent field is a different
    event; see the absent-key test below.)"""
    record = grok.read_usage(
        auth_path=_auth(tmp_path), fetcher=lambda url, timeout: payload
    )
    assert record["ok"] is False
    assert record["probe_state"] == "unknown"
    assert record["auth_state"] == "valid"
    assert marker in record["error"]
    assert "used_percent" not in record


@pytest.mark.parametrize(
    "used_percent",
    [float("nan"), float("inf"), float("-inf"), -0.1, 100.1],
)
def test_non_percentage_numeric_values_are_unknown(
    tmp_path: Path, used_percent: float
) -> None:
    record = grok.read_usage(
        auth_path=_auth(tmp_path),
        fetcher=lambda url, timeout: {
            "config": {"creditUsagePercent": used_percent}
        },
    )
    assert record["ok"] is False
    assert record["probe_state"] == "unknown"
    assert record["auth_state"] == "valid"
    assert "creditUsagePercent" in record["error"]
    assert "used_percent" not in record


def test_absent_percent_is_unknown_but_still_reports_what_it_measured(
    tmp_path: Path,
) -> None:
    """Input path: a freshly created account whose billing period just opened.

    Observed live 2026-08-12 -- the endpoint omits creditUsagePercent entirely
    rather than sending 0, while every other field parses. Failing the whole
    record would throw away a balance and reset date we DID measure and would
    hide a completely fresh seat behind an error.
    """
    payload = {
        "config": {
            "billingPeriodEnd": "2026-08-19T20:22:02.229859+00:00",
            "prepaidBalance": {"val": 12.5},
            "currentPeriod": {"type": "USAGE_PERIOD_TYPE_WEEKLY"},
        }
    }
    record = grok.read_usage(
        auth_path=_auth(tmp_path), fetcher=lambda url, timeout: payload
    )
    assert record["ok"] is True
    assert record["probe_state"] == "unknown"
    assert record["auth_state"] == "valid"
    assert record["used_percent"] is None, "absent must not become a number"
    assert record["used_percent_absent"] is True
    assert record["prepaid_balance"] == 12.5
    assert record["reset_at"] is not None
    assert record["period_type"] == "USAGE_PERIOD_TYPE_WEEKLY"


def test_account_label_rides_on_every_record_including_failures(
    tmp_path: Path,
) -> None:
    """With several logins configured, a record that cannot say WHICH login it
    describes is not actionable."""
    ok = grok.read_usage(
        auth_path=_auth(tmp_path),
        fetcher=lambda url, timeout: {"config": {"creditUsagePercent": 10.0}},
        account="6f3c47",
    )
    assert ok["account"] == "6f3c47"

    def boom(url, timeout):
        raise grok.GrokUsageError("billing endpoint unreachable")

    failed = grok.read_usage(
        auth_path=_auth(tmp_path), fetcher=boom, account="6f3c47"
    )
    assert failed["ok"] is False
    assert failed["account"] == "6f3c47"

    host = grok.read_usage(
        auth_path=_auth(tmp_path),
        fetcher=lambda url, timeout: {"config": {"creditUsagePercent": 10.0}},
    )
    assert host["account"] is None, "the host login stays unlabelled"


def test_accounts_lists_host_then_each_seat(tmp_path: Path, monkeypatch) -> None:
    """Input path: seat dirs created by `HOME=<dir> grok` logins."""
    home = tmp_path / "home"
    (home / ".grok").mkdir(parents=True)
    (home / ".grok" / "auth.json").write_text("{}")
    accounts_dir = tmp_path / "accounts"
    for seat in ("6f3c47", "aaa111"):
        (accounts_dir / seat / "grok" / ".grok").mkdir(parents=True)
        (accounts_dir / seat / "grok" / ".grok" / "auth.json").write_text("{}")
    # a dir with no grok login must not be reported as a grok account
    (accounts_dir / "codex-only" / "codex").mkdir(parents=True)

    monkeypatch.delenv("GROK_HOME", raising=False)
    monkeypatch.setattr(grok.Path, "home", staticmethod(lambda: home))
    monkeypatch.setattr(grok, "ACCOUNTS_DIR", accounts_dir)

    labels = [label for label, _ in grok.accounts()]
    assert labels == [None, "6f3c47", "aaa111"]


def test_grok_home_override_reports_that_one_account(
    tmp_path: Path, monkeypatch
) -> None:
    accounts_dir = tmp_path / "accounts"
    (accounts_dir / "6f3c47" / "grok" / ".grok").mkdir(parents=True)
    (accounts_dir / "6f3c47" / "grok" / ".grok" / "auth.json").write_text("{}")
    monkeypatch.setenv("GROK_HOME", str(tmp_path / "explicit"))
    monkeypatch.setattr(grok, "ACCOUNTS_DIR", accounts_dir)
    assert [label for label, _ in grok.accounts()] == [None]


def test_missing_login_is_reported_not_raised(tmp_path: Path) -> None:
    record = grok.read_usage(auth_path=tmp_path / "absent.json")
    assert record["ok"] is False and "no grok login" in record["error"]
    assert record["probe_state"] == "unusable"
    assert record["auth_state"] == "invalid"


def test_empty_and_tokenless_auth_documents_are_reported(tmp_path: Path) -> None:
    empty = tmp_path / "empty.json"
    empty.write_text("{}")
    empty_record = grok.read_usage(auth_path=empty)
    assert empty_record["ok"] is False
    assert empty_record["probe_state"] == "unusable"
    assert empty_record["auth_state"] == "invalid"

    tokenless = tmp_path / "tokenless.json"
    tokenless.write_text(json.dumps({"issuer::x": {"email": "a@b.c"}}))
    record = grok.read_usage(auth_path=tokenless)
    assert record["ok"] is False and "session token" in record["error"]
    assert record["probe_state"] == "unusable"
    assert record["auth_state"] == "invalid"


def test_whitespace_only_auth_token_is_invalid_and_unusable(tmp_path: Path) -> None:
    auth = _auth(tmp_path, token="\n\t ")
    record = grok.read_usage(auth_path=auth)
    assert record["ok"] is False
    assert record["probe_state"] == "unusable"
    assert record["auth_state"] == "invalid"
    assert "session token" in record["error"]


@pytest.mark.parametrize("document", [[], None, "x"])
def test_non_object_auth_document_is_malformed_and_unknown(
    tmp_path: Path, document: object
) -> None:
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps(document))
    record = grok.read_usage(auth_path=auth)
    assert record["ok"] is False
    assert record["probe_state"] == "unknown"
    assert record["auth_state"] == "unknown"
    assert "malformed" in record["error"]


def test_unreadable_auth_is_unknown(tmp_path: Path, monkeypatch) -> None:
    auth = tmp_path / "auth.json"
    auth.write_text("{}")
    original_read_text = Path.read_text

    def permission_error(path, *args, **kwargs):
        if path == auth:
            raise PermissionError("denied")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", permission_error)
    record = grok.read_usage(auth_path=auth)
    assert record["probe_state"] == "unknown"
    assert record["auth_state"] == "unknown"


@pytest.mark.parametrize(
    ("status", "probe_state", "auth_state"),
    [
        (401, "unusable", "invalid"),
        (402, "unusable", "valid"),
        (403, "unusable", "invalid"),
        (503, "unknown", "valid"),
    ],
)
def test_http_status_has_typed_probe_and_auth_outcomes(
    tmp_path: Path,
    monkeypatch,
    status: int,
    probe_state: str,
    auth_state: str,
) -> None:
    def fail(*_args, **_kwargs):
        raise grok.urllib.error.HTTPError(
            grok.BILLING_URL, status, "failure", {}, None
        )

    monkeypatch.setattr(grok.urllib.request, "urlopen", fail)
    record = grok.read_usage(auth_path=_auth(tmp_path))
    assert record["probe_state"] == probe_state
    assert record["auth_state"] == auth_state


def test_transport_failures_are_reported_without_a_percentage(tmp_path: Path) -> None:
    def boom(url, timeout):
        raise grok.GrokUsageError(
            "billing endpoint returned HTTP 401",
            probe_state=grok.PROBE_UNUSABLE,
            auth_state=grok.AUTH_INVALID,
        )

    record = grok.read_usage(auth_path=_auth(tmp_path), fetcher=boom)
    assert record["ok"] is False
    assert record["error"] == "billing endpoint returned HTTP 401"
    assert record["probe_state"] == "unusable"
    assert record["auth_state"] == "invalid"
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
