#!/usr/bin/env python3
"""Tests for marker → envelope conversion."""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
import threading
import time
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from acp_runner import extract_markers, extract_message_envelopes
import goalflight_messages as _carrier_messages
from goalflight_messages import MARKER_TO_TYPE, markers_to_envelopes


def _carrier_add(path: Path, envelope: dict) -> None:
    _carrier_messages.update_envelopes(
        path, lambda existing: (existing + [envelope], None)
    )


def assert_true(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)


def run_messages_cli(
    messages_dir: Path,
    fleet_dir: Path,
    args: list[str],
    *,
    cwd: Path = ROOT,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.update(
        {
            "GOALFLIGHT_MESSAGES_DIR": str(messages_dir),
            "GOALFLIGHT_FLEET_DIR": str(fleet_dir),
            "GOALFLIGHT_JOURNAL_DIR": str(messages_dir.parent / "journals"),
            "GOALFLIGHT_STATE_DIR": str(messages_dir.parent / "state"),
            "GOALFLIGHT_WAKE_LEDGER_DIR": str(messages_dir.parent / "wake-ledger"),
            "GOAL_FLIGHT_PIDFILE_DIR": str(messages_dir.parent / "pids"),
            "GOALFLIGHT_TASK_STORE_DIR": str(messages_dir.parent / "task-store"),
            "GOALFLIGHT_CAPACITY_CONF": os.devnull,
        }
    )
    # A parent GOALFLIGHT_DISPATCH_DIR would send mailbox writes outside this
    # fixture's state_dir/dispatch, so absence checks would miss them.
    env.pop("GOALFLIGHT_DISPATCH_DIR", None)
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "goalflight_messages.py"),
            "--messages-dir",
            str(messages_dir),
            "--fleet-dir",
            str(fleet_dir),
            *args,
        ],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def start_messages_listener(
    messages_dir: Path,
    fleet_dir: Path,
    project_root: Path,
    *,
    session_id: str | None = None,
) -> subprocess.Popen[str]:
    env = dict(os.environ)
    env.update(
        {
            "GOALFLIGHT_MESSAGES_DIR": str(messages_dir),
            "GOALFLIGHT_FLEET_DIR": str(fleet_dir),
            "GOALFLIGHT_STATE_DIR": str(messages_dir.parent / "state"),
            "GOAL_FLIGHT_PIDFILE_DIR": str(messages_dir.parent / "pids"),
            "GOALFLIGHT_TASK_STORE_DIR": str(messages_dir.parent / "task-store"),
        }
    )
    command = [
        sys.executable,
        str(ROOT / "scripts" / "goalflight_messages.py"),
        "--messages-dir",
        str(messages_dir),
        "--fleet-dir",
        str(fleet_dir),
        "listen",
        "--project-root",
        str(project_root),
        "--poll-secs",
        "0.05",
        "--timeout-s",
        "5",
        "--json",
    ]
    if session_id is not None:
        command.extend(["--session-id", session_id])
    return subprocess.Popen(
        command,
        cwd=str(project_root),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def listener_result(process: subprocess.Popen[str]) -> tuple[int, str, str]:
    try:
        stdout, stderr = process.communicate(timeout=7)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)
    return int(process.returncode or 0), stdout, stderr


def write_ledger_record(
    base: Path,
    dispatch_id: str,
    project_root: Path,
    *,
    state: str = "running",
    task_ids: list[str] | None = None,
    worker_pid: int | None = None,
    detached: bool = False,
    reason: str | None = None,
    started_at: str = "2026-07-23T00:00:00+00:00",
    controller_session_id: str | None = None,
    controller_pid: int | None = None,
    controller_label: str | None = None,
) -> None:
    runs_dir = base / "state" / "runs.d"
    runs_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "schema": "goalflight.dispatch.v1",
        "dispatch_id": dispatch_id,
        "project_root": str(project_root.resolve()),
        "state": state,
        "started_at": started_at,
    }
    if task_ids:
        record["task_ids"] = task_ids
    if worker_pid is not None:
        import goalflight_ledger

        record["worker_pid"] = worker_pid
        record["worker_identity"] = goalflight_ledger.process_identity(worker_pid)
    if detached:
        record["detached"] = True
    if reason is not None:
        record["reason"] = reason
    if controller_session_id is not None:
        record["controller_session_id"] = controller_session_id
        record["controller_pid"] = controller_pid if controller_pid is not None else os.getpid()
        if controller_label is not None:
            record["controller_label"] = controller_label
    (runs_dir / f"{dispatch_id}.json").write_text(json.dumps(record) + "\n", encoding="utf-8")


def init_git_project(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)


def _journal_test_env(base: Path) -> dict[str, str]:
    return {
        "GOALFLIGHT_TASK_STORE_DIR": str(base / "task-store"),
        "GOALFLIGHT_JOURNAL_DIR": str(base / "journals"),
        "GOALFLIGHT_MESSAGES_DIR": str(base / "messages"),
        "GOALFLIGHT_FLEET_DIR": str(base / "fleet"),
        "GOALFLIGHT_STATE_DIR": str(base / "state"),
        "GOALFLIGHT_DISPATCH_DIR": str(base / "state" / "dispatch"),
        "GOALFLIGHT_WAKE_LEDGER_DIR": str(base / "wake-ledger"),
        "GOAL_FLIGHT_PIDFILE_DIR": str(base / "pids"),
        "GOALFLIGHT_CAPACITY_CONF": os.devnull,
        "GOALFLIGHT_TEST_MODE": "1",
    }


def _post_journal_controller_mail(
    *,
    project: Path,
    messages_dir: Path,
    label: str,
    dispatch_id: str,
) -> None:
    _carrier_messages.post_message(
        dispatch_id=dispatch_id,
        msg_type="controller-notice",
        payload={"text": f"mail for {dispatch_id}"},
        messages_dir=messages_dir,
        source={"node": "test", "adapter": "pytest", "transport": "controller"},
        addressee=_carrier_messages.controller_addressee(
            label,
            project_root=project,
        ),
    )


def test_relay_drain_backlog_none_and_json_mutation_pair() -> None:
    import goalflight_journal

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        project = base / "project"
        init_git_project(project)
        env = _journal_test_env(base)
        label = "drain-controller"
        messages_dir = Path(env["GOALFLIGHT_MESSAGES_DIR"])
        argv = [
            "--messages-dir",
            str(messages_dir),
            "--fleet-dir",
            env["GOALFLIGHT_FLEET_DIR"],
            "relay",
            "--drain",
        ]
        with (
            mock.patch.dict(
                os.environ,
                {**env, "GOALFLIGHT_CONTROLLER_LABEL": label},
                clear=False,
            ),
            mock.patch.object(
                _carrier_messages,
                "_current_project_root",
                return_value=project,
            ),
            mock.patch.object(
                _carrier_messages,
                "emit_wake_entry_notice",
                side_effect=AssertionError("drain must not emit a wake-entry notice"),
            ) as wake_notice,
        ):
            authority = goalflight_journal.open_or_create_journal(project)
            claimed = authority.claim_or_renew_lease(
                label,
                principal={"principal_id": "drain-test"},
            )
            assert_true("drain controller lease claimed", claimed.committed)
            _post_journal_controller_mail(
                project=project,
                messages_dir=messages_dir,
                label=label,
                dispatch_id="drain-one",
            )
            lease = authority.active_lease(label)
            assert_true("drain controller lease readable", lease is not None)
            before_drain = authority.cursor_peek(label, nonce=lease.nonce, limit=1000)

            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                drained_rc = _carrier_messages.main(argv)
            after_drain = authority.cursor_peek(label, nonce=lease.nonce, limit=1000)
            drained_lines = stdout.getvalue().splitlines()
            assert_true("drain backlog succeeds", drained_rc == 0)
            assert_true("drain backlog writes no stderr", stderr.getvalue() == "")
            assert_true("drain backlog emits headline then receipt", len(drained_lines) == 2)
            assert_true(
                "every receipted item has its pre-receipt headline",
                drained_lines[0]
                == "[controller-notice] drain-one seq=1 — mail for drain-one",
            )
            assert_true(
                "drain backlog reports the exact snapshot-bound cursor move",
                drained_lines[1]
                == (
                    f"drained 1 · cursor {before_drain.cursor_version}"
                    f"->{after_drain.cursor_version}"
                ),
            )

            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                empty_rc = _carrier_messages.main(argv)
            assert_true("empty drain succeeds", empty_rc == 0)
            assert_true("empty drain is exact one-line no-op", stdout.getvalue() == "no mail\n")
            assert_true("empty drain writes no stderr", stderr.getvalue() == "")

            _post_journal_controller_mail(
                project=project,
                messages_dir=messages_dir,
                label=label,
                dispatch_id="drain-json",
            )
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                json_rc = _carrier_messages.main([*argv, "--json"])
            json_lines = stdout.getvalue().splitlines()
            assert_true("JSON drain succeeds", json_rc == 0)
            assert_true("JSON drain emits one object", len(json_lines) == 1)
            payload = json.loads(json_lines[0])
            assert_true("JSON drain reports composed success", payload["status"] == "drained")
            assert_true("JSON drain reports exact count", payload["drained"] == 1)
            assert_true(
                "JSON drain includes every receipted item",
                len(payload["items"]) == 1
                and payload["items"][0]["dispatch_id"] == "drain-json",
            )
            assert_true(
                "JSON drain reports advancing cursor",
                payload["cursor_version"] > payload["previous_cursor_version"],
            )
            assert_true("JSON drain writes no stderr", stderr.getvalue() == "")
            assert_true("drain bypasses wake-entry notices", wake_notice.call_count == 0)


