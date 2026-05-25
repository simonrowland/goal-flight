"""Drive the vendored ACP client against a real worker CLI.

Usage:
  ~/.goal-flight/venvs/acp-0.10/bin/python test/probe_real_worker.py codex-acp
  ~/.goal-flight/venvs/acp-0.10/bin/python test/probe_real_worker.py grok agent stdio
  ~/.goal-flight/venvs/acp-0.10/bin/python test/probe_real_worker.py \\
      --cwd /tmp/opencode-acp opencode acp

Probe flags (--cwd, --timeout, --prompt) must appear before the worker command.
Do not pass them after ``opencode acp`` — argparse would forward them to the
worker and break the ACP handshake.

Sends a single short prompt ("What is 2+2? Reply with just the number.") and
prints a structured PromptResult summary plus timing breakdown. Useful for
verifying that a worker speaks ACP correctly and for measuring cold-start cost.

Exit code: 0 if PromptResult.ok (stop_reason == "end_turn", no error); 1 otherwise.
"""

import argparse
import asyncio
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from acp_runner import run_prompt  # noqa: E402
from goalflight_acp_client import spawn_acp_connection  # noqa: E402


async def probe(cmd: str, args: list[str], *, prompt: str, idle_timeout: float, cwd: str) -> int:
    t0 = time.time()

    def stamp(label: str) -> str:
        return f"[{time.time() - t0:5.1f}s]"

    print(f"{stamp('start')} spawning: {cmd} {' '.join(args)}", flush=True)
    conn = await spawn_acp_connection(
        cmd,
        args,
        agent=cmd,
        session_id="probe-1",
        cwd=cwd,
        auto_allow_tools=True,
    )
    proc = conn.proc
    try:
        try:
            init = await asyncio.wait_for(conn.initialize(), timeout=idle_timeout)
        except asyncio.TimeoutError:
            print(f"{stamp('init')} TIMEOUT during initialize() after {idle_timeout}s")
            return 2
        print(f"{stamp('init')} agentInfo={init.agent_info} caps={init.agent_capabilities}", flush=True)

        try:
            sid = await asyncio.wait_for(conn.new_session(cwd=cwd), timeout=idle_timeout)
        except asyncio.TimeoutError:
            print(f"{stamp('sess')} TIMEOUT during session_new() after {idle_timeout}s")
            return 2
        print(f"{stamp('sess')} sessionId={sid}", flush=True)

        result = await run_prompt(conn, prompt, idle_timeout=idle_timeout)
        print(f"{stamp('done')} stop_reason={result.stop_reason!r} error={result.error}")
        print(f"  text     ({len(result.text):>5} chars): {result.text[:300]!r}{'...' if len(result.text) > 300 else ''}")
        if result.thoughts:
            print(f"  thoughts ({len(result.thoughts):>5} chars): {result.thoughts[:300]!r}{'...' if len(result.thoughts) > 300 else ''}")
        if result.tool_calls:
            print(f"  tool_calls: {len(result.tool_calls)} events")
            for tc in result.tool_calls[:3]:
                print(f"    - {tc.get('toolCallId', '?')[:8]} {tc.get('title', '?')[:60]} status={tc.get('status', '?')}")
        if result.plan_entries:
            print(f"  plan_entries: {len(result.plan_entries)}")
            for pe in result.plan_entries[:3]:
                print(f"    - {pe[:80]}")
        return 0 if result.ok else 1
    finally:
        await conn.kill()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--prompt", default="What is 2+2? Reply with just the number.")
    p.add_argument("--timeout", type=float, default=120.0, help="idle_timeout per phase")
    p.add_argument("--cwd", default="/tmp/acp-probe", help="cwd handed to the worker")
    p.add_argument("cmd", help="worker CLI binary (e.g. codex-acp, grok, opencode)")
    p.add_argument("args", nargs=argparse.REMAINDER, help="args passed to the worker after cmd")
    a = p.parse_args()
    worker_args = list(a.args)
    if worker_args[:1] == ["--"]:
        worker_args = worker_args[1:]
    for bad in ("--prompt", "--timeout", "--cwd"):
        if bad in worker_args:
            p.error(f"{bad} must appear before the worker command, not in worker args")
    Path(a.cwd).mkdir(parents=True, exist_ok=True)
    return asyncio.run(probe(a.cmd, worker_args, prompt=a.prompt, idle_timeout=a.timeout, cwd=a.cwd))


if __name__ == "__main__":
    sys.exit(main())
