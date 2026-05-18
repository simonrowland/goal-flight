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


async def case_d_idle_timeout_zero_disables_timeout() -> None:
    """idle_timeout=0 is documented as no timeout; verify it waits for a
    delayed final response instead of immediately returning agent_timeout."""
    shim_path = Path(f"/tmp/acp-delay-shim-{os.getpid()}.py")
    shim_path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys, time\n"
        "sessions = {}\n"
        "def send(obj): print(json.dumps(obj), flush=True)\n"
        "for line in sys.stdin:\n"
        "    try:\n"
        "        msg = json.loads(line)\n"
        "    except Exception:\n"
        "        continue\n"
        "    method = msg.get('method', '')\n"
        "    req_id = msg.get('id')\n"
        "    params = msg.get('params', {}) or {}\n"
        "    if method == 'initialize':\n"
        "        send({'jsonrpc':'2.0','id':req_id,'result':{'protocolVersion':1,'agentInfo':{'name':'delay-shim','version':'0'},'capabilities':{}}})\n"
        "    elif method == 'session/new':\n"
        "        sessions['s'] = {'cwd': params.get('cwd', '/tmp')}\n"
        "        send({'jsonrpc':'2.0','id':req_id,'result':{'sessionId':'s'}})\n"
        "    elif method == 'session/prompt':\n"
        "        text = ''.join(p.get('text','') for p in params.get('prompt', []) if p.get('type') == 'text')\n"
        "        time.sleep(0.25)\n"
        "        send({'jsonrpc':'2.0','method':'session/update','params':{'sessionId':'s','update':{'sessionUpdate':'agent_message_chunk','content':{'text':'delayed: '+text}}}})\n"
        "        send({'jsonrpc':'2.0','id':req_id,'result':{'sessionId':'s','stopReason':'end_turn'}})\n"
        "    elif method == 'session/cancel':\n"
        "        pass\n"
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
        async with AcpConnection(agent="delay", session_id="d", proc=proc) as conn:
            init = await conn.initialize()
            assert init["agentInfo"]["name"] == "delay-shim", f"unexpected init: {init}"
            await conn.session_new(cwd="/tmp")
            result = await asyncio.wait_for(
                run_prompt(conn, "slow", idle_timeout=0),
                timeout=3,
            )
            assert result.ok, f"expected delayed success, got err={result.error} stop={result.stop_reason!r}"
            assert "delayed: slow" in result.text, f"expected delayed text, got {result.text!r}"
    finally:
        shim_path.unlink(missing_ok=True)


async def case_e_idle_timeout_clears_pending_and_reuses_connection() -> None:
    """Timed-out session/prompt calls must not leak _pending entries and the
    same ACP connection should remain usable for a later prompt."""
    shim_path = Path(f"/tmp/acp-timeout-shim-{os.getpid()}.py")
    shim_path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "sessions = {}\n"
        "def send(obj): print(json.dumps(obj), flush=True)\n"
        "for line in sys.stdin:\n"
        "    try:\n"
        "        msg = json.loads(line)\n"
        "    except Exception:\n"
        "        continue\n"
        "    method = msg.get('method', '')\n"
        "    req_id = msg.get('id')\n"
        "    params = msg.get('params', {}) or {}\n"
        "    if method == 'initialize':\n"
        "        send({'jsonrpc':'2.0','id':req_id,'result':{'protocolVersion':1,'agentInfo':{'name':'timeout-shim','version':'0'},'capabilities':{}}})\n"
        "    elif method == 'session/new':\n"
        "        sessions['s'] = {'cwd': params.get('cwd', '/tmp')}\n"
        "        send({'jsonrpc':'2.0','id':req_id,'result':{'sessionId':'s'}})\n"
        "    elif method == 'session/prompt':\n"
        "        text = ''.join(p.get('text','') for p in params.get('prompt', []) if p.get('type') == 'text')\n"
        "        if text == 'hang':\n"
        "            continue\n"
        "        send({'jsonrpc':'2.0','method':'session/update','params':{'sessionId':'s','update':{'sessionUpdate':'agent_message_chunk','content':{'text':'ok: '+text}}}})\n"
        "        send({'jsonrpc':'2.0','id':req_id,'result':{'sessionId':'s','stopReason':'end_turn'}})\n"
        "    elif method == 'session/cancel':\n"
        "        pass\n"
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
        async with AcpConnection(agent="timeout", session_id="e", proc=proc) as conn:
            init = await conn.initialize()
            assert init["agentInfo"]["name"] == "timeout-shim", f"unexpected init: {init}"
            await conn.session_new(cwd="/tmp")
            timed_out = await asyncio.wait_for(
                run_prompt(conn, "hang", idle_timeout=0.1),
                timeout=3,
            )
            assert timed_out.error is not None, "expected idle timeout error"
            assert timed_out.error.get("message") == "agent_timeout (idle)", timed_out.error
            assert conn._pending == {}, f"pending request leaked after timeout: {conn._pending!r}"

            recovered = await asyncio.wait_for(
                run_prompt(conn, "hello", idle_timeout=3),
                timeout=3,
            )
            assert recovered.ok, f"connection not reusable: err={recovered.error} stop={recovered.stop_reason!r}"
            assert "ok: hello" in recovered.text, f"expected recovery text, got {recovered.text!r}"
    finally:
        shim_path.unlink(missing_ok=True)


