"""Smoke test: vendored ACP client + ergonomic runner against the echo-agent fixture.

Proves end-to-end JSON-RPC over stdio works in pure Python (no external worker
CLIs, no auth, no network). Also unit-tests the marker extractor against a
synthetic worker-output sample, and the PID-identity safety check that
defends ghost cleanup against PID reuse.

Run: `python test/test_acp_pipe.py`
Expect: `OK: acp pipe + runner + markers + pidfile safety`
"""

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from acp_client import AcpConnection, AcpProcessPool, _ps_meta  # noqa: E402
from acp_pool import compute_pool_ceiling, managed_pool  # noqa: E402
from acp_runner import extract_markers, run_prompt  # noqa: E402

FIXTURE = REPO_ROOT / "test" / "fixtures" / "acp_echo_agent.py"
PIDFILE_DIR_TEST = Path("/tmp/goal-flight-acp-pids-test.d")


async def smoke_echo() -> None:
    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(FIXTURE),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=8 * 1024 * 1024,
    )
    # Exercise the async context manager — close_gracefully() runs on exit.
    async with AcpConnection(agent="echo", session_id="test-1", proc=proc, verbose=False) as conn:
        init = await conn.initialize()
        assert init["agentInfo"]["name"] == "echo-agent", f"unexpected init: {init}"
        await conn.session_new(cwd="/tmp")

        result = await run_prompt(conn, "hello", idle_timeout=10)
        assert result.ok, f"not ok: stop_reason={result.stop_reason!r} error={result.error}"
        assert result.text == "echo: hello", f"unexpected text: {result.text!r}"
        assert result.thoughts == "", f"unexpected thoughts: {result.thoughts!r}"
        assert result.tool_calls == [], f"unexpected tool_calls: {result.tool_calls}"
    # Outside the async-with: conn is closed, process should have exited.
    assert proc.returncode is not None, "echo-agent process did not exit after close_gracefully()"


def smoke_markers() -> None:
    # Codex emits unwrapped markers; verify the canonical case.
    sample = (
        "preamble noise\n"
        "STATUS: probing config\n"
        "RESULT: kind=count value=4\n"
        "RESULT: kind=file path=/tmp/x.txt\n"
        "STATUS: writing\n"
        "COMPLETE: 1 file written\n"
        "trailing noise\n"
    )
    markers = extract_markers(sample)
    assert markers.get("STATUS") == ["probing config", "writing"], markers
    assert markers.get("RESULT") == ["kind=count value=4", "kind=file path=/tmp/x.txt"], markers
    assert markers.get("COMPLETE") == ["1 file written"], markers
    assert "USER-NEED" not in markers, markers

    # Grok wraps the marker tag in markdown bold: `**STATUS:** ...`
    # The regex was DESIGNED to tolerate this; lock the behavior in.
    grok_sample = (
        "**STATUS:** investigating buggy.py\n"
        "**RESULT:** pytest_pass=true\n"
        "**COMPLETE:** test fixed in one edit\n"
    )
    grok_markers = extract_markers(grok_sample)
    assert grok_markers.get("STATUS") == ["investigating buggy.py"], grok_markers
    assert grok_markers.get("RESULT") == ["pytest_pass=true"], grok_markers
    assert grok_markers.get("COMPLETE") == ["test fixed in one edit"], grok_markers

    # Mixed: same worker emits a tagged-and-tail-emphasized line ("**STATUS:** foo **").
    # The regex strips trailing emphasis via .rstrip("* \t").
    mixed_sample = (
        "**STATUS:** doing work **\n"
        "USER-NEED: clarify the constant\n"
        "*BLOCKED:* network unreachable\n"  # single-asterisk emphasis variant
    )
    mixed = extract_markers(mixed_sample)
    assert mixed.get("STATUS") == ["doing work"], mixed
    assert mixed.get("USER-NEED") == ["clarify the constant"], mixed
    assert mixed.get("BLOCKED") == ["network unreachable"], mixed


