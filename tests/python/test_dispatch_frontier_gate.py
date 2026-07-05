"""dispatch-frontier (alias: pipe) fan-out safety gate.

The command spawns one worker per frontier item — a fleet — so it must REFUSE to
fan out without explicit --autodispatch-confirm (incident 2026-07-05: a stray
`pipe` launched ~13 workers into the shared worktree). --dry-run stays a free
preview. Repo convention: case_* functions invoked by main().
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

import goalflight_task as t  # noqa: E402


class _FakeStore:
    project_root = "/tmp/proj"

    def __init__(self, rows):
        self._rows = rows

    def next_frontier(self):
        return list(self._rows)


def _args(**kw):
    base = dict(dry_run=False, autodispatch_confirm=False, json=False, agent="codex", by=None)
    base.update(kw)
    return argparse.Namespace(**base)


def _no_dispatch():
    """Replace the dispatch subprocess with a landmine; restore via return value."""
    orig = t._run_pipe_child

    def boom(*a, **k):
        raise AssertionError("dispatch attempted — gate failed to block fan-out")

    t._run_pipe_child = boom
    return orig


def case_gate_refuses_without_confirm() -> None:
    orig = _no_dispatch()
    try:
        rc = t._cmd_pipe(_FakeStore([{"id": "t-1", "prompt": "x"}, {"id": "t-2", "prompt": "y"}]), _args())
        assert rc == 2, f"expected refusal rc=2, got {rc}"
    finally:
        t._run_pipe_child = orig


def case_dry_run_previews_without_dispatch() -> None:
    orig = _no_dispatch()
    try:
        rc = t._cmd_pipe(_FakeStore([{"id": "t-1", "prompt": "x"}]), _args(dry_run=True))
        assert rc == 0, f"dry-run expected rc=0, got {rc}"
    finally:
        t._run_pipe_child = orig


def case_empty_frontier_is_silent() -> None:
    orig = _no_dispatch()
    try:
        rc = t._cmd_pipe(_FakeStore([]), _args())
        assert rc == 0, f"empty frontier expected rc=0, got {rc}"
    finally:
        t._run_pipe_child = orig


def case_both_names_parse_to_the_gate() -> None:
    parser = t.build_parser()
    for name in ("dispatch-frontier", "pipe"):
        ns = parser.parse_args([name])
        assert ns.func is t._cmd_pipe, f"{name} must route to _cmd_pipe"
        assert ns.autodispatch_confirm is False, f"{name} default must be no-confirm"
        assert ns.dry_run is False


def main() -> None:
    case_gate_refuses_without_confirm()
    case_dry_run_previews_without_dispatch()
    case_empty_frontier_is_silent()
    case_both_names_parse_to_the_gate()
    print("test_dispatch_frontier_gate: all cases passed")


if __name__ == "__main__":
    main()