async def case_f_cancelled_send_request_clears_pending() -> None:
    """If an awaiter cancels _send_request (e.g. wait_for timeout), the request
    future must be removed so later responses/prompts don't inherit stale state."""
    shim_path = Path(f"/tmp/acp-cancel-request-shim-{os.getpid()}.py")
    shim_path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "def send(obj): print(json.dumps(obj), flush=True)\n"
        "for line in sys.stdin:\n"
        "    try:\n"
        "        msg = json.loads(line)\n"
        "    except Exception:\n"
        "        continue\n"
        "    method = msg.get('method', '')\n"
        "    req_id = msg.get('id')\n"
        "    params = msg.get('params', {}) or {}\n"
        "    if method == 'initialize':\n"
        "        send({'jsonrpc':'2.0','id':req_id,'result':{'protocolVersion':1,'agentInfo':{'name':'cancel-request-shim','version':'0'},'capabilities':{}}})\n"
        "    elif method == 'session/new':\n"
        "        send({'jsonrpc':'2.0','id':req_id,'result':{'sessionId':'s'}})\n"
        "    elif method == 'stall/request':\n"
        "        continue\n"
        "    elif method == 'session/prompt':\n"
        "        text = ''.join(p.get('text','') for p in params.get('prompt', []) if p.get('type') == 'text')\n"
        "        send({'jsonrpc':'2.0','method':'session/update','params':{'sessionId':'s','update':{'sessionUpdate':'agent_message_chunk','content':{'text':'ok: '+text}}}})\n"
        "        send({'jsonrpc':'2.0','id':req_id,'result':{'sessionId':'s','stopReason':'end_turn'}})\n"
        "    elif method == 'session/cancel':\n"
        "        pass\n"
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
        async with AcpConnection(agent="cancel-request", session_id="f", proc=proc) as conn:
            init = await conn.initialize()
            assert init["agentInfo"]["name"] == "cancel-request-shim", f"unexpected init: {init}"
            await conn.session_new(cwd="/tmp")
            try:
                await asyncio.wait_for(conn._send_request("stall/request", {}), timeout=0.1)
                raise AssertionError("stall/request unexpectedly returned")
            except asyncio.TimeoutError:
                pass
            assert conn._pending == {}, f"pending request leaked after cancellation: {conn._pending!r}"

            recovered = await asyncio.wait_for(
                run_prompt(conn, "after-cancel", idle_timeout=3),
                timeout=3,
            )
            assert recovered.ok, f"connection not reusable: err={recovered.error} stop={recovered.stop_reason!r}"
            assert "ok: after-cancel" in recovered.text, f"expected recovery text, got {recovered.text!r}"
    finally:
        shim_path.unlink(missing_ok=True)


async def main() -> None:
    await case_a_worker_killed_mid_prompt()
    await case_b_pool_shutdown_drains_live_connection()
    await case_c_broken_stdio_garbage()
    await case_d_idle_timeout_zero_disables_timeout()
    await case_e_idle_timeout_clears_pending_and_reuses_connection()
    await case_f_cancelled_send_request_clears_pending()
    print("OK: all failure-mode tests pass")


if __name__ == "__main__":
    asyncio.run(main())
