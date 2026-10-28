"""Hermetic fixtures for the dispatch final-report command."""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_dispatch as D  # noqa: E402


def _invoke(monkeypatch, capsys, tmp_path: Path, dispatch_id: str, tail_text: str, **record_fields):
    tail = tmp_path / f"{dispatch_id}.tail"
    tail.write_text(tail_text, encoding="utf-8")
    record = {
        "dispatch_id": dispatch_id,
        "state": "complete",
        "terminal_state": "complete",
        "engine": "codex" if dispatch_id.startswith("codex") else "grok",
        "account": "fixture-account",
        "worktree_base": "base-sha",
        "worktree_head": "head-sha",
        "project_root": str(tmp_path),
        "worker_cwd": str(tmp_path),
        "stdout_path": str(tail),
        **record_fields,
    }
    monkeypatch.setattr(D, "_find_dispatch_record", lambda _dispatch_id: record)
    return D._cmd_final([dispatch_id]), capsys.readouterr()


@pytest.mark.parametrize(
    "prelaunch_failure", [False, True], ids=["launched", "prelaunch-failure"]
)
def test_reconciled_terminal_status_worktree_fields(
    monkeypatch, tmp_path: Path, prelaunch_failure: bool
) -> None:
    dispatch_id = "codex-reconciled-seat"
    status_path = tmp_path / f"{dispatch_id}.status.json"
    record = {
        "dispatch_id": dispatch_id,
        "state": "complete",
        "terminal_state": "complete",
        "worktree_path": str(tmp_path / "worktrees" / "s-5"),
        "worktree_seat": "s-5",
    }
    if prelaunch_failure:
        record["prelaunch_failure"] = True
    monkeypatch.setattr(D, "_find_dispatch_record", lambda _dispatch_id: record)

    D._write_reconciled_terminal_status(
        {
            "dispatch_id": dispatch_id,
            "agent": "codex",
            "shape": "bash",
            "project_root": str(tmp_path),
            "request": {"status_json": str(status_path)},
        },
        None,
    )

    status = json.loads(status_path.read_text(encoding="utf-8"))
    if prelaunch_failure:
        assert status.get("prelaunch_failure") is True, status
        assert "worktree_path" not in status, status
        assert "worktree_seat" not in status, status
    else:
        assert status["worktree_path"] == record["worktree_path"], status
        assert status["worktree_seat"] == record["worktree_seat"], status


@pytest.mark.parametrize(
    "prelaunch_failure", [False, True], ids=["launched", "prelaunch-failure"]
)
def test_recovered_claim_status_worktree_fields(
    monkeypatch, tmp_path: Path, prelaunch_failure: bool
) -> None:
    dispatch_id = "codex-recovered-claim-seat"
    status_path = tmp_path / f"{dispatch_id}.status.json"
    tail_path = tmp_path / f"{dispatch_id}.tail"
    tail_path.write_text("worker stopped\n", encoding="utf-8")
    record = {
        "dispatch_id": dispatch_id,
        "state": "worker_dead",
        "terminal_state": "worker_dead",
        "worktree_path": str(tmp_path / "worktrees" / "s-6"),
        "worktree_seat": "s-6",
    }
    if prelaunch_failure:
        record["prelaunch_failure"] = True
    entry = {
        "dispatch_id": dispatch_id,
        "agent": "codex",
        "shape": "bash",
        "project_root": str(tmp_path),
        "task_ids": [],
        "request": {
            "status_json": str(status_path),
            "tail": str(tail_path),
        },
    }

    class Transaction:
        def release(self):
            pass

    monkeypatch.setattr(
        D,
        "_queued_args_for_status",
        lambda _entry: SimpleNamespace(
            agent="codex", shape="bash", task_ids=[], prompt_file=None
        ),
    )
    monkeypatch.setattr(D.goalflight_task, "resolve_project_root", lambda value: Path(value))
    monkeypatch.setattr(D, "_ignore_prefix_lines", lambda _prompt: 0)
    monkeypatch.setattr(
        D,
        "_resolve_claim_terminal_outcome",
        lambda *_args, **_kwargs: ("worker_dead", "orphaned_claim", None),
    )
    monkeypatch.setattr(D, "_find_dispatch_record", lambda _dispatch_id: record)
    monkeypatch.setattr(D, "_is_task_linked", lambda *_args: False)
    monkeypatch.setattr(
        D,
        "_begin_reconcile_transaction",
        lambda *_args, **_kwargs: SimpleNamespace(
            transaction=Transaction(), refusal=None
        ),
    )
    monkeypatch.setattr(
        D,
        "_finish_ledger_under_lock",
        lambda *_args, **_kwargs: SimpleNamespace(
            committed=True, durable_state="worker_dead"
        ),
    )
    monkeypatch.setattr(D, "_maybe_requeue_terminal_claim", lambda *_args, **_kwargs: True)

    assert D._mark_claim_worker_dead(
        entry, reason="orphaned_claim", queue_dir=tmp_path / "queue"
    )

    status = json.loads(status_path.read_text(encoding="utf-8"))
    if prelaunch_failure:
        assert status.get("prelaunch_failure") is True, status
        assert "worktree_path" not in status, status
        assert "worktree_seat" not in status, status
    else:
        assert status["worktree_path"] == record["worktree_path"], status
        assert status["worktree_seat"] == record["worktree_seat"], status


