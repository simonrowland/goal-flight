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

Three rules carry the design:

* **Unknown usage is not usable or exhausted.** A timeout, unreadable document,
  or absent measurement proves neither state. It is retried on the next select,
  but never receives work merely because no measured seat is available.
* **Only measured headroom is selectable.** HTTP 402, auth rejection, parsed
  token absence, and an explicit wall are unusable; a numeric reading below the
  flip threshold is usable.
* **A 401 may be recoverable.** An optional local rotator (loaded from ``ext``
  if present, otherwise a no-op) may recover a failed probe before this module
  records the seat unusable, and may mark a seat exhausted the moment a dispatch
  proves it. Most installs have no rotator; a missing one must never change or
  fail a dispatch.
"""

from __future__ import annotations

import argparse
import json
import math
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
EXHAUSTED_AT_PERCENT = 98.0

STATE_PATH = Path.home() / ".goal-flight" / "grok-seat-states.json"
STATE_TTL_S = 600.0
PROBE_TIMEOUT_S = 10.0
HOST_KEY = ""  # the host ~/.grok login, which has no seat label
PROBE_STATES = {"usable", "unusable", "unknown"}
AUTH_STATES = {"valid", "invalid", "unknown"}

# Sentinels so a test can pass recover=None to disable the optional rotator
# without being confused with "use the default loader".
_UNSET = object()
_RECOVER_CACHE = _UNSET
_MARK_CACHE = _UNSET


class NoUsableSeat(RuntimeError):
    """No configured Grok login has a measured usable probe."""


def _now() -> float:
    return time.time()


def _optional_recover():
    """Return the local 401-recovery hook, or None when the rotator is absent."""
    global _RECOVER_CACHE
    if _RECOVER_CACHE is not _UNSET:
        return _RECOVER_CACHE
    try:
        from ext import grok_rotate

        hook = getattr(grok_rotate, "recover_probe", None)
    except BaseException:
        hook = None
    _RECOVER_CACHE = hook if callable(hook) else None
    return _RECOVER_CACHE


def _optional_mark_exhausted():
    """Return the local exhaustion-marker hook, or None when the rotator is absent."""
    global _MARK_CACHE
    if _MARK_CACHE is not _UNSET:
        return _MARK_CACHE
    try:
        from ext import grok_rotate

        hook = getattr(grok_rotate, "mark_exhausted", None)
    except BaseException:
        hook = None
    _MARK_CACHE = hook if callable(hook) else None
    return _MARK_CACHE


def _record_is_grok(record: dict) -> bool:
    """True when a ledger-shaped record billed a grok engine.

    Matches ``grok`` and the dispatch handles that share that engine
    (``grok-code``, ``grok-research``, ``grok-acp``) without importing the
    dispatcher -- this module must stay safe to load from a watcher.
    """
    for key in ("engine", "agent"):
        value = record.get(key)
        if isinstance(value, str) and value.split("-", 1)[0] == "grok":
            return True
    return False


def note_exhausted(
    seat: str,
    *,
    path: Path | None = None,
    marker=None,
) -> bool:
    """Tell the optional rotator this seat is starved. Never raises.

    Returns True only if a rotator accepted the mark. A missing rotator is
    the common case and must be indistinguishable from a no-op.
    """
    if not isinstance(seat, str) or not seat.strip():
        return False
    mark = _optional_mark_exhausted() if marker is None else marker
    if not callable(mark):
        return False
    try:
        if path is not None:
            mark(seat.strip(), path=path)
        else:
            mark(seat.strip())
        return True
    except BaseException:
        return False


def note_exhausted_if_proven(
    record: dict | None,
    *,
    state: str | None = None,
    path: Path | None = None,
    marker=None,
) -> bool:
    """Mark a grok seat exhausted only when a dispatch just proved it.

    Requires a terminal ``quota_exhausted`` outcome AND a non-empty
    ``effective_account`` on a grok record. Anything else -- another engine,
    an unpinned host default, a different failure -- is a no-op.
    """
    if not isinstance(record, dict):
        return False
    outcome = state or record.get("state") or record.get("terminal_state")
    if outcome != "quota_exhausted":
        return False
    if not _record_is_grok(record):
        return False
    seat = record.get("effective_account")
    if not isinstance(seat, str) or not seat.strip():
        return False
    return note_exhausted(seat, path=path, marker=marker)


class TrustRefused(ValueError):
    """A trust target that must never be registered."""


def _trust_guard(project_root: Path) -> Path:
    """Return the resolved project path, or raise if it is too broad to trust.

    Mirrors the guards in install-codex-overrides.sh. These hold even though a
    grok trust key looks like an exact folder: registering root or a home
    directory is never the intent, and if grok ever matches by prefix such an
    entry would be a standing grant over everything beneath it.
    """
    literal = Path(project_root).expanduser()
    resolved = literal.resolve()
    for candidate in (literal, resolved):
        if candidate == Path(candidate.anchor):
            raise TrustRefused(f"refusing to trust the filesystem root: {candidate}")
        if candidate == Path.home() or candidate == Path.home().resolve():
            raise TrustRefused(f"refusing to trust the home directory: {candidate}")
        # Single-segment paths under root (/tmp, /usr, /etc) are system
        # directories. Both forms are checked because resolving first would
        # smuggle them past: on macOS /tmp resolves to /private/tmp, which has
        # enough segments to look like a real project while being the same
        # system directory. The literal form is what the operator typed and is
        # the honest thing to judge.
        if len(candidate.parts) < 3:
            raise TrustRefused(f"refusing to trust a top-level system path: {candidate}")
    return resolved


def is_project_trusted(home_dir: Path, project_root: Path) -> bool:
    trust_file = Path(home_dir) / ".grok" / "trusted_folders.toml"
    try:
        text = trust_file.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False
    return f'[folders."{_trust_guard(project_root)}"]' in text


def ensure_project_trusted(home_dir: Path, project_root: Path) -> bool:
    """Register project_root as trusted in home_dir's grok config, idempotently.

    Returns True if an entry was added, False if it was already present.

    Grok refuses to operate in a directory it has not been told to trust, and it
    exits within seconds writing nothing at all -- through the dispatcher that
    looks exactly like a worker that launched and died, with an empty tail and
    no error to read. A freshly created per-account home starts with an EMPTY
    trust list, so every seat hits this in every repo until registered, and any
    worktree counts as its own directory.

    Registering the directory the operator is explicitly dispatching INTO is not
    a widening of what the worker may touch: it is the cwd they chose, and the
    only alternative is a worker that dies without saying why. The guards above
    still refuse anything broader than a specific project.
    """
    resolved = _trust_guard(project_root)
    trust_file = Path(home_dir) / ".grok" / "trusted_folders.toml"
    if is_project_trusted(home_dir, resolved):
        return False
    trust_file.parent.mkdir(parents=True, exist_ok=True)
    # The LEADING newline is load-bearing: without it a file that does not end
    # in a newline would have the new table header glued onto its last line,
    # where it parses as something else entirely. It is unconditional because a
    # spare blank line between entries is harmless while a missing one is not.
    entry = (
        f'\n[folders."{resolved}"]\n'
        f"trusted = true\ndecided_at = {int(_now())}\n"
    )
    with trust_file.open("a", encoding="utf-8") as handle:
        handle.write(entry)
    try:
        os.chmod(trust_file, 0o600)
    except OSError:
        pass
    return True


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


def _http_status(record: object) -> int | None:
    if not isinstance(record, dict):
        return None
    prefix = "billing endpoint returned HTTP "
    error = record.get("error")
    if not isinstance(error, str) or not error.startswith(prefix):
        return None
    try:
        return int(error.removeprefix(prefix))
    except ValueError:
        return None


def _record_auth_state(record: object) -> str:
    if not isinstance(record, dict):
        return "unknown"
    state = record.get("auth_state")
    if state in AUTH_STATES:
        return str(state)
    status = _http_status(record)
    if status in {401, 403}:
        return "invalid"
    if status == 402:
        return "valid"
    if record.get("error") in {
        "no grok login found",
        "grok auth document is empty",
        "grok auth document carries no session token",
    }:
        return "invalid"
    if record.get("ok") is True:
        return "valid"
    return "unknown"


def _record_probe_state(record: object) -> str:
    if not isinstance(record, dict):
        return "unknown"
    used = record.get("used_percent")
    if record.get("walled") is True or _http_status(record) in {401, 402, 403}:
        return "unusable"
    state = record.get("probe_state")
    if state in PROBE_STATES:
        if state == "usable":
            if not _valid_percentage(used):
                return "unknown"
            if float(used) >= EXHAUSTED_AT_PERCENT:
                return "unusable"
        return str(state)
    # Compatibility for an older recovery hook: success is usable only when it
    # carries the measurement needed to prove headroom. `ok:true` alone is not.
    if (
        record.get("ok") is True
        and _valid_percentage(used)
    ):
        return (
            "unusable" if float(used) >= EXHAUSTED_AT_PERCENT else "usable"
        )
    return "unknown"


def _valid_percentage(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and 0.0 <= value <= 100.0
        and math.isfinite(float(value))
    )


def states_are_fresh(document: dict | None, *, now: float | None = None) -> bool:
    if not document:
        return False
    updated_at = document.get("updated_at")
    if not isinstance(updated_at, (int, float)):
        return False
    age = (_now() if now is None else now) - float(updated_at)
    # A timestamp from the future means a clock moved; treat it as stale rather
    # than trusting it forever.
    if not (0 <= age <= STATE_TTL_S):
        return False
    seats = document.get("seats")
    if not isinstance(seats, dict):
        return False
    if any(
        not isinstance(entry, dict)
        or _record_probe_state(entry) == "unknown"
        or _record_auth_state(entry) != "valid"
        for entry in seats.values()
    ):
        # Unknown probes retry immediately; invalid auth retries too so a fresh
        # login is observed without waiting out the cache TTL. States are
        # DERIVED, not read raw: the external rotator (scripts/ext) and older
        # recovery hooks write ``ok`` + ``used_percent`` without the typed keys,
        # and a measured legacy entry is a fresh usable seat, not a stale one.
        return False
    return True


def refresh_states(
    *,
    path: Path = STATE_PATH,
    timeout_s: float = PROBE_TIMEOUT_S,
    now: float | None = None,
    reader=None,
    recover=_UNSET,
) -> dict:
    """Probe every configured grok login and cache the result.

    ``reader`` is the seam tests inject; by default this calls the bundled grok
    usage reader once per account. ``recover`` is the optional 401-recovery
    hook: omit it to load the local rotator if present, pass ``None`` to
    disable, or pass a callable to inject one.
    """
    current = _now() if now is None else now
    read_usage = grok_usage.read_usage if reader is None else reader
    recover_fn = _optional_recover() if recover is _UNSET else recover
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
        # A 401 is the kimi-style "lapsed, auto-heals" case: the login still
        # exists, the access token just expired. Recording it dead benches a
        # live seat until the next TTL and can starve the whole fleet. Only
        # a 401 is offered to the rotator; a 403/5xx/malformed body is left
        # as-is so we do not launch a recovery process per broken seat.
        if (
            recover_fn is not None
            and _http_status(record) == 401
        ):
            try:
                recovered = recover_fn(
                    label=label,
                    auth_path=auth_path,
                    probe=record,
                    reader=read_usage,
                    timeout_s=timeout_s,
                )
                if isinstance(recovered, dict):
                    record = recovered
            except BaseException:
                pass
        probe_state = _record_probe_state(record)
        auth_state = _record_auth_state(record)
        if auth_state == "invalid":
            probe_state = "unusable"
        elif auth_state == "unknown" and probe_state == "usable":
            probe_state = "unknown"
        seats[label or HOST_KEY] = {
            # `ok` remains for existing usage readers. Selection uses the typed
            # state exclusively so probe completion cannot imply usability.
            "ok": probe_state == "usable",
            "probe_state": probe_state,
            "auth_state": auth_state,
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

    Only a measured usable probe receives a rank, ordered by least-used first.
    """
    if not isinstance(entry, dict):
        return None
    # Derive, never read raw: a legacy ``ok`` + ``used_percent`` record from the
    # external rotator is a measured usable seat and must rank like one.
    if (
        _record_probe_state(entry) != "usable"
        or _record_auth_state(entry) != "valid"
    ):
        return None
    used = entry.get("used_percent")
    if not _valid_percentage(used):
        return None
    if float(used) >= EXHAUSTED_AT_PERCENT:
        return None
    return (0, float(used))


