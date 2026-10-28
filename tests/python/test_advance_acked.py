"""Advancing the cursor should not require transcribing what a peek already knows.

Clearing nine read items previously meant supplying three CAS inputs by hand —
`--cursor-version`, a `--stream-snapshot STREAM=TOKEN` per stream, and a
`--position STREAM=SEQ` per stream. All three come from the same peek, and the
tokens are opaque hashes printed only by the doorbell, so a controller that had
already read its mail had to re-derive them: read the journal for the version,
import the module for the tokens, then assemble a nine-pair command line. That is
friction without safety — the compare-and-swap is what makes the write safe, and
it still runs either way.

`--acked` uses the most recent relay display for the same controller session.
These tests cover that display boundary, the explicit CAS path, and refusals.
"""

from __future__ import annotations

import contextlib
import ast
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import goalflight_journal as journal  # noqa: E402
import goalflight_messages as gm  # noqa: E402


def test_acked_positions_uses_only_the_safe_shown_prefix() -> None:
    rows = [
        {"stream_id": "alpha", "stream_seq": 1},
        {"stream_id": "alpha", "stream_seq": 2},
        {"stream_id": "alpha", "stream_seq": 3, "listener_status": "STUCK"},
        {"stream_id": "alpha", "stream_seq": 4},
        {"stream_id": "beta", "stream_seq": 7},
    ]
    shown = [(rows[0], {}), (rows[1], {}), (rows[3], {}), (rows[4], {})]
    assert gm._acked_positions(rows, shown) == {"alpha": 2, "beta": 7}


def test_acked_positions_preserves_ordered_max_and_empty_result() -> None:
    rows = [
        {"stream_id": "alpha", "stream_seq": 8},
        {"stream_id": "alpha", "stream_seq": 2},
        {"stream_id": "beta", "stream_seq": 5},
    ]
    shown = [(row, {}) for row in rows]
    assert gm._acked_positions(rows, shown) == {"alpha": 8, "beta": 5}
    assert gm._acked_positions([], []) == {}


def test_acked_positions_skips_missing_none_and_malformed_sequences() -> None:
    rows = [
        {"stream_id": "alpha"},
        {"stream_id": "alpha", "stream_seq": None},
        {"stream_id": "alpha", "stream_seq": "bad"},
        {"stream_id": "alpha", "stream_seq": 4},
    ]
    shown = [(row, {}) for row in rows]
    assert gm._acked_positions(rows, shown) == {"alpha": 4}


def _cursor_write_violations(source: str, filename: str) -> list[str]:
    raw_methods = {
        "_advance_cursor_raw",
        "_set_cursor_raw",
        "_repair_delivery_event_sequence",
    }
    writer_method = "_write_cursor_positions"
    scope_method = "_cursor_write_scope"
    writer_callers = {
        "_advance_cursor_raw",
        "_set_cursor_raw",
        "_repair_delivery_event_sequence",
    }
    allowed_raw_caller = "_advance_cursor_through_safe_prefix"
    violations: list[str] = []
    aliases: dict[str, str] = {}
    functions: list[str] = []
    classes: list[str] = []

    def raw_target(node: ast.AST) -> str | None:
        if isinstance(node, ast.Attribute) and node.attr in raw_methods | {
            writer_method,
            scope_method,
        }:
            return node.attr
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value
            in raw_methods | {writer_method, scope_method}
        ):
            return str(node.args[1].value)
        if isinstance(node, ast.Name) and node.id in aliases:
            return aliases[node.id]
        return None

    class Visitor(ast.NodeVisitor):
        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            functions.append(node.name)
            self.generic_visit(node)
            functions.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            classes.append(node.name)
            self.generic_visit(node)
            classes.pop()

        def visit_Assign(self, node: ast.Assign) -> None:
            target = raw_target(node.value)
            if target is not None:
                aliases.update(
                    {
                        value.id: target
                        for value in node.targets
                        if isinstance(value, ast.Name)
                    }
                )
            self.generic_visit(node)

        def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
            target = raw_target(node.value) if node.value is not None else None
            if target is not None:
                if isinstance(node.target, ast.Name):
                    aliases[node.target.id] = target
            self.generic_visit(node)

        def visit_Call(self, node: ast.Call) -> None:
            target = raw_target(node.func)
            if target in raw_methods and functions[-1:] != [allowed_raw_caller]:
                violations.append(f"raw cursor call: {target}")
            if target == scope_method and functions[-1:] != [allowed_raw_caller]:
                violations.append(f"cursor write scope call: {target}")
            if target == writer_method and not (
                filename == "goalflight_journal.py"
                and classes[-1:] == ["Journal"]
                and any(function in writer_callers for function in functions)
            ):
                violations.append(f"cursor SQL writer call: {target}")
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr in {"insert", "update", "delete"}
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "controller_stream_cursors"
            ):
                violations.append("RowOperation writes controller_stream_cursors")
            self.generic_visit(node)

        def visit_Constant(self, node: ast.Constant) -> None:
            if isinstance(node.value, str) and re.search(
                r"(?is)\b(?:insert\s+into|update|delete\s+from)\s+[`\"]?controller_stream_cursors\b",
                node.value,
            ):
                if not (
                    filename == "goalflight_journal.py"
                    and functions[-1:] == ["_write_cursor_positions"]
                ):
                    violations.append("raw SQL writes controller_stream_cursors")

    Visitor().visit(ast.parse(source))
    return violations


def test_raw_cursor_writes_have_one_carrier_safe_choke_point() -> None:
    raw_methods = {
        "_advance_cursor_raw",
        "_set_cursor_raw",
        "_repair_delivery_event_sequence",
    }
    definitions: set[tuple[str, str]] = set()
    calls: list[tuple[str, str, str]] = []
    cursor_sql_writers: set[tuple[str, str]] = set()

    class Visitor(ast.NodeVisitor):
        def __init__(self, filename: str) -> None:
            self.filename = filename
            self.functions: list[str] = []

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self.functions.append(node.name)
            self.generic_visit(node)
            self.functions.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, node: ast.Call) -> None:
            name = getattr(node.func, "attr", None)
            if name in raw_methods:
                calls.append((self.filename, self.functions[-1], name))
            self.generic_visit(node)

        def visit_Constant(self, node: ast.Constant) -> None:
            if isinstance(node.value, str) and re.search(
                r"(?is)\b(?:insert\s+into|update|delete\s+from)\s+[`\"]?controller_stream_cursors\b",
                node.value,
            ):
                cursor_sql_writers.add((self.filename, self.functions[-1]))

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            if node.name == "Journal":
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name in raw_methods:
                        definitions.add((self.filename, child.name))
            self.generic_visit(node)

    scripts = sorted(SCRIPTS.rglob("*.py"))
    for path in scripts:
        source = path.read_text(encoding="utf-8")
        Visitor(path.name).visit(ast.parse(source))
        assert not _cursor_write_violations(source, path.name), path

    assert definitions == {
        ("goalflight_journal.py", "_advance_cursor_raw"),
        ("goalflight_journal.py", "_set_cursor_raw"),
        ("goalflight_journal.py", "_repair_delivery_event_sequence"),
    }
    assert cursor_sql_writers == {
        ("goalflight_journal.py", "_write_cursor_positions")
    }
    assert sorted(calls) == [
        ("goalflight_messages.py", "_advance_cursor_through_safe_prefix", "_advance_cursor_raw"),
        ("goalflight_messages.py", "_advance_cursor_through_safe_prefix", "_repair_delivery_event_sequence"),
        ("goalflight_messages.py", "_advance_cursor_through_safe_prefix", "_set_cursor_raw"),
    ]


