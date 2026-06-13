#!/usr/bin/env python3
"""Hermetic tests for fleet tool-smoke canaries."""

from __future__ import annotations

from support import skip_posix_on_native_windows

skip_posix_on_native_windows("fleet tool-smoke fixtures use POSIX /tmp paths")

import json
import io
import re
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_fleet as fleet
import goalflight_fleet_billing as billing
import goalflight_fleet_dispatch as fleet_dispatch
import goalflight_fleet_tool_smoke as tool_smoke

BASE_SHA = "0123456789abcdef0123456789abcdef01234567"


def assert_true(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)


def green_runner(_argv: list[str]) -> tuple[int, str, str]:
    return 0, "logged_in: true\n", ""


def _read_tool_calls(absolute_path: str = "/tmp/goal-flight/worktree/README.md") -> list[dict]:
    return [
        {
            "title": "Read",
            "status": "completed",
            "locations": [{"path": "VERSION"}],
        },
        {
            "title": "Read",
            "status": "completed",
            "locations": [{"path": absolute_path}],
        },
    ]


def _fixture_fleet(fleet_dir: Path, state_dir: Path) -> None:
    fleet.bootstrap(fleet_dir)
    fleet_doc = fleet.read_json(fleet_dir / "fleet.json")
    fleet_doc["nodes"] = {
        "localhost": {
            "node_id": "localhost",
            "status": "active",
            "ssh": {"alias": "localhost", "hostname": "localhost"},
            "repo_root": str(ROOT),
            "state_dir": str(state_dir),
            "billing_accounts": [],
            "added_at": "2026-06-12T12:00:00+00:00",
        }
    }
    fleet._atomic_write_json(fleet_dir / "fleet.json", fleet_doc)
    billing.link_account_to_node(
        fleet_dir,
        "openai/default",
        "localhost",
        runner=green_runner,
    )


def _extract_wrapped_flag(argv: list[str], flag: str, default: str = "") -> str:
    joined = " ".join(argv)
    match = re.search(rf"{re.escape(flag)}\s+'?([^'\s]+)'?", joined)
    if match:
        return match.group(1)
    for idx, part in enumerate(argv):
        if part == flag and idx + 1 < len(argv):
            return argv[idx + 1]
    return default


def _runner_with_acp_status(*, red_read_error: bool = False, include_tool_calls: bool = True):
    def _runner(argv: list[str]) -> tuple[int, str, str]:
        joined = " ".join(argv)
        if "goalflight_acp_run.py" not in joined:
            if re.search(r"\bcat\s+'?[^'\s]+status\.json'?", joined):
                match = re.search(r"\bcat\s+'?([^'\s]+status\.json)'?", joined)
                if match:
                    path = Path(match.group(1))
                    if path.exists():
                        return 0, path.read_text(), ""
            return 0, "{}", ""
        status_path = Path(_extract_wrapped_flag(argv, "--status-json"))
        cwd = _extract_wrapped_flag(argv, "--cwd", "/tmp/goal-flight/worktree")
        status_path.parent.mkdir(parents=True, exist_ok=True)
        stderr_path = status_path.with_suffix(".stderr.log")
        result_text = (
            "TOOL-SMOKE-READY\n"
            "RELATIVE_OK: 1.0.0\n"
            "ABSOLUTE_OK: # Goal Flight\n"
        )
        payload = {
            "schema": "goalflight.acp-run.v1",
            "dispatch_id": _extract_wrapped_flag(argv, "--dispatch-id", "tool-smoke"),
            "agent": "grok-acp",
            "state": "complete",
            "ok": True,
            "result_text": result_text,
            "text_excerpt": result_text,
            "status_path": str(status_path),
            "agent_stderr_path": str(stderr_path),
        }
        if include_tool_calls:
            payload["tool_calls"] = _read_tool_calls(f"{cwd.rstrip('/')}/README.md")
        stderr = ""
        if red_read_error:
            stderr = (
                'ERROR tool_error: tool_output_error tool_name="Read" '
                'effective_tool_name="Read" model_id="grok-composer-2.5-fast"\n'
            )
            payload["consecutive_tool_errors"] = 1
            payload["repeated_tool_error_tool"] = "Read"
            payload["last_tool_error"] = stderr.strip()
        status_path.write_text(json.dumps(payload, sort_keys=True) + "\n")
        return 0, json.dumps(payload, sort_keys=True), stderr

    return _runner


