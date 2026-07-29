#!/usr/bin/env python3
"""Marker sigil acceptance and bare sign-off position discipline.

Regression cover for a live false terminal: `--wait` reported a running worker
as COMPLETE because the bare sign-off pattern matched `done` -- the loop
terminator of a shell script the worker had echoed into its own tail. A
controller acting on that verdict gates, commits and pushes unfinished work.

Two rules are pinned here:

1. A marker may carry the `!` sigil (`!COMPLETE: ...`). The bare form stays
   accepted, because deployed skill installs still emit it and a sigil-only
   scanner would convert those workers into false-DEATH -- strictly worse than
   the false-COMPLETE the sigil prevents.
2. `extract_markers` stays PERMISSIVE about the bare sign-off. Filtering it here
   by position was tried and reverted: it breaks D022 (a terminal marker
   followed by a trailing TL;DR must still resolve), and the sign-off cannot be
   separated lexically anyway -- a bare lowercase `complete` is a legitimate
   sign-off while a bare lowercase `done` is shell syntax.

The `done` false-terminal is defended one layer up, where a marker becomes a
verdict: see tests/python/test_wait_marker_vs_liveness.py.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_watch  # noqa: E402

ACP_MARKER_KINDS = (
    "STATUS",
    "RESULT",
    "USER-NEED",
    "USER-CONFIRM",
    "BLOCKED",
    "FAILED",
    "COMPLETE",
    "READY",
    "PERMISSION-OK-PROCEEDED",
)


def assert_true(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)


def _kinds(tmp: Path, name: str, tail: str) -> list[str]:
    path = tmp / f"{name}.tail"
    path.write_text(tail, encoding="utf-8")
    markers, _size = goalflight_watch.extract_markers(path)
    return [marker["kind"] for marker in markers]


def test_extract_markers_stays_permissive_about_bare_signoff() -> None:
    """Pin the deliberate choice, so nobody 'fixes' it and breaks D022.

    A mid-stream `done` DOES still produce a marker here. That is intentional:
    this list feeds the mail bridge and status, while the terminal decision for a
    live worker runs through _last_line_is_terminal_marker and for a dead worker
    through final reconciliation -- which must honour a sign-off followed by a
    trailing summary. Filtering by position here breaks that case, which is why
    the attempt was reverted. The protection against the false terminal lives in
    goalflight_status.done_code (see test_wait_marker_vs_liveness.py).
    """
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        kinds = _kinds(
            tmp,
            "loop",
            "codex\nRunning the load loop now.\n"
            "for i in {1..10}; do\n  python3 tests/python/test_x.py\ndone\n"
            "still working on the fold...\n",
        )
        assert_true(
            "mid-stream shell `done` is still collected (verdict layer defends)",
            kinds == ["COMPLETE"],
        )


def test_trailing_signoff_still_terminal() -> None:
    """Do not regress the case the sign-off heuristic exists for."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        assert_true(
            "trailing `Done.` is still a terminal COMPLETE",
            _kinds(tmp, "signoff", "did the work\nDone.\n") == ["COMPLETE"],
        )
        assert_true(
            "trailing sign-off tolerates trailing blank lines",
            _kinds(tmp, "signoff_blank", "did the work\nDone.\n\n\n") == ["COMPLETE"],
        )


def test_sigil_markers_are_accepted() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        assert_true(
            "sigil COMPLETE accepted",
            _kinds(tmp, "sigil", "!COMPLETE: hardened the thing\n") == ["COMPLETE"],
        )
        assert_true(
            "sigil RESULT then COMPLETE accepted in order",
            _kinds(tmp, "sigil_pair", "!RESULT: n=4\n!COMPLETE: all green\n")
            == ["RESULT", "COMPLETE"],
        )
        assert_true(
            "sigil BLOCKED accepted",
            _kinds(tmp, "sigil_blocked", "!BLOCKED: sandbox refused write\n")
            == ["BLOCKED"],
        )


def test_legacy_bare_kind_markers_still_accepted() -> None:
    """Dual-accept: deployed installs emit the un-sigiled form."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        assert_true(
            "legacy COMPLETE still accepted",
            _kinds(tmp, "legacy", "COMPLETE: legacy form\n") == ["COMPLETE"],
        )


def test_sigil_does_not_bypass_fence_skip() -> None:
    """The sigil must not become a new way to inject terminals from examples."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        kinds = _kinds(
            tmp,
            "fenced",
            "```\n!COMPLETE: example in the docs\n```\nstill working\n",
        )
        assert_true("sigil marker inside a fence is ignored", kinds == [])


def test_acp_sigil_vocabulary_matches_legacy() -> None:
    """Every ACP marker kind must dual-accept the same payload."""
    import acp_runner  # noqa: PLC0415

    failures: list[str] = []
    for kind in ACP_MARKER_KINDS:
        value = f"payload-for-{kind.lower()}"
        expected = {kind: [value]}
        bare = acp_runner.extract_markers(f"{kind}: {value}\n")
        sigiled = acp_runner.extract_markers(f"!{kind}: {value}\n")
        if bare != expected or sigiled != bare:
            failures.append(
                f"{kind}: expected={expected!r} bare={bare!r} sigiled={sigiled!r}"
            )
    assert_true(
        "ACP prefixed and unprefixed marker extraction agree for every kind: "
        + "; ".join(failures),
        not failures,
    )