def _status_seat_field_projection_sites(
    source_text: str,
) -> tuple[str, set[str], list[tuple[str, int]], set[str]]:
    tree = ast.parse(source_text)

    def call_name(node: ast.Call) -> str | None:
        if isinstance(node.func, ast.Name):
            return node.func.id
        return node.func.attr if isinstance(node.func, ast.Attribute) else None

    def field_name(node: ast.AST) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in seat_fields:
            return node.value
        return None

    def status_payload(node: ast.Call) -> ast.AST | None:
        if call_name(node) == "write_status" and len(node.args) > 1:
            return node.args[1]
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "write"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "status_lock"
            and node.args
        ):
            return node.args[0]
        return None

    def function_nodes(function: ast.AST) -> list[ast.AST]:
        nodes = []
        pending = list(function.body)
        while pending:
            node = pending.pop()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            nodes.append(node)
            pending.extend(ast.iter_child_nodes(node))
        return nodes

    functions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    canonical = next(
        function
        for function in functions
        if function.name == "_dispatch_status_worktree_fields"
    )
    canonical_comprehensions = [
        node for node in ast.walk(canonical) if isinstance(node, ast.DictComp)
    ]
    assert len(canonical_comprehensions) == 1, canonical_comprehensions
    canonical_projection = canonical_comprehensions[0]
    seat_fields = {
        value.value
        for generator in canonical_projection.generators
        for value in ast.walk(generator.iter)
        if isinstance(value, ast.Constant)
        and isinstance(value.value, str)
        and value.value.startswith("worktree_")
    }
    assert seat_fields

    def projections_in_function(function: ast.AST) -> list[ast.AST]:
        nodes = sorted(
            function_nodes(function),
            key=lambda node: (getattr(node, "lineno", 0), getattr(node, "col_offset", 0)),
        )
        fields_by_name: dict[str, list[ast.AST]] = {}

        def mapping_projections(expression: ast.AST) -> list[ast.AST]:
            if isinstance(expression, ast.Dict):
                projections = [key for key in expression.keys if field_name(key)]
                for key, value in zip(expression.keys, expression.values):
                    if key is None:
                        projections.extend(mapping_projections(value))
                return projections
            if isinstance(expression, ast.DictComp):
                if field_name(expression.key):
                    return [expression.key]
                for generator in expression.generators:
                    selected = {
                        value.value
                        for value in ast.walk(generator.iter)
                        if isinstance(value, ast.Constant)
                        and isinstance(value.value, str)
                        and value.value in seat_fields
                    }
                    if selected and isinstance(expression.key, ast.Name):
                        return [expression]
                return []
            if isinstance(expression, ast.Call):
                if call_name(expression) == canonical.name:
                    return []
                projections = []
                if call_name(expression) == "dict":
                    projections.extend(
                        keyword
                        for keyword in expression.keywords
                        if keyword.arg in seat_fields
                    )
                for keyword in expression.keywords:
                    if keyword.arg is None:
                        projections.extend(mapping_projections(keyword.value))
                return projections
            if isinstance(expression, ast.Name):
                return fields_by_name.get(expression.id, [])
            return []

        projections = []
        for node in nodes:
            if isinstance(node, ast.Call):
                payload = status_payload(node)
                if payload is not None:
                    projections.extend(mapping_projections(payload))
                elif isinstance(node.func, ast.Attribute) and node.func.attr == "update":
                    receiver = node.func.value
                    if isinstance(receiver, ast.Name):
                        updates = [
                            keyword
                            for keyword in node.keywords
                            if keyword.arg in seat_fields
                        ]
                        updates.extend(
                            projection
                            for argument in node.args
                            for projection in mapping_projections(argument)
                        )
                        updates.extend(
                            projection
                            for keyword in node.keywords
                            if keyword.arg is None
                            for projection in mapping_projections(keyword.value)
                        )
                        fields_by_name.setdefault(receiver.id, []).extend(updates)
                continue

            if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                value_projections = mapping_projections(node.value)
                for target in targets:
                    if isinstance(target, ast.Name):
                        fields_by_name[target.id] = list(value_projections)
                    elif (
                        isinstance(target, ast.Subscript)
                        and isinstance(target.value, ast.Name)
                        and field_name(target.slice)
                    ):
                        fields_by_name.setdefault(target.value.id, []).append(target.slice)
            elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Subscript):
                target = node.target
                if isinstance(target.value, ast.Name) and field_name(target.slice):
                    fields_by_name.setdefault(target.value.id, []).append(target.slice)

        return list(dict.fromkeys(projections))

    def status_sink(function: ast.AST) -> bool:
        return any(
            isinstance(node, ast.Call) and status_payload(node) is not None
            for node in function_nodes(function)
        )

    acp = next(function for function in functions if function.name == "_run_test_acp_shape_if_requested")
    acp_assignments = [
        node
        for node in function_nodes(acp)
        if isinstance(node, ast.Assign)
        and node.value is not None
        and any(
            isinstance(target, ast.Subscript)
            and field_name(target.slice)
            for target in node.targets
        )
    ]
    acp_fields = {
        field_name(target.slice)
        for node in acp_assignments
        for target in node.targets
        if isinstance(target, ast.Subscript) and field_name(target.slice)
    }
    assert acp_fields == seat_fields
    for node in acp_assignments:
        assert (
            isinstance(node.value, ast.Call)
            and call_name(node.value) == "str"
            and len(node.value.args) == 1
            and isinstance(node.value.args[0], ast.Attribute)
            and isinstance(node.value.args[0].value, ast.Name)
            and node.value.args[0].value.id == "args"
            and node.value.args[0].attr.startswith("_worktree_")
        ), ast.dump(node.value)
    assert {
        field_name(node)
        for node in projections_in_function(acp)
        if field_name(node)
    } == seat_fields

    violations = []
    helper_writers = set()
    for function in functions:
        if not status_sink(function):
            continue
        if any(
            isinstance(node, ast.Call) and call_name(node) == canonical.name
            for node in function_nodes(function)
        ):
            helper_writers.add(function.name)
        if function is acp:
            continue  # The ACP seat values above are explicitly sourced from args.
        violations.extend((function.name, node.lineno) for node in projections_in_function(function))
    return canonical.name, seat_fields, violations, helper_writers


def test_non_acp_status_payloads_project_worktree_fields_only_through_shared_helper() -> None:
    source = Path(D.__file__).read_text(encoding="utf-8")

    # Predicate: in non-ACP write_status/status_lock.write payloads, literal seat-field
    # projections tracked through local dicts, dict(), unpacking, comprehensions,
    # update(), or subscript writes outside the helper are violations; record provenance
    # is not checked.
    # Residual: computed keys, attribute mutations, branch-dependent aliases, other
    # status sinks, and mappings supplied only by opaque arguments or helpers are not inferred.
    helper_name, seat_fields, violations, helper_writers = _status_seat_field_projection_sites(source)
    assert len(helper_writers) == 4, sorted(helper_writers)
    assert not violations, f"seat-field projection outside canonical helper: {violations}"
    test_function = next(
        node
        for node in ast.walk(ast.parse(Path(__file__).read_text(encoding="utf-8")))
        if isinstance(node, ast.FunctionDef)
        and node.name == "test_non_acp_status_payloads_project_worktree_fields_only_through_shared_helper"
    )
    def is_injected_projection_assertion(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Assert)
            and isinstance(node.test, ast.UnaryOp)
            and isinstance(node.test.op, ast.Not)
            and isinstance(node.test.operand, ast.Name)
            and node.test.operand.id == "injected_violations"
        )

    injection_loop = next(
        node for node in test_function.body if isinstance(node, ast.For)
    )
    injection_failure_line = next(
        node.lineno for node in ast.walk(injection_loop) if is_injected_projection_assertion(node)
    )
    after_helper_failure_line = next(
        nested.lineno
        for node in test_function.body
        if isinstance(node, ast.With)
        for nested in ast.walk(node)
        if is_injected_projection_assertion(nested)
    )

    fields = sorted(seat_fields)
    first_field = fields[0]
    injections = (
        f"""
def _injected_update_argument(record, base, status_fields, status_path):
    projected_fields = dict(**base, {first_field}=record.get({first_field!r}))
    status_fields.update(projected_fields)
    write_status(status_path, status_fields)
""",
        f"""
def _injected_parameter_record(record, base, status_path):
    projected_value = record.get({first_field!r})
    write_status(status_path, dict(**base, {first_field}=projected_value))
""",
    )
    for injection in injections:
        with pytest.raises(
            AssertionError,
            match="seat-field projection outside canonical helper",
        ) as failure:
            helper, fields_from_source, injected_violations, _writers = _status_seat_field_projection_sites(
                source + injection
            )
            assert not injected_violations, (
                "seat-field projection outside canonical helper: "
                f"helper={helper}, fields={sorted(fields_from_source)}, violations={injected_violations}"
            )
        frame = failure.traceback[-1]
        assert frame.name == "test_non_acp_status_payloads_project_worktree_fields_only_through_shared_helper"
        assert frame.lineno + 1 == injection_failure_line

    after_helper = f"""
def _injected_after_helper(record, status_path):
    fields = {helper_name}(record)
    fields[{first_field!r}] = record.get({first_field!r})
    write_status(status_path, fields)
"""
    with pytest.raises(
        AssertionError,
        match="seat-field projection outside canonical helper",
    ) as failure:
        helper, fields_from_source, injected_violations, _writers = _status_seat_field_projection_sites(
            source + after_helper
        )
        assert not injected_violations, (
            "seat-field projection outside canonical helper: "
            f"helper={helper}, fields={sorted(fields_from_source)}, violations={injected_violations}"
        )
    frame = failure.traceback[-1]
    assert frame.name == "test_non_acp_status_payloads_project_worktree_fields_only_through_shared_helper"
    assert frame.lineno + 1 == after_helper_failure_line

    unrelated_debug = """
def _injected_unrelated_debug(config, status_path):
    debug_fields = {"worktree_path": config.get("worktree_path")}
    write_status(status_path, {"state": "done"})
"""
    assert not _status_seat_field_projection_sites(source + unrelated_debug)[2]