def _green_record() -> dict:
    identity = tool_smoke.build_identity(
        node_id="localhost",
        agent="grok-acp",
        base_sha=BASE_SHA,
        sandbox="read-only",
        model_version=tool_smoke.resolve_model_version("grok-acp"),
    )
    return tool_smoke.build_result_record(
        identity=identity,
        ttl_s=31_536_000,
        exit_code=0,
        status_payload={
            "state": "complete",
            "result_text": (
                "TOOL-SMOKE-READY\n"
                "RELATIVE_OK: 1.0.0\n"
                "ABSOLUTE_OK: # Goal Flight\n"
            ),
            "tool_calls": _read_tool_calls(),
        },
        updated_at="2026-06-12T12:00:00+00:00",
        expected_absolute_path="/tmp/goal-flight/worktree/README.md",
    )


def test_run_tool_smoke_green_writes_cache() -> None:
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        state_dir = Path(td) / "state"
        _fixture_fleet(fleet_dir, state_dir)
        record = tool_smoke.run_tool_smoke_canary(
            fleet_dir,
            node_id="localhost",
            agent="grok-acp",
            base_sha=BASE_SHA,
            sandbox="read-only",
            runner=_runner_with_acp_status(),
            iso_now="2026-06-12T12:00:00+00:00",
        )
        assert_true("green", record["status"] == "green")
        assert_true("relative read", record["read_relative_ok"] is True)
        assert_true("absolute read", record["read_absolute_ok"] is True)
        assert_true("no read error", record["read_tool_error_seen"] is False)
        assert_true("status path", str(state_dir) in str(record["status_path"]))
        cached = tool_smoke.read_tool_smoke_artifact(fleet_dir, record["identity"])
        assert_true("cached green", cached is not None and cached["status"] == "green")


def test_tool_smoke_requires_native_read_evidence() -> None:
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        state_dir = Path(td) / "state"
        _fixture_fleet(fleet_dir, state_dir)
        record = tool_smoke.run_tool_smoke_canary(
            fleet_dir,
            node_id="localhost",
            agent="grok-acp",
            base_sha=BASE_SHA,
            sandbox="read-only",
            runner=_runner_with_acp_status(include_tool_calls=False),
            iso_now="2026-06-12T12:00:00+00:00",
        )
        assert_true("label-only red", record["status"] == "red")
        assert_true("relative label present", record["read_relative_ok"] is True)
        assert_true("absolute label present", record["read_absolute_ok"] is True)
        assert_true("relative tool missing", record["read_relative_tool_ok"] is False)
        assert_true("absolute tool missing", record["read_absolute_tool_ok"] is False)
        assert_true("diagnosis names native Read", "native Read evidence" in record["diagnosis"])


def test_run_tool_smoke_red_on_read_tool_error_even_with_final_text() -> None:
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        state_dir = Path(td) / "state"
        _fixture_fleet(fleet_dir, state_dir)
        record = tool_smoke.run_tool_smoke_canary(
            fleet_dir,
            node_id="localhost",
            agent="grok-acp",
            base_sha=BASE_SHA,
            sandbox="read-only",
            runner=_runner_with_acp_status(red_read_error=True),
            iso_now="2026-06-12T12:00:00+00:00",
        )
        assert_true("red", record["status"] == "red")
        assert_true("final text was usable", record["read_relative_ok"] is True)
        assert_true("read error detected", record["read_tool_error_seen"] is True)
        assert_true("model captured", record["model_version"] == "grok-composer-2.5-fast")
        assert_true("teaching diagnosis", "do not commit a goal-loop" in record["diagnosis"])


