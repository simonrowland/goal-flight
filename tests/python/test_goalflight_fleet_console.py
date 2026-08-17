#!/usr/bin/env python3
"""Security and composition tests for the backend fleet-console projection."""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import goalflight_fleet_console as F
import goalflight_fleet_console_producer as producer
import goalflight_ledger


LAST_FAST_PLANE_MEASUREMENT: dict[str, float | int] = {}


def assert_true(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)


def _script_payload(path: Path, global_name: str) -> dict:
    text = path.read_text(encoding="utf-8")
    prefix = f"window.{global_name} = "
    assignment = text[text.index(prefix) + len(prefix) :]
    assert assignment.endswith(";\n")
    return json.loads(assignment[:-2])


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

        def local_status(**kwargs: object) -> dict:
            assert kwargs == {"reconcile_terminal_history": False}
            events.append("status")
            return _status_payload(project)

        def remote_status(_fleet_dir: Path, **kwargs: object) -> dict:
            assert kwargs == {"live_only": True}
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
            mock.patch.object(F.goalflight_session_status, "aggregate_status", side_effect=session) as session_mock,
            mock.patch.object(F.goalflight_status, "milestone_status_payload", side_effect=milestone) as milestone_mock,
        ):
            payload = F.build_fleet_plane(
                fleet_dir=Path(td) / "fleet",
                generation_id="fleet-generation",
            )

        assert_true("status sampled exactly once", status_mock.call_count == 1)
        assert_true(
            "all machine facts finish before grouping regardless of concurrent order",
            len(events) == 4
            and set(events) == {"status", "remote", "usage", "projects"},
        )
        assert_true("fast plane skips deep session scans", session_mock.call_count == 0)
        assert_true("fast plane skips per-project milestone scans", milestone_mock.call_count == 0)
        worker = payload["projects"][0]["workers"][0]
        assert_true("canonical classification preserved", worker["classification"] == "expected_live")
        assert_true("raw state preserved without reclassification", worker["state"] == "complete")
        assert_true("canonical alive observation preserved", worker["worker_alive"] is True)
        assert_true("transport consumed from aggregate", worker["transport"] == "acp")
        assert_true("sandbox consumed from aggregate", worker["os_sandbox"] == "workspace-write")
        assert_true("project path replaced by opaque id", payload["projects"][0]["project_id"].startswith("safe-project-"))
        assert_true("full success timestamped", payload["last_success_at"] == payload["sample_finished_at"])
        assert_true("fleet producer cadence stamped", payload["cadence_seconds"] == 60)
        assert_true("active lease count comes from machine sample", payload["projects"][0]["session"]["active_leases"] == 1)
        assert_true("deep milestone data stays unknown on fast plane", payload["projects"][0]["milestone"]["available"] is False)

        encoded = json.dumps(payload, sort_keys=True)
        assert_true("absolute project path denied", str(project) not in encoded)
        assert_true("local account denied", "secret@example.test" not in encoded)
        assert_true("usage account denied", "usage-secret@example.test" not in encoded)
        assert_true("remote account denied", "remote-secret@example.test" not in encoded)
        assert_true("raw marker body denied", "MARKER-INJECTION" not in encoded)
        assert_true("denied fields absent recursively", not (_all_keys(payload) & F.DENIED_FIELDS))
        return payload


def test_global_history_count_excludes_unreachable_remote_terminals_mutation_pair() -> None:
    ancient_remote = [
        {
            "dispatch_id": f"ancient-remote-{index}",
            "node": "studio-1",
            "state": "complete",
            "classification": "complete",
            "terminal_state": "complete",
            "ended_at": f"2020-01-01T00:00:{index:02d}+00:00",
            "ssh_reachable": True,
            "may_release": False,
        }
        for index in range(8)
    ]
    local = {
        "capacity": {},
        "capacity_state": {"leases": {}},
        "rate_pressure": {},
        "dispatch": {"records": []},
        "warnings": [],
    }
    remote = {
        "available": True,
        "nodes": [{"node_id": "studio-1", "accounts": [], "dispatches": ancient_remote}],
        "dispatches": ancient_remote,
    }
    with (
        mock.patch.object(F.goalflight_status, "status_payload", return_value=local),
        mock.patch.object(F.goalflight_fleet_status_cli, "build_fleet_status", return_value=remote),
        mock.patch.object(F.goalflight_usage, "collect_usage", return_value=[]),
        mock.patch.object(F.goalflight_task, "read_project_registry", return_value=[]),
    ):
        payload = F.build_fleet_plane(generation_id="remote-history-regression")

    assert_true("five ancient remote terminals stay on the fast plane", len(payload["remote"]["workers"]) == 5)
    assert_true("remote projection records the three omitted rows", payload["remote"]["history_excluded"] == 3)
    # Mutation control: adding the remote-only counter back to the global
    # counter recreates the unreachable '+3 in history' review finding.
    legacy_global_count = payload["history_excluded"] + payload["remote"]["history_excluded"]
    assert_true("legacy global count would advertise unreachable rows", legacy_global_count == 3)
    assert_true("global history advertises only retrievable project rows", payload["history_excluded"] == 0)


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
        payload = F.build_attention_plane(
            generation_id="attention-generation",
            project_roots=[],
        )

    assert_true("attention sorts oldest real timestamp first", [item["dispatch_id"] for item in payload["items"]] == ["older", "later", "not-in-fleet"])
    assert_true("valid envelope timestamp retained", payload["items"][0]["observed_at"] == "2026-08-02T10:00:00+00:00")
    assert_true("attention producer cadence stamped", payload["cadence_seconds"] == 20)
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
    def fail_remote(_fleet_dir: Path, **_kwargs: object) -> dict:
        raise RuntimeError("secret /Users/alice/fleet")

    with (
        mock.patch.object(F.goalflight_status, "status_payload", return_value=_status_payload(Path("/tmp/no-project"))),
        mock.patch.object(F.goalflight_fleet_status_cli, "build_fleet_status", side_effect=fail_remote),
        mock.patch.object(F.goalflight_usage, "collect_usage", return_value=[]),
        mock.patch.object(F.goalflight_task, "read_project_registry", return_value=[]),
    ):
        payload = F.build_fleet_plane(fleet_dir=Path("/tmp/fleet"))

    assert_true("error type emitted without message", str(payload["last_error"]).startswith("remote:RuntimeError"))
    assert_true("source error includes operator action", "install-fleet-console.sh --status --plane fleet" in str(payload["last_error"]))
    assert_true("partial sample not called full success", payload["last_success_at"] is None)
    assert_true("exception path denied", "/Users/alice/fleet" not in json.dumps(payload))


def test_limit_display_uses_measured_kind_and_marks_legacy_unknown() -> None:
    exhausted = F._worker_display_verdict(
        {"state": "quota_exhausted", "limit_kind": "exhausted"}
    )
    legacy = F._worker_display_verdict({"state": "rate_limited"})
    mixed = F._worker_display_verdict(
        {"state": "worker_dead", "limit_kind": "exhausted"}
    )

    assert_true("exhausted state displayed", exhausted["display_state"] == "quota_exhausted")
    assert_true("exhausted terminal", exhausted["is_terminal"] is True)
    assert_true("legacy state does not imply transient", legacy["display_state"] == "limit_unknown")
    assert_true("legacy remains terminal", legacy["is_terminal"] is True)
    assert_true("measured kind beats bare death", mixed["display_state"] == "quota_exhausted")


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

    hostile_action = json.loads(json.dumps(attention_payload))
    hostile_action["items"][0]["action"] = (
        "python3 /tmp/goalflight_messages.py listen-auto --project-root /tmp/project"
    )
    try:
        F.validate_projection(hostile_action, "attention")
    except F.ProjectionSecurityError:
        pass
    else:
        raise AssertionError("allowlist accepted an imposter listener command")

    valid_action = json.loads(json.dumps(attention_payload))
    valid_action["items"][0]["action"] = F.goalflight_wake.listener_start_command(
        Path("/tmp/project with spaces"),
        controller_label="main controller",
    )
    F.validate_projection(valid_action, "attention")

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


def test_registry_pass_is_bounded_and_reports_what_it_skipped() -> None:
    """The registry is unbounded in practice; the pass over it must not be.

    Measured on a real machine: 1433 registered roots -- the registry keeps a
    root for every project any dispatch ever ran in, including hundreds of
    throwaway per-ticket worktrees -- at ~0.85s each for session+milestone.
    A full serial pass is ~20 minutes against a ~60s drain tick, which is why
    the producer hung and emitted nothing at all.

    Three things are pinned, each with its own way of silently regressing:
    the cap actually applies; the TOTAL is still reported, so a bounded sample
    can never read as "this is everything"; and projects with work in flight
    outrank merely-recent ones, since those are what the operator is watching.
    """
    registry = [
        {"project_root": f"/tmp/p{i:04d}", "last_seen": f"2026-01-{(i % 28) + 1:02d}T00:00:00+00:00"}
        for i in range(400)
    ]
    registry.append({"project_root": "/tmp/stale-but-busy", "last_seen": "1999-01-01T00:00:00+00:00"})

    sampled, total = F._registered_projects(
        registry,
        active_roots={str(Path("/tmp/stale-but-busy").resolve())},
        limit=5,
    )
    assert_true("cap applied", len(sampled) == 5)
    assert_true("total reported, not the capped length", total == 401)
    roots = [item["root"] for item in sampled]
    assert_true(
        "a project with work in flight outranks newer idle ones",
        str(Path("/tmp/stale-but-busy").resolve()) in roots,
    )

    unbounded, total_again = F._registered_projects(registry, limit=None)
    assert_true("limit=None samples everything", len(unbounded) == 401)
    assert_true("total is stable regardless of limit", total_again == 401)


def test_degraded_sample_exits_nonzero_instead_of_looking_healthy() -> None:
    """A sample whose sources failed must not report success.

    Source failures are captured as data so a partial payload still publishes.
    That is deliberate -- but it also means a run where everything failed emits
    zeros and empty lists, which a page renders as a calm, healthy fleet and a
    scheduler records as a clean tick. The exit code is the only channel that
    reaches both a cron and a human, so it has to carry the verdict.
    """
    import contextlib
    import io

    real = F.goalflight_status.status_payload
    try:
        F.goalflight_status.status_payload = lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("source down")
        )
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = F.main(["fleet"])
        assert_true("degraded sample exits nonzero", code == 1)
        assert_true("degraded exit names the failing source", "local_status" in err.getvalue())
        assert_true("degraded exit is legible as degraded", "DEGRADED" in err.getvalue())
    finally:
        F.goalflight_status.status_payload = real


def test_unrecognised_attention_type_is_dropped_not_promoted() -> None:
    """Automation must not be laundered into a pending human decision.

    controller_mail_summary hands over types that already describe themselves.
    done-suggest / resume-ready / parallel-ready are a controller prompting
    ITSELF ("worker says done: b-663 -> review?"). The old fallback coerced every
    unrecognised type to user_need, so 137 of 202 rows on a real machine told the
    operator a human decision was pending that no human had ever been asked for.
    Rendered on a phone that was 57 screens of demand.

    Pinned as a DROP rather than a deny-list of known automation kinds: a
    deny-list rots the moment a producer adds a kind, and the failure mode of
    forgetting is silent laundering again. Also pinned is that the four real
    kinds still survive -- over-correcting into dropping everything would be
    just as wrong and far quieter.
    """
    summary = {"needs": [
        {"type": "done-suggest", "ts": "2026-08-02T10:00:00+00:00", "dispatch_id": "a", "text": "worker says done"},
        {"type": "resume-ready", "ts": "2026-08-02T10:00:00+00:00", "dispatch_id": "b", "text": "276 ready"},
        {"type": "parallel-ready", "ts": "2026-08-02T10:00:00+00:00", "dispatch_id": "c", "text": "fan out"},
        {"type": "totally-new-automation-kind", "ts": "2026-08-02T10:00:00+00:00", "dispatch_id": "d", "text": "future"},
        {"type": "controller-hung", "ts": "2026-08-02T10:00:00+00:00", "dispatch_id": "spoof", "text": "unmeasured claim"},
        {"type": "user_need", "ts": "2026-08-02T10:00:00+00:00", "dispatch_id": "e", "text": "decide"},
        {"type": "user_confirm", "ts": "2026-08-02T10:00:00+00:00", "dispatch_id": "f", "text": "approve"},
        {"type": "blocked", "ts": "2026-08-02T10:00:00+00:00", "dispatch_id": "g", "text": "stuck"},
        {"type": "advisory", "ts": "2026-08-02T10:00:00+00:00", "dispatch_id": "h", "text": "fyi"},
    ]}
    rows = F._attention_rows(summary)
    kinds = sorted(row["kind"] for row in rows)
    assert_true(
        "only real operator attention survives",
        kinds == ["advisory", "blocked", "user_confirm", "user_need"],
    )
    assert_true(
        "an unknown FUTURE kind is dropped, not promoted",
        all(row["dispatch_id"] != "d" for row in rows),
    )
    assert_true(
        "mail cannot spoof a lock-derived HUNG verdict",
        all(row["dispatch_id"] != "spoof" for row in rows),
    )


