#!/usr/bin/env python3
"""Security and composition tests for the backend fleet-console projection."""

from __future__ import annotations

import contextlib
import json
import os
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
        payload = F.build_attention_plane(
            generation_id="attention-generation",
            project_roots=[],
        )

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
    projects, _ = F._project_rows(
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
    assert_true("and it did NOT get a deep sample",
                by_name["outside-the-cap"]["session"]["available"] is False)


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
                "fleet identity path still resolves historical labels",
                fleet_context[ended.nonce]["label"] == "ended-controller",
            )
            assert_true(
                "attention inspects ended rows for locks while fleet identity reads history",
                include_ended_calls == [True, True],
            )


def test_attention_keeps_superseded_generation_until_kernel_lock_releases() -> None:
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
            second_holder = None
            try:
                second_claim = authority.claim_or_renew_lease(
                    "takeover-controller",
                    principal={"principal_id": "takeover-second"},
                    takeover=True,
                )
                assert_true("second takeover generation claimed", second_claim.committed)
                second = second_claim.value
                assert second is not None
                second_holder = F.goalflight_wake.register_lease_holder(
                    project_root,
                    controller_label="takeover-controller",
                    lease_nonce=second.nonce,
                )
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
                    "superseded but still-locked N1 remains HUNG attention",
                    len(while_locked) == 2
                    and any(
                        f"generation-{first.generation}" in str(row["dispatch_id"])
                        for row in while_locked
                    ),
                )

                first_holder.close()
                after_release = F._controller_attention_rows(
                    [project_root], machine_status
                )
                assert_true(
                    "released N1 drops while active N2 remains",
                    len(after_release) == 1
                    and f"generation-{second.generation}"
                    in str(after_release[0]["dispatch_id"]),
                )
            finally:
                first_holder.close()
                if second_holder is not None:
                    second_holder.close()


def test_attention_bounds_ended_generation_lock_probes_and_reports_truncation() -> None:
    with tempfile.TemporaryDirectory() as td:
        temp_root = Path(td)
        project_root = (temp_root / "project").resolve()
        project_root.mkdir()
        with mock.patch.dict(os.environ, _controller_test_env(temp_root), clear=False):
            authority = F.goalflight_journal.open_or_create_journal(project_root)
            leases = []
            holders = []
            try:
                for index in range(12):
                    claimed = authority.claim_or_renew_lease(
                        "bounded-history-controller",
                        principal={"principal_id": f"bounded-history-{index + 1}"},
                        takeover=index > 0,
                    )
                    assert_true(
                        f"bounded history generation {index + 1} claimed",
                        claimed.committed and claimed.value is not None,
                    )
                    lease = claimed.value
                    assert lease is not None
                    leases.append(lease)
                    holders.append(
                        F.goalflight_wake.register_lease_holder(
                            project_root,
                            controller_label="bounded-history-controller",
                            lease_nonce=lease.nonce,
                        )
                    )
                released = authority.release_lease(
                    "bounded-history-controller",
                    nonce=leases[-1].nonce,
                    reason="bounded-history-test",
                )
                assert_true("newest bounded history generation ended", released.committed)
                _running_attempt(authority, "bounded-history-unowned")
                machine_status = {
                    "capacity": {},
                    "capacity_state": {"leases": {}},
                    "rate_pressure": {},
                    "warnings": [],
                    "dispatch": {"records": []},
                }
                with (
                    mock.patch.object(
                        F.goalflight_messages,
                        "controller_mail_summary",
                        return_value={"needs": []},
                    ),
                    mock.patch.object(
                        F.goalflight_status,
                        "status_payload",
                        return_value=machine_status,
                    ),
                ):
                    payload = F.build_attention_plane(
                        generation_id="bounded-history-attention",
                        project_roots=[project_root],
                    )

                controller_rows = [
                    row for row in payload["items"] if row["kind"] == "controller_hung"
                ]
                observed_generations = {
                    int(str(row["dispatch_id"]).rsplit("generation-", 1)[1])
                    for row in controller_rows
                }
                # All twelve ended generations retain held locks in the
                # constructed ledger. Eight rows therefore proves the scan
                # probed only the newest eight without patching the probe.
                assert_true(
                    "only the newest eight ended generation locks are probed",
                    observed_generations == set(range(5, 13)),
                )
                assert_true(
                    "attention metadata reports four omitted history probes",
                    payload["controller_history_probes_truncated"] == 4,
                )
                assert_true(
                    "held newest-eight lock still pages while held older lock does not",
                    5 in observed_generations and 4 not in observed_generations,
                )
            finally:
                for holder in holders:
                    holder.close()


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


def main() -> None:
    fleet_payload = test_fleet_consumes_status_once_before_project_grouping()
    attention_payload = test_attention_uses_envelope_timestamps_and_tolerates_missing_fleet_join()
    test_script_publication_escapes_injection_and_is_atomic(fleet_payload, attention_payload)
    test_source_error_is_bounded_and_not_a_false_success()
    test_registry_pass_is_bounded_and_reports_what_it_skipped()
    test_degraded_sample_exits_nonzero_instead_of_looking_healthy()
    test_unrecognised_attention_type_is_dropped_not_promoted()
    test_controller_authored_mail_reaches_the_attention_plane()
    test_registry_membership_is_not_a_statement_about_sampling()
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
    test_long_controller_nonces_remain_distinct_context_keys()
    test_journal_in_flight_count_follows_canonical_attempt_lifecycle_states()
    test_journal_in_flight_count_ignores_forged_mail_and_cursor_advances()
    test_controller_label_lookup_reads_history_without_wake_probes()
    test_attention_excludes_ended_generations_without_held_kernel_locks()
    test_attention_keeps_superseded_generation_until_kernel_lock_releases()
    test_attention_bounds_ended_generation_lock_probes_and_reports_truncation()
    test_attention_status_failure_degrades_without_inventing_hung()
    test_allowlist_rejects_unknown_and_unsafe_fields(attention_payload)
    print("OK: fleet-console projection tests pass")


if __name__ == "__main__":
    main()
