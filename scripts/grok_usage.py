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
disappear without notice. Every failure mode here - missing auth, HTTP error,
unparseable body, missing or non-numeric fields - reports ``ok: false`` with a
reason. None of them may render as a healthy row or as 0% remaining: a shape
change means "could not measure", never "measured, and it is bad".
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import urllib.error
import urllib.request

BILLING_URL = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"
AUTH_PATH = Path(os.environ.get("GROK_HOME", Path.home() / ".grok")) / "auth.json"
DEFAULT_TIMEOUT_S = 15.0
LABEL = "grok"


class GrokUsageError(RuntimeError):
    """A safe-to-report failure. The message never contains credential bytes."""


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
        raise GrokUsageError("no grok login found") from exc
    except (OSError, ValueError) as exc:
        raise GrokUsageError("grok auth document is unreadable") from exc

    if not isinstance(document, dict) or not document:
        raise GrokUsageError("grok auth document is empty")
    entry = next(iter(document.values()))
    token = entry.get("key") if isinstance(entry, dict) else None
    if not isinstance(token, str) or not token:
        raise GrokUsageError("grok auth document carries no session token")
    return token


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
        raise GrokUsageError(f"billing endpoint returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise GrokUsageError("billing endpoint unreachable") from exc
    except UnicodeError as exc:
        raise GrokUsageError("billing response was not valid UTF-8") from exc

    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise GrokUsageError("billing response was not valid JSON") from exc
    if not isinstance(payload, dict):
        raise GrokUsageError("billing response was not an object")
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
) -> dict:
    """Return one normalized record. Never raises for an expected failure."""
    auth_path = AUTH_PATH if auth_path is None else auth_path
    try:
        payload = (
            fetcher(url, timeout_s)
            if fetcher is not None
            else _fetch(_session_token(auth_path), url=url, timeout_s=timeout_s)
        )
    except GrokUsageError as exc:
        return {"label": LABEL, "ok": False, "error": str(exc)}

    config = payload.get("config")
    if not isinstance(config, dict):
        return {
            "label": LABEL,
            "ok": False,
            "error": "billing response lacks config",
        }

    used = config.get("creditUsagePercent")
    if isinstance(used, bool) or not isinstance(used, (int, float)):
        # A missing or re-typed field is a contract change, not 100% headroom.
        return {
            "label": LABEL,
            "ok": False,
            "error": "billing response lacks a numeric creditUsagePercent",
        }

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
        "ok": True,
        "used_percent": float(used),
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
    args = parser.parse_args(argv)

    record = read_usage(timeout_s=args.timeout_s)
    if args.json:
        print(json.dumps([record]))
    elif record.get("ok"):
        print(f"  {LABEL:14s} used={record['used_percent']:.0f}%")
    else:
        print(f"  {LABEL:14s} {record.get('error')}")
    return 0 if record.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