def test_controller_authored_mail_reaches_the_attention_plane() -> None:
    """The channel a human actually writes on must be read.

    The plane was sourced only from the worker-MARKER stream (user_need /
    user_confirm / blocked). Controller-addressed types -- controller-question,
    controller-notice, coordination -- existed in the store in the hundreds and
    were surfaced to nobody, so a peer controller's question reached no one and
    the operator relayed messages by hand between sessions.

    Mapped onto the existing kinds rather than new ones so the renderer and the
    allowlist stay closed: a question needs an answer (user_need), the rest are
    informational (advisory). Genuine automation must still be dropped, so that
    is asserted here too -- widening the map until everything gets through would
    re-create the laundering this same function was just fixed for.
    """
    summary = {"needs": [
        {"type": "controller-question", "ts": "2026-08-02T10:00:00+00:00", "dispatch_id": "peer", "text": "can I merge?"},
        {"type": "controller-notice", "ts": "2026-08-02T10:00:00+00:00", "dispatch_id": "peer", "text": "fyi"},
        {"type": "coordination", "ts": "2026-08-02T10:00:00+00:00", "dispatch_id": "peer", "text": "merge train"},
        {"type": "blocked", "ts": "2026-08-02T10:00:00+00:00", "dispatch_id": "w1", "text": "worker stuck"},
        {"type": "done-suggest", "ts": "2026-08-02T10:00:00+00:00", "dispatch_id": "auto", "text": "review + accept?"},
    ]}
    rows = F._attention_rows(summary)
    by_id = {row["dispatch_id"]: row["kind"] for row in rows}
    assert_true("a peer controller's question needs an answer", by_id.get("peer") is not None)
    kinds = sorted(row["kind"] for row in rows)
    assert_true("question -> need, notices -> advisory, marker survives",
                kinds == ["advisory", "advisory", "blocked", "user_need"])
    assert_true("automation is STILL dropped", "auto" not in by_id)


def test_registry_membership_is_not_a_statement_about_sampling() -> None:
    """A project outside the deep-sample cap is still registered.

    registered_roots used to be built from the CAPPED sample, so the 13th
    registered project -- one with a live dispatch record, therefore visible --
    was emitted as registered=false purely because that tick did not sample it.
    Two different questions had one answer.
    """
    machine = {
        "schema": "goalflight.status.aggregate.v1",
        "capacity": {},
        "capacity_state": {"leases": {}},
        "rate_pressure": {},
        "warnings": [],
        "dispatch": {"records": [
            {"dispatch_id": "w1", "project_root": "/tmp/outside-the-cap", "state": "running"},
        ]},
    }
    sampled = [{"root": F._canonical_root("/tmp/inside"), "last_seen": None, "skill_version": None}]
    projects, _, _, _ = F._project_rows(
        machine, sampled, [],
        # Canonical form: _canonical_root resolves /tmp -> /private/tmp on
        # macOS, and the join is on the resolved value.
        all_registered_roots={
            F._canonical_root("/tmp/inside"),
            F._canonical_root("/tmp/outside-the-cap"),
        },
    )
    by_name = {p["name"]: p for p in projects}
    assert_true("the unsampled project still reports registered",
                by_name["outside-the-cap"]["registered"] is True)
    assert_true(
        "live project gets only machine-derived session data, not a deep sample",
        by_name["outside-the-cap"]["session"]["available"] is False
        and by_name["outside-the-cap"]["session"]["active"] is None
        and by_name["outside-the-cap"]["session"]["queue_state"] is None
        and by_name["outside-the-cap"]["milestone"]["available"] is False,
    )


def test_fast_plane_project_classes_are_live_only_mutation_pair() -> None:
    """Idle registry/worktree rows and ended generations stay in slow history."""
    with tempfile.TemporaryDirectory() as td:
        temp_root = Path(td)
        live_root = (temp_root / "live-project").resolve()
        lease_root = (temp_root / "lease-only-project").resolve()
        idle_worktree = (temp_root / "old-worktree").resolve()
        for root in (live_root, lease_root, idle_worktree):
            root.mkdir()
        ancient = "2020-01-01T00:00:00+00:00"
        terminal_status = temp_root / "terminal-status.json"
        terminal_status.write_text(
            json.dumps({"dispatch_id": "live-terminal", "state": "complete"}),
            encoding="utf-8",
        )
        machine = {
            "schema": "goalflight.status.aggregate.v1",
            "capacity": {"operating_cap": 12},
            "capacity_state": {
                "leases": {
                    "active": {
                        "state": "active",
                        "project_root": str(lease_root),
                    },
                    "ended": {
                        "state": "released",
                        "project_root": str(idle_worktree),
                    },
                }
            },
            "rate_pressure": {},
            "warnings": [],
            "dispatch": {
                "records": [
                    {
                        "dispatch_id": "live-worker",
                        "project_root": str(live_root),
                        "state": "running",
                        "classification": "expected_live",
                    },
                    {
                        "dispatch_id": "live-terminal",
                        "project_root": str(live_root),
                        "state": "complete",
                        "classification": "complete",
                        "terminal_state": "complete",
                        "ended_at": ancient,
                        "status_path": str(terminal_status),
                    },
                    {
                        "dispatch_id": "idle-terminal",
                        "project_root": str(idle_worktree),
                        "state": "complete",
                        "classification": "complete",
                        "terminal_state": "complete",
                        "ended_at": ancient,
                    },
                ]
            },
        }
        registry = [
            {
                "project_root": str(root),
                "last_seen": "2030-01-01T00:00:00+00:00",
            }
            for root in (live_root, lease_root, idle_worktree)
        ]
        probed_statuses: list[str] = []

        def status_probe(record: dict) -> dict:
            probed_statuses.append(str(record.get("dispatch_id")))
            return {
                "dispatch_id": record.get("dispatch_id"),
                "state": record.get("state"),
            }

        with (
            mock.patch.dict(os.environ, _controller_test_env(temp_root), clear=False),
            mock.patch.object(F.goalflight_status, "status_payload", return_value=machine),
            mock.patch.object(F.goalflight_status, "_status_json_payload", side_effect=status_probe),
            mock.patch.object(F.goalflight_fleet_status_cli, "build_fleet_status", return_value={}),
            mock.patch.object(F.goalflight_usage, "collect_usage", return_value=[]),
            mock.patch.object(F.goalflight_task, "read_project_registry", return_value=registry),
            mock.patch.object(F.goalflight_session_status, "aggregate_status", return_value={}),
            mock.patch.object(F.goalflight_status, "milestone_status_payload", return_value={}),
        ):
            payload = F.build_fleet_plane(generation_id="record-class-mutation-pair")

    names = {project["name"] for project in payload["projects"]}
    assert_true(
        "only live-work and active-lease projects cross the fast plane",
        names == {"live-project", "lease-only-project"},
    )
    assert_true(
        "idle registry/worktree project is counted but not deep-sampled",
        payload["registry_total"] == 3
        and payload["registry_deep_sampled"] == 2,
    )
    assert_true(
        "the hidden terminal remains disclosed through slow-history count",
        payload["history_excluded"] == 1,
    )
    assert_true(
        "retained warm terminal rows reconcile their status sidecars",
        probed_statuses.count("live-terminal") >= 1,
    )


def test_detached_orphan_with_matching_identity_stays_fast_without_lease_mutation_pair() -> None:
    """Controller exit cannot erase a still-matching detached worker."""
    with tempfile.TemporaryDirectory() as td:
        temp_root = Path(td)
        project_root = (temp_root / "orphan-project").resolve()
        project_root.mkdir()
        orphan = {
            "dispatch_id": "orphan-still-live",
            "project_root": str(project_root),
            "state": "orphaned",
            "reason": "controller_dead",
            "classification": "expected_live",
            "terminal_state": "unknown",
            "detached": True,
            "worker_still_alive": True,
            "started_at": "2030-01-01T00:00:00+00:00",
        }
        machine = {
            "schema": "goalflight.status.aggregate.v1",
            "capacity": {"operating_cap": 12},
            "capacity_state": {"leases": {}},
            "rate_pressure": {},
            "warnings": [],
            "dispatch": {"records": [orphan]},
        }
        registry = [{"project_root": str(project_root), "last_seen": "2030-01-01T00:00:00+00:00"}]
        attention_scans: list[str] = []

        def capture_attention_roots(
            roots: list[Path],
            _machine_status: dict,
            **_kwargs: object,
        ) -> list[dict]:
            attention_scans.extend(str(root) for root in roots)
            return []

        with (
            mock.patch.dict(os.environ, _controller_test_env(temp_root), clear=False),
            mock.patch.object(F.goalflight_status, "status_payload", return_value=machine),
            mock.patch.object(F.goalflight_fleet_status_cli, "build_fleet_status", return_value={}),
            mock.patch.object(F.goalflight_usage, "collect_usage", return_value=[]),
            mock.patch.object(F.goalflight_task, "read_project_registry", return_value=registry),
            mock.patch.object(F.goalflight_messages, "controller_mail_summary", return_value={"needs": []}),
            mock.patch.object(F, "_controller_attention_rows", side_effect=capture_attention_roots),
        ):
            fleet = F.build_fleet_plane(generation_id="orphan-live-fleet")
            F.build_attention_plane(generation_id="orphan-live-attention")

    workers = [worker for project in fleet["projects"] for worker in project["workers"]]
    assert_true(
        "matching detached orphan remains a visible live worker without a capacity lease",
        len(workers) == 1
        and workers[0]["dispatch_id"] == "orphan-still-live"
        and workers[0]["display_state"] == "running"
        and workers[0]["is_terminal"] is False,
    )
    assert_true(
        "matching detached orphan keeps its project eligible for attention scanning",
        attention_scans == [str(project_root)],
    )
    dead_mutant = {
        **orphan,
        "classification": "worker_dead",
        "worker_still_alive": False,
    }
    assert_true(
        "identity-dead mutation leaves the fast project set",
        str(project_root)
        not in F._fast_project_roots(
            {**machine, "dispatch": {"records": [dead_mutant]}}
        ),
    )


def test_live_worker_count_ignores_permanent_terminal_history() -> None:
    """"How many are running" must not answer "how much history is there".

    The ledger keeps terminal records forever -- 1541 on a real machine against
    37 actually running. A record can also read state="running" while its own
    classification says worker_dead, so the check has to consider every field
    the record uses to describe itself, not just the most convenient one.
    """
    records = [
        {"dispatch_id": "live", "state": "running", "classification": "expected_live"},
        {"dispatch_id": "done", "state": "complete"},
        {"dispatch_id": "lying", "state": "running", "classification": "worker_dead"},
        {"dispatch_id": "failed", "state": "running", "terminal_state": "failed"},
    ]
    row = F._machine_row({"dispatch": {"records": records}, "capacity": {}, "capacity_state": {}})
    assert_true(f"only the genuinely live worker counts (got {row['local_workers']})",
                row["local_workers"] == 1)


def test_controller_liveness_projection_rejects_unregistered_scalar() -> None:
    try:
        F._validate_scalar_types(
            {"controller_liveness_state": "HUNG injected-class"}
        )
    except F.ProjectionSecurityError:
        pass
    else:
        raise AssertionError("controller liveness enum accepted an unregistered scalar")


def _controller_test_env(temp_root: Path) -> dict[str, str]:
    return {
        "GOALFLIGHT_MESSAGES_DIR": str(temp_root / "messages"),
        "GOALFLIGHT_FLEET_DIR": str(temp_root / "fleet"),
        "GOALFLIGHT_JOURNAL_DIR": str(temp_root / "state"),
        "GOALFLIGHT_TASK_STORE_DIR": str(temp_root / "task-store"),
        "GOALFLIGHT_STATE_DIR": str(temp_root / "state-root"),
        "GOALFLIGHT_WAKE_LEDGER_DIR": str(temp_root / "wake-ledger"),
        "GOAL_FLIGHT_PIDFILE_DIR": str(temp_root / "pids"),
        "GOALFLIGHT_FLEET_CONSOLE_OUTPUT_DIR": str(temp_root / "console"),
        "GOALFLIGHT_TEST_MODE": "1",
    }


def _running_attempt(
    authority: F.goalflight_journal.Journal,
    dispatch_id: str,
    *,
    owner_controller_label: str | None = None,
    owner_session_nonce: str | None = None,
) -> None:
    prepared = authority.prepare_attempt(
        dispatch_id,
        owner_controller_label=owner_controller_label,
        owner_session_nonce=owner_session_nonce,
    )
    assert_true("attempt prepared", prepared.committed and prepared.value is not None)
    attempt = prepared.value
    assert attempt is not None
    starting = authority.start_attempt(attempt.attempt_id, attempt.launch_token)
    assert_true("attempt starting", starting.committed and starting.value is not None)
    started = starting.value
    assert started is not None
    running = authority.mark_attempt_running(
        started.attempt_id,
        started.launch_token,
        launch_epoch=started.launch_epoch,
        worker_instance={"pid": os.getpid(), "source": "fleet-console-test"},
    )
    assert_true("attempt running", running.committed)


