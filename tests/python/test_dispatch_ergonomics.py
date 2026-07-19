#!/usr/bin/env python3
"""Small dispatch ergonomics regression tests."""

from __future__ import annotations

import sys
import contextlib
import io
import os
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import goalflight_dispatch as D  # noqa: E402
import goalflight_acp_run as ACP  # noqa: E402

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
        "cwd": None,
        "ignore_git_warn": False,
        "model": None,
        "max_idle_secs": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _run_git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
    )
    return proc.stdout.strip()


def _make_git_repo(root: Path) -> str:
    _run_git(root, "init")
    _run_git(root, "config", "user.email", "goalflight@example.invalid")
    _run_git(root, "config", "user.name", "Goal Flight Test")
    (root / "README.md").write_text("fixture\n", encoding="utf-8")
    _run_git(root, "add", "README.md")
    _run_git(root, "commit", "-m", "fixture")
    return _run_git(root, "rev-parse", "HEAD")


def test_default_idle_windows() -> None:
    args = _args(agent="codex")
    D._apply_max_idle_default(args)
    check("codex write default idle is 600s", args.max_idle_secs == 600.0)

    args = _args(agent="grok-code")
    D._apply_max_idle_default(args)
    check("grok-code write default idle is 600s", args.max_idle_secs == 600.0)

    args = _args(agent="kimi")
    D._apply_max_idle_default(args)
    check("kimi write default idle is 600s", args.max_idle_secs == 600.0)

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
        check(f"mixed write path plus inline prompt is allowed ({exc})", False)
    else:
        check("mixed write path plus inline prompt is allowed", True)

    args = _args(
        read_only=True,
        prompt=(
            "Review the staged diff. The sandbox has no write access. "
            "Output: return everything inline in your final response "
            "(the sandbox is read-only; inline is the expected delivery). "
            "Findings as `P0|P1|P2|P3 - <file:line> - <claim> - <fix>`."
        ),
    )
    try:
        D._guard_read_only_write_prompt(args)
    except D.DispatchUsageError as exc:
        check(f"collocated write-access findings prompt with inline contract is allowed ({exc})", False)
    else:
        check("collocated write-access findings prompt with inline contract is allowed", True)


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


def test_git_pin_warning() -> None:
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td) / "repo"
        repo.mkdir()
        head = _make_git_repo(repo)
        short_head = head[:7]

        no_pin = D._git_pin_warning(_args(cwd=str(repo), prompt="Implement the queued change."))
        check("git no-pin warns", no_pin is not None and "prompt carries no git base pin" in no_pin)
        check("git no-pin warning carries HEAD", no_pin is not None and short_head in no_pin)
        check("git no-pin warning names opt-out", no_pin is not None and "--ignore-git-warn" in no_pin)

        matching = D._git_pin_warning(_args(cwd=str(repo), prompt=f"Verify HEAD is {short_head} before edits."))
        check("matching short HEAD pin is silent", matching is None)

        full_matching = D._git_pin_warning(_args(cwd=str(repo), prompt=f"Verify HEAD is {head} before edits."))
        check("matching full HEAD pin is silent", full_matching is None)

        mismatch_pin = "deadbee" if not head.startswith("deadbee") else "cafebab"
        mismatch = D._git_pin_warning(_args(cwd=str(repo), prompt=f"Verify HEAD is {mismatch_pin} before edits."))
        check("mismatching pin warns", mismatch is not None and "GIT BASE PIN MISMATCH" in mismatch)
        check("mismatching pin names prompt pin", mismatch is not None and mismatch_pin in mismatch)
        check("mismatching pin names cwd HEAD", mismatch is not None and short_head in mismatch)

        mixed = D._git_pin_warning(
            _args(cwd=str(repo), prompt=f"Verify HEAD is {short_head} and compare {mismatch_pin} before edits")
        )
        check("mixed fresh and stale pins warn", mixed is not None and "GIT BASE PIN MISMATCH" in mixed)
        check("mixed warning names stale pin", mixed is not None and mismatch_pin in mixed)

        ignored_no_pin = D._git_pin_warning(_args(cwd=str(repo), prompt="Implement.", ignore_git_warn=True))
        ignored_mismatch = D._git_pin_warning(
            _args(cwd=str(repo), prompt=f"Verify HEAD is {mismatch_pin}.", ignore_git_warn=True)
        )
        check("--ignore-git-warn silences no-pin", ignored_no_pin is None)
        check("--ignore-git-warn silences mismatch", ignored_mismatch is None)

        ordinary = D._git_pin_warning(
            _args(cwd=str(repo), prompt="ordinary words, abc, and buildabc1234defartifact are not pins")
        )
        check("heuristic non-cases do not become mismatch", ordinary is not None and "MISMATCH" not in ordinary)
        check("ordinary words are not SHA pins", D._extract_git_base_pins("ordinary words only") == [])
        check("short hex is not a SHA pin", D._extract_git_base_pins("abc") == [])
        check("hex inside longer identifier is not a SHA pin", D._extract_git_base_pins("buildabc1234defartifact") == [])
        check("deadline is not a SHA pin", D._extract_git_base_pins("deadline") == [])
        check("slash-adjacent hex path is not a SHA pin", D._extract_git_base_pins("research/deadbeef") == [])
        check("dot-adjacent hex path is not a SHA pin", D._extract_git_base_pins("fixture.deadbeef01") == [])
        check("colon-adjacent hex host token is not a SHA pin", D._extract_git_base_pins("host:deadbeef") == [])
        check("bare quoted SHA pin matches", D._extract_git_base_pins("'deadbee'") == ["deadbee"])
        check("spaced SHA pin matches", D._extract_git_base_pins("verify HEAD is deadbee before edits") == ["deadbee"])

        tail = Path(td) / "dispatch.tail"
        with contextlib.redirect_stderr(io.StringIO()):
            D._emit_dispatch_warnings([no_pin], tail_path=tail, reset_tail=True)
        check("warnings are written to dispatch tail", short_head in tail.read_text(encoding="utf-8"))

    with tempfile.TemporaryDirectory() as td:
        non_git = D._git_pin_warning(_args(cwd=td, prompt="Implement without a pin."))
        check("non-git cwd is silent", non_git is None)


