#!/usr/bin/env python3
"""A read-only grok dispatch must carry enforcement, not just a polite brief.

`--os-sandbox` is a bash-shape-codex / ACP-runner knob. Grok's bash CLI ignores
it, so an accepted-but-inert safety flag is refused at dispatch. `--read-only`
is the grok alternative, and it must actually constrain the worker.

`--deny` rules are the surface the grok CLI honours. Probed on grok 1.0.0 with
a write-a-file prompt: without deny rules the file was written; with
`--deny Write --deny Edit --deny Bash` the model tried the write tool, then a
shell command, then a relative path, and finally reported that writes were
blocked.

The broken `--permission-mode` flag (documented in goalflight_dispatch.py) is a
different surface and stays omitted; nothing here touches it.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_dispatch as D  # noqa: E402


def _args(**over):
    base = dict(
        agent="grok-code",
        shape="bash",
        model=None,
        cwd=None,
        read_only=False,
        os_sandbox=None,
        account=None,
        billing="sub",
        dispatch_id="t",
        parent_dispatch_id=None,
        engine_session_id=None,
        web_qa=False,
        web_research_ok=False,
        no_orientation=True,
        fast=False,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _grok_argv(args) -> list[str]:
    with tempfile.TemporaryDirectory() as td:
        prompt = Path(td) / "prompt.md"
        prompt.write_text("Review the change.\n", encoding="utf-8")
        argv, _stdin = D.build_worker(args, prompt, [])
    return argv


def _deny_values(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, tok in enumerate(argv[:-1]) if tok == "--deny"]


def test_read_only_grok_denies_every_write_capable_tool() -> None:
    denied = _deny_values(_grok_argv(_args(read_only=True)))
    assert set(denied) >= {"Write", "Edit", "Bash"}, denied


def test_writable_grok_carries_no_deny_rules() -> None:
    argv = _grok_argv(_args(read_only=False))
    assert "--deny" not in argv, argv


def test_read_only_enforcement_is_scoped_to_grok() -> None:
    with tempfile.TemporaryDirectory() as td:
        prompt = Path(td) / "prompt.md"
        prompt.write_text("Review.\n", encoding="utf-8")
        args = _args(agent="codex", read_only=True, os_sandbox="read-only")
        argv, _ = D.build_worker(args, prompt, [])
    assert "--deny" not in argv, argv