def _assert_controller_state(
    expected: str,
    *,
    holder_mode: str,
    waiter_count: int,
    attempt_mode: str,
) -> None:
    """Exercise one classifier branch through real temp journal/flock inputs."""
    with tempfile.TemporaryDirectory() as td:
        temp_root = Path(td)
        project_root = temp_root / "project"
        project_root.mkdir()
        project_root = project_root.resolve()
        isolated_env = _controller_test_env(temp_root)
        with mock.patch.dict(os.environ, isolated_env, clear=False):
            authority = F.goalflight_journal.open_or_create_journal(project_root)
            claim_options = (
                {"nonce": "alive-controller-session-" + ("x" * 160)}
                if expected == "ALIVE"
                else {}
            )
            claimed = authority.claim_or_renew_lease(
                "console-test",
                principal={"principal_id": f"console-{expected.lower()}"},
                **claim_options,
            )
            assert_true("journal lease claimed", claimed.committed and claimed.value is not None)
            lease = claimed.value
            assert lease is not None

            if attempt_mode in {"running", "terminal"}:
                prepared = authority.prepare_attempt(f"dispatch-{expected.lower()}")
                assert_true("attempt prepared", prepared.committed and prepared.value is not None)
                attempt = prepared.value
                assert attempt is not None
                if attempt_mode == "running":
                    starting = authority.start_attempt(attempt.attempt_id, attempt.launch_token)
                    assert_true("attempt starting", starting.committed and starting.value is not None)
                    started = starting.value
                    assert started is not None
                    running = authority.mark_attempt_running(
                        started.attempt_id,
                        started.launch_token,
                        launch_epoch=started.launch_epoch,
                        worker_instance={"pid": os.getpid(), "source": "fleet-console-test"},
                    )
                    assert_true("attempt running", running.committed)
                else:
                    terminal = authority.commit_terminal(
                        attempt.attempt_id,
                        terminal_state="complete",
                        observation={"state": "complete", "outcome": {}},
                    )
                    assert_true("attempt terminal", terminal.committed)

            with contextlib.ExitStack() as locks:
                if holder_mode == "held":
                    locks.enter_context(
                        F.goalflight_wake.register_lease_holder(
                            project_root,
                            controller_label="console-test",
                            lease_nonce=lease.nonce,
                        )
                    )
                elif holder_mode == "released":
                    released = F.goalflight_wake.register_lease_holder(
                        project_root,
                        controller_label="console-test",
                        lease_nonce=lease.nonce,
                    )
                    released.close()
                elif holder_mode != "unknown":
                    raise AssertionError(f"unknown holder mode: {holder_mode}")

                for _index in range(waiter_count):
                    locks.enter_context(
                        F.goalflight_wake.register_waiter(
                            project_root,
                            controller_label="console-test",
                            kind="listener",
                        )
                    )

                holder_lock = F.goalflight_wake.lease_holder_alive(
                    project_root,
                    controller_label="console-test",
                    lease_nonce=lease.nonce,
                )
                live_waiters = F.goalflight_wake.live_waiters(
                    project_root,
                    controller_label="console-test",
                    prune_dead=False,
                )
                live_waiter_count = (
                    None if live_waiters is None else len(live_waiters)
                )
                reader = F.goalflight_journal.Journal.open_reader(project_root)
                dispatch_id = f"dispatch-{expected.lower()}"
                in_flight_count = F._journal_in_flight_count(
                    reader,
                    controller_label="console-test",
                )
                expected_inputs = {
                    "ALIVE": (True, 1, 1),
                    "HUNG": (True, 0, 1),
                    "WAITING-ON-USER": (True, 0, 0),
                    "DEAD": (False, 1, 1),
                    "UNKNOWN": (None, None, 1),
                }
                assert_true(
                    f"real probes construct the {expected} boundary",
                    (holder_lock, live_waiter_count, in_flight_count)
                    == expected_inputs[expected],
                )
                assert_true(
                    f"pure classifier returns {expected}",
                    F.classify_controller(
                        holder_lock,
                        live_waiter_count,
                        in_flight_count,
                    ) == expected,
                )

                machine_status = {
                    "schema": "goalflight.status.aggregate.v1",
                    "capacity": {},
                    "capacity_state": {"leases": {}},
                    "rate_pressure": {},
                    "warnings": [],
                    "dispatch": {
                        "records": [
                            {
                                "dispatch_id": dispatch_id,
                                "project_root": str(project_root),
                                "state": "running",
                                "classification": "expected_live",
                                "controller_session_id": lease.nonce,
                                "controller_pid": os.getpid(),
                            }
                        ]
                    },
                }
                registry = [
                    {
                        "project_root": str(project_root),
                        "last_seen": "2030-01-01T00:00:00+00:00",
                        "skill_version": "test",
                    }
                ]
                with (
                    mock.patch.object(F.goalflight_status, "status_payload", return_value=machine_status),
                    mock.patch.object(F.goalflight_fleet_status_cli, "build_fleet_status", return_value={}),
                    mock.patch.object(F.goalflight_usage, "collect_usage", return_value=[]),
                    mock.patch.object(F.goalflight_task, "read_project_registry", return_value=registry),
                    mock.patch.object(F.goalflight_session_status, "aggregate_status", return_value={}),
                    mock.patch.object(F.goalflight_status, "milestone_status_payload", return_value={}),
                    mock.patch.object(F.goalflight_messages, "controller_mail_summary", return_value={"needs": []}),
                ):
                    fleet = F.build_fleet_plane(generation_id=f"fleet-{expected.lower()}")
                    attention = F.build_attention_plane(
                        generation_id=f"attention-{expected.lower()}",
                        project_roots=[project_root],
                    )

                row = fleet["projects"][0]["workers"][0]
                assert_true(
                    f"fleet row returns {expected}",
                    row["controller_liveness_state"] == expected,
                )
                hung_items = [
                    item for item in attention["items"]
                    if item["kind"] == "controller_hung"
                ]
                assert_true(
                    "only HUNG enters attention",
                    len(hung_items) == (1 if expected == "HUNG" else 0),
                )
                if expected == "HUNG":
                    assert_true(
                        "HUNG carries the wake layer's exact listener command",
                        hung_items[0]["action"]
                        == F.goalflight_wake.listener_start_command(
                            project_root,
                            controller_label="console-test",
                        ),
                    )


def test_controller_state_alive_with_held_lease_and_one_live_waiter() -> None:
    _assert_controller_state(
        "ALIVE",
        holder_mode="held",
        waiter_count=1,
        attempt_mode="running",
    )


def test_controller_state_hung_with_nonterminal_attempt() -> None:
    _assert_controller_state(
        "HUNG",
        holder_mode="held",
        waiter_count=0,
        attempt_mode="running",
    )


def test_controller_state_waiting_on_user_with_terminal_attempt() -> None:
    _assert_controller_state(
        "WAITING-ON-USER",
        holder_mode="held",
        waiter_count=0,
        attempt_mode="terminal",
    )


def test_controller_state_dead_with_released_lease_lock() -> None:
    _assert_controller_state(
        "DEAD",
        holder_mode="released",
        waiter_count=1,
        attempt_mode="running",
    )


def test_controller_state_unknown_when_ledger_path_cannot_be_probed() -> None:
    _assert_controller_state(
        "UNKNOWN",
        holder_mode="unknown",
        waiter_count=0,
        attempt_mode="running",
    )


def test_controller_in_flight_uses_recorded_owner_and_retires_to_unowned() -> None:
    with tempfile.TemporaryDirectory() as td:
        temp_root = Path(td)
        project_root = (temp_root / "project").resolve()
        project_root.mkdir()
        with mock.patch.dict(os.environ, _controller_test_env(temp_root), clear=False):
            authority = F.goalflight_journal.open_or_create_journal(project_root)
            leases = {}
            for label in ("controller-a", "controller-b"):
                claimed = authority.claim_or_renew_lease(
                    label,
                    principal={"principal_id": f"principal-{label}"},
                )
                assert_true(f"{label} lease claimed", claimed.committed and claimed.value is not None)
                leases[label] = claimed.value
            lease_a = leases["controller-a"]
            lease_b = leases["controller-b"]
            assert lease_a is not None and lease_b is not None
            _running_attempt(
                authority,
                "owned-by-a",
                owner_controller_label="controller-a",
                owner_session_nonce=lease_a.nonce,
            )
            with (
                F.goalflight_wake.register_lease_holder(
                    project_root,
                    controller_label="controller-a",
                    lease_nonce=lease_a.nonce,
                ) as holder_a,
                F.goalflight_wake.register_lease_holder(
                    project_root,
                    controller_label="controller-b",
                    lease_nonce=lease_b.nonce,
                ),
            ):
                owned_contexts = F._controller_contexts_by_session(
                    project_root,
                    [
                        {
                            "dispatch_id": "owned-by-a",
                            # Deliberately false status ownership: the owner
                            # recorded on the attempt remains authoritative.
                            "controller_session_id": lease_b.nonce,
                        }
                    ],
                    include_all=True,
                )
                assert_true(
                    "A-owned work makes waiter-less A HUNG",
                    owned_contexts[lease_a.nonce]["liveness_state"] == "HUNG",
                )
                assert_true(
                    "A-owned work leaves idle waiter-less B waiting on the user",
                    owned_contexts[lease_b.nonce]["liveness_state"] == "WAITING-ON-USER",
                )

                released = authority.release_lease(
                    "controller-a",
                    nonce=lease_a.nonce,
                    reason="fleet-console-retirement-counterexample",
                )
                assert_true("controller A retired", released.committed)
                retired_contexts = F._controller_contexts_by_session(
                    project_root,
                    [],
                    include_all=True,
                    include_locked_ended=True,
                )
                assert_true(
                    "retired owner makes the attempt unowned for both generations",
                    retired_contexts[lease_a.nonce]["liveness_state"] == "HUNG"
                    and retired_contexts[lease_b.nonce]["liveness_state"] == "HUNG",
                )


def test_controller_in_flight_null_owner_counts_for_every_controller() -> None:
    with tempfile.TemporaryDirectory() as td:
        temp_root = Path(td)
        project_root = (temp_root / "project").resolve()
        project_root.mkdir()
        with mock.patch.dict(os.environ, _controller_test_env(temp_root), clear=False):
            authority = F.goalflight_journal.open_or_create_journal(project_root)
            leases = []
            for label in ("controller-a", "controller-b"):
                claimed = authority.claim_or_renew_lease(
                    label,
                    principal={"principal_id": f"unowned-{label}"},
                )
                assert_true(f"{label} lease claimed", claimed.committed)
                assert claimed.value is not None
                leases.append(claimed.value)
            _running_attempt(authority, "unowned-running")
            with (
                F.goalflight_wake.register_lease_holder(
                    project_root,
                    controller_label="controller-a",
                    lease_nonce=leases[0].nonce,
                ),
                F.goalflight_wake.register_lease_holder(
                    project_root,
                    controller_label="controller-b",
                    lease_nonce=leases[1].nonce,
                ),
            ):
                contexts = F._controller_contexts_by_session(
                    project_root,
                    [],
                    include_all=True,
                )
                assert_true(
                    "NULL owner makes every waiter-less controller HUNG",
                    all(
                        context["liveness_state"] == "HUNG"
                        for context in contexts.values()
                    ),
                )


def test_controller_state_unknown_when_waiter_directory_is_unreadable() -> None:
    assert_true(
        "an unavailable waiter count is UNKNOWN even with a known held lock",
        F.classify_controller(True, None, 1) == "UNKNOWN",
    )
    with tempfile.TemporaryDirectory() as td:
        temp_root = Path(td)
        project_root = (temp_root / "project").resolve()
        project_root.mkdir()
        with mock.patch.dict(os.environ, _controller_test_env(temp_root), clear=False):
            authority = F.goalflight_journal.open_or_create_journal(project_root)
            claimed = authority.claim_or_renew_lease(
                "unreadable-controller",
                principal={"principal_id": "unreadable-controller"},
            )
            assert_true("unreadable lease claimed", claimed.committed and claimed.value is not None)
            lease = claimed.value
            assert lease is not None
            _running_attempt(authority, "unreadable-running")
            with (
                F.goalflight_wake.register_lease_holder(
                    project_root,
                    controller_label="unreadable-controller",
                    lease_nonce=lease.nonce,
                ),
                F.goalflight_wake.register_waiter(
                    project_root,
                    controller_label="unreadable-controller",
                    kind="listener",
                ),
            ):
                directory = F.goalflight_wake.ledger_dir(project_root)
                original_mode = directory.stat().st_mode & 0o777
                try:
                    directory.chmod(0)
                    contexts = F._controller_contexts_by_session(
                        project_root,
                        [
                            {
                                "dispatch_id": "unreadable-running",
                                "controller_session_id": lease.nonce,
                            }
                        ],
                    )
                    assert_true(
                        "an unreadable waiter ledger produces UNKNOWN",
                        contexts[lease.nonce]["liveness_state"] == "UNKNOWN",
                    )
                finally:
                    directory.chmod(original_mode)