def smoke_pidfile_safety() -> None:
    """Three cases against the per-controller pidfile-dir scheme:
      A) pidfile entry (from a dead "controller") whose worker started_at/cmd
         no longer match the live PID -> SKIPPED (PID-reuse defense)
      B) pidfile entry whose worker identity DOES match -> KILLED (orphan reaping)
      C) pidfile from a LIVE controller (other goal-flight run) -> SKIPPED entirely
         (concurrent-controller defense; we never touch another live run's workers)

    "Dead controller" simulated via a definitely-dead PID (sys.maxsize) as the
    pidfile name; "live controller" simulated via PID 1 (init/launchd).
    """
    import shutil
    import acp_client  # for module-level _PIDFILE_DIR monkey-patch
    DEAD_CONTROLLER_PID = sys.maxsize  # impossible to be a live PID

    # 0.3.4: _pidfile_dir is no longer a pool attribute — Design 2's
    # module-level _PIDFILE_DIR is the single source of truth. Monkey-patch
    # it for test isolation; restore in the outer try/finally below.
    _orig_pidfile_dir = acp_client._PIDFILE_DIR
    acp_client._PIDFILE_DIR = PIDFILE_DIR_TEST

    def _setup_pool() -> AcpProcessPool:
        PIDFILE_DIR_TEST.mkdir(parents=True, exist_ok=True)
        return AcpProcessPool(agents_config={})

    # Case A: stale entry, bystander must survive.
    bystander_a = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        time.sleep(0.5)
        assert _ps_meta(bystander_a.pid) is not None, "case-A bystander died early"
        stale_entry = {
            "pid": bystander_a.pid,
            "pgid": bystander_a.pid,
            "started_at": "Sun Jan  1 00:00:00 2000",  # obviously wrong
            "cmd": "ghost-from-the-past",
            "agent": "fake",
            "session_id": "stale",
        }
        pool = _setup_pool()
        (PIDFILE_DIR_TEST / f"{DEAD_CONTROLLER_PID}.jsonl").write_text(json.dumps(stale_entry) + "\n")
        killed = pool.cleanup_ghosts()
        assert killed == 0, f"case A: cleanup killed {killed} (expected 0)"
        assert bystander_a.poll() is None, "case A: bystander was killed! safety check failed!"
    finally:
        try:
            bystander_a.terminate(); bystander_a.wait(timeout=2)
        except Exception:
            try: bystander_a.kill()
            except Exception: pass
        shutil.rmtree(PIDFILE_DIR_TEST, ignore_errors=True)

    # Case B: matching entry, bystander must be killed.
    bystander_b = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        time.sleep(0.5)
        live = _ps_meta(bystander_b.pid)
        assert live is not None, "case-B bystander died early"
        live_lstart, live_comm = live
        valid_entry = {
            "pid": bystander_b.pid,
            "pgid": bystander_b.pid,  # start_new_session=True makes pid == pgid (session leader)
            "started_at": live_lstart,
            "cmd": live_comm,
            "agent": "test",
            "session_id": "valid-orphan",
        }
        pool = _setup_pool()
        (PIDFILE_DIR_TEST / f"{DEAD_CONTROLLER_PID}.jsonl").write_text(json.dumps(valid_entry) + "\n")
        killed = pool.cleanup_ghosts()
        assert killed == 1, f"case B: cleanup killed {killed} (expected 1)"
        time.sleep(0.5)
        assert bystander_b.poll() is not None, "case B: bystander not killed — cleanup happy path broken"
    finally:
        try:
            bystander_b.terminate(); bystander_b.wait(timeout=2)
        except Exception:
            try: bystander_b.kill()
            except Exception: pass
        shutil.rmtree(PIDFILE_DIR_TEST, ignore_errors=True)

    # Case C: pidfile from a LIVE controller (another goal-flight run) — must be
    # skipped entirely. Even if the worker identity matches, don't touch another
    # live run's workers.
    bystander_c = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        time.sleep(0.5)
        live = _ps_meta(bystander_c.pid)
        assert live is not None, "case-C bystander died early"
        live_lstart, live_comm = live
        entry = {
            "pid": bystander_c.pid,
            "pgid": bystander_c.pid,
            "started_at": live_lstart,
            "cmd": live_comm,
            "agent": "test",
            "session_id": "live-controller-protect",
        }
        pool = _setup_pool()
        LIVE_OTHER_PID = 1  # init/launchd — always alive on macOS/Linux
        (PIDFILE_DIR_TEST / f"{LIVE_OTHER_PID}.jsonl").write_text(json.dumps(entry) + "\n")
        killed = pool.cleanup_ghosts()
        assert killed == 0, f"case C: cleanup killed {killed} (expected 0 — live controller's workers must not be touched)"
        assert bystander_c.poll() is None, "case C: bystander was killed! concurrent-controller defense failed!"
        assert (PIDFILE_DIR_TEST / f"{LIVE_OTHER_PID}.jsonl").exists(), "case C: live controller's pidfile was consumed!"
    finally:
        try:
            bystander_c.terminate(); bystander_c.wait(timeout=2)
        except Exception:
            try: bystander_c.kill()
            except Exception: pass
        shutil.rmtree(PIDFILE_DIR_TEST, ignore_errors=True)
        # 0.3.4: restore module-level _PIDFILE_DIR so later tests
        # (smoke_bare_connection_registry) see the production path.
        acp_client._PIDFILE_DIR = _orig_pidfile_dir


