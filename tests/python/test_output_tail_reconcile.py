"""Regression: reconcile-from-output must find a terminal marker that is NOT the
last line (D022 false-death).

Workers legitimately emit `READY:` followed by a trailing TL;DR / summary, so the
success marker is not the final non-empty line. status._reconcile_output_tail_record
must scan the whole worker-dead tail (the watcher's reconciliation-grade scan), not
only the last line, or a completed worker is falsely reported worker_dead.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_ledger as ledger  # noqa: E402
import goalflight_status as status  # noqa: E402


def assert_eq(name: str, got: object, expected: object) -> None:
    if got != expected:
        raise AssertionError(f"{name}: got {got!r}, expected {expected!r}")


TAIL_WITH_TRAILING_TLDR = """\
worker starting
... lots of analysis ...
wrote proposal
READY: docs-private/reviews/2026-06-20-arch-review/DRY-DUPLICATION-PROPOSAL.md

TL;DR:
1. Fleet/message env resolvers first.
2. Terminal-state vocabularies diverged.
3. Cheap wins: filename sanitizer, path resolver.
"""

TAIL_NO_MARKER = """\
worker starting
... lots of analysis ...
process died mid-run with no terminal marker
"""


def _record_for(tail: Path) -> dict:
    return {
        "dispatch_id": "recon",
        "classification": "worker_dead",
        "worker_pid": 4242,
        "worker_identity": {"lstart": "Tue Jun 20 12:00:00 2026", "comm": "python3"},
        "tail_path": str(tail),
        "started_at": None,
    }


def _with_dead_worker(fn):
    saved = ledger.identity_matches
    ledger.identity_matches = lambda rec: (False, "dead")  # worker no longer live
    try:
        return fn()
    finally:
        ledger.identity_matches = saved


def _with_live_worker(fn):
    saved = ledger.identity_matches
    ledger.identity_matches = lambda rec: (True, "alive")  # identity-matched, still running
    try:
        return fn()
    finally:
        ledger.identity_matches = saved


def test_live_worker_unpromoted_tail_marker_is_not_terminal() -> None:
    # A LIVE worker (identity-matched) in a liveness-recheck state whose tail already
    # contains a terminal marker must NOT be reported terminal: the reconcile gate
    # refuses promotion (worker alive), and the unpromoted record must NOT carry the
    # marker as `terminal_marker`/`last_marker` (a terminal SIGNAL) — otherwise
    # done_code -> 0 and chunk_summary -> terminal would false-done a running worker
    # (independent-review P1, the very false-death class this change set fixes).
    import goalflight_chunk_summary as cs

    with tempfile.TemporaryDirectory() as d:
        tail = Path(d) / "live.tail"
        tail.write_text(TAIL_WITH_TRAILING_TLDR, encoding="utf-8")  # has READY: marker
        rec = _record_for(tail)
        rec["classification"] = "watcher_stopped"  # a liveness-recheck state, worker still alive
        rec["state"] = "watcher_stopped"
        out = _with_live_worker(lambda: status._reconcile_output_tail_record(rec))

        assert_eq("not promoted", out.get("output_tail_reconciliation", {}).get("promoted"), False)
        assert_eq("no top-level terminal_marker on unpromoted live record", out.get("terminal_marker"), None)
        assert_eq("no terminal_marker_source either", out.get("terminal_marker_source"), None)
        assert_eq("marker kept as diagnostic only",
                  bool(out.get("output_tail_reconciliation", {}).get("observed_marker")), True)
        assert_eq("_record_has_terminal_marker False", status._record_has_terminal_marker(out), False)
        # done_code must recheck liveness (watcher_stopped) and report LIVE, not terminal.
        assert_eq("done_code == 1 (live, not false-done)",
                  _with_live_worker(lambda: status.done_code(out)), 1)
        # chunk_summary must read this as running, never complete/wedged.
        assert_eq("normalize_state -> running",
                  cs.normalize_state(out, None, None, worker_live=True), "running")


def test_marker_followed_by_tldr_promotes_to_complete() -> None:
    with tempfile.TemporaryDirectory(prefix="gf-recon-") as d:
        tail = Path(d) / "recon.tail"
        tail.write_text(TAIL_WITH_TRAILING_TLDR, encoding="utf-8")
        out = _with_dead_worker(lambda: status._reconcile_output_tail_record(_record_for(tail)))
        assert_eq("promoted classification", out.get("classification"), "complete")
        assert_eq("marker kind", (out.get("terminal_marker") or {}).get("kind"), "READY")
        assert_eq(
            "promoted flag",
            out.get("output_tail_reconciliation", {}).get("promoted"),
            True,
        )


def test_no_marker_stays_worker_dead() -> None:
    # Negative control: a genuinely-crashed tail with NO marker must NOT be
    # promoted (guards against the anywhere-scan over-promoting).
    with tempfile.TemporaryDirectory(prefix="gf-recon-") as d:
        tail = Path(d) / "recon.tail"
        tail.write_text(TAIL_NO_MARKER, encoding="utf-8")
        out = _with_dead_worker(lambda: status._reconcile_output_tail_record(_record_for(tail)))
        assert_eq("stays worker_dead", out.get("classification"), "worker_dead")


def main() -> None:
    tests = [
        test_marker_followed_by_tldr_promotes_to_complete,
        test_no_marker_stays_worker_dead,
        test_live_worker_unpromoted_tail_marker_is_not_terminal,
    ]
    for test in tests:
        test()
    print(f"PASS tests/python/test_output_tail_reconcile.py ({len(tests)} tests)")


if __name__ == "__main__":
    main()