def test_controller_fields_without_session_identity_are_unknown() -> None:
    fields = F._controller_fields(
        {"controller_pid": os.getpid()},
        {},
        {},
    )
    assert_true(
        "missing session identity is absence of liveness evidence",
        fields["controller_liveness_state"] == "UNKNOWN",
    )


def test_long_controller_nonces_remain_distinct_context_keys() -> None:
    with tempfile.TemporaryDirectory() as td:
        temp_root = Path(td)
        project_root = (temp_root / "project").resolve()
        project_root.mkdir()
        with mock.patch.dict(os.environ, _controller_test_env(temp_root), clear=False):
            authority = F.goalflight_journal.open_or_create_journal(project_root)
            shared_prefix = "nonce-" + ("x" * 150)
            claimed_a = authority.claim_or_renew_lease(
                "long-a",
                principal={"principal_id": "long-a"},
                nonce=shared_prefix + "-a",
            )
            claimed_b = authority.claim_or_renew_lease(
                "long-b",
                principal={"principal_id": "long-b"},
                nonce=shared_prefix + "-b",
            )
            assert_true("long nonce A claimed", claimed_a.committed and claimed_a.value is not None)
            assert_true("long nonce B claimed", claimed_b.committed and claimed_b.value is not None)
            lease_a = claimed_a.value
            lease_b = claimed_b.value
            assert lease_a is not None and lease_b is not None
            _running_attempt(authority, "unowned-long-nonce")
            with (
                F.goalflight_wake.register_lease_holder(
                    project_root,
                    controller_label="long-a",
                    lease_nonce=lease_a.nonce,
                ),
                F.goalflight_wake.register_lease_holder(
                    project_root,
                    controller_label="long-b",
                    lease_nonce=lease_b.nonce,
                ),
            ):
                contexts = F._controller_contexts_by_session(
                    project_root,
                    [{"dispatch_id": "unowned-long-nonce"}],
                    include_all=True,
                )
                machine_status = {
                    "schema": "goalflight.status.aggregate.v1",
                    "capacity": {},
                    "capacity_state": {"leases": {}},
                    "rate_pressure": {},
                    "warnings": [],
                    "dispatch": {
                        "records": [
                            {
                                "dispatch_id": "long-a-worker",
                                "project_root": str(project_root),
                                "state": "running",
                                "classification": "expected_live",
                                "controller_session_id": lease_a.nonce,
                            },
                            {
                                "dispatch_id": "long-b-worker",
                                "project_root": str(project_root),
                                "state": "running",
                                "classification": "expected_live",
                                "controller_session_id": lease_b.nonce,
                            },
                        ]
                    },
                }
                registry = [
                    {
                        "project_root": str(project_root),
                        "last_seen": "2030-01-01T00:00:00+00:00",
                        "skill_version": "test",
                    }
                ]
                with (
                    mock.patch.object(
                        F.goalflight_status,
                        "status_payload",
                        return_value=machine_status,
                    ),
                    mock.patch.object(
                        F.goalflight_fleet_status_cli,
                        "build_fleet_status",
                        return_value={},
                    ),
                    mock.patch.object(F.goalflight_usage, "collect_usage", return_value=[]),
                    mock.patch.object(
                        F.goalflight_task,
                        "read_project_registry",
                        return_value=registry,
                    ),
                    mock.patch.object(
                        F.goalflight_session_status,
                        "aggregate_status",
                        return_value={},
                    ),
                    mock.patch.object(
                        F.goalflight_status,
                        "milestone_status_payload",
                        return_value={},
                    ),
                ):
                    fleet = F.build_fleet_plane(generation_id="long-nonce-plane")
            assert_true(
                "raw long nonces remain two identity keys",
                set(contexts) == {lease_a.nonce, lease_b.nonce},
            )
            assert_true(
                "both long-nonce controllers retain independent HUNG contexts",
                all(context["liveness_state"] == "HUNG" for context in contexts.values()),
            )
            workers = fleet["projects"][0]["workers"]
            digests = {worker["controller_session_digest"] for worker in workers}
            assert_true(
                "published session digests are 16 hex characters and remain distinct",
                len(digests) == 2
                and all(
                    isinstance(value, str)
                    and len(value) == 16
                    and all(character in "0123456789abcdef" for character in value)
                    for value in digests
                ),
            )
            encoded_plane = json.dumps(fleet, sort_keys=True)
            assert_true(
                "raw controller nonces never enter the full published fleet plane",
                lease_a.nonce not in encoded_plane and lease_b.nonce not in encoded_plane,
            )


def test_journal_in_flight_count_follows_canonical_attempt_lifecycle_states() -> None:
    with tempfile.TemporaryDirectory() as td:
        temp_root = Path(td)
        project_root = (temp_root / "project").resolve()
        project_root.mkdir()
        with mock.patch.dict(os.environ, _controller_test_env(temp_root), clear=False):
            authority = F.goalflight_journal.open_or_create_journal(project_root)
            prepared = authority.prepare_attempt("state-prepared")
            assert_true("PREPARED attempt created", prepared.committed and prepared.value is not None)

            starting = authority.prepare_attempt("state-starting")
            assert_true("STARTING attempt prepared", starting.committed and starting.value is not None)
            starting_value = starting.value
            assert starting_value is not None
            started = authority.start_attempt(starting_value.attempt_id, starting_value.launch_token)
            assert_true("STARTING attempt transitioned", started.committed)

            _running_attempt(authority, "state-running")

            terminal = authority.prepare_attempt("state-terminal")
            assert_true("TERMINAL attempt prepared", terminal.committed and terminal.value is not None)
            terminal_value = terminal.value
            assert terminal_value is not None
            terminal_result = authority.commit_terminal(
                terminal_value.attempt_id,
                terminal_state="complete",
                observation={"state": "complete", "outcome": {}},
            )
            assert_true("TERMINAL attempt committed", terminal_result.committed)

            abandoned = authority.prepare_attempt("state-abandoned")
            assert_true("ABANDONED attempt prepared", abandoned.committed and abandoned.value is not None)
            abandoned_value = abandoned.value
            assert abandoned_value is not None
            abandoned_result = authority.commit_terminal(
                abandoned_value.attempt_id,
                terminal_state="abandoned",
                observation={"state": "abandoned", "outcome": {}},
            )
            assert_true("ABANDONED attempt committed", abandoned_result.committed)

            live_states = set(F.goalflight_journal.ATTEMPT_LIVE_STATES)
            final_states = set(F.goalflight_journal.ATTEMPT_FINAL_STATES)
            assert_true("PREPARED is canonically live", F.goalflight_journal.ATTEMPT_PREPARED in live_states)
            assert_true("STARTING is canonically live", F.goalflight_journal.ATTEMPT_STARTING in live_states)
            assert_true("RUNNING is canonically live", F.goalflight_journal.ATTEMPT_RUNNING in live_states)
            assert_true("TERMINAL is canonically final", F.goalflight_journal.ATTEMPT_TERMINAL in final_states)
            assert_true("ABANDONED is canonically final", F.goalflight_journal.ATTEMPT_ABANDONED in final_states)
            assert_true("no canonical final state is live", live_states.isdisjoint(final_states))

            count = F._journal_in_flight_count(
                F.goalflight_journal.Journal.open_reader(project_root),
                controller_label="owner",
            )
            assert_true("only PREPARED, STARTING, and RUNNING count", count == 3)


def test_journal_in_flight_count_ignores_forged_mail_and_cursor_advances() -> None:
    with tempfile.TemporaryDirectory() as td:
        temp_root = Path(td)
        project_root = (temp_root / "project").resolve()
        project_root.mkdir()
        with mock.patch.dict(os.environ, _controller_test_env(temp_root), clear=False):
            authority = F.goalflight_journal.open_or_create_journal(project_root)
            leases = {}
            for label in ("controller-a", "controller-b"):
                claimed = authority.claim_or_renew_lease(
                    label,
                    principal={"principal_id": f"pending-{label}"},
                )
                assert_true(
                    f"{label} pending-test lease claimed",
                    claimed.committed and claimed.value is not None,
                )
                leases[label] = claimed.value

            lease_a = leases["controller-a"]
            lease_b = leases["controller-b"]
            assert lease_a is not None and lease_b is not None
            _running_attempt(
                authority,
                "owned-live-work",
                owner_controller_label="controller-a",
                owner_session_nonce=lease_a.nonce,
            )
            reader = F.goalflight_journal.Journal.open_reader(project_root)
            assert_true(
                "recorded owner is the initial work attribution",
                F._journal_in_flight_count(reader, controller_label="controller-a") == 1
                and F._journal_in_flight_count(reader, controller_label="controller-b") == 0,
            )

            forged = F.goalflight_messages.post_message(
                dispatch_id="owned-live-work",
                msg_type="controller-notice",
                payload={"text": "forged self-mail must not claim work"},
                messages_dir=F.goalflight_messages.default_messages_dir(),
                source={
                    "node": "fleet-console-test",
                    "adapter": "pytest",
                    "transport": "controller",
                },
                addressee=F.goalflight_messages.controller_addressee(
                    "controller-b",
                    project_root=project_root,
                ),
            )
            assert_true("forged self-mail was recorded", forged["recorded"] is True)
            assert_true(
                "forged same-dispatch-id mail cannot change work ownership",
                F._journal_in_flight_count(reader, controller_label="controller-a") == 1
                and F._journal_in_flight_count(reader, controller_label="controller-b") == 0,
            )

            owner_mail = F.goalflight_messages.post_message(
                dispatch_id="owned-live-work",
                msg_type="controller-notice",
                payload={"text": "owner cursor independence probe"},
                messages_dir=F.goalflight_messages.default_messages_dir(),
                source={
                    "node": "fleet-console-test",
                    "adapter": "pytest",
                    "transport": "controller",
                },
                addressee=F.goalflight_messages.controller_addressee(
                    "controller-a",
                    project_root=project_root,
                ),
            )
            assert_true("owner mail was recorded", owner_mail["recorded"] is True)
            peek = authority.cursor_peek("controller-a", nonce=lease_a.nonce)
            stream_position = max(
                int(item["stream_seq"])
                for item in peek.items
                if item["stream_id"] == "owned-live-work"
            )
            advanced = authority.advance_cursor(
                "controller-a",
                nonce=lease_a.nonce,
                expected_cursor_version=peek.cursor_version,
                expected_stream_snapshots=peek.stream_snapshots,
                advances={"owned-live-work": stream_position},
                actor="fleet-console-pending-test",
            )
            assert_true("controller A advanced past the owner event", advanced.committed)
            assert_true(
                "cursor advancement cannot hide still-running work",
                F._journal_in_flight_count(reader, controller_label="controller-a") == 1
                and F._journal_in_flight_count(reader, controller_label="controller-b") == 0,
            )


def test_controller_label_lookup_reads_history_without_wake_probes() -> None:
    with tempfile.TemporaryDirectory() as td:
        temp_root = Path(td)
        project_root = (temp_root / "project").resolve()
        project_root.mkdir()
        with mock.patch.dict(os.environ, _controller_test_env(temp_root), clear=False):
            authority = F.goalflight_journal.open_or_create_journal(project_root)
            claimed = authority.claim_or_renew_lease(
                "historical-label",
                principal={"principal_id": "historical-label"},
            )
            assert_true("historical label claimed", claimed.committed and claimed.value is not None)
            lease = claimed.value
            assert lease is not None
            released = authority.release_lease(
                "historical-label",
                nonce=lease.nonce,
                reason="label-probe-test",
            )
            assert_true("historical label released", released.committed)
            with (
                mock.patch.object(
                    F.goalflight_wake,
                    "lease_holder_alive",
                    side_effect=AssertionError("label-only lookup probed holder liveness"),
                ),
                mock.patch.object(
                    F.goalflight_wake,
                    "live_waiters",
                    side_effect=AssertionError("label-only lookup probed waiter liveness"),
                ),
            ):
                labels = F._controller_labels_by_session(
                    project_root,
                    [{"controller_session_id": lease.nonce}],
                )
            assert_true(
                "label-only lookup retains historical identity without wake I/O",
                labels == {lease.nonce: "historical-label"},
            )