def test_codex_complete_is_verbatim_through_marker_and_trailer(monkeypatch, capsys, tmp_path):
    dispatch_id = "codex-fixture-1"
    tail_text = (
        "tool\nignored tool transcript\ntokens used\n42\nFinal Codex response.\n"
        "```text\n!BLOCKED: codex-fixture-1 — quoted example\n```\n"
        "!COMPLETE: codex-fixture-1 — done\nTrailing final summary.\n"
    )
    rc, output = _invoke(monkeypatch, capsys, tmp_path, dispatch_id, tail_text)
    assert rc == 0
    assert output.out.startswith(f"{dispatch_id} !COMPLETE head - (unknown) codex fixture-account\n")
    assert "ignored tool transcript" not in output.out
    assert output.out.endswith(
        "Final Codex response.\n```text\n!BLOCKED: codex-fixture-1 — quoted example\n```\n"
        "!COMPLETE: codex-fixture-1 — done\nTrailing final summary.\n"
        "base base-sha; head - (unknown); behind: unknown (origin/main ref missing)\n"
    )


def test_codex_footer_examples_preserve_complete_response(monkeypatch, capsys, tmp_path):
    for newline in ("\n", "\r\n"):
        for example in ("```text\ntokens used\n123\n```", "    tokens used\n    123"):
            dispatch_id = "codex-footer-example"
            message = (
                "Finding before the example.\n" + example + "\nFinding after the example.\n"
                f"!COMPLETE: {dispatch_id} — done\n"
            ).replace("\n", newline)
            prefix = ("tool\nold transcript\ntokens used\n42\n").replace("\n", newline)
            prefix = "assistant" + newline + message + prefix
            rc, output = _invoke(monkeypatch, capsys, tmp_path, dispatch_id, prefix + message)
            assert rc == 0
            assert message in output.out
            assert "old transcript" not in output.out


def test_ambiguous_footer_uses_earliest_verified_transcript_copy(monkeypatch, capsys, tmp_path):
    for newline in ("\n", "\r\n"):
        for indent in ("", "    "):
            dispatch_id = "codex-duplicate-footer"
            message = (
                "Finding before example.\n" + indent + "tokens used\n" + indent + "123\n"
                "Finding after example.\n" + f"!COMPLETE: {dispatch_id} — done\n"
            ).replace("\n", newline)
            # The transcript copy need not be immediately before the footer.
            tail = "assistant" + newline + message + "tool\nfinished\ntokens used\n42\n".replace("\n", newline) + message
            rc, output = _invoke(monkeypatch, capsys, tmp_path, dispatch_id, tail)
            assert rc == 0
            assert message in output.out
            assert "boundary: unverified" not in output.out
            assert "finished" not in output.out
            assert D._cmd_final([dispatch_id, "--json"]) == 0
            result = json.loads(capsys.readouterr().out)
            assert result["message"] == message
            assert "boundary" not in result


def test_verified_footer_survives_unmatched_transcript_fence(tmp_path):
    message = b"Full finding.\ntokens used\n123\nRemaining finding.\n"
    prefix = b"tool\n```text\nassistant\n" + message + b"tool\nfinished\ntokens used\n42\n"
    tail = tmp_path / "unmatched-transcript-fence.tail"
    tail.write_bytes(prefix + message)
    assert D._final_footer_boundary(tail, 64 * 1024) == (len(prefix), None)


