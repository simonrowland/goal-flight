#!/usr/bin/env python3
"""Small dispatch ergonomics regression tests."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import goalflight_dispatch as D  # noqa: E402

_FAILS: list[str] = []


def check(name: str, cond: bool) -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        _FAILS.append(name)


def _args(**overrides):
    base = {
        "agent": "codex",
        "read_only": False,
        "prompt": "COMPLETE: no-op",
        "prompt_file": None,
        "max_idle_secs": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_default_idle_windows() -> None:
    args = _args(agent="codex")
    D._apply_max_idle_default(args)
    check("codex write default idle is 600s", args.max_idle_secs == 600.0)

    args = _args(agent="grok-code")
    D._apply_max_idle_default(args)
    check("grok-code write default idle is 600s", args.max_idle_secs == 600.0)

    args = _args(agent="codex", read_only=True)
    D._apply_max_idle_default(args)
    check("read-only keeps quick idle default", args.max_idle_secs == 180.0)

    args = _args(agent="grok-research")
    D._apply_max_idle_default(args)
    check("research keeps quick idle default", args.max_idle_secs == 180.0)

    args = _args(agent="codex", max_idle_secs=42.0)
    D._apply_max_idle_default(args)
    check("explicit idle value is preserved", args.max_idle_secs == 42.0)


def test_read_only_review_artifact_guard() -> None:
    with tempfile.TemporaryDirectory() as td:
        prompt = Path(td) / "review.md"
        prompt.write_text(
            "Run review. Write the findings to docs-private/reviews/x/codex-review.final.md.\n",
            encoding="utf-8",
        )
        args = _args(read_only=True, prompt=None, prompt_file=str(prompt))
        try:
            D._guard_read_only_write_prompt(args)
        except D.DispatchUsageError as exc:
            text = str(exc)
            check("read-only write prompt is refused", "cannot write review files" in text)
            check("guard points to inline return", "return findings inline" in text)
            check("guard points to writable sandbox", "writable sandbox/worktree" in text)
        else:
            check("read-only write prompt is refused", False)

    args = _args(
        read_only=True,
        prompt=(
            "Review the staged diff. Return verdict INLINE in chat, "
            "do not create any file."
        ),
    )
    try:
        D._guard_read_only_write_prompt(args)
    except D.DispatchUsageError as exc:
        check(f"inline read-only review prompt is allowed ({exc})", False)
    else:
        check("inline read-only review prompt is allowed", True)

    args = _args(
        read_only=True,
        prompt="Review the staged diff. Write your review to docs-private/reviews/x/review.md.",
    )
    try:
        D._guard_read_only_write_prompt(args)
    except D.DispatchUsageError as exc:
        check("read-only write review path prompt is refused", "cannot write review files" in str(exc))
    else:
        check("read-only write review path prompt is refused", False)

    args = _args(
        read_only=True,
        prompt=(
            "Review the staged diff. Write your review to "
            "docs-private/reviews/x/review.md and return inline in the final response."
        ),
    )
    try:
        D._guard_read_only_write_prompt(args)
    except D.DispatchUsageError as exc:
        check("mixed write path plus inline prompt is refused", "cannot write review files" in str(exc))
    else:
        check("mixed write path plus inline prompt is refused", False)


def test_grok_code_research_intent_guard() -> None:
    """Web-research prompts on grok-code bounce with a teaching hint; coding
    prompts that merely mention the web, suppressed prompts, the override
    flag, and other agents all pass (precision-first — B5c lesson)."""
    def reason(**kw):
        return D._research_intent_reason(_args(**kw))

    research = "Research refractory coatings. Search the web for HfC data and cite source URLs."
    # triggers on grok-code
    check("web-search prompt triggers", reason(agent="grok-code", prompt=research) is not None)
    check("deep-research triggers", reason(agent="grok-code", prompt="Run a deep-research sweep on UHTC oxidation.") is not None)
    check("web-fetch triggers", reason(agent="grok-code", prompt="web_fetch the vendor page and summarize.") is not None)
    # guard raises with the teaching message + override pointer
    try:
        D._guard_grok_code_research_prompt(_args(agent="grok-code", prompt=research))
    except D.DispatchUsageError as exc:
        check("guard names grok-research", "--agent grok-research" in str(exc))
        check("guard names override", "--web-research-ok" in str(exc))
    else:
        check("research prompt on grok-code is refused", False)
    # precision: must NOT trigger (round-1 review FP corpus)
    check("bare URL does not trigger", reason(agent="grok-code", prompt="Fix the bug per https://github.com/x/y/issues/12 in this repo.") is None)
    check("'research the codebase' does not trigger", reason(agent="grok-code", prompt="Research the codebase and refactor the parser.") is None)
    check("scraper coding task does not trigger", reason(agent="grok-code", prompt="Implement the scraper module's retry logic per the spec in docs/.") is None)
    check("web-search FEATURE does not trigger", reason(agent="grok-code", prompt="Add web search to the app settings page; wire the websearch module.") is None)
    check("web_fetch as symbol does not trigger", reason(agent="grok-code", prompt="Refactor web_fetch() error handling and add tests for fetch retries.") is None)
    check("literature-review doc does not trigger", reason(agent="grok-code", prompt="Move the literature review section into docs/ and fix its table.") is None)
    check("internet-facing does not trigger", reason(agent="grok-code", prompt="Harden the internet-facing routes; audit the auth middleware.") is None)
    check("meta-review prompt suppressed", reason(agent="grok-code", prompt="INLINE-RETURN review (read-only; no file writes): review the new web-search guard; it bounces prompts that say search the web.") is None)
    check("--read-only exempt", reason(agent="grok-code", prompt="Search the web for HfC data.", read_only=True) is None)
    # suppressors win
    check("scoped offline suppressor wins", reason(agent="grok-code", prompt="Search the web examples are in fixtures; run fully offline.") is None)
    check("no-web suppressor wins", reason(agent="grok-code", prompt="Do not use the web; search the web phrasing appears only in the quoted bug report.") is None)
    check("incidental offline does NOT suppress", reason(agent="grok-code", prompt="Search the web for offline-first sync patterns and cite source URLs.") is not None)
    # FN adds
    check("look-up-online triggers", reason(agent="grok-code", prompt="Look up the HfB2 emissivity values online.") is not None)
    check("fetch-url-from triggers", reason(agent="grok-code", prompt="Fetch the page from the vendor site and summarize the errata.") is not None)
    # scoping + override
    check("grok-research is exempt", reason(agent="grok-research", prompt=research) is None)
    check("codex is exempt", reason(agent="codex", prompt=research) is None)
    check("--web-research-ok overrides", reason(agent="grok-code", prompt=research, web_research_ok=True) is None)


def test_reused_nonterminal_dispatch_id_guard() -> None:
    orig_find = D._find_dispatch_record
    try:
        D._find_dispatch_record = lambda dispatch_id: {
            "dispatch_id": dispatch_id,
            "state": "running",
            "worker_pid": None,
            "status_path": "/tmp/dup.status.json",
        }
        try:
            D._refuse_reused_nonterminal_dispatch_id("dup")
        except D.DispatchUsageError as exc:
            text = str(exc)
            check("active duplicate id is refused", "already has a non-terminal ledger record" in text)
            check("duplicate id message points to unique ids", "unique --dispatch-id" in text)
        else:
            check("active duplicate id is refused", False)

        D._find_dispatch_record = lambda dispatch_id: {
            "dispatch_id": dispatch_id,
            "state": "complete",
        }
        try:
            D._refuse_reused_nonterminal_dispatch_id("done")
        except D.DispatchUsageError as exc:
            check(f"terminal duplicate id is reusable ({exc})", False)
        else:
            check("terminal duplicate id is reusable", True)
    finally:
        D._find_dispatch_record = orig_find


def test_dispatch_end_hint() -> None:
    hint = D._dispatch_end_reattach_hint(
        "quiet-worker",
        terminal_state="idle_timeout",
        worker_alive=True,
    )
    check("idle-timeout live worker gets reattach hint",
          hint == "worker still alive - re-attach via goalflight_status.py --done quiet-worker")
    hint = D._dispatch_end_reattach_hint(
        "marker-worker",
        terminal_state="watcher_stopped",
        worker_alive=True,
    )
    check("watcher-stopped live worker gets reattach hint",
          hint == "worker still alive - re-attach via goalflight_status.py --done marker-worker")
    check("dead idle-timeout gets no hint",
          D._dispatch_end_reattach_hint("dead", terminal_state="idle_timeout", worker_alive=False) is None)


def main() -> int:
    test_default_idle_windows()
    test_read_only_review_artifact_guard()
    test_grok_code_research_intent_guard()
    test_reused_nonterminal_dispatch_id_guard()
    test_dispatch_end_hint()
    if _FAILS:
        print(f"\n{len(_FAILS)} FAILED: {_FAILS}")
        return 1
    print("\nall dispatch ergonomics tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
