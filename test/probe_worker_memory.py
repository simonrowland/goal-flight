"""Measure RSS (resident memory) of each ACP worker process tree.

Spawns each worker, runs a trivial prompt, samples the worker's whole process
tree's RSS at idle (post-init) and tracks the peak during the prompt. Prints
a comparison table. Useful for pool-sizing decisions on a given box.

Usage: python3 test/probe_worker_memory.py
"""

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from acp_client import AcpConnection  # noqa: E402
from acp_runner import run_prompt  # noqa: E402

WORKERS: list[tuple[str, list[str]]] = [
    ("codex-acp", []),
    ("grok", ["agent", "stdio"]),
    ("cursor-agent", ["acp"]),
    ("claude-code-cli-acp", []),
]


def tree_rss_mb(root_pid: int) -> float:
    """Sum RSS of root_pid and all descendants. Returns MB."""
    r = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,rss="],
        capture_output=True, text=True,
    )
    rows: list[tuple[int, int, int]] = []
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) == 3:
            try:
                rows.append((int(parts[0]), int(parts[1]), int(parts[2])))
            except ValueError:
                pass
    by_ppid: dict[int, list[tuple[int, int]]] = {}
    by_pid: dict[int, int] = {}
    for pid, ppid, rss in rows:
        by_ppid.setdefault(ppid, []).append((pid, rss))
        by_pid[pid] = rss
    total_kb = 0
    stack = [root_pid]
    seen: set[int] = set()
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        total_kb += by_pid.get(cur, 0)
        for child_pid, _ in by_ppid.get(cur, []):
            stack.append(child_pid)
    return total_kb / 1024  # ps RSS is in KB


async def probe(cmd: str, args: list[str], cwd: str) -> tuple[float, float, int]:
    proc = await asyncio.create_subprocess_exec(
        cmd, *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd, start_new_session=True, limit=8 * 1024 * 1024,
    )
    conn = AcpConnection(agent=cmd, session_id="mem", proc=proc, auto_allow_tools=True)
    rss_peak = 0.0
    proc_count = 0
    try:
        await asyncio.wait_for(conn.initialize(), timeout=30)
        await asyncio.wait_for(conn.session_new(cwd=cwd), timeout=30)
        await asyncio.sleep(2)  # let process tree settle
        idle_rss = tree_rss_mb(proc.pid)
        # Count processes in the tree
        r = subprocess.run(["ps", "-axo", "pid=,ppid="], capture_output=True, text=True)
        children_of: dict[int, list[int]] = {}
        for line in r.stdout.splitlines():
            p = line.split()
            if len(p) == 2:
                try:
                    children_of.setdefault(int(p[1]), []).append(int(p[0]))
                except ValueError:
                    pass
        stack = [proc.pid]
        seen: set[int] = set()
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            stack.extend(children_of.get(cur, []))
        proc_count = len(seen)

        # Now run prompt + sample RSS in parallel
        async def sampler() -> None:
            nonlocal rss_peak
            while True:
                cur = tree_rss_mb(proc.pid)
                if cur > rss_peak:
                    rss_peak = cur
                await asyncio.sleep(0.25)

        rss_peak = idle_rss
        sampler_task = asyncio.create_task(sampler())
        try:
            await asyncio.wait_for(
                run_prompt(conn, "Reply with the single word: ok", idle_timeout=60),
                timeout=60,
            )
        finally:
            sampler_task.cancel()
            try:
                await sampler_task
            except asyncio.CancelledError:
                pass
        return idle_rss, rss_peak, proc_count
    finally:
        await conn.kill()


async def main() -> None:
    print(f"{'Worker':<24} {'Procs':>6} {'Idle RSS (MB)':>15} {'Peak RSS (MB)':>15}")
    print("-" * 64)
    for cmd, args in WORKERS:
        cwd = f"/tmp/acp-mem-{cmd}"
        os.makedirs(cwd, exist_ok=True)
        try:
            idle, peak, count = await probe(cmd, args, cwd)
            print(f"{cmd:<24} {count:>6} {idle:>15.1f} {peak:>15.1f}")
        except Exception as e:
            print(f"{cmd:<24} ERROR: {type(e).__name__}: {e}")


if __name__ == "__main__":
    asyncio.run(main())
