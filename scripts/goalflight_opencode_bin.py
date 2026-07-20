"""Shared OpenCode CLI binary resolution (matches worker-verify / adapter probes)."""

from __future__ import annotations

import os
import shutil
from pathlib import Path


def resolve_opencode_binary() -> str | None:
    """Return an executable opencode path, or None when not installed."""
    found = shutil.which("opencode")
    if found:
        return found
    for candidate in (
        Path.home() / ".local/bin/opencode",
        Path.home() / ".opencode/bin/opencode",
    ):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None