def select_seat(
    *,
    path: Path = STATE_PATH,
    now: float | None = None,
    allow_refresh: bool = True,
    refresher=None,
    exclude: set[str] | None = None,
) -> str | None:
    """Return a usable seat label, None for a usable host, or raise if unknown."""
    try:
        document = load_states(path)
        if allow_refresh and not states_are_fresh(document, now=now):
            refresh = refresh_states if refresher is None else refresher
            document = refresh(path=path, now=now)
        if not document:
            raise NoUsableSeat("no usable grok seat: probe state unavailable")

        excluded = exclude or set()
        ranked: list[tuple[tuple[int, float], str]] = []
        for key, entry in document.get("seats", {}).items():
            if key in excluded:
                continue
            rank = _rank(entry)
            if rank is not None:
                ranked.append((rank, key))
        if not ranked:
            raise NoUsableSeat("no usable grok seat")
        ranked.sort(key=lambda item: (item[0], item[1]))
        best = ranked[0][1]
        return None if best == HOST_KEY else best
    except NoUsableSeat:
        raise
    except Exception as exc:
        raise NoUsableSeat("no usable grok seat: probe failed") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Show or refresh grok seat headroom and the selected seat."
    )
    parser.add_argument("--refresh", action="store_true", help="probe now")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    document = load_states() or {}
    if args.refresh or not states_are_fresh(document):
        document = refresh_states()
    try:
        selected = select_seat(allow_refresh=False)
        selected_text = selected or "(host ~/.grok)"
    except NoUsableSeat:
        selected = None
        selected_text = "(no usable seat)"

    if args.json:
        print(json.dumps({"selected": selected, "states": document}, indent=2))
        return 0
    age = _now() - float(document.get("updated_at") or 0)
    print(f"grok seat states (age {age / 60:.0f}m, flip at {EXHAUSTED_AT_PERCENT:.0f}% used):")
    for key, entry in sorted(document.get("seats", {}).items()):
        name = key or "(host ~/.grok)"
        used = entry.get("used_percent")
        shown = "unknown" if used is None else f"{float(used):.0f}%"
        state = entry.get("probe_state")
        if state not in PROBE_STATES:
            state = "unknown"
        note = f"  [{entry.get('error')}]" if entry.get("error") else ""
        print(f"  {name:18s} used={shown:8s} state={state}{note}")
    print(f"selected: {selected_text}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
