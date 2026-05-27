"""End-to-end real-world dispatch via the new `[acp]` path.

Proves the full pipeline:
  managed_pool → get_or_create codex-acp → run_prompt → extract_markers →
  verify deliverable on disk

Task: write scripts/acp_quickstart.md (5-section bounded format).
Worker: codex-acp (sub-billed via OpenAI Pro device-auth).
Marker: `COMPLETE: quickstart written` expected as the final-line emit.

This is what `commands/execute.md` step 2b's `[acp]` branch will look like
when wired into per-chunk loop dispatch.
"""

import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from acp_pool import managed_pool  # noqa: E402
from acp_runner import extract_markers, run_prompt  # noqa: E402

DELIVERABLE = REPO_ROOT / "scripts" / "acp_quickstart.md"

DISPATCH_PROMPT = (
    "Create the file scripts/acp_quickstart.md containing a 5-section "
    "getting-started guide for the goal-flight ACP layer:\n\n"
    "  (1) Install ACP adapters (codex-acp, claude-code-cli-acp, etc.)\n"
    "  (2) Spawn a connection via managed_pool\n"
    "  (3) Send a prompt with run_prompt\n"
    "  (4) Extract markers from the result text\n"
    "  (5) Cleanup happens automatically on context exit\n\n"
    "Each section should be 3-5 lines max with a Python code example. "
    "Total file under 80 lines. Then on the LAST line of your reply emit "
    "exactly: COMPLETE: quickstart written"
)


async def main() -> int:
    agents_config = {
        "codex": {
            "command": "codex-acp",
            "acp_args": [],
            "working_dir": str(REPO_ROOT),
        },
    }
    # Remove any prior deliverable so we know the worker wrote it this run
    DELIVERABLE.unlink(missing_ok=True)

    env_caveats = REPO_ROOT / "docs-private" / "env-caveats.md"
    async with managed_pool(
        agents_config,
        env_caveats_path=env_caveats,
        install_signal_handlers=False,
    ) as pool:
        conn = await pool.get_or_create("codex", "e2e", cwd=str(REPO_ROOT))
        print(f"[+] spawned codex-acp via pool (pgid={conn.proc.pid})", flush=True)

        result = await run_prompt(conn, DISPATCH_PROMPT, idle_timeout=180)
        print(f"[+] stop_reason={result.stop_reason!r} error={result.error}", flush=True)
        print(f"[+] text ({len(result.text)} chars): {result.text[:200]!r}{'...' if len(result.text) > 200 else ''}", flush=True)
        print(f"[+] tool_calls: {len(result.tool_calls)}", flush=True)

        markers = extract_markers(result.text)
        print(f"[+] markers: {markers}", flush=True)

    # Verify deliverable
    if not DELIVERABLE.exists():
        print(f"[!] FAIL: deliverable {DELIVERABLE} was not created")
        return 1
    content = DELIVERABLE.read_text()
    lines = content.count("\n")
    print(f"[+] deliverable: {DELIVERABLE.relative_to(REPO_ROOT)} ({lines} lines, {len(content)} bytes)")
    has_complete = "COMPLETE" in markers and any("quickstart" in v.lower() for v in markers["COMPLETE"])
    print(f"[+] COMPLETE marker emitted: {has_complete}")
    if not result.ok:
        return 1
    if not has_complete:
        print("[!] WARNING: COMPLETE marker not found in extracted markers (worker may have skipped it)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
