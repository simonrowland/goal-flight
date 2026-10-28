#!/usr/bin/env python3
"""Tests for marker → envelope conversion."""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import sqlite3
import os
import select
import subprocess
import sys
import tempfile
import traceback
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
            heartbeat_before_drain = lease.renewed_at
            before_drain = authority.cursor_peek(label, nonce=lease.nonce, limit=1000)

            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                drained_rc = _carrier_messages.main(argv)
            after_drain = authority.cursor_peek(label, nonce=lease.nonce, limit=1000)
            heartbeat_after_drain = authority.active_lease(label).renewed_at
            drained_lines = stdout.getvalue().splitlines()
            assert_true("drain backlog succeeds", drained_rc == 0)
            assert_true(
                "successful drain refreshes controller heartbeat",
                heartbeat_after_drain != heartbeat_before_drain,
            )
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
            heartbeat_before_empty_drain = heartbeat_after_drain
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                empty_rc = _carrier_messages.main(argv)
            heartbeat_after_empty_drain = authority.active_lease(label).renewed_at
            assert_true("empty drain succeeds", empty_rc == 0)
            assert_true(
                "empty drain refreshes controller heartbeat",
                heartbeat_after_empty_drain != heartbeat_before_empty_drain,
            )
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
        assert_true("ambiguous drain writes one line", len(stderr.splitlines()) == 1)
        assert_true("ambiguous drain uses NOT RECORDED", stderr.startswith("NOT RECORDED:"))
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


def test_relay_foreign_cwd_fails_loudly() -> None:
    """An unresolved journal must not look like an empty mailbox."""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        foreign = base / "foreign"
        foreign.mkdir()
        env = _journal_test_env(base)
        messages_dir = Path(env["GOALFLIGHT_MESSAGES_DIR"])
        fleet_dir = Path(env["GOALFLIGHT_FLEET_DIR"])

        with mock.patch.dict(os.environ, env, clear=False):
            for name in (
                "GOALFLIGHT_CONTROLLER_LABEL",
                "GOALFLIGHT_CONTROLLER_LEASE_NONCE",
                "GOALFLIGHT_CONTROLLER_SESSION_ID",
                "GOALFLIGHT_DISPATCH_ID",
            ):
                os.environ.pop(name, None)
            unresolved = run_messages_cli(
                messages_dir,
                fleet_dir,
                ["relay", "--new", "--json"],
                cwd=foreign,
            )
            assert_true("foreign-cwd relay exits nonzero", unresolved.returncode != 0)
            assert_true("foreign-cwd relay writes no empty result", unresolved.stdout == "")
            assert_true(
                "foreign-cwd relay names the unresolved journal",
                "relay: journal database is absent:" in unresolved.stderr,
            )