def test_sigil_keeps_injection_suppressions() -> None:
    """Fence, prompt-echo, vocabulary-example, and position rules still win."""
    import acp_runner  # noqa: PLC0415
    from goalflight_acp_client import _has_actionable_user_confirm_marker  # noqa: PLC0415

    failures: list[str] = []
    if acp_runner.extract_markers("```text\n!COMPLETE: fenced ACP example\n```\n"):
        failures.append("ACP fence skip accepted a prefixed example")
    if not _has_actionable_user_confirm_marker("!USER-CONFIRM: approve edit? [Y/N]\n"):
        failures.append("ACP permission guard rejected a prefixed confirmation")
    if _has_actionable_user_confirm_marker(
        "```text\n!USER-CONFIRM: fenced example only\n```\n"
    ):
        failures.append("ACP permission guard accepted a fenced prefixed example")

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        prompt_echo = tmp / "prompt-echo.tail"
        prompt_echo.write_text(
            "tail began after the prompt anchor\n!COMPLETE: prompt-only\n",
            encoding="utf-8",
        )
        prompt_lines = ["Do the work", "!COMPLETE: prompt-only"]
        if goalflight_watch._final_terminal_marker(
            prompt_echo,
            ignore_prefix_lines=prompt_lines,
        ) is not None:
            failures.append("prefixed prompt echo reconciled as terminal")

        vocabulary_bullet = tmp / "vocabulary-bullet.tail"
        vocabulary_bullet.write_text("- `!COMPLETE:`\n", encoding="utf-8")
        if goalflight_watch._final_terminal_marker(vocabulary_bullet) is not None:
            failures.append("prefixed marker-vocabulary bullet reconciled as terminal")

        early = tmp / "early.tail"
        early.write_text(
            "!COMPLETE: too early\nordinary output continues\n",
            encoding="utf-8",
        )
        if goalflight_watch._last_line_is_terminal_marker(early) is not None:
            failures.append("prefixed non-final marker bypassed last-line discipline")

    assert_true(
        "prefixed markers preserve injection suppressions: " + "; ".join(failures),
        not failures,
    )


def test_real_marker_after_a_loop_still_wakes_controller() -> None:
    """A genuine sigil marker after a loop must still be the resolving one.

    The loop's `done` also lands in the list (see the permissiveness test), so
    assert on the FINAL marker and its payload -- an empty-text sign-off and a
    real `!COMPLETE: <summary>` are both kind COMPLETE, and only the payload
    distinguishes them.
    """
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        path = tmp / "loop_then_marker.tail"
        path.write_text(
            "for x in a b; do\n echo hi\ndone\n!COMPLETE: finished after loop\n",
            encoding="utf-8",
        )
        markers, _size = goalflight_watch.extract_markers(path)
        assert_true("final marker is the real terminal", markers[-1]["kind"] == "COMPLETE")
        assert_true(
            "final marker carries the worker's payload, not an empty sign-off",
            markers[-1]["text"] == "finished after loop",
        )


def test_nothing_teaches_the_sigil_until_every_parser_agrees() -> None:
    """Teaching a marker syntax one parser rejects is a safety regression.

    This fired for real: the prompt template was changed to instruct
    `!USER-CONFIRM: ...` while acp_runner.extract_markers remained sigil-blind.
    On the ACP path extraction returned {}, so the confirmation guard never
    engaged and the following edit reached auto-allow with no human approval --
    the taught syntax disabled the mechanism it shipped beside.

    Accepting the sigil is harmless; instructing it is not, until the grammar is
    shared. This test is self-limiting: once the ACP path understands the sigil,
    the unification has landed and teaching becomes safe, so the check retires
    itself rather than blocking the migration it exists to sequence.
    """
    sys.path.insert(0, str(ROOT / "scripts"))
    import acp_runner  # noqa: PLC0415

    if acp_runner.extract_markers("!COMPLETE: unified"):
        return  # ACP understands the sigil: grammar unified, teaching is safe.

    offenders: list[str] = []
    for rel in ("templates", "protocols"):
        base = ROOT / rel
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not path.is_file() or path.suffix not in {".md", ".tpl"}:
                continue
            try:
                body = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for kind in ("COMPLETE", "USER-CONFIRM", "USER-NEED", "BLOCKED", "RESULT", "STATUS"):
                if f"!{kind}:" in body:
                    offenders.append(f"{path.relative_to(ROOT)} teaches !{kind}:")

    assert_true(
        "no template/protocol instructs the sigil while ACP rejects it: "
        + "; ".join(sorted(set(offenders))),
        not offenders,
    )


def main() -> None:
    test_nothing_teaches_the_sigil_until_every_parser_agrees()
    test_extract_markers_stays_permissive_about_bare_signoff()
    test_trailing_signoff_still_terminal()
    test_sigil_markers_are_accepted()
    test_legacy_bare_kind_markers_still_accepted()
    test_sigil_does_not_bypass_fence_skip()
    test_acp_sigil_vocabulary_matches_legacy()
    test_sigil_keeps_injection_suppressions()
    test_real_marker_after_a_loop_still_wakes_controller()
    print("OK: marker sigil and sign-off position tests pass")


if __name__ == "__main__":
    main()
