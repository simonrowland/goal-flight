"""Ledger status human text: silence uniform none-sandbox unless --verbose."""

from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_ledger as ledger  # noqa: E402


def _row(
    dispatch_id: str,
    *,
    classification: str = "complete",
    agent: str = "codex",
    pid: int | None = 1,
    state: str = "complete",
    os_sandbox: object = None,
) -> dict:
    return {
        "dispatch_id": dispatch_id,
        "classification": classification,
        "agent": agent,
        "worker_pid": pid,
        "state": state,
        "os_sandbox": os_sandbox,
    }


def _none_sandbox() -> dict:
    return {
        "requested_profile": None,
        "supported_profile": None,
        "enforced_profile": None,
    }


def _payload(rows: list[dict], surplus: list[dict] | None = None) -> dict:
    return {
        "schema": ledger.SCHEMA,
        "state_dir": "/tmp/ledger-status-test",
        "records": rows,
        "surplus_processes": surplus or [],
    }


def _legacy_suffix(posture: object) -> str:
    if not isinstance(posture, dict):
        return ""
    if not any(
        key in posture
        for key in ("requested_profile", "supported_profile", "enforced_profile")
    ):
        return ""
    return (
        " sandbox"
        f" requested={posture.get('requested_profile') or 'none'}"
        f" supported={posture.get('supported_profile') or 'none'}"
        f" enforced={posture.get('enforced_profile') or 'none'}"
    )


def case_status_omits_uniform_none_sandbox() -> None:
    payload = _payload(
        [
            _row("a", os_sandbox=_none_sandbox()),
            _row("b", os_sandbox=_none_sandbox()),
            _row("c"),
        ]
    )
    lines = ledger.format_status_lines(payload, limit=20)
    text = "\n".join(lines)
    assert lines[0] == "dispatch ledger: /tmp/ledger-status-test"
    assert "requested=none" not in text
    assert "sandbox" not in text
    assert any(line.startswith("- complete: a ") for line in lines)
    assert any(line.startswith("- complete: b ") for line in lines)


def case_status_says_uniform_non_none_once() -> None:
    posture = {
        "requested_profile": "read-only",
        "supported_profile": "off",
        "enforced_profile": None,
    }
    payload = _payload([_row("rej", os_sandbox=posture), _row("rej2", os_sandbox=posture)])
    lines = ledger.format_status_lines(payload, limit=20)
    text = "\n".join(lines)
    assert text.count("sandbox requested=read-only supported=off enforced=none") == 1
    assert lines[1] == "sandbox requested=read-only supported=off enforced=none"
    assert "sandbox requested=" not in lines[2]
    assert "sandbox requested=" not in lines[3]


def case_status_shows_per_row_when_mixed() -> None:
    payload = _payload(
        [
            _row("none", os_sandbox=_none_sandbox()),
            _row(
                "ro",
                os_sandbox={
                    "requested_profile": "read-only",
                    "supported_profile": "off",
                    "enforced_profile": None,
                },
            ),
        ]
    )
    lines = ledger.format_status_lines(payload, limit=20)
    text = "\n".join(lines)
    assert "requested=none supported=none enforced=none" not in text
    assert "sandbox requested=read-only supported=off enforced=none" in text
    assert not any(line.startswith("sandbox ") for line in lines)
    verbose = "\n".join(ledger.format_status_lines(payload, limit=20, verbose=True))
    assert "sandbox requested=none supported=none enforced=none" in verbose


def case_status_shows_each_differing_non_none() -> None:
    payload = _payload(
        [
            _row(
                "ro",
                os_sandbox={
                    "requested_profile": "read-only",
                    "supported_profile": "off",
                    "enforced_profile": None,
                },
            ),
            _row(
                "ww",
                os_sandbox={
                    "requested_profile": "workspace-write",
                    "supported_profile": "workspace-write",
                    "enforced_profile": "workspace-write",
                },
            ),
        ]
    )
    text = "\n".join(ledger.format_status_lines(payload, limit=20))
    assert "sandbox requested=read-only supported=off enforced=none" in text
    assert (
        "sandbox requested=workspace-write supported=workspace-write enforced=workspace-write"
        in text
    )


