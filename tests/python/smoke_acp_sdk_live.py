#!/usr/bin/env python3
"""Live ACP SDK smoke against codex-acp.

Not part of the hermetic gate. Run with:
  ~/.goal-flight/venvs/acp-0.10/bin/python tests/python/smoke_acp_sdk_live.py
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from acp_runner import extract_markers, run_prompt  # noqa: E402
from goalflight_acp_client import spawn_acp_connection  # noqa: E402


async def amain() -> int:
    binary = shutil.which("codex-acp")
    if not binary:
        print("SKIP: codex-acp not on PATH")
        return 0
    conn = await spawn_acp_connection(
        binary,
        [],
        agent="codex-acp",
        session_id="live-smoke",
        cwd=str(ROOT),
    )
    try:
        await conn.initialize(timeout=60)
        await conn.new_session(str(ROOT), timeout=60)
        result = await run_prompt(
            conn,
            "Reply with exactly: COMPLETE: live smoke",
            idle_timeout=300,
        )
        markers = extract_markers(result.text)
        print({"ok": result.ok, "stop_reason": result.stop_reason, "markers": markers})
        return 0 if result.ok or markers.get("COMPLETE") else 1
    finally:
        await conn.close_gracefully()


def main() -> int:
    return asyncio.run(amain())


if __name__ == "__main__":
    raise SystemExit(main())