def test_relay_explicit_address_succeeds_from_foreign_cwd() -> None:
    """Explicit address wins over both cwd and ambient controller identity."""
    import goalflight_journal

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        project = base / "project"
        foreign = base / "foreign"
        init_git_project(project)
        foreign.mkdir()
        env = _journal_test_env(base)
        messages_dir = Path(env["GOALFLIGHT_MESSAGES_DIR"])
        fleet_dir = Path(env["GOALFLIGHT_FLEET_DIR"])
        label = "foreign-cwd-controller"

        with mock.patch.dict(os.environ, env, clear=False):
            authority = goalflight_journal.open_or_create_journal(project)
            claimed = authority.claim_or_renew_lease(
                label,
                principal={"principal_id": "foreign-cwd-test"},
            )
            assert_true("foreign-cwd controller lease claimed", claimed.committed)
            other = authority.claim_or_renew_lease(
                "other-controller",
                principal={"principal_id": "foreign-cwd-other-test"},
            )
            assert_true("second controller makes label selection material", other.committed)
            _post_journal_controller_mail(
                project=project,
                messages_dir=messages_dir,
                label=label,
                dispatch_id="foreign-cwd-mail",
            )
            os.environ["GOALFLIGHT_CONTROLLER_LABEL"] = "other-controller"
            explicit = run_messages_cli(
                messages_dir,
                fleet_dir,
                [
                    "relay",
                    "--new",
                    "--json",
                    "--project-root",
                    str(project),
                    "--controller-label",
                    label,
                ],
                cwd=foreign,
            )
        assert_true(
            f"explicit foreign-cwd relay succeeds: {explicit.stderr}",
            explicit.returncode == 0,
        )
        payload = json.loads(explicit.stdout)
        assert_true(
            "explicit relay selects the requested controller",
            payload["controller_label"] == label,
        )
        assert_true(
            "explicit relay retrieves the pending envelope",
            [item["dispatch_id"] for item in payload["items"]] == ["foreign-cwd-mail"],
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
            cas_ready = threading.Event()
            release_cas = threading.Event()
            captured: dict[str, object] = {}
            original_peek = goalflight_journal.Journal.cursor_peek
            original_advance = goalflight_journal.Journal.advance_cursor
            peek_calls = 0
            peek_thread_names: list[str] = []
            advance_calls = 0
            advance_thread_names: list[str] = []

            def blocked_peek(self, *args, **kwargs):
                nonlocal peek_calls
                peek_calls += 1
                peek_thread_names.append(threading.current_thread().name)
                snapshot = original_peek(self, *args, **kwargs)
                if threading.current_thread().name == "drain-race-cli":
                    captured["snapshot"] = snapshot
                    snapshot_ready.set()
                    assert release_snapshot.wait(5), "drain race snapshot was not released"
                return snapshot

            def blocked_advance(self, *args, **kwargs):
                nonlocal advance_calls
                advance_calls += 1
                advance_thread_names.append(threading.current_thread().name)
                if threading.current_thread().name == "drain-race-cli":
                    cas_ready.set()
                    assert release_cas.wait(5), "drain race CAS was not released"
                return original_advance(self, *args, **kwargs)

            stdout = io.StringIO()
            stderr = io.StringIO()
            outcome: dict[str, object] = {"return": None, "exception": None}

            def drain() -> None:
                try:
                    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                        outcome["return"] = _carrier_messages.main(argv)
                except BaseException:
                    outcome["exception"] = traceback.format_exc()

            with (
                mock.patch.object(goalflight_journal.Journal, "cursor_peek", blocked_peek),
                mock.patch.object(
                    goalflight_journal.Journal,
                    "advance_cursor",
                    blocked_advance,
                ),
            ):
                worker = threading.Thread(target=drain, name="drain-race-cli")
                worker.start()
                assert_true("drain captured its snapshot", snapshot_ready.wait(5))
                snapshot = captured["snapshot"]
                positions = _carrier_messages._cursor_positions(snapshot.items)
                release_snapshot.set()
                assert_true("drain reached its CAS", cas_ready.wait(5))
                advanced = authority.advance_cursor(
                    label,
                    nonce=lease.nonce,
                    expected_cursor_version=snapshot.cursor_version,
                    expected_stream_snapshots=snapshot.stream_snapshots,
                    advances=positions,
                    actor="concurrent-test-controller",
                )
                assert_true("concurrent cursor advance committed", advanced.committed)
                release_cas.set()
                # Bounded: a hung drain must fail this test, never hang the suite.
                worker.join(timeout=60)
                assert_true("drain race thread finished", not worker.is_alive())
                assert_true(
                    "drain race completed with exactly one CAS loss; "
                    f"return={outcome['return']!r}; "
                    f"exception={outcome['exception']!r}; "
                    f"blocked_peek_entered={peek_calls > 0} calls={peek_calls} "
                    f"threads={peek_thread_names!r}; "
                    f"blocked_advance_entered={advance_calls > 0} calls={advance_calls} "
                    f"threads={advance_thread_names!r}",
                    outcome["return"] == 3
                    and outcome["exception"] is None
                    and advance_calls == 2
                    and advance_thread_names == ["drain-race-cli", "MainThread"],
                )

            combined_lines = [
                line
                for line in (stdout.getvalue() + stderr.getvalue()).splitlines()
                if line
            ]
            assert_true("drain race exits CAS-lost", outcome["return"] == 3)
            assert_true(
                "drain race shows the attempted item before reporting CAS loss",
                combined_lines
                == [
                    "[controller-notice] drain-race seq=1 — mail for drain-race",
                    "drain conflict · retry relay --drain",
                ],
            )
            assert_true("racing drain bypasses wake-entry notices", wake_notice.call_count == 0)
            after = authority.cursor_peek(label, nonce=lease.nonce, limit=1000)
            assert_true(
                "drain race leaves the cursor at the competing advance",
                after.cursor_version == advanced.value["cursor_version"]
                and not after.items,
            )


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
    assert_true("top-level trap uses NOT RECORDED", lines[0].startswith("NOT RECORDED:"))
    assert_true(
        "top-level trap is actionable",
        "; next: python3 scripts/goalflight_messages.py relay --new --json" in lines[0],
    )
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


def test_follow_wait_timeout_respects_heartbeat_deadline() -> None:
    delay = _carrier_messages._follow_wait_timeout(
        _carrier_messages.WAKE_NOTIFY_SAFETY_POLL_S,
        now=10.0,
        next_heartbeat=130.0,
    )
    assert_true(
        "notify wait wakes for heartbeat",
        delay == min(_carrier_messages.WAKE_NOTIFY_SAFETY_POLL_S, 120.0),
    )


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


def test_read_resolves_explicit_controller_context_like_relay(monkeypatch) -> None:
    import types

    root = Path.cwd() / "read-controller-project"
    authority = mock.Mock()
    authority.active_lease.return_value = object()
    journal_module = types.SimpleNamespace(
        Journal=types.SimpleNamespace(open_reader=mock.Mock(return_value=authority)),
        JournalBusy=RuntimeError,
        JournalDisappeared=RuntimeError,
        JournalIOError=RuntimeError,
    )
    task_module = types.SimpleNamespace(
        resolve_project_root=mock.Mock(return_value=root)
    )
    monkeypatch.setitem(sys.modules, "goalflight_journal", journal_module)
    monkeypatch.setitem(sys.modules, "goalflight_task", task_module)
    monkeypatch.setattr(_carrier_messages, "_require_relay_controller_label", lambda auth, label: "main")
    monkeypatch.setattr(_carrier_messages, "collect_inbox_paths", lambda *args, **kwargs: [])
    monkeypatch.setattr(_carrier_messages, "logical_envelopes_for_paths", lambda *args, **kwargs: [])
    monkeypatch.setattr(_carrier_messages, "_current_project_root", lambda: None)
    monkeypatch.setattr(_carrier_messages, "_explicit_controller_label", lambda: None)

    args = mock.Mock(
        project_root=str(root),
        controller_label="main",
        dispatch_id="worker-1",
        messages_dir=root / "messages",
        fleet_dir=root / "fleet",
        last=None,
        json=True,
    )
    with contextlib.redirect_stdout(io.StringIO()) as output:
        assert _carrier_messages.cmd_read(args) == 0
    task_module.resolve_project_root.assert_called_once_with(str(root))
    journal_module.Journal.open_reader.assert_called_once_with(root)
    authority.active_lease.assert_called_once_with("main")
    assert output.getvalue().strip() == "[]"


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


def test_corrupt_carrier_is_reported_once_while_relay_delivers_healthy_mail() -> None:
    import goalflight_journal

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        project = base / "project"
        init_git_project(project)
        env = _journal_test_env(base)
        label = "carrier-isolation-controller"
        messages_dir = Path(env["GOALFLIGHT_MESSAGES_DIR"])
        with mock.patch.dict(
            os.environ,
            {**env, "GOALFLIGHT_CONTROLLER_LABEL": label},
            clear=False,
        ), mock.patch.object(_carrier_messages, "_current_project_root", return_value=project):
            authority = goalflight_journal.open_or_create_journal(project)
            claimed = authority.claim_or_renew_lease(
                label,
                principal={"principal_id": "carrier-isolation-test"},
            )
            assert_true("carrier isolation lease claimed", claimed.committed)
            _post_journal_controller_mail(
                project=project,
                messages_dir=messages_dir,
                label=label,
                dispatch_id="healthy-carrier",
            )
            bad = _carrier_messages.post_message(
                dispatch_id="corrupt-carrier",
                msg_type="controller-notice",
                payload={"text": "must be skipped"},
                messages_dir=messages_dir,
                source={"node": "test", "adapter": "pytest", "transport": "controller"},
                addressee=_carrier_messages.controller_addressee(
                    label,
                    project_root=project,
                ),
            )
            with Path(bad["path"]).open("ab") as handle:
                handle.write(b"{corrupt carrier row\n")

            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                rc = _carrier_messages.main(
                    [
                        "--messages-dir",
                        str(messages_dir),
                        "--fleet-dir",
                        env["GOALFLIGHT_FLEET_DIR"],
                        "relay",
                        "--new",
                        "--json",
                    ]
                )
            payload = json.loads(stdout.getvalue())
            assert_true("relay survives corrupt carrier", rc == 0)
            assert_true(
                "healthy mail remains visible",
                any(item.get("dispatch_id") == "healthy-carrier" for item in payload["items"]),
            )
            assert_true(
                "readable assignment remains visible despite sibling corruption",
                any(item.get("dispatch_id") == "corrupt-carrier" for item in payload["items"]),
            )
            assert_true(
                "corrupt carrier is reported once",
                len(payload.get("carrier_errors") or []) == 1
                and payload["carrier_errors"][0]["carrier_path"].endswith(
                    "corrupt-carrier.jsonl"
                ),
            )
            assignments = authority.read_all(
                "SELECT withdrawn_at FROM delivery_events WHERE event_uuid = ?",
                (bad["envelope"]["id"],),
            )
            assert_true(
                "readable row assignment remains live",
                assignments and assignments[0]["withdrawn_at"] is None,
            )
            assert_true(
                "corruption warning is one line",
                stderr.getvalue().count("WARNING: carrier corruption:") == 1,
            )


def test_drain_corrupt_assigned_carrier_stays_pending() -> None:
    import goalflight_journal

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        project = base / "project"
        init_git_project(project)
        env = _journal_test_env(base)
        label = "corrupt-drain-controller"
        messages_dir = Path(env["GOALFLIGHT_MESSAGES_DIR"])
        with mock.patch.dict(
            os.environ,
            {**env, "GOALFLIGHT_CONTROLLER_LABEL": label},
            clear=False,
        ), mock.patch.object(_carrier_messages, "_current_project_root", return_value=project):
            authority = goalflight_journal.open_or_create_journal(project)
            claimed = authority.claim_or_renew_lease(
                label,
                principal={"principal_id": "corrupt-drain-test"},
            )
            assert_true("corrupt drain lease claimed", claimed.committed)
            bad = _carrier_messages.post_message(
                dispatch_id="corrupt-drain",
                msg_type="controller-notice",
                payload={"text": "must remain pending"},
                messages_dir=messages_dir,
                source={"node": "test", "adapter": "pytest", "transport": "controller"},
                addressee=_carrier_messages.controller_addressee(
                    label,
                    project_root=project,
                ),
            )
            same_stream_later = _carrier_messages.post_message(
                dispatch_id="corrupt-drain",
                msg_type="controller-notice",
                payload={"text": "must remain behind the stuck row"},
                messages_dir=messages_dir,
                source={"node": "test", "adapter": "pytest", "transport": "controller"},
                addressee=_carrier_messages.controller_addressee(
                    label,
                    project_root=project,
                ),
            )
            healthy = _carrier_messages.post_message(
                dispatch_id="healthy-drain-stream",
                msg_type="controller-notice",
                payload={"text": "healthy stream still drains"},
                messages_dir=messages_dir,
                source={"node": "test", "adapter": "pytest", "transport": "controller"},
                addressee=_carrier_messages.controller_addressee(
                    label,
                    project_root=project,
                ),
            )
            carrier = Path(bad["path"])
            carrier_lines = carrier.read_text(encoding="utf-8").splitlines()
            carrier.write_text(
                "{corrupt carrier row\n" + carrier_lines[1] + "\n",
                encoding="utf-8",
            )

            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                rc = _carrier_messages.main(
                    [
                        "--messages-dir",
                        str(messages_dir),
                        "--fleet-dir",
                        env["GOALFLIGHT_FLEET_DIR"],
                        "relay",
                        "--drain",
                        "--json",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            assert_true("healthy stream drains beside corrupt stream", rc == 0)
            assert_true("corrupt drain reports committed prefix", payload["status"] == "drained")
            assert_true("drained count contains only committed prefix", payload["drained"] == 1)
            assert_true(
                "drain items contain only committed prefix",
                [item["id"] for item in payload["items"]] == [healthy["envelope"]["id"]],
            )
            assert_true(
                "same-stream followup is pending behind stuck row",
                payload["pending_behind_stuck"]
                == [
                    {
                        "id": same_stream_later["envelope"]["id"],
                        "stream_id": "corrupt-drain",
                        "stream_seq": 2,
                        "type": "controller-notice",
                    }
                ],
            )
            assert_true(
                "stuck row is separately reported with its reason",
                len(payload["stuck"]) == 1
                and payload["stuck"][0]["delivery_id"] == bad["envelope"]["id"]
                and payload["stuck"][0]["stream_seq"] == 1
                and "invalid JSON" in payload["stuck"][0]["reason"],
            )
            errors = payload.get("carrier_errors") or []
            assert_true(
                f"corrupt drain reports one stuck delivery: {errors!r}",
                len(errors) == 1,
            )
            stuck = errors[0]
            assert_true("stuck status is explicit", stuck.get("status") == "STUCK")
            assert_true(
                "stuck report names delivery",
                stuck.get("delivery_id") == bad["envelope"]["id"],
            )
            assert_true("stuck report names carrier", str(carrier) in stuck["error"])
            assert_true("stuck report names reason", "invalid JSON" in stuck["reason"])
            assert_true("quarantine is recorded", _carrier_messages.quarantine_path(carrier).is_file())
            assignment = authority.read_all(
                "SELECT projected_at, withdrawn_at FROM delivery_events WHERE event_uuid = ?",
                (bad["envelope"]["id"],),
            )
            assert_true(
                "corrupt delivery remains pending",
                assignment
                and assignment[0]["projected_at"] is not None
                and assignment[0]["withdrawn_at"] is None,
            )
            lease = authority.active_lease(label)
            assert_true("corrupt drain lease remains readable", lease is not None)
            assert_true(
                "corrupt drain cursor remains pending",
                authority.cursor_peek(label, nonce=lease.nonce).items,
            )
            assert_true("stuck warning is emitted", "STUCK:" in stderr.getvalue())

            retry_stdout = io.StringIO()
            with contextlib.redirect_stdout(retry_stdout):
                retry_rc = _carrier_messages.main(
                    [
                        "--messages-dir",
                        str(messages_dir),
                        "--fleet-dir",
                        env["GOALFLIGHT_FLEET_DIR"],
                        "relay",
                        "--drain",
                        "--json",
                    ]
                )
            retry_payload = json.loads(retry_stdout.getvalue())
            retry_errors = retry_payload.get("carrier_errors") or []
            assert_true("stuck retry remains blocked", retry_rc == 3)
            assert_true(
                "stuck retry remains visible with its reason",
                retry_payload["status"] == "blocked"
                and len(retry_errors) == 1
                and retry_errors[0].get("status") == "STUCK"
                and "invalid JSON" in retry_errors[0].get("reason", ""),
            )
            assert_true(
                "stuck retry does not present the later row as drained",
                retry_payload["drained"] == 0
                and retry_payload["items"] == []
                and retry_payload["pending_behind_stuck"][0]["id"]
                == same_stream_later["envelope"]["id"],
            )

            # A valid envelope for the same carrier can still be the wrong
            # journal event. It is stuck mail, not a reason to withdraw the
            # assignment or advance its stream.
            mismatched = _carrier_messages.markers_to_envelopes(
                {"STATUS": ["valid but wrong event"]},
                dispatch_id="corrupt-drain",
                source={"node": "test", "adapter": "pytest", "transport": "controller"},
            )[0]
            carrier.write_text(
                json.dumps(mismatched, ensure_ascii=False, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                rc = _carrier_messages.main(
                    [
                        "--messages-dir",
                        str(messages_dir),
                        "--fleet-dir",
                        env["GOALFLIGHT_FLEET_DIR"],
                        "relay",
                        "--drain",
                        "--json",
                    ]
                )
            mismatched_result = json.loads(stdout.getvalue())
            assert_true("mismatched drain reports blocked", rc == 3)
            assert_true("mismatched drain status is blocked", mismatched_result["status"] == "blocked")
            assert_true("mismatched drain reports STUCK", "STUCK:" in stderr.getvalue())
            assignment = authority.read_all(
                "SELECT projected_at, withdrawn_at FROM delivery_events WHERE event_uuid = ?",
                (bad["envelope"]["id"],),
            )
            assert_true(
                "mismatched delivery remains pending",
                assignment
                and assignment[0]["projected_at"] is not None
                and assignment[0]["withdrawn_at"] is None,
            )
            assert_true(
                "mismatched drain leaves unreportable row pending",
                bool(authority.cursor_peek(label, nonce=lease.nonce).items),
            )

            _carrier_messages.post_message(
                dispatch_id="healthy-drain-conflict",
                msg_type="controller-notice",
                payload={"text": "visible while another carrier is corrupt"},
                messages_dir=messages_dir,
                source={"node": "test", "adapter": "pytest", "transport": "controller"},
                addressee=_carrier_messages.controller_addressee(
                    label,
                    project_root=project,
                ),
            )

            def lose_conflict_cas(*_args, **_kwargs):
                raise goalflight_journal.CASMismatch("cursor moved")

            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                mock.patch.object(
                    goalflight_journal.Journal,
                    "advance_cursor",
                    lose_conflict_cas,
                ),
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                conflict_rc = _carrier_messages.main(
                    [
                        "--messages-dir",
                        str(messages_dir),
                        "--fleet-dir",
                        env["GOALFLIGHT_FLEET_DIR"],
                        "relay",
                        "--drain",
                        "--json",
                    ]
                )
            conflict = json.loads(stdout.getvalue())
            assert_true("conflicted corrupt drain returns CAS loss", conflict_rc == 3)
            assert_true("conflicted drain reports conflict", conflict["status"] == "conflict")
            assert_true(
                f"conflicted drain retains the stuck delivery: {conflict!r}",
                any(
                    item.get("delivery_id") == bad["envelope"]["id"]
                    and item.get("status") == "STUCK"
                    for item in conflict["carrier_errors"]
                ),
            )


def test_drain_snapshot_is_the_safe_prefix_and_retries_after_carrier_repair() -> None:
    import goalflight_journal

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        project = base / "project"
        init_git_project(project)
        env = _journal_test_env(base)
        label = "prefix-snapshot-controller"
        messages_dir = Path(env["GOALFLIGHT_MESSAGES_DIR"])
        with mock.patch.dict(
            os.environ,
            {**env, "GOALFLIGHT_CONTROLLER_LABEL": label},
            clear=False,
        ):
            authority = goalflight_journal.open_or_create_journal(project)
            claimed = authority.claim_or_renew_lease(
                label,
                principal={"principal_id": "prefix-snapshot-test"},
            )
            assert_true("prefix snapshot lease claimed", claimed.committed)
            addressee = _carrier_messages.controller_addressee(
                label,
                project_root=project,
            )

            def post(dispatch_id: str, text: str):
                return _carrier_messages.post_message(
                    dispatch_id=dispatch_id,
                    msg_type="controller-notice",
                    payload={"text": text},
                    messages_dir=messages_dir,
                    source={"node": "test", "adapter": "pytest", "transport": "controller"},
                    addressee=addressee,
                )

            prefix = post("prefix-stream", "readable prefix")
            stuck = post("prefix-stream", "stuck row")
            later = post("prefix-stream", "readable successor")
            sibling = post("healthy-prefix-sibling", "healthy sibling")
            carrier = Path(stuck["path"])
            original_lines = carrier.read_text(encoding="utf-8").splitlines()
            assert len(original_lines) == 3
            carrier.write_text(
                original_lines[0] + "\n{invalid row\n" + original_lines[2] + "\n",
                encoding="utf-8",
            )

            expected_prefix_token, _ = authority._read_with_retry(
                "test selected cursor prefix",
                lambda connection: authority._cursor_stream_snapshot(
                    connection,
                    project_root=str(project.resolve()),
                    recipient_label=label,
                    stream_id="prefix-stream",
                    requested_position=1,
                ),
            )

        view = run_messages_cli(
            messages_dir,
            Path(env["GOALFLIGHT_FLEET_DIR"]),
            [
                "relay",
                "--project-root",
                str(project),
                "--controller-label",
                label,
                "--json",
            ],
        )
        view_payload = json.loads(view.stdout)
        assert view.returncode == 0, (view.stdout, view.stderr)
        assert view_payload["positions"] == {
            "prefix-stream": 1,
            "healthy-prefix-sibling": 1,
        }
        assert view_payload["stream_snapshots"]["prefix-stream"] == expected_prefix_token

        drain = run_messages_cli(
            messages_dir,
            Path(env["GOALFLIGHT_FLEET_DIR"]),
            [
                "relay",
                "--project-root",
                str(project),
                "--controller-label",
                label,
                "--drain",
                "--json",
            ],
        )
        payload = json.loads(drain.stdout)
        assert drain.returncode == 0, (drain.stdout, drain.stderr)
        assert payload["drained"] == 2
        assert {item["id"] for item in payload["items"]} == {
            prefix["envelope"]["id"],
            sibling["envelope"]["id"],
        }
        assert payload["stuck"][0]["delivery_id"] == stuck["envelope"]["id"]
        assert payload["stuck"][0]["stream_seq"] == 2
        assert payload["pending_behind_stuck"][0]["id"] == later["envelope"]["id"]
        assert authority.cursor_status(label)["positions"] == {
            "prefix-stream": 1,
            "healthy-prefix-sibling": 1,
        }

        carrier.write_text("\n".join(original_lines) + "\n", encoding="utf-8")
        repaired = run_messages_cli(
            messages_dir,
            Path(env["GOALFLIGHT_FLEET_DIR"]),
            [
                "relay",
                "--project-root",
                str(project),
                "--controller-label",
                label,
                "--drain",
                "--json",
            ],
        )
        repaired_payload = json.loads(repaired.stdout)
        assert repaired.returncode == 0, (repaired.stdout, repaired.stderr)
        assert repaired_payload["drained"] == 2
        assert [item["id"] for item in repaired_payload["items"]] == [
            stuck["envelope"]["id"],
            later["envelope"]["id"],
        ]
        assert not authority.cursor_peek(
            label,
            nonce=authority.active_lease(label).nonce,
        ).items


def test_relay_new_hides_later_result_until_stuck_head_is_repaired() -> None:
    import goalflight_journal

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        project = base / "project"
        init_git_project(project)
        env = _journal_test_env(base)
        label = "safe-relay-controller"
        messages_dir = Path(env["GOALFLIGHT_MESSAGES_DIR"])
        with mock.patch.dict(
            os.environ,
            {**env, "GOALFLIGHT_CONTROLLER_LABEL": label},
            clear=False,
        ):
            authority = goalflight_journal.open_or_create_journal(project)
            claimed = authority.claim_or_renew_lease(
                label,
                principal={"principal_id": "safe-relay-test"},
            )
            assert_true("safe relay lease claimed", claimed.committed)
            addressee = _carrier_messages.controller_addressee(
                label,
                project_root=project,
            )

            def post(msg_type: str, text: str):
                return _carrier_messages.post_message(
                    dispatch_id="safe-relay-stream",
                    msg_type=msg_type,
                    payload={"complete": msg_type == "result", "text": text},
                    messages_dir=messages_dir,
                    source={"node": "test", "adapter": "pytest", "transport": "controller"},
                    addressee=addressee,
                )

            stuck = post("controller-notice", "unreadable head")
            result = post("result", "terminal result behind the head")
            carrier = Path(stuck["path"])
            original_lines = carrier.read_text(encoding="utf-8").splitlines()
            assert len(original_lines) == 2
            carrier.write_text("{invalid stuck head\n" + original_lines[1] + "\n", encoding="utf-8")

        relay = run_messages_cli(
            messages_dir,
            Path(env["GOALFLIGHT_FLEET_DIR"]),
            [
                "relay",
                "--project-root",
                str(project),
                "--controller-label",
                label,
                "--new",
                "--json",
            ],
        )
        payload = json.loads(relay.stdout)
        assert relay.returncode == 0, (relay.stdout, relay.stderr)
        assert payload["items"] == []
        assert payload["stuck"][0]["delivery_id"] == stuck["envelope"]["id"]
        assert payload["pending_behind_stuck"] == [
            {
                "id": result["envelope"]["id"],
                "stream_id": "safe-relay-stream",
                "stream_seq": 2,
                "type": "result",
            }
        ]

        carrier.write_text("\n".join(original_lines) + "\n", encoding="utf-8")
        repaired = run_messages_cli(
            messages_dir,
            Path(env["GOALFLIGHT_FLEET_DIR"]),
            [
                "relay",
                "--project-root",
                str(project),
                "--controller-label",
                label,
                "--new",
                "--json",
            ],
        )
        repaired_payload = json.loads(repaired.stdout)
        assert repaired.returncode == 0, (repaired.stdout, repaired.stderr)
        results = [item for item in repaired_payload["items"] if item["type"] == "result"]
        assert [item["id"] for item in results] == [result["envelope"]["id"]]


def test_relay_new_treats_unprojected_head_as_barrier_until_projection() -> None:
    import goalflight_journal

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        project = base / "project"
        init_git_project(project)
        env = _journal_test_env(base)
        label = "unprojected-relay-controller"
        messages_dir = Path(env["GOALFLIGHT_MESSAGES_DIR"])
        with mock.patch.dict(
            os.environ,
            {**env, "GOALFLIGHT_CONTROLLER_LABEL": label},
            clear=False,
        ):
            authority = goalflight_journal.open_or_create_journal(project)
            claimed = authority.claim_or_renew_lease(
                label,
                principal={"principal_id": "unprojected-relay-test"},
            )
            assert_true("unprojected relay lease claimed", claimed.committed)
            addressee = _carrier_messages.controller_addressee(
                label,
                project_root=project,
            )

            def post(msg_type: str, text: str):
                return _carrier_messages.post_message(
                    dispatch_id="unprojected-relay-stream",
                    msg_type=msg_type,
                    payload={"complete": msg_type == "result", "text": text},
                    messages_dir=messages_dir,
                    source={"node": "test", "adapter": "pytest", "transport": "controller"},
                    addressee=addressee,
                )

            first = post("controller-notice", "unprojected head")
            second = post("result", "terminal behind unprojected head")
            projected_at = authority.read_all(
                "SELECT projected_at FROM delivery_events WHERE event_uuid = ?",
                (first["envelope"]["id"],),
            )[0]["projected_at"]
            connection = authority._connect()
            try:
                connection.execute(
                    "UPDATE delivery_events SET projected_at = NULL WHERE event_uuid = ?",
                    (first["envelope"]["id"],),
                )
            finally:
                connection.close()

        command = [
            "relay", "--project-root", str(project), "--controller-label", label,
            "--new", "--json",
        ]
        blocked = run_messages_cli(
            messages_dir, Path(env["GOALFLIGHT_FLEET_DIR"]), command
        )
        payload = json.loads(blocked.stdout)
        assert blocked.returncode == 0, (blocked.stdout, blocked.stderr)
        assert payload["items"] == []
        assert payload["stuck"][0]["delivery_id"] == first["envelope"]["id"]
        assert payload["pending_behind_stuck"] == [
            {
                "id": second["envelope"]["id"],
                "stream_id": "unprojected-relay-stream",
                "stream_seq": 2,
                "type": "result",
            }
        ]

        connection = authority._connect()
        try:
            connection.execute(
                "UPDATE delivery_events SET projected_at = ? WHERE event_uuid = ?",
                (projected_at, first["envelope"]["id"]),
            )
        finally:
            connection.close()
        recovered = run_messages_cli(
            messages_dir, Path(env["GOALFLIGHT_FLEET_DIR"]), command
        )
        recovered_payload = json.loads(recovered.stdout)
        assert recovered.returncode == 0, (recovered.stdout, recovered.stderr)
        assert [item["id"] for item in recovered_payload["items"]] == [
            first["envelope"]["id"], second["envelope"]["id"]
        ]


def test_duplicate_carrier_sequence_is_quarantined_without_hiding_valid_rows() -> None:
    with tempfile.TemporaryDirectory() as td:
        messages_dir = Path(td) / "messages"
        posted = _carrier_messages.post_message(
            dispatch_id="duplicate-seq-carrier",
            msg_type="status",
            payload={"text": "valid"},
            messages_dir=messages_dir,
            project_journal_delivery=False,
        )
        path = Path(posted["path"])
        with path.open("a", encoding="utf-8") as handle:
            handle.write(posted["line"])
        errors: list[dict[str, object]] = []
        loaded = _carrier_messages.read_envelopes_tolerant(path, carrier_errors=errors)
        assert_true("duplicate leaves the first row", len(loaded) == 1)
        assert_true(
            "duplicate sequence is named",
            len(errors) == 1 and "duplicate sequence number" in str(errors[0]["reason"]),
        )


def test_post_refuses_case_variant_dispatch_id_before_writing() -> None:
    with tempfile.TemporaryDirectory() as td:
        messages_dir = Path(td) / "messages"
        _carrier_messages.post_message(
            dispatch_id="Case-Variant-Carrier",
            msg_type="status",
            payload={"text": "existing"},
            messages_dir=messages_dir,
            project_journal_delivery=False,
        )
        before = {
            entry.name: entry.read_bytes()
            for entry in messages_dir.iterdir()
            if entry.is_file()
        }
        try:
            _carrier_messages.post_message(
                dispatch_id="case-variant-carrier",
                msg_type="status",
                payload={"text": "must refuse"},
                messages_dir=messages_dir,
                project_journal_delivery=False,
            )
            assert_true("case collision is refused", False)
        except _carrier_messages.MessageError as exc:
            detail = str(exc)
            assert_true("requested id is named", "case-variant-carrier" in detail)
            assert_true("existing id is named", "Case-Variant-Carrier" in detail)
        after = {
            entry.name: entry.read_bytes()
            for entry in messages_dir.iterdir()
            if entry.is_file()
        }
        assert_true("case collision writes nothing", after == before)


def test_post_refuses_placeholder_stream_ids_and_accepts_ordinary_ids() -> None:
    with tempfile.TemporaryDirectory() as td:
        messages_dir = Path(td) / "messages"
        for dispatch_id in ("None", "none", "null", "NULL", "undefined", "", " ", "\t"):
            try:
                _carrier_messages.post_message(
                    dispatch_id=dispatch_id,
                    msg_type="status",
                    payload={"text": "must refuse"},
                    messages_dir=messages_dir,
                    project_journal_delivery=False,
                )
                assert_true(f"placeholder stream id {dispatch_id!r} is refused", False)
            except _carrier_messages.MessageError as exc:
                assert_true(
                    f"placeholder stream id {dispatch_id!r} explains the fix",
                    "dispatch id is unset; pass a real stream id" in str(exc),
                )
        assert_true("placeholder posts create no message directory", not messages_dir.exists())

        ordinary = _carrier_messages.post_message(
            dispatch_id="ordinary-stream",
            msg_type="status",
            payload={"text": "accepted"},
            messages_dir=messages_dir,
            project_journal_delivery=False,
        )
        assert_true("ordinary stream id remains accepted", Path(ordinary["path"]).is_file())

        legacy = dict(ordinary["envelope"])
        legacy["dispatch_id"] = "None"
        legacy_path = messages_dir / "None.jsonl"
        legacy_path.write_text(
            _carrier_messages.serialize_envelope_line(legacy),
            encoding="utf-8",
        )
        loaded = _carrier_messages.read_envelopes(legacy_path)
        assert_true(
            "legacy placeholder stream remains readable",
            len(loaded) == 1 and loaded[0]["dispatch_id"] == "None",
        )


def test_concurrent_case_variant_posts_have_one_winner() -> None:
    with tempfile.TemporaryDirectory() as td:
        messages_dir = Path(td) / "messages"
        original_admit_stream_seq = _carrier_messages._admit_stream_seq
        admit_barrier = threading.Barrier(2)
        successes: list[dict] = []
        failures: list[BaseException] = []

        def synchronized_admit(*, provided_seq: int | None, envelopes: list[dict]) -> int:
            try:
                admit_barrier.wait(timeout=1)
            except threading.BrokenBarrierError:
                pass
            return original_admit_stream_seq(
                provided_seq=provided_seq,
                envelopes=envelopes,
            )

        def post(dispatch_id: str) -> None:
            try:
                successes.append(
                    _carrier_messages.post_message(
                        dispatch_id=dispatch_id,
                        msg_type="status",
                        payload={"text": dispatch_id},
                        messages_dir=messages_dir,
                        project_journal_delivery=False,
                    )
                )
            except BaseException as exc:  # noqa: BLE001 - asserted below
                failures.append(exc)

        _carrier_messages._admit_stream_seq = synchronized_admit  # type: ignore[assignment]
        first = threading.Thread(target=post, args=("CaseId",))
        second = threading.Thread(target=post, args=("caseid",))
        try:
            first.start()
            second.start()
            first.join(timeout=3)
            second.join(timeout=3)
        finally:
            _carrier_messages._admit_stream_seq = original_admit_stream_seq  # type: ignore[assignment]
            assert_true("case-variant post threads finish", not first.is_alive() and not second.is_alive())

        assert_true("exactly one case-variant post succeeds", len(successes) == 1)
        assert_true("exactly one case-variant post is refused", len(failures) == 1)
        assert_true("collision failure names both ids", "case" in str(failures[0]).lower())
        carriers = sorted(
            entry.name
            for entry in messages_dir.iterdir()
            if entry.name.endswith(".jsonl") and not entry.name.endswith(".quarantine.jsonl")
        )
        assert_true("case-variant race leaves one carrier", len(carriers) == 1)


def test_stream_owner_sidecar_adopts_legacy_and_refuses_cross_project_post() -> None:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        project_a = base / "project-a"
        project_b = base / "project-b"
        init_git_project(project_a)
        init_git_project(project_b)
        messages_dir = base / "messages"
        dispatch_id = "shared-stream"
        owner_path = messages_dir / f"{dispatch_id}.owner.json"

        with mock.patch.object(
            _carrier_messages, "_current_project_root", return_value=project_a.resolve()
        ):
            first = _carrier_messages.post_message(
                dispatch_id=dispatch_id,
                msg_type="status",
                payload={"text": "first project"},
                messages_dir=messages_dir,
                source={
                    "node": "test",
                    "adapter": "pytest",
                    "transport": "controller",
                    "controller_label": "project-a-controller",
                },
                project_journal_delivery=False,
            )
            _carrier_messages.post_message(
                dispatch_id=dispatch_id,
                msg_type="status",
                payload={"text": "same project"},
                messages_dir=messages_dir,
                project_journal_delivery=False,
            )

        owner = json.loads(owner_path.read_text(encoding="utf-8"))
        assert_true("owner sidecar stores canonical project", owner["project_root"] == str(project_a.resolve()))
        assert_true(
            "owner sidecar stores known controller label",
            owner["controller_label"] == "project-a-controller",
        )

        try:
            with mock.patch.object(
                _carrier_messages, "_current_project_root", return_value=project_b.resolve()
            ):
                _carrier_messages.post_message(
                    dispatch_id=dispatch_id,
                    msg_type="status",
                    payload={"text": "must refuse"},
                    messages_dir=messages_dir,
                    project_journal_delivery=False,
                )
            assert_true("cross-project post is refused", False)
        except _carrier_messages.MessageError as exc:
            detail = str(exc)
            assert_true("owner project is named", str(project_a.resolve()) in detail)
            assert_true("posting project is named", str(project_b.resolve()) in detail)
            assert_true("project-distinct id is suggested", "project-distinct" in detail)
        assert_true(
            "refused post does not append",
            len(_carrier_messages.read_envelopes(first["path"])) == 2,
        )

        # A pre-fix stream has no owner record. The first post after the fix
        # adopts it, even when earlier envelopes cannot identify their project.
        legacy_id = "legacy-stream"
        with mock.patch.object(
            _carrier_messages, "_current_project_root", return_value=project_a.resolve()
        ):
            _carrier_messages.post_message(
                dispatch_id=legacy_id,
                msg_type="status",
                payload={"text": "legacy record"},
                messages_dir=messages_dir,
                project_journal_delivery=False,
            )
        legacy_owner = messages_dir / f"{legacy_id}.owner.json"
        legacy_owner.unlink()
        with mock.patch.object(
            _carrier_messages, "_current_project_root", return_value=project_b.resolve()
        ):
            _carrier_messages.post_message(
                dispatch_id=legacy_id,
                msg_type="status",
                payload={"text": "first post after fix"},
                messages_dir=messages_dir,
                project_journal_delivery=False,
            )
        adopted = json.loads(legacy_owner.read_text(encoding="utf-8"))
        assert_true("legacy stream adopts first post project", adopted["project_root"] == str(project_b.resolve()))


def test_concurrent_first_posts_from_different_projects_have_one_winner() -> None:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        project_a = base / "project-a"
        project_b = base / "project-b"
        init_git_project(project_a)
        init_git_project(project_b)
        messages_dir = base / "messages"
        thread_project = threading.local()
        start_barrier = threading.Barrier(2)
        successes: list[dict] = []
        failures: list[BaseException] = []

        def post(project: Path) -> None:
            thread_project.root = project.resolve()
            try:
                start_barrier.wait(timeout=2)
                successes.append(
                    _carrier_messages.post_message(
                        dispatch_id="concurrent-shared-stream",
                        msg_type="status",
                        payload={"text": str(project)},
                        messages_dir=messages_dir,
                        project_journal_delivery=False,
                    )
                )
            except BaseException as exc:  # noqa: BLE001 - asserted below
                failures.append(exc)

        with mock.patch.object(
            _carrier_messages,
            "_current_project_root",
            side_effect=lambda: thread_project.root,
        ):
            threads = [
                threading.Thread(target=post, args=(project_a,)),
                threading.Thread(target=post, args=(project_b,)),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)

        assert_true("concurrent first-post threads finish", all(not thread.is_alive() for thread in threads))
        assert_true("one project wins initial ownership", len(successes) == 1)
        assert_true("other project is refused", len(failures) == 1)
        detail = str(failures[0])
        assert_true("race refusal names project A", str(project_a.resolve()) in detail)
        assert_true("race refusal names project B", str(project_b.resolve()) in detail)
        owner = json.loads(
            (messages_dir / "concurrent-shared-stream.owner.json").read_text(encoding="utf-8")
        )
        assert_true(
            "race owner is exactly one candidate project",
            owner["project_root"] in {str(project_a.resolve()), str(project_b.resolve())},
        )
        assert_true(
            "only winning first post is appended",
            len(_carrier_messages.read_envelopes(messages_dir / "concurrent-shared-stream.jsonl")) == 1,
        )


def test_fleet_wide_quota_stream_accepts_posts_from_multiple_projects() -> None:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        project_a = base / "project-a"
        project_b = base / "project-b"
        init_git_project(project_a)
        init_git_project(project_b)
        messages_dir = base / "messages"

        for project in (project_a, project_b):
            with mock.patch.object(
                _carrier_messages,
                "_current_project_root",
                return_value=project.resolve(),
            ):
                _carrier_messages.post_message(
                    dispatch_id="controller-quota-advisory",
                    msg_type="advisory",
                    payload={"text": f"quota pressure from {project.name}"},
                    messages_dir=messages_dir,
                    project_journal_delivery=False,
                )

        stream = messages_dir / "controller-quota-advisory.jsonl"
        assert_true(
            "both project advisories share the fleet-wide stream",
            len(_carrier_messages.read_envelopes(stream)) == 2,
        )
        assert_true(
            "fleet-wide stream does not receive a project owner sidecar",
            not (messages_dir / "controller-quota-advisory.owner.json").exists(),
        )


def test_worker_owned_stream_owner_uses_dispatch_project() -> None:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        project = base / "worker-project"
        init_git_project(project)
        env = _journal_test_env(base)
        dispatch_id = "worker-owned-owner"
        write_ledger_record(
            base,
            dispatch_id,
            project,
            controller_session_id="owner-test-session",
            controller_label="worker-project-controller",
        )
        with mock.patch.dict(
            os.environ,
            {**env, "GOALFLIGHT_DISPATCH_ID": dispatch_id},
            clear=False,
        ):
            _carrier_messages.post_message(
                dispatch_id=dispatch_id,
                msg_type="status",
                payload={"text": "worker-owned post"},
                messages_dir=Path(env["GOALFLIGHT_MESSAGES_DIR"]),
                source={"node": "test", "adapter": "pytest", "transport": "worker"},
                project_journal_delivery=False,
            )

        owner = json.loads(
            (Path(env["GOALFLIGHT_MESSAGES_DIR"]) / f"{dispatch_id}.owner.json").read_text(
                encoding="utf-8"
            )
        )
        assert_true("worker stream owner uses dispatch project", owner["project_root"] == str(project.resolve()))
        assert_true(
            "worker stream owner keeps known controller label",
            owner["controller_label"] == "worker-project-controller",
        )


def test_post_normalizes_controller_project_root_alias() -> None:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        root = base / "project-root"
        root.mkdir()
        alias = base / "project-alias"
        alias.symlink_to(root, target_is_directory=True)
        result = _carrier_messages.post_message(
            dispatch_id="canonical-root-post",
            msg_type="controller-notice",
            payload={"text": "canonical"},
            messages_dir=base / "messages",
            addressee={
                "kind": "controller",
                "label": "main",
                "project_root": str(alias),
            },
            project_journal_delivery=False,
        )
        assert_true(
            "post stores canonical project root",
            result["envelope"]["addressee"]["project_root"] == str(root.resolve()),
        )


def test_post_allows_controller_addressee_on_blocked_event() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        root = base / "project-root"
        root.mkdir()
        result = _carrier_messages.post_message(
            dispatch_id="blocked-controller-address",
            msg_type="blocked",
            payload={"text": "prelaunch refusal"},
            messages_dir=base / "messages",
            addressee={
                "kind": "controller",
                "label": "main",
                "project_root": str(root),
            },
            project_journal_delivery=False,
        )

        assert_true("blocked post is recorded", result["recorded"] is True)
        assert_true(
            "blocked post retains controller address",
            result["envelope"]["addressee"]["label"] == "main",
        )


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

        shared_id = messages.uuid.UUID("00000000-0000-4000-8000-000000000001")
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

        project = base / "project"
        init_git_project(project)
        read = run_messages_cli(
            messages_dir,
            fleet_dir,
            [
                "read",
                "--dispatch-id",
                dispatch_id,
            ],
        )
        assert_true("fleet-only read succeeds", read.returncode == 0)
        envelopes = json.loads(read.stdout)
        assert_true("fleet-only read returns one envelope", len(envelopes) == 1)
        assert_true("fleet-only read returns body", envelopes[0]["payload"]["text"] == "fleet-only body")


def test_advance_to_latest_derives_one_stream_snapshot() -> None:
    import goalflight_journal

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        project = base / "project"
        init_git_project(project)
        env = _journal_test_env(base)
        messages_dir = Path(env["GOALFLIGHT_MESSAGES_DIR"])
        label = "latest-controller"
        with mock.patch.dict(os.environ, env, clear=False):
            authority = goalflight_journal.open_or_create_journal(project)
            claimed = authority.claim_or_renew_lease(
                label,
                principal={"principal_id": "latest-controller-test"},
            )
            assert_true("latest test lease claimed", claimed.committed)
            _post_journal_controller_mail(
                project=project,
                messages_dir=messages_dir,
                label=label,
                dispatch_id="latest-mail",
            )
            lease = authority.active_lease(label)
            peek = authority.cursor_peek(label, nonce=lease.nonce, limit=1000)
            stream = peek.items[0]["stream_id"]

        advanced = run_messages_cli(
            messages_dir,
            Path(env["GOALFLIGHT_FLEET_DIR"]),
            [
                "advance",
                "--project-root",
                str(project),
                "--controller-label",
                label,
                "--lease-nonce",
                lease.nonce,
                "--to-latest",
                stream,
            ],
        )
        assert_true("to-latest advance succeeds", advanced.returncode == 0)
        assert_true("to-latest reports cursor advancement", "cursor advanced" in advanced.stdout)
        after = authority.cursor_peek(label, nonce=lease.nonce, limit=1000)
        assert_true("to-latest acknowledges only selected stream", not after.items)


def test_advance_derived_positions_stop_at_unreadable_user_confirm() -> None:
    import goalflight_journal

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        project = base / "project"
        init_git_project(project)
        env = _journal_test_env(base)
        messages_dir = Path(env["GOALFLIGHT_MESSAGES_DIR"])
        label = "stuck-advance-controller"
        stream = "stuck-confirm-stream"
        with mock.patch.dict(os.environ, env, clear=False):
            authority = goalflight_journal.open_or_create_journal(project)
            claimed = authority.claim_or_renew_lease(
                label,
                principal={"principal_id": "stuck-advance-test"},
            )
            assert_true("stuck advance lease claimed", claimed.committed)
            addressee = _carrier_messages.controller_addressee(
                label,
                project_root=project,
            )
            unreadable = _carrier_messages.post_message(
                dispatch_id=stream,
                msg_type="user_confirm",
                payload={
                    "text": "confirm this action",
                    "question": "confirm this action",
                    "question_kind": "USER-CONFIRM",
                    "wait_id": "confirm-wait",
                },
                messages_dir=messages_dir,
                source={"node": "test", "adapter": "pytest", "transport": "controller"},
                addressee=addressee,
            )
            later = _carrier_messages.post_message(
                dispatch_id=stream,
                msg_type="result",
                payload={"text": "visible result behind confirm"},
                messages_dir=messages_dir,
                source={"node": "test", "adapter": "pytest", "transport": "controller"},
                addressee=addressee,
            )
            carrier = Path(unreadable["path"])
            carrier_lines = carrier.read_text(encoding="utf-8").splitlines()
            assert len(carrier_lines) == 2
            carrier.write_text("{invalid USER-CONFIRM\n" + carrier_lines[1] + "\n", encoding="utf-8")
            lease = authority.active_lease(label)
            assert lease is not None

        to_latest = run_messages_cli(
            messages_dir,
            Path(env["GOALFLIGHT_FLEET_DIR"]),
            [
                "advance",
                "--project-root",
                str(project),
                "--controller-label",
                label,
                "--lease-nonce",
                lease.nonce,
                "--to-latest",
                stream,
            ],
        )
        assert_true(
            f"to-latest refuses unsafe prefix: rc={to_latest.returncode} "
            f"stdout={to_latest.stdout!r} stderr={to_latest.stderr!r}",
            to_latest.returncode == 2,
        )
        assert_true("to-latest reports stuck USER-CONFIRM", "STUCK:" in to_latest.stderr)
        pending = authority.cursor_peek(label, nonce=lease.nonce, limit=1000)
        assert_true(
            "to-latest leaves cursor at zero across stuck seq 1 and result seq 2",
            [(item["stream_seq"], item["event_uuid"]) for item in pending.items]
            == [(1, unreadable["envelope"]["id"]), (2, later["envelope"]["id"])],
        )

        for session_id, description in (("", "without session"), ("stuck-session", "without display")):
            with mock.patch.dict(os.environ, {"GOALFLIGHT_CONTROLLER_SESSION_ID": session_id}):
                fallback = run_messages_cli(
                    messages_dir,
                    Path(env["GOALFLIGHT_FLEET_DIR"]),
                    [
                        "advance",
                        "--project-root",
                        str(project),
                        "--controller-label",
                        label,
                        "--lease-nonce",
                        lease.nonce,
                        "--acked",
                    ],
                )
            assert_true(f"--acked {description} refuses", fallback.returncode == 2)
            assert_true(f"--acked {description} reports stuck row", "STUCK:" in fallback.stderr)
            assert_true(
                f"--acked {description} does not offer seq 2",
                f"{stream}=2" not in fallback.stderr,
            )


def test_advance_refusal_is_one_actionable_not_recorded_line() -> None:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        project = base / "project"
        init_git_project(project)
        env = _journal_test_env(base)
        refused = run_messages_cli(
            Path(env["GOALFLIGHT_MESSAGES_DIR"]),
            Path(env["GOALFLIGHT_FLEET_DIR"]),
            ["advance", "--project-root", str(project), "--controller-label", "missing", "--acked"],
        )
        assert_true("advance refusal exits nonzero", refused.returncode != 0)
        assert_true("advance refusal has one stderr line", len(refused.stderr.splitlines()) == 1)
        assert_true("advance refusal says NOT RECORDED", refused.stderr.startswith("NOT RECORDED:"))
        assert_true("advance refusal has a check command", "; next: python3 scripts/goalflight_messages.py relay" in refused.stderr)


def test_relay_dead_lease_prints_takeover_command() -> None:
    import goalflight_journal
    import socket

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        project = base / "project"
        init_git_project(project)
        env = _journal_test_env(base)
        label = "dead-controller"
        with mock.patch.dict(os.environ, env, clear=False):
            authority = goalflight_journal.open_or_create_journal(project)
            claimed = authority.claim_or_renew_lease(
                label,
                principal={
                    "pid": os.getpid(),
                    "start_token": "test",
                    "hostname": socket.gethostname(),
                },
            )
            assert_true("dead-lease test lease claimed", claimed.committed)
            lease = authority.active_lease(label)
            with (
                mock.patch.object(_carrier_messages.goalflight_wake, "lease_holder_alive", return_value=False),
                contextlib.redirect_stdout(io.StringIO()) as stdout,
                contextlib.redirect_stderr(io.StringIO()) as stderr,
            ):
                rc = _carrier_messages.main(
                    [
                        "--messages-dir", env["GOALFLIGHT_MESSAGES_DIR"],
                        "--fleet-dir", env["GOALFLIGHT_FLEET_DIR"],
                        "relay", "--project-root", str(project),
                        "--controller-label", label,
                    ]
                )
        assert_true("dead relay lease refuses", rc != 0)
        assert_true("dead relay names exact takeover options", "--takeover --controller-pid-from-ancestry" in stderr.getvalue())
        assert_true("dead relay fills label", f"--session-label {label}" in stderr.getvalue())
        assert_true("dead relay fills project root", f"--project-root {project}" in stderr.getvalue())
        assert_true("dead relay prints no pending success line", stdout.getvalue() == "")


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
        identity_store = messages_dir / messages.INGESTION_IDENTITY_DB
        assert_true("canonical ingestion identity store is outside carrier", identity_store.is_file() and identity_store.parent == messages_dir)
        remote_hash = messages._ingestion_identity_hash(messages.read_envelopes(remote_path)[0])
        with contextlib.closing(sqlite3.connect(identity_store)) as conn:
            stored = conn.execute("SELECT ingestion_order FROM identities WHERE identity_hash = ?", (remote_hash,)).fetchone()
        assert_true("identity store keys the remote event to its first order", stored == (first_order,))


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
                posted.stderr.startswith("NOT RECORDED:"),
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
            envelope = _carrier_messages.read_envelopes(
                _carrier_messages.inbox_path(messages_dir, "controller-report")
            )[0]
            assert_true(
                "controller mail records its addressee label",
                envelope["addressee"]["label"] == label,
            )
            assert_true(
                "controller mail records its addressee project",
                envelope["addressee"]["project_root"] == str(project.resolve()),
            )
            owner = json.loads(
                (messages_dir / "controller-report.owner.json").read_text(encoding="utf-8")
            )
            assert_true(
                "controller stream owner is the addressed project",
                owner["project_root"] == str(project.resolve()),
            )
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


def test_controller_post_projection_exception_keeps_recorded_result() -> None:
    import goalflight_journal

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        project = base / "project"
        init_git_project(project)
        messages_dir = base / "messages"
        fleet_dir = base / "fleet"
        label = "projection-error-controller"
        env = _journal_test_env(base)
        with mock.patch.dict(os.environ, env, clear=False):
            authority = goalflight_journal.open_or_create_journal(project)
            claimed = authority.claim_or_renew_lease(
                label,
                principal={"principal_id": "post-projection-error-test"},
            )
            assert_true("projection error controller lease claimed", claimed.committed)
            stdout = io.StringIO()
            stderr = io.StringIO()
            args = [
                "--messages-dir", str(messages_dir),
                "--fleet-dir", str(fleet_dir),
                "post", "--dispatch-id", "controller-projection-error",
                "--type", "controller-answer", "--text", "projection failed",
                "--to-controller", label, "--controller-project-root", str(project),
            ]
            with mock.patch.object(
                _carrier_messages,
                "_mark_journal_delivery",
                side_effect=_carrier_messages.MessageError("projection unavailable"),
            ), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                exit_code = _carrier_messages.main(args)

            result = json.loads(stdout.getvalue())
            delivery = result["controller_delivery"]
            assert_true("projection failure keeps post exit", exit_code == 1)
            assert_true("projection failure remains recorded", result["recorded"] is True)
            assert_true("projection failure is explicit", delivery["status"] == "controller_projection_failed")
            assert_true("projection reason is preserved", "projection unavailable" in delivery["detail"])
            assert_true(
                "post-append projection failure has one recorded-not-delivered line",
                stderr.getvalue().splitlines()
                == [
                    "RECORDED, NOT DELIVERED: "
                    f"{delivery['detail']}; next: python3 scripts/goalflight_messages.py read "
                    "--dispatch-id controller-projection-error --json"
                ],
            )
            carrier = _carrier_messages.read_envelopes(
                _carrier_messages.inbox_path(messages_dir, "controller-projection-error")
            )
            assert_true("projection failure leaves envelope durable", len(carrier) == 1)


def test_worker_post_survives_denied_journal_and_watcher_projects_once() -> None:
    import goalflight_journal
    import goalflight_watch

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        project = base / "project"
        init_git_project(project)
        messages_dir = base / "messages"
        fleet_dir = base / "fleet"
        label = "worker-projection-controller"
        dispatch_id = "worker-journal-denied"
        env = _journal_test_env(base)
        outbox = Path(env["GOALFLIGHT_DISPATCH_DIR"]) / f"{dispatch_id}.questions"
        outbox.mkdir(parents=True)
        env["GOALFLIGHT_QUESTION_OUTBOX_DIR"] = str(outbox)
        with mock.patch.dict(os.environ, env, clear=False):
            authority = goalflight_journal.open_or_create_journal(project)
            claimed = authority.claim_or_renew_lease(
                label,
                principal={"principal_id": "worker-projection-test"},
            )
            assert_true("worker projection controller lease claimed", claimed.committed)

            stdout = io.StringIO()
            stderr = io.StringIO()
            args = [
                "--messages-dir", str(messages_dir),
                "--fleet-dir", str(fleet_dir),
                "post", "--dispatch-id", dispatch_id,
                "--type", "user_need",
                "--payload", json.dumps({"text": "worker needs help", "project_root": str(project)}),
                "--json",
            ]
            with mock.patch.dict(
                os.environ, {"GOALFLIGHT_DISPATCH_ID": dispatch_id}, clear=False
            ), mock.patch.object(
                goalflight_journal,
                "open_or_create_journal",
                side_effect=goalflight_journal.JournalIOError("journal access denied"),
            ), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                exit_code = _carrier_messages.main(args)

            result = json.loads(stdout.getvalue())
            carrier = _carrier_messages.inbox_path(messages_dir, dispatch_id)
            envelopes = _carrier_messages.read_envelopes(carrier)
            assert_true("worker post is nonfatal", exit_code == 0)
            assert_true("worker post reports durable carrier", result["recorded"] is True)
            assert_true("worker post keeps its carrier despite journal denial", len(envelopes) == 1)
            event_uuid = envelopes[0]["id"]
            assert_true(
                "worker post explains pending projection",
                result["controller_delivery"]["status"] == "controller_projection_failed",
            )
            assert_true("worker projection denial emits no failure", stderr.getvalue() == "")
            assert_true(
                "worker post stages its dispatch outbox record",
                len(list((outbox / "journal").glob("journal-*.delivery"))) == 1,
            )

            projected = goalflight_watch.consume_worker_journal_outbox(outbox, dispatch_id)
            assert_true("watcher projects the staged event", projected == 1)
            rows = authority.read_all(
                """SELECT projected_at, withdrawn_at FROM delivery_events
                   WHERE event_uuid = ?""",
                (event_uuid,),
            )
            assert_true(
                "watcher creates one projected journal event",
                len(rows) == 1 and rows[0]["projected_at"] is not None,
            )

            repeated_projection = goalflight_watch.consume_worker_journal_outbox(
                outbox,
                dispatch_id,
            )
            assert_true("watcher restart does not project the event twice", repeated_projection == 0)
            rows = authority.read_all(
                "SELECT event_uuid FROM delivery_events WHERE event_uuid = ?",
                (event_uuid,),
            )
            assert_true("repeat watcher leaves one journal event", len(rows) == 1)


def test_denied_worker_post_is_nonfatal_and_stages_dispatch_outbox() -> None:
    import goalflight_journal

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        project = base / "project"
        init_git_project(project)
        env = _journal_test_env(base)
        dispatch_id = "worker-outbox-post"
        outbox = Path(env["GOALFLIGHT_DISPATCH_DIR"]) / f"{dispatch_id}.questions"
        outbox.mkdir(parents=True)
        env["GOALFLIGHT_QUESTION_OUTBOX_DIR"] = str(outbox)
        stdout = io.StringIO()
        stderr = io.StringIO()
        argv = [
            "--messages-dir", env["GOALFLIGHT_MESSAGES_DIR"],
            "--fleet-dir", env["GOALFLIGHT_FLEET_DIR"],
            "post", "--dispatch-id", dispatch_id, "--type", "user_need",
            "--payload", json.dumps({"text": "wake from outbox", "project_root": str(project)}),
            "--json",
        ]
        with (
            mock.patch.dict(
                os.environ,
                {**env, "GOALFLIGHT_DISPATCH_ID": dispatch_id},
                clear=False,
            ),
            mock.patch.object(
                _carrier_messages,
                "_prepare_journal_delivery",
                side_effect=goalflight_journal.JournalIOError("journal access denied"),
            ),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            result_code = _carrier_messages.main(argv)
        result = json.loads(stdout.getvalue())
        assert_true("denied worker post is a documented nonfatal result", result_code == 0)
        assert_true(
            "carrier was appended before the denied journal projection",
            _carrier_messages.inbox_path(Path(env["GOALFLIGHT_MESSAGES_DIR"]), dispatch_id).is_file(),
        )
        assert_true(
            "one sequenced record was staged",
            len(list((outbox / "journal").glob("journal-*.delivery"))) == 1,
        )


def test_worker_outbox_carrier_membership_and_setup_obey_pass_budget() -> None:
    dispatch_id = "bounded-outbox-carrier-membership"
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        project = base / "project"
        init_git_project(project)
        env = _journal_test_env(base)
        outbox = Path(env["GOALFLIGHT_DISPATCH_DIR"]) / f"{dispatch_id}.questions"
        outbox.mkdir(parents=True)
        env["GOALFLIGHT_QUESTION_OUTBOX_DIR"] = str(outbox)
        with mock.patch.dict(
            os.environ,
            {**env, "GOALFLIGHT_DISPATCH_ID": dispatch_id},
            clear=False,
        ), mock.patch.object(
            _carrier_messages,
            "_worker_journal_projection",
            return_value=((), RuntimeError("worker journal denied")),
        ):
            posted = _carrier_messages.post_message(
                dispatch_id=dispatch_id,
                msg_type="status",
                payload={"text": "current carrier record", "project_root": str(project)},
                messages_dir=Path(env["GOALFLIGHT_MESSAGES_DIR"]),
                source={"node": "local", "adapter": "pytest", "transport": "worker"},
            )

        carrier_path = _carrier_messages.inbox_path(
            Path(env["GOALFLIGHT_MESSAGES_DIR"]), dispatch_id
        )
        filler = []
        for sequence in range(2, 2502):
            envelope = {
                "schema": "goalflight.message.v1",
                "schema_version": 1,
                "id": str(_carrier_messages.uuid.uuid4()),
                "dispatch_id": dispatch_id,
                "seq": sequence,
                "ts": _carrier_messages.utc_now(),
                "source": {"node": "test", "adapter": "pytest", "transport": "worker"},
                "type": "status",
                "payload": {"text": "x" * 2048},
            }
            filler.append(
                json.dumps(envelope, ensure_ascii=False, sort_keys=True).encode("utf-8")
                + b"\n"
            )
        with carrier_path.open("ab") as handle:
            handle.write(b"".join(filler))

        storage = outbox / "journal"
        for entry in tuple(storage.iterdir()):
            entry.replace(outbox / entry.name)
        storage.rmdir()

        observed_lock_timeouts: list[float] = []
        observed_membership_reads: list[tuple[int, float | None]] = []
        original_read_slice = _carrier_messages.CarrierTransaction.read_recovery_slice

        @contextlib.contextmanager
        def no_wait_mail_lock(_path, *, timeout_secs=2.0):
            observed_lock_timeouts.append(float(timeout_secs))
            yield

        def tracked_read_slice(transaction, offset, **kwargs):
            observed_membership_reads.append(
                (int(kwargs["max_bytes"]), kwargs.get("deadline"))
            )
            return original_read_slice(transaction, offset, **kwargs)

        with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(
            _carrier_messages, "mail_lock", no_wait_mail_lock
        ), mock.patch.object(
            _carrier_messages.CarrierTransaction,
            "read_bytes",
            side_effect=AssertionError("membership validation must not read the whole carrier"),
        ), mock.patch.object(
            _carrier_messages.CarrierTransaction,
            "read_recovery_slice",
            tracked_read_slice,
        ), mock.patch.object(
            _carrier_messages, "_prepare_journal_delivery", return_value=()
        ):
            projected = _carrier_messages.project_worker_journal_outbox(
                dispatch_id,
                outbox_dir=outbox,
                messages_dir=Path(env["GOALFLIGHT_MESSAGES_DIR"]),
                budget_s=0.2,
            )

        assert_true("outbox membership projected within its byte budget", projected == 1)
        assert_true(
            "layout migration and all pass locks use the remaining deadline",
            bool(observed_lock_timeouts)
            and all(0 < timeout <= 0.2 for timeout in observed_lock_timeouts),
        )
        assert_true(
            "carrier membership read is bounded and deadline-aware",
            len(observed_membership_reads) == 1
            and observed_membership_reads[0][0] <= 4 * 1024 * 1024
            and isinstance(observed_membership_reads[0][1], float),
        )
        assert_true(
            "matching envelope is found without reading the 5 MB carrier",
            observed_membership_reads[0][0] < carrier_path.stat().st_size,
        )
        assert_true("outbox event identity stays stable", posted["envelope"]["id"])


def test_worker_outbox_recovery_appends_missing_wait_before_listener_delivery() -> None:
    import goalflight_journal

    dispatch_id = "example-dispatch-recovery"
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        project = base / "project"
        init_git_project(project)
        env = _journal_test_env(base)
        messages_dir = Path(env["GOALFLIGHT_MESSAGES_DIR"])
        outbox = Path(env["GOALFLIGHT_DISPATCH_DIR"]) / f"{dispatch_id}.questions"
        outbox.mkdir(parents=True)
        env["GOALFLIGHT_QUESTION_OUTBOX_DIR"] = str(outbox)
        write_ledger_record(base, dispatch_id, project)
        with mock.patch.dict(os.environ, env, clear=False):
            authority = goalflight_journal.Journal.create(project)
            lease = authority.claim_or_renew_lease(
                "controller", principal={"principal_id": "resume-outbox-recovery"}
            ).value
        assert_true("controller lease claimed", lease is not None)

        with mock.patch.dict(
            os.environ, {**env, "GOALFLIGHT_DISPATCH_ID": dispatch_id}, clear=False
        ), mock.patch.object(
            _carrier_messages,
            "_worker_journal_projection",
            return_value=((), RuntimeError("worker journal denied")),
        ):
            question = _carrier_messages.post_message(
                dispatch_id=dispatch_id,
                msg_type="user_confirm",
                payload={"text": "continue?", "project_root": str(project)},
                messages_dir=messages_dir,
                source={
                    "node": "local",
                    "adapter": "goalflight-dispatch",
                    "transport": "steer-wait",
                },
            )["envelope"]

        carrier = _carrier_messages.inbox_path(messages_dir, dispatch_id)
        carrier.unlink()
        prepared = authority.prepare_attempt(
            dispatch_id, owner_controller_label="controller"
        )
        assert_true("terminal attempt prepared", prepared.committed and prepared.value)
        terminal = authority.commit_terminal(
            prepared.value.attempt_id,
            terminal_state="complete",
            observation={"state": "complete", "text": "finished"},
        )
        assert_true("terminal attempt committed", terminal.committed)
        authority.project_terminal_outbox(messages_dir=messages_dir)

        with mock.patch.dict(os.environ, env, clear=False):
            projected = _carrier_messages.project_worker_journal_outbox(
                dispatch_id,
                outbox_dir=outbox,
                messages_dir=messages_dir,
                budget_s=1.0,
            )
        assert_true("missing wait envelope repaired and projected", projected == 1)
        raw = _carrier_messages.read_envelopes(carrier)
        assert [(item["type"], item["seq"]) for item in raw] == [
            ("result", 1),
            ("user_confirm", 2),
        ]
        snapshot = authority.cursor_peek("controller", nonce=lease.nonce)
        delivered = _carrier_messages._envelopes_with_rows(
            authority, list(snapshot.items), controller_label="controller"
        )
        assert [envelope["type"] for _, envelope in delivered] == [
            "result",
            "user_confirm",
        ]


def test_example_carrier_delivers_all_three_projected_events() -> None:
    import goalflight_journal

    dispatch_id = "example-dispatch-carrier"
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        project = base / "project"
        init_git_project(project)
        env = _journal_test_env(base)
        messages_dir = Path(env["GOALFLIGHT_MESSAGES_DIR"])
        outbox = Path(env["GOALFLIGHT_DISPATCH_DIR"]) / f"{dispatch_id}.questions"
        outbox.mkdir(parents=True)
        env["GOALFLIGHT_QUESTION_OUTBOX_DIR"] = str(outbox)
        write_ledger_record(base, dispatch_id, project)
        with mock.patch.dict(os.environ, env, clear=False):
            authority = goalflight_journal.Journal.create(project)
            lease = authority.claim_or_renew_lease(
                "controller", principal={"principal_id": "sol-carrier-projection"}
            ).value
        assert_true("controller lease claimed", lease is not None)
        with mock.patch.dict(
            os.environ, {**env, "GOALFLIGHT_DISPATCH_ID": dispatch_id}, clear=False
        ), mock.patch.object(
            _carrier_messages,
            "_worker_journal_projection",
            return_value=((), RuntimeError("worker journal denied")),
        ):
            _carrier_messages.post_message(
                dispatch_id=dispatch_id,
                msg_type="user_confirm",
                payload={"text": "continue?", "project_root": str(project)},
                messages_dir=messages_dir,
                source={
                    "node": "local",
                    "adapter": "goalflight-dispatch",
                    "transport": "steer-wait",
                },
            )

        with mock.patch.dict(os.environ, env, clear=False):
            _post_journal_controller_mail(
                project=project,
                messages_dir=messages_dir,
                label="controller",
                dispatch_id=dispatch_id,
            )
        prepared = authority.prepare_attempt(
            dispatch_id, owner_controller_label="controller"
        )
        assert_true("terminal attempt prepared", prepared.committed and prepared.value)
        terminal = authority.commit_terminal(
            prepared.value.attempt_id,
            terminal_state="complete",
            observation={"state": "complete", "text": "finished"},
        )
        assert_true("terminal attempt committed", terminal.committed)
        authority.project_terminal_outbox(messages_dir=messages_dir)

        with mock.patch.dict(os.environ, env, clear=False):
            assert_true(
                "wait question delivery projected",
                _carrier_messages.project_worker_journal_outbox(
                    dispatch_id,
                    outbox_dir=outbox,
                    messages_dir=messages_dir,
                    budget_s=1.0,
                ) == 1,
            )
        carrier = _carrier_messages.inbox_path(messages_dir, dispatch_id)
        raw = _carrier_messages.read_envelopes(carrier)
        assert [(item["type"], item["seq"]) for item in raw] == [
            ("user_confirm", 1),
            ("controller-notice", 2),
            ("result", 3),
        ]
        snapshot = authority.cursor_peek("controller", nonce=lease.nonce)
        delivered = _carrier_messages._envelopes_with_rows(
            authority, list(snapshot.items), controller_label="controller"
        )
        assert [(item["type"], item["seq"]) for _, item in delivered] == [
            ("user_confirm", 1),
            ("controller-notice", 2),
            ("result", 3),
        ]


def test_worker_outbox_assignment_uses_carrier_row_sequence() -> None:
    import goalflight_journal

    dispatch_id = "resumed-carrier-sequence"
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        project = base / "project"
        init_git_project(project)
        env = _journal_test_env(base)
        messages_dir = Path(env["GOALFLIGHT_MESSAGES_DIR"])
        outbox = Path(env["GOALFLIGHT_DISPATCH_DIR"]) / f"{dispatch_id}.questions"
        outbox.mkdir(parents=True)
        env["GOALFLIGHT_QUESTION_OUTBOX_DIR"] = str(outbox)
        write_ledger_record(base, dispatch_id, project)
        with mock.patch.dict(os.environ, env, clear=False):
            authority = goalflight_journal.Journal.create(project)
            lease = authority.claim_or_renew_lease(
                "controller", principal={"principal_id": "carrier-sequence"}
            ).value
        assert_true("controller lease claimed", lease is not None)
        with mock.patch.dict(
            os.environ, {**env, "GOALFLIGHT_DISPATCH_ID": dispatch_id}, clear=False
        ), mock.patch.object(
            _carrier_messages,
            "_worker_journal_projection",
            return_value=((), RuntimeError("worker journal denied")),
        ):
            question = _carrier_messages.post_message(
                dispatch_id=dispatch_id,
                msg_type="user_confirm",
                payload={"text": "continue?", "project_root": str(project)},
                messages_dir=messages_dir,
                source={"node": "local", "adapter": "goalflight-dispatch", "transport": "steer-wait"},
            )["envelope"]

        carrier = _carrier_messages.inbox_path(messages_dir, dispatch_id)
        filler = [
            {
                **question,
                "id": str(_carrier_messages.uuid.uuid4()),
                "seq": sequence,
                "source": {"node": "other", "adapter": "pytest", "transport": "worker"},
                "type": "status",
                "payload": {"text": f"prior {sequence}", "project_root": str(project)},
            }
            for sequence in (1, 2)
        ]
        resequenced = {**question, "seq": 3}
        _carrier_messages.update_envelopes(
            carrier, lambda _existing: (filler + [resequenced], None)
        )
        captured: list[dict] = []
        original_prepare = _carrier_messages._prepare_journal_delivery

        def capture(envelope, path, **kwargs):
            captured.append(dict(envelope))
            return original_prepare(envelope, path, **kwargs)

        with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(
            _carrier_messages, "_prepare_journal_delivery", capture
        ):
            projected = _carrier_messages.project_worker_journal_outbox(
                dispatch_id,
                outbox_dir=outbox,
                messages_dir=messages_dir,
                budget_s=1.0,
            )
        assert_true("resequenced outbox event projected", projected == 1)
        assert_true("journal assignment uses authoritative carrier seq", captured[0]["seq"] == 3)


def test_worker_outbox_timeouts_do_not_quarantine_deliverable_head() -> None:
    dispatch_id = "outbox-timeout-not-poison"
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        project = base / "project"
        init_git_project(project)
        env = _journal_test_env(base)
        outbox = Path(env["GOALFLIGHT_DISPATCH_DIR"]) / f"{dispatch_id}.questions"
        outbox.mkdir(parents=True)
        env["GOALFLIGHT_QUESTION_OUTBOX_DIR"] = str(outbox)
        with mock.patch.dict(
            os.environ,
            {**env, "GOALFLIGHT_DISPATCH_ID": dispatch_id},
            clear=False,
        ), mock.patch.object(
            _carrier_messages,
            "_worker_journal_projection",
            return_value=((), RuntimeError("worker journal denied")),
        ):
            posted = _carrier_messages.post_message(
                dispatch_id=dispatch_id,
                msg_type="status",
                payload={"text": "current carrier record", "project_root": str(project)},
                messages_dir=Path(env["GOALFLIGHT_MESSAGES_DIR"]),
                source={"node": "local", "adapter": "pytest", "transport": "worker"},
            )

        carrier_path = _carrier_messages.inbox_path(
            Path(env["GOALFLIGHT_MESSAGES_DIR"]), dispatch_id
        )
        original_transaction = _carrier_messages.carrier_transaction

        @contextlib.contextmanager
        def busy_carrier_transaction(path, **kwargs):
            if Path(path) == carrier_path:
                raise TimeoutError("mailbox lock busy for bounded projection pass")
            with original_transaction(path, **kwargs) as transaction:
                yield transaction

        with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(
            _carrier_messages,
            "carrier_transaction",
            busy_carrier_transaction,
        ), mock.patch.object(
            _carrier_messages,
            "_WORKER_JOURNAL_OUTBOX_RETRY_BASE_S",
            0.0,
        ):
            results = [
                _carrier_messages.project_worker_journal_outbox(
                    dispatch_id,
                    outbox_dir=outbox,
                    messages_dir=Path(env["GOALFLIGHT_MESSAGES_DIR"]),
                    budget_s=0.2,
                )
                for _ in range(3)
            ]

        record = outbox / "journal" / "journal-00000000000000000001.delivery"
        assert_true("lock timeouts leave the deliverable head pending", results == [0, 0, 0])
        assert_true("the deliverable head remains available", record.is_file())
        assert_true(
            "lock timeouts do not publish a poison marker",
            not record.with_suffix(".published").exists(),
        )
        assert_true(
            "lock timeouts do not quarantine the head",
            not (record.with_suffix(".jsonl").with_suffix(".quarantine.jsonl")).exists(),
        )
        with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(
            _carrier_messages,
            "_journal_delivery_targets",
            side_effect=TimeoutError("carrier recovery pass budget expired"),
        ), mock.patch.object(
            _carrier_messages,
            "_WORKER_JOURNAL_OUTBOX_RETRY_BASE_S",
            0.0,
        ):
            timed_out = [
                _carrier_messages.project_worker_journal_outbox(
                    dispatch_id,
                    outbox_dir=outbox,
                    messages_dir=Path(env["GOALFLIGHT_MESSAGES_DIR"]),
                    budget_s=0.2,
                )
                for _ in range(3)
            ]
        assert_true("preparation timeouts preserve the pending head", timed_out == [0, 0, 0])
        quarantine = _carrier_messages.quarantine_path(record.with_suffix(".jsonl"))
        assert_true("preparation timeouts do not quarantine", not quarantine.exists())
        with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(
            _carrier_messages,
            "_prepare_journal_delivery",
            side_effect=_carrier_messages.MessageError("persistent projection failure"),
        ), mock.patch.object(
            _carrier_messages,
            "_WORKER_JOURNAL_OUTBOX_RETRY_BASE_S",
            0.0,
        ):
            for _ in range(3):
                _carrier_messages.project_worker_journal_outbox(
                    dispatch_id,
                    outbox_dir=outbox,
                    messages_dir=Path(env["GOALFLIGHT_MESSAGES_DIR"]),
                    budget_s=0.2,
                )
        assert_true("real projection errors still quarantine after three attempts", quarantine.exists())
        assert_true("outbox event identity stays stable", posted["envelope"]["id"])


def test_journal_write_timeouts_do_not_quarantine_outbox_head() -> None:
    import goalflight_journal
    import goalflight_task

    def fixture(base: Path, dispatch_id: str):
        project = base / f"project-{dispatch_id}"
        init_git_project(project)
        env = _journal_test_env(base / dispatch_id)
        outbox = Path(env["GOALFLIGHT_DISPATCH_DIR"]) / f"{dispatch_id}.questions"
        outbox.mkdir(parents=True)
        env["GOALFLIGHT_QUESTION_OUTBOX_DIR"] = str(outbox)
        with mock.patch.dict(
            os.environ,
            {**env, "GOALFLIGHT_DISPATCH_ID": dispatch_id},
            clear=False,
        ), mock.patch.object(
            _carrier_messages,
            "_worker_journal_projection",
            return_value=((), RuntimeError("journal denied")),
        ):
            posted = _carrier_messages.post_message(
                dispatch_id=dispatch_id,
                msg_type="status",
                payload={"text": "retryable write", "project_root": str(project)},
                messages_dir=Path(env["GOALFLIGHT_MESSAGES_DIR"]),
                source={"node": "local", "adapter": "pytest", "transport": "worker"},
            )
        return project, env, outbox, posted["envelope"]["id"]

    def passes(dispatch_id: str, env: dict, outbox: Path) -> list[int]:
        with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(
            _carrier_messages,
            "_WORKER_JOURNAL_OUTBOX_RETRY_BASE_S",
            0.0,
        ):
            return [
                _carrier_messages.project_worker_journal_outbox(
                    dispatch_id,
                    outbox_dir=outbox,
                    messages_dir=Path(env["GOALFLIGHT_MESSAGES_DIR"]),
                    budget_s=0.2,
                )
                for _ in range(3)
            ]

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        project, env, outbox, event_id = fixture(base, "journal-lock-timeout")
        with mock.patch.dict(os.environ, env, clear=False):
            authority = goalflight_journal.open_or_create_journal(project)
        record = outbox / "journal" / "journal-00000000000000000001.delivery"
        with goalflight_task.FileLock(goalflight_journal.journal_write_lock_path(authority.path)):
            assert_true("held journal write lock retries", passes("journal-lock-timeout", env, outbox) == [0, 0, 0])
        assert_true("lock timeout head remains pending", record.is_file())
        assert_true(
            "lock timeout head is not quarantined",
            not record.with_suffix(".published").exists(),
        )
        assert_true(
            "lock timeout has no quarantine sidecar",
            not record.with_suffix(".jsonl").with_suffix(".quarantine.jsonl").exists(),
        )
        with mock.patch.dict(os.environ, env, clear=False):
            assert_true(
                "pending event projects after journal lock release",
                _carrier_messages.project_worker_journal_outbox(
                    "journal-lock-timeout",
                    outbox_dir=outbox,
                    messages_dir=Path(env["GOALFLIGHT_MESSAGES_DIR"]),
                    budget_s=1.0,
                ) == 1,
            )
        rows = authority.read_all(
            "SELECT projected_at FROM delivery_events WHERE event_uuid = ?",
            (event_id,),
        )
        assert_true("released lock projects the event", len(rows) == 1 and rows[0]["projected_at"] is not None)

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        project, env, outbox, event_id = fixture(base, "journal-budget-timeout")
        with mock.patch.dict(os.environ, env, clear=False):
            authority = goalflight_journal.open_or_create_journal(project)
        original_journal = goalflight_journal.Journal

        def exhausted_journal(root, **kwargs):
            return original_journal(
                root,
                **{**kwargs, "transaction_budget_s": 1e-12},
            )

        with mock.patch.object(goalflight_journal, "Journal", exhausted_journal):
            assert_true("transaction budget retries", passes("journal-budget-timeout", env, outbox) == [0, 0, 0])
        record = outbox / "journal" / "journal-00000000000000000001.delivery"
        assert_true("transaction timeout head remains pending", record.is_file())
        assert_true(
            "transaction timeout head is not quarantined",
            not record.with_suffix(".published").exists(),
        )
        assert_true(
            "transaction timeout has no quarantine sidecar",
            not record.with_suffix(".jsonl").with_suffix(".quarantine.jsonl").exists(),
        )
        with mock.patch.dict(os.environ, env, clear=False):
            assert_true(
                "pending event projects after restoring the transaction budget",
                _carrier_messages.project_worker_journal_outbox(
                    "journal-budget-timeout",
                    outbox_dir=outbox,
                    messages_dir=Path(env["GOALFLIGHT_MESSAGES_DIR"]),
                    budget_s=1.0,
                ) == 1,
            )
        rows = authority.read_all(
            "SELECT projected_at FROM delivery_events WHERE event_uuid = ?",
            (event_id,),
        )
        assert_true("restored budget projects the event", len(rows) == 1 and rows[0]["projected_at"] is not None)


def test_journal_write_timeout_is_a_message_error_for_controller_and_mcp() -> None:
    import goalflight_journal
    import goalflight_task

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        project = base / "project"
        init_git_project(project)
        env = _journal_test_env(base)
        messages_dir = Path(env["GOALFLIGHT_MESSAGES_DIR"])
        with mock.patch.dict(os.environ, env, clear=False):
            authority = goalflight_journal.open_or_create_journal(project)
        payload = json.dumps(
            {"text": "retryable controller post", "project_root": str(project)}
        )
        with goalflight_task.FileLock(goalflight_journal.journal_write_lock_path(authority.path)):
            controller = run_messages_cli(
                messages_dir,
                Path(env["GOALFLIGHT_FLEET_DIR"]),
                [
                    "post",
                    "--dispatch-id",
                    "controller-retryable-post",
                    "--type",
                    "status",
                    "--payload",
                    payload,
                ],
            )
            assert_true(
                "controller post gets the normal refusal exit",
                controller.returncode == 2,
            )
            assert_true(
                "controller post reports the retryable journal failure",
                "write budget" in controller.stderr,
            )

            request = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "goalflight_post_message",
                    "arguments": {
                        "dispatch_id": "mcp-retryable-post",
                        "type": "status",
                        "payload": {
                            "text": "retryable MCP post",
                            "project_root": str(project),
                        },
                    },
                },
            }
            ping = {"jsonrpc": "2.0", "id": 2, "method": "ping"}
            stdio = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "goalflight_mcp_messages.py"),
                    "stdio",
                ],
                input=json.dumps(request) + "\n" + json.dumps(ping) + "\n",
                env={**dict(os.environ), **env},
                capture_output=True,
                text=True,
                check=False,
            )
        assert_true("MCP stdio process stays alive", stdio.returncode == 0)
        responses = [json.loads(line) for line in stdio.stdout.splitlines()]
        assert_true(
            "MCP returns its normal JSON-RPC error",
            "error" in responses[0],
        )
        assert_true(
            "MCP stdio handles the following request",
            responses[1] == {"jsonrpc": "2.0", "id": 2, "result": {}},
        )


def test_invalid_worker_journal_outbox_cursor_fails_closed() -> None:
    import goalflight_journal

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        project = base / "project"
        init_git_project(project)
        env = _journal_test_env(base)
        dispatch_id = "invalid-outbox-cursor"
        outbox = Path(env["GOALFLIGHT_DISPATCH_DIR"]) / f"{dispatch_id}.questions"
        outbox.mkdir(parents=True)
        env["GOALFLIGHT_QUESTION_OUTBOX_DIR"] = str(outbox)
        with mock.patch.dict(
            os.environ,
            {**env, "GOALFLIGHT_DISPATCH_ID": dispatch_id},
            clear=False,
        ), mock.patch.object(
            _carrier_messages,
            "_worker_journal_projection",
            return_value=((), RuntimeError("journal denied")),
        ):
            posted = _carrier_messages.post_message(
                dispatch_id=dispatch_id,
                msg_type="status",
                payload={"text": "already projected", "project_root": str(project)},
                messages_dir=Path(env["GOALFLIGHT_MESSAGES_DIR"]),
                source={"node": "local", "adapter": "pytest", "transport": "worker"},
            )
        with mock.patch.dict(os.environ, env, clear=False):
            authority = goalflight_journal.open_or_create_journal(project)
            assert_true(
                "first projection succeeds",
                _carrier_messages.project_worker_journal_outbox(
                    dispatch_id,
                    outbox_dir=outbox,
                    messages_dir=Path(env["GOALFLIGHT_MESSAGES_DIR"]),
                    budget_s=1.0,
                ) == 1,
            )
        cursor = outbox / "journal" / ".journal-cursor"
        cursor.write_text("{corrupt cursor\n", encoding="utf-8")
        with mock.patch.dict(os.environ, env, clear=False):
            try:
                _carrier_messages.project_worker_journal_outbox(
                    dispatch_id,
                    outbox_dir=outbox,
                    messages_dir=Path(env["GOALFLIGHT_MESSAGES_DIR"]),
                    budget_s=1.0,
                )
            except _carrier_messages.MessageError as exc:
                assert_true("cursor failure is reported clearly", "cursor" in str(exc))
            else:
                raise AssertionError("invalid cursor should stop outbox projection")
        rows = authority.read_all(
            "SELECT event_uuid, recipient_label, projected_at "
            "FROM delivery_events WHERE event_uuid = ?",
            (posted["envelope"]["id"],),
        )
        assert_true("invalid cursor does not create a duplicate delivery row", len(rows) == 1)
        assert_true("existing delivery remains projected", rows[0]["projected_at"] is not None)


def test_controller_to_worker_steer_does_not_enter_journal_outbox() -> None:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        env = _journal_test_env(base)
        dispatch_id = "steer-outbox-exclusion"
        outbox = Path(env["GOALFLIGHT_DISPATCH_DIR"]) / f"{dispatch_id}.questions"
        outbox.mkdir(parents=True)
        env["GOALFLIGHT_QUESTION_OUTBOX_DIR"] = str(outbox)
        with (
            mock.patch.dict(
                os.environ,
                {**env, "GOALFLIGHT_DISPATCH_ID": ""},
                clear=False,
            ),
            mock.patch.object(
                _carrier_messages,
                "_deliver_message_to_worker",
                return_value={"requested": True, "delivered": True},
            ),
        ):
            _carrier_messages.post_message(
                dispatch_id=dispatch_id,
                msg_type="controller-notice",
                payload={"text": "steer the worker"},
                messages_dir=Path(env["GOALFLIGHT_MESSAGES_DIR"]),
                source={"node": "local", "adapter": "goalflight-dispatch", "transport": "steer"},
                deliver_to_worker=True,
                project_journal_delivery=False,
            )
        assert_true(
            "steer has no journal delivery record",
            list((outbox / "journal").glob("journal-*.delivery")) == [],
        )


def test_concurrent_workers_project_only_to_their_own_projects() -> None:
    import goalflight_journal
    import goalflight_watch

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        env = _journal_test_env(base)
        script = base / "post_worker.py"
        script.write_text(
            "\n".join(
                (
                    "import json, os, sys",
                    "from pathlib import Path",
                    "from unittest import mock",
                    f"sys.path.insert(0, {str(ROOT / 'scripts')!r})",
                    "import goalflight_messages as messages",
                    "dispatch_id = os.environ['GOALFLIGHT_DISPATCH_ID']",
                    "with mock.patch.object(messages, '_worker_journal_projection', return_value=((), RuntimeError('journal denied'))):",
                    "    result = messages.post_message(",
                    "        dispatch_id=dispatch_id, msg_type='user_need',",
                    "        payload={'text': dispatch_id, 'project_root': os.environ['TEST_PROJECT']},",
                    "        messages_dir=Path(os.environ['GOALFLIGHT_MESSAGES_DIR']),",
                    "        source={'node': 'local', 'adapter': 'pytest', 'transport': 'worker'},",
                    "    )",
                    "print(result['envelope']['id'])",
                )
            ),
            encoding="utf-8",
        )
        projects = [base / "project-a", base / "project-b"]
        dispatch_ids = ["worker-project-a", "worker-project-b"]
        for project in projects:
            init_git_project(project)
        with mock.patch.dict(os.environ, env, clear=False):
            for project in projects:
                goalflight_journal.open_or_create_journal(project)
        outboxes = []
        processes = []
        for project, dispatch_id in zip(projects, dispatch_ids):
            outbox = Path(env["GOALFLIGHT_DISPATCH_DIR"]) / f"{dispatch_id}.questions"
            outbox.mkdir(parents=True)
            outboxes.append(outbox)
            worker_env = os.environ.copy()
            worker_env.update(env)
            worker_env.update(
                {
                    "GOALFLIGHT_DISPATCH_ID": dispatch_id,
                    "GOALFLIGHT_QUESTION_OUTBOX_DIR": str(outbox),
                    "TEST_PROJECT": str(project),
                }
            )
            processes.append(
                subprocess.Popen(
                    [sys.executable, str(script)],
                    cwd=str(ROOT),
                    env=worker_env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            )
        event_ids = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=10)
            assert_true(f"concurrent worker post succeeds: {stderr}", process.returncode == 0)
            event_ids.append(stdout.strip())

        with mock.patch.dict(os.environ, env, clear=False):
            for outbox, dispatch_id in zip(outboxes, dispatch_ids):
                assert_true(
                    f"{dispatch_id} watcher drains its own outbox",
                    goalflight_watch.consume_worker_journal_outbox(outbox, dispatch_id) == 1,
                )
            for index, project in enumerate(projects):
                authority = goalflight_journal.Journal(project)
                own = authority.read_all(
                    "SELECT event_uuid, projected_at FROM delivery_events WHERE event_uuid = ?",
                    (event_ids[index],),
                )
                other = authority.read_all(
                    "SELECT event_uuid FROM delivery_events WHERE event_uuid = ?",
                    (event_ids[1 - index],),
                )
                assert_true(
                    f"{dispatch_ids[index]} reaches its own project",
                    len(own) == 1 and own[0]["projected_at"] is not None,
                )
                assert_true("one project's outbox never crosses to the other", other == [])


def test_outbox_repairs_partial_multi_controller_projection() -> None:
    import goalflight_journal
    import goalflight_watch

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        project = base / "project"
        init_git_project(project)
        env = _journal_test_env(base)
        dispatch_id = "worker-multi-controller"
        outbox = Path(env["GOALFLIGHT_DISPATCH_DIR"]) / f"{dispatch_id}.questions"
        outbox.mkdir(parents=True)
        env["GOALFLIGHT_QUESTION_OUTBOX_DIR"] = str(outbox)
        write_ledger_record(base, dispatch_id, project)
        with mock.patch.dict(os.environ, {**env, "GOALFLIGHT_DISPATCH_ID": dispatch_id}, clear=False):
            authority = goalflight_journal.open_or_create_journal(project)
            for label in ("outbox-controller-a", "outbox-controller-b"):
                claimed = authority.claim_or_renew_lease(
                    label,
                    principal={"principal_id": label},
                )
                assert_true(f"{label} lease claimed", claimed.committed)
            with mock.patch.object(
                _carrier_messages,
                "_worker_journal_projection",
                return_value=((), RuntimeError("journal denied")),
            ):
                posted = _carrier_messages.post_message(
                    dispatch_id=dispatch_id,
                    msg_type="result",
                    payload={"text": "one result", "project_root": str(project)},
                    messages_dir=Path(env["GOALFLIGHT_MESSAGES_DIR"]),
                    source={"node": "local", "adapter": "pytest", "transport": "worker"},
                )
            original_mark = _carrier_messages._mark_journal_delivery
            marked_assignments = 0
            injected = False

            def fail_last_recipient(assignments):
                nonlocal marked_assignments, injected
                assignments = tuple(assignments)
                marked_assignments += len(assignments)
                if not injected and marked_assignments > 1:
                    injected = True
                    raise goalflight_journal.JournalIOError("second recipient temporarily denied")
                return original_mark(assignments)

            clock = [time.time()]
            with mock.patch.object(_carrier_messages.time, "time", lambda: clock[0]):
                with mock.patch.object(
                    _carrier_messages,
                    "_mark_journal_delivery",
                    side_effect=fail_last_recipient,
                ):
                    goalflight_watch.consume_worker_journal_outbox(outbox, dispatch_id)
                rows = authority.read_all(
                    "SELECT recipient_label, projected_at FROM delivery_events "
                    "WHERE event_uuid = ? ORDER BY recipient_label",
                    (posted["envelope"]["id"],),
                )
                assert_true("both recipient assignments are durable", len(rows) == 2)
                assert_true(
                    "one recipient was delivered before the injected failure",
                    sum(row["projected_at"] is not None for row in rows) == 1,
                )
                clock[0] += 2
                projected = goalflight_watch.consume_worker_journal_outbox(outbox, dispatch_id)

            assert_true("retry repairs the remaining recipient", projected == 1)
            rows = authority.read_all(
                "SELECT recipient_label, projected_at FROM delivery_events "
                "WHERE event_uuid = ? ORDER BY recipient_label",
                (posted["envelope"]["id"],),
            )
            assert_true("each controller has one delivery row", len(rows) == 2)
            assert_true("both controller deliveries complete", all(row["projected_at"] is not None for row in rows))



def test_outbox_replacement_projection_ignores_superseded_carrier_event() -> None:
    import goalflight_journal
    import goalflight_watch

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        project = base / "project"
        init_git_project(project)
        env = _journal_test_env(base)
        messages_dir = Path(env["GOALFLIGHT_MESSAGES_DIR"])
        dispatch_id = "replacement-race-carrier"
        worker_a = "replacement-race-a"
        worker_b = "replacement-race-b"
        outbox_a = Path(env["GOALFLIGHT_DISPATCH_DIR"]) / f"{worker_a}.questions"
        outbox_b = Path(env["GOALFLIGHT_DISPATCH_DIR"]) / f"{worker_b}.questions"
        outbox_a.mkdir(parents=True)
        outbox_b.mkdir(parents=True)
        with mock.patch.dict(os.environ, env, clear=False):
            authority = goalflight_journal.open_or_create_journal(project)

        def deny_projection(*_args, **_kwargs):
            return (), goalflight_journal.JournalIOError("worker journal denied")

        with mock.patch.dict(
            os.environ,
            {
                **env,
                "GOALFLIGHT_DISPATCH_ID": worker_a,
                "GOALFLIGHT_QUESTION_OUTBOX_DIR": str(outbox_a),
            },
            clear=False,
        ), mock.patch.object(_carrier_messages, "_worker_journal_projection", deny_projection):
            original = _carrier_messages.post_message(
                dispatch_id=dispatch_id,
                msg_type="status",
                payload={"text": "old", "project_root": str(project), "replaceable": True},
                messages_dir=messages_dir,
                source={"node": "local", "adapter": "pytest", "transport": "worker"},
            )["envelope"]
        with mock.patch.dict(
            os.environ,
            {
                **env,
                "GOALFLIGHT_DISPATCH_ID": worker_b,
                "GOALFLIGHT_QUESTION_OUTBOX_DIR": str(outbox_b),
            },
            clear=False,
        ), mock.patch.object(_carrier_messages, "_worker_journal_projection", deny_projection):
            replacement = _carrier_messages.post_message(
                dispatch_id=dispatch_id,
                msg_type="status",
                payload={"text": "new", "project_root": str(project)},
                messages_dir=messages_dir,
                source={"node": "local", "adapter": "pytest", "transport": "worker"},
                replace_if=lambda item: item.get("payload", {}).get("replaceable") is True,
            )["envelope"]

        with mock.patch.dict(os.environ, {**env, "GOALFLIGHT_DISPATCH_ID": ""}, clear=False):
            projected_b = goalflight_watch.consume_worker_journal_outbox(outbox_b, worker_b)
            retry = outbox_b / "journal" / "journal-00000000000000000001.retry"
            assert projected_b == 1, (
                retry.read_text(encoding="utf-8") if retry.exists() else "no retry record"
            )
            assert goalflight_watch.consume_worker_journal_outbox(outbox_a, worker_a) == 0

        old_rows = authority.read_all(
            "SELECT projected_at, withdrawn_at FROM delivery_events WHERE event_uuid = ?",
            (original["id"],),
        )
        new_rows = authority.read_all(
            "SELECT projected_at, withdrawn_at FROM delivery_events WHERE event_uuid = ?",
            (replacement["id"],),
        )
        assert_true(
            "superseded outbox event is never live after replacement projects first",
            not old_rows or all(row["withdrawn_at"] is not None for row in old_rows),
        )
        assert_true(
            "current replacement is projected",
            len(new_rows) == 1 and new_rows[0]["projected_at"] is not None,
        )


def test_worker_journal_outbox_migrates_legacy_root_records() -> None:
    import goalflight_journal
    import goalflight_watch

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        project = base / "project"
        init_git_project(project)
        env = _journal_test_env(base)
        dispatch_id = "legacy-journal-outbox"
        outbox = Path(env["GOALFLIGHT_DISPATCH_DIR"]) / f"{dispatch_id}.questions"
        outbox.mkdir(parents=True)
        envelope = {
            "schema": "goalflight.message.v1",
            "schema_version": 1,
            "id": str(_carrier_messages.uuid.uuid4()),
            "dispatch_id": dispatch_id,
            "seq": 1,
            "ts": _carrier_messages.utc_now(),
            "source": {"node": "test", "adapter": "pytest", "transport": "worker"},
            "type": "status",
            "payload": {"project_root": str(project), "text": "legacy record"},
        }
        legacy_record = outbox / "journal-00000000000000000001.delivery"
        legacy_record.write_text(
            json.dumps(
                {
                    "schema": _carrier_messages._WORKER_JOURNAL_OUTBOX_SCHEMA,
                    "dispatch_id": dispatch_id,
                    "sequence": 1,
                    "project_journal_delivery": True,
                    "envelope": envelope,
                    "replaced_envelopes": [],
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        (outbox / ".journal-sequence").write_text("1\n", encoding="ascii")
        _carrier_add(
            _carrier_messages.inbox_path(
                Path(env["GOALFLIGHT_MESSAGES_DIR"]),
                dispatch_id,
            ),
            envelope,
        )

        with mock.patch.dict(os.environ, env, clear=False):
            authority = goalflight_journal.open_or_create_journal(project)
            assert goalflight_watch.consume_worker_journal_outbox(outbox, dispatch_id) == 1

        current_record = outbox / "journal" / legacy_record.name
        assert current_record.is_file()
        assert not legacy_record.exists()
        rows = authority.read_all(
            "SELECT event_uuid, projected_at FROM delivery_events WHERE event_uuid = ?",
            (envelope["id"],),
        )
        assert len(rows) == 1 and rows[0]["projected_at"] is not None


def test_worker_journal_outbox_layout_migration_is_lazy_and_resumes() -> None:
    with tempfile.TemporaryDirectory() as td:
        outbox = Path(td) / "outbox"
        outbox.mkdir()
        for index in range(401):
            (outbox / f"question-{index:04d}.json").write_text("retained", encoding="utf-8")
        for index in range(401):
            (outbox / f"journal-{index:020d}.delivery").write_bytes(b"legacy\n")
        storage = outbox / "journal"
        storage.mkdir()

        original_scandir = os.scandir

        class SlowEntry:
            def __init__(self, entry):
                self._entry = entry
                self.name = entry.name
                self.path = entry.path

            def is_file(self, *, follow_symlinks=True):
                time.sleep(0.002)
                return self._entry.is_file(follow_symlinks=follow_symlinks)

        class SlowEntries:
            def __init__(self, path):
                self._entries = original_scandir(path)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self._entries.close()

            def __iter__(self):
                yield from (
                    SlowEntry(entry)
                    for entry in sorted(
                        self._entries,
                        key=lambda item: (not item.name.startswith("question-"), item.name),
                    )
                )

        calls = 0
        migrated_any = False
        def slow_scandir(path):
            if Path(path) == outbox:
                return SlowEntries(path)
            return original_scandir(path)

        with mock.patch.object(os, "scandir", side_effect=slow_scandir), mock.patch.object(
            _carrier_messages,
            "_WORKER_JOURNAL_OUTBOX_PASS_BUDGET_S",
            0.1,
        ):
            while not (storage / ".layout-v1").exists():
                before = sum(
                    path.name.endswith(".delivery")
                    for path in storage.iterdir()
                )
                started = time.monotonic()
                _carrier_messages._worker_journal_outbox_storage_dir(
                    outbox,
                    deadline=started + _carrier_messages._WORKER_JOURNAL_OUTBOX_PASS_BUDGET_S,
                )
                elapsed = time.monotonic() - started
                after = sum(
                    path.name.endswith(".delivery")
                    for path in storage.iterdir()
                )
                migrated_any = migrated_any or after > before
                assert_true("migration pass stays near its deadline", elapsed < 0.2)
                calls += 1
                assert_true("migration finishes in several bounded passes", calls < 100)
        assert_true("migration makes progress", migrated_any)
        assert_true(
            "all legacy records migrate",
            sum(path.name.endswith(".delivery") for path in storage.iterdir()) == 401,
        )


def test_worker_journal_outbox_maximum_dispatch_id_resumes_carrier_scan() -> None:
    import goalflight_journal

    dispatch_id = "a" + "b" * 253 + "c"
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        project = base / "project"
        init_git_project(project)
        env = _journal_test_env(base)
        messages_dir = Path(env["GOALFLIGHT_MESSAGES_DIR"])
        import goalflight_dispatch_paths

        outbox = Path(env["GOALFLIGHT_DISPATCH_DIR"]) / "bounded.questions"
        carrier_dispatch_id = "bounded-carrier"
        carrier = _carrier_messages.inbox_path(messages_dir, carrier_dispatch_id)
        with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(
            goalflight_dispatch_paths,
            "worker_question_outbox_dir",
            return_value=outbox,
        ):
            outbox.mkdir(parents=True)
        filler = {
            "schema": "goalflight.message.v1",
            "schema_version": 1,
            "dispatch_id": carrier_dispatch_id,
            "source": {"node": "test", "adapter": "pytest", "transport": "worker"},
            "type": "status",
            "payload": {"text": "x" * 20000, "project_root": str(project)},
        }
        envelopes = [
            {**filler, "id": str(_carrier_messages.uuid.uuid4()), "seq": seq,
             "ts": _carrier_messages.utc_now()}
            for seq in range(1, 251)
        ]
        target = {
            **filler,
            "id": str(_carrier_messages.uuid.uuid4()),
            "seq": 251,
            "ts": _carrier_messages.utc_now(),
            "payload": {"text": "target", "project_root": str(project)},
        }
        envelopes.append(target)
        _carrier_messages.update_envelopes(carrier, lambda _old: (envelopes, None))
        storage = _carrier_messages._worker_journal_outbox_storage_dir(outbox)
        record = {
            "schema": _carrier_messages._WORKER_JOURNAL_OUTBOX_SCHEMA,
            "dispatch_id": dispatch_id,
            "sequence": 1,
            "project_journal_delivery": True,
            "envelope": target,
            "replaced_envelopes": [],
        }
        (storage / "journal-00000000000000000001.delivery").write_text(
            json.dumps(record, sort_keys=True) + "\n", encoding="utf-8"
        )
        (storage / ".journal-sequence").write_text("1\n", encoding="ascii")
        with mock.patch.dict(os.environ, {**env, "GOALFLIGHT_DISPATCH_ID": ""}, clear=False), mock.patch.object(
            goalflight_dispatch_paths,
            "worker_question_outbox_dir",
            return_value=outbox,
        ):
            authority = goalflight_journal.open_or_create_journal(project)
            projected = 0
            passes = 0
            while not (storage / "journal-00000000000000000001.published").exists():
                projected += _carrier_messages.project_worker_journal_outbox(
                    dispatch_id,
                    outbox_dir=outbox,
                    messages_dir=messages_dir,
                    budget_s=0.5,
                )
                passes += 1
                assert_true("multi-slice carrier scan completes", passes < 100)
        rows = authority.read_all(
            "SELECT projected_at FROM delivery_events WHERE event_uuid = ?",
            (target["id"],),
        )
        assert_true("maximum legal dispatch ID target is projected", projected == 1)
        assert_true("resumed target is projected once", len(rows) == 1 and rows[0]["projected_at"] is not None)

        cursor_path = storage / ".journal-cursor"
        cursor_path.write_bytes(b" " * (_carrier_messages._WORKER_JOURNAL_OUTBOX_CURSOR_MAX_BYTES + 1))
        try:
            _carrier_messages._read_worker_journal_outbox_cursor(cursor_path, dispatch_id)
        except _carrier_messages.MessageError:
            pass
        else:
            raise AssertionError("over-limit cursor must not silently reset to zero")


def test_unowned_terminal_outbox_reader_uses_remaining_pass_deadline() -> None:
    import goalflight_journal

    envelope = {
        "dispatch_id": "unowned-terminal-deadline",
        "type": "result",
        "payload": {},
    }
    reader = mock.Mock()
    reader.lease_records.return_value = []
    deadline = time.monotonic() + 0.25
    with mock.patch.object(
        _carrier_messages,
        "_dispatch_record",
        return_value=({"dispatch_id": envelope["dispatch_id"], "project_root": "/project"}, None),
    ), mock.patch.object(
        goalflight_journal.Journal,
        "open_reader",
        return_value=reader,
    ) as open_reader:
        targets = _carrier_messages._journal_delivery_targets(envelope, deadline=deadline)

    assert targets == ((Path("/project"), "*", False),)
    kwargs = open_reader.call_args.kwargs
    assert 0 < kwargs["retry_budget_s"] <= 0.25
    assert kwargs["open_retry_budget_s"] == kwargs["retry_budget_s"]
    assert kwargs["transaction_budget_s"] == kwargs["retry_budget_s"]


def test_worker_outbox_quarantine_bounds_reason_and_reaches_later_records() -> None:
    import goalflight_journal
    import goalflight_watch

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        project = base / "project"
        init_git_project(project)
        env = _journal_test_env(base)
        dispatch_id = "outbox-quarantine"
        outbox = Path(env["GOALFLIGHT_DISPATCH_DIR"]) / f"{dispatch_id}.questions"
        outbox.mkdir(parents=True)

        def envelope(sequence: int, payload: dict) -> dict:
            return {
                "schema": "goalflight.message.v1",
                "schema_version": 1,
                "id": str(_carrier_messages.uuid.uuid4()),
                "dispatch_id": dispatch_id,
                "seq": sequence,
                "ts": _carrier_messages.utc_now(),
                "source": {"node": "test", "adapter": "pytest", "transport": "worker"},
                "type": "status",
                "payload": payload,
            }

        def outbox_record(sequence: int, value: dict) -> bytes:
            return (
                json.dumps(
                    {
                        "schema": _carrier_messages._WORKER_JOURNAL_OUTBOX_SCHEMA,
                        "dispatch_id": dispatch_id,
                        "sequence": sequence,
                        "project_journal_delivery": True,
                        "envelope": value,
                        "replaced_envelopes": [],
                    },
                    ensure_ascii=True,
                    sort_keys=True,
                )
                + "\n"
            ).encode("utf-8")

        deep: object = "deep value"
        for _ in range(_carrier_messages.MAX_JSON_DEPTH + 4):
            deep = [deep]
        invalid_long = envelope(
            1,
            {"project_root": str(project), "nested": {"k" * 70_000 + chr(0xD800): deep}},
        )
        short_deep: object = "deep value"
        for _ in range(_carrier_messages.MAX_JSON_DEPTH + 4):
            short_deep = [short_deep]
        invalid_surrogate = envelope(
            2,
            {"project_root": str(project), "nested": {chr(0xD800): short_deep}},
        )
        valid = envelope(3, {"project_root": str(project), "text": "after invalid records"})
        for sequence, value in ((1, invalid_long), (2, invalid_surrogate), (3, valid)):
            _carrier_messages._worker_journal_outbox_record_path(outbox, sequence).write_bytes(
                outbox_record(sequence, value)
            )
        _carrier_add(
            _carrier_messages.inbox_path(
                Path(env["GOALFLIGHT_MESSAGES_DIR"]),
                dispatch_id,
            ),
            valid,
        )
        storage = getattr(_carrier_messages, "_worker_journal_outbox_storage_dir", None)
        sequence_path = (storage(outbox) if storage else outbox) / ".journal-sequence"
        sequence_path.write_text("3\n", encoding="ascii")

        with mock.patch.dict(os.environ, env, clear=False):
            authority = goalflight_journal.open_or_create_journal(project)
            for _ in range(3):
                goalflight_watch.consume_worker_journal_outbox(outbox, dispatch_id)
            delivered = authority.read_all(
                "SELECT event_uuid, projected_at FROM delivery_events WHERE stream_id = ?",
                (dispatch_id,),
            )

        assert_true(
            "valid row after bounded quarantine reasons is projected",
            len(delivered) == 1 and delivered[0]["projected_at"] is not None,
        )
        quarantine_rows = []
        storage_dir = storage(outbox) if storage else outbox
        for sequence in (1, 2):
            quarantine_file = _carrier_messages.quarantine_path(
                storage_dir / f"journal-{sequence:020d}.jsonl"
            )
            rows = [
                json.loads(line)
                for line in quarantine_file.read_text(encoding="utf-8").splitlines()
            ]
            assert_true(f"sequence {sequence} quarantined once", len(rows) == 1)
            quarantine_rows.append(rows[0])
        assert_true(
            "long validation reason is truncated at the storage bound",
            len(quarantine_rows[0]["reason"].encode("utf-8"))
            == _carrier_messages._CARRIER_RECOVERY_QUARANTINE_REASON_BYTES,
        )
        assert_true(
            "short validation reason replaces the lone surrogate",
            chr(0xD800) not in quarantine_rows[1]["reason"]
            and "?" in quarantine_rows[1]["reason"],
        )


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
            assert_true("unresolved controller post keeps its failure exit", posted.returncode == 1)
            assert_true("unresolved controller post remains recorded", result["recorded"] is True)
            assert_true("unresolved controller channel was requested", delivery["requested"] is True)
            assert_true("unresolved controller is not claimed delivered", delivery["delivered"] is False)
            assert_true(
                "unresolved status is machine-distinct",
                delivery["status"] == "controller_addressee_unresolved",
            )
            assert_true("unresolved label is reported", delivery["recipient_label"] == missing_label)
            assert_true("unresolved report says it reached nobody", "reached nobody" in delivery["detail"])
            expected_next = (
                "python3 scripts/goalflight_messages.py read "
                "--dispatch-id controller-unresolved --json"
            )
            assert_true(
                "durable post failure has one recorded-not-delivered line",
                posted.stderr.splitlines()
                == [
                    "RECORDED, NOT DELIVERED: "
                    f"{delivery['detail']}; next: {expected_next}"
                ],
            )
            carrier = messages_dir / "controller-unresolved.jsonl"
            envelopes = _carrier_messages.read_envelopes(carrier)
            assert_true(
                "failed post remains in its carrier",
                len(envelopes) == 1 and envelopes[0]["id"] == delivery["event_uuid"],
            )
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


def test_controller_steer_read_replays_projection_after_commit_failure() -> None:
    import tempfile
    import goalflight_steer_mailbox as steer

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        project = base / "project"
        init_git_project(project)
        (project / "README.md").write_text("fixture\n", encoding="utf-8")
        for args in (
            ("config", "user.email", "test@example.test"),
            ("config", "user.name", "Test"),
            ("add", "README.md"),
            ("commit", "-m", "fixture"),
        ):
            subprocess.run(
                ["git", "-C", str(project), *args],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
        linked = base / "linked"
        subprocess.run(
            ["git", "-C", str(project), "worktree", "add", "--detach", str(linked), "HEAD"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        dispatch_id = "d-controller-replay"
        write_ledger_record(base, dispatch_id, project, worker_pid=os.getpid())
        env = {
            **_journal_test_env(base),
            "GOALFLIGHT_PROJECT_ROOT": str(linked),
            "GOALFLIGHT_CONTROLLER_LABEL": "replay-controller",
            "GOALFLIGHT_CONTROLLER_PID": str(os.getpid()),
        }
        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch.object(
                _carrier_messages,
                "_deliver_message_to_worker",
                side_effect=RuntimeError("projection process died"),
            ):
                with contextlib.suppress(RuntimeError):
                    _carrier_messages.post_controller_steer(dispatch_id, "replay this steer")

            messages_dir = Path(env["GOALFLIGHT_MESSAGES_DIR"])
            first = _carrier_messages.read_envelopes(messages_dir / f"{dispatch_id}.jsonl")
            assert len(first) == 1, first
            first_id = first[0]["id"]
            first_identity = first[0]["source"].get("project_identity")
            assert isinstance(first_identity, list), first
            subprocess.run(
                ["git", "-C", str(project), "worktree", "remove", "--force", str(linked)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
            os.environ["GOALFLIGHT_PROJECT_ROOT"] = str(project)

            retry_source = dict(first[0]["source"])
            retry_source["project_root"] = str(project)
            retry_payload = dict(first[0]["payload"])
            retry_sender = dict(retry_payload["sender"])
            retry_sender["project_root"] = str(project)
            retry_payload["sender"] = retry_sender
            replay = _carrier_messages.post_message(
                dispatch_id=dispatch_id,
                msg_type=first[0]["type"],
                payload=retry_payload,
                messages_dir=messages_dir,
                source=retry_source,
                priority=first[0]["priority"],
                event_id=first_id,
                deliver_to_worker=True,
                retain_terminal_worker_view=True,
                project_journal_delivery=False,
            )
            assert replay["recorded"] is False, replay

            for _ in range(2):
                with mock.patch.dict(
                    os.environ,
                    {
                        "GOALFLIGHT_PROJECT_ROOT": "",
                        "GOALFLIGHT_CONTROLLER_LABEL": "",
                    },
                ), contextlib.redirect_stdout(io.StringIO()):
                    assert _carrier_messages.cmd_read(mock.Mock(
                        dispatch_id=dispatch_id, messages_dir=messages_dir,
                        fleet_dir=base / "fleet", last=None, json=True,
                    )) == 0

            entries = steer.worker_entries(
                steer.read_steer_entries(steer.steer_file(dispatch_id))
            )
            assert len(entries) == 1, entries
            assert entries[0]["context"]["message_envelope"]["id"] == first_id

            second = _carrier_messages.post_controller_steer(dispatch_id, "replay this steer")
            assert second["recorded"] is True, second
            assert second["envelope"]["id"] != first_id, second
            canonical = _carrier_messages.read_envelopes(messages_dir / f"{dispatch_id}.jsonl")
            assert len(canonical) == 2, canonical
            assert [item["id"] for item in canonical] == [first_id, second["envelope"]["id"]]
            assert canonical[1]["source"]["project_identity"] == first_identity

            for _ in range(2):
                with mock.patch.dict(
                    os.environ,
                    {
                        "GOALFLIGHT_PROJECT_ROOT": "",
                        "GOALFLIGHT_CONTROLLER_LABEL": "",
                    },
                ), contextlib.redirect_stdout(io.StringIO()):
                    assert _carrier_messages.cmd_read(mock.Mock(
                        dispatch_id=dispatch_id, messages_dir=messages_dir,
                        fleet_dir=base / "fleet", last=None, json=True,
                    )) == 0
            entries = steer.worker_entries(
                steer.read_steer_entries(steer.steer_file(dispatch_id))
            )
            assert len(entries) == 2, entries
            assert {entry["context"]["message_envelope"]["id"] for entry in entries} == {
                first_id,
                second["envelope"]["id"],
            }


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


def test_stuck_listener_row_is_not_advanced_and_does_not_block_other_streams() -> None:
    stuck = {"stream_id": "same-stream", "stream_seq": 1, "listener_status": "STUCK"}
    later = {"stream_id": "same-stream", "stream_seq": 2}
    other = {"stream_id": "other-stream", "stream_seq": 1}
    positions = _carrier_messages._cursor_positions_for_shown_rows(
        [stuck, later, other], [(later, {}), (other, {})]
    )
    assert positions == {"other-stream": 1}
    positions = _carrier_messages._cursor_positions_for_shown_rows(
        [later, stuck, other], [(later, {}), (other, {})]
    )
    assert positions == {"other-stream": 1}

    before_stuck = {"stream_id": "same-stream", "stream_seq": 1}
    stuck_after = {
        "stream_id": "same-stream",
        "stream_seq": 2,
        "listener_status": "STUCK",
    }
    later_after = {"stream_id": "same-stream", "stream_seq": 3}
    positions = _carrier_messages._cursor_positions_for_shown_rows(
        [before_stuck, stuck_after, later_after, other],
        [(before_stuck, {}), (later_after, {}), (other, {})],
    )
    assert positions == {"same-stream": 1, "other-stream": 1}

    unreported = {"stream_id": "held-stream", "stream_seq": 1}
    after = {"stream_id": "held-stream", "stream_seq": 2}
    positions = _carrier_messages._cursor_positions_for_shown_rows(
        [unreported, after, other], [(after, {}), (other, {})]
    )
    assert positions == {"other-stream": 1}


def test_listener_repairs_legacy_sequence_mismatches_from_copied_carriers() -> None:
    import goalflight_journal

    fixtures = ROOT / "tests" / "fixtures" / "journal_outbox_carriers"
    cases = (
        ("result-only.jsonl", 1, 2),
        ("confirm-notice-result.jsonl", 3, 1),
    )
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        project = base / "project"
        init_git_project(project)
        env = _journal_test_env(base)
        messages_dir = Path(env["GOALFLIGHT_MESSAGES_DIR"])
        with mock.patch.dict(os.environ, env, clear=False):
            authority = goalflight_journal.open_or_create_journal(project)
            for fixture_name, actual_seq, assigned_seq in cases:
                carrier_records = [
                    json.loads(line)
                    for line in (fixtures / fixture_name).read_text(encoding="utf-8").splitlines()
                ]
                for record in carrier_records:
                    addressee = record.get("addressee")
                    if isinstance(addressee, dict):
                        addressee["project_root"] = str(project.resolve())
                result_record = next(record for record in carrier_records if record["type"] == "result")
                label = result_record["addressee"]["label"]
                claimed = authority.claim_or_renew_lease(
                    label,
                    principal={"principal_id": "legacy-sequence-repair-test"},
                )
                assert_true("repair test lease claimed", claimed.committed)
                carrier = messages_dir / f"{result_record['dispatch_id']}.jsonl"
                for record in carrier_records:
                    _carrier_add(carrier, record)

                assigned = authority.record_delivery_event(
                    recipient_label=label,
                    origin_node=result_record["source"]["node"],
                    event_uuid=result_record["id"],
                    stream_id=result_record["dispatch_id"],
                    stream_seq=assigned_seq,
                    carrier_path=carrier,
                    event_type="result",
                    wake_class="waking",
                    created_at=result_record["ts"],
                )
                assert_true("legacy assignment recorded", assigned.committed)
                projected = authority.mark_delivery_projected(
                    recipient_label=label,
                    origin_node=result_record["source"]["node"],
                    event_uuid=result_record["id"],
                )
                assert_true("legacy assignment projected", projected.committed)

                row = next(
                    item
                    for item in authority.pending_delivery_events(label)
                    if item["event_uuid"] == result_record["id"]
                )
                reader = goalflight_journal.Journal.open_reader(project)
                repaired_envelope = _carrier_messages._listener_envelope(
                    reader,
                    row,
                    repair_authority=authority,
                    controller_label=label,
                )
                assert_true(
                    "listener recovers the exact carrier event",
                    repaired_envelope is not None
                    and repaired_envelope["id"] == result_record["id"]
                    and int(repaired_envelope["seq"]) == actual_seq,
                )
                assert_true(
                    "repair invalidates the old cursor snapshot",
                    _carrier_messages._cursor_positions_for_shown_rows(
                        [row], [(row, repaired_envelope)]
                    )
                    == {},
                )
                stored = authority.read_all(
                    """SELECT stream_seq FROM delivery_events
                       WHERE recipient_label = ? AND origin_node = ? AND event_uuid = ?""",
                    (label, result_record["source"]["node"], result_record["id"]),
                )
                assert [int(item["stream_seq"]) for item in stored] == [actual_seq]

                # A second repair request is idempotent even when it carries
                # the stale sequence from the original listener snapshot.
                repeated = authority._repair_delivery_event_sequence_through_safe_prefix(
                    recipient_label=label,
                    origin_node=result_record["source"]["node"],
                    event_uuid=result_record["id"],
                    stream_id=result_record["dispatch_id"],
                    old_stream_seq=assigned_seq,
                    stream_seq=actual_seq,
                    carrier_path=carrier,
                    event_type="result",
                    wake_class="waking",
                    created_at=result_record["ts"],
                )
                assert_true("repeated repair is idempotent", repeated.committed)

                fresh = authority.cursor_peek(label, nonce=claimed.value.nonce)
                delivered = _carrier_messages._envelopes_with_rows(
                    reader,
                    list(fresh.items),
                    repair_authority=authority,
                    controller_label=label,
                )
                positions = _carrier_messages._cursor_positions_for_shown_rows(
                    list(fresh.items), delivered
                )
                assert positions == {result_record["dispatch_id"]: actual_seq}
                advanced = authority.advance_cursor(
                    label,
                    nonce=claimed.value.nonce,
                    expected_cursor_version=fresh.cursor_version,
                    expected_stream_snapshots=fresh.stream_snapshots,
                    advances=positions,
                    actor=label,
                )
                assert_true("repaired event can be acknowledged", advanced.committed)
                assert not authority.cursor_peek(label, nonce=claimed.value.nonce).items


def test_listener_tags_carrier_identity_mismatch_as_stuck() -> None:
    import goalflight_journal

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        project = base / "project"
        init_git_project(project)
        env = _journal_test_env(base)
        messages_dir = Path(env["GOALFLIGHT_MESSAGES_DIR"])
        with mock.patch.dict(os.environ, env, clear=False):
            authority = goalflight_journal.open_or_create_journal(project)
            label = "identity-mismatch-controller"
            stream_id = "identity-mismatch-dispatch"
            event_uuid = str(_carrier_messages.uuid.uuid4())
            carrier = messages_dir / f"{stream_id}.jsonl"
            created_at = goalflight_journal.utc_now()
            envelope = {
                "schema": "goalflight.message.v1",
                "schema_version": 1,
                "id": event_uuid,
                "dispatch_id": stream_id,
                "seq": 1,
                "ts": created_at,
                "source": {
                    "node": "identity-mismatch-origin",
                    "adapter": "pytest",
                    "transport": "worker",
                },
                "type": "result",
                "priority": "normal",
                "payload": {"text": "carrier result"},
            }
            _carrier_add(carrier, envelope)
            _carrier_messages.update_envelopes(
                carrier,
                lambda rows: ([{**rows[0], "type": "controller-notice"}], None),
            )
            assigned = authority.record_delivery_event(
                recipient_label=label,
                origin_node="identity-mismatch-origin",
                event_uuid=event_uuid,
                stream_id=stream_id,
                stream_seq=1,
                carrier_path=carrier,
                event_type="result",
                wake_class="waking",
                created_at=created_at,
            )
            assert_true("identity-mismatch assignment recorded", assigned.committed)
            assert_true(
                "identity-mismatch assignment projected",
                authority.mark_delivery_projected(
                    recipient_label=label,
                    origin_node="identity-mismatch-origin",
                    event_uuid=event_uuid,
                ).committed,
            )

            row = next(
                item
                for item in authority.pending_delivery_events(label)
                if item["event_uuid"] == event_uuid
            )
            errors: list[dict[str, object]] = []
            assert _carrier_messages._envelopes_with_rows(
                authority,
                [row],
                controller_label=label,
                carrier_errors=errors,
            ) == []
            assert row.get("listener_status") == "STUCK"
            assert errors and errors[0].get("status") == "STUCK"
            assert _carrier_messages._cursor_positions_for_shown_rows([row], []) == {}


def test_projector_repairs_misassigned_sequence_and_rewinds_consumed_position() -> None:
    import goalflight_journal

    fixture = ROOT / "tests" / "fixtures" / "journal_outbox_carriers" / "result-only.jsonl"
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        project = base / "project"
        init_git_project(project)
        env = _journal_test_env(base)
        messages_dir = Path(env["GOALFLIGHT_MESSAGES_DIR"])
        with mock.patch.dict(os.environ, env, clear=False):
            authority = goalflight_journal.open_or_create_journal(project)
            records = [
                json.loads(line)
                for line in fixture.read_text(encoding="utf-8").splitlines()
            ]
            for record in records:
                if isinstance(record.get("addressee"), dict):
                    record["addressee"]["project_root"] = str(project.resolve())
            envelope = next(record for record in records if record["type"] == "result")
            label = str(envelope["addressee"]["label"])
            carrier = messages_dir / f"{envelope['dispatch_id']}.jsonl"
            for record in records:
                _carrier_add(carrier, record)

            assigned = authority.record_delivery_event(
                recipient_label=label,
                origin_node=envelope["source"]["node"],
                event_uuid=envelope["id"],
                stream_id=envelope["dispatch_id"],
                stream_seq=2,
                carrier_path=carrier,
                event_type="result",
                wake_class="waking",
                created_at=envelope["ts"],
            )
            assert_true("legacy sequence assignment recorded", assigned.committed)
            assert_true(
                "legacy sequence assignment projected",
                authority.mark_delivery_projected(
                    recipient_label=label,
                    origin_node=envelope["source"]["node"],
                    event_uuid=envelope["id"],
                ).committed,
            )
            stream_id = str(envelope["dispatch_id"])
            snapshot, _ = authority._read_with_retry(
                "test cursor setup snapshot",
                lambda connection: authority._cursor_stream_snapshot(
                    connection,
                    project_root=str(project.resolve()),
                    recipient_label=label,
                    stream_id=stream_id,
                    requested_position=2,
                ),
            )
            with authority._cursor_write_scope():
                assert authority._set_cursor_raw(
                    label,
                    positions={stream_id: 2},
                    expected_stream_snapshots={stream_id: snapshot},
                    actor=label,
                ).committed

            assignments = _carrier_messages._prepare_journal_delivery(envelope, carrier)
            assert len(assignments) == 1
            stored = authority.read_all(
                "SELECT stream_seq FROM delivery_events WHERE event_uuid = ?",
                (envelope["id"],),
            )
            assert [int(row["stream_seq"]) for row in stored] == [1]
            assert authority.cursor_status(label)["positions"] == {envelope["dispatch_id"]: 0}
            assert authority._cursor_rewinds(label) == {envelope["dispatch_id"]: 1}

            reader = goalflight_journal.Journal.open_reader(project)
            pending = authority.pending_delivery_events(label)
            delivered = _carrier_messages._envelopes_with_rows(
                reader,
                pending,
                repair_authority=authority,
                controller_label=label,
            )
            positions = _carrier_messages._cursor_positions_for_shown_rows(
                pending, delivered
            )
            assert positions == {envelope["dispatch_id"]: 1}


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


def test_supervise_failed_migration_reports_unverified_coverage() -> None:
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
    assert "wake coverage was not verified" in stderr.getvalue()
    assert "existing wake coverage retained" not in stderr.getvalue()


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
        assert kwargs["before_renewal"](ROOT, "migration-test", "resolved-migration-nonce") is None
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
        mock.patch.object(
            _carrier_messages,
            "_pid_in_caller_ancestry",
            return_value=False,
        ),
        mock.patch.object(
            _carrier_messages,
            "_pid_in_caller_process_group",
            return_value=False,
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
        assert kwargs["before_renewal"](ROOT, "migration-test", "resolved-migration-nonce") is None
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
        mock.patch.object(
            _carrier_messages,
            "_pid_in_caller_ancestry",
            return_value=False,
        ),
        mock.patch.object(
            _carrier_messages,
            "_pid_in_caller_process_group",
            return_value=False,
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
        assert kwargs["before_renewal"](ROOT, "migration-test", "resolved-migration-nonce") is None
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


def test_named_stream_read_skips_quota_advisory() -> None:
    """``read --dispatch-id X --last 1`` returns X, not a newer advisory on another stream."""
    import goalflight_messages as messages

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        messages_dir = base / "messages"
        fleet_dir = base / "fleet"
        named = "b5745-rebase"
        messages.post_message(
            dispatch_id=named,
            msg_type="user_need",
            payload={"text": "the named body"},
            messages_dir=messages_dir,
            seq=1,
        )
        messages.post_message(
            dispatch_id="other-stream",
            msg_type="user_need",
            payload={"text": "the other stream"},
            messages_dir=messages_dir,
            seq=1,
        )
        messages.post_message(
            dispatch_id="controller-quota-advisory",
            msg_type="advisory",
            payload={"text": "moonshot quota exhausted: 4 agent(s) stuck"},
            messages_dir=messages_dir,
            seq=1,
        )
        read = run_messages_cli(
            messages_dir,
            fleet_dir,
            ["read", "--dispatch-id", named, "--last", "1"],
        )
        assert_true(f"named read succeeds: {read.stderr}", read.returncode == 0)
        envelopes = json.loads(read.stdout)
        assert_true("named read returns one envelope", len(envelopes) == 1)
        assert_true(
            "named read returns that stream, not the advisory",
            envelopes[0].get("dispatch_id") == named
            and envelopes[0].get("payload", {}).get("text") == "the named body",
        )
        advisory = run_messages_cli(
            messages_dir,
            fleet_dir,
            ["read", "--dispatch-id", "controller-quota-advisory", "--last", "1"],
        )
        assert_true("naming the advisory still reads it", advisory.returncode == 0)
        advisory_envelopes = json.loads(advisory.stdout)
        assert_true(
            "advisory body is readable by name",
            len(advisory_envelopes) == 1
            and "quota exhausted" in advisory_envelopes[0]["payload"]["text"],
        )


def test_drain_bodies_prints_body_before_cursor_advance() -> None:
    """Text ``relay --drain --bodies`` prints the body, including when the CAS loses."""
    import goalflight_journal

    body = "visible headline\nsecond line\nUNIQUE-BODY-SENTINEL-9f3a"
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        project = base / "project"
        init_git_project(project)
        env = _journal_test_env(base)
        label = "drain-bodies-controller"
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
            mock.patch.object(
                _carrier_messages,
                "emit_wake_entry_notice",
                side_effect=AssertionError("drain must not emit a wake-entry notice"),
            ),
        ):
            authority = goalflight_journal.open_or_create_journal(project)
            claimed = authority.claim_or_renew_lease(
                label,
                principal={"principal_id": "drain-bodies-test"},
            )
            assert_true("bodies controller lease claimed", claimed.committed)
            _carrier_messages.post_message(
                dispatch_id="drain-bodies",
                msg_type="controller-notice",
                payload={"text": body},
                messages_dir=messages_dir,
                source={"node": "test", "adapter": "pytest", "transport": "controller"},
                addressee=_carrier_messages.controller_addressee(
                    label,
                    project_root=project,
                ),
            )
            lease = authority.active_lease(label)
            assert_true("bodies controller lease readable", lease is not None)
            argv = [
                "--messages-dir",
                str(messages_dir),
                "--fleet-dir",
                env["GOALFLIGHT_FLEET_DIR"],
                "relay",
                "--drain",
                "--bodies",
            ]
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                drained_rc = _carrier_messages.main(argv)
            rendered = stdout.getvalue()
            assert_true("drain --bodies succeeds", drained_rc == 0)
            assert_true(
                "drain --bodies prints the body the headline omits",
                "UNIQUE-BODY-SENTINEL-9f3a" in rendered,
            )
            assert_true(
                "body is printed before the cursor receipt",
                rendered.index("UNIQUE-BODY-SENTINEL-9f3a") < rendered.index("drained "),
            )
            assert_true(
                "drain --bodies still advances",
                not authority.cursor_peek(label, nonce=lease.nonce).items,
            )

            _carrier_messages.post_message(
                dispatch_id="drain-bodies-cas",
                msg_type="controller-notice",
                payload={"text": body},
                messages_dir=messages_dir,
                source={"node": "test", "adapter": "pytest", "transport": "controller"},
                addressee=_carrier_messages.controller_addressee(
                    label,
                    project_root=project,
                ),
            )
            before = authority.cursor_peek(label, nonce=lease.nonce, limit=10)

            def lose_cas(*_args, **_kwargs):
                raise goalflight_journal.CASMismatch("cursor moved")

            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                mock.patch.object(goalflight_journal.Journal, "advance_cursor", lose_cas),
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                conflict_rc = _carrier_messages.main(argv)
            combined = stdout.getvalue() + stderr.getvalue()
            assert_true("cas loss exits conflict", conflict_rc == 3)
            assert_true(
                "body was printed even though the cursor did not advance",
                "UNIQUE-BODY-SENTINEL-9f3a" in combined,
            )
            assert_true(
                "conflict is reported after the body",
                combined.index("UNIQUE-BODY-SENTINEL-9f3a")
                < combined.index("drain conflict · retry relay --drain"),
            )
            after = authority.cursor_peek(label, nonce=lease.nonce, limit=10)
            assert_true(
                "failed drain did not advance the cursor",
                after.cursor_version == before.cursor_version
                and len(after.items) == len(before.items),
            )


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