def smoke_pool_ceiling() -> None:
    """Verify compute_pool_ceiling uses the operational capacity profile.

    Post-0.4.0-prep refactor: compute_pool_ceiling delegates to
    goalflight_capacity.profile() when available, which uses a tiered
    operating-cap scheme:
      ≤8GB=1, ≤16GB=3, ≤32GB=4, ≤64GB=6, >64GB=16 (override-able).
    The >64GB tier bumped from 8→16 in the rate-limit-cap update to give
    multi-session parallel work headroom; per-agent caps grew to 10 for
    codex/grok, so the prior tier-8 cap was the binding constraint.
    Raw RAM ceiling is clamped to operating cap.
    """
    cases = [
        # (ram_mb, expected_ceiling) — tiered op-cap scheme via goalflight_capacity.profile()
        (8192,  1),    # 8 GB → op-cap 1
        (16384, 3),    # 16 GB → op-cap 3
        (32768, 4),    # 32 GB → op-cap 4
        (131072, 16),  # 128 GB → op-cap 16 (was 8 pre-rate-limit-cap update)
        (1024, 1),     # 1 GB → floors to 1
    ]
    test_path = Path("/tmp/goal-flight-env-caveats-test.md")
    try:
        for ram_mb, expected in cases:
            content = f"# test fixture\n- RAM: {ram_mb/1024:.1f} GB ({ram_mb} MB total)\n"
            test_path.write_text(content)
            got = compute_pool_ceiling(test_path)
            assert got == expected, f"ram_mb={ram_mb}: expected {expected}, got {got}"

        # Missing file → falls back to conservative ceiling (4).
        # Rationale: hard_cap=20 would spawn ~24 GB worst-case (cursor mix) on
        # an unknown box; 4 is safe on most laptops including small Macs.
        test_path.unlink()
        got = compute_pool_ceiling(test_path)
        assert got == 4, f"missing file: expected 4 (conservative fallback), got {got}"

        # Malformed file → also conservative fallback
        test_path.write_text("garbage content with no RAM line\n")
        got = compute_pool_ceiling(test_path)
        assert got == 4, f"malformed file: expected 4 (conservative fallback), got {got}"
    finally:
        test_path.unlink(missing_ok=True)


async def smoke_managed_pool() -> None:
    """Verify managed_pool() async context manager spawns + cleans up cleanly.

    Uses the echo-agent fixture (no real worker CLI needed).
    """
    fixture_str = str(FIXTURE)
    agents_config = {
        "echo": {
            "command": sys.executable,
            "acp_args": [fixture_str],
            "working_dir": "/tmp",
        },
    }
    # Don't install signal handlers in tests — main thread re-entrancy gets messy.
    async with managed_pool(agents_config, install_signal_handlers=False) as pool:
        conn = await pool.get_or_create("echo", "managed-test", cwd="/tmp")
        # echo-agent doesn't have a ping method and returns method-not-found,
        # which AcpConnection.ping() treats as "still alive" — verify.
        assert await conn.ping(timeout=3), "managed_pool: echo conn not alive after get_or_create"
        assert pool.stats["total"] == 1, pool.stats
        captured_proc = conn.proc
    # After context exit: pool drained, conn killed
    assert captured_proc.returncode is not None, "managed_pool: conn proc not killed on context exit"