def test_relay_drain_ambiguous_identity_refuses_instead_of_no_mail() -> None:
    """Drain may print 'no mail' only after looking at a uniquely resolved mailbox."""
    import goalflight_journal

    argv = ["relay", "--drain"]

    def run_drain(
        project: Path,
        env: dict[str, str],
        extra_env: dict[str, str] | None = None,
        extra_argv: list[str] | None = None,
    ) -> tuple[int, str, str]:
        merged = dict(env)
        if extra_env:
            merged.update(extra_env)
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch.dict(os.environ, merged, clear=False),
            mock.patch.object(
                _carrier_messages,
                "_current_project_root",
                return_value=project,
            ),
            mock.patch.object(
                _carrier_messages,
                "emit_wake_entry_notice",
                side_effect=AssertionError("drain must not emit a wake-entry notice"),
            ),
        ):
            os.environ.pop("GOALFLIGHT_DISPATCH_ID", None)
            if "GOALFLIGHT_CONTROLLER_LABEL" not in (extra_env or {}):
                os.environ.pop("GOALFLIGHT_CONTROLLER_LABEL", None)
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                rc = _carrier_messages.main([*argv, *(extra_argv or [])])
        return rc, stdout.getvalue(), stderr.getvalue()

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        project = base / "project"
        init_git_project(project)
        env = _journal_test_env(base)
        messages_dir = Path(env["GOALFLIGHT_MESSAGES_DIR"])
        with mock.patch.dict(os.environ, env, clear=False):
            os.environ.pop("GOALFLIGHT_CONTROLLER_LABEL", None)
            authority = goalflight_journal.open_or_create_journal(project)
            for label in ("ghard", "pm2", "pm2-main"):
                claimed = authority.claim_or_renew_lease(
                    label,
                    principal={"principal_id": f"{label}-principal"},
                )
                assert_true(f"{label} lease claimed", claimed.committed)
            _post_journal_controller_mail(
                project=project,
                messages_dir=messages_dir,
                label="pm2",
                dispatch_id="stranded-pm2",
            )
            lease = authority.active_lease("pm2")
            assert_true("pm2 lease readable", lease is not None)
            before = authority.cursor_peek("pm2", nonce=lease.nonce, limit=1000)
            assert_true("event is peekable for pm2", len(list(before.items)) == 1)

        rc, stdout, stderr = run_drain(project, env)
        after_refuse = authority.cursor_peek("pm2", nonce=lease.nonce, limit=1000)
        assert_true("ambiguous drain exits 2", rc == 2)
        assert_true("ambiguous drain writes no stdout", stdout == "")
        assert_true(
            "ambiguous drain does not print no mail",
            "no mail" not in stdout and "no mail" not in stderr,
        )
        assert_true(
            "ambiguous drain names the ambiguity",
            "ambiguous ACTIVE controller leases:" in stderr,
        )
        assert_true(
            "ambiguous drain lists every ACTIVE label",
            "ghard, pm2, pm2-main" in stderr,
        )
        assert_true(
            "ambiguous drain names the identity knob",
            "GOALFLIGHT_CONTROLLER_LABEL" in stderr,
        )
        assert_true(
            "peekable event survives the refusal",
            len(list(after_refuse.items)) == 1,
        )

        json_rc, json_out, json_err = run_drain(project, env, extra_argv=["--json"])
        assert_true("ambiguous JSON drain exits 2", json_rc == 2)
        assert_true("ambiguous JSON drain writes no stdout", json_out == "")
        assert_true(
            "ambiguous JSON drain does not emit no_mail status",
            "no_mail" not in json_out and "no mail" not in json_err,
        )
        assert_true(
            "ambiguous JSON drain still names the ambiguity",
            "ambiguous ACTIVE controller leases:" in json_err,
        )

        rc, stdout, stderr = run_drain(
            project, env, extra_env={"GOALFLIGHT_CONTROLLER_LABEL": "pm2"}
        )
        after_drain = authority.cursor_peek("pm2", nonce=lease.nonce, limit=1000)
        assert_true("unambiguous drain succeeds", rc == 0)
        assert_true("unambiguous drain writes no stderr", stderr == "")
        assert_true("unambiguous drain receipts the peekable item", "stranded-pm2" in stdout)
        assert_true("unambiguous drain empties that mailbox", len(list(after_drain.items)) == 0)

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        project = base / "pm2"
        init_git_project(project)
        env = _journal_test_env(base)
        messages_dir = Path(env["GOALFLIGHT_MESSAGES_DIR"])
        with mock.patch.dict(os.environ, env, clear=False):
            os.environ.pop("GOALFLIGHT_CONTROLLER_LABEL", None)
            authority = goalflight_journal.open_or_create_journal(project)
            for label in ("ghard", "pm2", "pm2-main"):
                claimed = authority.claim_or_renew_lease(
                    label,
                    principal={"principal_id": f"{label}-principal"},
                )
                assert_true(f"{label} lease claimed in pm2-named project", claimed.committed)
            _post_journal_controller_mail(
                project=project,
                messages_dir=messages_dir,
                label="pm2-main",
                dispatch_id="stranded-pm2-main",
            )
            lease = authority.active_lease("pm2-main")
            assert_true("pm2-main lease readable", lease is not None)
            peekable = authority.cursor_peek("pm2-main", nonce=lease.nonce, limit=1000)
            assert_true(
                "event is peekable for pm2-main",
                len(list(peekable.items)) == 1,
            )

        rc, stdout, stderr = run_drain(project, env)
        still = authority.cursor_peek("pm2-main", nonce=lease.nonce, limit=1000)
        assert_true("repo-name collision still exits 2", rc == 2)
        assert_true(
            "repo-name collision does not print no mail",
            "no mail" not in stdout and "no mail" not in stderr,
        )
        assert_true(
            "repo-name collision names the ambiguity",
            "ambiguous ACTIVE controller leases:" in stderr,
        )
        assert_true(
            "pm2-main mail remains peekable after the repo-name guess is refused",
            len(list(still.items)) == 1,
        )

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        project = base / "project"
        init_git_project(project)
        env = _journal_test_env(base)
        messages_dir = Path(env["GOALFLIGHT_MESSAGES_DIR"])
        with mock.patch.dict(os.environ, env, clear=False):
            os.environ.pop("GOALFLIGHT_CONTROLLER_LABEL", None)
            authority = goalflight_journal.open_or_create_journal(project)
            claimed = authority.claim_or_renew_lease(
                "only-ctl",
                principal={"principal_id": "only-principal"},
            )
            assert_true("unique lease claimed", claimed.committed)
            _post_journal_controller_mail(
                project=project,
                messages_dir=messages_dir,
                label="only-ctl",
                dispatch_id="unique-mail",
            )
            lease = authority.active_lease("only-ctl")
            assert_true("unique lease readable", lease is not None)

        rc, stdout, stderr = run_drain(project, env)
        after = authority.cursor_peek("only-ctl", nonce=lease.nonce, limit=1000)
        assert_true("unique-lease drain succeeds without env label", rc == 0)
        assert_true("unique-lease drain receipts the item", "unique-mail" in stdout)
        assert_true("unique-lease drain writes no stderr", stderr == "")
        assert_true("unique-lease drain empties the mailbox", len(list(after.items)) == 0)

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        project = base / "project"
        init_git_project(project)
        env = _journal_test_env(base)
        with mock.patch.dict(os.environ, env, clear=False):
            os.environ.pop("GOALFLIGHT_CONTROLLER_LABEL", None)
            goalflight_journal.open_or_create_journal(project)

        rc, stdout, stderr = run_drain(project, env)
        assert_true("empty-roster drain exits 2", rc == 2)
        assert_true("empty-roster drain writes no stdout", stdout == "")
        assert_true(
            "empty-roster drain does not print no mail",
            "no mail" not in stdout and "no mail" not in stderr,
        )
        assert_true(
            "empty-roster drain names the missing lease",
            "no ACTIVE controller lease" in stderr,
        )


def test_relay_skips_self_peek_but_drain_receipts_self_and_foreign_mutation_pair() -> None:
    import goalflight_journal

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        project = base / "project"
        init_git_project(project)
        env = _journal_test_env(base)
        label = "self-skip-controller"
        messages_dir = Path(env["GOALFLIGHT_MESSAGES_DIR"])
        with (
            mock.patch.dict(
                os.environ,
                {**env, "GOALFLIGHT_CONTROLLER_LABEL": label},
                clear=False,
            ),
            mock.patch.object(
                _carrier_messages,
                "_current_project_root",
                return_value=project,
            ),
        ):
            os.environ.pop("GOALFLIGHT_DISPATCH_ID", None)
            authority = goalflight_journal.open_or_create_journal(project)
            claimed = authority.claim_or_renew_lease(
                label,
                principal={"principal_id": "self-skip-test"},
            )
            assert_true("self-skip lease claimed", claimed.committed)
            lease = authority.active_lease(label)
            assert_true("self-skip lease readable", lease is not None)
            assert lease is not None
            os.environ["GOALFLIGHT_CONTROLLER_LEASE_NONCE"] = lease.nonce
            addressee = _carrier_messages.controller_addressee(
                label,
                project_root=project,
            )
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self_post_rc = _carrier_messages.main(
                    [
                        "--messages-dir",
                        str(messages_dir),
                        "post",
                        "--dispatch-id",
                        "self-mail",
                        "--type",
                        "controller-notice",
                        "--text",
                        "controller wrote this",
                        "--to-controller",
                        label,
                        "--controller-project-root",
                        str(project),
                    ]
                )
            assert_true("ambient controller self-post succeeds", self_post_rc == 0)
            self_post = json.loads(stdout.getvalue())["envelope"]
            assert_true(
                "self-post publishes label authorship",
                self_post["source"]["controller_label"] == label,
            )
            assert_true(
                "self-post publishes capability-derived author digest",
                self_post["author_digest"]
                == _carrier_messages.goalflight_wake.controller_session_digest(lease.nonce),
            )
            foreign_post = _carrier_messages.post_message(
                dispatch_id="foreign-mail",
                msg_type="controller-notice",
                payload={"text": "peer wrote this"},
                messages_dir=messages_dir,
                source={
                    "node": "peer",
                    "adapter": "pytest",
                    "transport": "controller",
                    "controller_label": label,
                },
                addressee=addressee,
            )["envelope"]
            assert_true(
                "foreign label claim cannot mint an author digest",
                "author_digest" not in foreign_post,
            )

            # Mutation control: replacing the digest comparison with the source
            # label comparison makes the spoofed foreign post self-authored.
            assert_true(
                "production author compare identifies self",
                _carrier_messages.envelope_authored_by_controller(
                    self_post,
                    controller_label=label,
                    lease_nonce=lease.nonce,
                ),
            )
            assert_true(
                "production author compare rejects label spoof",
                not _carrier_messages.envelope_authored_by_controller(
                    foreign_post,
                    controller_label=label,
                    lease_nonce=lease.nonce,
                ),
            )
            assert_true(
                "labels-instead-of-digests mutant would suppress spoof",
                foreign_post["source"]["controller_label"] == label,
            )

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                peek_rc = _carrier_messages.main(["relay", "--new"])
            assert_true("self-filtered relay peek succeeds", peek_rc == 0)
            assert_true("foreign mail remains visible", "foreign-mail" in stdout.getvalue())
            assert_true("self mail is hidden from peek", "self-mail" not in stdout.getvalue())

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                drain_rc = _carrier_messages.main(["relay", "--drain"])
            drain_lines = stdout.getvalue().splitlines()
            assert_true("self+foreign drain succeeds", drain_rc == 0)
            assert_true(
                "drain headlines both receipted events",
                drain_lines[:2]
                == [
                    "[controller-notice] foreign-mail seq=1 — peer wrote this",
                    "[controller-notice] self-mail seq=1 — controller wrote this",
                ],
            )
            assert_true("drain receipts both", drain_lines[-1].startswith("drained 2 · cursor "))
            assert_true(
                "drain advances past self and foreign",
                not authority.cursor_peek(label, nonce=lease.nonce).items,
            )


