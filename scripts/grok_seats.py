#!/usr/bin/env python3
"""Pick a grok login that still has headroom, the way codex picks a seat.

Codex has a daemon that probes every seat, publishes ``codex-seat-states.json``
and an active-seat pointer, and lets ``codex_seat_lib.resolve_codex_seat`` fall
through explicit account -> per-repo table -> pointer -> host. Grok had none of
that: every unpinned grok dispatch ran on the host ``~/.grok`` no matter how
exhausted it was, and a second account could only be reached by pinning
``--account`` by hand.

This module supplies the missing piece with the same shape but no new daemon.
State is cached in ``grok-seat-states.json`` and refreshed inline when stale, so
the common path is a file read and a stale cache costs one probe round rather
than a background service.

Two rules carry the design:

* **A selection failure must never fail a dispatch.** Every error path returns
  None, which means "no opinion, use the host default" -- exactly what happened
  before this module existed. Grok headroom is an optimisation; losing it must
  not cost a worker.
* **Unknown usage is not zero and not exhausted.** The billing endpoint omits
  ``creditUsagePercent`` for an account whose period just opened, so a seat can
  be genuinely unmeasurable. Such a seat stays ELIGIBLE (there is no evidence it
  is starved) but ranks behind any seat measured to have headroom, because a
  measured number is better evidence than an absence.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import grok_usage  # noqa: E402

# A seat at or above this much of its credit spent is "starved" and selection
# flips away from it. Below 100 on purpose: a seat that is exactly empty has
# already begun failing dispatches, so the flip has to happen while there is
# still usable headroom left to flip with.
EXHAUSTED_AT_PERCENT = 95.0

STATE_PATH = Path.home() / ".goal-flight" / "grok-seat-states.json"
STATE_TTL_S = 600.0
PROBE_TIMEOUT_S = 10.0
HOST_KEY = ""  # the host ~/.grok login, which has no seat label


def _now() -> float:
    return time.time()


def load_states(path: Path = STATE_PATH) -> dict | None:
    """Return the cached probe document, or None if absent/unusable."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(document, dict) or document.get("version") != 1:
        return None
    if not isinstance(document.get("seats"), dict):
        return None
    return document


def states_are_fresh(document: dict | None, *, now: float | None = None) -> bool:
    if not document:
        return False
    updated_at = document.get("updated_at")
    if not isinstance(updated_at, (int, float)):
        return False
    age = (_now() if now is None else now) - float(updated_at)
    # A timestamp from the future means a clock moved; treat it as stale rather
    # than trusting it forever.
    return 0 <= age <= STATE_TTL_S


def refresh_states(
    *,
    path: Path = STATE_PATH,
    timeout_s: float = PROBE_TIMEOUT_S,
    now: float | None = None,
    reader=None,
) -> dict:
    """Probe every configured grok login and cache the result.

    ``reader`` is the seam tests inject; by default this calls the bundled grok
    usage reader once per account.
    """
    current = _now() if now is None else now
    read_usage = grok_usage.read_usage if reader is None else reader
    seats: dict[str, dict] = {}
    for label, auth_path in grok_usage.accounts():
        try:
            record = read_usage(
                auth_path=auth_path, timeout_s=timeout_s, account=label
            )
        except Exception:
            # A reader that raises is a reader we cannot use; record it as
            # unmeasurable rather than dropping the account silently.
            record = {"ok": False, "error": "reader raised"}
        seats[label or HOST_KEY] = {
            "ok": bool(record.get("ok")),
            "used_percent": record.get("used_percent"),
            "error": record.get("error"),
        }
    document = {"version": 1, "updated_at": current, "seats": seats}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError:
        # An uncacheable probe is still a usable probe for this call.
        pass
    return document


def _rank(entry: object) -> tuple[int, float] | None:
    """Sort key for one seat, or None when the seat is not eligible.

    Rank 0 = measured with headroom, ordered by least-used first.
    Rank 1 = unmeasurable, so eligible but ranked behind any measured seat.
    """
    if not isinstance(entry, dict):
        return None
    used = entry.get("used_percent")
    if isinstance(used, bool) or not isinstance(used, (int, float)):
        # Unknown usage. Not evidence of exhaustion, so still eligible -- but a
        # reader that outright FAILED is not eligible, because we do not even
        # know the login works.
        return (1, 0.0) if entry.get("ok") else None
    if float(used) >= EXHAUSTED_AT_PERCENT:
        return None
    return (0, float(used))


def select_seat(
    *,
    path: Path = STATE_PATH,
    now: float | None = None,
    allow_refresh: bool = True,
    refresher=None,
) -> str | None:
    """Return the grok seat label to bill, or None to use the host default.

    None is returned both when the host is the best choice and when no decision
    could be made -- they are the same action, and conflating them keeps every
    failure path on the pre-existing behaviour.
    """
    try:
        document = load_states(path)
        if allow_refresh and not states_are_fresh(document, now=now):
            refresh = refresh_states if refresher is None else refresher
            document = refresh(path=path, now=now)
        if not document:
            return None

        ranked: list[tuple[tuple[int, float], str]] = []
        for key, entry in document.get("seats", {}).items():
            rank = _rank(entry)
            if rank is not None:
                ranked.append((rank, key))
        if not ranked:
            # Everything is starved or unusable. Returning None keeps the host
            # default, which fails loudly on its own rather than this module
            # inventing a seat that cannot serve either.
            return None
        ranked.sort(key=lambda item: (item[0], item[1]))
        best = ranked[0][1]
        return None if best == HOST_KEY else best
    except Exception:
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Show or refresh grok seat headroom and the selected seat."
    )
    parser.add_argument("--refresh", action="store_true", help="probe now")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    document = refresh_states() if args.refresh else (load_states() or {})
    if not document:
        document = refresh_states()
    selected = select_seat(allow_refresh=False)

    if args.json:
        print(json.dumps({"selected": selected, "states": document}, indent=2))
        return 0
    age = _now() - float(document.get("updated_at") or 0)
    print(f"grok seat states (age {age / 60:.0f}m, flip at {EXHAUSTED_AT_PERCENT:.0f}% used):")
    for key, entry in sorted(document.get("seats", {}).items()):
        name = key or "(host ~/.grok)"
        used = entry.get("used_percent")
        shown = "unknown" if used is None else f"{float(used):.0f}%"
        eligible = "eligible" if _rank(entry) is not None else "STARVED"
        note = "" if entry.get("ok") else f"  [{entry.get('error')}]"
        print(f"  {name:18s} used={shown:8s} {eligible}{note}")
    print(f"selected: {selected or '(host ~/.grok)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