def test_tool_smoke_stale_cache_blocks_goal_gate() -> None:
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        state_dir = Path(td) / "state"
        _fixture_fleet(fleet_dir, state_dir)
        record = _green_record()
        record["ttl_s"] = 1
        record["expires_at"] = "2026-06-12T12:00:01+00:00"
        tool_smoke.write_tool_smoke_artifact(fleet_dir, record)
        try:
            tool_smoke.assert_green_canary(
                fleet_dir,
                node_id="localhost",
                agent="grok-acp",
                base_sha=BASE_SHA,
                sandbox="read-only",
                now=tool_smoke.parse_iso("2026-06-12T12:00:02+00:00"),
            )
        except tool_smoke.ToolSmokeGateError as exc:
            assert_true("stale", exc.cache_state == "stale")
            return
        assert_true("expected stale gate", False)


def test_tool_smoke_corrupt_expiry_blocks_goal_gate() -> None:
    for case in ("missing", "invalid"):
        with tempfile.TemporaryDirectory() as td:
            fleet_dir = Path(td) / "fleet"
            state_dir = Path(td) / "state"
            _fixture_fleet(fleet_dir, state_dir)
            record = _green_record()
            if case == "missing":
                record.pop("expires_at", None)
            else:
                record["expires_at"] = "not-a-date"
            tool_smoke.write_tool_smoke_artifact(fleet_dir, record)
            try:
                tool_smoke.assert_green_canary(
                    fleet_dir,
                    node_id="localhost",
                    agent="grok-acp",
                    base_sha=BASE_SHA,
                    sandbox="read-only",
                    now=tool_smoke.parse_iso("2026-06-12T12:00:00+00:00"),
                )
            except tool_smoke.ToolSmokeGateError as exc:
                assert_true(f"{case} expiry stale", exc.cache_state == "stale")
                continue
            assert_true(f"expected stale gate for {case} expiry", False)


def test_dispatch_gate_requires_green_canary_for_goal_mode() -> None:
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        state_dir = Path(td) / "state"
        _fixture_fleet(fleet_dir, state_dir)
        try:
            fleet_dispatch.assert_dispatch_gates(
                fleet_dir,
                node_id="localhost",
                billing_account="openai/default",
                agent="grok-acp",
                base_sha=BASE_SHA,
                dispatch_mode="goal",
            )
        except fleet_dispatch.DispatchGateError as exc:
            assert_true("tool smoke missing", exc.code == "tool_smoke")
        else:
            assert_true("expected missing tool smoke gate", False)

        red = _green_record()
        red["status"] = "red"
        red["ok"] = False
        red["diagnosis"] = "worker grok-acp on node localhost failed a tool-smoke"
        tool_smoke.write_tool_smoke_artifact(fleet_dir, red)
        try:
            fleet_dispatch.assert_dispatch_gates(
                fleet_dir,
                node_id="localhost",
                billing_account="openai/default",
                agent="grok-acp",
                base_sha=BASE_SHA,
                dispatch_mode="goal",
            )
        except fleet_dispatch.DispatchGateError as exc:
            assert_true("tool smoke red", exc.code == "tool_smoke")
        else:
            assert_true("expected red tool smoke gate", False)

        fleet_dispatch.assert_dispatch_gates(
            fleet_dir,
            node_id="localhost",
            billing_account="openai/default",
            agent="grok-acp",
            base_sha=BASE_SHA,
            dispatch_mode="one-shot",
        )

        green = _green_record()
        tool_smoke.write_tool_smoke_artifact(fleet_dir, green)
        fleet_dispatch.assert_dispatch_gates(
            fleet_dir,
            node_id="localhost",
            billing_account="openai/default",
            agent="grok-acp",
            base_sha=BASE_SHA,
            dispatch_mode="goal",
        )