def test_relay_drain_concurrent_advance_is_one_line_cas_loss() -> None:
    import goalflight_journal

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        project = base / "project"
        init_git_project(project)
        env = _journal_test_env(base)
        label = "drain-race-controller"
        messages_dir = Path(env["GOALFLIGHT_MESSAGES_DIR"])
        argv = [
            "--messages-dir",
            str(messages_dir),
            "--fleet-dir",
            env["GOALFLIGHT_FLEET_DIR"],
            "relay",
            "--drain",
        ]
        with (
            mock.patch.dict(
                os.environ,
                {**env, "GOALFLIGHT_CONTROLLER_LABEL": label},
                clear=False,
            ),
            mock.patch.object(
                _carrier_messages,
                "_current_project_root",
                return_value=project,
            ),
            mock.patch.object(
                _carrier_messages,
                "emit_wake_entry_notice",
                side_effect=AssertionError("drain must not emit a wake-entry notice"),
            ) as wake_notice,
        ):
            authority = goalflight_journal.open_or_create_journal(project)
            claimed = authority.claim_or_renew_lease(
                label,
                principal={"principal_id": "drain-race-test"},
            )
            assert_true("race controller lease claimed", claimed.committed)
            lease = authority.active_lease(label)
            assert_true("race controller lease readable", lease is not None)
            _post_journal_controller_mail(
                project=project,
                messages_dir=messages_dir,
                label=label,
                dispatch_id="drain-race",
            )

            snapshot_ready = threading.Event()
            release_snapshot = threading.Event()
            captured: dict[str, object] = {}
            original_peek = goalflight_journal.Journal.cursor_peek

            def blocked_peek(self, *args, **kwargs):
                snapshot = original_peek(self, *args, **kwargs)
                if threading.current_thread().name == "drain-race-cli":
                    captured["snapshot"] = snapshot
                    snapshot_ready.set()
                    assert release_snapshot.wait(5), "drain race snapshot was not released"
                return snapshot

            stdout = io.StringIO()
            stderr = io.StringIO()
            outcome: dict[str, int] = {}

            def drain() -> None:
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    outcome["rc"] = _carrier_messages.main(argv)

            with mock.patch.object(goalflight_journal.Journal, "cursor_peek", blocked_peek):
                worker = threading.Thread(target=drain, name="drain-race-cli")
                worker.start()
                assert_true("drain captured its snapshot", snapshot_ready.wait(5))
                snapshot = captured["snapshot"]
                positions = _carrier_messages._cursor_positions(snapshot.items)
                advanced = authority.advance_cursor(
                    label,
                    nonce=lease.nonce,
                    expected_cursor_version=snapshot.cursor_version,
                    expected_stream_snapshots=snapshot.stream_snapshots,
                    advances=positions,
                    actor="concurrent-test-controller",
                )
                assert_true("concurrent cursor advance committed", advanced.committed)
                release_snapshot.set()
                worker.join(timeout=5)
                assert_true("drain race thread exited", not worker.is_alive())

            combined_lines = [
                line
                for line in (stdout.getvalue() + stderr.getvalue()).splitlines()
                if line
            ]
            assert_true("drain race exits CAS-lost", outcome["rc"] == 3)
            assert_true(
                "drain race shows the attempted item before reporting CAS loss",
                combined_lines
                == [
                    "[controller-notice] drain-race seq=1 — mail for drain-race",
                    "drain conflict · retry relay --drain",
                ],
            )
            assert_true("racing drain bypasses wake-entry notices", wake_notice.call_count == 0)


def test_messages_main_trap_is_one_actionable_line_mutation_pair() -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with mock.patch.object(
        _carrier_messages,
        "_run_cli",
        side_effect=RuntimeError("forced\ninternal error"),
    ):
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            fixed_rc = _carrier_messages.main(["relay", "--drain"])
        try:
            _carrier_messages._run_cli(["relay", "--drain"])
        except RuntimeError as exc:
            mutant_error = exc
        else:  # pragma: no cover - mutation control must raise
            raise AssertionError("unguarded mutation did not propagate")

    lines = stderr.getvalue().splitlines()
    assert_true("top-level trap exits nonzero", fixed_rc != 0)
    assert_true("top-level trap writes no stdout", stdout.getvalue() == "")
    assert_true("top-level trap emits one line", len(lines) == 1)
    assert_true("top-level trap names error class", "RuntimeError" in lines[0])
    assert_true("top-level trap is actionable", "next: run" in lines[0])
    assert_true("top-level trap suppresses traceback", "Traceback" not in lines[0])
    assert_true("mutation half exposes the raw exception", "forced" in str(mutant_error))


def mirror_remote_message(
    *,
    remote_messages_dir: Path,
    fleet_dir: Path,
    messages_dir: Path,
    dispatch_id: str,
    msg_type: str,
    payload: dict,
    seq: int | None = None,
) -> Path:
    import goalflight_messages as messages

    posted = messages.post_message(
        dispatch_id=dispatch_id,
        msg_type=msg_type,
        payload=payload,
        messages_dir=remote_messages_dir,
        source={"node": "remote", "adapter": "codex", "transport": "acp"},
        seq=seq,
    )
    merged = messages.merge_remote_register(
        fleet_dir,
        Path(posted["path"]),
        messages_dir=messages_dir,
    )
    return Path(merged["merged_into"])


def test_marker_mapping() -> None:
    sample = "**STATUS:** working\nUSER-NEED: need maintainer\nCOMPLETE: goal done\n"
    markers = extract_markers(sample)
    envelopes = markers_to_envelopes(
        markers,
        dispatch_id="d-test",
        source={"node": "local", "adapter": "codex-acp", "transport": "acp"},
    )
    assert_true("three envelopes", len(envelopes) == 3)
    assert_true("monotonic seq", [e["seq"] for e in envelopes] == [1, 2, 3])
    assert_true("status type", envelopes[0]["type"] == "status")
    assert_true("user_need type", envelopes[1]["type"] == "user_need")
    assert_true("complete maps to result", envelopes[2]["type"] == "result")
    assert_true("complete payload flag", envelopes[2]["payload"].get("complete") is True)


def test_unknown_marker_monitor() -> None:
    envelopes = markers_to_envelopes({"CUSTOM": ["something"]}, dispatch_id="d2")
    assert_true("unknown -> monitor", envelopes[0]["type"] == "monitor")
    assert_true("unknown payload", envelopes[0]["payload"]["unknown_marker"] == "CUSTOM")


def test_acp_runner_wrapper() -> None:
    text = "USER-CONFIRM: approve risky change\n"
    envelopes = extract_message_envelopes(text, "d3", source={"transport": "bash-tail"})
    assert_true("wrapper count", len(envelopes) == 1)
    assert_true("wrapper type", envelopes[0]["type"] == "user_confirm")
    assert_true("all mapped kinds covered", set(MARKER_TO_TYPE) >= {
        "STATUS", "RESULT", "USER-NEED", "USER-CONFIRM", "BLOCKED", "COMPLETE"
    })


def test_inbox_append_read_order() -> None:
    import tempfile
    from goalflight_messages import inbox_path, read_envelopes

    with tempfile.TemporaryDirectory() as td:
        messages_dir = Path(td) / "messages"
        path = inbox_path(messages_dir, "d-inbox")
        env1 = markers_to_envelopes({"STATUS": ["a"]}, dispatch_id="d-inbox")[0]
        env2 = markers_to_envelopes({"USER-NEED": ["help"]}, dispatch_id="d-inbox", seq_start=2)[0]
        _carrier_add(path, env1)
        _carrier_add(path, env2)
        loaded = read_envelopes(path)
        assert_true("two lines", len(loaded) == 2)
        assert_true("order preserved", loaded[0]["seq"] == 1 and loaded[1]["seq"] == 2)
        assert_true("last one", read_envelopes(path, last_n=1)[0]["type"] == "user_need")


def test_inbox_corrupt_line_fails_closed() -> None:
    import tempfile
    from goalflight_messages import MessageError, inbox_path, read_envelopes

    with tempfile.TemporaryDirectory() as td:
        messages_dir = Path(td) / "messages"
        path = inbox_path(messages_dir, "bad")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"schema":"goalflight.message.v1"}\n')
        try:
            read_envelopes(path)
            assert_true("should fail", False)
        except MessageError:
            pass


def test_aggregate_open_user_need() -> None:
    import tempfile
    from goalflight_messages import build_aggregate, inbox_path, refresh_aggregate

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        messages_dir = base / "messages"
        fleet_dir = base / "fleet"
        fleet_dir.mkdir()
        (fleet_dir / "register").mkdir()
        path = inbox_path(messages_dir, "d-agg")
        _carrier_add(
            path,
            markers_to_envelopes({"USER-NEED": ["pick account"]}, dispatch_id="d-agg")[0],
        )
        aggregate = build_aggregate(messages_dir=messages_dir, fleet_dir=fleet_dir)
        assert_true("active dispatch", "d-agg" in aggregate["active_dispatches"])
        assert_true("open need", len(aggregate["open_user_needs"]) == 1)
        written = refresh_aggregate(fleet_dir, messages_dir=messages_dir)
        assert_true("written aggregate", (fleet_dir / "register" / "aggregate.json").exists())
        assert_true("same open need", len(written["open_user_needs"]) == 1)


def test_dual_source_inboxes_aggregate_without_fleet_overwrite() -> None:
    import tempfile
    import goalflight_messages as messages

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        messages_dir = base / "messages"
        remote_messages_dir = base / "remote-messages"
        fleet_dir = base / "fleet"
        dispatch_id = "d-dual-local-need"
        local_path = Path(
            messages.post_message(
                dispatch_id=dispatch_id,
                msg_type="user_need",
                payload={"text": "choose the account"},
                messages_dir=messages_dir,
            )["path"]
        )
        fleet_path = mirror_remote_message(
            remote_messages_dir=remote_messages_dir,
            fleet_dir=fleet_dir,
            messages_dir=messages_dir,
            dispatch_id=dispatch_id,
            msg_type="status",
            payload={"text": "remote worker is waiting"},
        )

        paths = messages.collect_inbox_paths(messages_dir, fleet_dir)
        assert_true("local then fleet streams retained", paths == [local_path, fleet_path])
        aggregate = messages.build_aggregate(messages_dir=messages_dir, fleet_dir=fleet_dir)
        assert_true("local user need survives fleet merge", len(aggregate["open_user_needs"]) == 1)
        assert_true(
            "private cursor identity stays out of aggregate contract",
            "_goalflight_inbox_cursor_key" not in aggregate["open_user_needs"][0],
        )
        assert_true(
            "local user need text survives fleet merge",
            aggregate["open_user_needs"][0]["text"] == "choose the account",
        )


