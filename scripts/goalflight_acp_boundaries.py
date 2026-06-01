"""ACP adapter boundary classifications."""

from __future__ import annotations


UNRELIABLE_ESCALATION_AGENTS = frozenset(
    {
        "cursor",
        "cursor-agent",
        "grok",
        "grok-acp",
    }
)

PERMISSION_BOUNDARY_WARNING = (
    "cursor/grok auto-mode does NOT enforce a write boundary (writes are not "
    "routed through the ACP permission gate); a tool write can escape the "
    "working directory. Pair with --os-sandbox=workspace-write (writers) / "
    "read-only (review) for a hard boundary."
)


def agent_has_unreliable_escalation(agent: str | None) -> bool:
    # Normalize like capacity.normalize_agent (.strip().lower()) so "Cursor",
    # "GROK", etc. don't slip the frozenset and silently MISS the warning
    # (fail-open on a security warning is the wrong direction). The set is
    # lowercase; match against the normalized form. (grok F2 P1.)
    return str(agent or "").strip().lower() in UNRELIABLE_ESCALATION_AGENTS


def permission_boundary_warning(
    *,
    agent: str | None,
    permission_mode: str | None,
    os_sandbox_profile: str | None,
    read_only: bool = False,
    interactive: bool = False,
) -> str | None:
    """Return startup warning text for write dispatches lacking a hard boundary."""
    if not agent_has_unreliable_escalation(agent):
        return None
    if read_only:
        return None
    if os_sandbox_profile not in {None, "off"}:
        return None
    mode = str(permission_mode or "auto")
    interactive_inline = interactive and mode == "inline"
    if mode != "auto" and not interactive_inline:
        return None
    return PERMISSION_BOUNDARY_WARNING
