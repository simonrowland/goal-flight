"""Controller-label shape shared by registration and persistence boundaries."""

from __future__ import annotations

import re


_CONTROLLER_LABEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
CONTROLLER_LABEL_RULE = (
    "1-64 ASCII letters, digits, '.', '_' or '-', starting with a letter or digit"
)


class ControllerLabelValidationError(ValueError):
    """A controller label cannot be used as persisted ownership metadata."""


def validate_controller_label(value: object, *, field: str) -> str | None:
    """Normalize blank labels to absence and validate present labels."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ControllerLabelValidationError(
            f"{field} must be a controller label matching {CONTROLLER_LABEL_RULE} "
            f"(for example, 'goal-flight'; type {type(value).__name__})"
        )
    normalized = value.strip()
    if not normalized:
        return None
    if _CONTROLLER_LABEL_RE.fullmatch(normalized) is None:
        raise ControllerLabelValidationError(
            f"{field} must be a controller label matching {CONTROLLER_LABEL_RULE} "
            f"(for example, 'goal-flight'; type {type(value).__name__})"
        )
    return normalized
