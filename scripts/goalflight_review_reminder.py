#!/usr/bin/env python3
"""goal-flight review reminder — an OPTIONAL pre-commit nudge to review before committing.

Why optional, by construction:
  - Git hooks are never shipped with a clone (`.git/hooks/` is local), and this repo
    activates its hooks via `core.hooksPath=hooks` — itself a local, opt-in setting. So a
    downloader gets NOTHING unless they opt in.
  - DOUBLE opt-in: even with the repo hooks active, this reminder is a no-op unless enabled
    via `git config goalflight.reviewReminder true` or env `GOALFLIGHT_REVIEW_REMINDER=1`.

Modes (when enabled):
  - reminder (default): print a nudge to review and EXIT 0 — never blocks.
  - strict (`git config goalflight.reviewStrict true` / `GOALFLIGHT_REVIEW_STRICT=1`):
    EXIT 1 (blocks) until acknowledged.

Overrides (any of):
  - GOALFLIGHT_REVIEW_OK=1                       acknowledge: you reviewed this commit
  - git commit --no-verify                       git built-in; skips ALL hooks
  - git config goalflight.reviewReminder false   turn the reminder off

This is a solo/local nudge. It is deliberately NOT an enforced gate on anyone who downloads
goal-flight — see protocols/chunk-review.md for the actual review doctrine.
"""
from __future__ import annotations

import os
import subprocess
import sys

_TRUE = {"1", "true", "yes", "on"}


def _env_true(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUE


def _git_config_tristate(key: str):
    """True / False / None(unset) for a boolean git config key."""
    try:
        out = subprocess.run(
            ["git", "config", "--bool", "--get", key],
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception:
        return None
    if out.returncode != 0:
        return None
    val = out.stdout.strip()
    return True if val == "true" else False if val == "false" else None


def _knob(env_name: str, config_key: str) -> bool:
    """A knob is ON when env or config enables it — but an EXPLICIT config `false`
    is an override-to-disable that wins over an env enable, so
    `git config <key> false` genuinely turns it off even if the env var is set."""
    cfg = _git_config_tristate(config_key)
    if cfg is False:
        return False
    return cfg is True or _env_true(env_name)


def _enabled() -> bool:
    return _knob("GOALFLIGHT_REVIEW_REMINDER", "goalflight.reviewReminder")


def _strict() -> bool:
    return _knob("GOALFLIGHT_REVIEW_STRICT", "goalflight.reviewStrict")


def _acknowledged() -> bool:
    return _env_true("GOALFLIGHT_REVIEW_OK")


REMINDER = (
    "\n  goal-flight: remember to review this change before you push.\n"
    "  protocols/chunk-review.md — run a parallel review flight (gstack /review + autoreview,\n"
    "  concern-diverse) and fold findings BEFORE committing. This is a reminder, not a block.\n"
    "  Silence: GOALFLIGHT_REVIEW_OK=1 (you reviewed) · `git commit --no-verify` (skip hooks)\n"
    "  · `git config goalflight.reviewReminder false` (turn off).\n"
)

STRICT_BLOCK = (
    "\n  goal-flight (strict): no review acknowledgement for this commit.\n"
    "  Review per protocols/chunk-review.md, then re-commit with GOALFLIGHT_REVIEW_OK=1,\n"
    "  or override with `git commit --no-verify`.\n"
    "  (Disable strict: `git config goalflight.reviewStrict false`.)\n"
)


def main(argv: list[str] | None = None) -> int:
    # argv is accepted for hook-call symmetry; no flags are required.
    if not _enabled():
        return 0  # default posture: opt-in only -> silent no-op
    if _acknowledged():
        return 0  # reviewer explicitly acknowledged this commit
    if _strict():
        sys.stderr.write(STRICT_BLOCK)
        return 1  # blocks; overridable per the message
    sys.stderr.write(REMINDER)
    return 0  # reminder only -> never blocks


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
