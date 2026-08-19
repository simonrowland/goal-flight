#!/usr/bin/env python3
"""Decision ids mint as d-NNN; legacy q-* rows keep working."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TASK = ROOT / "goalflight_task.py"
NODE = shutil.which("node")


def assert_true(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)


def run_task(project: Path, *args: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["GOALFLIGHT_TASK_STORE_DIR"] = str(project / ".goal-flight-state")
    return subprocess.run(
        [sys.executable, str(TASK), "--project-root", str(project), *args],
        cwd=str(project),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )


def write_legacy_store(project: Path, items: list[dict]) -> None:
    docs = project / "docs-private"
    docs.mkdir(parents=True)
    (docs / "tasks.jsonl").write_text(
        "".join(json.dumps(item, separators=(",", ":")) + "\n" for item in items),
        encoding="utf-8",
    )


def _legacy_decision(item_id: str, title: str) -> dict:
    return {
        "schema_version": 1,
        "id": item_id,
        "kind": "decision",
        "title": title,
        "blocked_by": [],
        "links": [],
        "done": False,
    }


def test_legacy_q_rows_round_trip_with_new_d_minting() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td)
        write_legacy_store(
            project,
            [
                _legacy_decision("q-106", "Locked-looking pending decision"),
                _legacy_decision("q-107", "Second legacy decision"),
                _legacy_decision("q-108", "Third legacy decision"),
            ],
        )

        shown = run_task(project, "show", "q-106", "--json")
        assert_true(f"show legacy q-106: {shown.stderr}", shown.returncode == 0)
        payload = json.loads(shown.stdout)
        assert_true("show keeps legacy q id", payload["id"] == "q-106")
        assert_true("show keeps decision kind", payload["kind"] == "decision")

        appended = run_task(project, "append", "q-106", "still the same row", "--json")
        assert_true(f"append legacy q-106: {appended.stderr}", appended.returncode == 0)
        assert_true("append names legacy id", json.loads(appended.stdout)["items"] == ["q-106"])

        minted = run_task(project, "new", "Fresh pending decision", "--kind", "decision")
        assert_true(f"new decision: {minted.stderr}", minted.returncode == 0)
        new_id = minted.stdout.strip()
        assert_true(f"new decision mints d- prefix, got {new_id!r}", new_id == "d-001")

        shown_new = run_task(project, "show", "d-001", "--json")
        assert_true(f"show minted d-001: {shown_new.stderr}", shown_new.returncode == 0)
        new_payload = json.loads(shown_new.stdout)
        assert_true("minted row is a decision", new_payload["kind"] == "decision")
        assert_true("minted title stored", new_payload["title"] == "Fresh pending decision")

        appended_new = run_task(project, "append", "d-001", "new row accepts notes", "--json")
        assert_true(f"append minted d-001: {appended_new.stderr}", appended_new.returncode == 0)
        assert_true("append names minted id", json.loads(appended_new.stdout)["items"] == ["d-001"])

        shown_old = run_task(project, "show", "q-108", "--json")
        assert_true(f"show leftover q-108: {shown_old.stderr}", shown_old.returncode == 0)
        assert_true("legacy q-108 survives mint", json.loads(shown_old.stdout)["id"] == "q-108")

        listed = run_task(project, "list", "--kind", "decision", "--json")
        assert_true(f"list decisions: {listed.stderr}", listed.returncode == 0)
        ids = {item["id"] for item in json.loads(listed.stdout)}
        assert_true("list keeps legacy q ids and new d id", ids == {"q-106", "q-107", "q-108", "d-001"})

        second = run_task(project, "new", "Another pending decision", "--kind", "decision")
        assert_true(f"second new decision: {second.stderr}", second.returncode == 0)
        assert_true("d family increments independently of q", second.stdout.strip() == "d-002")


def main() -> None:
    if not NODE:
        print("SKIP: test_decision_id_namespace.py: node not found on PATH")
        return
    test_legacy_q_rows_round_trip_with_new_d_minting()
    print("OK: decision id namespace tests pass")


if __name__ == "__main__":
    main()
