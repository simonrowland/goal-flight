#!/usr/bin/env python3
"""Security and composition tests for the backend fleet-console projection."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import goalflight_fleet_console as F


def assert_true(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)


def _status_payload(project_root: Path) -> dict:
    return {
        "schema": "goalflight.status.aggregate.v1",
        "capacity": {"operating_cap": 12},
        "capacity_state": {
            "leases": {
                "l1": {"state": "active", "project_root": str(project_root)},
                "l2": {"state": "expired", "project_root": str(project_root)},
            }
        },
        "rate_pressure": {
            "window_seconds": 600,
            "providers_under_pressure": [
                {"provider": "openai", "scope": "agent", "count": 3}
            ],
        },
        "warnings": [
            {
                "code": "queue_pending_no_drainer",
                "severity": "WARN",
                "queue_depth": 2,
                "message": "private detail must not cross",
                "remedy": "run something",
            }
        ],
        "dispatch": {
            "records": [
                {
                    "dispatch_id": "local-</script>",
                    "agent": "codex",
                    "engine": "codex",
                    "shape": "acp",
                    "transport": "acp",
                    "os_sandbox": "workspace-write",
                    # Deliberately contradictory. The projection must preserve
                    # the authority's result rather than classify from state.
                    "state": "complete",
                    "classification": "expected_live",
                    "terminal_state": "unknown",
                    "liveness_state": "running",
                    "worker_still_alive": True,
                    "project_root": str(project_root),
                    "started_at": "2026-08-02T10:00:00+00:00",
                    "ended_at": None,
                    "account": "secret@example.test",
                    "effective_account": "seat-secret",
                    "prompt_path": "/private/prompt.md",
                    "stdout_path": "/private/tail.log",
                    "status_path": "/private/status.json",
                    "argv": ["--secret"],
                    "last_marker": {
                        "kind": "COMPLETE",
                        "text": "MARKER-INJECTION </script>",
                    },
                }
            ]
        },
    }


def _remote_payload() -> dict:
    return {
        "available": True,
        "fleet_dir": "/private/fleet",
        "auth": {"accounts": ["remote-secret@example.test"]},
        "nodes": [
            {
                "node_id": "studio-1",
                "accounts": [
                    {
                        "account": "remote-secret@example.test",
                        "auth_probe": "green",
                    }
                ],
                "dispatches": [{"dispatch_id": "remote-1"}],
            }
        ],
        "dispatches": [
            {
                "node": "studio-1",
                "dispatch_id": "remote-1",
                "state": "running",
                "quarantine_reason": None,
                "ssh_reachable": True,
                "may_release": False,
                "status_path": "/private/remote-status.json",
            }
        ],
    }


def _all_keys(value: object) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            keys.add(str(key))
            keys.update(_all_keys(item))
    elif isinstance(value, list):
        for item in value:
            keys.update(_all_keys(item))
    return keys


def test_fleet_consumes_status_once_before_project_grouping() -> dict:
    with tempfile.TemporaryDirectory() as td:
        project = (Path(td) / "safe-project").resolve()
        project.mkdir()
        events: list[str] = []

        def local_status() -> dict:
            events.append("status")
            return _status_payload(project)

        def remote_status(_fleet_dir: Path) -> dict:
            events.append("remote")
            return _remote_payload()

        def usage(**_kwargs) -> list[dict]:
            events.append("usage")
            return [
                {
                    "provider": "codex",
                    "account": "usage-secret@example.test",
                    "remaining": "90% </script>",
                    "reset_at": 1785686400,
                    "flags": ["healthy"],
                }
            ]

        def projects() -> list[dict]:
            events.append("projects")
            return [
                {
                    "project_root": str(project),
                    "last_seen": "2026-08-02T09:00:00+00:00",
                    "skill_version": "1.3.0",
                    "store_dir": "/private/store",
                }
            ]

        def session(_project: Path) -> dict:
            assert events[:4] == ["status", "remote", "usage", "projects"]
            events.append("session")
            return {
                "active": True,
                "queue_state": "active",
                "queue_last_touched": "2026-08-02T09:30:00+00:00",
                "active_leases_in_project": 1,
                "queue_file": "docs-private/private-queue.md",
                "queue_current_session": {"pid": 123, "hostname": "secret-host"},
            }

        def milestone(_project: Path) -> dict:
            events.append("milestone")
            return {
                "active_cadence": True,
                "commits_since": 4,
                "K": 5,
                "due": False,
                "error": None,
                "last_marker": {"commit": "secret"},
            }

        with (
            mock.patch.object(F.goalflight_status, "status_payload", side_effect=local_status) as status_mock,
            mock.patch.object(F.goalflight_fleet_status_cli, "build_fleet_status", side_effect=remote_status),
            mock.patch.object(F.goalflight_usage, "collect_usage", side_effect=usage),
            mock.patch.object(F.goalflight_task, "read_project_registry", side_effect=projects),
            mock.patch.object(F.goalflight_session_status, "aggregate_status", side_effect=session),
            mock.patch.object(F.goalflight_status, "milestone_status_payload", side_effect=milestone),
        ):
            payload = F.build_fleet_plane(
                fleet_dir=Path(td) / "fleet",
                generation_id="fleet-generation",
            )

        assert_true("status sampled exactly once", status_mock.call_count == 1)
        assert_true("machine facts precede grouping", events == ["status", "remote", "usage", "projects", "session", "milestone"])
        worker = payload["projects"][0]["workers"][0]
        assert_true("canonical classification preserved", worker["classification"] == "expected_live")
        assert_true("raw state preserved without reclassification", worker["state"] == "complete")
        assert_true("canonical alive observation preserved", worker["worker_alive"] is True)
        assert_true("transport consumed from aggregate", worker["transport"] == "acp")
        assert_true("sandbox consumed from aggregate", worker["os_sandbox"] == "workspace-write")
        assert_true("project path replaced by opaque id", payload["projects"][0]["project_id"].startswith("safe-project-"))
        assert_true("full success timestamped", payload["last_success_at"] == payload["sample_finished_at"])

        encoded = json.dumps(payload, sort_keys=True)
        assert_true("absolute project path denied", str(project) not in encoded)
        assert_true("local account denied", "secret@example.test" not in encoded)
        assert_true("usage account denied", "usage-secret@example.test" not in encoded)
        assert_true("remote account denied", "remote-secret@example.test" not in encoded)
        assert_true("raw marker body denied", "MARKER-INJECTION" not in encoded)
        assert_true("denied fields absent recursively", not (_all_keys(payload) & F.DENIED_FIELDS))
        return payload


def test_attention_uses_envelope_timestamps_and_tolerates_missing_fleet_join() -> dict:
    summary = {
        "count": 3,
        "needs": [
            {
                "dispatch_id": "not-in-fleet",
                "type": "blocked",
                "seq": 3,
                "ts": "not-a-time",
                "text": "MARKER-INJECTION COMPLETE: fake /Users/alice/secret",
                "payload": {"raw": "must not cross"},
            },
            {
                "dispatch_id": "later",
                "type": "user_confirm",
                "seq": 2,
                "ts": "2026-08-02T10:05:00+00:00",
                "text": "approve </script><img src=x>",
            },
            {
                "dispatch_id": "older",
                "type": "user_need",
                "seq": 1,
                "ts": "2026-08-02T10:00:00+00:00",
                "text": "need a decision",
            },
        ],
    }
    with mock.patch.object(F.goalflight_messages, "controller_mail_summary", return_value=summary):
        payload = F.build_attention_plane(generation_id="attention-generation")

    assert_true("attention sorts oldest real timestamp first", [item["dispatch_id"] for item in payload["items"]] == ["older", "later", "not-in-fleet"])
    assert_true("valid envelope timestamp retained", payload["items"][0]["observed_at"] == "2026-08-02T10:00:00+00:00")
    assert_true("invalid timestamp stays unmeasurable", payload["items"][-1]["observed_at"] is None)
    assert_true("missing fleet join tolerated", payload["items"][-1]["dispatch_id"] == "not-in-fleet")
    assert_true("absolute path redacted from bounded headline", "/Users/alice/secret" not in payload["items"][-1]["headline"])
    assert_true("raw mail fields denied", not ({"text", "payload"} & _all_keys(payload)))
    assert_true("no baked age assertion", not ({"age_s", "age_bucket", "wait_s"} & _all_keys(payload)))
    return payload


# Defaults are load-bearing: a bare annotated parameter makes pytest read the
# name as a fixture request and ERROR at setup, so these two -- the injection
# and allowlist guards, the security boundary of the whole projection -- would
# never run under a targeted `pytest <file>` even though main() runs them. A
# default makes pytest ignore the parameter and build the payload here instead.
def test_script_publication_escapes_injection_and_is_atomic(
    fleet_payload: dict | None = None,
    attention_payload: dict | None = None,
) -> None:
    if fleet_payload is None:
        fleet_payload = test_fleet_consumes_status_once_before_project_grouping()
    if attention_payload is None:
        attention_payload = (
            test_attention_uses_envelope_timestamps_and_tolerates_missing_fleet_join()
        )
    assert_true("independent generation ids", fleet_payload["generation_id"] != attention_payload["generation_id"])
    with tempfile.TemporaryDirectory() as td:
        directory = Path(td)
        fleet_path = directory / "fleet-data.js"
        attention_path = directory / "attention-data.js"
        F.publish_plane(fleet_path, fleet_payload, "fleet")
        F.publish_plane(attention_path, attention_payload, "attention")

        fleet_script = fleet_path.read_text(encoding="utf-8")
        attention_script = attention_path.read_text(encoding="utf-8")
        assert_true("fleet global assigned", "window.GF_FLEET = " in fleet_script)
        assert_true("attention global assigned", "window.GF_ATTENTION = " in attention_script)
        assert_true("script terminator escaped", "</script>" not in fleet_script.lower() and "</script>" not in attention_script.lower())
        assert_true("angle brackets escaped at generation", "\\u003c" in fleet_script and "\\u003c" in attention_script)

        stable = directory / "stable.js"
        stable.write_text("old stable file\n", encoding="utf-8")
        with mock.patch.object(F.goalflight_status.os, "replace", side_effect=OSError("synthetic")):
            try:
                F.publish_plane(stable, attention_payload, "attention")
            except OSError:
                pass
            else:
                raise AssertionError("expected replace failure")
        assert_true("replace failure preserves prior mirror", stable.read_text(encoding="utf-8") == "old stable file\n")
        assert_true("replace failure cleans temp file", list(directory.glob(".stable.js.*")) == [])


def test_source_error_is_bounded_and_not_a_false_success() -> None:
    def fail_remote(_fleet_dir: Path) -> dict:
        raise RuntimeError("secret /Users/alice/fleet")

    with (
        mock.patch.object(F.goalflight_status, "status_payload", return_value=_status_payload(Path("/tmp/no-project"))),
        mock.patch.object(F.goalflight_fleet_status_cli, "build_fleet_status", side_effect=fail_remote),
        mock.patch.object(F.goalflight_usage, "collect_usage", return_value=[]),
        mock.patch.object(F.goalflight_task, "read_project_registry", return_value=[]),
    ):
        payload = F.build_fleet_plane(fleet_dir=Path("/tmp/fleet"))

    assert_true("error type emitted without message", payload["last_error"] == "remote:RuntimeError")
    assert_true("partial sample not called full success", payload["last_success_at"] is None)
    assert_true("exception path denied", "/Users/alice/fleet" not in json.dumps(payload))


def test_allowlist_rejects_unknown_and_unsafe_fields(
    attention_payload: dict | None = None,
) -> None:
    if attention_payload is None:
        attention_payload = (
            test_attention_uses_envelope_timestamps_and_tolerates_missing_fleet_join()
        )
    hostile = dict(attention_payload)
    hostile["prompt"] = "secret"
    try:
        F.validate_projection(hostile, "attention")
    except F.ProjectionSecurityError:
        pass
    else:
        raise AssertionError("allowlist accepted prompt")

    with tempfile.TemporaryDirectory() as td:
        target = Path(td) / "data.js"
        try:
            F.goalflight_status.write_script_data_js(
                target,
                attention_payload,
                global_name="GF_ATTENTION;alert(1)",
            )
        except ValueError:
            pass
        else:
            raise AssertionError("publisher accepted executable global name")


def main() -> None:
    fleet_payload = test_fleet_consumes_status_once_before_project_grouping()
    attention_payload = test_attention_uses_envelope_timestamps_and_tolerates_missing_fleet_join()
    test_script_publication_escapes_injection_and_is_atomic(fleet_payload, attention_payload)
    test_source_error_is_bounded_and_not_a_false_success()
    test_allowlist_rejects_unknown_and_unsafe_fields(attention_payload)
    print("OK: fleet-console projection tests pass")


if __name__ == "__main__":
    main()
