"""Regression: goalflight_status surfaces a read-side "you have mail" hint.

Controllers run goalflight_status.py constantly; the controller-mail design wants
the "you have mail" signal piggybacked onto that call (computed FRESH, fail-open,
never stored in any status JSON) so a controller learns it has open user-needs
without having to remember to poll `goalflight_messages relay`.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_status as S  # noqa: E402
import goalflight_messages as M  # noqa: E402


def assert_true(name: str, cond: bool) -> None:
    if not cond:
        raise AssertionError(name)


def assert_eq(name: str, got: object, exp: object) -> None:
    if got != exp:
        raise AssertionError(f"{name}: got {got!r}, expected {exp!r}")


def _with_mail_dirs(fn):
    with tempfile.TemporaryDirectory() as d:
        msgs = Path(d) / "messages"
        fleet = Path(d) / "fleet"
        msgs.mkdir()
        fleet.mkdir()
        saved = {k: os.environ.get(k) for k in ("GOALFLIGHT_MESSAGES_DIR", "GOALFLIGHT_FLEET_DIR")}
        os.environ["GOALFLIGHT_MESSAGES_DIR"] = str(msgs)
        os.environ["GOALFLIGHT_FLEET_DIR"] = str(fleet)
        try:
            return fn(msgs, fleet)
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


def _bare_payload(mail: dict) -> dict:
    return {
        "scope": {"project_root": "/tmp/x"},
        "capacity": {"operating_cap": 8},
        "dispatch": {"records": []},
        "capacity_state": {"leases": {}, "cooldowns": {}},
        "rate_pressure": {},
        "warnings": [],
        "mail": mail,
    }


def test_no_mail_empty_summary() -> None:
    out = _with_mail_dirs(lambda m, f: S._mail_summary())
    assert_eq("empty inbox -> {}", out, {})


def test_open_user_need_surfaces_hint() -> None:
    def body(msgs, fleet):
        M.post_message(
            dispatch_id="worker-7",
            msg_type="user_need",
            payload={"text": "need a decision on X"},
            messages_dir=msgs,
        )
        return S._mail_summary()

    out = _with_mail_dirs(body)
    assert_eq("count", out.get("open_user_needs"), 1)
    assert_true("dispatch id captured", "worker-7" in (out.get("dispatch_ids") or []))
    assert_true("hint names the relay command", "goalflight_messages.py relay" in (out.get("hint") or ""))
    assert_true("hint carries the mail glyph", "\U0001f4ec" in (out.get("hint") or ""))


def test_mail_check_is_fail_open() -> None:
    # If the mail layer raises, a status call must NOT break -> summary is {}.
    saved = M.build_aggregate
    M.build_aggregate = lambda **k: (_ for _ in ()).throw(RuntimeError("boom"))  # type: ignore[assignment]
    try:
        assert_eq("raise -> {}", S._mail_summary(), {})
    finally:
        M.build_aggregate = saved  # type: ignore[assignment]


def test_render_text_includes_hint_when_present() -> None:
    lines = S.render_text(_bare_payload({"hint": "\U0001f4ec mail: 2 open user-need(s) from [a, b] - run: goalflight_messages.py relay"}), 20)
    assert_true("hint line present", any("mail:" in ln for ln in lines))


def test_render_text_silent_when_no_mail() -> None:
    lines = S.render_text(_bare_payload({}), 20)
    assert_true("no mail line when inbox empty", not any("mail:" in ln for ln in lines))


def test_non_regular_inbox_file_does_not_hang() -> None:
    # A FIFO/device named *.jsonl in the inbox dir must be SKIPPED, not opened:
    # read_text()'s open() would block forever on a FIFO and hang status before
    # the fail-open guard could fire (independent-review P2). Bound with SIGALRM so
    # a regression FAILS loudly instead of hanging the whole suite.
    import signal

    if not hasattr(signal, "SIGALRM"):
        return  # platform without alarm (e.g. Windows) — skip

    def body(msgs, fleet):
        M.post_message(dispatch_id="real", msg_type="user_need", payload={"text": "q"}, messages_dir=msgs)
        os.mkfifo(msgs / "trap.jsonl")  # would block open() if not skipped

        def _boom(signum, frame):
            raise TimeoutError("mail check hung on a non-regular inbox file")

        old = signal.signal(signal.SIGALRM, _boom)
        signal.alarm(5)
        try:
            return S._mail_summary()
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old)

    out = _with_mail_dirs(body)
    assert_eq("real need still surfaced, FIFO skipped without hang", out.get("open_user_needs"), 1)


def main() -> None:
    tests = [
        test_no_mail_empty_summary,
        test_open_user_need_surfaces_hint,
        test_mail_check_is_fail_open,
        test_render_text_includes_hint_when_present,
        test_render_text_silent_when_no_mail,
        test_non_regular_inbox_file_does_not_hang,
    ]
    for t in tests:
        t()
    print(f"PASS tests/python/test_status_mail_hint.py ({len(tests)} tests)")


if __name__ == "__main__":
    main()
