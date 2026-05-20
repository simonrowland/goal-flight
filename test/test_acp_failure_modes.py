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

from acp_client import AcpConnection, AcpError, AcpProcessPool  # noqa: E402
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


async def case_g_handshake_timeout_on_wedged_init() -> None:
    """A worker that spawns but never answers `initialize` must fail fast with
    an AcpError (the codex-acp handshake wedge: idle CPU, empty log, no status
    JSON), not hang forever. Verifies the 0.4.2 handshake timeout — without it
    `await fut` blocked indefinitely, and since handshake precedes
    session_prompt the execution idle-timeout never applied (worse after 0.4.1
    raised goal-mode idle-timeout to 36000s)."""
    shim_path = Path(f"/tmp/acp-wedge-init-shim-{os.getpid()}.py")
    shim_path.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, time\n"
        "# Spawn, consume stdin, but NEVER respond to initialize — the wedge.\n"
        "for line in sys.stdin:\n"
        "    pass\n"
        "time.sleep(3600)\n"
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
        async with AcpConnection(agent="wedge-init", session_id="g", proc=proc) as conn:
            raised = False
            loop = asyncio.get_event_loop()
            start = loop.time()
            try:
                await conn.initialize(timeout=2)
            except AcpError as e:
                raised = True
                msg = str(e).lower()
                assert "no response" in msg or "wedged" in msg, f"unexpected error text: {e}"
            elapsed = loop.time() - start
            assert raised, "initialize() did not raise on wedged handshake (hung?)"
            assert elapsed < 5, f"handshake timeout too slow: {elapsed:.1f}s (expected ~2s)"
            assert conn._pending == {}, f"pending request leaked after handshake timeout: {conn._pending!r}"
    finally:
        shim_path.unlink(missing_ok=True)


async def case_h_on_idle_keeps_busy_worker_alive() -> None:
    """The codex P1 regression: a worker silent past idle_timeout but reported
    ALIVE by the on_idle hook must NOT be cancelled — the runner keeps waiting
    and receives the delayed result. Before the fix, session_prompt cancelled on
    idle with no liveness check, false-positiving healthy-but-quiet ACP workers
    (the retry storm). The shim stays silent ~1.5s (past the idle window) then
    answers; on_idle returns True so the prompt rides it out."""
    shim_path = Path(f"/tmp/acp-onidle-keep-shim-{os.getpid()}.py")
    shim_path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys, time\n"
        "def send(o): print(json.dumps(o), flush=True)\n"
        "for line in sys.stdin:\n"
        "    try:\n"
        "        msg = json.loads(line)\n"
        "    except Exception:\n"
        "        continue\n"
        "    m = msg.get('method', ''); rid = msg.get('id')\n"
        "    if m == 'initialize':\n"
        "        send({'jsonrpc':'2.0','id':rid,'result':{'protocolVersion':1,'agentInfo':{'name':'onidle-keep','version':'0'},'capabilities':{}}})\n"
        "    elif m == 'session/new':\n"
        "        send({'jsonrpc':'2.0','id':rid,'result':{'sessionId':'s'}})\n"
        "    elif m == 'session/prompt':\n"
        "        time.sleep(1.5)\n"
        "        send({'jsonrpc':'2.0','method':'session/update','params':{'sessionId':'s','update':{'sessionUpdate':'agent_message_chunk','content':{'text':'finished after silence'}}}})\n"
        "        send({'jsonrpc':'2.0','id':rid,'result':{'sessionId':'s','stopReason':'end_turn'}})\n"
        "    elif m == 'session/cancel':\n"
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
        async with AcpConnection(agent="onidle-keep", session_id="h", proc=proc) as conn:
            await conn.initialize()
            await conn.session_new(cwd="/tmp")
            idle_calls = 0

            def keep_alive():
                nonlocal idle_calls
                idle_calls += 1
                return True

            result = await asyncio.wait_for(
                run_prompt(conn, "go", idle_timeout=0.1, on_idle=keep_alive),
                timeout=8,
            )
            assert result.ok, f"expected success after deferred idle, got err={result.error} stop={result.stop_reason!r}"
            assert "finished after silence" in result.text, result.text
            assert idle_calls >= 1, f"on_idle was never consulted — idle path not wired (calls={idle_calls})"
    finally:
        shim_path.unlink(missing_ok=True)