def test_single_source_inboxes_and_deterministic_order_reject_local_overwrite() -> None:
    import tempfile
    import goalflight_messages as messages

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        messages_dir = base / "messages"
        remote_messages_dir = base / "remote-messages"
        fleet_dir = base / "fleet"
        fleet_only_id = "a-fleet-only"
        dual_id = "m-dual-fleet-need"
        local_only_id = "z-local-only"

        local_only_path = Path(
            messages.post_message(
                dispatch_id=local_only_id,
                msg_type="user_need",
                payload={"text": "local only"},
                messages_dir=messages_dir,
            )["path"]
        )
        fleet_only_path = mirror_remote_message(
            remote_messages_dir=remote_messages_dir,
            fleet_dir=fleet_dir,
            messages_dir=messages_dir,
            dispatch_id=fleet_only_id,
            msg_type="user_need",
            payload={"text": "fleet only"},
        )
        dual_local_path = Path(
            messages.post_message(
                dispatch_id=dual_id,
                msg_type="status",
                payload={"text": "local controller status"},
                messages_dir=messages_dir,
            )["path"]
        )
        dual_fleet_path = mirror_remote_message(
            remote_messages_dir=remote_messages_dir,
            fleet_dir=fleet_dir,
            messages_dir=messages_dir,
            dispatch_id=dual_id,
            msg_type="user_need",
            payload={"text": "fleet escalation"},
        )

        expected = [fleet_only_path, dual_local_path, dual_fleet_path, local_only_path]
        first = messages.collect_inbox_paths(messages_dir, fleet_dir)
        second = messages.collect_inbox_paths(messages_dir, fleet_dir)
        assert_true("local-only and fleet-only streams retained", first == expected)
        assert_true("collection order stable across runs", second == first)
        aggregate = messages.build_aggregate(messages_dir=messages_dir, fleet_dir=fleet_dir)
        needs = {
            (item["dispatch_id"], item["text"])
            for item in aggregate["open_user_needs"]
        }
        assert_true(
            "single-source directions and dual fleet need all surface",
            needs
            == {
                (local_only_id, "local only"),
                (fleet_only_id, "fleet only"),
                (dual_id, "fleet escalation"),
            },
        )


def test_same_id_different_envelopes_both_survive_and_later_need_reopens() -> None:
    import tempfile
    from unittest.mock import patch
    import goalflight_messages as messages

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        messages_dir = base / "messages"
        remote_messages_dir = base / "remote-messages"
        fleet_dir = base / "fleet"
        dispatch_id = "d-shared-envelope-id"

        shared_id = messages.uuid.UUID("00000000-0000-0000-0000-000000000122")
        with patch.object(messages.uuid, "uuid4", return_value=shared_id):
            messages.post_message(
                dispatch_id=dispatch_id,
                msg_type="result",
                payload={"complete": True, "text": "local terminal result"},
                messages_dir=messages_dir,
            )
            remote = messages.post_message(
                dispatch_id=dispatch_id,
                msg_type="user_need",
                payload={"text": "later remote blocker"},
                messages_dir=remote_messages_dir,
                source={"node": "remote", "adapter": "codex", "transport": "acp"},
            )
        messages.merge_remote_register(
            fleet_dir,
            Path(remote["path"]),
            messages_dir=messages_dir,
        )

        logical = messages.logical_envelopes_for_paths(
            messages.collect_inbox_paths(messages_dir, fleet_dir, dispatch_ids={dispatch_id}),
            messages_dir=messages_dir,
        )
        assert_true("same UUID with different content remains two events", len(logical) == 2)
        assert_true("both logical types survive", [env["type"] for env in logical] == ["result", "user_need"])
        aggregate = messages.build_aggregate(messages_dir=messages_dir, fleet_dir=fleet_dir)
        assert_true(
            "later cross-carrier need survives earlier terminal result",
            [item["text"] for item in aggregate["open_user_needs"]] == ["later remote blocker"],
        )


def test_read_returns_fleet_only_message_body() -> None:
    import tempfile
    import goalflight_messages as messages

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        messages_dir = base / "messages"
        remote_messages_dir = base / "remote-messages"
        fleet_dir = base / "fleet"
        dispatch_id = "d-read-fleet-only"
        mirror_remote_message(
            remote_messages_dir=remote_messages_dir,
            fleet_dir=fleet_dir,
            messages_dir=messages_dir,
            dispatch_id=dispatch_id,
            msg_type="user_need",
            payload={"text": "fleet-only body"},
        )

        read = run_messages_cli(messages_dir, fleet_dir, ["read", "--dispatch-id", dispatch_id])
        assert_true("fleet-only read succeeds", read.returncode == 0)
        envelopes = json.loads(read.stdout)
        assert_true("fleet-only read returns one envelope", len(envelopes) == 1)
        assert_true("fleet-only read returns body", envelopes[0]["payload"]["text"] == "fleet-only body")


def test_last_steering_uses_controller_ingestion_order_across_clock_skew() -> None:
    import tempfile
    from unittest.mock import patch
    import goalflight_messages as messages

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        messages_dir = base / "messages"
        remote_messages_dir = base / "remote-messages"
        fleet_dir = base / "fleet"
        dispatch_id = messages.STEERING_DISPATCH_ID

        with patch.object(messages, "utc_now", return_value="2026-08-08T10:00:10+00:00"):
            local_path = Path(messages.post_message(
                dispatch_id=dispatch_id,
                msg_type="steering",
                payload={"text": "first local steer"},
                messages_dir=messages_dir,
                seq=1,
            )["path"])
        with patch.object(messages, "utc_now", return_value="2026-08-08T10:00:00+00:00"):
            remote_path = Path(messages.post_message(
                dispatch_id=dispatch_id,
                msg_type="steering",
                payload={"text": "second remote steer"},
                messages_dir=remote_messages_dir,
                source={"node": "remote", "adapter": "codex", "transport": "acp"},
                seq=1,
            )["path"])
        merged = messages.merge_remote_register(
            fleet_dir,
            remote_path,
            messages_dir=messages_dir,
        )
        fleet_path = Path(merged["merged_into"])
        assert_true("steering local input path", local_path == messages.inbox_path(messages_dir, dispatch_id))
        assert_true("steering fleet input path", fleet_path.exists())

        aggregate = messages.build_aggregate(messages_dir=messages_dir, fleet_dir=fleet_dir)
        assert_true("second ingestion wins despite backwards source clock", aggregate["last_steering"]["payload"]["text"] == "second remote steer")
        assert_true("source timestamp remains display value", aggregate["last_steering"]["ts"] == "2026-08-08T10:00:00+00:00")


def test_remerged_identical_steering_reuses_persisted_ingestion_order() -> None:
    import tempfile
    import goalflight_messages as messages

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        messages_dir = base / "messages"
        remote_messages_dir = base / "remote-messages"
        fleet_dir = base / "fleet"
        dispatch_id = messages.STEERING_DISPATCH_ID

        remote_path = Path(messages.post_message(
            dispatch_id=dispatch_id,
            msg_type="steering",
            payload={"text": "old remote steer"},
            messages_dir=remote_messages_dir,
            source={"node": "remote", "adapter": "codex", "transport": "acp"},
            seq=1,
        )["path"])
        first_merge = messages.merge_remote_register(
            fleet_dir,
            remote_path,
            messages_dir=messages_dir,
        )
        fleet_path = Path(first_merge["merged_into"])
        first_order = messages.read_envelopes(fleet_path)[0][messages._INGESTION_ORDER_FIELD]
        local_path = Path(messages.post_message(
            dispatch_id=dispatch_id,
            msg_type="steering",
            payload={"text": "newer local steer"},
            messages_dir=messages_dir,
            seq=1,
        )["path"])
        assert_true("remerge remote input path measured", remote_path == messages.inbox_path(remote_messages_dir, dispatch_id))
        assert_true("remerge local input path measured", local_path == messages.inbox_path(messages_dir, dispatch_id))
        before_rotation = messages.build_aggregate(messages_dir=messages_dir, fleet_dir=fleet_dir)
        assert_true("genuinely newer local steer wins before rotation", before_rotation["last_steering"]["payload"]["text"] == "newer local steer")

        rotated_path = fleet_path.with_name(f"{fleet_path.stem}.rotated.jsonl")
        fleet_path.rename(rotated_path)
        second_merge = messages.merge_remote_register(
            fleet_dir,
            remote_path,
            messages_dir=messages_dir,
        )
        remerged_path = Path(second_merge["merged_into"])
        second_order = messages.read_envelopes(remerged_path)[0][messages._INGESTION_ORDER_FIELD]
        after_remerge = messages.build_aggregate(messages_dir=messages_dir, fleet_dir=fleet_dir)
        assert_true("remerged carrier input path measured", remerged_path == fleet_path)
        assert_true("identical envelope reuses first ingestion order", second_order == first_order)
        assert_true("remerge cannot displace newer local steer", after_remerge["last_steering"]["payload"]["text"] == "newer local steer")
        identity_store = messages_dir / ".ingestion-identities.json"
        assert_true("canonical ingestion identity store is outside carrier", identity_store.is_file() and identity_store.parent == messages_dir)
        identity_document = json.loads(identity_store.read_text(encoding="utf-8"))
        remote_identity = messages._canonical_envelope_identity(messages.read_envelopes(remote_path)[0])
        assert_true("identity store keys the remote event to its first order", identity_document["orders"][remote_identity] == first_order)


def test_mcp_post_matches_file_append() -> None:
    import tempfile
    from goalflight_messages import (
        goalflight_post_message_tool,
        inbox_path,
        post_message,
        read_envelopes,
        serialize_envelope_line,
    )

    with tempfile.TemporaryDirectory() as td:
        messages_dir = Path(td) / "messages"
        args = {
            "dispatch_id": "d-mcp",
            "type": "user_need",
            "payload": {"text": "via mcp"},
            "source": {"node": "local", "adapter": "mcp-spike", "transport": "mcp"},
            "seq": 1,
        }
        mcp = goalflight_post_message_tool(args, messages_dir=messages_dir)
        cli = post_message(
            dispatch_id="d-mcp",
            msg_type="user_need",
            payload={"text": "via cli"},
            messages_dir=messages_dir,
            source={"node": "local", "adapter": "cli", "transport": "controller"},
            seq=2,
        )
        path = inbox_path(messages_dir, "d-mcp")
        raw = path.read_text()
        lines = raw.splitlines(keepends=True)
        assert_true("two lines", len(lines) == 2)
        assert_true("mcp bytes canonical", lines[0] == mcp["line"])
        assert_true("cli bytes canonical", lines[1] == cli["line"])
        loaded = read_envelopes(path)
        assert_true("seq order", [e["seq"] for e in loaded] == [1, 2])
        assert_true("serialize helper", serialize_envelope_line(loaded[0]) == lines[0])


def test_post_message_rejects_invalid_seq_and_accepts_one() -> None:
    import tempfile
    from goalflight_messages import MessageError, inbox_path, post_message, read_envelopes

    with tempfile.TemporaryDirectory() as td:
        messages_dir = Path(td) / "messages"
        for bad_seq in (0, "abc"):
            try:
                post_message(
                    dispatch_id="d-seq",
                    msg_type="status",
                    payload={"text": "bad"},
                    messages_dir=messages_dir,
                    seq=bad_seq,  # type: ignore[arg-type]
                )
                assert_true(f"seq {bad_seq!r} rejected", False)
            except MessageError as exc:
                assert_true("seq error is closed", "seq must be an integer >= 1" in str(exc))

        result = post_message(
            dispatch_id="d-seq",
            msg_type="status",
            payload={"text": "ok"},
            messages_dir=messages_dir,
            seq=1,
        )
        path = inbox_path(messages_dir, "d-seq")
        loaded = read_envelopes(path)
        assert_true("valid seq writes", len(loaded) == 1)
        assert_true("valid seq remains one", loaded[0]["seq"] == 1)
        assert_true("returned line matches", path.read_text() == result["line"])


