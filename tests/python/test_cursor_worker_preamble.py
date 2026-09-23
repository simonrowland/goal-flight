#!/usr/bin/env python3
"""Cursor workers must not be told to shell-read their prompt file.

Observed twice, live: a cursor-transport worker obeyed the generic
`PROMPT_FILE_PREAMBLE` ("your FULL original brief is at $GOALFLIGHT_PROMPT_FILE"),
cursor's automatic command reviewer escalated the read, nobody was watching to
approve it in an unattended dispatch, and the worker retried until it hit the ACP
event cap and was killed with nothing written.

The worker was following instructions. The instruction was unfollowable on that
transport. These tests pin the per-transport split so it cannot regress.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_dispatch as gd  # noqa: E402


def assert_true(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)


def test_cursor_is_not_told_to_read_the_prompt_file_env_var() -> None:
    for agent in ("cursor", "cursor-agent"):
        text = gd._worker_prompt_preamble(agent)
        assert_true(
            f"{agent}: generic prompt-file preamble is not used",
            gd.PROMPT_FILE_PREAMBLE not in text,
        )
        assert_true(
            f"{agent}: worker is told the brief is inline",
            "delivered inline" in text,
        )
        assert_true(
            f"{agent}: worker is told not to shell out for its own brief",
            "Do not run shell commands to locate" in text,
        )
        # Naming the variable in order to forbid it planted it: workers read it
        # anyway. The cursor preamble must not mention it at all -- there is
        # nothing to look up, so nothing should suggest there is.
        assert_true(
            f"{agent}: prompt-file env var is not named at all",
            "GOALFLIGHT_PROMPT_FILE" not in text,
        )


def test_non_cursor_agents_keep_the_prompt_file_instruction() -> None:
    """The re-read-from-disk rule is load-bearing everywhere else.

    It is what keeps a long goal-loop worker anchored to its authoritative brief
    after an internal compaction. Only cursor loses it, and only because it
    cannot follow it.
    """
    for agent in ("codex", "grok-code", "kimi", None):
        text = gd._worker_prompt_preamble(agent)
        assert_true(
            f"{agent}: still told to re-read $GOALFLIGHT_PROMPT_FILE",
            gd.PROMPT_FILE_PREAMBLE in text,
        )
        assert_true(
            f"{agent}: does not receive cursor-only tooling guidance",
            gd.CURSOR_TOOLING_PREAMBLE not in text,
        )


def test_cursor_is_told_not_to_retry_refusals() -> None:
    text = gd._worker_prompt_preamble("cursor")
    assert_true(
        "cursor worker is told never to retry a rejected command",
        "NEVER retry a rejected command" in text,
    )
    assert_true(
        "cursor worker is given the BLOCKED escape",
        "!BLOCKED:" in text,
    )


def test_cursor_does_not_get_the_bash_tail_execution_contract() -> None:
    """Cursor gets the dispatch-id identity contract, and only that.

    Cursor runs on the text path, so the watcher scrapes its markers from the
    tail exactly as it does for codex. Codex completes with the identity
    contract alone; the longer execution preamble belongs to the grok/moonshot
    set. Cursor gets what codex gets and nothing more (see
    `test_cursor_prompt_carries_the_dispatch_id_marker_contract`).
    `case_preamble_routing_matrix` in test_dispatch_steer.py pins the split.
    """
    text = gd._worker_prompt_preamble("cursor")
    assert_true(
        "cursor does not receive the bash-tail execution contract",
        gd.WORKER_EXECUTION_PREAMBLE not in text,
    )


def _assembled_prompt(agent: str, dispatch_id: str) -> str:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        body = tmp / "body.md"
        body.write_text("Fix the parser and commit.\n", encoding="utf-8")
        # Materializing hardens the dispatch dir; keep that off the host's.
        prior = os.environ.get("GOALFLIGHT_DISPATCH_DIR")
        os.environ["GOALFLIGHT_DISPATCH_DIR"] = str(tmp / "dispatch")
        try:
            assembled = gd._materialize_steer_prompt(
                str(body), tmp / "dispatch", dispatch_id, agent=agent
            )
        finally:
            if prior is None:
                os.environ.pop("GOALFLIGHT_DISPATCH_DIR", None)
            else:
                os.environ["GOALFLIGHT_DISPATCH_DIR"] = prior
        return Path(assembled).read_text(encoding="utf-8")


def test_cursor_prompt_carries_the_dispatch_id_marker_contract() -> None:
    """A cursor worker must be told to put its dispatch id on its markers.

    The watcher binds COMPLETE/READY/RESULT to the dispatch id and ignores a
    success marker without it. Cursor was never told the id, and a live
    cursor worker signed off `COMPLETE: b-5856 landed on ...` (its task id,
    not its dispatch id). The run succeeded and was recorded `worker_dead`.
    """
    for agent in ("cursor", "cursor-agent"):
        dispatch_id = "cursor-53688-1790108089"
        text = _assembled_prompt(agent, dispatch_id)
        assert_true(
            f"{agent}: prompt names the exact dispatch id for markers",
            "Every terminal marker payload starts with the exact dispatch id "
            f"`{dispatch_id}`." in text,
        )
        assert_true(
            f"{agent}: prompt carries the id-prefixed success shape",
            f"`!COMPLETE: {dispatch_id} — <summary>`" in text,
        )
        # Same contract as codex, nothing more.
        assert_true(
            f"{agent}: no bash-tail execution preamble",
            gd.WORKER_EXECUTION_PREAMBLE not in text,
        )
    codex_text = _assembled_prompt("codex", "cursor-53688-1790108089")
    cursor_text = _assembled_prompt("cursor", "cursor-53688-1790108089")
    contract_start = "\n\nTerminal evidence identity contract:\n"
    assert_true(
        "cursor's contract block is byte-identical to codex's",
        cursor_text[cursor_text.index(contract_start):]
        == codex_text[codex_text.index(contract_start):],
    )


def test_genuine_acp_agents_still_skip_the_text_marker_contract() -> None:
    """Control: codex-acp and claude-acp stay excluded; only cursor moved."""
    for agent in ("codex-acp", "claude-acp"):
        text = _assembled_prompt(agent, "acp-case-1")
        assert_true(
            f"{agent}: no text identity contract",
            "Terminal evidence identity contract" not in text,
        )


def main() -> None:
    test_cursor_is_not_told_to_read_the_prompt_file_env_var()
    test_non_cursor_agents_keep_the_prompt_file_instruction()
    test_cursor_is_told_not_to_retry_refusals()
    test_cursor_does_not_get_the_bash_tail_execution_contract()
    test_cursor_prompt_carries_the_dispatch_id_marker_contract()
    test_genuine_acp_agents_still_skip_the_text_marker_contract()
    print("OK: cursor worker preamble tests pass")


if __name__ == "__main__":
    main()