async def case_i_on_idle_false_still_cancels() -> None:
    """When the liveness hook says wedged (returns False), the runner must still
    cancel with agent_timeout (idle) — and the hook must be consulted (proves the
    idle path runs the hook rather than bypassing it). Uses a hang shim that
    answers the handshake but never the prompt."""
    shim_path = Path(f"/tmp/acp-onidle-wedged-shim-{os.getpid()}.py")
    shim_path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "def send(o): print(json.dumps(o), flush=True)\n"
        "for line in sys.stdin:\n"
        "    try:\n"
        "        msg = json.loads(line)\n"
        "    except Exception:\n"
        "        continue\n"
        "    m = msg.get('method', ''); rid = msg.get('id')\n"
        "    if m == 'initialize':\n"
        "        send({'jsonrpc':'2.0','id':rid,'result':{'protocolVersion':1,'agentInfo':{'name':'onidle-wedged','version':'0'},'capabilities':{}}})\n"
        "    elif m == 'session/new':\n"
        "        send({'jsonrpc':'2.0','id':rid,'result':{'sessionId':'s'}})\n"
        "    elif m == 'session/prompt':\n"
        "        continue\n"
        "    elif m == 'session/cancel':\n"
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
        async with AcpConnection(agent="onidle-wedged", session_id="i", proc=proc) as conn:
            await conn.initialize()
            await conn.session_new(cwd="/tmp")
            idle_calls = 0

            def say_wedged():
                nonlocal idle_calls
                idle_calls += 1
                return False

            result = await asyncio.wait_for(
                run_prompt(conn, "hang", idle_timeout=0.1, on_idle=say_wedged),
                timeout=5,
            )
            assert result.error is not None, "expected idle timeout error"
            assert result.error.get("message") == "agent_timeout (idle)", result.error
            assert idle_calls >= 1, f"on_idle not consulted before cancel (calls={idle_calls})"

            # A hook that RAISES must be treated as wedged (conservative
            # fallback), not swallow the cancel. Connection is reusable after a
            # timeout (see case_e), so reuse it here.
            def boom():
                raise RuntimeError("liveness hook failure")

            raised_result = await asyncio.wait_for(
                run_prompt(conn, "hang", idle_timeout=0.1, on_idle=boom),
                timeout=5,
            )
            assert raised_result.error is not None, "raising hook should still time out"
            assert raised_result.error.get("message") == "agent_timeout (idle)", raised_result.error
    finally:
        shim_path.unlink(missing_ok=True)