def test_attention_excludes_ended_generations_without_held_kernel_locks() -> None:
    with tempfile.TemporaryDirectory() as td:
        temp_root = Path(td)
        project_root = (temp_root / "project").resolve()
        project_root.mkdir()
        with mock.patch.dict(os.environ, _controller_test_env(temp_root), clear=False):
            authority = F.goalflight_journal.open_or_create_journal(project_root)
            ended_claim = authority.claim_or_renew_lease(
                "ended-controller",
                principal={"principal_id": "ended-controller"},
            )
            active_claim = authority.claim_or_renew_lease(
                "active-controller",
                principal={"principal_id": "active-controller"},
            )
            assert_true("ended controller claimed", ended_claim.committed and ended_claim.value is not None)
            assert_true("active controller claimed", active_claim.committed and active_claim.value is not None)
            ended = ended_claim.value
            active = active_claim.value
            assert ended is not None and active is not None
            released = authority.release_lease(
                "ended-controller",
                nonce=ended.nonce,
                reason="attention-scope-test",
            )
            assert_true("ended controller released", released.committed)
            _running_attempt(authority, "attention-unowned")
            machine_status = {
                "capacity": {},
                "capacity_state": {"leases": {}},
                "rate_pressure": {},
                "warnings": [],
                "dispatch": {
                    "records": [
                        {
                            "dispatch_id": "attention-unowned",
                            "project_root": str(project_root),
                        }
                    ]
                }
            }
            include_ended_calls: list[bool] = []
            real_lease_records = F.goalflight_journal.Journal.lease_records

            def audited_lease_records(
                journal: F.goalflight_journal.Journal,
                *,
                include_ended: bool = False,
            ) -> list[dict[str, object]]:
                include_ended_calls.append(include_ended)
                return real_lease_records(journal, include_ended=include_ended)

            with (
                F.goalflight_wake.register_lease_holder(
                    project_root,
                    controller_label="active-controller",
                    lease_nonce=active.nonce,
                ),
                mock.patch.object(
                    F.goalflight_journal.Journal,
                    "lease_records",
                    audited_lease_records,
                ),
            ):
                attention_rows = F._controller_attention_rows(
                    [project_root],
                    machine_status,
                )
                fleet_context = F._controller_contexts_by_session(
                    project_root,
                    [{"controller_session_id": ended.nonce}],
                )
            assert_true(
                "attention emits only the active HUNG controller",
                len(attention_rows) == 1
                and "active-controller" in str(attention_rows[0]["headline"]),
            )
            assert_true(
                "fleet fast path does not resolve historical lease labels",
                fleet_context[ended.nonce]["label"] is None,
            )
            assert_true(
                "attention reads bounded ended generations while fleet labels stay ACTIVE-only",
                include_ended_calls == [True, False],
            )


def test_attention_scans_superseded_generation_while_exact_lock_is_held() -> None:
    with tempfile.TemporaryDirectory() as td:
        temp_root = Path(td)
        project_root = (temp_root / "project").resolve()
        project_root.mkdir()
        with mock.patch.dict(os.environ, _controller_test_env(temp_root), clear=False):
            authority = F.goalflight_journal.open_or_create_journal(project_root)
            first_claim = authority.claim_or_renew_lease(
                "takeover-controller",
                principal={"principal_id": "takeover-first"},
            )
            assert_true("first takeover generation claimed", first_claim.committed)
            first = first_claim.value
            assert first is not None
            first_holder = F.goalflight_wake.register_lease_holder(
                project_root,
                controller_label="takeover-controller",
                lease_nonce=first.nonce,
            )
            try:
                second_claim = authority.claim_or_renew_lease(
                    "takeover-controller",
                    principal={"principal_id": "takeover-second"},
                    takeover=True,
                )
                assert_true("second takeover generation claimed", second_claim.committed)
                second = second_claim.value
                assert second is not None
                _running_attempt(authority, "takeover-unowned")
                machine_status = {
                    "capacity": {},
                    "capacity_state": {"leases": {}},
                    "rate_pressure": {},
                    "warnings": [],
                    "dispatch": {"records": []},
                }
                while_locked = F._controller_attention_rows(
                    [project_root], machine_status
                )
                assert_true(
                    "ended N1 zombie is scanned while holder-less active N2 is not HUNG",
                    len(while_locked) == 1
                    and f"generation-{first.generation}"
                    in str(while_locked[0]["dispatch_id"])
                    and f"generation-{second.generation}"
                    not in str(while_locked[0]["dispatch_id"]),
                )

                first_holder.close()
                after_release = F._controller_attention_rows(
                    [project_root], machine_status
                )
                assert_true(
                    "releasing N1's exact lock removes the zombie attention row",
                    after_release == [],
                )
            finally:
                first_holder.close()


def test_attention_bounds_ended_generation_probes_to_newest_eight() -> None:
    with tempfile.TemporaryDirectory() as td:
        temp_root = Path(td)
        project_root = (temp_root / "project").resolve()
        project_root.mkdir()
        with mock.patch.dict(os.environ, _controller_test_env(temp_root), clear=False):
            authority = F.goalflight_journal.open_or_create_journal(project_root)
            _running_attempt(authority, "bounded-active-unowned")
            fake_rows = [
                {
                    "label": "bounded-active-controller",
                    "nonce": f"active-{generation}",
                    "generation": generation,
                    "state": F.goalflight_journal.LEASE_RETIRED,
                }
                for generation in range(1, 13)
            ]
            probe_metadata = {"controller_history_probes_truncated": 0}
            with (
                mock.patch.object(authority, "lease_records", return_value=fake_rows),
                mock.patch.object(F.goalflight_wake, "lease_holder_alive", return_value=True),
                mock.patch.object(F.goalflight_wake, "live_waiters", return_value=[]),
            ):
                contexts = F._controller_contexts_by_session(
                    project_root,
                    [],
                    include_all=True,
                    include_locked_ended=True,
                    probe_metadata=probe_metadata,
                    authority=authority,
                    open_if_missing=False,
                )

            observed_generations = {
                int(context["generation"])
                for context in contexts.values()
                if isinstance(context.get("generation"), int)
            }
            assert_true(
                "only the newest eight ended generations are probed",
                observed_generations == set(range(5, 13)),
            )
            assert_true(
                "metadata reports four omitted ended generation probes",
                probe_metadata["controller_history_probes_truncated"] == 4,
            )


def test_attention_status_failure_degrades_without_inventing_hung() -> None:
    with tempfile.TemporaryDirectory() as td:
        temp_root = Path(td)
        project_root = (temp_root / "project").resolve()
        project_root.mkdir()
        with (
            mock.patch.dict(os.environ, _controller_test_env(temp_root), clear=False),
            mock.patch.object(
                F.goalflight_messages,
                "controller_mail_summary",
                return_value={"needs": []},
            ),
            mock.patch.object(
                F.goalflight_status,
                "status_payload",
                side_effect=RuntimeError("synthetic status failure"),
            ),
        ):
            payload = F.build_attention_plane(
                generation_id="attention-status-failure",
                project_roots=[project_root],
            )
    assert_true(
        "status failure is visible as degraded",
        payload["last_success_at"] is None
        and str(payload["last_error"]).startswith("local_status:"),
    )
    assert_true(
        "missing ownership/status evidence cannot invent HUNG attention",
        payload["items"] == [],
    )


def test_fast_plane_retention_is_small_and_prompt_free() -> None:
    global LAST_FAST_PLANE_MEASUREMENT
    with tempfile.TemporaryDirectory() as td:
        temp_root = Path(td)
        project_root = (temp_root / "project").resolve()
        project_root.mkdir()
        now = dt.datetime.now(dt.timezone.utc)
        records: list[dict] = []
        for index in range(3):
            records.append(
                {
                    "dispatch_id": f"live-{index}",
                    "project_root": str(project_root),
                    "state": "running",
                    "classification": "expected_live",
                    "started_at": (now - dt.timedelta(minutes=index + 1)).isoformat(),
                    "task_ids": ["t-242"],
                    "prompt": "must never enter the fast plane",
                    "prompt_path": str(project_root / "private-prompt.md"),
                }
            )
        for index in range(47):
            age = dt.timedelta(minutes=30 + index * 30) if index < 2 else dt.timedelta(hours=3, minutes=index)
            records.append(
                {
                    "dispatch_id": f"terminal-{index}",
                    "project_root": str(project_root),
                    "state": "complete",
                    "classification": "complete",
                    "terminal_state": "complete",
                    "started_at": (now - age - dt.timedelta(minutes=5)).isoformat(),
                    "ended_at": (now - age).isoformat(),
                    "worker_still_alive": False,
                    "prompt": "terminal prompt must be slow-only",
                    "prompt_path": str(project_root / f"prompt-{index}.md"),
                }
            )
        machine = {
            "schema": "goalflight.status.aggregate.v1",
            "capacity": {"operating_cap": 12},
            "capacity_state": {"leases": {}},
            "rate_pressure": {},
            "warnings": [],
            "dispatch": {"records": records},
        }
        registry = [{"project_root": str(project_root), "last_seen": now.isoformat()}]
        isolated_env = _controller_test_env(temp_root)
        with (
            mock.patch.dict(os.environ, isolated_env, clear=False),
            mock.patch.object(F.goalflight_status, "status_payload", return_value=machine),
            mock.patch.object(F.goalflight_fleet_status_cli, "build_fleet_status", return_value={}),
            mock.patch.object(F.goalflight_usage, "collect_usage", return_value=[]),
            mock.patch.object(F.goalflight_task, "read_project_registry", return_value=registry),
            mock.patch.object(F.goalflight_session_status, "aggregate_status", return_value={}),
            mock.patch.object(F.goalflight_status, "milestone_status_payload", return_value={}),
            mock.patch.object(F.goalflight_messages, "controller_mail_summary", return_value={"needs": []}),
        ):
            fleet_started = time.perf_counter()
            fleet = F.build_fleet_plane(generation_id="retention-measurement")
            fleet_elapsed = time.perf_counter() - fleet_started
            attention_started = time.perf_counter()
            attention = F.build_attention_plane(
                generation_id="attention-measurement",
                project_roots=[project_root],
            )
            attention_elapsed = time.perf_counter() - attention_started

    workers = fleet["projects"][0]["workers"]
    encoded = json.dumps(fleet, separators=(",", ":"), sort_keys=True)
    LAST_FAST_PLANE_MEASUREMENT = {
        "fleet_seconds": fleet_elapsed,
        "attention_seconds": attention_elapsed,
        "fleet_bytes": len(encoded.encode("utf-8")),
        "retained_rows": len(workers),
        "excluded_rows": int(fleet["history_excluded"]),
    }
    assert_true("three live plus newest-five terminal rows retained", len(workers) == 8)
    assert_true("forty-two immutable rows excluded", fleet["history_excluded"] == 42)
    assert_true("project exclusion counter is exact", fleet["projects"][0]["history_excluded"] == 42)
    assert_true("task links remain scalar fast-plane metadata", workers[0]["task_ids"] == ["t-242"])
    assert_true("prompt bodies and paths never enter fast plane", "must never" not in encoded and "prompt_path" not in encoded)
    assert_true("constructed fast payload stays under 100KB", len(encoded.encode("utf-8")) < 100_000)
    assert_true("constructed fleet build stays under one second", fleet_elapsed < 1.0)
    assert_true("constructed attention build stays under one second", attention_elapsed < 1.0)
    assert_true("attention carries no terminal dispatch history", all("terminal-" not in str(item) for item in attention["items"]))