def test_dispatch_gate_uses_model_keyed_canary() -> None:
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        state_dir = Path(td) / "state"
        _fixture_fleet(fleet_dir, state_dir)
        model_version = tool_smoke.resolve_model_version("grok-acp")
        identity = tool_smoke.build_identity(
            node_id="localhost",
            agent="grok-acp",
            base_sha=BASE_SHA,
            sandbox="read-only",
            model_version=model_version,
        )
        record = tool_smoke.build_result_record(
            identity=identity,
            ttl_s=31_536_000,
            exit_code=0,
            stderr='model_id="grok-composer-2.5-fast"\n',
            status_payload={
                "state": "complete",
                "result_text": (
                    "TOOL-SMOKE-READY\n"
                    "RELATIVE_OK: 1.0.0\n"
                    "ABSOLUTE_OK: # Goal Flight\n"
                ),
                "tool_calls": _read_tool_calls(),
            },
            updated_at="2026-06-12T12:00:00+00:00",
            expected_absolute_path="/tmp/goal-flight/worktree/README.md",
        )
        assert_true("model captured as metadata", record["model_version"] == "grok-composer-2.5-fast")
        assert_true("identity is model-keyed", record["identity"].get("model_version") == model_version)
        tool_smoke.write_tool_smoke_artifact(fleet_dir, record)
        assert_true("exact model-keyed read", tool_smoke.read_tool_smoke_artifact(fleet_dir, identity) is not None)

        fleet_dispatch.assert_dispatch_gates(
            fleet_dir,
            node_id="localhost",
            billing_account="openai/default",
            agent="grok-acp",
            base_sha=BASE_SHA,
            dispatch_mode="goal",
        )
        args = type(
            "Args",
            (),
            {
                "fleet_dir": fleet_dir,
                "node": "localhost",
                "agent": "grok-acp",
                "base_sha": BASE_SHA,
                "sandbox": "read-only",
                "model_version": None,
            },
        )()
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = tool_smoke.cmd_tool_smoke_status(args)
        payload = json.loads(stdout.getvalue())
        assert_true("status green", code == 0 and payload["cache_state"] == "green")


def test_tool_smoke_rejects_discovered_model_without_identity_key() -> None:
    identity = tool_smoke.build_identity(
        node_id="localhost",
        agent="unknown-acp",
        base_sha=BASE_SHA,
        sandbox="read-only",
    )
    record = tool_smoke.build_result_record(
        identity=identity,
        ttl_s=31_536_000,
        exit_code=0,
        stderr='model_id="unknown-model-v2"\n',
        status_payload={
            "state": "complete",
            "result_text": (
                "TOOL-SMOKE-READY\n"
                "RELATIVE_OK: 1.0.0\n"
                "ABSOLUTE_OK: # Goal Flight\n"
            ),
            "tool_calls": _read_tool_calls(),
        },
        updated_at="2026-06-12T12:00:00+00:00",
        expected_absolute_path="/tmp/goal-flight/worktree/README.md",
    )
    assert_true("unkeyed model red", record["status"] == "red")
    assert_true("diagnosis names model key", "keyed model version" in record["diagnosis"])


def main() -> None:
    test_run_tool_smoke_green_writes_cache()
    test_tool_smoke_requires_native_read_evidence()
    test_run_tool_smoke_red_on_read_tool_error_even_with_final_text()
    test_tool_smoke_stale_cache_blocks_goal_gate()
    test_tool_smoke_corrupt_expiry_blocks_goal_gate()
    test_dispatch_gate_requires_green_canary_for_goal_mode()
    test_dispatch_gate_uses_model_keyed_canary()
    test_tool_smoke_rejects_discovered_model_without_identity_key()
    print("OK: fleet tool-smoke tests pass")


if __name__ == "__main__":
    main()