async def case_j_handshake_retry_once() -> None:
    """spawn_and_handshake_with_retry must kill+respawn a worker that wedges the
    handshake (the intermittent codex-acp wedge) and succeed on the retry. A
    marker file makes the shim wedge on its first spawn and behave on the
    second. Asserts: 2 spawn attempts, the first (wedged) worker killed before
    the retry, the returned connection handshook and is usable."""
    from goalflight_acp_run import spawn_and_handshake_with_retry

    marker = Path(f"/tmp/acp-retry-marker-{os.getpid()}")
    marker.unlink(missing_ok=True)
    shim_path = Path(f"/tmp/acp-retry-shim-{os.getpid()}.py")
    shim_path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys, os, time\n"
        "marker = sys.argv[1]\n"
        "if not os.path.exists(marker):\n"
        "    open(marker, 'w').close()\n"
        "    # First spawn: wedge — consume stdin, never answer initialize.\n"
        "    for line in sys.stdin:\n"
        "        pass\n"
        "    time.sleep(3600)\n"
        "    sys.exit(0)\n"
        "def send(o): print(json.dumps(o), flush=True)\n"
        "for line in sys.stdin:\n"
        "    try:\n"
        "        msg = json.loads(line)\n"
        "    except Exception:\n"
        "        continue\n"
        "    m = msg.get('method', ''); rid = msg.get('id')\n"
        "    if m == 'initialize':\n"
        "        send({'jsonrpc':'2.0','id':rid,'result':{'protocolVersion':1,'agentInfo':{'name':'retry-shim','version':'0'},'capabilities':{}}})\n"
        "    elif m == 'session/new':\n"
        "        send({'jsonrpc':'2.0','id':rid,'result':{'sessionId':'s'}})\n"
        "    elif m == 'session/prompt':\n"
        "        send({'jsonrpc':'2.0','method':'session/update','params':{'sessionId':'s','update':{'sessionUpdate':'agent_message_chunk','content':{'text':'retried ok'}}}})\n"
        "        send({'jsonrpc':'2.0','id':rid,'result':{'sessionId':'s','stopReason':'end_turn'}})\n"
    )
    attempts_seen: list = []

    async def track(attempt: int, p) -> None:
        attempts_seen.append(p)

    conn = None
    try:
        proc, conn = await asyncio.wait_for(
            spawn_and_handshake_with_retry(
                sys.executable, [str(shim_path), str(marker)],
                agent="retry", session_id="j", cwd="/tmp",
                handshake_timeout=2, on_attempt=track,
            ),
            timeout=15,
        )
        assert len(attempts_seen) == 2, f"expected 2 spawn attempts, got {len(attempts_seen)}"
        assert attempts_seen[0].returncode is not None, "first (wedged) worker not killed before retry"
        assert proc is attempts_seen[1], "returned proc is not the successful (second) worker"
        assert conn.acp_session_id is not None, "handshake did not complete on retry"
        recovered = await asyncio.wait_for(run_prompt(conn, "go", idle_timeout=3), timeout=5)
        assert recovered.ok and "retried ok" in recovered.text, f"retry connection unusable: {recovered.text!r}"
    finally:
        if conn is not None:
            await conn.kill()
        marker.unlink(missing_ok=True)
        shim_path.unlink(missing_ok=True)


async def case_k_on_idle_async_race_does_not_drop_result() -> None:
    """Regression for the codex re-review P1: when on_idle is ASYNC, the prompt's
    final response can arrive DURING the `await on_idle()`. A False verdict must
    NOT then cancel the just-completed prompt and return agent_timeout —
    session_prompt rechecks `fut.done()` after the hook and yields the real
    result. The shim answers ~1.3s after the prompt; the async hook (idle fires
    ~1s in) sleeps ~0.7s and returns False, so the response lands inside the
    hook's await window. Without the fix this drops the result → agent_timeout."""
    shim_path = Path(f"/tmp/acp-onidle-race-shim-{os.getpid()}.py")
    shim_path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys, time\n"
        "def send(o): print(json.dumps(o), flush=True)\n"
        "for line in sys.stdin:\n"
        "    try:\n"
        "        msg = json.loads(line)\n"
        "    except Exception:\n"
        "        continue\n"
        "    m = msg.get('method', ''); rid = msg.get('id')\n"
        "    if m == 'initialize':\n"
        "        send({'jsonrpc':'2.0','id':rid,'result':{'protocolVersion':1,'agentInfo':{'name':'onidle-race','version':'0'},'capabilities':{}}})\n"
        "    elif m == 'session/new':\n"
        "        send({'jsonrpc':'2.0','id':rid,'result':{'sessionId':'s'}})\n"
        "    elif m == 'session/prompt':\n"
        "        time.sleep(1.3)\n"
        "        send({'jsonrpc':'2.0','method':'session/update','params':{'sessionId':'s','update':{'sessionUpdate':'agent_message_chunk','content':{'text':'late result'}}}})\n"
        "        send({'jsonrpc':'2.0','id':rid,'result':{'sessionId':'s','stopReason':'end_turn'}})\n"
        "    elif m == 'session/cancel':\n"
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
        async with AcpConnection(agent="onidle-race", session_id="k", proc=proc) as conn:
            await conn.initialize()
            await conn.session_new(cwd="/tmp")

            async def slow_wedged():
                # Slow async hook: the prompt response arrives during this await.
                await asyncio.sleep(0.7)
                return False  # claim wedged — must NOT drop the completed result

            result = await asyncio.wait_for(
                run_prompt(conn, "go", idle_timeout=0.1, on_idle=slow_wedged),
                timeout=8,
            )
            assert result.ok, f"async-hook race dropped the result: err={result.error} stop={result.stop_reason!r}"
            assert "late result" in result.text, result.text
    finally:
        shim_path.unlink(missing_ok=True)