def test_post_message_allocates_seq_under_mail_lock() -> None:
    import tempfile
    import goalflight_messages as messages
    from goalflight_messages import post_message, read_envelopes

    with tempfile.TemporaryDirectory() as td:
        messages_dir = Path(td) / "messages"
        path = messages.inbox_path(messages_dir, "d-race")
        original_admit_stream_seq = messages._admit_stream_seq
        guard = threading.Lock()
        active = 0
        max_active = 0

        def slow_admit_stream_seq(*, provided_seq: int | None, envelopes: list[dict]) -> int:
            nonlocal active, max_active
            with guard:
                active += 1
                max_active = max(max_active, active)
            try:
                time.sleep(0.05)
                return original_admit_stream_seq(
                    provided_seq=provided_seq,
                    envelopes=envelopes,
                )
            finally:
                with guard:
                    active -= 1

        messages._admit_stream_seq = slow_admit_stream_seq  # type: ignore[assignment]
        try:
            threads = [
                threading.Thread(
                    target=post_message,
                    kwargs={
                        "dispatch_id": "d-race",
                        "msg_type": "status",
                        "payload": {"text": f"msg-{idx}"},
                        "messages_dir": messages_dir,
                    },
                )
                for idx in range(2)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        finally:
            messages._admit_stream_seq = original_admit_stream_seq  # type: ignore[assignment]

        loaded = read_envelopes(path)
        assert_true("serialized sequence admission critical section", max_active == 1)
        assert_true("two messages", len(loaded) == 2)
        assert_true("unique monotonic seqs", [env["seq"] for env in loaded] == [1, 2])


def test_controller_post_reaches_worker_steer_read_path() -> None:
    import tempfile
    import goalflight_steer_mailbox as steer

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        messages_dir = base / "messages"
        dispatch_id = "d-controller-live"
        with mock.patch.dict(os.environ, _journal_test_env(base), clear=False):
            write_ledger_record(base, dispatch_id, base / "project", state="running", worker_pid=os.getpid())
            steer_path = steer.steer_file(dispatch_id, state_dir=base / "state")
            steer.append_steer_entry(steer_path, "earlier steer", dispatch_id=dispatch_id)
            result = _carrier_messages.post_message(
                dispatch_id=dispatch_id,
                msg_type="controller-notice",
                payload={"text": "worker-visible notice"},
                messages_dir=messages_dir,
                deliver_to_worker=True,
            )

            assert_true("record is reported", result["recorded"] is True)
            assert_true("worker delivery is reported", result["delivery"]["delivered"] is True)
            entries = steer.worker_entries(steer.read_steer_entries(steer_path))
        delivered = entries[-1]
        assert_true("worker read path sees posted text", delivered["text"] == "worker-visible notice")
        assert_true("steer sequence remains independent", delivered["seq"] == 2)
        envelope = delivered["context"]["message_envelope"]
        assert_true("typed envelope survives projection", envelope["type"] == "controller-notice")
        assert_true("message sequence remains canonical", envelope["seq"] == 1)


def test_post_type_steer_to_dispatch_id_refuses_and_names_steer_command() -> None:
    """`post --type steer` to a worker dispatch is journal-only; refuse it."""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        messages_dir = base / "messages"
        fleet_dir = base / "fleet"
        dispatch_id = "d-live-worker-steer"
        write_ledger_record(
            base,
            dispatch_id,
            base / "project",
            state="running",
            worker_pid=os.getpid(),
        )
        journal = messages_dir / f"{dispatch_id}.jsonl"
        mailbox = messages_dir.parent / "state" / "dispatch" / f"{dispatch_id}.steer.jsonl"
        assert_true("mailbox starts empty", not mailbox.exists())
        assert_true("journal starts empty", not journal.exists())

        for msg_type in ("steer", "steering"):
            posted = run_messages_cli(
                messages_dir,
                fleet_dir,
                [
                    "post",
                    "--dispatch-id",
                    dispatch_id,
                    "--type",
                    msg_type,
                    "--text",
                    "looks delivered",
                ],
            )
            assert_true(
                f"{msg_type} post is usage-refuse: rc={posted.returncode} stdout={posted.stdout!r}",
                posted.returncode == 2,
            )
            assert_true(
                f"{msg_type} uses refused prefix",
                "post: refused:" in posted.stderr,
            )
            assert_true(
                f"{msg_type} names dispatch.steer",
                "goalflight_dispatch.py steer" in posted.stderr
                and dispatch_id in posted.stderr,
            )
            assert_true(
                f"{msg_type} says no worker reads it",
                "no worker will read" in posted.stderr,
            )
            assert_true(
                f"{msg_type} does not print a success envelope",
                "recorded" not in posted.stdout,
            )
            assert_true(f"{msg_type} does not write journal", not journal.exists())
            assert_true(f"{msg_type} does not write mailbox", not mailbox.exists())


def test_post_type_steer_to_fleet_steering_register_still_records() -> None:
    """Fleet-register `steer` posts still file on the steering stream."""
    import tempfile
    from goalflight_messages import STEERING_DISPATCH_ID, read_envelopes

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        messages_dir = base / "messages"
        fleet_dir = base / "fleet"
        journal = messages_dir / f"{STEERING_DISPATCH_ID}.jsonl"
        mailbox = messages_dir.parent / "state" / "dispatch" / f"{STEERING_DISPATCH_ID}.steer.jsonl"
        assert_true("fleet-steering mailbox starts empty", not mailbox.exists())
        assert_true("fleet-steering journal starts empty", not journal.exists())
        posted = run_messages_cli(
            messages_dir,
            fleet_dir,
            [
                "post",
                "--dispatch-id",
                STEERING_DISPATCH_ID,
                "--type",
                "steer",
                "--text",
                "fleet register body",
            ],
        )
        assert_true(f"fleet-steering post succeeds: {posted.stderr}", posted.returncode == 0)
        result = json.loads(posted.stdout)
        assert_true("fleet-steering post is recorded", result["recorded"] is True)
        assert_true(
            "fleet-steering post is not a worker delivery",
            result["delivery"]["worker_view_written"] is False,
        )
        envelopes = read_envelopes(journal)
        assert_true("fleet-steering journal has the body", envelopes[0]["payload"]["text"] == "fleet register body")
        assert_true("fleet-steering journal keeps type steer", envelopes[0]["type"] == "steer")
        assert_true("fleet-steering post does not write a worker mailbox", not mailbox.exists())


def test_post_message_steer_type_to_dispatch_id_raises_before_write() -> None:
    """Library and MCP post helpers refuse before creating a carrier."""
    import tempfile
    import goalflight_steer_mailbox as steer
    from goalflight_messages import (
        MessageError,
        goalflight_post_message_tool,
        post_message,
    )

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        messages_dir = base / "messages"
        dispatch_id = "d-library-steer"
        journal = messages_dir / f"{dispatch_id}.jsonl"
        isolated = {
            "GOALFLIGHT_STATE_DIR": str(base / "state"),
            "GOALFLIGHT_MESSAGES_DIR": str(messages_dir),
        }
        previous = {key: os.environ.get(key) for key in (*isolated, "GOALFLIGHT_DISPATCH_DIR")}
        os.environ.update(isolated)
        os.environ.pop("GOALFLIGHT_DISPATCH_DIR", None)
        try:
            mailbox = steer.steer_file(dispatch_id, state_dir=base / "state")
            assert_true("library journal starts empty", not journal.exists())
            assert_true("library mailbox starts empty", not mailbox.exists())
            callers = (
                (
                    "post_message",
                    lambda: post_message(
                        dispatch_id=dispatch_id,
                        msg_type="steer",
                        payload={"text": "library post"},
                        messages_dir=messages_dir,
                    ),
                ),
                (
                    "mcp_tool",
                    lambda: goalflight_post_message_tool(
                        {
                            "dispatch_id": dispatch_id,
                            "type": "steering",
                            "payload": {"text": "mcp post"},
                        },
                        messages_dir=messages_dir,
                    ),
                ),
            )
            for label, call in callers:
                try:
                    call()
                except MessageError as exc:
                    detail = str(exc)
                    assert_true(f"{label} names dispatch.steer", "goalflight_dispatch.py steer" in detail)
                    assert_true(f"{label} names the dispatch id", dispatch_id in detail)
                else:
                    raise AssertionError(f"{label} accepted a steering type to a worker dispatch")
            assert_true("library path does not create the journal", not journal.exists())
            assert_true("library path does not create the mailbox", not mailbox.exists())
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def test_resolved_controller_post_reports_projected_journal_delivery() -> None:
    import goalflight_journal

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        project = base / "project"
        init_git_project(project)
        messages_dir = base / "messages"
        fleet_dir = base / "fleet"
        label = "report-controller"
        env = _journal_test_env(base)
        with mock.patch.dict(os.environ, env, clear=False):
            authority = goalflight_journal.open_or_create_journal(project)
            claimed = authority.claim_or_renew_lease(
                label,
                principal={"principal_id": "post-report-test"},
            )
            assert_true("report controller lease claimed", claimed.committed)

            posted = run_messages_cli(
                messages_dir,
                fleet_dir,
                [
                    "post",
                    "--dispatch-id",
                    "controller-report",
                    "--type",
                    "controller-answer",
                    "--text",
                    "delivery is visible",
                    "--to-controller",
                    label,
                    "--controller-project-root",
                    str(project),
                ],
            )

            assert_true(f"resolved controller post succeeds: {posted.stderr}", posted.returncode == 0)
            result = json.loads(posted.stdout)
            delivery = result["controller_delivery"]
            assert_true("controller channel requested", delivery["requested"] is True)
            assert_true("controller channel delivered", delivery["delivered"] is True)
            assert_true("controller delivery status is explicit", delivery["status"] == "delivered_to_controller")
            assert_true("controller recipient is reported", delivery["recipient_label"] == label)
            assert_true("controller cursor backlog is reported", delivery["cursor"]["backlog_pending"] == 1)
            rows = authority.read_all(
                """SELECT recipient_label, event_uuid, projected_at
                   FROM delivery_events WHERE event_uuid = ?""",
                (delivery["event_uuid"],),
            )
            assert_true("reported delivery event exists in journal", len(rows) == 1)
            assert_true("journal recipient matches report", rows[0]["recipient_label"] == label)
            assert_true("journal event UUID matches report", rows[0]["event_uuid"] == delivery["event_uuid"])
            assert_true("journal delivery is projected", rows[0]["projected_at"] is not None)


def test_unresolved_controller_post_reports_reaching_nobody() -> None:
    import goalflight_journal

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        project = base / "project"
        init_git_project(project)
        messages_dir = base / "messages"
        fleet_dir = base / "fleet"
        missing_label = "missing-controller"
        env = _journal_test_env(base)
        with mock.patch.dict(os.environ, env, clear=False):
            authority = goalflight_journal.open_or_create_journal(project)
            posted = run_messages_cli(
                messages_dir,
                fleet_dir,
                [
                    "post",
                    "--dispatch-id",
                    "controller-unresolved",
                    "--type",
                    "controller-answer",
                    "--text",
                    "nobody can receive this",
                    "--to-controller",
                    missing_label,
                    "--controller-project-root",
                    str(project),
                ],
            )

            result = json.loads(posted.stdout)
            delivery = result["controller_delivery"]
            assert_true("unresolved controller post exits nonzero", posted.returncode != 0)
            assert_true("unresolved controller channel was requested", delivery["requested"] is True)
            assert_true("unresolved controller is not claimed delivered", delivery["delivered"] is False)
            assert_true(
                "unresolved status is machine-distinct",
                delivery["status"] == "controller_addressee_unresolved",
            )
            assert_true("unresolved label is reported", delivery["recipient_label"] == missing_label)
            assert_true("unresolved report says it reached nobody", "reached nobody" in delivery["detail"])
            assert_true("unresolved stderr reports controller failure", "reached nobody" in posted.stderr)
            rows = authority.read_all(
                "SELECT event_uuid FROM delivery_events WHERE event_uuid = ?",
                (delivery["event_uuid"],),
            )
            assert_true("unresolved post remains recorded in journal", len(rows) == 1)


def test_post_without_any_delivery_target_reports_reaching_nobody() -> None:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        posted = run_messages_cli(
            base / "messages",
            base / "fleet",
            [
                "post",
                "--dispatch-id",
                "record-only-no-target",
                "--type",
                "status",
                "--text",
                "record without a recipient",
            ],
        )

        assert_true(f"record-only post succeeds: {posted.stderr}", posted.returncode == 0)
        result = json.loads(posted.stdout)
        delivery = result["controller_delivery"]
        assert_true("controller channel is not requested", delivery["requested"] is False)
        assert_true("record-only post is not delivered", delivery["delivered"] is False)
        assert_true("record-only status names nobody", delivery["status"] == "recorded_reached_nobody")
        assert_true("record-only detail names nobody", "reached nobody" in delivery["detail"])


def test_worker_delivery_post_report_shape_is_unchanged() -> None:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        messages_dir = base / "messages"
        dispatch_id = "worker-report-shape"
        write_ledger_record(base, dispatch_id, base / "project", state="running", worker_pid=os.getpid())

        with mock.patch.dict(os.environ, _journal_test_env(base), clear=False):
            result = _carrier_messages.post_message(
                dispatch_id=dispatch_id,
                msg_type="controller-notice",
                payload={"text": "worker channel only"},
                messages_dir=messages_dir,
                deliver_to_worker=True,
            )

        assert_true(
            "worker post top-level report shape is unchanged",
            set(result) == {"envelope", "line", "path", "recorded", "delivery"},
        )
        assert_true(
            "worker delivery report fields are unchanged",
            set(result["delivery"])
            == {
                "requested",
                "delivered",
                "worker_view_written",
                "status",
                "dispatch_classification",
                "steer_path",
                "steer_seq",
                "steer_entry",
                "detail",
            },
        )
        assert_true("worker delivery remains reported", result["delivery"]["status"] == "worker_view_written")


def test_cross_project_controller_post_reports_recipient_journal_delivery() -> None:
    import goalflight_journal

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        sender_project = base / "sender"
        recipient_project = base / "recipient"
        init_git_project(sender_project)
        init_git_project(recipient_project)
        messages_dir = base / "messages"
        fleet_dir = base / "fleet"
        label = "cross-project-controller"
        env = _journal_test_env(base)
        with mock.patch.dict(os.environ, env, clear=False):
            recipient_authority = goalflight_journal.open_or_create_journal(recipient_project)
            claimed = recipient_authority.claim_or_renew_lease(
                label,
                principal={"principal_id": "cross-project-report-test"},
            )
            assert_true("cross-project controller lease claimed", claimed.committed)

            posted = run_messages_cli(
                messages_dir,
                fleet_dir,
                [
                    "post",
                    "--dispatch-id",
                    "cross-project-report",
                    "--type",
                    "controller-answer",
                    "--text",
                    "recipient project owns this",
                    "--to-controller",
                    label,
                    "--controller-project-root",
                    str(recipient_project),
                ],
                cwd=sender_project,
            )

            assert_true(f"cross-project post succeeds: {posted.stderr}", posted.returncode == 0)
            result = json.loads(posted.stdout)
            delivery = result["controller_delivery"]
            assert_true("cross-project controller delivery is reported", delivery["delivered"] is True)
            assert_true("cross-project recipient is reported", delivery["recipient_label"] == label)
            assert_true(
                "cross-project recipient root is reported",
                delivery["project_root"] == str(recipient_project.resolve()),
            )
            rows = recipient_authority.read_all(
                "SELECT event_uuid FROM delivery_events WHERE event_uuid = ?",
                (delivery["event_uuid"],),
            )
            assert_true("reported event is in recipient journal", len(rows) == 1)


def test_worker_sideband_type_does_not_echo_to_worker_from_controller_context() -> None:
    import tempfile
    from goalflight_messages import read_envelopes

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        messages_dir = base / "messages"
        fleet_dir = base / "fleet"
        dispatch_id = "d-controller-sideband"
        write_ledger_record(base, dispatch_id, base / "project", state="running", worker_pid=os.getpid())

        posted = run_messages_cli(
            messages_dir,
            fleet_dir,
            ["post", "--dispatch-id", dispatch_id, "--type", "status", "--text", "worker progress"],
        )

        assert_true(f"sideband record succeeds: {posted.stderr}", posted.returncode == 0)
        result = json.loads(posted.stdout)
        assert_true("worker delivery is not requested for sideband type", result["delivery"]["requested"] is False)
        assert_true("sideband is still recorded", read_envelopes(messages_dir / f"{dispatch_id}.jsonl")[0]["type"] == "status")
        steer_path = base / "state" / "dispatch" / f"{dispatch_id}.steer.jsonl"
        assert_true("sideband does not echo into worker steer", not steer_path.exists())


def test_controller_channel_types_project_and_remain_in_aggregate() -> None:
    import tempfile
    import goalflight_steer_mailbox as steer
    from goalflight_messages import CONTROLLER_CHANNEL_TYPES, build_aggregate

    expected_types = {
        "controller-question",
        "controller-answer",
        "controller-notice",
        "controller-coordination",
        "coordination",
        "notice",
    }
    assert_true("controller channel type contract is complete", CONTROLLER_CHANNEL_TYPES == expected_types)

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        messages_dir = base / "messages"
        fleet_dir = base / "fleet"
        dispatch_id = "d-controller-types"
        fleet_dir.mkdir()
        write_ledger_record(base, dispatch_id, base / "project", state="running", worker_pid=os.getpid())

        with mock.patch.dict(os.environ, _journal_test_env(base), clear=False):
            for msg_type in sorted(expected_types):
                posted = _carrier_messages.post_message(
                    dispatch_id=dispatch_id,
                    msg_type=msg_type,
                    payload={"text": msg_type},
                    messages_dir=messages_dir,
                    deliver_to_worker=True,
                )
                assert_true(f"{msg_type} reaches worker", posted["recorded"] is True)
            entries = steer.worker_entries(
                steer.read_steer_entries(steer.steer_file(dispatch_id, state_dir=base / "state"))
            )
            projected_types = {
                entry["context"]["message_envelope"]["type"]
                for entry in entries
            }
            assert_true("every controller channel type projects", projected_types == expected_types)
            aggregate = build_aggregate(messages_dir=messages_dir, fleet_dir=fleet_dir)
            aggregate_types = {item["type"] for item in aggregate["open_controller_channel"]}
            assert_true("every projected type remains relay-visible", aggregate_types == expected_types)


def test_concurrent_controller_posts_preserve_worker_view_order() -> None:
    import tempfile
    import goalflight_messages as messages
    import goalflight_steer_mailbox as steer

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        messages_dir = base / "messages"
        dispatch_id = "d-controller-concurrent"
        first_delivery_started = threading.Event()
        release_first_delivery = threading.Event()
        second_delivery_started = threading.Event()
        errors: list[BaseException] = []
        original_record = messages._dispatch_record
        original_append = messages.goalflight_steer_mailbox.append_message_view

        def running_record(_dispatch_id: str) -> tuple[dict, None]:
            import goalflight_ledger

            return (
                {
                    "dispatch_id": dispatch_id,
                    "state": "running",
                    "worker_pid": os.getpid(),
                    "worker_identity": goalflight_ledger.process_identity(os.getpid()),
                },
                None,
            )

        def delayed_append(target_dispatch_id: str, envelope: dict):
            if envelope["seq"] == 1:
                first_delivery_started.set()
                if not release_first_delivery.wait(timeout=2):
                    raise AssertionError("timed out waiting to release first delivery")
            else:
                second_delivery_started.set()
            return original_append(target_dispatch_id, envelope, state_dir=base / "state")

        def post(text: str) -> None:
            try:
                messages.post_message(
                    dispatch_id=dispatch_id,
                    msg_type="controller-notice",
                    payload={"text": text},
                    messages_dir=messages_dir,
                    deliver_to_worker=True,
                )
            except BaseException as exc:  # noqa: BLE001 - relayed to the test thread
                errors.append(exc)

        messages._dispatch_record = running_record  # type: ignore[assignment]
        messages.goalflight_steer_mailbox.append_message_view = delayed_append  # type: ignore[assignment]
        first = threading.Thread(target=post, args=("first",))
        second = threading.Thread(target=post, args=("second",))
        try:
            first.start()
            assert_true("first delivery reaches projection", first_delivery_started.wait(timeout=1))
            second.start()
            # Broken code reaches the second projection while seq 1 is paused.
            # Correct code holds the message lock until seq 1's projection lands.
            second_delivery_started.wait(timeout=0.5)
            release_first_delivery.set()
            first.join(timeout=2)
            second.join(timeout=2)
        finally:
            release_first_delivery.set()
            first.join(timeout=2)
            second.join(timeout=2)
            messages._dispatch_record = original_record  # type: ignore[assignment]
            messages.goalflight_steer_mailbox.append_message_view = original_append  # type: ignore[assignment]

        assert_true("concurrent posts do not raise", not errors)
        assert_true("concurrent post threads finish", not first.is_alive() and not second.is_alive())
        entries = steer.worker_entries(
            steer.read_steer_entries(steer.steer_file(dispatch_id, state_dir=base / "state"))
        )
        envelope_seqs = [entry["context"]["message_envelope"]["seq"] for entry in entries]
        assert_true("worker view follows canonical message order", envelope_seqs == [1, 2])


def test_live_controller_post_delivery_failure_is_nonzero_and_recorded() -> None:
    import tempfile
    from goalflight_messages import read_envelopes

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        messages_dir = base / "messages"
        fleet_dir = base / "fleet"
        dispatch_id = "d-controller-undeliverable"
        write_ledger_record(base, dispatch_id, base / "project", state="running", worker_pid=os.getpid())
        blocked_steer_path = base / "state" / "dispatch" / f"{dispatch_id}.steer.jsonl"
        blocked_steer_path.mkdir(parents=True)

        with mock.patch.dict(os.environ, _journal_test_env(base), clear=False):
            result = _carrier_messages.post_message(
                dispatch_id=dispatch_id,
                msg_type="controller-notice",
                payload={"text": "record even when delivery fails"},
                messages_dir=messages_dir,
                deliver_to_worker=True,
            )

        assert_true("failed delivery still reports record", result["recorded"] is True)
        assert_true("failed delivery is explicit", result["delivery"]["status"] == "worker_delivery_failed")
        assert_true("failed delivery is not claimed", result["delivery"]["delivered"] is False)
        assert_true(
            "call site says record versus delivery",
            "recorded but worker delivery failed" in result["delivery"]["detail"],
        )
        envelopes = read_envelopes(messages_dir / f"{dispatch_id}.jsonl")
        assert_true(
            "record survives delivery failure",
            envelopes[0]["payload"]["text"] == "record even when delivery fails",
        )


def test_running_label_without_live_identity_is_not_delivery() -> None:
    import tempfile
    from goalflight_messages import read_envelopes

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        messages_dir = base / "messages"
        fleet_dir = base / "fleet"
        dispatch_id = "d-controller-no-worker"
        write_ledger_record(base, dispatch_id, base / "project", state="running")

        with mock.patch.dict(os.environ, _journal_test_env(base), clear=False):
            result = _carrier_messages.post_message(
                dispatch_id=dispatch_id,
                msg_type="controller-notice",
                payload={"text": "do not call a state label delivery"},
                messages_dir=messages_dir,
                deliver_to_worker=True,
            )

        assert_true("message remains recorded", result["recorded"] is True)
        assert_true("no worker delivery is claimed", result["delivery"]["delivered"] is False)
        assert_true("missing worker identity is explicit", result["delivery"]["dispatch_classification"] == "unknown_no_pid")
        assert_true("worker view is not written", result["delivery"]["worker_view_written"] is False)
        assert_true("record survives unavailable worker", len(read_envelopes(messages_dir / f"{dispatch_id}.jsonl")) == 1)


def test_detached_controller_dead_record_with_live_worker_still_delivers() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        messages_dir = base / "messages"
        fleet_dir = base / "fleet"
        dispatch_id = "d-detached-live-worker"
        write_ledger_record(
            base,
            dispatch_id,
            base / "project",
            state="controller_dead",
            worker_pid=os.getpid(),
            detached=True,
            reason="controller_dead",
        )

        with mock.patch.dict(os.environ, _journal_test_env(base), clear=False):
            result = _carrier_messages.post_message(
                dispatch_id=dispatch_id,
                msg_type="controller-notice",
                payload={"text": "detached worker still owns this mailbox"},
                messages_dir=messages_dir,
                deliver_to_worker=True,
            )

        assert_true("detached worker is classified live", result["delivery"]["dispatch_classification"] == "expected_live")
        assert_true("detached live worker receives view", result["delivery"]["delivered"] is True)


def test_terminal_controller_post_is_recorded_and_labelled_record_only() -> None:
    import tempfile
    from goalflight_messages import read_envelopes

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        messages_dir = base / "messages"
        fleet_dir = base / "fleet"
        dispatch_id = "d-controller-terminal"
        write_ledger_record(base, dispatch_id, base / "project", state="complete")

        with mock.patch.dict(os.environ, _journal_test_env(base), clear=False):
            result = _carrier_messages.post_message(
                dispatch_id=dispatch_id,
                msg_type="controller-notice",
                payload={"text": "terminal history"},
                messages_dir=messages_dir,
                deliver_to_worker=True,
            )

        assert_true("terminal post is recorded", result["recorded"] is True)
        assert_true("terminal delivery is false", result["delivery"]["delivered"] is False)
        assert_true(
            "terminal result is labelled record-only",
            result["delivery"]["status"] == "terminal_recorded_only",
        )
        assert_true("terminal result says no reader", "no worker will read it" in result["delivery"]["detail"])
        envelopes = read_envelopes(messages_dir / f"{dispatch_id}.jsonl")
        assert_true("terminal message stays in record", envelopes[0]["payload"]["text"] == "terminal history")
        steer_path = base / "state" / "dispatch" / f"{dispatch_id}.steer.jsonl"
        assert_true("terminal post does not create worker view", not steer_path.exists())


def _claim_test_controller(
    base: Path,
    project: Path,
    *,
    label: str = "mine-controller",
    session_id: str = "mine-session",
) -> dict[str, str | None]:
    import goalflight_session_status as sessions

    sessions.claim_session(
        project,
        pid=os.getpid(),
        session_id=session_id,
        label=label,
    )
    updates = {
        "GOALFLIGHT_STATE_DIR": str(base / "state"),
        "GOALFLIGHT_MESSAGES_DIR": str(base / "messages"),
        "GOALFLIGHT_FLEET_DIR": str(base / "fleet"),
        "GOALFLIGHT_CONTROLLER_PID": str(os.getpid()),
        "GOALFLIGHT_CONTROLLER_LABEL": label,
    }
    previous = {key: os.environ.get(key) for key in updates}
    os.environ.update(updates)
    return previous


def _restore_test_controller(previous: dict[str, str | None]) -> None:
    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def test_supervise_failed_migration_retains_existing_coverage_and_reports() -> None:
    import goalflight_wake
    import goalflight_wake_supervise as supervise

    incumbent = goalflight_wake.WaiterRecord(
        kind="listener",
        label_hash="a" * 16,
        pid=41001,
        start_hash="b" * 16,
        instance_id="c" * 32,
        path=ROOT / "incumbent.lock",
        generation_hash="d" * 24,
    )
    args = mock.Mock(
        project_root=str(ROOT),
        controller_label="migration-test",
        lease_nonce="migration-nonce",
    )

    def replacement_fails(_args, **kwargs) -> int:
        assert callable(kwargs.get("on_startup_probe"))
        return 17

    stderr = io.StringIO()
    with (
        mock.patch.object(
            _carrier_messages.goalflight_wake,
            "live_waiters",
            return_value=[incumbent],
        ),
        mock.patch.object(supervise, "cmd_supervise", replacement_fails),
        mock.patch.object(_carrier_messages.os, "kill") as kill,
        contextlib.redirect_stderr(stderr),
    ):
        result = _carrier_messages.cmd_supervise(args)

    assert result == 17
    kill.assert_not_called()
    assert "did not emit the stdout-peer-liveness probe" in stderr.getvalue()
    assert "existing wake coverage retained" in stderr.getvalue()


def test_supervise_proven_migration_releases_existing_coverage_once() -> None:
    import goalflight_wake
    import goalflight_wake_supervise as supervise

    start_token = "migration-incumbent-start"
    incumbent = goalflight_wake.WaiterRecord(
        kind="listener",
        label_hash="a" * 16,
        pid=41002,
        start_hash=goalflight_wake._start_hash(start_token),
        instance_id="c" * 32,
        path=ROOT / "incumbent.lock",
        generation_hash="d" * 24,
    )
    args = mock.Mock(
        project_root=str(ROOT),
        controller_label="migration-test",
        lease_nonce="migration-nonce",
    )
    events: list[object] = []

    def replacement_proves_live(_args, **kwargs) -> int:
        events.append("probe")
        on_probe = kwargs["on_startup_probe"]
        assert on_probe(ROOT, "migration-test", "resolved-migration-nonce") is None
        events.append("replacement-continues")
        assert on_probe(ROOT, "migration-test", "resolved-migration-nonce") is None
        return 0

    def record_release(pid: int, signum: int) -> None:
        events.append(("release", pid, signum))

    stderr = io.StringIO()
    with (
        mock.patch.object(
            _carrier_messages.goalflight_wake,
            "live_waiters",
            side_effect=[[incumbent], [incumbent], []],
        ) as live_waiters,
        mock.patch.object(supervise, "cmd_supervise", replacement_proves_live),
        mock.patch.object(
            _carrier_messages.goalflight_compat,
            "process_start_identity",
            return_value={"start_token": start_token},
        ),
        mock.patch.object(_carrier_messages.os, "kill", side_effect=record_release) as kill,
        mock.patch.object(_carrier_messages.time, "sleep"),
        contextlib.redirect_stderr(stderr),
    ):
        result = _carrier_messages.cmd_supervise(args)

    assert result == 0
    assert events == [
        "probe",
        ("release", incumbent.pid, _carrier_messages.signal.SIGTERM),
        "replacement-continues",
    ]
    kill.assert_called_once_with(incumbent.pid, _carrier_messages.signal.SIGTERM)
    assert live_waiters.call_count == 3
    assert all(
        call.kwargs["generation_key"] == "resolved-migration-nonce"
        for call in live_waiters.call_args_list
    )
    assert stderr.getvalue() == ""


def test_supervise_uncertain_release_arms_proven_replacement() -> None:
    import goalflight_wake
    import goalflight_wake_supervise as supervise

    start_token = "migration-incumbent-start"
    incumbent = goalflight_wake.WaiterRecord(
        kind="listener",
        label_hash="a" * 16,
        pid=41003,
        start_hash=goalflight_wake._start_hash(start_token),
        instance_id="c" * 32,
        path=ROOT / "incumbent.lock",
        generation_hash="d" * 24,
    )
    args = mock.Mock(
        project_root=str(ROOT),
        controller_label="migration-test",
        lease_nonce=None,
    )
    continued = False

    def replacement_proves_live(_args, **kwargs) -> int:
        nonlocal continued
        on_probe = kwargs["on_startup_probe"]
        assert on_probe(ROOT, "migration-test", "resolved-migration-nonce") is None
        continued = True
        return 0

    stderr = io.StringIO()
    with (
        mock.patch.object(
            _carrier_messages.goalflight_wake,
            "live_waiters",
            return_value=[incumbent],
        ),
        mock.patch.object(supervise, "cmd_supervise", replacement_proves_live),
        mock.patch.object(
            _carrier_messages.goalflight_compat,
            "process_start_identity",
            return_value={"start_token": start_token},
        ),
        mock.patch.object(_carrier_messages.os, "kill") as kill,
        mock.patch.object(
            _carrier_messages.time,
            "monotonic",
            side_effect=[0.0, 4.0],
        ),
        contextlib.redirect_stderr(stderr),
    ):
        result = _carrier_messages.cmd_supervise(args)

    assert result == 0
    assert continued
    kill.assert_called_once_with(incumbent.pid, _carrier_messages.signal.SIGTERM)
    assert "could not be confirmed" in stderr.getvalue()
    assert "arming replacement to avoid zero coverage" in stderr.getvalue()


def test_supervise_migration_skips_released_or_reused_incumbent_pid() -> None:
    import goalflight_wake
    import goalflight_wake_supervise as supervise

    incumbent = goalflight_wake.WaiterRecord(
        kind="listener",
        label_hash="a" * 16,
        pid=41004,
        start_hash=goalflight_wake._start_hash("original-owner"),
        instance_id="c" * 32,
        path=ROOT / "incumbent.lock",
        generation_hash="d" * 24,
    )
    args = mock.Mock(
        project_root=str(ROOT),
        controller_label="migration-test",
        lease_nonce="migration-nonce",
    )

    def replacement_proves_live(_args, **kwargs) -> int:
        failure = kwargs["on_startup_probe"](
            ROOT,
            "migration-test",
            "resolved-migration-nonce",
        )
        assert failure is None
        return 0

    cases = [
        ({"start_token": "reused-owner"}, True),
        (None, False),
    ]
    for identity, liveness in cases:
        with (
            mock.patch.object(
                _carrier_messages.goalflight_wake,
                "live_waiters",
                return_value=[incumbent],
            ),
            mock.patch.object(supervise, "cmd_supervise", replacement_proves_live),
            mock.patch.object(
                _carrier_messages.goalflight_compat,
                "process_start_identity",
                return_value=identity,
            ),
            mock.patch.object(
                _carrier_messages.goalflight_compat,
                "pid_liveness",
                return_value=liveness,
            ),
            mock.patch.object(
                _carrier_messages.goalflight_compat,
                "pid_is_zombie",
                return_value=False,
            ),
            mock.patch.object(_carrier_messages.os, "kill") as kill,
        ):
            result = _carrier_messages.cmd_supervise(args)

        assert result == 0
        kill.assert_not_called()


def test_mcp_stdio_tools_call() -> None:
    import json
    import subprocess
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        messages_dir = Path(td) / "messages"
        env = {
            **dict(os.environ),
            "GOALFLIGHT_MESSAGES_DIR": str(messages_dir),
            "GOALFLIGHT_STATE_DIR": str(Path(td) / "state"),
            "GOAL_FLIGHT_PIDFILE_DIR": str(Path(td) / "pids"),
            "GOALFLIGHT_TASK_STORE_DIR": str(Path(td) / "task-store"),
        }
        req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "goalflight_post_message",
                "arguments": {
                    "dispatch_id": "d-stdio",
                    "type": "status",
                    "payload": {"text": "stdio spike"},
                    "seq": 1,
                },
            },
        }
        proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "goalflight_mcp_messages.py"), "stdio"],
            input=json.dumps(req) + "\n",
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert_true("stdio exit 0", proc.returncode == 0)
        response = json.loads(proc.stdout.strip().splitlines()[-1])
        assert_true("no rpc error", "error" not in response)
        content = response["result"]["content"][0]["text"]
        posted = json.loads(content)
        path = messages_dir / "d-stdio.jsonl"
        assert_true("file exists", path.exists())
        assert_true("stdio line match", path.read_text() == posted["line"])