def test_grok_model_passthrough_warning() -> None:
    warning = D._grok_model_passthrough_warning(
        _args(agent="grok-code", model="grok-composer-2.5-fast")
    )
    check("grok-code --model note", warning is not None and "your explicit --model is honored" in warning)
    warning = D._grok_model_passthrough_warning(
        _args(agent="grok-research", model="grok-build")
    )
    check("grok-research --model note", warning is not None and "pin a non-default grok model" in warning)
    check("codex --model is silent", D._grok_model_passthrough_warning(_args(agent="codex", model="gpt-5")) is None)
    check("grok without --model is silent", D._grok_model_passthrough_warning(_args(agent="grok-code")) is None)


def test_kimi_worker_argv() -> None:
    with tempfile.TemporaryDirectory() as td:
        prompt = Path(td) / "prompt.md"
        prompt.write_text("Implement the change.\n", encoding="utf-8")
        args = _args(agent="kimi", cwd="/tmp/x")
        argv, stdin_path = D.build_worker(args, prompt, [])
        joined = " ".join(argv)
        check("kimi uses login-shell cwd wrapper", argv[:2] == ["/bin/sh", "-lc"] and 'cd "$0"' in argv[2])
        check("kimi resolves off-PATH fallback", 'command -v kimi' in argv[2] and '$HOME/.kimi-code/bin/kimi' in argv[2])
        check("kimi uses print text mode", ' -p "$prompt" --output-format text ' in argv[2])
        check("kimi omits incompatible permission flags", "--auto" not in joined and "-y" not in argv and "--yolo" not in argv)
        check("kimi passes cwd as shell arg and add-dir", argv[3] == "/tmp/x" and argv[-2:] == ["--add-dir", "/tmp/x"])
        check("kimi passes prompt by argv", argv[4] == "Implement the change.\n" and stdin_path is None)
        check("kimi omits model by default", "--model" not in argv)

        args.model = "kimi-code/k3-preview"
        model_argv, _ = D.build_worker(args, prompt, [])
        check("kimi passes explicit model", model_argv[-4:] == ["--model", "kimi-code/k3-preview", "--add-dir", "/tmp/x"])