def test_actionable_emitters_use_the_complete_safe_delivery_prefix() -> None:
    source = (SCRIPTS / "goalflight_messages.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    emitters = {"cmd_relay", "cmd_follow", "cmd_listen", "controller_mail_summary"}
    acknowledgement_paths = {
        "_advance_cursor_through_safe_prefix",
        "_materialized_ack_positions",
    }
    delivery_paths = emitters | acknowledgement_paths
    delivery_nodes: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    follow_emitter_calls: list[str] = []
    functions: list[str] = []

    class Visitor(ast.NodeVisitor):
        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            if node.name in delivery_paths:
                delivery_nodes[node.name] = node
            functions.append(node.name)
            self.generic_visit(node)
            functions.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, node: ast.Call) -> None:
            target = (
                node.func.id
                if isinstance(node.func, ast.Name)
                else getattr(node.func, "attr", None)
            )
            if target == "_emit_claimed_follow_events":
                follow_emitter_calls.append(functions[-1])
            self.generic_visit(node)

    Visitor().visit(tree)
    assert delivery_nodes.keys() == delivery_paths
    for name, node in delivery_nodes.items():
        calls = {
            child.func.id
            if isinstance(child.func, ast.Name)
            else child.func.attr
            for child in ast.walk(node)
            if isinstance(child, ast.Call)
            and isinstance(child.func, (ast.Name, ast.Attribute))
        }
        assert "_deliverable_rows" in calls, name
        assert "_envelopes_with_rows" not in calls, name
        if name in {"controller_mail_summary", *acknowledgement_paths}:
            assert "_listener_envelope" not in calls, name
    assert follow_emitter_calls == ["cmd_follow"]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "def bypass(authority):\n    raw = authority._advance_cursor_raw\n    raw()\n",
            "raw cursor call",
        ),
        (
            "def bypass(authority):\n    getattr(authority, '_set_cursor_raw')()\n",
            "raw cursor call",
        ),
        (
            "def bypass(authority):\n    with authority._cursor_write_scope():\n        pass\n",
            "cursor write scope call",
        ),
        (
            "def bypass():\n    RowOperation.update('controller_stream_cursors', {}, where={}, row_cap=1)\n",
            "RowOperation writes",
        ),
        (
            "def bypass(connection):\n    connection.execute('UPDATE controller_stream_cursors SET position = 1')\n",
            "raw SQL writes",
        ),
        (
            "def bypass(authority):\n    authority._write_cursor_positions(None)\n",
            "cursor SQL writer call",
        ),
    ],
)
def test_raw_cursor_write_guard_rejects_bypass_forms(
    source: str, expected: str
) -> None:
    assert any(
        expected in violation
        for violation in _cursor_write_violations(source, "injected.py")
    )


def test_journal_write_rejects_direct_cursor_row_operations(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    authority = journal.open_or_create_journal(_git_project(tmp_path))
    for operation in (
        lambda: journal.RowOperation.insert(
            "controller_stream_cursors", {"position": 1}
        ),
        lambda: journal.RowOperation.update(
            "controller_stream_cursors",
            {"position": 1},
            where={"label": "x"},
            row_cap=1,
        ),
        lambda: journal.RowOperation.delete(
            "controller_stream_cursors", where={"label": "x"}, row_cap=1
        ),
    ):
        with pytest.raises(ValueError, match="carrier-safe cursor choke point"):
            operation()

    forged = object.__new__(journal.RowOperation)
    object.__setattr__(forged, "table", "controller_stream_cursors")
    with pytest.raises(journal.JournalError, match="carrier-safe cursor choke point"):
        authority.write([forged])


def test_sqlite_authorizer_blocks_cursor_writes_outside_safe_clamp(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    project = _git_project(tmp_path)
    authority = journal.open_or_create_journal(project)
    lease = _claim(authority, "owner")
    writes = [
        "UPDATE [controller_stream_cursors] SET position = 99",
        'DELETE FROM "controller_stream_cursors"',
        "UPDATE main.controller_stream_cursors SET position = 99",
        "INSERT OR REPLACE INTO main.[controller_stream_cursors] "
        "(project_root, label, stream_id, position, updated_at) "
        "VALUES ('x', 'x', 'x', 99, 'now')",
    ]
    for statement in writes:
        connection = authority._connect()
        try:
            with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
                connection.execute(statement)
        finally:
            connection.close()

    connection = authority._connect()
    try:
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            connection.executescript(
                'UPDATE "main"."controller_stream_cursors" SET position = 99;'
            )
    finally:
        connection.close()

    _post(project, Path(os.environ["GOALFLIGHT_MESSAGES_DIR"]), "owner", "clamped")
    peek = authority.cursor_peek("owner", nonce=lease.nonce)
    result = authority.advance_cursor(
        "owner",
        nonce=lease.nonce,
        expected_cursor_version=peek.cursor_version,
        expected_stream_snapshots=peek.stream_snapshots,
        advances={STREAM: 1},
        actor="test-authorized-clamp",
    )
    assert result.committed, result.reason
    assert authority.cursor_status("owner")["positions"] == {STREAM: 1}


def test_advance_cursor_cas_refusal_keeps_write_result_contract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    project = _git_project(tmp_path)
    authority = journal.open_or_create_journal(project)
    lease = _claim(authority, "owner")
    _post(project, Path(os.environ["GOALFLIGHT_MESSAGES_DIR"]), "owner", "mail")
    peek = authority.cursor_peek("owner", nonce=lease.nonce)

    result = authority.advance_cursor(
        "owner",
        nonce="stale-lease-nonce",
        expected_cursor_version=peek.cursor_version,
        expected_stream_snapshots=peek.stream_snapshots,
        advances={STREAM: 1},
        actor="test-cas-contract",
    )

    assert result.cas_lost
    assert "lease generation changed" in result.reason


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "goalflight_messages.py"), "advance",
         "--controller-label", "probe", "--lease-nonce", "nonce", *args],
        capture_output=True, text=True,
    )


def test_acked_refuses_to_mix_with_hand_supplied_cas() -> None:
    """Half-derived, half-supplied CAS inputs are the one way to get this wrong."""
    done = _run("--acked", "--cursor-version", "5")
    assert done.returncode != 0
    assert "pass it alone" in done.stderr


def test_cursor_version_still_required_without_acked() -> None:
    """The old contract is unchanged for callers that do supply the inputs."""
    done = _run("--position", "alpha=1")
    assert done.returncode != 0
    assert "--cursor-version is required" in done.stderr
    assert "--acked" in done.stderr, "the error should name the easier route"


STREAM = "engine-fleet-laptop-load"
FANOUT = (
    "battery-main",
    "battery-bugs",
    "battery-perf",
    "battery-webui",
    "battery-webui",
)


