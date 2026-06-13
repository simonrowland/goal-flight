#!/usr/bin/env python3
"""Hermetic tests for goalflight_reap_shims.py CLI."""

from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_reap_shims as reap_cli  # noqa: E402

COUNT_PAYLOAD = {
    "orphan_count": 2,
    "reapable_count": 1,
    "count_includes_foreign_shims": True,
    "orphans": [
        {"pid": 101, "ppid": 1, "age_s": 1200.0, "comm": "claude-code-cli-acp", "goalflight_owned": True},
        {"pid": 104, "ppid": 1, "age_s": 30.0, "comm": "claude-code-cli-acp", "goalflight_owned": False},
    ],
}

REAP_PAYLOAD = {
    "reaped": [{"pid": 101, "age_s": 1200.0, "pgid": 101, "action": "SIGTERM+SIGKILL", "comm": "claude-code-cli-acp"}],
    "candidates": [{"pid": 101, "age_s": 1200.0, "comm": "claude-code-cli-acp"}],
    "skipped": None,
}


def assert_true(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)


def test_default_dry_run_calls_count_only() -> None:
    count_calls: list[int] = []
    reap_calls: list[int] = []

    def fake_count(**_kwargs):
        count_calls.append(1)
        return dict(COUNT_PAYLOAD)

    def fake_reap(**_kwargs):
        reap_calls.append(1)
        return dict(REAP_PAYLOAD)

    with patch.object(reap_cli, "count_orphaned_acp_shims", fake_count), patch.object(
        reap_cli, "reap_orphaned_acp_shims", fake_reap
    ):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = reap_cli.main([])
    assert_true("exit 0", code == 0)
    assert_true("count called once", len(count_calls) == 1)
    assert_true("reap never called", len(reap_calls) == 0)
    out = buf.getvalue()
    assert_true("human summary orphan_count", "orphan_count=2" in out)
    assert_true("human summary reapable_count", "reapable_count=1" in out)
    assert_true("per-orphan pid", "pid=101" in out)
    assert_true("per-orphan age", "age_s=1200.0" in out)
    assert_true("per-orphan comm", "comm=claude-code-cli-acp" in out)
    assert_true("exec hint", "--exec" in out)


def test_exec_calls_reap() -> None:
    count_calls: list[int] = []
    reap_calls: list[int] = []

    def fake_count(**_kwargs):
        count_calls.append(1)
        return dict(COUNT_PAYLOAD)

    def fake_reap(**_kwargs):
        reap_calls.append(1)
        return dict(REAP_PAYLOAD)

    with patch.object(reap_cli, "count_orphaned_acp_shims", fake_count), patch.object(
        reap_cli, "reap_orphaned_acp_shims", fake_reap
    ):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = reap_cli.main(["--exec"])
    assert_true("exit 0", code == 0)
    assert_true("count not called", len(count_calls) == 0)
    assert_true("reap called once", len(reap_calls) == 1)
    out = buf.getvalue()
    assert_true("reaped pid", "pid=101" in out)
    assert_true("reaped action", "action=SIGTERM+SIGKILL" in out)


def test_json_dry_run_shape() -> None:
    with patch.object(reap_cli, "count_orphaned_acp_shims", return_value=dict(COUNT_PAYLOAD)):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = reap_cli.main(["--json"])
    assert_true("exit 0", code == 0)
    payload = json.loads(buf.getvalue())
    assert_true("schema", payload.get("schema") == reap_cli.SCHEMA)
    assert_true("mode dry-run", payload.get("mode") == "dry-run")
    assert_true("orphan_count", payload.get("orphan_count") == 2)
    assert_true("reapable_count", payload.get("reapable_count") == 1)
    assert_true("orphans list", isinstance(payload.get("orphans"), list) and len(payload["orphans"]) == 2)


def test_json_exec_shape() -> None:
    with patch.object(reap_cli, "reap_orphaned_acp_shims", return_value=dict(REAP_PAYLOAD)):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = reap_cli.main(["--exec", "--json"])
    assert_true("exit 0", code == 0)
    payload = json.loads(buf.getvalue())
    assert_true("schema", payload.get("schema") == reap_cli.SCHEMA)
    assert_true("mode exec", payload.get("mode") == "exec")
    assert_true("reaped list", isinstance(payload.get("reaped"), list) and len(payload["reaped"]) == 1)
    assert_true("reaped pid", payload["reaped"][0].get("pid") == 101)
    assert_true("reaped action", payload["reaped"][0].get("action") == "SIGTERM+SIGKILL")


def main() -> None:
    tests = (
        test_default_dry_run_calls_count_only,
        test_exec_calls_reap,
        test_json_dry_run_shape,
        test_json_exec_shape,
    )
    for test in tests:
        test()
        print(f"PASS {test.__name__}")


if __name__ == "__main__":
    main()