def test_kimi_worker_dash_execution() -> None:
    injected = Path("/tmp/injected")
    check("kimi injection sentinel absent before test", not injected.exists())
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        cwd = base / "cwd with spaces"
        fake_bin = base / "bin"
        cwd.mkdir()
        fake_bin.mkdir()
        prompt_text = '\"; touch /tmp/injected #'
        prompt = base / "prompt.md"
        prompt.write_text(prompt_text, encoding="utf-8")
        args_file = base / "args.txt"
        cwd_file = base / "cwd.txt"
        fake_kimi = fake_bin / "kimi"
        fake_kimi.write_text(
            '#!/bin/sh\nprintf "%s\\n" "$@" > "$KIMI_ARGS_FILE"\npwd > "$KIMI_CWD_FILE"\n',
            encoding="utf-8",
        )
        fake_kimi.chmod(0o755)

        args = _args(agent="kimi", cwd=str(cwd), model="m")
        argv, stdin_path = D.build_worker(args, prompt, [])
        env = os.environ.copy()
        env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"
        env["KIMI_ARGS_FILE"] = str(args_file)
        env["KIMI_CWD_FILE"] = str(cwd_file)
        proc = subprocess.run(
            ["/bin/dash", *argv[1:]],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            check=False,
        )
        received = args_file.read_text(encoding="utf-8").splitlines() if args_file.exists() else []
        check("kimi generated wrapper executes under dash", proc.returncode == 0)
        check(
            "kimi dash wrapper preserves exact argv",
            received == [
                "-p",
                prompt_text,
                "--output-format",
                "text",
                "--model",
                "m",
                "--add-dir",
                str(cwd),
            ],
        )
        check("kimi dash wrapper preserves cwd with spaces", cwd_file.read_text(encoding="utf-8").strip() == str(cwd))
        check("kimi dash wrapper keeps prompt as data", not injected.exists())
        check("kimi dash wrapper keeps prompt off stdin", stdin_path is None)


def test_kimi_worker_preamble_is_neutral() -> None:
    preamble = D._worker_prompt_preamble("kimi")
    check("kimi preamble contains COMPLETE contract", "COMPLETE: <summary>" in preamble)
    check("kimi preamble has neutral worker identity", "Grok worker" not in preamble)


def test_windows_acp_warnings_written_to_tail() -> None:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        tail = base / "dispatch.tail"
        args = _args(
            agent="codex-acp",
            prompt="Implement the queued change.",
            dispatch_id="win-acp",
            status_json=str(base / "dispatch.status.json"),
            tail=str(tail),
            billing="main",
            shape="acp",
            poll_secs=0.1,
            priority="normal",
            capacity_wait_s=None,
            permission_mode="auto",
            permission_dir=None,
            permission_inline_timeout_s=None,
            permission_user_timeout_s=None,
            interactive=False,
        )
        args.dispatch_warnings = ["WARN: prompt carries no git base pin; HEAD is 1234567"]

        orig_is_windows = D.goalflight_compat.is_windows
        orig_build_acp_cfg = D._build_acp_cfg
        orig_refuse_reused = D._refuse_reused_nonterminal_dispatch_id
        orig_run_acp_dispatch = ACP.run_acp_dispatch
        try:
            D.goalflight_compat.is_windows = lambda: True
            D._build_acp_cfg = lambda patched_args, status_json: SimpleNamespace(
                dispatch_id=patched_args.dispatch_id,
                agent=patched_args.agent,
            )
            D._refuse_reused_nonterminal_dispatch_id = lambda dispatch_id: None

            async def fake_run_acp_dispatch(cfg):
                return {
                    "dispatch_id": cfg.dispatch_id,
                    "agent": cfg.agent,
                    "state": "blocked_windows_dispatch",
                    "reason": "windows fixture",
                }

            ACP.run_acp_dispatch = fake_run_acp_dispatch
            with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                code = D._run_acp_shape(args, base=base, account_env={})
        finally:
            D.goalflight_compat.is_windows = orig_is_windows
            D._build_acp_cfg = orig_build_acp_cfg
            D._refuse_reused_nonterminal_dispatch_id = orig_refuse_reused
            ACP.run_acp_dispatch = orig_run_acp_dispatch

        tail_text = tail.read_text(encoding="utf-8")
        check("windows ACP refusal keeps exit code", code == 2)
        check("windows ACP warnings are written to tail", "goalflight_dispatch: WARN: prompt carries no git base pin" in tail_text)


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
          hint == "worker still alive - re-attach via goalflight_status.py --wait quiet-worker")
    hint = D._dispatch_end_reattach_hint(
        "marker-worker",
        terminal_state="watcher_stopped",
        worker_alive=True,
    )
    check("watcher-stopped live worker gets reattach hint",
          hint == "worker still alive - re-attach via goalflight_status.py --wait marker-worker")
    check("dead idle-timeout gets no hint",
          D._dispatch_end_reattach_hint("dead", terminal_state="idle_timeout", worker_alive=False) is None)


def main() -> int:
    test_default_idle_windows()
    test_read_only_review_artifact_guard()
    test_grok_code_research_intent_guard()
    test_git_pin_warning()
    test_grok_model_passthrough_warning()
    test_kimi_worker_argv()
    test_kimi_worker_dash_execution()
    test_kimi_worker_preamble_is_neutral()
    test_windows_acp_warnings_written_to_tail()
    test_reused_nonterminal_dispatch_id_guard()
    test_dispatch_end_hint()
    if _FAILS:
        print(f"\n{len(_FAILS)} FAILED: {_FAILS}")
        return 1
    print("\nall dispatch ergonomics tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