def test_real_shape_2000_record_fast_plane_guard_mutation_pair() -> None:
    """Measure the installed producer quantity over production-shaped runs.d."""
    with tempfile.TemporaryDirectory() as td:
        temp_root = Path(td)
        isolated_env = _controller_test_env(temp_root)
        live_roots = [
            (temp_root / "active-project-a").resolve(),
            (temp_root / "active-project-b").resolve(),
        ]
        for root in live_roots:
            root.mkdir()
        readers_dir = temp_root / "usage-readers"
        readers_dir.mkdir()
        terminal_tail = temp_root / "ancient-worker-output.log"
        terminal_tail.write_text(
            "historical output without a terminal marker\n" * 1024,
            encoding="utf-8",
        )
        terminal_status = temp_root / "ancient-worker-status.json"
        terminal_status.write_text(
            json.dumps({"dispatch_id": "historical-worker", "state": "worker_dead"}),
            encoding="utf-8",
        )
        prompt_path = temp_root / "production-prompt.md"
        prompt_path.write_text("production-shaped performance prompt\n", encoding="utf-8")
        registry: list[dict[str, str]] = []

        with mock.patch.dict(os.environ, isolated_env, clear=False):
            runs_dir = goalflight_ledger.runs_dir()
            runs_dir.mkdir(parents=True, exist_ok=True)
            first_root = temp_root / "worktrees" / "ancient-0000"
            request_envelope = {
                "dispatch_argv": [
                    "--agent",
                    "codex",
                    "--shape",
                    "acp",
                    "--task",
                    "t-246",
                    "--frame",
                    "context-package-" + ("x" * 3200),
                ],
                "request": {
                    "agent": "codex",
                    "shape": "acp",
                    "task_ids": ["t-246"],
                    "os_sandbox": "workspace-write",
                },
            }
            # Generate the template through the real ledger writer so schema,
            # process identity, request envelope, and sandbox posture match v1.
            args = SimpleNamespace(
                dispatch_id="ancient-0000",
                prompt_id="perf-production-shape",
                prompt_path=str(prompt_path),
                agent="codex",
                engine="codex",
                shape="acp",
                account="perf-seat",
                effective_account="perf-seat",
                transport="acp",
                project_root=str(first_root),
                controller_pid=None,
                controller_session_id=None,
                controller_label=None,
                claimant_pid=None,
                worker_pid=os.getpid(),
                acp_session_id="acp-performance-session",
                logical_session_id="logical-performance-session",
                lease_id="performance-lease",
                stdout_path=str(terminal_tail),
                stderr_path=str(temp_root / "ancient-worker-stderr.log"),
                status_path=str(terminal_status),
                os_sandbox_json=json.dumps(
                    {
                        "requested_profile": "workspace-write",
                        "supported_profile": "workspace-write",
                        "enforced_profile": "workspace-write",
                    }
                ),
                state="worker_dead",
                request_envelope_json=json.dumps(request_envelope),
                task_id=None,
                task_ids=["t-246"],
                detached=True,
                queue_launch_token=None,
                json=True,
            )
            with contextlib.redirect_stdout(io.StringIO()):
                assert_true("real ledger schema template writes", goalflight_ledger.cmd_record(args) == 0)
            template = json.loads(
                goalflight_ledger.record_path("ancient-0000").read_text(encoding="utf-8")
            )
            for index in range(1994):
                root = (temp_root / "worktrees" / f"ancient-{index:04d}").resolve()
                dispatch_id = f"ancient-{index:04d}"
                record = json.loads(json.dumps(template))
                record.update(
                    {
                        "dispatch_id": dispatch_id,
                        "project_root": str(root),
                        "state": "worker_dead",
                        "terminal_state": "worker_dead",
                        "started_at": "2020-01-01T00:00:00+00:00",
                        "ended_at": "2020-01-01T00:01:00+00:00",
                        "updated_at": "2020-01-01T00:01:00+00:00",
                        "worker_pid": 99_000_000 + index,
                        "worker_still_alive": False,
                    }
                )
                record["worker_identity"] = {
                    "pid": record["worker_pid"],
                    "ppid": 1,
                    "pgid": record["worker_pid"],
                    "lstart": "Thu Jul  2 17:53:52 2026",
                    "comm": "codex",
                    "args": "codex exec --sandbox workspace-write --enable web_search_cached",
                    "start_token": f"perf-start-{index:04d}",
                }
                (runs_dir / f"{dispatch_id}.json").write_text(
                    json.dumps(record), encoding="utf-8"
                )
                registry.append(
                    {
                        "project_root": str(root),
                        "last_seen": "2020-01-01T00:01:00+00:00",
                    }
                )
            for index in range(6):
                root = live_roots[index % len(live_roots)]
                dispatch_id = f"live-{index}"
                status_path = temp_root / f"{dispatch_id}.status.json"
                status_path.write_text(
                    json.dumps(
                        {
                            "schema": "goalflight.status.v1",
                            "dispatch_id": dispatch_id,
                            "state": "running",
                            "worker_pid": os.getpid(),
                            "worker_alive": True,
                            "heartbeat_at": "2030-01-01T00:00:01+00:00",
                        }
                    ),
                    encoding="utf-8",
                )
                record = json.loads(json.dumps(template))
                record.update(
                    {
                        "dispatch_id": dispatch_id,
                        "project_root": str(root),
                        "state": "running",
                        "terminal_state": "unknown",
                        "started_at": "2030-01-01T00:00:00+00:00",
                        "ended_at": None,
                        "updated_at": "2030-01-01T00:00:01+00:00",
                        "worker_pid": os.getpid(),
                        "worker_identity": goalflight_ledger.process_identity(os.getpid()),
                        "worker_still_alive": True,
                        "status_path": str(status_path),
                    }
                )
                (runs_dir / f"{dispatch_id}.json").write_text(
                    json.dumps(record), encoding="utf-8"
                )
            registry.extend(
                {
                    "project_root": str(root),
                    "last_seen": "2030-01-01T00:00:00+00:00",
                }
                for root in live_roots
            )
            registry_path = F.goalflight_task.project_registry_index_path()
            registry_path.parent.mkdir(parents=True, exist_ok=True)
            registry_path.write_text(
                json.dumps(
                    {
                        "schema": F.goalflight_task.PROJECT_REGISTRY_INDEX_SCHEMA,
                        "updated_at": "2030-01-01T00:00:00+00:00",
                        "projects": registry,
                    }
                ),
                encoding="utf-8",
            )
            record_sizes = [path.stat().st_size for path in runs_dir.glob("*.json")]
            fixture_p50 = statistics.median(record_sizes)
            # Live runs.d p50 measured read-only on 2026-08-16 was 5,847 bytes;
            # keep this fixture within 2x so byte-sensitive regressions are real.
            live_p50 = 5_847
            assert_true(
                f"fixture median {fixture_p50}B stays within 2x of live p50 {live_p50}B",
                live_p50 / 2 <= fixture_p50 <= live_p50 * 2,
            )
            assert_true(
                "production records carry request envelopes and worker identities",
                isinstance(template.get("request_envelope"), dict)
                and isinstance(template.get("worker_identity"), dict),
            )
            thin_mutant = dict(template)
            thin_mutant.pop("request_envelope", None)
            thin_mutant.pop("worker_identity", None)
            thin_mutant_bytes = len(json.dumps(thin_mutant).encode("utf-8"))
            assert_true(
                "removing production envelope and identity recreates an undersized fixture",
                thin_mutant_bytes < live_p50 / 2,
            )

            output_dir = temp_root / "producer-output"
            output_dir.mkdir()
            # Usage readers use the real collector against an empty isolated
            # directory: provider credentials/network are outside this pipeline guard.
            subprocess_env = dict(os.environ)
            subprocess_env.update(isolated_env)
            subprocess_env["PYTHONDONTWRITEBYTECODE"] = "1"
            elapsed_by_plane: dict[str, float] = {}
            payload_by_plane: dict[str, dict] = {}
            for plane in ("attention", "fleet"):
                output = output_dir / f"{plane}-data.js"
                if plane == "fleet":
                    # Slow-history catch-up is a separate hourly quantity; the
                    # installed fast tick normally sees this fresh stamp.
                    (output_dir / ".history-catchup").touch()
                command = [
                    sys.executable,
                    str(ROOT / "scripts" / "goalflight_fleet_console_producer.py"),
                    plane,
                    "--output",
                    str(output),
                    "--lock-dir",
                    str(temp_root / "producer-locks"),
                    "--interval-s",
                    str({"attention": 20, "fleet": 60}[plane]),
                ]
                if plane == "fleet":
                    command.extend(("--readers-dir", str(readers_dir)))
                started = time.perf_counter()
                completed = subprocess.run(
                    command,
                    cwd=ROOT,
                    env=subprocess_env,
                    capture_output=True,
                    text=True,
                    timeout=producer.DEFAULT_BUDGET_S[plane] + 5.0,
                    check=False,
                )
                elapsed_by_plane[plane] = time.perf_counter() - started
                assert_true(
                    f"{plane} production entrypoint publishes healthy ({completed.stderr})",
                    completed.returncode == 0,
                )
                payload_by_plane[plane] = _script_payload(
                    output,
                    "GF_ATTENTION" if plane == "attention" else "GF_FLEET",
                )

    payload = payload_by_plane["fleet"]
    assert_true(
        "installed intervals reach both published payload stamps",
        payload_by_plane["attention"]["cadence_seconds"] == 20
        and payload_by_plane["fleet"]["cadence_seconds"] == 60,
    )
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    assert_true("guard exercised all 2,000 v1 records", payload["history_excluded"] == 1994)
    assert_true("only two live worktrees become project rows", len(payload["projects"]) == 2)
    assert_true("real-shape fast payload stays under 200KB", len(encoded) < 200_000)
    for plane, budget in producer.DEFAULT_BUDGET_S.items():
        headroom_limit = budget * 0.70
        elapsed = elapsed_by_plane[plane]
        assert_true(
            f"{plane} producer entrypoint keeps 30% headroom ({elapsed:.3f}s < {headroom_limit:.3f}s)",
            elapsed < headroom_limit,
        )
        old_guard_counterexample = budget * 0.85
        assert_true(
            f"{plane} guard mutation fails inside installed budget instead of accepting old 5s bound",
            old_guard_counterexample < 5.0
            and not old_guard_counterexample < headroom_limit,
        )


def test_authority_detail_names_sources_and_journal_reconciles() -> None:
    row = F._worker_row(
        {
            "dispatch_id": "authority-conflict",
            "state": "running",
            "classification": "expected_live",
            "terminal_state": "complete",
            "updated_at": "2030-01-01T00:00:00+00:00",
        },
        journal_authority={
            "lifecycle_state": F.goalflight_journal.ATTEMPT_TERMINAL,
            "terminal_state": "complete",
        },
    )
    assert_true("journal terminal structurally wins", row["display_state"] == "complete" and row["is_terminal"] is True)
    assert_true("resolved disagreement is not labelled unknown", row["classification_conflict"] is False)
    assert_true(
        "named authority detail identifies ledger and journal fields",
        "ledger.state=running" in str(row["authority_detail"])
        and "journal.terminal_state=complete" in str(row["authority_detail"]),
    )
    assert_true("resolution names journal", row["authority_resolution"] == "journal")
    identity_conflict = F._worker_row(
        {
            "dispatch_id": "identity-conflict",
            "state": "running",
            "classification": "expected_live",
            "worker_still_alive": False,
        }
    )
    assert_true(
        "identity conflicts name the disagreeing ledger facts",
        identity_conflict["classification_conflict"] is True
        and "ledger.state=running" in str(identity_conflict["authority_detail"])
        and "ledger.worker_still_alive=False"
        in str(identity_conflict["authority_detail"]),
    )
    with tempfile.TemporaryDirectory() as td:
        status_path = Path(td) / "status.json"
        status_path.write_text(
            json.dumps(
                {
                    "dispatch_id": "newer-status-conflict",
                    "state": "running",
                    "heartbeat_at": "2030-01-01T00:00:10+00:00",
                    "worker_alive": True,
                }
            ),
            encoding="utf-8",
        )
        newer_status = F._worker_row(
            {
                "dispatch_id": "newer-status-conflict",
                "state": "complete",
                "classification": "complete",
                "terminal_state": "complete",
                "updated_at": "2030-01-01T00:00:00+00:00",
                "status_path": str(status_path),
            }
        )
    assert_true(
        "newer running status sidecar reopens a stale terminal ledger presentation",
        newer_status["display_state"] == "running"
        and newer_status["is_terminal"] is False
        and newer_status["classification_conflict"] is False
        and newer_status["authority_resolution"] == "status.json:newer"
        and "reconciled by newer status.json observation"
        in str(newer_status["authority_detail"]),
    )
    with tempfile.TemporaryDirectory() as td:
        mismatched_path = Path(td) / "status.json"
        mismatched_path.write_text(
            json.dumps(
                {
                    "dispatch_id": "different-dispatch",
                    "state": "running",
                    "heartbeat_at": "2030-01-01T00:00:10+00:00",
                }
            ),
            encoding="utf-8",
        )
        mismatched_status = F._worker_row(
            {
                "dispatch_id": "status-owner",
                "state": "complete",
                "classification": "complete",
                "terminal_state": "complete",
                "updated_at": "2030-01-01T00:00:00+00:00",
                "status_path": str(mismatched_path),
            }
        )
    assert_true(
        "mismatched status sidecar cannot reconcile another dispatch",
        mismatched_status["display_state"] == "complete"
        and mismatched_status["classification_conflict"] is False
        and mismatched_status["authority_resolution"] is None,
    )


def test_fast_worker_row_runs_ready_promotion_recovery_mutation_pair() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        tail = root / "ready-worker.tail"
        tail.write_text(
            "work complete\n"
            "READY: ready-promoted — artifact written\n"
            "trailing summary after the terminal marker\n",
            encoding="utf-8",
        )
        status_path = root / "ready-worker.status.json"
        status_path.write_text(
            json.dumps(
                {
                    "dispatch_id": "ready-promoted",
                    "last_marker": {
                        "kind": "READY",
                        "text": "ready-promoted — artifact written",
                    },
                }
            ),
            encoding="utf-8",
        )
        record = {
            "dispatch_id": "ready-promoted",
            "state": "worker_dead",
            "classification": "worker_dead",
            "terminal_state": "worker_dead",
            "worker_pid": 999_999_991,
            "worker_identity": {
                "pid": 999_999_991,
                "lstart": "Tue Jun  9 09:00:00 2026",
                "comm": "python3",
            },
            "stdout_path": str(tail),
            "status_path": str(status_path),
            "started_at": "2026-08-16T00:00:00+00:00",
            "updated_at": "2026-08-16T00:01:00+00:00",
        }
        with mock.patch.object(
            F.goalflight_status.goalflight_ledger,
            "identity_matches",
            return_value=(False, "dead"),
        ):
            row = F._worker_row(record)

    assert_true(
        "bounded fast-row reconciliation promotes READY plus trailing summary",
        row["display_state"] == "complete"
        and row["is_terminal"] is True
        and row["classification"] == "complete",
    )
    unreconciled = F._worker_display_verdict(record)
    assert_true(
        "removing fast-row recovery recreates the false worker_dead presentation",
        unreconciled["display_state"] == "worker_dead"
        and unreconciled["is_terminal"] is True,
    )
    old = "2029-01-01T00:00:00+00:00"
    records = [
        {
            "dispatch_id": f"old-{index}",
            "state": "complete",
            "classification": "complete",
            "terminal_state": "complete",
            "ended_at": old,
        }
        for index in range(10)
    ]
    journal_live = {
        "old-0": {
            "lifecycle_state": F.goalflight_journal.ATTEMPT_RUNNING,
            "terminal_state": None,
        }
    }
    kept, excluded = F._fast_plane_records(
        records,
        sampled_at=F._parse_timestamp("2030-01-01T00:00:00+00:00"),
        journal_authority=journal_live,
    )
    assert_true(
        "journal contradiction cannot resurrect a structurally terminal row",
        "old-0" not in {item["dispatch_id"] for item in kept},
    )
    assert_true("only newest-five terminals remain warm", len(kept) == 5 and excluded == 5)


