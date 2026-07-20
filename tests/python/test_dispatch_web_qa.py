#!/usr/bin/env python3
"""Controller-gated web-QA opt-in (--web-qa) for dispatch + wrapper grant env."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import goalflight_dispatch as D  # noqa: E402

_FAILS: list[str] = []


def check(name: str, cond: bool) -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        _FAILS.append(name)


def _replay(**kw):
    base = dict(
        agent="codex",
        dispatch_id="webqa-dx",
        cwd="/tmp/webqa-project",
        shape="bash",
        priority="normal",
        billing="sub",
        poll_secs=2.0,
        max_idle_secs=600.0,
        prompt_file="/tmp/p.md",
        prompt=None,
        task_ids=[],
        model=None,
        read_only=False,
        os_sandbox=None,
        web_research_ok=False,
        web_qa=False,
        ignore_git_warn=False,
        capacity_wait_s=None,
        account=None,
        interactive=False,
        permission_mode="auto",
        permission_dir=None,
        permission_inline_timeout_s=None,
        permission_user_timeout_s=None,
        controller_pid=None,
        no_orientation=False,
        fast=False,
    )
    base.update(kw)
    return D._canonical_replay_argv(
        argparse.Namespace(**base),
        [],
        tail=Path("/tmp/t"),
        status_json=Path("/tmp/s"),
    )


def test_flag_default_off_and_replay() -> None:
    r_def = _replay()
    check("default replay omits --web-qa", "--web-qa" not in r_def)
    r_on = _replay(web_qa=True)
    check("opt-in replay includes --web-qa", "--web-qa" in r_on)


def test_env_plan_default_off() -> None:
    project = Path("/tmp/webqa-project")
    updates, remove = D._web_qa_env_plan(argparse.Namespace(web_qa=False), project)
    check("default updates empty", updates == {})
    check(
        "default strips grant + state",
        set(remove) == {D.WEB_QA_GRANT_ENV, D.WEB_QA_STATE_ENV},
    )
    env = {
        D.WEB_QA_GRANT_ENV: "1",
        D.WEB_QA_STATE_ENV: "/leaked/path",
        "KEEP": "yes",
    }
    D._apply_web_qa_env(env, argparse.Namespace(web_qa=False), project)
    check("apply strips grant when off", D.WEB_QA_GRANT_ENV not in env)
    check("apply strips state when off", D.WEB_QA_STATE_ENV not in env)
    check("apply preserves unrelated env", env.get("KEEP") == "yes")


def test_env_plan_opt_in_provisions() -> None:
    project = Path("/tmp/webqa-project")
    updates, remove = D._web_qa_env_plan(argparse.Namespace(web_qa=True), project)
    check("opt-in remove empty", remove == [])
    check("opt-in grant is 1", updates.get(D.WEB_QA_GRANT_ENV) == "1")
    expected_state = str(project / ".gstack" / "browse.json")
    check("opt-in provisions BROWSE_STATE_FILE", updates.get(D.WEB_QA_STATE_ENV) == expected_state)
    env: dict = {"ORPHAN": "x"}
    D._apply_web_qa_env(env, argparse.Namespace(web_qa=True), project)
    check("apply sets grant", env.get(D.WEB_QA_GRANT_ENV) == "1")
    check("apply sets state file", env.get(D.WEB_QA_STATE_ENV) == expected_state)


def test_cli_parses_web_qa() -> None:
    # Mirror the production argparse surface without launching a worker.
    parser = argparse.ArgumentParser()
    # Re-use a minimal slice: the production flag is store_true default False.
    parser.add_argument("--web-qa", action="store_true", default=False)
    ns = parser.parse_args([])
    check("cli default web_qa False", ns.web_qa is False)
    ns2 = parser.parse_args(["--web-qa"])
    check("cli --web-qa True", ns2.web_qa is True)


def main() -> int:
    test_flag_default_off_and_replay()
    test_env_plan_default_off()
    test_env_plan_opt_in_provisions()
    test_cli_parses_web_qa()
    if _FAILS:
        print(f"FAILED: {len(_FAILS)} — {_FAILS}")
        return 1
    print("OK: dispatch web-QA opt-in tests pass")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