def _set_state_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, str]:
    values = {
        "GOALFLIGHT_TASK_STORE_DIR": str(tmp_path / "task-store"),
        "GOALFLIGHT_JOURNAL_DIR": str(tmp_path / "journal-state"),
        "GOALFLIGHT_MESSAGES_DIR": str(tmp_path / "messages"),
        "GOALFLIGHT_STATE_DIR": str(tmp_path / "dispatch-state"),
        "GOALFLIGHT_WAKE_LEDGER_DIR": str(tmp_path / "wake-ledger"),
        "GOAL_FLIGHT_PIDFILE_DIR": str(tmp_path / "pidfiles"),
        "GOALFLIGHT_CAPACITY_CONF": "/dev/null",
        "GOALFLIGHT_TEST_MODE": "1",
        "GOALFLIGHT_CONTROLLER_SESSION_ID": "pytest-session",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
        if value != "/dev/null":
            Path(value).mkdir(parents=True, exist_ok=True)
    for key in (
        "GOALFLIGHT_DISPATCH_ID",
        "GOALFLIGHT_CONTROLLER_LEASE_NONCE",
        "GOALFLIGHT_CONTROLLER_LABEL",
    ):
        monkeypatch.delenv(key, raising=False)
    env = os.environ.copy()
    env.update(values)
    return env


def _git_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    return project


def _claim(authority: journal.Journal, label: str):
    claimed = authority.claim_or_renew_lease(
        label, principal={"principal_id": f"{label}-principal"}
    )
    assert claimed.committed and claimed.value is not None, claimed.reason
    return claimed.value


def _post(
    project: Path,
    messages_dir: Path,
    label: str,
    text: str,
    *,
    dispatch_id: str = STREAM,
    allow_duplicate: bool = False,
    event_ts: str | None = None,
) -> dict:
    kwargs: dict = {
        "dispatch_id": dispatch_id,
        "msg_type": "controller-notice",
        "payload": {"text": text},
        "messages_dir": messages_dir,
        "source": {"node": "test", "adapter": "pytest", "transport": "controller"},
        "addressee": gm.controller_addressee(label, project_root=project),
    }
    if allow_duplicate:
        kwargs["skip_if"] = lambda _item: False
    if event_ts is not None:
        kwargs["event_ts"] = event_ts
    return gm.post_message(**kwargs)


def _pending(
    authority: journal.Journal,
    label: str,
    *,
    limit: int = 1000,
) -> list[tuple[str, int]]:
    return [
        (str(row["stream_id"]), int(row["stream_seq"]))
        for row in authority.pending_delivery_events(
            label,
            waking_only=False,
            limit=limit,
        )
    ]


def test_owner_set_drain_stops_at_stuck_and_resumes_after_repair(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    project = _git_project(tmp_path)
    authority = journal.open_or_create_journal(project)
    _claim(authority, "owner")
    messages_dir = Path(os.environ["GOALFLIGHT_MESSAGES_DIR"])
    first = _post(project, messages_dir, "owner", "first")
    stuck = _post(project, messages_dir, "owner", "stuck", dispatch_id=STREAM)
    later = _post(project, messages_dir, "owner", "later", dispatch_id=STREAM)
    carrier = Path(stuck["path"])
    original = carrier.read_text(encoding="utf-8").splitlines()
    assert len(original) == 3
    carrier.write_text(original[0] + "\n{broken\n" + original[2] + "\n", encoding="utf-8")

    blocked = authority.set_cursor("owner", positions=None, actor="test-owner")
    assert blocked.committed, blocked.reason
    assert blocked.value["advances"] == {STREAM: 1}
    assert blocked.value["stuck"][0]["delivery_id"] == stuck["envelope"]["id"]
    assert blocked.value["pending_behind_stuck"][0]["id"] == later["envelope"]["id"]
    assert authority.cursor_status("owner")["positions"] == {STREAM: 1}

    carrier.write_text("\n".join(original) + "\n", encoding="utf-8")
    repaired = authority.set_cursor("owner", positions=None, actor="test-owner")
    assert repaired.committed, repaired.reason
    assert repaired.value["advances"] == {STREAM: 3}
    assert not _pending(authority, "owner")
    assert first["envelope"]["id"] != stuck["envelope"]["id"]


def test_explicit_cursor_advance_clamps_to_stuck_prefix(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    project = _git_project(tmp_path)
    authority = journal.open_or_create_journal(project)
    lease = _claim(authority, "owner")
    messages_dir = Path(os.environ["GOALFLIGHT_MESSAGES_DIR"])
    _post(project, messages_dir, "owner", "first", dispatch_id=STREAM)
    stuck = _post(project, messages_dir, "owner", "stuck", dispatch_id=STREAM)
    later = _post(project, messages_dir, "owner", "later", dispatch_id=STREAM)
    carrier = Path(stuck["path"])
    original = carrier.read_text(encoding="utf-8").splitlines()
    carrier.write_text(original[0] + "\n{broken\n" + original[2] + "\n", encoding="utf-8")

    peek = authority.cursor_peek("owner", nonce=lease.nonce)
    clamped = authority.advance_cursor(
        "owner",
        nonce=lease.nonce,
        expected_cursor_version=peek.cursor_version,
        expected_stream_snapshots=peek.stream_snapshots,
        advances={STREAM: 3},
        actor="test-owner",
    )
    assert clamped.committed, clamped.reason
    assert clamped.value["advances"] == {STREAM: 1}
    assert clamped.value["clamped"] == {STREAM: 1}
    assert clamped.value["stuck"][0]["delivery_id"] == stuck["envelope"]["id"]
    assert clamped.value["pending_behind_stuck"][0]["id"] == later["envelope"]["id"]

    carrier.write_text("\n".join(original) + "\n", encoding="utf-8")
    repaired = authority.cursor_peek("owner", nonce=lease.nonce)
    delivered = authority.advance_cursor(
        "owner",
        nonce=lease.nonce,
        expected_cursor_version=repaired.cursor_version,
        expected_stream_snapshots=repaired.stream_snapshots,
        advances={STREAM: 3},
        actor="test-owner",
    )
    assert delivered.committed, delivered.reason
    assert delivered.value["advances"] == {STREAM: 3}
    assert not authority.cursor_peek("owner", nonce=lease.nonce).items


@pytest.mark.parametrize(
    "failure_mode", ["plain_message_error", "runtime_error", "sequence_repaired"]
)
def test_unmaterialized_row_stops_safe_prefix_without_advancing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure_mode: str,
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    project = _git_project(tmp_path)
    authority = journal.open_or_create_journal(project)
    lease = _claim(authority, "owner")
    messages_dir = Path(os.environ["GOALFLIGHT_MESSAGES_DIR"])
    first = _post(project, messages_dir, "owner", "plain error", dispatch_id=STREAM)
    later = _post(project, messages_dir, "owner", "readable later", dispatch_id=STREAM)
    peek = authority.cursor_peek("owner", nonce=lease.nonce)
    original = gm._listener_envelope

    def fail_first(source, row, **kwargs):
        if row["event_uuid"] == first["envelope"]["id"]:
            if failure_mode == "plain_message_error":
                raise gm.MessageError(
                    "carrier materialization failed without STUCK prefix"
                )
            if failure_mode == "runtime_error":
                raise RuntimeError("carrier materialization raised an exception")
            row["_journal_sequence_repaired"] = True
        return original(source, row, **kwargs)

    monkeypatch.setattr(gm, "_listener_envelope", fail_first)
    refused = authority.advance_cursor(
        "owner",
        nonce=lease.nonce,
        expected_cursor_version=peek.cursor_version,
        expected_stream_snapshots=peek.stream_snapshots,
        advances={STREAM: 2},
        actor="test-plain-message-error",
    )

    status = authority.cursor_status("owner")
    assert not refused.committed
    assert "no safe materialized prefix" in refused.reason
    assert status is not None and status["positions"].get(STREAM, 0) == 0
    assert refused.value["stuck"][0]["delivery_id"] == first["envelope"]["id"]
    assert refused.value["pending_behind_stuck"][0]["id"] == later["envelope"]["id"]


def _advance(
    project: Path,
    label: str,
    *args: str,
    nonce: str | None = None,
) -> tuple[int, str, str]:
    argv = [
        "advance",
        "--controller-label",
        label,
        "--project-root",
        str(project),
        *args,
    ]
    if nonce is not None:
        argv[1:1] = ["--lease-nonce", nonce]
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        rc = gm.main(argv)
    return rc, stdout.getvalue(), stderr.getvalue()


def _advance_acked(project: Path, label: str, nonce: str) -> int:
    rc_relay, relay_stdout, relay_stderr = _relay(project, label)
    assert rc_relay == 0, relay_stderr or relay_stdout
    rc, stdout, stderr = _advance(project, label, "--acked", "--json", nonce=nonce)
    assert rc == 0, stderr or stdout
    return rc


def _relay(
    project: Path,
    label: str,
    *args: str,
) -> tuple[int, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        rc = gm.main(
            ["relay", "--project-root", str(project), "--controller-label", label, *args]
        )
    return rc, stdout.getvalue(), stderr.getvalue()


def test_acked_uses_last_display_and_leaves_later_event_pending(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    project = _git_project(tmp_path)
    authority = journal.open_or_create_journal(project)
    lease = _claim(authority, "battery-webui")
    messages_dir = Path(os.environ["GOALFLIGHT_MESSAGES_DIR"])
    _post(project, messages_dir, "battery-webui", "shown A", dispatch_id="events")

    rc, relay_stdout, relay_stderr = _relay(project, "battery-webui", "--json")
    assert rc == 0, relay_stderr or relay_stdout
    assert '"positions": {"events": 1}' in relay_stdout

    _post(
        project,
        messages_dir,
        "battery-webui",
        "arrived later B",
        dispatch_id="events",
        allow_duplicate=True,
    )
    rc, stdout, stderr = _advance(
        project, "battery-webui", "--acked", "--json", nonce=lease.nonce
    )
    assert rc == 0, stderr or stdout
    payload = json.loads(stdout)
    assert payload["acknowledged_events"] == 1
    assert payload["pending_events"] == 1
    assert _pending(authority, "battery-webui") == [("events", 2)]


def test_acked_without_relay_refuses_with_exact_position_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    project = _git_project(tmp_path)
    authority = journal.open_or_create_journal(project)
    lease = _claim(authority, "battery-webui")
    _post(
        project,
        Path(os.environ["GOALFLIGHT_MESSAGES_DIR"]),
        "battery-webui",
        "not displayed",
    )
    rc, _, stderr = _advance(
        project, "battery-webui", "--acked", "--json", nonce=lease.nonce
    )
    assert rc != 0
    assert "exact per-position command" in stderr
    assert "--stream-snapshot" in stderr
    assert "--position" in stderr
    assert _pending(authority, "battery-webui") == [(STREAM, 1)]


def test_concurrent_sessions_keep_separate_relay_snapshots(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    project = _git_project(tmp_path)
    authority = journal.open_or_create_journal(project)
    lease = _claim(authority, "battery-webui")
    messages_dir = Path(os.environ["GOALFLIGHT_MESSAGES_DIR"])
    _post(project, messages_dir, "battery-webui", "shown by first", dispatch_id="events")

    monkeypatch.setenv("GOALFLIGHT_CONTROLLER_SESSION_ID", "session-one")
    rc, _, stderr = _relay(project, "battery-webui", "--json")
    assert rc == 0, stderr
    _post(
        project,
        messages_dir,
        "battery-webui",
        "later event",
        dispatch_id="events",
        allow_duplicate=True,
    )
    monkeypatch.setenv("GOALFLIGHT_CONTROLLER_SESSION_ID", "session-two")
    rc, _, stderr = _relay(project, "battery-webui", "--json")
    assert rc == 0, stderr

    monkeypatch.setenv("GOALFLIGHT_CONTROLLER_SESSION_ID", "session-one")
    rc, stdout, stderr = _advance(
        project, "battery-webui", "--acked", "--json", nonce=lease.nonce
    )
    assert rc == 0, stderr or stdout
    assert json.loads(stdout)["acknowledged_events"] == 1
    assert _pending(authority, "battery-webui") == [("events", 2)]


def test_idless_callers_cannot_use_each_others_relay_display(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    monkeypatch.delenv("GOALFLIGHT_CONTROLLER_SESSION_ID")
    project = _git_project(tmp_path)
    authority = journal.open_or_create_journal(project)
    lease = _claim(authority, "battery-webui")
    messages_dir = Path(os.environ["GOALFLIGHT_MESSAGES_DIR"])
    _post(project, messages_dir, "battery-webui", "first caller saw A", dispatch_id="events")
    rc, _, stderr = _relay(project, "battery-webui", "--json")
    assert rc == 0, stderr
    _post(
        project,
        messages_dir,
        "battery-webui",
        "B arrived",
        dispatch_id="events",
        allow_duplicate=True,
    )
    rc, _, stderr = _relay(project, "battery-webui", "--json")
    assert rc == 0, stderr

    rc, _, stderr = _advance(
        project, "battery-webui", "--acked", "--json", nonce=lease.nonce
    )
    assert rc != 0
    assert "GOALFLIGHT_CONTROLLER_SESSION_ID" in stderr
    assert "exact per-position command" in stderr
    assert "--stream-snapshot" in stderr and "--position" in stderr
    assert _pending(authority, "battery-webui") == [("events", 1), ("events", 2)]


def test_relay_snapshot_replaces_session_record_and_drops_old_lease(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    project = _git_project(tmp_path)
    authority = journal.open_or_create_journal(project)
    first = _claim(authority, "battery-webui")
    _post(
        project,
        Path(os.environ["GOALFLIGHT_MESSAGES_DIR"]),
        "battery-webui",
        "mail",
    )
    shown_dir = Path(os.environ["GOALFLIGHT_STATE_DIR"]) / "relay-shown"
    shown_dir.mkdir(parents=True, exist_ok=True)
    legacy_identity = json.dumps(
        [str(project.resolve()), "battery-webui", first.nonce, "pytest-session"],
        separators=(",", ":"),
    )
    legacy_path = shown_dir / (
        hashlib.sha256(legacy_identity.encode("utf-8")).hexdigest() + ".json"
    )
    legacy_path.write_text(json.dumps({
        "controller_label": "battery-webui",
        "lease_nonce": first.nonce,
        "session_id": "pytest-session",
    }))
    rc, _, stderr = _relay(project, "battery-webui", "--json")
    assert rc == 0, stderr
    rc, _, stderr = _relay(project, "battery-webui", "--json")
    assert rc == 0, stderr
    assert len(list(shown_dir.glob("*.json"))) == 1

    successor = authority.claim_or_renew_lease(
        "battery-webui",
        principal={"principal_id": "battery-webui-successor"},
        takeover=True,
    )
    assert successor.committed and successor.value is not None
    assert successor.value.nonce != first.nonce
    monkeypatch.setenv("GOALFLIGHT_CONTROLLER_SESSION_ID", "next-session")
    rc, _, stderr = _relay(project, "battery-webui", "--json")
    assert rc == 0, stderr
    records = list(shown_dir.glob("*.json"))
    assert len(records) == 1
    record = json.loads(records[0].read_text())
    assert record["lease_nonce"] == successor.value.nonce
    assert record["session_id"] == "next-session"


@pytest.mark.parametrize("mode", ["explicit", "to-latest"])
def test_real_cursor_peek_advances_dict_items(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: str
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    project = _git_project(tmp_path)
    authority = journal.open_or_create_journal(project)
    lease = _claim(authority, "dict-items")
    _post(project, Path(os.environ["GOALFLIGHT_MESSAGES_DIR"]), "dict-items", "mail")
    peek = authority.cursor_peek("dict-items", nonce=lease.nonce, limit=10)
    assert isinstance(peek.items[0], dict)
    stream = str(peek.items[0]["stream_id"])
    seq = int(peek.items[0]["stream_seq"])
    if mode == "explicit":
        args = [
            "--cursor-version", str(peek.cursor_version),
            "--stream-snapshot", f"{stream}={peek.stream_snapshots[stream]}",
            "--position", f"{stream}={seq}", "--json",
        ]
    else:
        args = ["--to-latest", stream, "--json"]

    rc, stdout, stderr = _advance(project, "dict-items", *args, nonce=lease.nonce)
    assert rc == 0, stderr or stdout
    result = json.loads(stdout)
    assert result["acknowledged_events"] == 1
    assert result["pending_events"] == 0
    assert _pending(authority, "dict-items") == []


def test_acked_advances_past_duplicate_envelopes_to_one_addressee(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Two seqs to one controller on one stream: --acked must drain both.

    A truncated peek locates only the first; completing the mentioned stream
    is what makes unread reach zero. Revert complete_addressed_streams and
    this fails with peek limit 1.
    """
    _set_state_env(monkeypatch, tmp_path)
    monkeypatch.setattr(gm, "_ACKED_PEEK_LIMIT", 1)
    project = _git_project(tmp_path)
    authority = journal.open_or_create_journal(project)
    lease = _claim(authority, "battery-webui")
    messages_dir = Path(os.environ["GOALFLIGHT_MESSAGES_DIR"])
    first = _post(project, messages_dir, "battery-webui", "one message")
    second = _post(
        project,
        messages_dir,
        "battery-webui",
        "one message",
        allow_duplicate=True,
    )
    assert first["envelope"]["seq"] == 1
    assert second["envelope"]["seq"] == 2
    assert _pending(authority, "battery-webui") == [(STREAM, 1), (STREAM, 2)]
    _advance_acked(project, "battery-webui", lease.nonce)
    assert _pending(authority, "battery-webui") == []
    cursor = authority.cursor_status("battery-webui")
    assert cursor is not None
    assert cursor["positions"] == {STREAM: 2}


def test_acked_does_not_skip_unshown_mail_on_another_stream(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    project = _git_project(tmp_path)
    authority = journal.open_or_create_journal(project)
    lease = _claim(authority, "battery-webui")
    messages_dir = Path(os.environ["GOALFLIGHT_MESSAGES_DIR"])
    _post(project, messages_dir, "battery-webui", "shown", dispatch_id="aaa-shown")
    _post(
        project,
        messages_dir,
        "battery-webui",
        "never shown",
        dispatch_id="zzz-unshown",
    )
    # A filtered diagnostic relay does not create an acknowledgeable snapshot.
    rc, _, _ = _relay(
        project, "battery-webui", "--since", "2026-01-01T00:00:00Z"
    )
    assert rc == 0
    rc, _, stderr = _advance(
        project, "battery-webui", "--acked", "--json", nonce=lease.nonce
    )
    assert rc != 0
    assert "exact per-position command" in stderr
    assert _pending(authority, "battery-webui") == [("aaa-shown", 1), ("zzz-unshown", 1)]


def test_acked_single_envelope_still_drains(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    project = _git_project(tmp_path)
    authority = journal.open_or_create_journal(project)
    lease = _claim(authority, "controller")
    messages_dir = Path(os.environ["GOALFLIGHT_MESSAGES_DIR"])
    _post(project, messages_dir, "controller", "solo", dispatch_id="solo-stream")
    _advance_acked(project, "controller", lease.nonce)
    assert _pending(authority, "controller") == []
    cursor = authority.cursor_status("controller")
    assert cursor is not None
    assert cursor["positions"] == {"solo-stream": 1}


def test_acked_fanout_with_duplicate_addressee_drains_every_controller(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    project = _git_project(tmp_path)
    authority = journal.open_or_create_journal(project)
    leases = {label: _claim(authority, label) for label in dict.fromkeys(FANOUT)}
    messages_dir = Path(os.environ["GOALFLIGHT_MESSAGES_DIR"])
    for index, label in enumerate(FANOUT):
        _post(
            project,
            messages_dir,
            label,
            "one message",
            allow_duplicate=index == len(FANOUT) - 1,
        )
    for label, lease in leases.items():
        _advance_acked(project, label, lease.nonce)
        assert _pending(authority, label) == [], label
        cursor = authority.cursor_status(label)
        assert cursor is not None
        assert STREAM in cursor["positions"]


def test_identical_addressee_payload_is_still_delivered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Identical content is not proof of a duplicate; deliver it.

    This inverts an earlier assertion. A producer-side dedupe keyed on
    (label, type, payload) across the carrier's whole lifetime was withdrawn
    because it silently dropped legitimate mail: the same advisory sent an
    hour later, and the same notice sent to one label in two different
    project roots. Identical text can be a genuinely new event, and losing
    real mail is worse than the duplicate the filter prevented. Duplicates
    are handled on the consumer side by --acked completion instead.
    """
    _set_state_env(monkeypatch, tmp_path)
    project = _git_project(tmp_path)
    authority = journal.open_or_create_journal(project)
    _claim(authority, "battery-webui")
    messages_dir = Path(os.environ["GOALFLIGHT_MESSAGES_DIR"])
    first = _post(project, messages_dir, "battery-webui", "one message")
    second = _post(project, messages_dir, "battery-webui", "one message")
    assert first["recorded"] is True
    assert second["recorded"] is True, "a legitimate repeat must not be suppressed"
    assert second["envelope"]["seq"] != first["envelope"]["seq"]


def test_relay_summary_only_since_is_headline_and_count_without_bodies(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env = _set_state_env(monkeypatch, tmp_path)
    project = _git_project(tmp_path)
    monkeypatch.setenv("GOALFLIGHT_CONTROLLER_LABEL", "battery-webui")
    monkeypatch.setattr(gm, "_current_project_root", lambda: project)
    authority = journal.open_or_create_journal(project)
    _claim(authority, "battery-webui")
    messages_dir = Path(env["GOALFLIGHT_MESSAGES_DIR"])
    _post(
        project,
        messages_dir,
        "battery-webui",
        "old body that must not print",
        dispatch_id="old-stream",
        event_ts="2026-01-01T00:00:00+00:00",
    )
    _post(
        project,
        messages_dir,
        "battery-webui",
        "new headline",
        dispatch_id="new-stream",
        event_ts="2026-08-01T00:00:00+00:00",
    )
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        rc = gm.main(
            ["relay", "--new", "--summary-only", "--since", "2026-07-01T00:00:00Z"]
        )
    assert rc == 0, stderr.getvalue()
    text = stdout.getvalue()
    assert "old body that must not print" not in text
    assert "new-stream" in text
    assert "new headline" in text
    assert "old-stream" not in text
    assert "pending counts: new-stream=1" in text
    assert '"text"' not in text


def test_parse_since_rejects_garbage() -> None:
    with pytest.raises(ValueError, match="RFC3339"):
        gm._parse_since_timestamp("yesterday")


def test_same_addressed_payload_requires_matching_label_and_body() -> None:
    def addressed(label: str) -> dict:
        return {
            "type": "controller-notice",
            "payload": {"text": "one message"},
            "addressee": {
                "kind": "controller",
                "label": label,
                "project_root": "/tmp/example-project",
            },
        }

    incoming = addressed("battery-webui")
    assert gm._same_addressed_payload(incoming, incoming)
    assert not gm._same_addressed_payload(addressed("battery-main"), incoming)
    different_text = addressed("battery-webui")
    different_text["payload"] = {"text": "other"}
    assert not gm._same_addressed_payload(different_text, incoming)


def test_acked_advances_with_label_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A controller that knows its slug does not need to carry a nonce."""
    _set_state_env(monkeypatch, tmp_path)
    project = _git_project(tmp_path)
    authority = journal.open_or_create_journal(project)
    lease = _claim(authority, "battery-webui")
    assert os.environ["GOALFLIGHT_CONTROLLER_SESSION_ID"] != lease.nonce
    messages_dir = Path(os.environ["GOALFLIGHT_MESSAGES_DIR"])
    _post(project, messages_dir, "battery-webui", "label-only mail")
    assert os.environ.get("GOALFLIGHT_CONTROLLER_LEASE_NONCE") in (None, "")
    rc_relay, relay_stdout, relay_stderr = _relay(project, "battery-webui")
    assert rc_relay == 0, relay_stderr or relay_stdout
    rc, stdout, stderr = _advance(
        project, "battery-webui", "--acked", "--json"
    )
    assert rc == 0, stderr or stdout
    assert "--lease-nonce" not in (stderr + stdout)
    assert _pending(authority, "battery-webui") == []
    cursor = authority.cursor_status("battery-webui")
    assert cursor is not None
    assert cursor["positions"] == {STREAM: 1}


def test_explicit_nonce_still_pins_the_live_generation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Passing --lease-nonce still binds the write to that generation."""
    _set_state_env(monkeypatch, tmp_path)
    project = _git_project(tmp_path)
    authority = journal.open_or_create_journal(project)
    lease = _claim(authority, "battery-webui")
    messages_dir = Path(os.environ["GOALFLIGHT_MESSAGES_DIR"])
    _post(project, messages_dir, "battery-webui", "pinned generation")
    rc_wrong, _, err_wrong = _advance(
        project,
        "battery-webui",
        "--acked",
        "--json",
        nonce="not-the-active-nonce",
    )
    assert rc_wrong != 0
    assert "no longer active" in err_wrong or "lease generation changed" in err_wrong
    assert _pending(authority, "battery-webui") == [(STREAM, 1)]
    rc_relay, relay_stdout, relay_stderr = _relay(project, "battery-webui")
    assert rc_relay == 0, relay_stderr or relay_stdout
    rc, stdout, stderr = _advance(
        project, "battery-webui", "--acked", "--json", nonce=lease.nonce
    )
    assert rc == 0, stderr or stdout
    assert _pending(authority, "battery-webui") == []


def test_stale_generation_nonce_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An older generation cannot fall back to the label's current nonce."""
    _set_state_env(monkeypatch, tmp_path)
    project = _git_project(tmp_path)
    authority = journal.open_or_create_journal(project)
    first = _claim(authority, "battery-webui")
    messages_dir = Path(os.environ["GOALFLIGHT_MESSAGES_DIR"])
    _post(project, messages_dir, "battery-webui", "survives stale child")
    successor = authority.claim_or_renew_lease(
        "battery-webui",
        principal={"principal_id": "battery-webui-successor"},
        takeover=True,
    )
    assert successor.committed and successor.value is not None
    assert successor.value.generation == first.generation + 1
    assert successor.value.nonce != first.nonce
    rc_acked, _, err_acked = _advance(
        project, "battery-webui", "--acked", "--json", nonce=first.nonce
    )
    assert rc_acked != 0
    assert "no longer active" in err_acked or "lease generation changed" in err_acked
    peek = authority.cursor_peek("battery-webui", nonce=successor.value.nonce)
    assert peek.items
    stream = str(peek.items[0]["stream_id"])
    seq = int(peek.items[0]["stream_seq"])
    token = peek.stream_snapshots[stream]
    rc_write, _, err_write = _advance(
        project,
        "battery-webui",
        "--cursor-version",
        str(peek.cursor_version),
        "--stream-snapshot",
        f"{stream}={token}",
        "--position",
        f"{stream}={seq}",
        nonce=first.nonce,
    )
    assert rc_write != 0
    assert "lease generation changed" in err_write
    assert _pending(authority, "battery-webui") == [(STREAM, 1)]
    rc_relay, relay_stdout, relay_stderr = _relay(project, "battery-webui")
    assert rc_relay == 0, relay_stderr or relay_stdout
    rc_live, stdout_live, err_live = _advance(
        project, "battery-webui", "--acked", "--json"
    )
    assert rc_live == 0, err_live or stdout_live
    assert _pending(authority, "battery-webui") == []


def test_no_active_lease_reconnects_by_slug(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    project = _git_project(tmp_path)
    journal.open_or_create_journal(project)
    rc, _, stderr = _advance(project, "ghost-controller", "--acked")
    assert rc != 0
    assert "no active lease for ghost-controller" in stderr
    assert "reconnect as:" in stderr
    assert "--session-label" in stderr
    assert "ghost-controller" in stderr
    assert "lease nonce are required" not in stderr
    assert "--lease-nonce" not in stderr
    assert "--takeover" not in stderr


def test_one_label_cannot_advance_anothers_cursor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Resolving nonce from a label must not drain a different label."""
    _set_state_env(monkeypatch, tmp_path)
    project = _git_project(tmp_path)
    authority = journal.open_or_create_journal(project)
    alice = _claim(authority, "alice")
    _claim(authority, "bob")
    messages_dir = Path(os.environ["GOALFLIGHT_MESSAGES_DIR"])
    _post(project, messages_dir, "alice", "alice mail", dispatch_id="alice-stream")
    _post(project, messages_dir, "bob", "bob mail", dispatch_id="bob-stream")
    rc_relay, relay_stdout, relay_stderr = _relay(project, "alice")
    assert rc_relay == 0, relay_stderr or relay_stdout
    rc, stdout, stderr = _advance(project, "alice", "--acked", "--json")
    assert rc == 0, stderr or stdout
    assert _pending(authority, "alice") == []
    assert _pending(authority, "bob") == [("bob-stream", 1)]
    rc_cross, _, err_cross = _advance(
        project, "bob", "--acked", "--json", nonce=alice.nonce
    )
    assert rc_cross != 0
    assert "no longer active" in err_cross or "lease generation changed" in err_cross
    assert _pending(authority, "bob") == [("bob-stream", 1)]


def _snapshot_token(
    authority: journal.Journal, label: str, stream: str, position: int
) -> str:
    def action(connection: object) -> str:
        token, has_unprojected = journal.Journal._cursor_stream_snapshot(
            connection,
            project_root=str(authority.project_root),
            recipient_label=label,
            stream_id=stream,
            requested_position=position,
        )
        assert not has_unprojected
        return token

    return authority._read_with_retry("test stream snapshot", action)


def test_orphaned_label_explicit_position_creates_cursor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A label with no lease can be acknowledged when every stream is named."""
    _set_state_env(monkeypatch, tmp_path)
    project = _git_project(tmp_path)
    authority = journal.open_or_create_journal(project)
    messages_dir = Path(os.environ["GOALFLIGHT_MESSAGES_DIR"])
    posted = _post(project, messages_dir, "webui-orphan", "backlog")
    assert authority.cursor_status("webui-orphan") is None
    assert authority.active_lease("webui-orphan") is None
    seq = int(posted["envelope"]["seq"])
    token = _snapshot_token(authority, "webui-orphan", STREAM, seq)
    monkeypatch.setenv("GOALFLIGHT_CONTROLLER_LEASE_NONCE", "other-session-nonce")
    rc, stdout, stderr = _advance(
        project,
        "webui-orphan",
        "--orphaned-label",
        "--cursor-version",
        "0",
        "--stream-snapshot",
        f"{STREAM}={token}",
        "--position",
        f"{STREAM}={seq}",
        "--json",
    )
    assert rc == 0, stderr or stdout
    cursor = authority.cursor_status("webui-orphan")
    assert cursor is not None
    assert cursor["positions"] == {STREAM: seq}
    assert str(cursor.get("advanced_by") or "").endswith(":orphaned-label")
    assert _pending(authority, "webui-orphan") == []


def test_orphaned_label_refuses_acked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """--acked derives positions; an orphaned advance must name every stream."""
    _set_state_env(monkeypatch, tmp_path)
    project = _git_project(tmp_path)
    journal.open_or_create_journal(project)
    rc, _, stderr = _advance(
        project, "webui-orphan", "--orphaned-label", "--acked"
    )
    assert rc != 0
    assert "--acked is refused" in stderr
    assert "orphaned-label" in stderr


def test_orphaned_label_refused_when_lease_is_active(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """--orphaned-label is an opt-in, never a way around a live holder."""
    _set_state_env(monkeypatch, tmp_path)
    project = _git_project(tmp_path)
    authority = journal.open_or_create_journal(project)
    lease = _claim(authority, "held")
    messages_dir = Path(os.environ["GOALFLIGHT_MESSAGES_DIR"])
    posted = _post(project, messages_dir, "held", "live holder mail")
    seq = int(posted["envelope"]["seq"])
    token = _snapshot_token(authority, "held", STREAM, seq)
    rc_flag, _, err_flag = _advance(
        project,
        "held",
        "--orphaned-label",
        "--cursor-version",
        "0",
        "--stream-snapshot",
        f"{STREAM}={token}",
        "--position",
        f"{STREAM}={seq}",
    )
    assert rc_flag != 0
    assert "has an active lease" in err_flag
    rc_cross, _, err_cross = _advance(
        project,
        "held",
        "--cursor-version",
        "0",
        "--stream-snapshot",
        f"{STREAM}={token}",
        "--position",
        f"{STREAM}={seq}",
        nonce="not-the-holder",
    )
    assert rc_cross != 0
    assert "lease generation changed" in err_cross
    assert "no active lease exists" not in err_cross
    assert _pending(authority, "held") == [(STREAM, seq)]
    assert authority.active_lease("held") is not None
    assert authority.active_lease("held").nonce == lease.nonce


def test_no_active_lease_error_is_not_a_generation_change(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Lease-is-None must not be worded as a generation change."""
    _set_state_env(monkeypatch, tmp_path)
    project = _git_project(tmp_path)
    authority = journal.open_or_create_journal(project)
    token = "a" * 64
    missing = authority.advance_cursor(
        "ghost",
        nonce="not-a-lease",
        expected_cursor_version=0,
        expected_stream_snapshots={"ghost-stream": token},
        advances={"ghost-stream": 1},
        actor="test-operator",
    )
    assert missing.cas_lost
    missing_reason = missing.reason or ""
    assert "no active lease exists" in missing_reason
    assert "lease generation changed" not in missing_reason
    lease = _claim(authority, "held")
    wrong = authority.advance_cursor(
        "held",
        nonce="not-the-holder",
        expected_cursor_version=0,
        expected_stream_snapshots={"ghost-stream": token},
        advances={"ghost-stream": 1},
        actor="test-operator",
    )
    assert wrong.cas_lost
    wrong_reason = wrong.reason or ""
    assert "lease generation changed" in wrong_reason
    assert "no active lease exists" not in wrong_reason
    assert lease.nonce != "not-the-holder"


def _set_cursor(project: Path, label: str, *args: str) -> tuple[int, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        try:
            rc = gm.main([
                "set", "--project-root", str(project), "--controller-label", label,
                "--json", *args,
            ])
        except SystemExit as exc:
            rc = exc.code
    return rc, stdout.getvalue(), stderr.getvalue()


@pytest.mark.parametrize("drain", [False, True], ids=["position", "drain"])
def test_owner_set_unwedges_changed_range_stale_version_and_retired_lease(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, drain: bool
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    project = _git_project(tmp_path)
    authority = journal.open_or_create_journal(project)
    lease = _claim(authority, "owner")
    messages_dir = Path(os.environ["GOALFLIGHT_MESSAGES_DIR"])
    _post(project, messages_dir, "owner", "withdrawn before acknowledgement")
    _post(project, messages_dir, "owner", "processed through here")
    peek = authority.cursor_peek("owner", nonce=lease.nonce)
    first = peek.items[0]
    assert authority.withdraw_delivery_event(
        recipient_label="owner", origin_node=first["origin_node"],
        event_uuid=first["event_uuid"],
    ).committed
    changed = authority.cursor_peek("owner", nonce=lease.nonce)
    assert changed.cursor_version > peek.cursor_version
    assert changed.stream_snapshots != peek.stream_snapshots
    old_inputs = dict(
        nonce=lease.nonce, expected_cursor_version=peek.cursor_version,
        expected_stream_snapshots=peek.stream_snapshots,
        advances={STREAM: 2}, actor="test-controller",
    )
    refused = authority.advance_cursor("owner", **old_inputs)
    assert refused.cas_lost and "unseen position" in refused.reason
    assert authority.release_lease("owner", nonce=lease.nonce).committed
    assert authority.active_lease("owner") is None
    refused = authority.advance_cursor("owner", **old_inputs)
    assert refused.cas_lost and "no active lease" in refused.reason

    # Even inherited stale identity must not become an owner-set precondition.
    monkeypatch.setenv("GOALFLIGHT_CONTROLLER_LEASE_NONCE", lease.nonce)
    args = ("--drain",) if drain else ("--position", f"{STREAM}=2")
    rc, stdout, stderr = _set_cursor(project, "owner", *args)
    assert rc == 0, stderr or stdout
    assert json.loads(stdout)["advances"] == {STREAM: 2}
    cursor = authority.cursor_status("owner")
    assert cursor["positions"] == {STREAM: 2}
    assert cursor["backlog_pending"] == 0
    assert cursor["advanced_by"].startswith("controller:")
    assert _pending(authority, "owner") == []


def test_owner_set_keeps_mail_after_named_position_pending(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    project = _git_project(tmp_path)
    authority = journal.open_or_create_journal(project)
    _claim(authority, "owner")
    _claim(authority, "peer")
    messages_dir = Path(os.environ["GOALFLIGHT_MESSAGES_DIR"])
    first = _post(project, messages_dir, "owner", "processed")
    position = first["envelope"]["seq"]
    # Arrivals between choosing the position and writing it remain unread.
    _post(project, messages_dir, "owner", "arrived after position was chosen")
    _post(project, messages_dir, "owner", "different stream", dispatch_id="untouched")
    _post(project, messages_dir, "peer", "peer mail", dispatch_id="peer-stream")
    for _ in range(2):  # Repeating an owner's set must also succeed.
        rc, stdout, stderr = _set_cursor(project, "owner", "--position", f"{STREAM}={position}")
        assert rc == 0, stderr or stdout
        assert _pending(authority, "owner") == [(STREAM, 2), ("untouched", 1)]
        assert authority.cursor_status("owner")["positions"] == {STREAM: position}
        assert authority.cursor_status("owner")["backlog_pending"] == 1
        assert _pending(authority, "peer") == [("peer-stream", 1)]


def test_owner_set_refuses_unknown_forward_positions_without_a_lease(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    project = _git_project(tmp_path)
    authority = journal.open_or_create_journal(project)
    assert authority.cursor_status("owner") is None
    for position in (9, 3):
        rc, stdout, stderr = _set_cursor(
            project, "owner", "--position", f"{STREAM}={position}", "other=4"
        )
        assert rc == 3, (stdout, stderr)
        assert "no safe materialized prefix" in stderr
        assert authority.cursor_status("owner") is None
        assert authority.active_lease("owner") is None
    rc, stdout, stderr = _set_cursor(
        project, "owner", "--position", f"{STREAM}=0", "other=0"
    )
    assert rc == 0, stderr or stdout
    assert authority.cursor_status("owner")["positions"] == {STREAM: 0, "other": 0}


def test_owner_set_drain_includes_all_deliverable_streams_and_is_repeatable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    project = _git_project(tmp_path)
    authority = journal.open_or_create_journal(project)
    messages_dir = Path(os.environ["GOALFLIGHT_MESSAGES_DIR"])
    _post(project, messages_dir, "owner", "first stream")
    _post(project, messages_dir, "owner", "same stream")
    _post(project, messages_dir, "owner", "last stream", dispatch_id="z-last")
    for _ in range(2):
        rc, stdout, stderr = _set_cursor(project, "owner", "--drain")
        assert rc == 0, stderr or stdout
        assert authority.cursor_status("owner")["positions"] == {STREAM: 2, "z-last": 1}
        assert _pending(authority, "owner") == []


def test_owner_set_drain_stops_before_unmaterialized_rows_without_a_peek_cap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    project = _git_project(tmp_path)
    authority = journal.open_or_create_journal(project)
    _claim(authority, "owner")
    messages_dir = Path(os.environ["GOALFLIGHT_MESSAGES_DIR"])
    _post(project, messages_dir, "owner", "seed")

    def seed(connection):
        # Bulk fixture beyond cursor_peek's maximum of 10,000. The first missing
        # carrier event is a head-of-line barrier; later rows remain pending.
        connection.execute(
            """
            WITH RECURSIVE seqs(n) AS (
                SELECT 2 UNION ALL SELECT n + 1 FROM seqs WHERE n < 10003
            )
            INSERT INTO delivery_events (
                project_root, recipient_label, origin_node, event_uuid,
                stream_id, stream_seq, carrier_path, event_type, wake_class,
                created_at, projected_at, withdrawn_at
            )
            SELECT e.project_root, e.recipient_label, e.origin_node,
                   printf('%08x-0000-4000-8000-000000000000', n), e.stream_id,
                   n, e.carrier_path, e.event_type, e.wake_class, e.created_at,
                   CASE WHEN n = 10002 THEN NULL ELSE e.projected_at END,
                   CASE WHEN n = 10003 THEN e.projected_at ELSE NULL END
            FROM delivery_events AS e CROSS JOIN seqs WHERE e.stream_seq = 1
            """
        )

    assert authority._domain_write(seed).committed
    _post(project, messages_dir, "owner", "last stream", dispatch_id="z-last")
    result = authority.set_cursor("owner", positions=None, actor="test-owner")
    assert result.committed, result.reason
    assert authority.cursor_status("owner")["positions"] == {STREAM: 1, "z-last": 1}
    pending = _pending(authority, "owner", limit=10_000)
    assert len(pending) == 10_000
    assert pending[0] == (STREAM, 2) and pending[-1] == (STREAM, 10001)
    assert len(result.value["pending_behind_stuck"]) == 10_000
    # Projection beyond the drained boundary remains pending when it arrives.
    row = authority.read_all(
        "SELECT origin_node, event_uuid FROM delivery_events WHERE stream_seq = 10002"
    )[0]
    assert authority.mark_delivery_projected(
        recipient_label="owner", origin_node=row["origin_node"], event_uuid=row["event_uuid"],
    ).committed
    # pending_delivery_events has a hard 10,000-row API limit. Earlier rows
    # intentionally remain pending, so seq 10002 need not appear in this
    # capped result; the invariant is that materialization leaves it durable,
    # unwithdrawn, and beyond the unchanged cursor for a later bounded read.
    pending = _pending(authority, "owner", limit=10_000)
    assert len(pending) == 10_000 and pending[-1] == (STREAM, 10001)
    assert (STREAM, 10002) not in pending
    late_row = authority.read_all(
        "SELECT projected_at, withdrawn_at FROM delivery_events "
        "WHERE recipient_label = 'owner' AND stream_id = ? AND stream_seq = 10002",
        (STREAM,),
    )
    assert len(late_row) == 1
    assert late_row[0]["projected_at"] is not None and late_row[0]["withdrawn_at"] is None
    assert authority.cursor_status("owner")["positions"] == {STREAM: 1, "z-last": 1}
