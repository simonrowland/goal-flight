#!/usr/bin/env python3
"""A read-only grok dispatch must carry enforcement, not just a polite brief.

Grok has no OS sandbox in this dispatcher (`--os-sandbox` is a codex-bash
knob, warned as ignored for everything else) and its ACP adapter bypasses the
permission gate for writes. Until 2026-08-25 a "read-only" grok review was
therefore constrained by nothing but the brief's phrasing.

`--deny` rules are the surface the grok CLI actually honours. Probed on grok
1.0.0 with a write-a-file prompt: without deny rules the file was written; with
`--deny Write --deny Edit --deny Bash` the model tried the write tool, then a
shell command, then a relative path, and finally reported that writes were
blocked. The rules held against the model's own bypass attempts — which also
shows what an un-denied tool would have meant: the model reaches for the next
tool on its own initiative.

The broken `--permission-mode` flag (documented at length in
goalflight_dispatch.py, with its failure set MOVING between grok releases) is a
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
    # The probed set: the write tool, the editor, and the shell the model
    # reached for when the write tool was blocked. A missing member here is an
    # open bypass route, not a smaller feature.
    assert set(denied) >= {"Write", "Edit", "Bash"}, denied


def test_writable_grok_carries_no_deny_rules() -> None:
    """The default write dispatch must be untouched.

    The measured history in goalflight_dispatch.py shows grok permission flags
    causing silent no-op workers; enforcement must therefore be scoped to
    dispatches that ASKED for read-only, never ambient.
    """
    argv = _grok_argv(_args(read_only=False))
    assert "--deny" not in argv, argv


def test_read_only_enforcement_is_scoped_to_grok() -> None:
    """codex read-only goes through its own sandbox; no grok flags may leak."""
    with tempfile.TemporaryDirectory() as td:
        prompt = Path(td) / "prompt.md"
        prompt.write_text("Review.\n", encoding="utf-8")
        args = _args(agent="codex", read_only=True, os_sandbox="read-only")
        argv, _ = D.build_worker(args, prompt, [])
    assert "--deny" not in argv, argv