def test_finish_projects_history_and_dispatch_projects_prompt_once() -> None:
    with tempfile.TemporaryDirectory() as td:
        temp_root = Path(td)
        project_root = (temp_root / "project").resolve()
        project_root.mkdir()
        prompt = project_root / "dispatch.md"
        prompt.write_text("original prompt\n", encoding="utf-8")
        isolated_env = _controller_test_env(temp_root)
        with mock.patch.dict(os.environ, isolated_env, clear=False):
            record_code = goalflight_ledger.main(
                [
                    "record",
                    "--dispatch-id", "finish-history",
                    "--prompt-path", str(prompt),
                    "--task-ids", "b-151,t-243",
                    "--agent", "codex",
                    "--transport", "acp",
                    "--project-root", str(project_root),
                    "--state", "starting",
                    "--json",
                ]
            )
            assert_true("dispatch record succeeded", record_code == 0)
            prompt_name = F.goalflight_fleet_console_history.prompt_filename("finish-history")
            mirrored_prompt = temp_root / "console" / "prompts" / str(prompt_name)
            assert_true("dispatch writes prompt mirror", mirrored_prompt.read_text(encoding="utf-8") == "original prompt\n")
            prompt.write_text("mutated after dispatch\n", encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                finish_code = goalflight_ledger.main(
                    ["finish", "--dispatch-id", "finish-history", "--state", "complete"]
                )
            assert_true("finish path succeeds", finish_code == 0)
            assert_true("prompt mirror remains write-once", mirrored_prompt.read_text(encoding="utf-8") == "original prompt\n")
            history_path = temp_root / "console" / "history-data.js"
            history = F.goalflight_fleet_console_history._read_payload(history_path)
    history_rows = history["projects"][0]["workers"]
    assert_true("finish event writes exactly one history row", len(history_rows) == 1)
    assert_true("history row keeps linked task ids", history_rows[0]["task_ids"] == ["b-151", "t-243"])
    assert_true("history row references lazy prompt file", history_rows[0]["prompt_file"] == prompt_name)


def test_history_catch_up_publishes_missed_terminals_in_one_batch() -> None:
    history_module = F.goalflight_fleet_console_history
    with tempfile.TemporaryDirectory() as td:
        temp_root = Path(td)
        output_dir = temp_root / "console"
        project_root = temp_root / "project"
        records = (
            {
                "dispatch_id": f"missed-{index}",
                "project_root": str(project_root),
                "agent": "/private/agent" if index == 0 else "codex",
                "state": "complete",
                "terminal_state": "complete",
                "ended_at": f"2026-08-15T20:{index:02d}:00+00:00",
            }
            for index in range(50)
        )
        with mock.patch.object(
            history_module,
            "_publish",
            wraps=history_module._publish,
        ) as publish:
            result = history_module.catch_up(records, output_dir=output_dir)
            duplicate = history_module.catch_up(
                [
                    {
                        "dispatch_id": "missed-49",
                        "project_root": str(project_root),
                        "state": "complete",
                        "terminal_state": "complete",
                    }
                ],
                output_dir=output_dir,
            )
            payload = history_module._read_payload(output_dir / "history-data.js")
    assert_true("catch-up projects all missed terminal rows", result["history"] == 50)
    assert_true("catch-up writes the slow blob once", publish.call_count == 1)
    assert_true("catch-up is idempotent", duplicate["history"] == 0)
    assert_true(
        "slow-history display fields redact absolute paths",
        "/private/agent" not in json.dumps(payload),
    )


def test_history_hooks_require_explicit_console_opt_in() -> None:
    history_module = F.goalflight_fleet_console_history
    with tempfile.TemporaryDirectory() as td:
        temp_root = Path(td)
        with mock.patch.dict(
            os.environ,
            {
                "GOALFLIGHT_FLEET_CONSOLE_OUTPUT_DIR": "",
                "GOALFLIGHT_FLEET_CONSOLE_CONFIG": str(temp_root / "missing-config"),
            },
            clear=False,
        ):
            configured = history_module.configured_output_dir()
    assert_true("unconfigured history hooks do not write implicitly", configured is None)


def test_controller_panel_lists_live_first_and_shows_retire_command() -> None:
    """FAST-plane controllers reuse lease rows; DEAD leftovers offer retire."""
    with tempfile.TemporaryDirectory() as td:
        temp_root = Path(td)
        project_root = (temp_root / "battery").resolve()
        project_root.mkdir()
        isolated_env = _controller_test_env(temp_root)
        with mock.patch.dict(os.environ, isolated_env, clear=False):
            authority = F.goalflight_journal.open_or_create_journal(project_root)
            live = authority.claim_or_renew_lease(
                "battery-main",
                principal={"principal_id": "battery-live"},
            )
            leftover = authority.claim_or_renew_lease(
                "battery-tool-v2",
                principal={"principal_id": "battery-legacy"},
            )
            assert_true("live lease claimed", live.committed and live.value is not None)
            assert_true("legacy lease claimed", leftover.committed and leftover.value is not None)
            live_lease = live.value
            leftover_lease = leftover.value
            assert live_lease is not None and leftover_lease is not None
            leftover_holder = F.goalflight_wake.register_lease_holder(
                project_root,
                controller_label="battery-tool-v2",
                lease_nonce=leftover_lease.nonce,
            )
            leftover_holder.close()
            machine_status = {
                "schema": "goalflight.status.aggregate.v1",
                "capacity": {},
                "capacity_state": {"leases": {}},
                "rate_pressure": {},
                "warnings": [],
                "dispatch": {
                    "records": [
                        {
                            "dispatch_id": "battery-live-worker",
                            "project_root": str(project_root),
                            "state": "running",
                            "classification": "expected_live",
                            "controller_session_id": live_lease.nonce,
                            "controller_pid": os.getpid(),
                        }
                    ]
                },
            }
            registry = [
                {
                    "project_root": str(project_root),
                    "last_seen": "2030-01-01T00:00:00+00:00",
                    "skill_version": "test",
                }
            ]
            with (
                contextlib.ExitStack() as locks,
                mock.patch.object(F.goalflight_status, "status_payload", return_value=machine_status),
                mock.patch.object(F.goalflight_fleet_status_cli, "build_fleet_status", return_value={}),
                mock.patch.object(F.goalflight_usage, "collect_usage", return_value=[]),
                mock.patch.object(F.goalflight_task, "read_project_registry", return_value=registry),
                mock.patch.object(F.goalflight_session_status, "aggregate_status", return_value={}),
                mock.patch.object(F.goalflight_status, "milestone_status_payload", return_value={}),
                mock.patch.object(F.goalflight_messages, "controller_mail_summary", return_value={"needs": []}),
            ):
                locks.enter_context(
                    F.goalflight_wake.register_lease_holder(
                        project_root,
                        controller_label="battery-main",
                        lease_nonce=live_lease.nonce,
                    )
                )
                locks.enter_context(
                    F.goalflight_wake.register_waiter(
                        project_root,
                        controller_label="battery-main",
                        kind="listener",
                    )
                )
                started = time.perf_counter()
                fleet = F.build_fleet_plane(generation_id="controllers-panel")
                elapsed = time.perf_counter() - started

        LAST_FAST_PLANE_MEASUREMENT["controller_panel_seconds"] = elapsed
        rows = fleet["controllers"]
        labels = [row["label"] for row in rows]
        assert_true("both lease labels are projected", labels == ["battery-main", "battery-tool-v2"])
        assert_true("live controller is classified ALIVE", rows[0]["controller_liveness_state"] == "ALIVE")
        assert_true("legacy leftover is classified DEAD", rows[1]["controller_liveness_state"] == "DEAD")
        assert_true(
            "DEAD row carries the real retire command",
            rows[1]["retire_command"]
            == "python3 scripts/goalflight_session_status.py --retire battery-tool-v2",
        )
        assert_true("live row has no retire command", rows[0]["retire_command"] is None)
        assert_true("listener depth is n/target scalars", rows[0]["listener_target"] == F.goalflight_wake.DEFAULT_LISTENER_SLOTS)
        encoded = json.dumps(fleet)
        assert_true("controller panel publishes no absolute paths", str(project_root) not in encoded)
        assert_true("constructed controller-panel fleet build stays under one second", elapsed < 1.0)


def test_controller_panel_aggregates_owned_workers_across_worktrees() -> None:
    """Owner labels roll up worktree projects without publishing their paths."""
    with tempfile.TemporaryDirectory() as td:
        temp_root = Path(td)
        parent = (temp_root / "battery-tool-v2").resolve()
        worktree = (temp_root / "bt-adapter").resolve()
        parent.mkdir()
        worktree.mkdir()
        (parent / ".git").mkdir()
        worktree_git = parent / ".git" / "worktrees" / "bt-adapter"
        worktree_git.mkdir(parents=True)
        (worktree_git / "commondir").write_text("../..\n", encoding="utf-8")
        (worktree / ".git").write_text(f"gitdir: {worktree_git}\n", encoding="utf-8")
        isolated_env = _controller_test_env(temp_root)
        with mock.patch.dict(os.environ, isolated_env, clear=False):
            authority = F.goalflight_journal.open_or_create_journal(parent)
            claimed = authority.claim_or_renew_lease(
                "webui",
                principal={"principal_id": "webui-owner"},
            )
            assert_true("webui lease claimed", claimed.committed and claimed.value is not None)
            lease = claimed.value
            assert lease is not None
            machine_status = {
                "schema": "goalflight.status.aggregate.v1",
                "capacity": {},
                "capacity_state": {"leases": {}},
                "rate_pressure": {},
                "warnings": [],
                "dispatch": {
                    "records": [
                        {
                            "dispatch_id": "webui-main",
                            "project_root": str(parent),
                            "state": "running",
                            "classification": "expected_live",
                            "controller_session_id": lease.nonce,
                            "controller_pid": os.getpid(),
                            "controller_label": "webui",
                        },
                        {
                            "dispatch_id": "webui-adapter",
                            "project_root": str(worktree),
                            "state": "running",
                            "classification": "expected_live",
                            "controller_session_id": lease.nonce,
                            "controller_pid": os.getpid(),
                            "controller_label": "webui",
                        },
                    ]
                },
            }
            registry = [
                {"project_root": str(parent), "last_seen": "2030-01-01T00:00:00+00:00"},
                {"project_root": str(worktree), "last_seen": "2030-01-01T00:00:00+00:00"},
            ]
            with (
                contextlib.ExitStack() as locks,
                mock.patch.object(F.goalflight_status, "status_payload", return_value=machine_status),
                mock.patch.object(F.goalflight_fleet_status_cli, "build_fleet_status", return_value={}),
                mock.patch.object(F.goalflight_usage, "collect_usage", return_value=[]),
                mock.patch.object(F.goalflight_task, "read_project_registry", return_value=registry),
                mock.patch.object(F.goalflight_session_status, "aggregate_status", return_value={}),
                mock.patch.object(F.goalflight_status, "milestone_status_payload", return_value={}),
                mock.patch.object(F.goalflight_messages, "controller_mail_summary", return_value={"needs": []}),
            ):
                locks.enter_context(
                    F.goalflight_wake.register_lease_holder(
                        parent,
                        controller_label="webui",
                        lease_nonce=lease.nonce,
                    )
                )
                locks.enter_context(
                    F.goalflight_wake.register_waiter(
                        parent,
                        controller_label="webui",
                        kind="listener",
                    )
                )
                fleet = F.build_fleet_plane(generation_id="worktree-roll-up")

        by_id = {project["project_id"]: project for project in fleet["projects"]}
        parent_id = F._project_id(str(parent))
        worktree_id = F._project_id(str(worktree))
        assert_true("parent checkout is not a worktree", by_id[parent_id]["worktree_name"] is None)
        assert_true("worktree publishes only its folder name", by_id[worktree_id]["worktree_name"] == "bt-adapter")
        assert_true(
            "worktree parent id matches the main checkout",
            by_id[worktree_id]["parent_project_id"] == parent_id,
        )
        webui = [row for row in fleet["controllers"] if row["label"] == "webui"]
        assert_true("one controller row per owner label", len(webui) == 1)
        assert_true("owned live workers count across worktrees", webui[0]["owned_live"] == 2)
        encoded = json.dumps(fleet)
        assert_true("worktree absolute path stays off the plane", str(worktree) not in encoded)
        assert_true("parent absolute path stays off the plane", str(parent) not in encoded)


def _constructed_repo_lens_env(temp_root: Path) -> dict[str, str]:
    return _controller_test_env(temp_root)


def _build_constructed_repo_lens_fleet(
    temp_root: Path,
    registry: list[dict],
    records: list[dict],
    *,
    generation_id: str = "repo-lens",
) -> tuple[dict, float]:
    machine_status = {
        "schema": "goalflight.status.aggregate.v1",
        "capacity": {},
        "capacity_state": {"leases": {}},
        "rate_pressure": {},
        "warnings": [],
        "dispatch": {"records": records},
    }
    with (
        mock.patch.dict(os.environ, _constructed_repo_lens_env(temp_root), clear=False),
        mock.patch.object(F.goalflight_status, "status_payload", return_value=machine_status),
        mock.patch.object(F.goalflight_fleet_status_cli, "build_fleet_status", return_value={}),
        mock.patch.object(F.goalflight_usage, "collect_usage", return_value=[]),
        mock.patch.object(F.goalflight_task, "read_project_registry", return_value=registry),
        mock.patch.object(F.goalflight_session_status, "aggregate_status", return_value={}),
        mock.patch.object(F.goalflight_status, "milestone_status_payload", return_value={}),
        mock.patch.object(F.goalflight_messages, "controller_mail_summary", return_value={"needs": []}),
        mock.patch.object(
            F.goalflight_task,
            "git_repo_identity",
            side_effect=AssertionError("producer must not shell git"),
        ),
    ):
        started = time.perf_counter()
        fleet = F.build_fleet_plane(generation_id=generation_id)
        elapsed = time.perf_counter() - started
    return fleet, elapsed


def _repo_band_key(project: dict) -> str:
    """Renderer grouping key: one GitHub identity is one band; unknown is path-id."""
    identity = project.get("repo_identity")
    if isinstance(identity, str) and identity:
        return f"repo|{identity}"
    return f"unlinked|{project['project_id']}"


def test_fast_plane_carries_cached_repo_identity_without_guessing_mutation_pair() -> None:
    """Registry cache is the only identity source; missing stays unknown."""
    with tempfile.TemporaryDirectory() as td:
        temp_root = Path(td)
        clone_a = (temp_root / "battery-tool-v2").resolve()
        clone_b = (temp_root / "bt-verify").resolve()
        unlinked_a = (temp_root / "left" / "scratch").resolve()
        unlinked_b = (temp_root / "right" / "scratch").resolve()
        kiln = (temp_root / "kiln").resolve()
        for root in (clone_a, clone_b, unlinked_a, unlinked_b, kiln):
            root.mkdir(parents=True)
        shared = "github.com/timdrpp/battery-tool-v2"
        kiln_identity = "github.com/simonrowland/kiln"
        registry = [
            {
                "project_root": str(clone_a),
                "last_seen": "2030-01-01T00:00:00+00:00",
                "skill_version": "test",
                "repo_identity": shared,
            },
            {
                "project_root": str(clone_b),
                "last_seen": "2030-01-01T00:00:00+00:00",
                "skill_version": "test",
                "repo_identity": shared,
            },
            {
                "project_root": str(unlinked_a),
                "last_seen": "2030-01-01T00:00:00+00:00",
                "skill_version": "test",
            },
            {
                "project_root": str(unlinked_b),
                "last_seen": "2030-01-01T00:00:00+00:00",
                "skill_version": "test",
            },
            {
                "project_root": str(kiln),
                "last_seen": "2030-01-01T00:00:00+00:00",
                "skill_version": "test",
                "repo_identity": kiln_identity,
            },
        ]
        records = [
            {
                "dispatch_id": f"{root.parent.name}-{root.name}-live"
                if root.name == "scratch"
                else f"{root.name}-live",
                "project_root": str(root),
                "state": "running",
                "classification": "expected_live",
            }
            for root in (clone_a, clone_b, unlinked_a, unlinked_b, kiln)
        ]
        fleet, elapsed = _build_constructed_repo_lens_fleet(
            temp_root, registry, records, generation_id="repo-identity-cache"
        )

    LAST_FAST_PLANE_MEASUREMENT["repo_lens_seconds"] = elapsed
    clones = [
        project
        for project in fleet["projects"]
        if project["repo_identity"] == shared
    ]
    unlinked = [
        project
        for project in fleet["projects"]
        if project["name"] == "scratch"
    ]
    kiln_row = next(project for project in fleet["projects"] if project["name"] == "kiln")
    assert_true("constructed set keeps every checkout as its own project row", len(fleet["projects"]) == 5)
    assert_true("two clones share the cached GitHub identity", len(clones) == 2)
    assert_true(
        "a record without the field stays unknown instead of being guessed",
        all(project["repo_identity"] is None for project in unlinked) and len(unlinked) == 2,
    )
    assert_true(
        "separate clones keep distinct parent ids — directory roll-up cannot unify them",
        clones[0]["parent_project_id"] != clones[1]["parent_project_id"],
    )
    band_keys = [_repo_band_key(project) for project in fleet["projects"]]
    assert_true(
        "two clones sharing one identity form one repo band",
        band_keys.count(f"repo|{shared}") == 2,
    )
    assert_true(
        "each unlinked checkout keeps its own path-id band and is not merged",
        len({_repo_band_key(project) for project in unlinked}) == 2,
    )
    assert_true(
        "a single-checkout repo is its own band (renderer flattens the nest)",
        band_keys.count(f"repo|{kiln_identity}") == 1 and kiln_row["repo_identity"] == kiln_identity,
    )
    encoded = json.dumps(fleet)
    assert_true("constructed repo-lens plane publishes no absolute paths", str(clone_a) not in encoded)
    assert_true("unlinked display labels are folder names, not paths", "scratch" in encoded and str(unlinked_a) not in encoded)
    assert_true("constructed repo-lens fleet build stays under one second", elapsed < 1.0)
    # Mutation: guessing from a missing cache (or from the checkout path) is
    # rejected. The patched git_repo_identity would have raised if the producer
    # shelled git; a silent invented identity would fail the None asserts above.
    def guessed_when_missing(project: dict) -> str | None:
        return project.get("repo_identity") or "github.com/should/not-guess"

    assert_true(
        "the guessing mutation would invent an identity the plane left unknown",
        all(guessed_when_missing(project) != project["repo_identity"] for project in unlinked),
    )
    assert_true("path-id keys stay distinct for same-name unlinked checkouts", unlinked[0]["project_id"] != unlinked[1]["project_id"])


def test_controllers_aggregate_by_owner_label_across_repos_mutation_pair() -> None:
    """A controller's workers stay its own wherever they run."""
    projects = [
        {
            "project_id": "clone-a",
            "parent_project_id": "clone-a",
            "repo_identity": "github.com/timdrpp/battery-tool-v2",
            "workers": [
                {"controller_label": "webui", "is_terminal": False, "dispatch_id": "a"},
            ],
        },
        {
            "project_id": "clone-b",
            "parent_project_id": "clone-b",
            "repo_identity": "github.com/timdrpp/battery-tool-v2",
            "workers": [
                {"controller_label": "webui", "is_terminal": False, "dispatch_id": "b"},
            ],
        },
        {
            "project_id": "kiln",
            "parent_project_id": "kiln",
            "repo_identity": "github.com/simonrowland/kiln",
            "workers": [
                {"controller_label": "webui", "is_terminal": False, "dispatch_id": "k"},
            ],
        },
    ]
    rows = [
        {
            "label": "webui",
            "parent_project_id": "clone-a",
            "project_id": "clone-a",
            "project_name": "battery-tool-v2",
            "parent_name": "battery-tool-v2",
            "controller_liveness_state": "ALIVE",
            "in_flight_count": 1,
            "owned_live": 0,
            "last_seen": "2030-01-01T00:00:00+00:00",
            "listener_live": 4,
            "listener_target": 4,
            "generation": 1,
            "retire_command": None,
            "controller_key": "clone-a:webui",
        },
        {
            "label": "webui",
            "parent_project_id": "kiln",
            "project_id": "kiln",
            "project_name": "kiln",
            "parent_name": "kiln",
            "controller_liveness_state": "ALIVE",
            "in_flight_count": 1,
            "owned_live": 0,
            "last_seen": "2030-01-02T00:00:00+00:00",
            "listener_live": 4,
            "listener_target": 4,
            "generation": 2,
            "retire_command": None,
            "controller_key": "kiln:webui",
        },
    ]

    def parent_keyed_counts(
        scoped: list[dict],
        unassigned: list[dict],
        remote_workers: list[dict],
    ) -> dict[tuple[str, str], int]:
        counts: dict[tuple[str, str], int] = {}
        for project in scoped:
            parent_id = str(project.get("parent_project_id") or "")
            for worker in project.get("workers") or []:
                label = worker.get("controller_label")
                if not isinstance(label, str) or not label:
                    continue
                if worker.get("is_terminal") is True:
                    continue
                key = (parent_id, label)
                counts[key] = counts.get(key, 0) + 1
        return counts

    owned = F._owned_live_counts(projects, [], [])
    aggregated = F._aggregate_controller_rows(rows, owned)
    assert_true("owned counts key by owner label only", owned == {"webui": 3})
    assert_true("one controller row per owner label across repos", len(aggregated) == 1)
    assert_true("owned live workers count wherever they run", aggregated[0]["owned_live"] == 3)
    assert_true("controller key is the owner label, not a parent:label pair", aggregated[0]["controller_key"] == "webui")
    legacy = parent_keyed_counts(projects, [], [])
    assert_true(
        "parent-keyed mutation splits the same workers across two repos",
        legacy == {("clone-a", "webui"): 1, ("clone-b", "webui"): 1, ("kiln", "webui"): 1},
    )
    assert_true("label-keyed counts are not the parent-keyed mutation", owned != legacy)


def main() -> None:
    fleet_payload = test_fleet_consumes_status_once_before_project_grouping()
    test_global_history_count_excludes_unreachable_remote_terminals_mutation_pair()
    attention_payload = test_attention_uses_envelope_timestamps_and_tolerates_missing_fleet_join()
    test_script_publication_escapes_injection_and_is_atomic(fleet_payload, attention_payload)
    test_source_error_is_bounded_and_not_a_false_success()
    test_registry_pass_is_bounded_and_reports_what_it_skipped()
    test_degraded_sample_exits_nonzero_instead_of_looking_healthy()
    test_unrecognised_attention_type_is_dropped_not_promoted()
    test_controller_authored_mail_reaches_the_attention_plane()
    test_registry_membership_is_not_a_statement_about_sampling()
    test_fast_plane_project_classes_are_live_only_mutation_pair()
    test_detached_orphan_with_matching_identity_stays_fast_without_lease_mutation_pair()
    test_live_worker_count_ignores_permanent_terminal_history()
    test_controller_liveness_projection_rejects_unregistered_scalar()
    test_controller_state_alive_with_held_lease_and_one_live_waiter()
    test_controller_state_hung_with_nonterminal_attempt()
    test_controller_state_waiting_on_user_with_terminal_attempt()
    test_controller_state_dead_with_released_lease_lock()
    test_controller_state_unknown_when_ledger_path_cannot_be_probed()
    test_controller_in_flight_uses_recorded_owner_and_retires_to_unowned()
    test_controller_in_flight_null_owner_counts_for_every_controller()
    test_controller_state_unknown_when_waiter_directory_is_unreadable()
    test_controller_fields_without_session_identity_are_unknown()
    test_controller_panel_lists_live_first_and_shows_retire_command()
    test_controller_panel_aggregates_owned_workers_across_worktrees()
    test_fast_plane_carries_cached_repo_identity_without_guessing_mutation_pair()
    test_controllers_aggregate_by_owner_label_across_repos_mutation_pair()
    test_long_controller_nonces_remain_distinct_context_keys()
    test_journal_in_flight_count_follows_canonical_attempt_lifecycle_states()
    test_journal_in_flight_count_ignores_forged_mail_and_cursor_advances()
    test_controller_label_lookup_reads_history_without_wake_probes()
    test_attention_excludes_ended_generations_without_held_kernel_locks()
    test_attention_scans_superseded_generation_while_exact_lock_is_held()
    test_attention_bounds_ended_generation_probes_to_newest_eight()
    test_attention_status_failure_degrades_without_inventing_hung()
    test_fast_plane_retention_is_small_and_prompt_free()
    test_real_shape_2000_record_fast_plane_guard_mutation_pair()
    test_authority_detail_names_sources_and_journal_reconciles()
    test_fast_worker_row_runs_ready_promotion_recovery_mutation_pair()
    test_finish_projects_history_and_dispatch_projects_prompt_once()
    test_history_catch_up_publishes_missed_terminals_in_one_batch()
    test_history_hooks_require_explicit_console_opt_in()
    test_allowlist_rejects_unknown_and_unsafe_fields(attention_payload)
    print("OK: fleet-console projection tests pass")


if __name__ == "__main__":
    main()
