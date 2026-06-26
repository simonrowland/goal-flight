"""Regression: the watcher bridges worker USER-NEED / USER-CONFIRM / BLOCKED
markers into the dispatch inbox, so the controller's read-side status mail hint
surfaces the question/blocker text. Must post each marker exactly once (in-memory
+ restart-safe dedup), bridge ONLY the urgent trio, and never break the watcher's
liveness loop on a messaging failure.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_watch as W  # noqa: E402
import goalflight_messages as M  # noqa: E402


def assert_eq(name: str, got: object, exp: object) -> None:
    if got != exp:
        raise AssertionError(f"{name}: got {got!r}, expected {exp!r}")


def assert_true(name: str, cond: bool) -> None:
    if not cond:
        raise AssertionError(name)


def _markers_from(text: str, tmp: str) -> list[dict]:
    tail = Path(tmp) / "w.tail"
    tail.write_text(text, encoding="utf-8")
    markers, _ = W.extract_markers(tail)
    return markers


def _inbox(dispatch_id: str) -> list[tuple]:
    envs = M.read_envelopes(M.inbox_path(M.default_messages_dir(), dispatch_id))
    return [(e.get("type"), (e.get("payload") or {}).get("text")) for e in envs]


def _with_env(fn):
    with tempfile.TemporaryDirectory() as d:
        saved = os.environ.get("GOALFLIGHT_MESSAGES_DIR")
        os.environ["GOALFLIGHT_MESSAGES_DIR"] = str(Path(d) / "messages")
        try:
            return fn(d)
        finally:
            if saved is None:
                os.environ.pop("GOALFLIGHT_MESSAGES_DIR", None)
            else:
                os.environ["GOALFLIGHT_MESSAGES_DIR"] = saved


def test_user_need_marker_posts_envelope() -> None:
    def body(d):
        markers = _markers_from("working...\nUSER-NEED: approve the schema change?\n", d)
        W.post_worker_mail("worker-7", markers, set())
        return _inbox("worker-7")

    out = _with_env(body)
    assert_eq("one envelope", len(out), 1)
    assert_eq("type user_need", out[0][0], "user_need")
    assert_true("text preserved", "approve the schema change?" in (out[0][1] or ""))


def test_dedup_in_memory_and_across_restart() -> None:
    def body(d):
        markers = _markers_from("USER-NEED: same question\n", d)
        keys: set = set()
        W.post_worker_mail("w", markers, keys)   # first post
        W.post_worker_mail("w", markers, keys)   # same set -> in-memory dedup
        W.post_worker_mail("w", markers, set())  # fresh set (watcher restart) -> lazy inbox dedup
        return _inbox("w")

    out = _with_env(body)
    assert_eq("exactly one envelope despite restart", len(out), 1)


def test_disabled_after_mail_failure_no_retry_storm() -> None:
    # The FIRST mail-layer failure disables the bridge for the run: a later DISTINCT
    # marker is not even attempted (no raise, no further disk I/O), so a broken/slow
    # inbox cannot cause a per-poll retry storm on the liveness loop.
    saved = M.post_message
    calls = {"n": 0}

    def boom(**k):
        calls["n"] += 1
        raise RuntimeError("boom")

    M.post_message = boom  # type: ignore[assignment]
    try:
        def body(d):
            keys: set = set()
            W.post_worker_mail("w", _markers_from("USER-NEED: a\n", d), keys)  # attempt 1 -> fails -> disables
            W.post_worker_mail("w", _markers_from("BLOCKED: b\n", d), keys)    # disabled -> not attempted
            return calls["n"], (W._BRIDGE_DISABLED in keys)

        n, disabled = _with_env(body)
        assert_eq("post attempted exactly once, then bridge disabled", n, 1)
        assert_true("disable sentinel parked in the dedup set", disabled)
    finally:
        M.post_message = saved  # type: ignore[assignment]


def test_only_urgent_trio_bridged() -> None:
    def body(d):
        text = "STATUS: still working\nUSER-CONFIRM: ok to push?\nBLOCKED: ssh failed\n"
        W.post_worker_mail("w", _markers_from(text, d), set())
        return sorted(t for t, _ in _inbox("w"))

    types = _with_env(body)
    assert_eq("user_confirm + blocked only (STATUS excluded)", types, ["blocked", "user_confirm"])


def test_bridge_is_best_effort_never_raises() -> None:
    # If the mail layer blows up, the bridge must swallow it (liveness comes first).
    saved = M.post_message
    M.post_message = lambda **k: (_ for _ in ()).throw(RuntimeError("boom"))  # type: ignore[assignment]
    try:
        def body(d):
            W.post_worker_mail("w", _markers_from("USER-NEED: x\n", d), set())
            return True

        assert_true("no exception escaped the bridge", _with_env(body) is True)
    finally:
        M.post_message = saved  # type: ignore[assignment]


def main() -> None:
    tests = [
        test_user_need_marker_posts_envelope,
        test_dedup_in_memory_and_across_restart,
        test_disabled_after_mail_failure_no_retry_storm,
        test_only_urgent_trio_bridged,
        test_bridge_is_best_effort_never_raises,
    ]
    for t in tests:
        t()
    print(f"PASS tests/python/test_watcher_mail_bridge.py ({len(tests)} tests)")


if __name__ == "__main__":
    main()