async def case_l_progress_event_during_async_hook_not_dropped() -> None:
    """Sibling of case_k (codex round-3 P1): a NON-terminal progress event can
    arrive on the queue during `await on_idle()` while the final response is
    still pending. A False verdict must NOT cancel — a queued event is proof of
    life. session_prompt continues when `q` is non-empty (or fut.done, or
    CPU-busy). The shim emits a progress chunk ~1.3s in (lands inside the hook
    await), then the result ~2s later. Without the fix this cancels at the first
    idle as agent_timeout; with it, the prompt completes."""
    shim_path = Path(f"/tmp/acp-onidle-progress-shim-{os.getpid()}.py")
    shim_path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys, time\n"
        "def send(o): print(json.dumps(o), flush=True)\n"
        "for line in sys.stdin:\n"
        "    try:\n"
        "        msg = json.loads(line)\n"
        "    except Exception:\n"
        "        continue\n"
        "    m = msg.get('method', ''); rid = msg.get('id')\n"
        "    if m == 'initialize':\n"
        "        send({'jsonrpc':'2.0','id':rid,'result':{'protocolVersion':1,'agentInfo':{'name':'onidle-progress','version':'0'},'capabilities':{}}})\n"
        "    elif m == 'session/new':\n"
        "        send({'jsonrpc':'2.0','id':rid,'result':{'sessionId':'s'}})\n"
        "    elif m == 'session/prompt':\n"
        "        time.sleep(1.3)\n"
        "        send({'jsonrpc':'2.0','method':'session/update','params':{'sessionId':'s','update':{'sessionUpdate':'agent_message_chunk','content':{'text':'progress'}}}})\n"
        "        time.sleep(2.0)\n"
        "        send({'jsonrpc':'2.0','method':'session/update','params':{'sessionId':'s','update':{'sessionUpdate':'agent_message_chunk','content':{'text':' done'}}}})\n"
        "        send({'jsonrpc':'2.0','id':rid,'result':{'sessionId':'s','stopReason':'end_turn'}})\n"
        "    elif m == 'session/cancel':\n"
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
        async with AcpConnection(agent="onidle-progress", session_id="l", proc=proc) as conn:
            await conn.initialize()
            await conn.session_new(cwd="/tmp")

            async def slow_wedged():
                await asyncio.sleep(0.7)
                return False  # claim wedged — but a progress event arrived during the await

            result = await asyncio.wait_for(
                run_prompt(conn, "go", idle_timeout=0.1, on_idle=slow_wedged),
                timeout=10,
            )
            assert result.ok, f"progress-during-hook race cancelled a live worker: err={result.error} stop={result.stop_reason!r}"
            assert "progress" in result.text and "done" in result.text, result.text
    finally:
        shim_path.unlink(missing_ok=True)


async def main() -> None:
    await case_a_worker_killed_mid_prompt()
    await case_b_pool_shutdown_drains_live_connection()
    await case_c_broken_stdio_garbage()
    await case_d_idle_timeout_zero_disables_timeout()
    await case_e_idle_timeout_clears_pending_and_reuses_connection()
    await case_f_cancelled_send_request_clears_pending()
    await case_g_handshake_timeout_on_wedged_init()
    await case_h_on_idle_keeps_busy_worker_alive()
    await case_i_on_idle_false_still_cancels()
    await case_j_handshake_retry_once()
    await case_k_on_idle_async_race_does_not_drop_result()
    await case_l_progress_event_during_async_hook_not_dropped()
    print("OK: all failure-mode tests pass")


if __name__ == "__main__":
    asyncio.run(main())