async def smoke_idle_cleanup() -> None:
    """managed_pool's background idle-cleanup task should reap connections
    whose last_active is older than the configured TTL.

    Defends against the rate-limit-retry RAM-burn scenario: long runs that
    spawn distinct session_ids leave idle workers behind without active
    culling. Especially load-bearing for claude-class workers (~614 MB RSS).
    """
    fixture_str = str(FIXTURE)
    agents_config = {
        "echo": {
            "command": sys.executable,
            "acp_args": [fixture_str],
            "working_dir": "/tmp",
        },
    }
    async with managed_pool(
        agents_config,
        install_signal_handlers=False,
        idle_cleanup_ttl_seconds=1.0,        # tiny TTL for test
        idle_cleanup_interval_seconds=0.5,   # rapid scan
    ) as pool:
        conn = await pool.get_or_create("echo", "idle-test", cwd="/tmp")
        captured_proc = conn.proc
        # Force last_active into the past so the cleanup loop trips on the next scan
        conn.last_active = time.time() - 5.0
        # Wait long enough for the background task to scan + cull
        await asyncio.sleep(1.5)
        assert captured_proc.returncode is not None, (
            "idle-cleanup: connection should have been reaped after TTL"
        )
        assert pool.stats["total"] == 0, (
            f"idle-cleanup: pool still reports {pool.stats['total']} active "
            "after cleanup_idle should have run"
        )


def smoke_scope_leak_audit() -> None:
    """Design 1 — scope-leak audit (`acp_runner._scan_out_of_scope_paths`).

    Verifies the post-hoc tool_call locations scan: ACP `ToolCall` /
    `ToolCallUpdate` updates may include a `locations: [{path, line?}]`
    array; any path resolving outside the connection's cwd should land in
    `PromptResult.out_of_scope_writes` as an audit signal.

    Four sub-cases:
      a) all paths inside cwd → empty
      b) some paths outside cwd → flagged
      c) relative paths resolve against connection cwd (not caller cwd)
      d) empty cwd / None disables checking
    """
    import tempfile
    from acp_runner import _scan_out_of_scope_paths  # noqa: E402

    with tempfile.TemporaryDirectory() as td:
        cwd = Path(td).resolve()
        (cwd / "in_scope.py").write_text("# fixture")
        (cwd / "subdir").mkdir()
        (cwd / "subdir" / "nested.py").write_text("# fixture")

        # (a) all paths inside cwd
        in_scope_calls = [
            {"locations": [{"path": str(cwd / "in_scope.py")}]},
            {"locations": [{"path": str(cwd / "subdir" / "nested.py"), "line": 1}]},
        ]
        got = _scan_out_of_scope_paths(in_scope_calls, str(cwd))
        assert got == [], f"in-scope only: expected [], got {got}"

        # (b) some paths outside cwd → flagged, dedupe + source-order
        leaky_calls = [
            {"locations": [{"path": str(cwd / "in_scope.py")}]},
            {"locations": [{"path": "/etc/passwd"}]},
            {"locations": [{"path": "/tmp/leaked.txt"}]},
            {"locations": [{"path": "/etc/passwd"}]},  # duplicate, must be dedup'd
        ]
        got = _scan_out_of_scope_paths(leaky_calls, str(cwd))
        assert got == ["/etc/passwd", "/tmp/leaked.txt"], f"leak case: expected 2 unique paths, got {got}"

        # (c) relative paths resolve against connection cwd (NOT caller cwd).
        # Caller (this test process) might be running from elsewhere; the
        # worker's relative path "in_scope.py" should be interpreted relative
        # to the connection's recorded cwd.
        relative_calls = [
            {"locations": [{"path": "in_scope.py"}]},
            {"locations": [{"path": "subdir/nested.py"}]},
            {"locations": [{"path": "../out_of_scope.py"}]},  # outside cwd
        ]
        got = _scan_out_of_scope_paths(relative_calls, str(cwd))
        assert got == ["../out_of_scope.py"], f"relative resolution: expected 1 out-of-scope, got {got}"

        # (d) empty/None cwd disables checking entirely (would otherwise
        # spuriously match the test process's own cwd via Path("").resolve())
        assert _scan_out_of_scope_paths(leaky_calls, "") == [], "empty cwd must disable"
        assert _scan_out_of_scope_paths(leaky_calls, None) == [], "None cwd must disable"  # type: ignore[arg-type]

        # (e) malformed tool_call entries don't crash the scanner
        malformed = [
            {},                                        # no locations key
            {"locations": []},                         # empty array
            {"locations": [{}]},                       # location without path
            {"locations": [{"path": ""}]},             # empty path
            {"locations": [{"path": str(cwd / "in_scope.py")}]},  # one valid in-scope
        ]
        got = _scan_out_of_scope_paths(malformed, str(cwd))
        assert got == [], f"malformed inputs: expected [], got {got}"