def case_status_verbose_recovers_per_row_verbatim() -> None:
    rows = [
        _row("a", os_sandbox=_none_sandbox()),
        _row("b", os_sandbox=_none_sandbox()),
        _row("c"),
    ]
    payload = _payload(rows, surplus=[{"pid": 9, "comm": "zsh", "args": "zsh -c x"}])
    verbose = ledger.format_status_lines(payload, limit=20, verbose=True)
    expected = [
        "dispatch ledger: /tmp/ledger-status-test",
        f"- complete: a agent=codex pid=1 state=complete{_legacy_suffix(rows[0]['os_sandbox'])}",
        f"- complete: b agent=codex pid=1 state=complete{_legacy_suffix(rows[1]['os_sandbox'])}",
        f"- complete: c agent=codex pid=1 state=complete{_legacy_suffix(rows[2]['os_sandbox'])}",
        "surplus worker-like processes:",
        "- pid=9 comm=zsh args=zsh -c x",
    ]
    assert verbose == expected
    compact = ledger.format_status_lines(payload, limit=20, verbose=False)
    assert compact != verbose
    assert "requested=none supported=none enforced=none" in "\n".join(verbose)
    assert "requested=none" not in "\n".join(compact)
    assert compact[1].startswith("- complete: a ")


def case_status_json_unchanged() -> None:
    payload = _payload([_row("a", os_sandbox=_none_sandbox())])
    original = ledger.status_payload
    ledger.status_payload = lambda: payload
    try:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = ledger.cmd_status(
                SimpleNamespace(json=True, limit=20, verbose=False)
            )
        assert rc == 0
        data = json.loads(buf.getvalue())
        assert data == payload
        assert data["records"][0]["os_sandbox"]["requested_profile"] is None
    finally:
        ledger.status_payload = original
    human = "\n".join(ledger.format_status_lines(payload))
    assert "requested=none" not in human


def case_status_json_cli_keeps_sandbox_payload() -> None:
    live = {
        "schema": ledger.SCHEMA,
        "state_dir": "/tmp/x",
        "records": [_row("a", os_sandbox=_none_sandbox())],
        "surplus_processes": [],
    }
    buf = io.StringIO()
    with redirect_stdout(buf):
        # Patch status_payload so we do not read live controller state.
        original = ledger.status_payload
        ledger.status_payload = lambda: live
        try:
            rc = ledger.cmd_status(SimpleNamespace(json=True, limit=20, verbose=True))
        finally:
            ledger.status_payload = original
    assert rc == 0
    data = json.loads(buf.getvalue())
    assert data == live
    assert data["records"][0]["os_sandbox"]["requested_profile"] is None


def case_status_exit_zero() -> None:
    live = _payload([])
    original = ledger.status_payload
    ledger.status_payload = lambda: live
    try:
        buf = io.StringIO()
        with redirect_stdout(buf):
            assert ledger.cmd_status(SimpleNamespace(json=False, limit=20, verbose=False)) == 0
            assert ledger.cmd_status(SimpleNamespace(json=False, limit=20, verbose=True)) == 0
            assert ledger.cmd_status(SimpleNamespace(json=True, limit=20, verbose=False)) == 0
    finally:
        ledger.status_payload = original


def case_status_unhealthy_sandbox_still_reports() -> None:
    payload = _payload(
        [
            _row(
                "blocked",
                classification="blocked_os_sandbox",
                state="blocked_os_sandbox",
                os_sandbox={
                    "requested_profile": "read-only",
                    "supported_profile": "off",
                    "enforced_profile": None,
                },
            )
        ]
    )
    text = "\n".join(ledger.format_status_lines(payload))
    assert "sandbox requested=read-only supported=off enforced=none" in text
    assert "blocked_os_sandbox: blocked" in text


def main() -> None:
    case_status_omits_uniform_none_sandbox()
    case_status_says_uniform_non_none_once()
    case_status_shows_per_row_when_mixed()
    case_status_shows_each_differing_non_none()
    case_status_verbose_recovers_per_row_verbatim()
    case_status_json_unchanged()
    case_status_json_cli_keeps_sandbox_payload()
    case_status_exit_zero()
    case_status_unhealthy_sandbox_still_reports()
    print("OK: ledger status terse tests pass")


if __name__ == "__main__":
    main()
