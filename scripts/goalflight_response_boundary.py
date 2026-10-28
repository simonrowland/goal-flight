"""Shared selection rule for ambiguous rendered-response footer candidates."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TypeVar

T = TypeVar("T")


def select_response_footer_candidate(
    candidates: Sequence[T],
    is_verified: Callable[[T], bool],
    *,
    verify_single: bool = False,
) -> T | None:
    """Select the sole candidate, or the earliest candidate verified by its repeat."""
    if len(candidates) == 1 and not verify_single:
        return candidates[0]
    return next((candidate for candidate in candidates if is_verified(candidate)), None)