def test_unverifiable_footer_reports_boundary_in_text_and_json(monkeypatch, capsys, tmp_path):
    dispatch_id = "codex-unverified-footer"
    message = f"Remaining finding.\n!COMPLETE: {dispatch_id} — done\n"
    tail = "tokens used\n42\nEarlier finding.\ntokens used\n123\n" + message
    rc, output = _invoke(monkeypatch, capsys, tmp_path, dispatch_id, tail)
    expected = f"unverified (2 footer candidates); full tail: {tmp_path / (dispatch_id + '.tail')}"
    assert rc == 0
    assert f"boundary: {expected}\n" in output.out
    assert message in output.out
    assert D._cmd_final([dispatch_id, "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["boundary"] == expected
    assert result["message"] == message


def test_unmatched_tool_fence_never_suppresses_boundary_warning(monkeypatch, capsys, tmp_path):
    for newline in ("\n", "\r\n"):
        for duplicated in (False, True):
            dispatch_id = "codex-unmatched-fence"
            message = (
                "Finding before example.\n```\ntokens used\n123\n```\nFinding after example.\n"
                f"!COMPLETE: {dispatch_id} — done\n"
            ).replace("\n", newline)
            prefix = "tool\n```text\nold tool output\n".replace("\n", newline)
            if duplicated:
                prefix += "assistant" + newline + message + "tool" + newline
            prefix += "tokens used\n42\n".replace("\n", newline)
            rc, output = _invoke(monkeypatch, capsys, tmp_path, dispatch_id, prefix + message)
            assert rc == 0
            if duplicated:
                assert message in output.out
                assert "boundary: unverified" not in output.out
            else:
                assert "boundary: unverified (2 footer candidates); full tail:" in output.out
            assert D._cmd_final([dispatch_id, "--json"]) == 0
            result = json.loads(capsys.readouterr().out)
            if duplicated:
                assert result["message"] == message
                assert "boundary" not in result
            else:
                assert result["boundary"].startswith("unverified (2 footer candidates); full tail:")
                assert result["message"] == message.split(newline + "123" + newline, 1)[1]


def test_footer_outside_search_window_reports_missing_boundary(monkeypatch, capsys, tmp_path):
    dispatch_id = "codex-footer-outside-window"
    marker = f"!COMPLETE: {dispatch_id} — done\n"
    tail = "tokens used\n42\n" + "A" * (17 * 1024 * 1024) + "\n" + marker
    rc, output = _invoke(monkeypatch, capsys, tmp_path, dispatch_id, tail)
    expected = f"not found in the last 16 MB; full tail: {tmp_path / (dispatch_id + '.tail')}"
    assert rc == 0
    assert f"boundary: {expected}\n" in output.out
    assert marker in output.out
    assert len(output.out.encode()) < 64 * 1024
    assert D._cmd_final([dispatch_id, "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["boundary"] == expected
    assert result["message"] == marker


def test_sole_quoted_footer_in_partial_window_is_unverified(monkeypatch, capsys, tmp_path):
    for newline in ("\n", "\r\n"):
        dispatch_id = "codex-sole-visible-example"
        suffix = ("```\nFinding after example.\n" + f"!COMPLETE: {dispatch_id} — done\n").replace("\n", newline)
        message = "A" * (17 * 1024 * 1024) + (
            "\nFinding before example.\n```text\ntokens used\n123\n"
        ).replace("\n", newline) + suffix
        tail = "assistant" + newline + message + "tokens used\n42\n".replace("\n", newline) + message
        rc, output = _invoke(monkeypatch, capsys, tmp_path, dispatch_id, tail)
        expected = f"unverified (1 footer candidates); full tail: {tmp_path / (dispatch_id + '.tail')}"
        assert rc == 0
        assert f"boundary: {expected}\n" in output.out
        assert len(output.out.encode()) < 64 * 1024
        assert D._cmd_final([dispatch_id, "--json"]) == 0
        result = json.loads(capsys.readouterr().out)
        assert result["boundary"] == expected
        assert result["message"] == suffix


def test_sole_verified_footer_in_partial_window_needs_no_warning(monkeypatch, tmp_path):
    monkeypatch.setattr(D, "_FINAL_TAIL_CHUNK_BYTES", 128)
    message = b"Final response appears twice.\n"
    tail = tmp_path / "verified-partial-window.tail"
    tail.write_bytes(b"x" * 400 + b"\n" + message + b"tokens used\n42\n" + message)
    boundary, warning = D._final_footer_boundary(tail, 64 * 1024)
    assert boundary is not None
    assert warning is None


def test_single_footer_needs_no_duplicate(monkeypatch, capsys, tmp_path):
    dispatch_id = "codex-single-footer"
    message = f"Final finding.\n!COMPLETE: {dispatch_id} — done\n"
    rc, output = _invoke(monkeypatch, capsys, tmp_path, dispatch_id, "tokens used\n42\n" + message)
    assert rc == 0
    assert message in output.out
    assert "boundary:" not in output.out


def test_codex_marker_only_response_is_valid(monkeypatch, capsys, tmp_path):
    dispatch_id = "codex-marker-only"
    marker = f"!COMPLETE: {dispatch_id} — done\n"
    rc, output = _invoke(monkeypatch, capsys, tmp_path, dispatch_id, marker)
    assert rc == 0
    assert marker in output.out


def test_final_reconciliation_json_includes_verbatim_marker(monkeypatch, capsys, tmp_path):
    dispatch_id = "codex-resume-fixture-2"
    marker = f"!COMPLETE: {dispatch_id} — done\n"
    _invoke(
        monkeypatch,
        capsys,
        tmp_path,
        dispatch_id,
        "tokens used\n12\nResumed response.\n" + marker + "after marker\n",
        reason="marker:COMPLETE:final_reconciliation",
    )
    monkeypatch.setattr(D, "_find_dispatch_record", lambda _dispatch_id: {
        "dispatch_id": dispatch_id,
        "state": "complete",
        "terminal_state": "complete",
        "engine": "codex",
        "account": "fixture-account",
        "worktree_base": "base-sha",
        "worktree_head": "head-sha",
        "project_root": str(tmp_path),
        "worker_cwd": str(tmp_path),
        "stdout_path": str(tmp_path / f"{dispatch_id}.tail"),
        "reason": "marker:COMPLETE:final_reconciliation",
    })
    assert D._cmd_final([dispatch_id, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["marker"] == "!COMPLETE:final_reconciliation"
    assert payload["message"] == "Resumed response.\n" + marker + "after marker\n"
    assert set(payload) == {"id", "marker", "sha", "head", "source", "engine", "account", "message", "base", "behind"}


def test_grok_preserves_findings_before_result_and_all_trailing_text(monkeypatch, capsys, tmp_path):
    dispatch_id = "grok-fixture-3"
    tail_text = (
        "## Verdict: integrate\n\nVerified the follow-up.\n\n"
        "| Finding | Status |\n|---|---|\n| P1 import break | closed |\n"
        "RESULT: grok-fixture-3 — integrate\nREADY: /tmp/artifact\n"
        "!COMPLETE: grok-fixture-3 — done\npost-marker detail\n"
    )
    rc, output = _invoke(
        monkeypatch,
        capsys,
        tmp_path,
        dispatch_id,
        tail_text,
        state="complete",
        terminal_state="complete",
    )
    assert rc == 0
    assert output.out.startswith(f"{dispatch_id} !COMPLETE head - (unknown) grok fixture-account\n")
    assert output.out.endswith(tail_text + "base base-sha; head - (unknown); behind: unknown (origin/main ref missing)\n")


def test_running_codex_tool_chatter_without_boundary_is_not_reported(monkeypatch, capsys, tmp_path):
    dispatch_id = "codex-running-no-response"
    tail = tmp_path / f"{dispatch_id}.tail"
    tail.write_text("exec\n" + ("tool output\n" * 100), encoding="utf-8")
    record = {
        "dispatch_id": dispatch_id,
        "state": "running",
        "terminal_state": "unknown",
        "engine": "codex",
        "account": "fixture-account",
        "worktree_base": "base-sha",
        "worktree_head": "head-sha",
        "project_root": str(tmp_path),
        "stdout_path": str(tail),
    }
    monkeypatch.setattr(D, "_find_dispatch_record", lambda _dispatch_id: record)
    assert D._cmd_final([dispatch_id]) == 0
    output = capsys.readouterr().out
    assert output.startswith(f"{dispatch_id} running head - (unknown) codex fixture-account\n")
    assert f"running; no complete assistant message yet; tail: {tail}\n" in output
    assert "tool output" not in output


def test_running_codex_returns_bounded_assistant_before_exec(monkeypatch, capsys, tmp_path):
    dispatch_id = "codex-running-with-response"
    tail = tmp_path / f"{dispatch_id}.tail"
    tail.write_text(
        "exec\nold tool output\ntokens used\n42\n" + "A" * 9000 + "\nexec\nnew tool output\n",
        encoding="utf-8",
    )
    record = {
        "dispatch_id": dispatch_id,
        "state": "running",
        "terminal_state": "unknown",
        "engine": "codex",
        "account": "fixture-account",
        "worktree_base": "base-sha",
        "worktree_head": "head-sha",
        "project_root": str(tmp_path),
        "stdout_path": str(tail),
    }
    monkeypatch.setattr(D, "_find_dispatch_record", lambda _dispatch_id: record)
    assert D._cmd_final([dispatch_id]) == 0
    output = capsys.readouterr().out
    assert "A" * 7900 in output
    assert "new tool output" not in output
    assert "[truncated: " in output and f"full tail: {tail}]" in output
    message = output.split("\n", 1)[1].split("\nbase ", 1)[0]
    assert len(message.encode("utf-8")) <= 8192


def test_running_grok_without_terminal_turn_is_not_reported(monkeypatch, capsys, tmp_path):
    dispatch_id = "grok-running-fixture-4"
    tail = tmp_path / f"{dispatch_id}.tail"
    tail.write_text("partial response\nRESULT: still writing\n", encoding="utf-8")
    record = {
        "dispatch_id": dispatch_id,
        "state": "running",
        "terminal_state": "unknown",
        "engine": "grok",
        "account": "fixture-account",
        "worktree_base": "base-sha",
        "worktree_head": "head-sha",
        "project_root": str(tmp_path),
        "stdout_path": str(tail),
    }
    monkeypatch.setattr(D, "_find_dispatch_record", lambda _dispatch_id: record)
    assert D._cmd_final([dispatch_id]) == 0
    output = capsys.readouterr().out
    assert f"running; no complete assistant message yet; tail: {tail}" in output
    assert "partial response" not in output


def test_git_timeout_is_bounded_and_reported(monkeypatch, tmp_path):
    calls = []

    def timeout_run(args, **kwargs):
        calls.append((args, kwargs))
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    monkeypatch.setattr(D.subprocess, "run", timeout_run)
    summary = D._behind_summary({"project_root": str(tmp_path), "worktree_base": "base-sha"}, "head-sha")
    assert summary == "behind: unknown (git timeout)"
    assert len(calls) == 1
    assert calls[0][1]["timeout"] == 5


def test_missing_tail_and_unknown_id_refuse_empty_success(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(D, "_find_dispatch_record", lambda _dispatch_id: None)
    assert D._cmd_final(["missing-id", "--json"]) != 0
    assert json.loads(capsys.readouterr().out) == {"id": "missing-id", "error": "unknown dispatch id"}
    record = {"dispatch_id": "missing-tail", "stdout_path": str(tmp_path / "absent.tail")}
    monkeypatch.setattr(D, "_find_dispatch_record", lambda _dispatch_id: record)
    assert D._cmd_final(["missing-tail", "--json"]) != 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["id"] == "missing-tail" and "tail file missing" in payload["error"]


def test_final_refuses_relative_recorded_worktree_path(monkeypatch, capsys, tmp_path):
    dispatch_id = "relative-final-seat"
    tail = tmp_path / f"{dispatch_id}.tail"
    tail.write_text("worker output\n", encoding="utf-8")
    record = {
        "dispatch_id": dispatch_id,
        "state": "complete",
        "terminal_state": "complete",
        "engine": "codex",
        "project_root": str(tmp_path),
        "stdout_path": str(tail),
        "worktree_path": ".",
    }
    monkeypatch.setattr(D, "_find_dispatch_record", lambda _dispatch_id: record)
    monkeypatch.setattr(
        D,
        "_final_terminal_marker",
        lambda *_args, **_kwargs: {"kind": "COMPLETE", "line": 1, "text": "done"},
    )
    monkeypatch.setattr(D, "_final_message", lambda *_args, **_kwargs: ("done", "verified"))

    assert D._cmd_final([dispatch_id, "--json"]) != 0

    payload = json.loads(capsys.readouterr().out)
    assert "worktree_path is not absolute" in payload["error"]


def test_final_refuses_relative_worktree_path_without_project_root(
    monkeypatch, capsys, tmp_path
):
    dispatch_id = "relative-final-seat-no-root"
    tail = tmp_path / f"{dispatch_id}.tail"
    tail.write_text("worker output\n", encoding="utf-8")
    record = {
        "dispatch_id": dispatch_id,
        "state": "complete",
        "terminal_state": "complete",
        "engine": "codex",
        "stdout_path": str(tail),
        "worktree_path": "relative-seat",
    }
    monkeypatch.setattr(D, "_find_dispatch_record", lambda _dispatch_id: record)
    monkeypatch.setattr(
        D,
        "_final_terminal_marker",
        lambda *_args, **_kwargs: {"kind": "COMPLETE", "line": 1, "text": "done"},
    )
    monkeypatch.setattr(D, "_final_message", lambda *_args, **_kwargs: ("done", "verified"))

    assert D._cmd_final([dispatch_id, "--json"]) != 0

    payload = json.loads(capsys.readouterr().out)
    assert "worktree_path is not absolute" in payload["error"]


def test_final_allows_no_seat_prelaunch_failure(monkeypatch, capsys, tmp_path):
    dispatch_id = "prelaunch-final"
    tail = tmp_path / f"{dispatch_id}.tail"
    tail.write_text("worker output\n", encoding="utf-8")
    record = {
        "dispatch_id": dispatch_id,
        "state": "complete",
        "terminal_state": "complete",
        "engine": "codex",
        "project_root": str(tmp_path),
        "stdout_path": str(tail),
        "prelaunch_failure": True,
    }
    monkeypatch.setattr(D, "_find_dispatch_record", lambda _dispatch_id: record)
    monkeypatch.setattr(
        D,
        "_final_terminal_marker",
        lambda *_args, **_kwargs: {"kind": "FAILED", "line": 1, "text": "failed"},
    )
    monkeypatch.setattr(D, "_final_message", lambda *_args, **_kwargs: ("failed", "verified"))

    assert D._cmd_final([dispatch_id, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["id"] == dispatch_id


def test_final_names_missing_worktree_path_on_non_prelaunch_record(
    monkeypatch, capsys, tmp_path
):
    dispatch_id = "missing-final-seat"
    tail = tmp_path / f"{dispatch_id}.tail"
    tail.write_text("worker output\n", encoding="utf-8")
    record = {
        "dispatch_id": dispatch_id,
        "state": "complete",
        "terminal_state": "complete",
        "engine": "codex",
        "project_root": str(tmp_path),
        "stdout_path": str(tail),
    }
    monkeypatch.setattr(D, "_find_dispatch_record", lambda _dispatch_id: record)
    monkeypatch.setattr(
        D,
        "_final_terminal_marker",
        lambda *_args, **_kwargs: {"kind": "COMPLETE", "line": 1, "text": "done"},
    )
    monkeypatch.setattr(D, "_final_message", lambda *_args, **_kwargs: ("done", "verified"))

    assert D._cmd_final([dispatch_id, "--json"]) != 0

    payload = json.loads(capsys.readouterr().out)
    assert "missing worker_cwd/worktree_path" in payload["error"]
    assert "prelaunch_failure" in payload["error"]


def test_worker_dead_and_throttled_refusals_are_json_objects(monkeypatch, capsys, tmp_path):
    tail = tmp_path / "dead.tail"
    tail.write_text("tokens used\n12\n", encoding="utf-8")
    record = {
        "dispatch_id": "dead-fixture",
        "state": "worker_dead",
        "terminal_state": "worker_dead",
        "reason": "worker_dead_no_terminal_marker:death_cause=no_evidence",
        "engine": "codex",
        "stdout_path": str(tail),
        "worker_cwd": str(tmp_path),
    }
    monkeypatch.setattr(D, "_find_dispatch_record", lambda _dispatch_id: record)
    assert D._cmd_final(["dead-fixture", "--json"]) != 0
    assert json.loads(capsys.readouterr().out)["error"].startswith("worker_dead_no_terminal_marker")
    record.update(state="transient_throttle", terminal_state="transient_throttle", reason=None,
                  error={"message": "dispatch_worker_limit_reached"})
    assert D._cmd_final(["dead-fixture", "--json"]) != 0
    assert json.loads(capsys.readouterr().out)["error"] == "dispatch_worker_limit_reached"
    record.update(state="worker_dead", terminal_state="worker_dead", error=None)
    monkeypatch.setattr(D, "_final_terminal_marker", lambda *_a, **_kw: {"kind": "BLOCKED", "line": 0, "text": ""})
    assert D._cmd_final(["dead-fixture", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["marker"] == "!BLOCKED" and payload["message"] == "(no final message)"


def test_empty_terminal_body_keeps_marker_and_header(monkeypatch, capsys, tmp_path):
    dispatch_id = "codex-empty-final"
    tail = tmp_path / "empty.tail"
    tail.write_text("tokens used\n42\n", encoding="utf-8")
    record = {
        "dispatch_id": dispatch_id, "state": "blocked", "terminal_state": "blocked",
        "engine": "codex", "worktree_base": "launch-base", "worktree_branch": None,
        "project_root": str(tmp_path), "stdout_path": str(tail),
        "worker_cwd": str(tmp_path),
        "error": {"marker_kind": "BLOCKED", "reason": "marker:BLOCKED:final_reconciliation"},
    }
    monkeypatch.setattr(D, "_find_dispatch_record", lambda _dispatch_id: record)
    monkeypatch.setattr(D, "_final_terminal_marker", lambda *_a, **_kw: {"kind": "BLOCKED", "line": 0, "text": ""})
    assert D._cmd_final([dispatch_id, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["marker"] == "!BLOCKED:final_reconciliation"
    assert payload["message"] == "(no final message)"
    assert payload["base"] == "launch-base" and payload["head"] == "-"


def test_terminal_message_is_capped_and_footer_search_reads_from_end(tmp_path):
    tail = tmp_path / "large.tail"
    tail.write_bytes(b"tokens used\n1\nold response\n" + b"x" * (40 * 1024 * 1024) + b"\ntokens used\n2\n" + b"A" * (70 * 1024))
    assert D._final_footer_boundary(tail, 64 * 1024)[0] == tail.stat().st_size - 70 * 1024
    message, _boundary = D._final_message(tail, "codex", None, running=False)
    assert message is not None
    assert len(message.encode("utf-8")) <= 64 * 1024
    assert "[truncated: " in message
    assert "A" * 100 in message


def test_head_comes_from_dispatch_branch_with_timeout(monkeypatch, tmp_path):
    calls = []
    def resolve(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, "a" * 40 + "\n", "")
    monkeypatch.setattr(D.subprocess, "run", resolve)
    head, source = D._final_head({"project_root": str(tmp_path), "worktree_branch": "worktree/codex-head-fixture"}, "codex-head-fixture")
    assert head == "a" * 40
    assert source == "branch refs/heads/worktree/codex-head-fixture"
    assert calls[0][1]["timeout"] == 5


def test_resumed_head_follows_parent_branch(monkeypatch, tmp_path):
    parent = {
        "dispatch_id": "codex-root-fixture", "project_root": str(tmp_path),
        "worktree_branch": "worktree/codex-root-fixture",
    }
    monkeypatch.setattr(D, "_find_dispatch_record", lambda dispatch_id: parent if dispatch_id == "codex-root-fixture" else None)
    monkeypatch.setattr(D.subprocess, "run", lambda args, **_kw: subprocess.CompletedProcess(args, 0, "b" * 40 + "\n", ""))
    head, source = D._final_head(
        {"project_root": str(tmp_path), "parent_dispatch_id": "codex-root-fixture"},
        "codex-resume-child-fixture",
    )
    assert head == "b" * 40
    assert source == "branch refs/heads/worktree/codex-root-fixture"


def test_final_records_resumed_branch_and_admits_three_dispatch_chain(
    monkeypatch, capsys, tmp_path
):
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("GOALFLIGHT_DISPATCH_DIR", str(tmp_path / "dispatch"))
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "1")

    repo = tmp_path / "repo"
    repo.mkdir()

    def git(cwd, *args):
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Dispatch Test")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    git(repo, "add", "base.txt")
    git(repo, "commit", "-m", "base")

    root_id = "codex-root-branch-fixture"
    root_seat = D.goalflight_worktree_pool.acquire_worktree_seat(repo, root_id)
    seat = root_seat.path
    base_head = git(seat, "rev-parse", "HEAD")
    root_record = {
        "dispatch_id": root_id,
        "state": "complete",
        "terminal_state": "complete",
        "project_root": str(repo),
        "worker_cwd": str(seat),
        "worktree_path": str(seat),
        "worktree_branch": f"worktree/{root_id}",
        "worktree_base": base_head,
        "worktree_head": base_head,
        "worker_still_alive": False,
    }
    D.goalflight_ledger.write_record(root_record)
    root_seat.release()

    parent_id = root_id
    parent_branch = f"worktree/{root_id}"
    parent_head = base_head
    for index, child_id in enumerate(
        ("codex-resume-one", "codex-resume-two"), start=1
    ):
        parent_record = D.goalflight_ledger.read_record(parent_id)
        assert parent_record is not None
        D._validate_resume_worktree_source(parent_id, parent_record, seat)

        tail = tmp_path / f"{child_id}.tail"
        tail.write_text(
            f"!COMPLETE: {child_id} — finished\n", encoding="utf-8"
        )
        child_record = {
            "dispatch_id": child_id,
            "parent_dispatch_id": parent_id,
            "state": "complete",
            "terminal_state": "complete",
            "engine": "codex",
            "project_root": str(repo),
            "worker_cwd": str(seat),
            "worktree_path": str(seat),
            "worktree_branch": parent_branch,
            "worktree_base": base_head,
            "worktree_head": parent_head,
            "worker_still_alive": False,
            "stdout_path": str(tail),
        }
        D.goalflight_ledger.write_record(child_record)

        child_branch = f"worktree/{child_id}"
        git(seat, "switch", "-c", child_branch)
        (seat / f"resume-{index}.txt").write_text(
            f"resume {index}\n", encoding="utf-8"
        )
        git(seat, "add", f"resume-{index}.txt")
        git(seat, "commit", "-m", f"resume {index}")
        committed_head = git(seat, "rev-parse", "HEAD")

        assert D._cmd_final([child_id, "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["head"] == committed_head
        assert payload["source"] == f"checkout {child_branch}"
        assert payload["dirty"] is False
        assert payload["dirty_paths"] == 0

        final_record = D.goalflight_ledger.read_record(child_id)
        assert final_record is not None
        assert final_record["worktree_branch"] == child_branch
        assert final_record["worktree_head"] == committed_head
        parent_id = child_id
        parent_branch = child_branch
        parent_head = committed_head

    final_record = D.goalflight_ledger.read_record(parent_id)
    assert final_record is not None
    D._validate_resume_worktree_source(parent_id, final_record, seat)

    git(seat, "switch", "-c", "worktree/foreign-dispatch")
    (seat / "foreign.txt").write_text("foreign\n", encoding="utf-8")
    git(seat, "add", "foreign.txt")
    git(seat, "commit", "-m", "foreign branch")
    try:
        D._validate_resume_worktree_source(parent_id, final_record, seat)
    except D.DispatchUsageError as exc:
        assert "actual branch worktree/foreign-dispatch" in str(exc)
    else:
        raise AssertionError("resume accepted a seat on a foreign branch")


def test_final_after_unrelated_seat_reuse_uses_recorded_checkout(
    monkeypatch, capsys, tmp_path
):
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("GOALFLIGHT_DISPATCH_DIR", str(tmp_path / "dispatch"))
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "1")

    repo = tmp_path / "repo"
    repo.mkdir()

    def git(cwd, *args):
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Dispatch Test")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    git(repo, "add", "base.txt")
    git(repo, "commit", "-m", "base")

    old_id = "codex-old-seat-fixture"
    old_seat = D.goalflight_worktree_pool.acquire_worktree_seat(repo, old_id)
    seat = old_seat.path
    recorded_head = git(seat, "rev-parse", "HEAD")
    tail = tmp_path / "old.tail"
    tail.write_text(f"!COMPLETE: {old_id} — done\n", encoding="utf-8")
    D.goalflight_ledger.write_record(
        {
            "dispatch_id": old_id,
            "state": "complete",
            "terminal_state": "complete",
            "worker_still_alive": False,
            "worker_pid": 2147483647,
            "worker_identity": {
                "pid": 2147483647,
                "start_token": "exited-test-worker",
            },
            "project_root": str(repo),
            "worker_cwd": str(seat),
            "worktree_path": str(seat),
            "worktree_branch": f"worktree/{old_id}",
            "worktree_head": recorded_head,
            "stdout_path": str(tail),
        }
    )
    old_seat.release()

    unrelated = D.goalflight_worktree_pool.acquire_worktree_seat(
        repo, "codex-unrelated-seat-fixture"
    )
    assert unrelated.path == seat
    (seat / "unrelated.txt").write_text("unrelated worker\n", encoding="utf-8")
    git(seat, "add", "unrelated.txt")
    git(seat, "commit", "-m", "unrelated work")
    unrelated_head = git(seat, "rev-parse", "HEAD")
    assert unrelated_head != recorded_head

    git_impl = D.goalflight_worktree_pool._git
    status_reads = []

    def track_status_read(cwd, *args, **kwargs):
        if args[:1] == ("status",):
            status_reads.append(cwd)
        return git_impl(cwd, *args, **kwargs)

    monkeypatch.setattr(D.goalflight_worktree_pool, "_git", track_status_read)
    try:
        assert D._cmd_final([old_id, "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["head"] == recorded_head
        assert payload["source"] == f"ledger worktree/{old_id}"
        assert "dirty" not in payload
        assert "dirty_paths" not in payload
        assert status_reads == []
        final_record = D.goalflight_ledger.read_record(old_id)
        assert final_record is not None
        assert final_record["worktree_branch"] == f"worktree/{old_id}"
        assert final_record["worktree_head"] == recorded_head
    finally:
        unrelated.release()


def test_final_holds_seat_lock_against_claim_during_checkout_read(
    monkeypatch, capsys, tmp_path
):
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("GOALFLIGHT_DISPATCH_DIR", str(tmp_path / "dispatch"))
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "1")

    repo = tmp_path / "repo"
    repo.mkdir()

    def git(cwd, *args):
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Dispatch Test")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    git(repo, "add", "base.txt")
    git(repo, "commit", "-m", "base")

    old_id = "codex-final-lock-fixture"
    old_lease = D.goalflight_worktree_pool.acquire_worktree_seat(repo, old_id)
    seat = old_lease.path
    recorded_head = git(seat, "rev-parse", "HEAD")
    tail = tmp_path / "final-lock.tail"
    tail.write_text(f"!COMPLETE: {old_id} — done\n", encoding="utf-8")
    D.goalflight_ledger.write_record(
        {
            "dispatch_id": old_id,
            "state": "complete",
            "terminal_state": "complete",
            "worker_still_alive": False,
            "worker_pid": 2147483647,
            "worker_identity": {
                "pid": 2147483647,
                "start_token": "exited-test-worker",
            },
            "project_root": str(repo),
            "worker_cwd": str(seat),
            "worktree_path": str(seat),
            "worktree_branch": f"worktree/{old_id}",
            "worktree_head": recorded_head,
            "stdout_path": str(tail),
        }
    )
    old_lease.release()

    claimant_id = "codex-concurrent-final-claim-fixture"
    git_impl = D.goalflight_worktree_pool._git
    claim_attempted = False
    claimed_during_final = []
    refused_during_final = []
    claim_errors = []
    claim_ready = threading.Event()
    start_claim = threading.Event()
    claim_done = threading.Event()

    def contend_for_seat():
        claim_ready.set()
        if not start_claim.wait(10):
            claim_errors.append(AssertionError("final never reached its ledger update"))
            claim_done.set()
            return
        try:
            lease = D.goalflight_worktree_pool.acquire_worktree_seat(repo, claimant_id)
            claimed_during_final.append(lease)
            (lease.path / "claim.txt").write_text("claimed concurrently\n", encoding="utf-8")
            git(lease.path, "add", "claim.txt")
            git(lease.path, "commit", "-m", "concurrent claim")
        except D.goalflight_worktree_pool.WorktreeSeatUnavailable as exc:
            refused_during_final.append(exc)
        except Exception as exc:  # surfaced in the main test thread below
            claim_errors.append(exc)
        finally:
            claim_done.set()

    claim_thread = threading.Thread(target=contend_for_seat, daemon=True)
    real_state_lock = D.goalflight_ledger.StateLock
    state_lock_triggered = False

    def start_claim_before_ledger_update(*args, **kwargs):
        nonlocal state_lock_triggered
        if claim_attempted and not state_lock_triggered:
            state_lock_triggered = True
            start_claim.set()
            assert claim_done.wait(10), "nonblocking seat claim did not settle"
        return real_state_lock(*args, **kwargs)

    def claim_at_status_read(cwd, *args, **kwargs):
        nonlocal claim_attempted
        if (
            not claim_attempted
            and Path(cwd).resolve() == seat.resolve()
            and args[:1] == ("status",)
        ):
            claim_attempted = True
            claim_thread.start()
            assert claim_ready.wait(5)
        return git_impl(cwd, *args, **kwargs)

    monkeypatch.setattr(D.goalflight_worktree_pool, "_git", claim_at_status_read)
    monkeypatch.setattr(
        D.goalflight_ledger, "StateLock", start_claim_before_ledger_update
    )
    later_claim = None
    try:
        assert D._cmd_final([old_id, "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert claim_attempted
        assert state_lock_triggered
        assert claim_done.is_set()
        assert claim_errors == []
        assert payload["head"] == recorded_head
        final_record = D.goalflight_ledger.read_record(old_id)
        assert final_record is not None
        assert final_record["worktree_head"] == recorded_head
        assert final_record["worktree_branch"] == f"worktree/{old_id}"
        assert claimed_during_final == []
        assert len(refused_during_final) == 1

        later_claim = D.goalflight_worktree_pool.acquire_worktree_seat(repo, claimant_id)
        (later_claim.path / "later-claim.txt").write_text(
            "claimed later\n", encoding="utf-8"
        )
        git(later_claim.path, "add", "later-claim.txt")
        git(later_claim.path, "commit", "-m", "claim after final")
        claimant_head = git(later_claim.path, "rev-parse", "HEAD")
        assert claimant_head != recorded_head
        final_record = D.goalflight_ledger.read_record(old_id)
        assert final_record is not None
        assert final_record["worktree_head"] == recorded_head
    finally:
        start_claim.set()
        if claim_thread.ident is not None:
            claim_thread.join(timeout=10)
        if later_claim is not None:
            later_claim.release()
        for claim in claimed_during_final:
            claim.release()


def test_final_reports_dirty_and_clean_owned_checkout(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("GOALFLIGHT_DISPATCH_DIR", str(tmp_path / "dispatch"))
    monkeypatch.setenv("GOALFLIGHT_WORKTREE_SEATS", "1")

    repo = tmp_path / "repo"
    repo.mkdir()

    def git(cwd, *args):
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Dispatch Test")
    (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
    git(repo, "add", "tracked.txt")
    git(repo, "commit", "-m", "base")

    dispatch_id = "codex-dirty-final-fixture"
    lease = D.goalflight_worktree_pool.acquire_worktree_seat(repo, dispatch_id)
    seat = lease.path
    (seat / "tracked.txt").write_text("modified\n", encoding="utf-8")
    (seat / "untracked.txt").write_text("untracked\n", encoding="utf-8")
    head = git(seat, "rev-parse", "HEAD")
    tail = tmp_path / "dirty.tail"
    tail.write_text(f"!COMPLETE: {dispatch_id} — done\n", encoding="utf-8")
    D.goalflight_ledger.write_record(
        {
            "dispatch_id": dispatch_id,
            "state": "complete",
            "terminal_state": "complete",
            "worker_still_alive": False,
            "project_root": str(repo),
            "worker_cwd": str(seat),
            "worktree_path": str(seat),
            "worktree_branch": f"worktree/{dispatch_id}",
            "worktree_head": head,
            "stdout_path": str(tail),
        }
    )
    lease.release()

    assert D._cmd_final([dispatch_id, "--json"]) == 0
    dirty_payload = json.loads(capsys.readouterr().out)
    assert dirty_payload["dirty"] is True
    assert dirty_payload["dirty_paths"] == 2

    assert D._cmd_final([dispatch_id]) == 0
    assert "(dirty: 2 paths)" in capsys.readouterr().out

    git(seat, "add", "tracked.txt", "untracked.txt")
    git(seat, "commit", "-m", "clean checkout")
    assert D._cmd_final([dispatch_id, "--json"]) == 0
    clean_payload = json.loads(capsys.readouterr().out)
    assert clean_payload["dirty"] is False
    assert clean_payload["dirty_paths"] == 0


def test_resume_rejects_unrecorded_branch_named_for_lineage(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(cwd, *args):
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Dispatch Test")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    git(repo, "add", "base.txt")
    git(repo, "commit", "-m", "base")

    root_id = "codex-unowned-root-fixture"
    child_id = "codex-unowned-child-fixture"
    branch = f"worktree/{root_id}"
    git(repo, "branch", branch)
    D.goalflight_ledger.write_record(
        {
            "dispatch_id": root_id,
            "state": "complete",
            "terminal_state": "complete",
            "project_root": str(repo),
        }
    )
    child_record = {
        "dispatch_id": child_id,
        "parent_dispatch_id": root_id,
        "state": "complete",
        "terminal_state": "complete",
        "project_root": str(repo),
    }
    D.goalflight_ledger.write_record(child_record)

    try:
        D._resume_worktree_branch_spec(repo, child_id, child_record, recovery_refs=[])
    except D.goalflight_worktree_pool.WorktreeCwdRefused as exc:
        assert "no recorded worktree branch" in str(exc)
    else:
        raise AssertionError("resume accepted a branch named for an unowned lineage id")
