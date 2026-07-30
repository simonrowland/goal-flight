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

import sys
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
            f"{agent}: worker is told not to shell out for the brief",
            "Do NOT shell out for the brief" in text,
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
    """Cursor is an ACP transport, so it stays out of that set.

    The execution contract teaches a text marker shape and belongs to the
    bash-tail agents whose terminal state is scraped from output. ACP transports
    carry terminal state in the protocol itself, which is why cursor, codex-acp
    and claude-acp are all excluded. `case_preamble_routing_matrix` in
    test_dispatch_steer.py pins that split; this test exists so a future reader
    sees the reason here too rather than assuming cursor was simply forgotten.
    """
    text = gd._worker_prompt_preamble("cursor")
    assert_true(
        "cursor does not receive the bash-tail execution contract",
        gd.WORKER_EXECUTION_PREAMBLE not in text,
    )


def main() -> None:
    test_cursor_is_not_told_to_read_the_prompt_file_env_var()
    test_non_cursor_agents_keep_the_prompt_file_instruction()
    test_cursor_is_told_not_to_retry_refusals()
    test_cursor_does_not_get_the_bash_tail_execution_contract()
    print("OK: cursor worker preamble tests pass")


if __name__ == "__main__":
    main()
