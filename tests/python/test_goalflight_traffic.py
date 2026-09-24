from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import goalflight_traffic as traffic  # noqa: E402


def test_live_traffic_excludes_terminal_and_pid_reused_dispatches(
    tmp_path: Path, monkeypatch, capsys
):
    dispatch_dir = tmp_path / "state" / "dispatch"
    dispatch_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("GOALFLIGHT_DISPATCH_DIR", str(dispatch_dir))

    live_pid = os.getpid()
    live_identity = {
        "pid": live_pid,
        "start_token": "fixture-live-process",
        "identity_available": True,
    }
    monkeypatch.setattr(
        traffic.goalflight_ledger,
        "process_identity",
        lambda pid: live_identity if pid == live_pid else None,
    )
    reused_identity = dict(live_identity)
    reused_identity["start_token"] = "different-process"

    def write_status(dispatch_id: str, **payload: object) -> None:
        status = {
            "schema": "goalflight.status.v1",
            "dispatch_id": dispatch_id,
            "state": "running",
            "worker_pid": live_pid,
            "worker_identity": live_identity,
            **payload,
        }
        (dispatch_dir / f"{dispatch_id}.status.json").write_text(
            json.dumps(status), encoding="utf-8"
        )

    write_status(
        "live-sol",
        agent="codex",
        model="gpt-5.6-sol",
        controller_label="alpha",
    )
    astra_tail = dispatch_dir / "live-astra.tail"
    astra_tail.write_text("model: gpt-6-astra\n", encoding="utf-8")
    write_status(
        "live-astra",
        agent="codex",
        controller_label="beta",
        tail_path=str(astra_tail),
    )
    write_status(
        "live-luna",
        agent="codex",
        model="gpt-5.5-luna",
        controller_label="alpha",
    )
    write_status(
        "live-grok",
        agent="grok-code",
        controller_label="gamma",
    )
    write_status(
        "unverified-sol",
        agent="codex",
        model="gpt-5.6-sol",
        controller_label="delta",
        worker_identity=None,
    )
    write_status(
        "terminal-reused",
        agent="codex",
        model="gpt-5.6-sol",
        controller_label="terminal",
        state="quota_exhausted",
    )
    write_status(
        "pid-reused",
        agent="codex",
        model="gpt-5.6-sol",
        controller_label="reused",
        worker_identity=reused_identity,
    )
    (dispatch_dir / "dead.status.json").write_text(
        json.dumps(
            {
                "schema": "goalflight.status.v1",
                "dispatch_id": "dead",
                "state": "running",
                "agent": "codex",
                "model": "gpt-5.5-luna",
                "controller_label": "dead",
                "worker_pid": 999_999_999,
                "worker_identity": {"pid": 999_999_999, "start_token": "gone"},
            }
        ),
        encoding="utf-8",
    )

    summary = traffic.live_workers_by_model()

    assert summary == {
        "total": 4,
        "unverified_total": 1,
        "models": {
            "gpt-5.6-sol": {
                "count": 1,
                "unverified": 1,
                "controllers": {"alpha": 1},
                "unverified_controllers": {"delta": 1},
            },
            "gpt-5.5-luna": {
                "count": 1,
                "unverified": 0,
                "controllers": {"alpha": 1},
                "unverified_controllers": {},
            },
            "gpt-6-astra": {
                "count": 1,
                "unverified": 0,
                "controllers": {"beta": 1},
                "unverified_controllers": {},
            },
            "grok": {
                "count": 1,
                "unverified": 0,
                "controllers": {"gamma": 1},
                "unverified_controllers": {},
            },
        },
    }
    rendered = traffic.render(summary)
    assert "MODEL" in rendered and "UNVERIFIED" in rendered
    assert "poor value vs grok" in rendered
    assert "use sparingly" in rendered
    assert "delta:1" in rendered
    assert traffic.live_mix_pointer(summary) == (
        "live mix: luna 1, grok 1, astra 1, sol 1; see /goal-flight traffic"
    )

    assert traffic.main(["--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload[traffic.JSON_KEY] == summary