async def smoke_bare_connection_registry() -> None:
    """Design 2 — module-level connection registry covers the bare-AcpConnection
    orphan-defense path (no pool, just `async with AcpConnection(...) as conn`).

    Verifies:
      a) On bare-conn spawn, a pidfile is written at
         /tmp/goal-flight-acp-pids.d/<controller-pid>.jsonl containing the
         spawned process's identity (pid/pgid/started_at/cmd/agent/session_id).
      b) On AcpConnection.kill() (via async-with exit), the entry is removed.
      c) The bare-conn path is what `cleanup_ghosts()` would reap on next
         controller startup if the controller crashed before kill ran.
    """
    import acp_client  # for the module-level registry inspection
    pidfile = acp_client._PIDFILE_DIR / f"{os.getpid()}.jsonl"

    # Pre-condition: no leftover from prior runs in our own pidfile
    pidfile.unlink(missing_ok=True)

    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(FIXTURE),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=8 * 1024 * 1024,
    )
    spawned_pid = proc.pid

    # Spawn a bare connection; __post_init__ should call _register_connection
    async with AcpConnection(agent="echo", session_id="bare-registry-test", proc=proc, verbose=False) as conn:
        await conn.initialize()
        await conn.session_new(cwd="/tmp")
        # (a) registry shows our connection
        assert spawned_pid in acp_client._live_connections, (
            f"bare conn missing from registry: have {list(acp_client._live_connections)}"
        )
        # (b) pidfile written; contains the spawned process identity
        assert pidfile.exists(), f"bare-conn pidfile not written at {pidfile}"
        contents = pidfile.read_text().splitlines()
        assert len(contents) >= 1, f"expected ≥1 entry in pidfile, got {contents!r}"
        entry = json.loads(contents[0])
        assert entry["pid"] == spawned_pid, f"pidfile entry pid={entry['pid']} != spawned {spawned_pid}"
        assert entry["agent"] == "echo", f"agent mismatch: {entry!r}"
        assert entry["session_id"] == "bare-registry-test", f"session_id mismatch: {entry!r}"
        # Send a prompt so the connection is exercised end-to-end
        result = await run_prompt(conn, "hello", idle_timeout=10)
        assert result.ok, f"bare-conn smoke prompt failed: {result.error!r}"
        # out_of_scope_writes empty since echo agent emits no tool_calls
        assert result.out_of_scope_writes == [], f"unexpected leaks: {result.out_of_scope_writes!r}"

    # (c) post-async-with-exit: kill() → _unregister_connection → pidfile cleaned
    assert spawned_pid not in acp_client._live_connections, (
        "registry not cleaned after async-with exit"
    )
    # When the registry is empty for this controller, pidfile should be unlinked
    # (the _write_through_pidfile_locked branch that removes empty files).
    # If other parallel test runs ARE registering against the same controller
    # pid (unlikely in our single-process test), this could fail — log instead
    # of hard-assert to avoid flakiness.
    if pidfile.exists():
        remaining = pidfile.read_text().splitlines()
        assert spawned_pid not in [json.loads(L).get("pid") for L in remaining if L], (
            f"pidfile still references killed conn {spawned_pid}: {remaining!r}"
        )

    # Defensive cleanup in case the test errored partway
    pidfile.unlink(missing_ok=True)


async def main() -> None:
    await smoke_echo()
    smoke_markers()
    smoke_pidfile_safety()
    smoke_pool_ceiling()
    smoke_scope_leak_audit()
    await smoke_bare_connection_registry()
    await smoke_managed_pool()
    await smoke_idle_cleanup()
    print("OK: acp pipe + runner + markers + pidfile safety + pool ceiling + scope-leak audit + bare-conn registry + managed pool + idle cleanup")


if __name__ == "__main__":
    asyncio.run(main())