def test_mcp_delivery_failure_sets_tool_error_and_call_exit() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        messages_dir = base / "messages"
        fleet_dir = base / "fleet"
        dispatch_id = "d-mcp-undeliverable"
        write_ledger_record(base, dispatch_id, base / "project", state="running", worker_pid=os.getpid())
        blocked_steer_path = base / "state" / "dispatch" / f"{dispatch_id}.steer.jsonl"
        blocked_steer_path.mkdir(parents=True)
        arguments = {
            "dispatch_id": dispatch_id,
            "type": "controller-notice",
            "payload": {"text": "recorded MCP failure"},
        }
        env = {
            **dict(os.environ),
            "GOALFLIGHT_MESSAGES_DIR": str(messages_dir),
            "GOALFLIGHT_FLEET_DIR": str(fleet_dir),
            "GOALFLIGHT_STATE_DIR": str(base / "state"),
            "GOALFLIGHT_DISPATCH_DIR": str(base / "state" / "dispatch"),
            "GOAL_FLIGHT_PIDFILE_DIR": str(base / "pids"),
            "GOALFLIGHT_TASK_STORE_DIR": str(base / "task-store"),
        }
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "goalflight_post_message", "arguments": arguments},
        }

        stdio = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "goalflight_mcp_messages.py"), "stdio"],
            input=json.dumps(request) + "\n",
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert_true(f"MCP stdio server stays healthy: {stdio.stderr}", stdio.returncode == 0)
        response = json.loads(stdio.stdout.strip().splitlines()[-1])
        assert_true("MCP tool result is an error", response["result"]["isError"] is True)
        posted = json.loads(response["result"]["content"][0]["text"])
        assert_true("MCP error still reports record", posted["recorded"] is True)
        assert_true("MCP error does not claim delivery", posted["delivery"]["delivered"] is False)

        call = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "goalflight_mcp_messages.py"),
                "call",
                "--arguments",
                json.dumps(arguments),
            ],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert_true("one-shot MCP call exits nonzero on delivery failure", call.returncode != 0)
        call_result = json.loads(call.stdout)
        assert_true("one-shot failure still reports record", call_result["recorded"] is True)


