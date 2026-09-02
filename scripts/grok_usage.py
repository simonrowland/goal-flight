#!/usr/bin/env python3
"""Report grok subscription credit headroom.

This bundled reader reads the grok CLI's existing OIDC session token and uses it
as a bearer credential. The token is never printed, logged, or included in an
emitted row. The billing endpoint is undocumented and internal to the CLI, so a
payload shape change must report ``ok: false`` rather than a healthy row.

The grok CLI has no usage subcommand; its ``/usage`` slash command is TUI-only.
The numbers behind that view come from an authenticated JSON endpoint, which is
what this reader calls directly.

This endpoint is undocumented and internal to the CLI, so it can change or
disappear without notice. Failure modes - missing auth, HTTP error, unparseable
body, a re-typed field - report ``ok: false`` with a reason. None of them may
render as a healthy row or as 0% remaining: a shape change means "could not
measure", never "measured, and it is bad".

One case is deliberately NOT a failure. When ``creditUsagePercent`` is simply
ABSENT the account is still measurable: observed 2026-08-12 on a newly created
account whose billing period had just opened, where the endpoint omits the key
rather than sending 0, and every other field parses. That record stays ``ok``
with ``used_percent`` None, so headroom reads "unknown" while the prepaid
balance and reset date it DID report still reach the operator. A key that is
present but re-typed remains a failure - absent and re-typed are different
events and are reported differently.

Several logins are reported, not one: the host ``~/.grok`` plus every
``~/.goal-flight/accounts/<seat>/.grok``. See ``accounts()``.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import math
import os
from pathlib import Path
import urllib.error
import urllib.request

BILLING_URL = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"
AUTH_PATH = Path(os.environ.get("GROK_HOME", Path.home() / ".grok")) / "auth.json"
ACCOUNTS_DIR = Path.home() / ".goal-flight" / "accounts"
DEFAULT_TIMEOUT_S = 15.0
LABEL = "grok"
PROBE_USABLE = "usable"
PROBE_UNUSABLE = "unusable"
PROBE_UNKNOWN = "unknown"
AUTH_VALID = "valid"
AUTH_INVALID = "invalid"
AUTH_UNKNOWN = "unknown"


def accounts() -> list[tuple[str | None, Path]]:
    """Every grok login to report, as (account label, auth path).

    The host login at ``~/.grok`` is labelled None so its row keeps rendering as
    plain ``grok``; named seats render as ``grok <label>``.

    Discovery globs the seat directories on purpose. Codex seats are governed by
    a registry and MUST NOT be globbed, but grok has no registry: the dispatcher
    resolves ``--account <name>`` straight to a per-account HOME and consults
    nothing else, so that directory IS the authority for grok. Reporting from the
    same substrate the dispatcher bills to keeps the row and the launch in
    agreement.

    The path has TWO levels and both matter. ``_account_home`` in the dispatcher
    builds ``accounts/<name>/<engine>`` and hands that to the worker as ``HOME``;
    grok then keeps its credentials in ``$HOME/.grok``. So a seat's auth lives at
    ``accounts/<name>/grok/.grok/auth.json`` -- the ``grok`` level is the HOME and
    the ``.grok`` level is grok's own directory inside it. Logging in with
    ``HOME`` set to the account directory instead puts auth one level too high,
    where the dispatcher refuses it as "not configured".

    An explicit ``GROK_HOME`` is an operator override: honour it alone, so this
    still reports exactly one account when someone points it at one.
    """
    if os.environ.get("GROK_HOME"):
        return [(None, AUTH_PATH)]
    found: list[tuple[str | None, Path]] = [(None, Path.home() / ".grok" / "auth.json")]
    try:
        seat_dirs = sorted(p for p in ACCOUNTS_DIR.iterdir() if p.is_dir())
    except OSError:
        seat_dirs = []
    for seat in seat_dirs:
        auth = seat / "grok" / ".grok" / "auth.json"
        if auth.is_file():
            found.append((seat.name, auth))
    return found


class GrokUsageError(RuntimeError):
    """A safe-to-report failure. The message never contains credential bytes."""

    def __init__(
        self,
        message: str,
        *,
        probe_state: str = PROBE_UNKNOWN,
        auth_state: str = AUTH_UNKNOWN,
    ) -> None:
        super().__init__(message)
        self.probe_state = probe_state
        self.auth_state = auth_state


def _balance(value: object) -> float | None:
    """Return a money field's amount, or None when it cannot be measured.

    The payload wraps amounts as ``{"val": <number>}``. None means "unknown",
    which downstream must treat as "cannot decide" -- never as zero. Collapsing
    an unreadable field to 0 would turn a shape change into a confident "no
    balance left" and wall a working seat.
    """
    if not isinstance(value, dict):
        return None
    amount = value.get("val")
    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
        return None
    return float(amount)


def _product_usage(value: object) -> dict[str, float] | None:
    """Return {product: usagePercent}, or None if the shape is not as expected.

    Per-product percentages are what distinguish "this account is spent" from
    "the lane I dispatch to is spent" -- on 2026-08-11 GrokBuild was at 95%
    while Voice and Chat were at 3% and 2%.
    """
    if not isinstance(value, list):
        return None
    out: dict[str, float] = {}
    for item in value:
        if not isinstance(item, dict):
            continue
        name = item.get("product")
        percent = item.get("usagePercent")
        if (
            isinstance(name, str)
            and not isinstance(percent, bool)
            and isinstance(percent, (int, float))
        ):
            out[name] = float(percent)
    return out or None


def _session_token(auth_path: Path) -> str:
    """Return the bearer token from the CLI's auth document.

    The document is keyed by issuer::principal, so read the single entry rather
    than hard-coding a key that differs per account.
    """
    try:
        document = json.loads(auth_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise GrokUsageError(
            "no grok login found",
            probe_state=PROBE_UNUSABLE,
            auth_state=AUTH_INVALID,
        ) from exc
    except (OSError, ValueError) as exc:
        raise GrokUsageError(
            "grok auth document is unreadable",
            probe_state=PROBE_UNKNOWN,
            auth_state=AUTH_UNKNOWN,
        ) from exc

    if not isinstance(document, dict):
        raise GrokUsageError(
            "grok auth document is malformed",
            probe_state=PROBE_UNKNOWN,
            auth_state=AUTH_UNKNOWN,
        )
    if not document:
        raise GrokUsageError(
            "grok auth document is empty",
            probe_state=PROBE_UNUSABLE,
            auth_state=AUTH_INVALID,
        )
    entry = next(iter(document.values()))
    token = entry.get("key") if isinstance(entry, dict) else None
    if not isinstance(token, str) or not token.strip():
        raise GrokUsageError(
            "grok auth document carries no session token",
            probe_state=PROBE_UNUSABLE,
            auth_state=AUTH_INVALID,
        )
    return token.strip()


def _fetch(token: str, *, url: str, timeout_s: float) -> dict:
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        # Report the status only. A response body from an auth-bearing request
        # is not safe to echo.
        status = int(exc.code)
        raise GrokUsageError(
            f"billing endpoint returned HTTP {status}",
            probe_state=(
                PROBE_UNUSABLE if status in {401, 402, 403} else PROBE_UNKNOWN
            ),
            auth_state=(AUTH_INVALID if status in {401, 403} else AUTH_VALID),
        ) from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise GrokUsageError(
            "billing endpoint unreachable",
            probe_state=PROBE_UNKNOWN,
            auth_state=AUTH_VALID,
        ) from exc
    except UnicodeError as exc:
        raise GrokUsageError(
            "billing response was not valid UTF-8",
            probe_state=PROBE_UNKNOWN,
            auth_state=AUTH_VALID,
        ) from exc

    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise GrokUsageError(
            "billing response was not valid JSON",
            probe_state=PROBE_UNKNOWN,
            auth_state=AUTH_VALID,
        ) from exc
    if not isinstance(payload, dict):
        raise GrokUsageError(
            "billing response was not an object",
            probe_state=PROBE_UNKNOWN,
            auth_state=AUTH_VALID,
        )
    return payload


def _epoch(value: object) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def read_usage(
    *,
    auth_path: Path | None = None,
    url: str = BILLING_URL,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    fetcher=None,
    account: str | None = None,
) -> dict:
    """Return one normalized record. Never raises for an expected failure.

    ``account`` is the seat label this record describes (None = the host login).
    It rides on every return path, including the failures: a row that cannot say
    WHICH login it failed for is not actionable when several are configured.
    """
    auth_path = AUTH_PATH if auth_path is None else auth_path
    try:
        payload = (
            fetcher(url, timeout_s)
            if fetcher is not None
            else _fetch(_session_token(auth_path), url=url, timeout_s=timeout_s)
        )
    except GrokUsageError as exc:
        return {
            "label": LABEL,
            "account": account,
            "ok": False,
            "probe_state": exc.probe_state,
            "auth_state": exc.auth_state,
            "error": str(exc),
        }

    config = payload.get("config")
    if not isinstance(config, dict):
        return {
            "label": LABEL,
            "account": account,
            "ok": False,
            "probe_state": PROBE_UNKNOWN,
            "auth_state": AUTH_VALID,
            "error": "billing response lacks config",
        }

    # ABSENT and RE-TYPED are different events and must not be conflated.
    #
    # Absent: observed 2026-08-12 on a freshly-created account whose billing
    # period had just opened -- the endpoint omits creditUsagePercent (and
    # productUsage) entirely rather than sending 0. Every other field parsed.
    # That is a measurable account with one field we cannot read, so the record
    # stays ok with used_percent None ("unknown"), and prepaid balance, reset,
    # and period still ride along. Returning early here instead would discard
    # fields we DID measure and hide a completely fresh seat behind a failure.
    #
    # Re-typed: the key is present but is not a number (or is a bool). That is
    # the endpoint contract changing under us, and it must read as a failure.
    #
    # Neither path may produce a healthy percentage: absent yields "unknown",
    # never 0% used and never 100% headroom.
    used_raw = config.get("creditUsagePercent")
    used_absent = "creditUsagePercent" not in config
    if not used_absent and (
        isinstance(used_raw, bool)
        or not isinstance(used_raw, (int, float))
        or not 0.0 <= used_raw <= 100.0
        or not math.isfinite(float(used_raw))
    ):
        return {
            "label": LABEL,
            "account": account,
            "ok": False,
            "probe_state": PROBE_UNKNOWN,
            "auth_state": AUTH_VALID,
            "error": "billing response re-typed creditUsagePercent",
        }
    used = None if used_absent else float(used_raw)

    # `creditUsagePercent` alone does NOT mean the seat is unusable. Subscription
    # credits and prepaid balance are separate purses: at 100% credit usage a
    # positive prepaid balance still serves requests. Reporting a wall from the
    # percent alone asserts a state this payload can actually measure and
    # contradicts -- the 2026-08-11 readout showed "0% wall" while the operator
    # knew prepaid was the deciding field. Pass it through and let the display
    # decide; `_balance` returns None (unknown) rather than 0 on a shape change,
    # so a contract change can never read as "no money left".
    return {
        "label": LABEL,
        "account": account,
        "ok": True,
        "probe_state": PROBE_UNKNOWN if used_absent else PROBE_USABLE,
        "auth_state": AUTH_VALID,
        # None = the endpoint did not report it for this account (see above).
        "used_percent": used,
        "used_percent_absent": used_absent,
        "reset_at": _epoch(config.get("billingPeriodEnd")),
        "source": "grok_billing_credits",
        "prepaid_balance": _balance(config.get("prepaidBalance")),
        # Pass-through only. Their semantics are not established, so nothing
        # downstream may derive a verdict from them until they are.
        "on_demand_cap": _balance(config.get("onDemandCap")),
        "on_demand_used": _balance(config.get("onDemandUsed")),
        "product_usage": _product_usage(config.get("productUsage")),
        "period_type": (config.get("currentPeriod") or {}).get("type")
        if isinstance(config.get("currentPeriod"), dict)
        else None,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Report grok subscription credit headroom."
    )
    parser.add_argument("--json", action="store_true", help="emit a JSON list")
    parser.add_argument("--timeout-s", type=float, default=DEFAULT_TIMEOUT_S)
    parser.add_argument(
        "--account",
        help="report only this seat label (default: every configured grok login)",
    )
    args = parser.parse_args(argv)

    targets = accounts()
    if args.account:
        targets = [(label, path) for label, path in targets if label == args.account]
        if not targets:
            targets = [(args.account, ACCOUNTS_DIR / args.account / ".grok" / "auth.json")]

    records = [
        read_usage(auth_path=path, timeout_s=args.timeout_s, account=label)
        for label, path in targets
    ]
    if args.json:
        print(json.dumps(records))
    else:
        for record in records:
            name = f"{LABEL} {record['account']}" if record.get("account") else LABEL
            used = record.get("used_percent")
            if not record.get("ok"):
                print(f"  {name:14s} {record.get('error')}")
            elif used is None:
                # ok, but the endpoint did not report the percentage for this
                # account -- printing it as a number would invent one.
                print(f"  {name:14s} used=unknown")
            else:
                print(f"  {name:14s} used={float(used):.0f}%")
    # Exit 0 when ANY login reported; one dead seat must not blank the others.
    return 0 if any(r.get("ok") for r in records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
