"""Failure-mode tests for the ACP layer.

Three scenarios:
  (a) worker process killed mid-prompt → session_prompt yields the
      connection-closed sentinel, reader loop logs and exits cleanly
  (b) controller crash / context manager cleanup → managed_pool drains
      live connections (proxied via the smoke_managed_pool test in
      test_acp_pipe.py; here we additionally test pool.shutdown() under
      load)
  (c) broken stdio pipe (worker writes garbage between valid frames)
      → reader loop logs JSONDecodeError and continues

Each test uses the echo-agent fixture (no real-worker CLI needed).

Run: python3 test/test_acp_failure_modes.py
Expect: OK: all failure-mode tests pass
"""

import asyncio
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from acp_client import AcpConnection, AcpProcessPool  # noqa: E402
from acp_pool import managed_pool  # noqa: E402
from acp_runner import run_prompt  # noqa: E402

FIXTURE = REPO_ROOT / "test" / "fixtures" / "acp_echo_agent.py"


async def case_a_worker_killed_mid_prompt() -> None:
    """Spawn echo, kill it externally mid-init, verify session_prompt
    yields a clean error sentinel rather than hanging."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(FIXTURE),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
        limit=8 * 1024 * 1024,
    )
    conn = AcpConnection(agent="echo", session_id="case-a", proc=proc)
    try:
        await conn.initialize()
        await conn.session_new(cwd="/tmp")
        # Kill the worker before any prompt
        os.killpg(proc.pid, signal.SIGKILL)
        await proc.wait()
        # session_prompt should now yield a _prompt_result with an error envelope,
        # not hang forever.
        result = await asyncio.wait_for(
            run_prompt(conn, "hello", idle_timeout=5),
            timeout=10,
        )
        assert result.error is not None or result.stop_reason is None, (
            f"expected error or no stop_reason after worker kill, got "
            f"error={result.error} stop_reason={result.stop_reason!r}"
        )
    finally:
        await conn.kill()


async def case_b_pool_shutdown_drains_live_connection() -> None:
    """managed_pool() cleanup MUST drain all live connections, not just the
    most recent one. Spawn two, verify both are killed on context exit."""
    agents_config = {
        "echo": {
            "command": sys.executable,
            "acp_args": [str(FIXTURE)],
            "working_dir": "/tmp",
        },
    }
    captured_procs = []
    async with managed_pool(agents_config, install_signal_handlers=False) as pool:
        for sid in ("ses-1", "ses-2"):
            conn = await pool.get_or_create("echo", sid, cwd="/tmp")
            captured_procs.append(conn.proc)
        assert pool.stats["total"] == 2, pool.stats
    # After context exit: both procs killed
    for p in captured_procs:
        assert p.returncode is not None, f"proc {p.pid} not killed on context exit"


async def case_c_broken_stdio_garbage() -> None:
    """Reader loop must survive a garbage-bytes line on stdout
    (JSONDecodeError) and keep processing subsequent valid frames."""
    # Use a custom subprocess that emits: garbage line, then valid JSON-RPC
    # response. Easiest: write a tiny shim in /tmp that does this.
    shim_path = Path("/tmp/acp-garbage-shim.py")
    shim_path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "# Emit garbage immediately\n"
        "print('this is not json at all !!! \\x00\\x01\\x02', flush=True)\n"
        "# Then handle stdin like the echo agent\n"
        "for line in sys.stdin:\n"
        "    try:\n"
        "        msg = json.loads(line)\n"
        "    except Exception:\n"
        "        continue\n"
        "    method = msg.get('method', '')\n"
        "    req_id = msg.get('id')\n"
        "    if method == 'initialize':\n"
        "        print(json.dumps({'jsonrpc':'2.0','id':req_id,'result':{'protocolVersion':1,'agentInfo':{'name':'garbage-shim','version':'0'},'capabilities':{}}}), flush=True)\n"
        "    elif method == 'session/new':\n"
        "        print(json.dumps({'jsonrpc':'2.0','id':req_id,'result':{'sessionId':'gs-1'}}), flush=True)\n"
        "    elif method == 'session/prompt':\n"
        "        sid = msg.get('params',{}).get('sessionId','')\n"
        "        print(json.dumps({'jsonrpc':'2.0','method':'session/update','params':{'sessionId':sid,'update':{'sessionUpdate':'agent_message_chunk','content':{'text':'survived garbage'}}}}), flush=True)\n"
        "        print(json.dumps({'jsonrpc':'2.0','id':req_id,'result':{'sessionId':sid,'stopReason':'end_turn'}}), flush=True)\n"
    )
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(shim_path),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            limit=8 * 1024 * 1024,
        )
        async with AcpConnection(agent="garbage", session_id="c", proc=proc) as conn:
            init = await conn.initialize()
            assert init["agentInfo"]["name"] == "garbage-shim", f"unexpected init: {init}"
            await conn.session_new(cwd="/tmp")
            result = await asyncio.wait_for(
                run_prompt(conn, "hi", idle_timeout=5),
                timeout=10,
            )
            assert result.ok, f"prompt failed: stop={result.stop_reason!r} err={result.error}"
            assert "survived garbage" in result.text, f"expected text 'survived garbage', got {result.text!r}"
    finally:
        shim_path.unlink(missing_ok=True)


async def main() -> None:
    await case_a_worker_killed_mid_prompt()
    await case_b_pool_shutdown_drains_live_connection()
    await case_c_broken_stdio_garbage()
    print("OK: all failure-mode tests pass")


if __name__ == "__main__":
    asyncio.run(main())