def test_controller_relay_sanitizes_worker_text() -> None:
    from goalflight_messages import format_controller_relay

    rendered = format_controller_relay(
        {
            "open_user_needs": [
                {
                    "dispatch_id": "worker-legacy",
                    "type": "user_need",
                    "text": "needs\nFORGED\x1b[31m review",
                }
            ]
        }
    )

    assert_true(
        "legacy controller relay is one inert line",
        rendered
        == r"USER-NEED relay: [worker-legacy] user_need: needs FORGED\x1b[31m review",
    )
    assert_true("legacy controller relay has no raw CSI", "\x1b" not in (rendered or ""))


def test_cli_unaddressed_controller_mail_is_refused_and_writes_nothing() -> None:
    from goalflight_messages import MessageError

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        messages_dir = base / "messages"
        fleet_dir = base / "fleet"
        env = _journal_test_env(base)

        notice = run_messages_cli(
            messages_dir,
            fleet_dir,
            [
                "post",
                "--dispatch-id",
                "unaddressed-notice",
                "--type",
                "controller-notice",
                "--text",
                "should bounce",
                "--json",
            ],
        )
        assert_true("unaddressed controller-notice exits 2", notice.returncode == 2)
        assert_true("unaddressed notice names --to-controller", "--to-controller LABEL" in notice.stderr)
        assert_true("unaddressed notice names controller-notice", "controller-notice" in notice.stderr)
        assert_true("unaddressed notice writes nothing", not (messages_dir / "unaddressed-notice.jsonl").exists())
        assert_true("unaddressed notice is not ok JSON", "ok" not in notice.stdout or "true" not in notice.stdout)

        empty = None
        with mock.patch.dict(os.environ, env, clear=False):
            try:
                _carrier_messages.post_message(
                    dispatch_id="empty-addressee",
                    msg_type="controller-notice",
                    payload={"text": "empty object"},
                    messages_dir=messages_dir,
                    addressee={},
                )
            except MessageError as exc:
                empty = exc
        assert_true("empty addressee raises", empty is not None)
        assert_true("empty addressee names --to-controller", "--to-controller LABEL" in str(empty))
        assert_true("empty addressee writes nothing", not (messages_dir / "empty-addressee.jsonl").exists())

        advisory = run_messages_cli(
            messages_dir,
            fleet_dir,
            [
                "post",
                "--dispatch-id",
                "advisory-nowhere",
                "--type",
                "advisory",
                "--text",
                "bug report",
                "--json",
            ],
        )
        assert_true("advisory CLI exits 2", advisory.returncode == 2)
        assert_true("advisory names --to-controller", "--to-controller LABEL" in advisory.stderr)
        assert_true("advisory names controller-notice", "--type controller-notice" in advisory.stderr)
        assert_true("advisory writes nothing", not (messages_dir / "advisory-nowhere.jsonl").exists())
        assert_true("advisory JSON is not ok:true", '"ok"' not in advisory.stdout)

        for junk in ("note", "defect-notice", "qa-bug", "controller-note"):
            posted = run_messages_cli(
                messages_dir,
                fleet_dir,
                ["post", "--dispatch-id", f"junk-{junk}", "--type", junk, "--text", "nope"],
            )
            assert_true(f"{junk} CLI exits 2", posted.returncode == 2)
            assert_true(f"{junk} names controller-notice", "controller-notice" in posted.stderr)
            assert_true(f"{junk} names --to-controller", "--to-controller LABEL" in posted.stderr)
            assert_true(f"{junk} writes nothing", not (messages_dir / f"junk-{junk}.jsonl").exists())


