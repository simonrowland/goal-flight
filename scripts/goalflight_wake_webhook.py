#!/usr/bin/env python3
"""Best-effort outbound wake nudge for controller mail harvest.

This is not a second inbox and not a mail transport. The journal remains
durable truth. When a waking delivery event becomes visible (the same
projection ``listen`` already waits on), an operator may optionally HTTP
POST a small JSON nudge to an external host so a Grok Bot webhook routine
can wake without depending on a flaky Mac local-exec ``listen`` session.

Failure to POST never raises to the caller. Config is env/file only.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.parse
import urllib.request


WAKE_WEBHOOK_URL_ENV = "GOALFLIGHT_WAKE_WEBHOOK_URL"
WAKE_WEBHOOK_SECRET_ENV = "GOALFLIGHT_WAKE_WEBHOOK_SECRET"
WAKE_WEBHOOK_AUTH_ENV = "GOALFLIGHT_WAKE_WEBHOOK_AUTH"
WAKE_WEBHOOK_TIMEOUT_ENV = "GOALFLIGHT_WAKE_WEBHOOK_TIMEOUT_S"
WAKE_WEBHOOK_CONFIG_ENV = "GOALFLIGHT_WAKE_WEBHOOK_CONFIG"
DEFAULT_CONFIG_PATH = Path.home() / ".goal-flight" / "wake-webhook.json"
DEFAULT_TIMEOUT_S = 2.0
MIN_TIMEOUT_S = 0.1
MAX_TIMEOUT_S = 15.0
AUTH_BEARER = "bearer"
AUTH_X_WEBHOOK_KEY = "x-webhook-key"
AUTH_MODES = frozenset({AUTH_BEARER, AUTH_X_WEBHOOK_KEY})
USER_AGENT = "goal-flight-wake-webhook/1"

# Terminal harvest of a worker. ``blocked`` is a finished attempt, not mail.
_COMPLETE_EVENT_TYPES = frozenset({"result", "blocked"})
# Controller-addressed channel and the typed aliases that may carry an addressee.
_MAIL_EVENT_TYPES = frozenset(
    {
        "controller-question",
        "controller-answer",
        "controller-notice",
        "controller-coordination",
        "coordination",
        "notice",
        "merge-request",
        "patch",
        "finding",
        "advisory",
    }
)
_ATTENTION_STREAM = "attention"


@dataclass(frozen=True)
class WakeWebhookConfig:
    url: str
    secret: str = ""
    auth: str = AUTH_BEARER
    timeout_s: float = DEFAULT_TIMEOUT_S


def classify_nudge_kind(event_type: object) -> str:
    """Map a delivery ``event_type`` to the nudge kind: mail, wake, or complete."""
    kind = str(event_type or "").strip()
    if kind in _COMPLETE_EVENT_TYPES:
        return "complete"
    if kind in _MAIL_EVENT_TYPES:
        return "mail"
    return "wake"


def default_config_path() -> Path:
    raw = os.environ.get(WAKE_WEBHOOK_CONFIG_ENV, "").strip()
    if raw:
        return Path(raw).expanduser()
    if os.environ.get("GOALFLIGHT_TEST_MODE") == "1":
        # Tests must not read the operator's home file if isolation forgot
        # GOALFLIGHT_WAKE_WEBHOOK_CONFIG. Production workers do not set TEST_MODE.
        return Path(os.devnull)
    return DEFAULT_CONFIG_PATH


def _clamp_timeout(value: object, default: float = DEFAULT_TIMEOUT_S) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if parsed != parsed:  # NaN
        return default
    return min(MAX_TIMEOUT_S, max(MIN_TIMEOUT_S, parsed))


def _normalize_auth(value: object) -> str:
    text = str(value or "").strip().lower().replace("_", "-")
    if text in AUTH_MODES:
        return text
    return AUTH_BEARER


def _file_config(path: Path) -> dict[str, object]:
    if str(path) in {"", os.devnull} or path.is_dir():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def load_config() -> WakeWebhookConfig | None:
    """Return operator webhook config, or None when no URL is configured.

    Never raises. Env overlays the optional ``~/.goal-flight/wake-webhook.json``
    (or ``$GOALFLIGHT_WAKE_WEBHOOK_CONFIG``). An absent/empty/invalid URL means
    zero HTTP.
    """
    file_cfg = _file_config(default_config_path())
    url = str(file_cfg.get("url") or "").strip()
    secret = str(file_cfg.get("secret") or "")
    auth = _normalize_auth(file_cfg.get("auth"))
    timeout_s = _clamp_timeout(file_cfg.get("timeout_s"), DEFAULT_TIMEOUT_S)

    if WAKE_WEBHOOK_URL_ENV in os.environ:
        url = os.environ.get(WAKE_WEBHOOK_URL_ENV, "").strip()
    if WAKE_WEBHOOK_SECRET_ENV in os.environ:
        secret = os.environ.get(WAKE_WEBHOOK_SECRET_ENV, "")
    if WAKE_WEBHOOK_AUTH_ENV in os.environ:
        auth = _normalize_auth(os.environ.get(WAKE_WEBHOOK_AUTH_ENV))
    if WAKE_WEBHOOK_TIMEOUT_ENV in os.environ:
        timeout_s = _clamp_timeout(os.environ.get(WAKE_WEBHOOK_TIMEOUT_ENV), timeout_s)

    if not url:
        return None
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        print(
            "wake-webhook: ignored URL (need http(s) with a host)",
            file=sys.stderr,
        )
        return None
    return WakeWebhookConfig(
        url=url,
        secret=secret,
        auth=auth,
        timeout_s=timeout_s,
    )


def nudge_payload(row: dict[str, object]) -> dict[str, object]:
    """Build the nudge body. No mail text, secrets, or task tables."""
    event_type = str(row.get("event_type") or "").strip()
    payload: dict[str, object] = {
        "kind": classify_nudge_kind(event_type),
        "controller_label": str(row.get("recipient_label") or "").strip(),
        "project_root": str(row.get("project_root") or "").strip(),
        "event_type": event_type,
    }
    stream_id = str(row.get("stream_id") or "").strip()
    if stream_id and stream_id != _ATTENTION_STREAM:
        payload["dispatch_id"] = stream_id
    return payload


def _safe_reason(exc: BaseException) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code}"
    return type(exc).__name__


def _auth_headers(config: WakeWebhookConfig) -> dict[str, str]:
    if not config.secret:
        return {}
    if config.auth == AUTH_X_WEBHOOK_KEY:
        return {"X-Webhook-Key": config.secret}
    return {"Authorization": f"Bearer {config.secret}"}


def post_nudge(config: WakeWebhookConfig, payload: dict[str, object]) -> bool:
    """POST one nudge. Returns True on HTTP success. Never raises."""
    try:
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode(
            "utf-8"
        )
    except (TypeError, ValueError) as exc:
        print(f"wake-webhook: skipped unserializable payload: {type(exc).__name__}", file=sys.stderr)
        return False
    headers = {
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        **_auth_headers(config),
    }
    request = urllib.request.Request(
        config.url,
        data=body,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=config.timeout_s) as response:
            response.read()
        return True
    except (urllib.error.URLError, OSError, TimeoutError, ValueError) as exc:
        print(f"wake-webhook: POST failed: {_safe_reason(exc)}", file=sys.stderr)
        return False
    except Exception as exc:  # noqa: BLE001 — fail-open fence; journal already committed
        print(f"wake-webhook: POST failed: {_safe_reason(exc)}", file=sys.stderr)
        return False


def nudge_projected_delivery(row: dict[str, object] | None) -> bool:
    """Best-effort nudge after a waking delivery becomes listen-visible.

    Returns True only when a POST was attempted and the server accepted it.
    Missing config, quiet events, and transport failures return False.
    """
    if not isinstance(row, dict):
        return False
    if str(row.get("wake_class") or "") != "waking":
        return False
    if not row.get("newly_projected"):
        return False
    config = load_config()
    if config is None:
        return False
    try:
        return post_nudge(config, nudge_payload(row))
    except Exception as exc:  # noqa: BLE001 — must never break journal projection
        print(f"wake-webhook: POST failed: {_safe_reason(exc)}", file=sys.stderr)
        return False
