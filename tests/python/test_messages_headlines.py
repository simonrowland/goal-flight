"""Production-shaped coverage for scannable ``relay --new`` headlines."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "goalflight_messages.py"
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import goalflight_messages as msg  # noqa: E402
import goalflight_journal as journal  # noqa: E402
import goalflight_quota_stuck as quota  # noqa: E402
import goalflight_status as status  # noqa: E402


CONTROLLER_LABEL = "headlines"
LEFTOVER_DISPATCH_IDS = (None, "leftover-worker-id")


def _expected_declared_label(leftover_dispatch_id: str | None) -> str:
    # A leftover worker id cannot establish a controller name; the stamp is
    # UNKNOWN rather than an omitted field or an inherited label.
    return "UNKNOWN" if leftover_dispatch_id else CONTROLLER_LABEL


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    return project


def _test_env(
    tmp_path: Path, *, leftover_dispatch_id: str | None = None
) -> dict[str, str]:
    env = os.environ.copy()
    journal_dir = tmp_path / "journals"
    state_dir = tmp_path / "state"
    wake_dir = tmp_path / "wake-ledger"
    messages_dir = tmp_path / "messages"
    tasks_dir = tmp_path / "tasks"
    pid_dir = tmp_path / "pids"
    gf_pid_dir = tmp_path / "gf-pids"
    for path in (
        journal_dir,
        state_dir,
        wake_dir,
        messages_dir,
        tasks_dir,
        pid_dir,
        gf_pid_dir,
    ):
        path.mkdir(parents=True, exist_ok=True)
    env.update(
        {
            "GOALFLIGHT_MESSAGES_DIR": str(messages_dir),
            "GOALFLIGHT_JOURNAL_DIR": str(journal_dir),
            "GOALFLIGHT_STATE_DIR": str(state_dir),
            "GOALFLIGHT_WAKE_LEDGER": str(tmp_path / "wake-ledger.json"),
            "GOALFLIGHT_WAKE_LEDGER_DIR": str(wake_dir),
            "GOALFLIGHT_TASK_STORE": str(tasks_dir),
            "GOALFLIGHT_TASK_STORE_DIR": str(tasks_dir),
            "GOALFLIGHT_PIDFILE_DIR": str(pid_dir),
            "GOAL_FLIGHT_PIDFILE_DIR": str(gf_pid_dir),
            "GOALFLIGHT_CAPACITY_CONF": "/dev/null",
            "GOALFLIGHT_CONTROLLER_LABEL": CONTROLLER_LABEL,
            "GOALFLIGHT_TEST_MODE": "1",
        }
    )
    if leftover_dispatch_id:
        env["GOALFLIGHT_DISPATCH_ID"] = leftover_dispatch_id
    else:
        env.pop("GOALFLIGHT_DISPATCH_ID", None)
    return env


def _ensure_controller(tmp_path: Path) -> None:
    with mock.patch.dict(os.environ, _test_env(tmp_path), clear=False):
        authority = journal.open_or_create_journal(_project(tmp_path))
        active = authority.active_lease(CONTROLLER_LABEL)
        result = authority.claim_or_renew_lease(
            CONTROLLER_LABEL,
            principal={"principal_id": "headline-test-controller"},
            nonce=active.nonce if active is not None else None,
        )
    assert result.committed, result.reason


def _post_env(
    tmp_path: Path,
    text: str,
    *,
    subject: str | None = None,
    dispatch_id: str = "d1",
    msg_type: str = "controller-notice",
    adapter: str = "codex",
    leftover_dispatch_id: str | None = None,
) -> dict:
    _ensure_controller(tmp_path)
    project = _project(tmp_path)
    argv = [
        sys.executable,
        str(SCRIPT),
        "--messages-dir",
        str(tmp_path / "messages"),
        "--fleet-dir",
        str(tmp_path / "fleet"),
        "post",
        "--dispatch-id",
        dispatch_id,
        "--type",
        msg_type,
        "--text",
        text,
        "--to-controller",
        CONTROLLER_LABEL,
        "--controller-project-root",
        str(project),
        "--json",
    ]
    # Omit flags that match CLI defaults so adapter="unknown" is the
    # documented send command (no --adapter), not a lookalike.
    if adapter != "unknown":
        argv.extend(["--adapter", adapter])
    if subject is not None:
        argv.extend(["--subject", subject])
    posted = subprocess.run(
        argv,
        cwd=project,
        env=_test_env(tmp_path, leftover_dispatch_id=leftover_dispatch_id),
        text=True,
        capture_output=True,
        check=False,
    )
    assert posted.returncode == 0, posted.stderr
    return json.loads(posted.stdout)["envelope"]


def _post_env_raw(
    tmp_path: Path,
    text: str,
    *,
    dispatch_id: str,
    msg_type: str = "controller-notice",
) -> subprocess.CompletedProcess[str]:
    """Post without asserting success -- for inputs the API must refuse."""
    _ensure_controller(tmp_path)
    project = _project(tmp_path)
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--messages-dir",
            str(tmp_path / "messages"),
            "--fleet-dir",
            str(tmp_path / "fleet"),
            "post",
            "--dispatch-id",
            dispatch_id,
            "--type",
            msg_type,
            "--text",
            text,
            "--node",
            "local",
            "--adapter",
            "codex",
            "--transport",
            "controller",
            "--to-controller",
            CONTROLLER_LABEL,
            "--controller-project-root",
            str(project),
            "--json",
        ],
        cwd=project,
        env=_test_env(tmp_path),
        text=True,
        capture_output=True,
        check=False,
    )


def _run_relay(
    tmp_path: Path,
    *,
    bodies: bool = False,
    leftover_dispatch_id: str | None = None,
) -> subprocess.CompletedProcess[str]:
    fleet_dir = tmp_path / "fleet"
    fleet_dir.mkdir(exist_ok=True)
    argv = [
        sys.executable,
        str(SCRIPT),
        "--messages-dir",
        str(tmp_path / "messages"),
        "--fleet-dir",
        str(fleet_dir),
        "relay",
        "--new",
    ]
    if bodies:
        argv.append("--bodies")
    return subprocess.run(
        argv,
        cwd=_project(tmp_path),
        env=_test_env(tmp_path, leftover_dispatch_id=leftover_dispatch_id),
        text=True,
        capture_output=True,
        check=False,
    )


def test_explicit_subject_wins_over_body(tmp_path: Path) -> None:
    envelope = _post_env(
        tmp_path,
        "line one\nline two",
        subject="Gate green",
    )
    assert msg.envelope_headline(envelope) == "Gate green"


def test_headline_falls_back_to_first_two_meaningful_lines(tmp_path: Path) -> None:
    envelope = _post_env(
        tmp_path,
        "\n\n  RESOLVED — root cause found  \nseat daemon PATH\nthird line",
    )
    assert (
        msg.envelope_headline(envelope)
        == "RESOLVED — root cause found / seat daemon PATH"
    )


def test_headline_is_bounded_and_single_line(tmp_path: Path) -> None:
    head = msg.envelope_headline(_post_env(tmp_path, "x" * 500))
    assert len(head) <= msg.HEADLINE_MAX
    assert "\n" not in head

    multi = msg.envelope_headline(
        _post_env(tmp_path, "a\nb\nc", dispatch_id="multi")
    )
    assert multi == "a / b"


def test_empty_body_is_labelled_not_blank(tmp_path: Path) -> None:
    assert msg.envelope_headline(_post_env(tmp_path, "")) == "(no text)"
    assert msg.envelope_headline({"payload": None}) == "(no text)"


def test_listing_carries_size_without_dumping_body(tmp_path: Path) -> None:
    fragment = "DISTINCTIVE-BODY-FRAGMENT:"
    body = fragment + ("y" * (2048 - len(fragment)))
    envelope = _post_env(
        tmp_path,
        body,
        subject="Big one",
        dispatch_id="proj",
    )
    out = msg.format_envelope_headlines([envelope])
    assert out == "proj #1 [controller-notice] from codex: Big one  (2048c)"
    assert fragment not in out


def test_malformed_entries_do_not_break_the_listing(tmp_path: Path) -> None:
    envelope = _post_env(tmp_path, "ok", dispatch_id="d")
    out = msg.format_envelope_headlines(["not-a-dict", None, envelope])
    assert out.count("\n") == 0 and "d #1" in out


def test_from_prefers_recorded_source_over_inbox_id(tmp_path: Path) -> None:
    envelope = _post_env(tmp_path, "body", dispatch_id="proj-inbox")
    assert msg.envelope_from(envelope) == "codex"

    envelope["source"]["adapter"] = "unknown"
    # The stamped controller label outranks an uninformative node: "local"
    # says nothing about which controller posted.
    assert msg.envelope_from(envelope) == CONTROLLER_LABEL
    envelope["source"] = {}
    assert msg.envelope_from(envelope) == "proj-inbox"


def test_from_renders_unknown_for_unattributed_controller_mail(tmp_path: Path) -> None:
    del tmp_path
    # Pre-attribution records carry no label; controller mail must render the
    # explicit sentinel rather than read the node ("local") as the sender.
    legacy = {
        "dispatch_id": "d",
        "source": {"node": "local", "adapter": "unknown", "transport": "controller"},
    }
    assert msg.envelope_from(legacy) == msg.UNKNOWN_CONTROLLER_LABEL == "UNKNOWN"
    # Non-controller transports keep the node/inbox fallback chain.
    worker = {
        "dispatch_id": "d",
        "source": {"node": "worker-box", "adapter": "unknown", "transport": "tail_file"},
    }
    assert msg.envelope_from(worker) == "worker-box"
    # An informative adapter still wins over everything below it.
    fleet = {
        "dispatch_id": "d",
        "source": {"node": "local", "adapter": "fleet", "transport": "controller"},
    }
    assert msg.envelope_from(fleet) == "fleet"


def test_labelled_controller_post_round_trips_label_to_relay(tmp_path: Path) -> None:
    # The documented send command (no --adapter flag) from a shell carrying
    # the controller label: stamped at ingress, then survives validation,
    # canonical serialization, carrier append, and the relay readback -- the
    # same normalization production uses, not a constructed envelope.
    envelope = _post_env(
        tmp_path,
        "pass-1 vs pass-2 queue counts disagree",
        subject="dispatch-queue finding",
        dispatch_id="queue-audit",
        msg_type="finding",
        adapter="unknown",
    )
    assert envelope["source"]["controller_label"] == CONTROLLER_LABEL

    bodies = _run_relay(tmp_path, bodies=True)
    assert bodies.returncode == 0, bodies.stderr
    round_tripped = json.loads(bodies.stdout.splitlines()[0])
    assert round_tripped[0]["source"]["controller_label"] == CONTROLLER_LABEL

    headlines = _run_relay(tmp_path)
    assert headlines.returncode == 0, headlines.stderr
    assert headlines.stdout.splitlines()[0].startswith(
        f"queue-audit #1 [finding] from {CONTROLLER_LABEL}: "
    )


def test_unestablishable_sender_is_stamped_and_rendered_unknown(tmp_path: Path) -> None:
    # A controller bash-tool shell drops the session identity variables (the
    # relay side documents the same drop). The post must still attribute:
    # explicitly UNKNOWN, never absent. Presence of the field with the
    # sentinel value is what distinguishes this from the absent-field defect
    # being fixed.
    _ensure_controller(tmp_path)
    project = _project(tmp_path)
    send_env = {
        key: value
        for key, value in _test_env(tmp_path).items()
        if not key.startswith("GOALFLIGHT_CONTROLLER")
    }
    send_env.pop("GOALFLIGHT_DISPATCH_ID", None)
    posted = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--messages-dir",
            str(tmp_path / "messages"),
            "--fleet-dir",
            str(tmp_path / "fleet"),
            "post",
            "--dispatch-id",
            "attr-topic",
            "--type",
            "finding",
            "--text",
            "unattributed shell post",
            "--to-controller",
            CONTROLLER_LABEL,
            "--controller-project-root",
            str(project),
            "--json",
        ],
        cwd=project,
        env=send_env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert posted.returncode == 0, posted.stderr
    envelope = json.loads(posted.stdout)["envelope"]
    assert envelope["source"]["controller_label"] == "UNKNOWN"

    relay = _run_relay(tmp_path)
    assert relay.returncode == 0, relay.stderr
    first = relay.stdout.splitlines()[0]
    assert first.startswith("attr-topic #1 [finding] from UNKNOWN: ")


def test_preexisting_unlabelled_record_renders_unknown_without_crashing(
    tmp_path: Path,
) -> None:
    # Journals written before this fix hold controller mail with no
    # source.controller_label at all. Relay must keep the row visible, render
    # the sender as UNKNOWN (never a node/inbox guess, never a pid or repo
    # name), and exit 0. New posts stamp; this test strips the field after
    # admission so the reader seam is exercised on the legacy shape.
    _ensure_controller(tmp_path)
    project = _project(tmp_path)
    with mock.patch.dict(os.environ, _test_env(tmp_path), clear=False):
        result = msg.post_message(
            dispatch_id="legacy-topic",
            msg_type="finding",
            payload={"text": "pre-attribution record"},
            messages_dir=tmp_path / "messages",
            source={"node": "local", "adapter": "unknown", "transport": "controller"},
            addressee=msg.controller_addressee(CONTROLLER_LABEL, project_root=project),
        )
    path = msg.inbox_path(tmp_path / "messages", "legacy-topic")

    def _strip(existing: list[dict]):
        for item in existing:
            source = item.get("source")
            if isinstance(source, dict):
                source.pop("controller_label", None)
        return existing, None

    msg.update_envelopes(path, _strip)
    planted = msg.read_envelopes(path)
    assert planted and "controller_label" not in planted[0]["source"]

    relay = _run_relay(tmp_path)
    assert relay.returncode == 0, relay.stderr
    first = relay.stdout.splitlines()[0]
    assert first.startswith("legacy-topic #1 [finding] from UNKNOWN: ")
    assert "headlines" not in first.split(":", 1)[0]


def test_mcp_ingress_stamps_the_same_attribution(tmp_path: Path) -> None:
    # The MCP tool is the same post through a second ingress; given the same
    # identity environment it must stamp the same bytes as the CLI, and the
    # label must survive relay the same way.
    _ensure_controller(tmp_path)
    project = _project(tmp_path)
    addressee = msg.controller_addressee(CONTROLLER_LABEL, project_root=project)
    # Replace the process env: patch.dict(clear=False) cannot drop an ambient
    # worker GOALFLIGHT_DISPATCH_ID, and that identity refuses to stamp.
    with mock.patch.dict(os.environ, _test_env(tmp_path), clear=True):
        result = msg.goalflight_post_message_tool(
            {
                "dispatch_id": "mcp-topic",
                "type": "finding",
                "payload": {"text": "via mcp"},
                "addressee": addressee,
            },
            messages_dir=tmp_path / "messages",
        )
        assert result["envelope"]["source"]["controller_label"] == CONTROLLER_LABEL
        assert result["envelope"]["source"]["adapter"] == "unknown"
        assert result["envelope"]["source"]["transport"] == "controller"

        # Non-controller transports are not controller mail: no stamp.
        tail = msg.goalflight_post_message_tool(
            {
                "dispatch_id": "mcp-tail",
                "type": "controller-notice",
                "payload": {"text": "worker harvest"},
                "source": {"node": "local", "adapter": "acp", "transport": "tail_file"},
            },
            messages_dir=tmp_path / "messages",
        )
        assert "controller_label" not in tail["envelope"]["source"]

        # A caller-supplied label is descriptive metadata but must still be a
        # bounded string; junk is refused at ingress like any source field.
        try:
            msg.goalflight_post_message_tool(
                {
                    "dispatch_id": "mcp-junk",
                    "type": "controller-notice",
                    "payload": {"text": "junk"},
                    "source": {
                        "node": "local",
                        "adapter": "acp",
                        "transport": "controller",
                        "controller_label": 137,
                    },
                },
                messages_dir=tmp_path / "messages",
            )
        except msg.MessageError:
            pass
        else:
            raise AssertionError("non-string controller_label must be refused")

    headlines = _run_relay(tmp_path)
    assert headlines.returncode == 0, headlines.stderr
    assert headlines.stdout.splitlines()[0].startswith(
        f"mcp-topic #1 [finding] from {CONTROLLER_LABEL}: "
    )


@pytest.mark.parametrize("leftover_dispatch_id", LEFTOVER_DISPATCH_IDS)
def test_cli_post_under_dispatch_id_stamps_and_round_trips(
    tmp_path: Path, leftover_dispatch_id: str | None
) -> None:
    # Incident shape: leftover GOALFLIGHT_DISPATCH_ID used to skip the stamp
    # and omit the field. The field must be present either way.
    expected = _expected_declared_label(leftover_dispatch_id)
    envelope = _post_env(
        tmp_path,
        "incident-shaped leftover dispatch id",
        subject="cli leftover",
        dispatch_id="cli-leftover",
        msg_type="finding",
        adapter="unknown",
        leftover_dispatch_id=leftover_dispatch_id,
    )
    assert envelope["source"]["controller_label"] == expected
    headlines = _run_relay(tmp_path, leftover_dispatch_id=leftover_dispatch_id)
    assert headlines.returncode == 0, headlines.stderr
    assert headlines.stdout.splitlines()[0].startswith(
        f"cli-leftover #1 [finding] from {expected}: "
    )


@pytest.mark.parametrize("leftover_dispatch_id", LEFTOVER_DISPATCH_IDS)
def test_post_message_primitive_stamps_and_round_trips(
    tmp_path: Path, leftover_dispatch_id: str | None
) -> None:
    expected = _expected_declared_label(leftover_dispatch_id)
    _ensure_controller(tmp_path)
    project = _project(tmp_path)
    with mock.patch.dict(
        os.environ,
        _test_env(tmp_path, leftover_dispatch_id=leftover_dispatch_id),
        clear=True,
    ):
        result = msg.post_message(
            dispatch_id="primitive-topic",
            msg_type="finding",
            payload={"text": "library admit path", "subject": "primitive stamp"},
            messages_dir=tmp_path / "messages",
            addressee=msg.controller_addressee(CONTROLLER_LABEL, project_root=project),
        )
    assert result["envelope"]["source"]["controller_label"] == expected
    headlines = _run_relay(tmp_path, leftover_dispatch_id=leftover_dispatch_id)
    assert headlines.returncode == 0, headlines.stderr
    assert headlines.stdout.splitlines()[0].startswith(
        f"primitive-topic #1 [finding] from {expected}: "
    )


@pytest.mark.parametrize("leftover_dispatch_id", LEFTOVER_DISPATCH_IDS)
def test_write_steering_envelope_stamps_and_round_trips(
    tmp_path: Path, leftover_dispatch_id: str | None
) -> None:
    expected = _expected_declared_label(leftover_dispatch_id)
    with mock.patch.dict(
        os.environ,
        _test_env(tmp_path, leftover_dispatch_id=leftover_dispatch_id),
        clear=True,
    ):
        envelope = msg.write_steering_envelope(
            tmp_path / "fleet",
            audit_id="audit-1",
            proposal_id="prop-1",
            patch=[{"op": "replace", "path": "/n", "value": 1}],
            after_hash="abc",
            messages_dir=tmp_path / "messages",
        )
    assert envelope["source"]["transport"] == "controller"
    assert envelope["source"]["adapter"] == "fleet"
    assert envelope["source"]["controller_label"] == expected
    rendered = msg.format_envelope_headlines([envelope])
    who = msg.envelope_from(envelope)
    assert f" from {who}:" in rendered
    assert expected in json.dumps(envelope["source"])
    assert " from local:" not in rendered
    assert who != "local"


@pytest.mark.parametrize("leftover_dispatch_id", LEFTOVER_DISPATCH_IDS)
def test_quota_advisory_stamps_and_round_trips(
    tmp_path: Path, leftover_dispatch_id: str | None
) -> None:
    expected = _expected_declared_label(leftover_dispatch_id)
    payload = {
        "schema": "goalflight.status.aggregate.v1",
        "scope": {"project_root": str(tmp_path), "machine_active_leases": 0},
        "capacity": {"operating_cap": 10},
        "capacity_state": {"leases": {}, "cooldowns": {}},
        "dispatch": {"records": []},
        "rate_pressure": {
            "providers_under_pressure": [
                {
                    "scope": "provider",
                    "provider": "xai",
                    "budget_key": "provider:xai",
                    "labels": ["grok-code"],
                    "count": 3,
                    "threshold": 3,
                    "quota_hard_stop": True,
                    "effective_caps": {"grok-code": 0},
                    "stuck_worker_count": 1,
                    "stuck_workers": [
                        {
                            "dispatch_id": "quota-mail",
                            "agent": "grok-code",
                            "signature": "usage balance exhausted",
                            "limit_kind": "exhausted",
                        }
                    ],
                }
            ],
            "window_seconds": 600,
        },
    }
    with mock.patch.dict(
        os.environ,
        _test_env(tmp_path, leftover_dispatch_id=leftover_dispatch_id),
        clear=True,
    ):
        status._post_quota_advisories(payload)
        inbox = msg.inbox_path(
            tmp_path / "messages", quota.QUOTA_STUCK_CONTROLLER_DISPATCH_ID
        )
        envelopes = msg.read_envelopes(inbox)
    assert len(envelopes) == 1
    assert envelopes[0]["source"]["controller_label"] == expected
    assert envelopes[0]["source"]["adapter"] == "goalflight_status"
    rendered = msg.format_envelope_headlines(envelopes)
    who = msg.envelope_from(envelopes[0])
    assert f" from {who}:" in rendered
    assert expected in json.dumps(envelopes[0]["source"])
    assert " from local:" not in rendered
    assert who != "local"


@pytest.mark.parametrize("leftover_dispatch_id", LEFTOVER_DISPATCH_IDS)
def test_pid_without_label_stamps_unknown_not_repo_name(
    tmp_path: Path, leftover_dispatch_id: str | None
) -> None:
    # The inventing fallback: PID present, LABEL dropped, resolver used to
    # stamp the git directory name as the sender. Must be UNKNOWN, never
    # that name, never the pid.
    repo_name = "probable-sender-repo"
    project = tmp_path / repo_name
    project.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    fake_pid = "424242"
    with mock.patch.dict(os.environ, _test_env(tmp_path), clear=False):
        authority = journal.open_or_create_journal(project)
        active = authority.active_lease(CONTROLLER_LABEL)
        result = authority.claim_or_renew_lease(
            CONTROLLER_LABEL,
            principal={"principal_id": "headline-test-controller"},
            nonce=active.nonce if active is not None else None,
        )
    assert result.committed, result.reason
    send_env = _test_env(tmp_path, leftover_dispatch_id=leftover_dispatch_id)
    for key in list(send_env):
        if key.startswith("GOALFLIGHT_CONTROLLER"):
            send_env.pop(key, None)
    send_env["GOALFLIGHT_CONTROLLER_PID"] = fake_pid
    posted = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--messages-dir",
            str(tmp_path / "messages"),
            "--fleet-dir",
            str(tmp_path / "fleet"),
            "post",
            "--dispatch-id",
            "pid-fallback",
            "--type",
            "finding",
            "--text",
            "pid without label",
            "--to-controller",
            CONTROLLER_LABEL,
            "--controller-project-root",
            str(project),
            "--json",
        ],
        cwd=project,
        env=send_env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert posted.returncode == 0, posted.stderr
    envelope = json.loads(posted.stdout)["envelope"]
    label = envelope["source"]["controller_label"]
    assert label == "UNKNOWN"
    assert label != repo_name
    assert fake_pid not in label
    rendered = msg.envelope_from(envelope)
    assert rendered == "UNKNOWN"
    assert rendered != repo_name
    assert fake_pid not in rendered


@pytest.mark.parametrize("leftover_dispatch_id", LEFTOVER_DISPATCH_IDS)
def test_supplied_label_is_trusted_not_lease_validated(
    tmp_path: Path, leftover_dispatch_id: str | None
) -> None:
    # Deliberate policy: a caller-supplied label is believed. The lease
    # holder here is CONTROLLER_LABEL; the supplied name is not that
    # holder. Proof remains author_digest, which this post does not mint.
    _ensure_controller(tmp_path)
    project = _project(tmp_path)
    impostor = "NOT-THE-LEASE-HOLDER"
    with mock.patch.dict(
        os.environ,
        _test_env(tmp_path, leftover_dispatch_id=leftover_dispatch_id),
        clear=True,
    ):
        result = msg.post_message(
            dispatch_id="trust-topic",
            msg_type="finding",
            payload={"text": "impostor label", "subject": "trusted stamp"},
            messages_dir=tmp_path / "messages",
            source={
                "node": "local",
                "adapter": "unknown",
                "transport": "controller",
                "controller_label": impostor,
            },
            addressee=msg.controller_addressee(CONTROLLER_LABEL, project_root=project),
        )
    assert result["envelope"]["source"]["controller_label"] == impostor
    assert "author_digest" not in result["envelope"]
    headlines = _run_relay(tmp_path, leftover_dispatch_id=leftover_dispatch_id)
    assert headlines.returncode == 0, headlines.stderr
    assert headlines.stdout.splitlines()[0].startswith(
        f"trust-topic #1 [finding] from {impostor}: "
    )


def test_default_and_bodies_cli_paths_round_trip_subject(tmp_path: Path) -> None:
    fragment = "CLI-BODY-LEAK-SENTINEL:"
    body = fragment + ("z" * (2048 - len(fragment)))
    envelope = _post_env(
        tmp_path,
        body,
        subject="Gate green",
        dispatch_id="kiln",
    )

    headlines = _run_relay(tmp_path)
    assert headlines.returncode == 0, headlines.stderr
    assert headlines.stdout.splitlines() == [
        "kiln #1 [controller-notice] from codex: Gate green  (2048c)",
        "bodies: re-run with --bodies",
        "pending counts: kiln=1",
    ]
    assert fragment not in headlines.stdout

    bodies = _run_relay(tmp_path, bodies=True)
    assert bodies.returncode == 0, bodies.stderr
    relayed = json.loads(bodies.stdout.splitlines()[0])
    assert relayed == [envelope]
    assert relayed[0]["payload"]["subject"] == "Gate green"


def test_default_cli_sanitizes_source_and_body_controls(tmp_path: Path) -> None:
    _post_env(
        tmp_path,
        "worker said \x1b[2Jclear",
        dispatch_id="mail",
        adapter="codex\nFORGED",
    )

    result = _run_relay(tmp_path)

    assert result.returncode == 0, result.stderr
    assert "\x1b" not in result.stdout
    assert "from codex FORGED:" in result.stdout
    assert r"\x1b[2J" in result.stdout
    assert "FORGED\n" not in result.stdout


def test_relay_new_sanitizes_full_stdout_structure(tmp_path: Path) -> None:
    # D3 closed the stream-name forgery vector at ingress: a dispatch_id
    # carrying a newline can no longer be posted at all, so it can no longer
    # reach stdout. The body stays free text, so the sanitizer is still the
    # only thing standing between hostile content and the rendered structure.
    body = "first line\nFORGED-BODY\x1b[2J\x9b31m"
    _post_env(
        tmp_path,
        body,
        dispatch_id="safe",
    )

    result = _run_relay(tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        (
            r"safe #1 [controller-notice] from codex: "
            rf"first line / FORGED-BODY\x1b[2J\x9b31m  ({len(body)}c)"
        ),
        "bodies: re-run with --bodies",
        "pending counts: safe=1",
    ]
    assert "\x1b" not in result.stdout
    assert "\x9b" not in result.stdout


def test_forged_stream_name_is_refused_at_ingress(tmp_path: Path) -> None:
    # The vector the test above used to exercise, asserted at its new home:
    # refused when posted, rather than sanitized after being accepted.
    proc = _post_env_raw(tmp_path, "body", dispatch_id="safe\nFORGED-COUNT")

    assert proc.returncode != 0, proc.stdout
    assert "dispatch_id" in (proc.stderr + proc.stdout)
    assert not (tmp_path / "messages").exists() or not list(
        (tmp_path / "messages").rglob("*FORGED*")
    )


def test_one_sanitizer_covers_dispatch_type_and_subject(tmp_path: Path) -> None:
    # dispatch_id and type are now refused at ingress (D3/D4), so the only way
    # hostile values in those fields can reach the formatter is a stream that
    # was written by something other than this API -- a stale file from an
    # older build, or a hand-planted one. The formatter is therefore still
    # responsible for them, and this asserts it on an envelope carrying all
    # three vectors at once: the one sanitizer covers dispatch, type, and
    # subject. The posted envelope supplies the real shape; the hostile values
    # are substituted into it rather than smuggled through a post the API
    # would (correctly) reject.
    envelope = dict(
        _post_env(tmp_path, "body", subject="Gate\n\x9b2Jgreen", dispatch_id="safe")
    )
    envelope["dispatch_id"] = "safe\nFORGED"
    envelope["type"] = "status\rINJECTED"

    out = msg.format_envelope_headlines([envelope])

    assert out.count("\n") == 0
    assert "\x9b" not in out
    assert "safe FORGED" in out
    assert "[status INJECTED]" in out
    assert r"Gate \x9b2Jgreen" in out


def test_forged_type_is_refused_at_ingress(tmp_path: Path) -> None:
    proc = _post_env_raw(
        tmp_path, "body", dispatch_id="safe", msg_type="status\rINJECTED"
    )

    assert proc.returncode != 0, proc.stdout
    assert "type" in (proc.stderr + proc.stdout)


def main() -> None:
    tests = (
        test_explicit_subject_wins_over_body,
        test_headline_falls_back_to_first_two_meaningful_lines,
        test_headline_is_bounded_and_single_line,
        test_empty_body_is_labelled_not_blank,
        test_listing_carries_size_without_dumping_body,
        test_malformed_entries_do_not_break_the_listing,
        test_from_prefers_recorded_source_over_inbox_id,
        test_from_renders_unknown_for_unattributed_controller_mail,
        test_labelled_controller_post_round_trips_label_to_relay,
        test_unestablishable_sender_is_stamped_and_rendered_unknown,
        test_preexisting_unlabelled_record_renders_unknown_without_crashing,
        test_mcp_ingress_stamps_the_same_attribution,
        test_default_and_bodies_cli_paths_round_trip_subject,
        test_default_cli_sanitizes_source_and_body_controls,
        test_relay_new_sanitizes_full_stdout_structure,
        test_forged_stream_name_is_refused_at_ingress,
        test_one_sanitizer_covers_dispatch_type_and_subject,
        test_forged_type_is_refused_at_ingress,
    )
    for test in tests:
        with tempfile.TemporaryDirectory() as td:
            test(Path(td))
        print(f"PASS {test.__name__}")
    parametrized = (
        test_cli_post_under_dispatch_id_stamps_and_round_trips,
        test_post_message_primitive_stamps_and_round_trips,
        test_write_steering_envelope_stamps_and_round_trips,
        test_quota_advisory_stamps_and_round_trips,
        test_pid_without_label_stamps_unknown_not_repo_name,
        test_supplied_label_is_trusted_not_lease_validated,
    )
    for test in parametrized:
        for leftover in LEFTOVER_DISPATCH_IDS:
            with tempfile.TemporaryDirectory() as td:
                test(Path(td), leftover)
            print(f"PASS {test.__name__} leftover={leftover!r}")


if __name__ == "__main__":
    main()


def _dated(ts, did="d", seq=1, body="body text"):
    return {
        "dispatch_id": did, "seq": seq, "type": "status", "ts": ts,
        "payload": {"text": body},
    }


def test_bodies_are_withheld_for_stale_mail():
    """An unacked envelope reappears on every check, so a backlog nobody acks
    re-floods the reader forever. Bodies are for mail you have not seen."""
    import datetime as _dt
    now = _dt.datetime(2026, 7, 28, 12, 0, tzinfo=_dt.timezone.utc)
    fresh_ts = (now - _dt.timedelta(hours=3)).isoformat()
    stale_ts = (now - _dt.timedelta(days=5)).isoformat()

    fresh, stale = msg.split_fresh_and_stale(
        [_dated(fresh_ts, did="new"), _dated(stale_ts, did="old")],
        now=now.timestamp(),
    )
    assert [e["dispatch_id"] for e in fresh] == ["new"]
    assert [e["dispatch_id"] for e in stale] == ["old"]


def test_boundary_is_inclusive_of_recent_mail():
    import datetime as _dt
    now = _dt.datetime(2026, 7, 28, 12, 0, tzinfo=_dt.timezone.utc)
    just_inside = (now - _dt.timedelta(seconds=msg.STALE_BODY_AGE_S - 60)).isoformat()
    just_outside = (now - _dt.timedelta(seconds=msg.STALE_BODY_AGE_S + 60)).isoformat()
    fresh, stale = msg.split_fresh_and_stale(
        [_dated(just_inside, did="in"), _dated(just_outside, did="out")],
        now=now.timestamp(),
    )
    assert [e["dispatch_id"] for e in fresh] == ["in"]
    assert [e["dispatch_id"] for e in stale] == ["out"]


def test_undateable_mail_is_treated_as_fresh():
    """Withholding a body we cannot date would silently hide NEW mail, which is
    the worse failure. Fail toward showing it."""
    for bad_ts in (None, "", "not-a-date", 12345):
        env = {"dispatch_id": "x", "seq": 1, "type": "status", "payload": {"text": "b"}}
        if bad_ts is not None:
            env["ts"] = bad_ts
        fresh, stale = msg.split_fresh_and_stale([env])
        assert len(fresh) == 1 and not stale, bad_ts


def test_naive_timestamps_do_not_crash_the_split():
    fresh, stale = msg.split_fresh_and_stale([_dated("2026-07-28T12:00:00")])
    assert len(fresh) + len(stale) == 1
