"""Regression tests for `goalflight_status.py --verify-artifacts`.

A controller must verify a worker's declared outputs by OPENING the exact paths named
in its terminal SUCCESS marker — never by directory enumeration (ls/find/git status/
grep), which can return a stale view for minutes on local APFS and nearly drove a
destructive re-author (2026-06-23 APFS stale-enumeration near-miss). Pins: marker-path extraction edge cases,
success-marker gating, direct-open verification (present vs absent), by-id record
lookup (no aggregate/enumeration), and exit codes.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_status as S  # noqa: E402
import goalflight_ledger as L  # noqa: E402


def assert_eq(name: str, got: object, expected: object) -> None:
    if got != expected:
        raise AssertionError(f"{name}: got {got!r}, expected {expected!r}")


def assert_true(name: str, cond: bool) -> None:
    if not cond:
        raise AssertionError(name)


def test_extract_marker_paths() -> None:
    f = S._extract_marker_paths
    assert_eq("bare relpath", f("docs-private/research/x/findings.md"), ["docs-private/research/x/findings.md"])
    assert_eq("bare filename (no dir)", f("findings.md"), ["findings.md"])
    assert_eq("markdown link + :line stripped", f("[findings.md](/Users/x/findings.md:1)"), ["/Users/x/findings.md"])
    assert_eq("#anchor stripped", f("see docs/x.md#section"), ["docs/x.md"])
    assert_eq("URL rejected", f("https://example.com/x.md"), [])
    assert_eq("prose only", f("done, all gates green"), [])


def test_direct_open_present_and_absent() -> None:
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "leaf.md"
        f.write_text("content", encoding="utf-8")
        assert_eq("present file", S._direct_open_exists(f), (True, 7))
        assert_eq("absent file", S._direct_open_exists(Path(d) / "nope.md"), (False, 0))


def _patch_ledger(records: list[dict]):
    saved = L.read_records
    L.read_records = lambda: records  # type: ignore[assignment]
    return lambda: setattr(L, "read_records", saved)


def _record(d: str, marker_line: str, *, dispatch_id: str = "vx", state: str = "complete") -> dict:
    tail = Path(d) / "worker.tail"
    tail.write_text(f"...work...\nverify_quote CODE_VERBATIM\n{marker_line}\n", encoding="utf-8")
    return {
        "dispatch_id": dispatch_id,
        "project_root": d,
        "stdout_path": str(tail),
        "terminal_state": state,
        "state": state,
    }


def test_verify_present_via_direct_open() -> None:
    with tempfile.TemporaryDirectory() as d:
        art = Path(d) / "sub" / "findings.md"
        art.parent.mkdir(parents=True)
        art.write_text("real leaf content", encoding="utf-8")
        restore = _patch_ledger([_record(d, "READY: sub/findings.md")])
        try:
            out = S.verify_artifacts("vx", project_root=None)
            assert_true("found", out["found"])
            assert_eq("declared", out["declared_artifacts"], ["sub/findings.md"])
            assert_true("present+bytes", out["results"][0]["present"] and out["results"][0]["bytes"] > 0)
            assert_true("all_present", out["all_present"] is True)
        finally:
            restore()


def test_verify_absent_artifact() -> None:
    with tempfile.TemporaryDirectory() as d:
        restore = _patch_ledger([_record(d, "READY: sub/missing.md")])
        try:
            out = S.verify_artifacts("vx", project_root=None)
            assert_true("present false", out["results"][0]["present"] is False)
            assert_true("all_present false", out["all_present"] is False)
        finally:
            restore()


def test_failure_marker_declares_no_artifacts() -> None:
    # A FAILED: marker naming an existing path must NOT report it as a delivered artifact.
    with tempfile.TemporaryDirectory() as d:
        art = Path(d) / "partial.md"
        art.write_text("half", encoding="utf-8")
        restore = _patch_ledger([_record(d, "FAILED: partial.md", state="blocked")])
        try:
            out = S.verify_artifacts("vx", project_root=None)
            assert_eq("no declared artifacts from failure marker", out["declared_artifacts"], [])
            assert_true("all_present false (nothing claimed)", out["all_present"] is False)
        finally:
            restore()


def test_verify_uses_open_not_enumeration() -> None:
    # Poison: stub os.listdir/scandir to lie (empty) — the stale-enumeration condition.
    # verify must STILL find the file because it opens the known path by name.
    import os
    with tempfile.TemporaryDirectory() as d:
        art = Path(d) / "leaves" / "a41-2.md"
        art.parent.mkdir(parents=True)
        art.write_text("present-but-enumeration-lies", encoding="utf-8")
        saved_listdir, saved_scandir = os.listdir, os.scandir
        os.listdir = lambda *a, **k: []  # type: ignore[assignment]
        os.scandir = lambda *a, **k: iter(())  # type: ignore[assignment]
        restore = _patch_ledger([_record(d, "READY: leaves/a41-2.md")])
        try:
            out = S.verify_artifacts("vx", project_root=None)
            assert_true("open-by-name beats stale enumeration", out["all_present"] is True)
        finally:
            os.listdir, os.scandir = saved_listdir, saved_scandir
            restore()


def test_not_found_exit_2_even_json() -> None:
    restore = _patch_ledger([])  # no records
    try:
        rc_text = S.main(["--verify-artifacts", "nope", "--all-projects"])
        rc_json = S.main(["--verify-artifacts", "nope", "--all-projects", "--json"])
        assert_eq("not-found text exit 2", rc_text, 2)
        assert_eq("not-found json exit 2", rc_json, 2)
    finally:
        restore()


def main() -> None:
    tests = [
        test_extract_marker_paths,
        test_direct_open_present_and_absent,
        test_verify_present_via_direct_open,
        test_verify_absent_artifact,
        test_failure_marker_declares_no_artifacts,
        test_verify_uses_open_not_enumeration,
        test_not_found_exit_2_even_json,
    ]
    for t in tests:
        t()
    print(f"PASS tests/python/test_verify_artifacts.py ({len(tests)} tests)")


if __name__ == "__main__":
    main()