def test_cli_addressed_controller_notice_still_succeeds() -> None:
    import goalflight_journal

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        project = base / "project"
        init_git_project(project)
        messages_dir = base / "messages"
        fleet_dir = base / "fleet"
        label = "addressed-controller"
        env = _journal_test_env(base)
        with mock.patch.dict(os.environ, env, clear=False):
            authority = goalflight_journal.open_or_create_journal(project)
            claimed = authority.claim_or_renew_lease(
                label,
                principal={"principal_id": "addressed-post-test"},
            )
            assert_true("addressed controller lease claimed", claimed.committed)
            posted = run_messages_cli(
                messages_dir,
                fleet_dir,
                [
                    "post",
                    "--dispatch-id",
                    "addressed-notice",
                    "--type",
                    "controller-notice",
                    "--text",
                    "reaches the label",
                    "--to-controller",
                    label,
                    "--controller-project-root",
                    str(project),
                    "--json",
                ],
            )
            assert_true(f"addressed controller-notice succeeds: {posted.stderr}", posted.returncode == 0)
            result = json.loads(posted.stdout)
            assert_true("addressed post is recorded", result["recorded"] is True)
            assert_true("addressed delivery status", result["controller_delivery"]["status"] == "delivered_to_controller")


def test_cli_worker_result_blocked_ack_without_to_controller_still_succeed() -> None:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        messages_dir = base / "messages"
        fleet_dir = base / "fleet"
        for msg_type, dispatch_id in (
            ("result", "worker-result"),
            ("blocked", "worker-blocked"),
            ("ack", "worker-ack"),
        ):
            posted = run_messages_cli(
                messages_dir,
                fleet_dir,
                [
                    "post",
                    "--dispatch-id",
                    dispatch_id,
                    "--type",
                    msg_type,
                    "--text",
                    f"{msg_type} from worker",
                    "--json",
                ],
            )
            assert_true(f"{msg_type} worker post succeeds: {posted.stderr}", posted.returncode == 0)
            result = json.loads(posted.stdout)
            assert_true(f"{msg_type} is recorded", result["recorded"] is True)
            assert_true(f"{msg_type} wrote a carrier", (messages_dir / f"{dispatch_id}.jsonl").is_file())


def test_bounded_relay_sanitizes_c1_csi() -> None:
    from goalflight_messages import format_bounded_relay

    rendered = format_bounded_relay(
        [
            {
                "dispatch_id": "worker-bounded",
                "type": "blocked",
                "text": "needs \x9b31mreview",
            }
        ]
    )

    assert_true(
        "bounded relay escapes C1 CSI",
        rendered == r"[worker-bounded] blocked: needs \x9b31mreview",
    )
    assert_true("bounded relay has no raw C1 CSI", "\x9b" not in (rendered or ""))


def main() -> None:
    tests = sorted(
        [
            value
            for name, value in globals().items()
            if name.startswith("test_") and callable(value)
        ],
        key=lambda test: test.__name__,
    )
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
