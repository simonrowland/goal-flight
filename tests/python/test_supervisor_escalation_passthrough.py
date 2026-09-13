#!/usr/bin/env python3
"""A worker's escalation must not lose a race against chatter (b-351).

Measured 2026-09-06: a worker wrote `!BLOCKED`, the journal recorded it with
wake_class "waking", and the supervisor WITHHELD it because the session had
already forwarded CHILD_DISTINCT_CAP (32) distinct envelopes in the window. The
withhold emitted a `retrieve: relay --drain` hint, nothing drained, and the
controller discovered the dead worker ~25 minutes later via an unrelated timer.

Envelope deduplication replaces the cap. Escalations still bypass suppression.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_wake_supervise as S  # noqa: E402
import goalflight_messages as M  # noqa: E402
import goalflight_terminal as T  # noqa: E402


def _event_line(event_type: str, seq: int) -> str:
    return json.dumps(
        {"kind": "event", "payload": {"type": event_type, "seq": seq},
         "stream_id": f"dispatch-{seq}"}
    )


def test_the_escalation_set_matches_its_sources_and_cannot_drift() -> None:
    """The three strings are duplicated in the supervisor (circular import).

    This is the guard that makes the duplication safe: if ATTENTION_MARKERS or
    MARKER_TO_TYPE changes, this fails rather than silently leaving an
    escalation withheld-able.
    """
    derived = {M.MARKER_TO_TYPE[k] for k in T.ATTENTION_MARKERS if k in M.MARKER_TO_TYPE}
    assert derived == set(S._ESCALATION_EVENT_TYPES), (
        f"supervisor escalation set {sorted(S._ESCALATION_EVENT_TYPES)} has drifted "
        f"from ATTENTION_MARKERS -> {sorted(derived)}"
    )


@pytest.mark.parametrize("escalation", sorted(S._ESCALATION_EVENT_TYPES))
def test_an_escalation_is_never_backlog_gated(escalation: str) -> None:
    """Not backlog-capable => it bypasses the gate, so the cap cannot hold it."""
    assert S._is_backlog_capable_line(_event_line(escalation, 1)) is False


def test_routine_traffic_is_deduplicable() -> None:
    """Routine mail can be deduplicated; unique notices must all be delivered."""
    assert S._is_backlog_capable_line(_event_line("result", 1)) is True
    assert S._is_backlog_capable_line(_event_line("notice", 1)) is True


def test_waking_types_are_not_all_dedup_exempt() -> None:
    """Pins the reasoning, so nobody 'simplifies' this to wake_class == waking.

    The registry marks nearly everything waking; that predicate is not a
    discriminator here.
    """
    waking = {t for t, r in M.EVENT_TYPE_REGISTRY.items()
              if getattr(r, "wake_class", None) == "waking"}
    assert len(waking) > 20, "if this ever becomes small, revisit the design"
    assert not waking.issubset(S._PASSTHROUGH_EVENT_TYPES), (
        "the passthrough set must stay a narrow exception, not all waking types"
    )
