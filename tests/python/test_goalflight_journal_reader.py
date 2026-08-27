#!/usr/bin/env python3
"""Readonly journal recovery and corruption-classification contracts."""

from __future__ import annotations

import ast
import contextlib
import os
from pathlib import Path
import sqlite3
import sys
import textwrap

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SCRIPTS))

import goalflight_journal as journal  # noqa: E402


def _set_state_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    values = {
        "GOALFLIGHT_TASK_STORE_DIR": str(tmp_path / "task-store"),
        "GOALFLIGHT_JOURNAL_DIR": str(tmp_path / "journal-state"),
        "GOALFLIGHT_MESSAGES_DIR": str(tmp_path / "messages"),
        "GOALFLIGHT_STATE_DIR": str(tmp_path / "dispatch-state"),
        "GOALFLIGHT_WAKE_LEDGER_DIR": str(tmp_path / "wake-ledger"),
        "GOAL_FLIGHT_PIDFILE_DIR": str(tmp_path / "pidfiles"),
        "GOALFLIGHT_CAPACITY_CONF": "/dev/null",
        "GOALFLIGHT_DISABLE_NUDGES": "1",
        "GOALFLIGHT_TEST_MODE": "1",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def _project(tmp_path: Path, name: str = "project") -> Path:
    project = tmp_path / name
    project.mkdir()
    return project


def _quiesced_journal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[Path, journal.Journal]:
    _set_state_env(monkeypatch, tmp_path)
    project = _project(tmp_path)
    authority = journal.Journal.create(project)
    prepared = authority.prepare_attempt("row-survives-quiescence")
    assert prepared.committed and prepared.value is not None
    with contextlib.closing(
        sqlite3.connect(authority.path, timeout=0, isolation_level=None)
    ) as connection:
        checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    assert checkpoint == (0, 0, 0)
    for suffix in ("-shm", "-wal"):
        Path(f"{authority.path}{suffix}").unlink(missing_ok=True)
        assert not Path(f"{authority.path}{suffix}").exists()
    return project, authority


def _force_readonly_cantopen(
    monkeypatch: pytest.MonkeyPatch,
) -> list[str]:
    calls: list[str] = []
    real_connect = journal._sqlite_connect

    def injected_connect(
        database: str | Path,
        *,
        uri: bool = False,
        timeout: float = 5.0,
        isolation_level: str | None = "",
    ) -> sqlite3.Connection:
        location = os.fspath(database)
        calls.append(location)
        if "?mode=ro" in location:
            raise sqlite3.OperationalError("unable to open database file")
        return real_connect(
            database,
            uri=uri,
            timeout=timeout,
            isolation_level=isolation_level,
        )

    monkeypatch.setattr(journal, "_sqlite_connect", injected_connect)
    return calls


def test_quiesced_wal_reader_falls_back_and_preserves_rows(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project, _authority = _quiesced_journal(monkeypatch, tmp_path)
    calls = _force_readonly_cantopen(monkeypatch)

    reader = journal.Journal.open_reader(project)
    attempt = reader.attempt_for_dispatch("row-survives-quiescence")

    assert attempt is not None
    assert attempt.dispatch_id == "row-survives-quiescence"
    assert any("?mode=ro" in call for call in calls)
    assert any("?mode=rw" in call for call in calls)


def test_query_only_hardens_quiesced_wal_fallback_handle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project, authority = _quiesced_journal(monkeypatch, tmp_path)
    calls = _force_readonly_cantopen(monkeypatch)
    reader = journal.Journal.open_reader(project)

    with contextlib.closing(reader._connect()) as connection:
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute(
                "DELETE FROM dispatch_attempts WHERE dispatch_id = ?",
                ("row-survives-quiescence",),
            )

    assert any("?mode=rw" in call for call in calls)
    assert authority.attempt_for_dispatch("row-survives-quiescence") is not None


def test_corrupt_file_still_reports_corruption_through_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project, authority = _quiesced_journal(monkeypatch, tmp_path)
    with authority.path.open("r+b") as handle:
        handle.write(b"not-a-sqlite-header-overwrite!!!")
        handle.flush()
        os.fsync(handle.fileno())
    calls = _force_readonly_cantopen(monkeypatch)

    reader = journal.Journal.open_reader(project)
    with pytest.raises(
        journal.JournalIntegrityError,
        match=r"integrity check failed.*(file is not a database|malformed)",
    ):
        reader.epochs()

    assert any("?mode=rw" in call for call in calls)


def test_present_file_with_both_opens_failing_is_probe_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project, _authority = _quiesced_journal(monkeypatch, tmp_path)

    def unavailable_connect(
        database: str | Path,
        *,
        uri: bool = False,
        timeout: float = 5.0,
        isolation_level: str | None = "",
    ) -> sqlite3.Connection:
        del database, uri, timeout, isolation_level
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(journal, "_sqlite_connect", unavailable_connect)
    reader = journal.Journal.open_reader(project, open_retry_budget_s=0.02)
    with pytest.raises(
        journal.JournalIOError,
        match="probe unavailable/unreadable",
    ) as captured:
        reader.epochs()

    assert not isinstance(captured.value, journal.JournalIntegrityError)


def test_absent_reader_keeps_legacy_unavailable_semantics(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    project = _project(tmp_path)

    with pytest.raises(journal.JournalDisappeared, match="journal database is absent"):
        journal.Journal.open_reader(project)


class _TargetedBusyConnection:
    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        needle: str,
        hits: list[str],
    ) -> None:
        self._connection = connection
        self._needle = needle
        self._hits = hits

    def execute(self, sql: str, *args: object, **kwargs: object):
        normalized = " ".join(sql.split())
        if self._needle in normalized:
            self._hits.append(normalized)
            raise sqlite3.OperationalError("database is locked")
        return self._connection.execute(sql, *args, **kwargs)

    def __getattr__(self, name: str):
        return getattr(self._connection, name)


@pytest.mark.parametrize(
    ("read_stage", "needle"),
    (
        ("read_all", "SELECT 1 AS injected_read"),
        ("cursor_peek", "BEGIN"),
    ),
)
def test_every_query_stage_busy_is_normalized_as_journal_busy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    read_stage: str,
    needle: str,
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    project = _project(tmp_path)
    authority = journal.Journal.create(project)
    claimed = authority.claim_or_renew_lease(
        "query-busy-reader",
        principal={"principal_id": "query-busy-reader"},
    )
    assert claimed.committed and claimed.value is not None
    reader = journal.Journal.open_reader(project, retry_budget_s=0)
    real_connect = journal.Journal._connect
    hits: list[str] = []

    def injected_connect(current: journal.Journal, **kwargs: object):
        return _TargetedBusyConnection(
            real_connect(current, **kwargs),
            needle=needle,
            hits=hits,
        )

    monkeypatch.setattr(journal.Journal, "_connect", injected_connect)
    with pytest.raises(journal.JournalBusy, match="remained busy"):
        if read_stage == "read_all":
            reader.read_all("SELECT 1 AS injected_read")
        else:
            reader.cursor_peek(
                claimed.value.label,
                nonce=claimed.value.nonce,
            )

    assert hits, f"{read_stage} query-stage injection did not bind"


def test_read_all_replays_generator_bindings_after_query_stage_busy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A busy first execute must not consume one-shot bindings for the retry."""
    _set_state_env(monkeypatch, tmp_path)
    project = _project(tmp_path)
    journal.Journal.create(project)
    reader = journal.Journal.open_reader(
        project,
        retry_budget_s=1.0,
        jitter_min_s=0,
        jitter_max_s=0,
    )
    real_connect = journal.Journal._connect
    observed_bindings: list[tuple[object, ...]] = []

    class BusyOnceConnection:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self._connection = connection

        def execute(self, sql: str, *args: object, **kwargs: object):
            if "SELECT ? AS replayed_value" in " ".join(sql.split()):
                bindings = tuple(args[0]) if args else ()
                observed_bindings.append(bindings)
                if len(observed_bindings) == 1:
                    raise sqlite3.OperationalError("database is locked")
            return self._connection.execute(sql, *args, **kwargs)

        def __getattr__(self, name: str):
            return getattr(self._connection, name)

    def injected_connect(current: journal.Journal, **kwargs: object):
        return BusyOnceConnection(real_connect(current, **kwargs))

    monkeypatch.setattr(journal.Journal, "_connect", injected_connect)
    rows = reader.read_all(
        "SELECT ? AS replayed_value",
        (value for value in (41,)),
    )

    assert [row["replayed_value"] for row in rows] == [41]
    assert observed_bindings == [(41,), (41,)]


@pytest.mark.parametrize(
    "failure",
    (
        journal.JournalDisappeared("injected arm disappearance"),
        journal.JournalIOError("injected arm path I/O failure"),
    ),
)
def test_domain_write_preserves_nonbusy_journal_failure_type(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: journal.JournalUnavailable,
) -> None:
    """Coverage-arm writes may retry busy, never disappearance or path I/O."""
    _set_state_env(monkeypatch, tmp_path)
    project = _project(tmp_path)
    authority = journal.Journal.create(project)
    claimed = authority.claim_or_renew_lease(
        "typed-arm-failure",
        principal={"principal_id": "typed-arm-failure"},
    )
    assert claimed.committed and claimed.value is not None
    monkeypatch.setattr(
        authority,
        "_connect",
        lambda **_kwargs: (_ for _ in ()).throw(failure),
    )

    with pytest.raises(type(failure), match="injected arm"):
        authority.arm_listener(
            claimed.value.label,
            nonce=claimed.value.nonce,
            pid=os.getpid(),
            start_token="typed-arm-failure",
            parent_pid=os.getppid() or os.getpid(),
        )


@pytest.mark.parametrize(
    "failure",
    (
        journal.JournalDisappeared("injected row-write disappearance"),
        journal.JournalIOError("injected row-write path I/O failure"),
    ),
)
def test_row_write_preserves_nonbusy_journal_failure_type(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: journal.JournalUnavailable,
) -> None:
    """A failed write-open is retryable only when its type says busy."""
    _set_state_env(monkeypatch, tmp_path)
    project = _project(tmp_path)
    authority = journal.Journal.create(project)
    monkeypatch.setattr(
        authority,
        "_connect",
        lambda **_kwargs: (_ for _ in ()).throw(failure),
    )

    operation = journal.RowOperation.insert(
        "injected_table",
        {"injected_column": "value"},
    )
    with pytest.raises(type(failure), match="injected row-write"):
        authority.write(operation)


@pytest.mark.parametrize(
    "failure",
    (
        journal.JournalDisappeared("injected startup disappearance"),
        journal.JournalIOError("injected startup path I/O failure"),
    ),
)
def test_startup_context_preserves_nonbusy_journal_failure_type(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: journal.JournalUnavailable,
) -> None:
    """The schema-open context wrapper must not erase the fatal subtype."""
    _set_state_env(monkeypatch, tmp_path)
    project = _project(tmp_path)
    journal.Journal.create(project)
    real_connect = journal.Journal._connect
    calls: list[str] = []

    def fail_second_connect(authority: journal.Journal, **kwargs: object):
        calls.append("connect")
        if len(calls) == 2:
            raise failure
        return real_connect(authority, **kwargs)

    monkeypatch.setattr(journal.Journal, "_connect", fail_second_connect)

    with pytest.raises(type(failure), match="journal startup could not open"):
        journal.Journal(project)
    assert calls == ["connect", "connect"], "failure did not bind at schema startup"


def test_construction_shares_one_busy_deadline_across_lock_and_open_stages(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Integrity and bootstrap cannot each restart the writer construction budget."""
    _set_state_env(monkeypatch, tmp_path)
    project = _project(tmp_path)
    journal.Journal.create(project)
    clock = [100.0]
    observed: dict[str, float] = {}

    class AcquiredLock:
        def release(self) -> None:
            observed["released_at"] = clock[0]

    def fake_try_acquire(
        _cls: type,
        _path: Path,
        *,
        deadline_s: float,
        poll_s: float = 0.010,
    ) -> AcquiredLock:
        del poll_s
        observed["lock_deadline"] = deadline_s
        clock[0] = 102.0
        return AcquiredLock()

    connect_deadlines: list[float | None] = []
    real_connect = journal.Journal._connect

    def tracking_connect(
        current: journal.Journal, *, busy_deadline_s: float | None = None
    ):
        connect_deadlines.append(busy_deadline_s)
        if len(connect_deadlines) == 1:
            return real_connect(current, busy_deadline_s=busy_deadline_s)
        raise journal.JournalBusy("startup stayed busy after the lock wait")

    monkeypatch.setattr(journal.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        journal.goalflight_task.FileLock,
        "try_acquire",
        classmethod(fake_try_acquire),
    )
    monkeypatch.setattr(journal.Journal, "_connect", tracking_connect)

    with pytest.raises(journal.JournalBusy, match="startup stayed busy"):
        journal.Journal(project)

    assert observed["lock_deadline"] == 105.0
    assert connect_deadlines[:2] == [105.0, 105.0]
    assert observed["released_at"] == 102.0


def test_domain_write_shares_lock_and_connect_busy_deadline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Coverage arm cannot restart its 10-second budget after the write lock."""
    _set_state_env(monkeypatch, tmp_path)
    project = _project(tmp_path)
    authority = journal.Journal.create(project, retry_budget_s=10.0)
    clock = [100.0]
    observed: dict[str, float] = {}

    class AcquiredLock:
        def release(self) -> None:
            observed["released_at"] = clock[0]

    def fake_try_acquire(
        _cls: type,
        _path: Path,
        *,
        deadline_s: float,
        poll_s: float = 0.010,
    ) -> AcquiredLock:
        del poll_s
        observed["lock_deadline"] = deadline_s
        clock[0] = 109.0
        return AcquiredLock()

    def busy_connect(*, busy_deadline_s: float | None = None):
        assert busy_deadline_s is not None
        observed["connect_deadline"] = busy_deadline_s
        raise journal.JournalBusy("connect stayed busy after the lock wait")

    monkeypatch.setattr(journal.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        journal.goalflight_task.FileLock,
        "try_acquire",
        classmethod(fake_try_acquire),
    )
    monkeypatch.setattr(authority, "_connect", busy_connect)

    result = authority._domain_write(lambda _connection: None)

    assert result.retryable
    assert observed == {
        "lock_deadline": 110.0,
        "connect_deadline": 110.0,
        "released_at": 109.0,
    }


@pytest.mark.parametrize(
    ("deadline_s", "busy_at_s"),
    (
        (100.0, 100.0),  # An upstream stage already consumed the budget.
        (101.0, 101.0),  # The first timeout=0 connect consumed the budget.
    ),
)
def test_connect_stops_after_one_attempt_when_shared_deadline_is_spent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    deadline_s: float,
    busy_at_s: float,
) -> None:
    """One attempt in one second is valid when elapsed time spends the budget."""
    _set_state_env(monkeypatch, tmp_path)
    project = _project(tmp_path)
    journal.Journal.create(project)
    authority = journal.Journal.open_reader(
        project,
        retry_budget_s=1.0,
        jitter_min_s=0,
        jitter_max_s=0,
    )
    clock = [100.0]
    attempts: list[float] = []

    def one_slow_busy(*_args: object, **_kwargs: object):
        attempts.append(clock[0])
        clock[0] = busy_at_s
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(journal.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(journal, "_open_readonly_connection", one_slow_busy)

    with pytest.raises(journal.JournalBusy, match="after 1 attempts within 1.000s"):
        authority._connect(busy_deadline_s=deadline_s)
    assert attempts == [100.0]


def _exception_names(node: ast.expr | None) -> list[str]:
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, ast.Attribute):
        return [node.attr]
    if isinstance(node, ast.Tuple):
        return [name for item in node.elts for name in _exception_names(item)]
    return []


def _expr_is_none(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


def _expr_is_empty_list(node: ast.expr) -> bool:
    return isinstance(node, ast.List) and not node.elts


def _expr_is_false(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and node.value is False


def _call_direct_values(call: ast.Call) -> list[ast.expr]:
    values: list[ast.expr] = []
    for arg in call.args:
        if not isinstance(arg, ast.Starred):
            values.append(arg)
    for keyword in call.keywords:
        if keyword.arg is not None:
            values.append(keyword.value)
    return values


def _uses_name(tree: ast.AST, name: str) -> bool:
    return any(
        isinstance(node, ast.Name) and node.id == name for node in ast.walk(tree)
    )


def _handler_maps_unavailable_to_unknown(handler: ast.ExceptHandler) -> bool:
    """True when the handler fails closed into unknown, or re-raises.

    The exemption is earned by what the body *does*, not by a comment,
    decorator, or helper name. Safe shapes:

    - a single ``raise`` / ``raise <bound name>`` (propagate)
    - a single ``return <call>(..., None, ...)`` that threads the bound
      exception into that result and does not also pass ``[]`` or ``False``

    ``return []``, ``return False``, ``return None``, ``pass``, ``continue``,
    and ``return fail_closed_unknown(exc)`` (a name with no unknown slot)
    do not qualify: they invent a definite outcome or hide the mapping
    behind a convention the next author can copy blindly.
    """
    if len(handler.body) != 1:
        return False
    stmt = handler.body[0]
    if isinstance(stmt, ast.Raise):
        if stmt.exc is None:
            return True
        return (
            handler.name is not None
            and isinstance(stmt.exc, ast.Name)
            and stmt.exc.id == handler.name
        )
    if not isinstance(stmt, ast.Return) or stmt.value is None:
        return False
    value = stmt.value
    if not isinstance(value, ast.Call):
        return False
    if handler.name is None or not _uses_name(value, handler.name):
        return False
    direct_values = _call_direct_values(value)
    has_unknown_slot = any(_expr_is_none(item) for item in direct_values)
    has_definite_answer = any(
        _expr_is_empty_list(item) or _expr_is_false(item) for item in direct_values
    )
    return has_unknown_slot and not has_definite_answer


def availability_handler_violations(tree: ast.AST, *, path_name: str) -> list[str]:
    """AST scan: handlers must name concrete availability subtypes.

    Catching ``JournalUnavailable`` is forbidden unless the handler's shape
    maps the ABC to an indeterminate result so a *future subclass* fails
    closed into unknown rather than escaping. Returning a definite answer
    (empty list, False, None-as-absent, or pass) still violates. The
    JournalError-widening rule is separate and has no such exemption:
    a handler naming ``JournalError`` must still already name Busy,
    Disappeared, and IOError.
    """
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Try):
            named_before: set[str] = set()
            for handler in node.handlers:
                names = set(_exception_names(handler.type))
                if "JournalError" in names and not {
                    "JournalBusy",
                    "JournalDisappeared",
                    "JournalIOError",
                } <= named_before | names:
                    violations.append(
                        f"{path_name}:{handler.lineno}: JournalError widens availability"
                    )
                named_before.update(names)
        elif isinstance(node, ast.ExceptHandler):
            if "JournalUnavailable" in _exception_names(node.type):
                if not _handler_maps_unavailable_to_unknown(node):
                    violations.append(
                        f"{path_name}:{node.lineno}: except JournalUnavailable"
                    )
        elif isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
            if "JournalUnavailable" in _exception_names(node.exc.func):
                violations.append(f"{path_name}:{node.lineno}: raise JournalUnavailable")
    return violations


def test_journal_unavailable_handlers_name_concrete_subclasses() -> None:
    """No handler or producer may flatten the three availability outcomes.

    The one production exemption is a fail-closed ABC catch whose body
    returns unknown (see availability_handler_violations). A definite
    answer at such a site must still fail this test.
    """
    violations: list[str] = []
    for path in sorted(SCRIPTS.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        violations.extend(
            availability_handler_violations(tree, path_name=path.name)
        )
    assert violations == []


@pytest.mark.parametrize(
    ("source", "expected_needle"),
    (
        (
            """
            def fn():
                try:
                    read()
                except JournalUnavailable as exc:
                    return Lookup(sessions=None, unreadable_reason=str(exc))
            """,
            None,
        ),
        (
            """
            def fn():
                try:
                    read()
                except JournalUnavailable:
                    raise
            """,
            None,
        ),
        (
            """
            def fn():
                try:
                    read()
                except JournalUnavailable as exc:
                    raise exc
            """,
            None,
        ),
        (
            """
            def fn():
                try:
                    read()
                except JournalUnavailable:
                    pass
            """,
            "except JournalUnavailable",
        ),
        (
            """
            def fn():
                try:
                    read()
                except JournalUnavailable:
                    return []
            """,
            "except JournalUnavailable",
        ),
        (
            """
            def fn():
                try:
                    read()
                except JournalUnavailable:
                    return False
            """,
            "except JournalUnavailable",
        ),
        (
            """
            def fn():
                try:
                    read()
                except JournalUnavailable:
                    return None
            """,
            "except JournalUnavailable",
        ),
        (
            """
            def fn():
                try:
                    read()
                except JournalUnavailable as exc:
                    return Lookup(sessions=[])
            """,
            "except JournalUnavailable",
        ),
        (
            """
            def fn():
                try:
                    read()
                except JournalUnavailable as exc:
                    return Lookup(sessions=None)
            """,
            "except JournalUnavailable",
        ),
        (
            """
            def fn():
                try:
                    read()
                except JournalUnavailable as exc:
                    return Lookup(sessions=[], unreadable_reason=str(exc))
            """,
            "except JournalUnavailable",
        ),
        (
            """
            def fn():
                try:
                    read()
                except JournalUnavailable as exc:
                    return fail_closed_unknown(exc)
            """,
            "except JournalUnavailable",
        ),
        (
            """
            def fn():
                while True:
                    try:
                        read()
                    except JournalUnavailable:
                        continue
            """,
            "except JournalUnavailable",
        ),
        (
            """
            def fn():
                try:
                    read()
                except JournalError as exc:
                    return Lookup(sessions=None, unreadable_reason=str(exc))
            """,
            "JournalError widens availability",
        ),
    ),
)
def test_journal_unavailable_abc_exemption_is_earned_by_unknown_shape(
    source: str,
    expected_needle: str | None,
) -> None:
    """A definite ABC catch still violates; only unknown/propagate is exempt."""
    tree = ast.parse(textwrap.dedent(source), filename="snippet.py")
    violations = availability_handler_violations(tree, path_name="snippet.py")
    if expected_needle is None:
        assert violations == []
    else:
        assert violations, f"expected a violation containing {expected_needle!r}"
        assert any(expected_needle in item for item in violations), violations


def test_attention_items_use_one_bounded_journal_read(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Synthetic-envelope batching must not widen the 10-second operation bound."""
    _set_state_env(monkeypatch, tmp_path)
    project = _project(tmp_path)
    authority = journal.Journal.create(project)
    real_read_all = authority.read_all
    calls: list[str] = []

    def measured_read(sql: str, parameters=()):
        calls.append(" ".join(sql.split()))
        return real_read_all(sql, parameters)

    monkeypatch.setattr(authority, "read_all", measured_read)
    assert authority.attention_items() == []
    assert len(calls) == 1
    assert "UNION ALL" in calls[0]


def test_snapshot_and_restore_readers_share_query_only_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    project = _project(tmp_path)
    authority = journal.Journal.create(project)
    snapshot = authority.snapshot(tmp_path / "snapshot.sqlite3")
    calls = _force_readonly_cantopen(monkeypatch)

    restored = journal.restore_snapshot(project, snapshot, i_understand=True)

    assert restored == authority.path
    assert sum("?mode=ro" in call for call in calls) >= 3
    assert sum("?mode=rw" in call for call in calls) >= 3
