#!/usr/bin/env python3
"""Hermetic tests for goalflight_agent_traits.py — temp files only."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_agent_traits as at  # noqa: E402


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def case_absent_install_becomes_current() -> None:
    with tempfile.TemporaryDirectory() as td:
        target = Path(td) / "nested" / "CLAUDE.md"
        _assert(at.status(target) == "absent", "starts absent")
        result = at.install(target)
        _assert(result["action"] == "appended", result["action"])
        _assert(result["backup"] is None, "no backup when file did not exist")
        _assert(at.status(target) == "current", "current after install")
        text = target.read_text(encoding="utf-8")
        _assert(at.END_MARKER in text, "END marker present")
        _assert(text.count(at.END_MARKER) == 1, "exactly one block")


def case_install_twice_is_noop() -> None:
    with tempfile.TemporaryDirectory() as td:
        target = Path(td) / "CLAUDE.md"
        first = at.install(target)
        _assert(first["backup"] is None, first)
        second = at.install(target)
        _assert(second["action"] == "noop", second)
        text = target.read_text(encoding="utf-8")
        _assert(text.count(at.END_MARKER) == 1, "still one block")


def case_stale_block_updates_in_place() -> None:
    with tempfile.TemporaryDirectory() as td:
        target = Path(td) / "CLAUDE.md"
        old_body = "# old section\nstale text\n"
        target.write_text(
            f"prefix\n<!-- >>> goal-flight agent traits (v0) >>> -->\n{old_body}{at.END_MARKER}\nsuffix\n",
            encoding="utf-8",
        )
        _assert(at.status(target) == "stale", "seed is stale")
        result = at.install(target)
        _assert(result["action"] == "updated", result["action"])
        _assert(result["backup"] is not None, "backup on update")
        _assert(Path(result["backup"]).is_file(), "backup exists")
        text = target.read_text(encoding="utf-8")
        _assert(text.count(at.END_MARKER) == 1, "one block after update")
        _assert("prefix" in text and "suffix" in text, "surrounding content preserved")
        _assert(old_body.strip() not in text, "old inner body replaced")
        _assert(at.status(target) == "current", "current after update")


def case_status_and_canonical_text() -> None:
    raw = (ROOT / "configs" / "agent-traits.md").read_text(encoding="utf-8")
    if raw.lstrip().startswith("<!--"):
        end = raw.find("-->")
        expected_body = raw[end + 3 :].lstrip("\n")
    else:
        expected_body = raw
    _assert(at.canonical_text() == expected_body, "canonical_text matches configs/agent-traits.md body")
    with tempfile.TemporaryDirectory() as td:
        missing = Path(td) / "missing.md"
        _assert(at.status(missing) == "absent", "missing file is absent")
        target = Path(td) / "ok.md"
        at.install(target)
        _assert(at.status(target) == "current", "installed is current")
        target.write_text(
            target.read_text(encoding="utf-8").replace(
                at.canonical_text().splitlines()[0],
                "# mutated heading",
                1,
            ),
            encoding="utf-8",
        )
        _assert(at.status(target) == "stale", "drifted inner is stale")


def case_creates_parent_dir() -> None:
    with tempfile.TemporaryDirectory() as td:
        target = Path(td) / "deep" / "dir" / "CLAUDE.md"
        at.install(target)
        _assert(target.is_file(), "file created")
        _assert(at.status(target) == "current", "current")


def case_cli_json() -> None:
    with tempfile.TemporaryDirectory() as td:
        target = Path(td) / "CLAUDE.md"
        import subprocess

        proc = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "goalflight_agent_traits.py"),
                "--install",
                "--target",
                str(target),
                "--json",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        payload = json.loads(proc.stdout)
        _assert(payload["action"] == "appended", payload)


def main() -> None:
    case_absent_install_becomes_current()
    case_install_twice_is_noop()
    case_stale_block_updates_in_place()
    case_status_and_canonical_text()
    case_creates_parent_dir()
    case_cli_json()
    print("PASS  test_agent_traits")


if __name__ == "__main__":
    main()