"""Focused tests for low-risk DRY leaf extractions."""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_adapter_gate as adapter_gate  # noqa: E402
import goalflight_compat as compat  # noqa: E402
import goalflight_dispatch as dispatch  # noqa: E402
import goalflight_ledger as ledger  # noqa: E402
import goalflight_rate_pressure as rate_pressure  # noqa: E402
import goalflight_validate_adapters as validate_adapters  # noqa: E402


@contextmanager
def env_var(name: str, value: str | None):
    sentinel = object()
    old = os.environ.get(name, sentinel)
    try:
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
        yield
    finally:
        if old is sentinel:
            os.environ.pop(name, None)
        else:
            os.environ[name] = str(old)


def assert_eq(name: str, got: object, expected: object) -> None:
    if got != expected:
        raise AssertionError(f"{name}: got {got!r}, expected {expected!r}")


def assert_true(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)


def test_safe_dispatch_filename_shared_by_queue_and_ledger() -> None:
    dispatch_id = "a/b"
    expected = f"a-b-{hashlib.sha256(dispatch_id.encode()).hexdigest()[:8]}"
    assert_eq("sanitized slash id", compat.safe_dispatch_filename(dispatch_id), expected)
    assert_eq("plain id unchanged", compat.safe_dispatch_filename("a_b"), "a_b")
    assert_true(
        "slash and underscore ids do not collide",
        compat.safe_dispatch_filename(dispatch_id) != compat.safe_dispatch_filename("a_b"),
    )

    with tempfile.TemporaryDirectory(prefix="gf-dry-safe-name-") as tmp:
        root = Path(tmp)
        with env_var("GOALFLIGHT_STATE_DIR", str(root / "state")):
            queue_path = dispatch._queue_entry_path(dispatch_id, queue_dir=root / "queue")  # noqa: SLF001
            ledger_path = ledger.record_path(dispatch_id)
    assert_eq("dispatch queue and ledger basename", queue_path.name, ledger_path.name)


def test_nearest_existing_path_walks_to_closest_parent() -> None:
    with tempfile.TemporaryDirectory(prefix="gf-dry-nearest-") as tmp:
        existing = Path(tmp) / "existing"
        existing.mkdir()
        missing = existing / "missing" / "leaf"
        assert_eq("public helper nearest parent", compat.nearest_existing_path(missing), existing)
        assert_eq("private compatibility helper nearest parent", compat._nearest_existing_path(missing), existing)  # noqa: SLF001


def test_rate_limit_signature_scanner_is_shared_and_literal() -> None:
    assert_eq(
        "usage limit signature",
        rate_pressure.rate_limit_signature_in_text("Worker hit a USAGE LIMIT today"),
        "usage limit",
    )
    assert_eq(
        "too many requests signature",
        rate_pressure.rate_limit_signature_in_text("provider returned too many requests"),
        "too many requests",
    )
    assert_eq(
        "bare 429 is not a literal signature",
        rate_pressure.rate_limit_signature_in_text("line 429 in a log file"),
        None,
    )


def test_adapter_tokenize_args_shared_between_gate_and_validator() -> None:
    values = ["--a b", 3, "'unterminated"]
    expected = ["--a", "b", "'unterminated"]
    assert_eq("gate public tokenizer", adapter_gate.tokenize_args(values), expected)
    assert_eq("gate private compatibility wrapper", adapter_gate._tokenize_args(values), expected)  # noqa: SLF001
    assert_eq("validator split wrapper", validate_adapters._split_tokens(values), expected)  # noqa: SLF001
    assert_true(
        "validator update probe tokenizes shell strings",
        validate_adapters._looks_like_update_or_registry_probe(["pip install goal-flight"]),  # noqa: SLF001
    )


def main() -> None:
    tests = [
        test_safe_dispatch_filename_shared_by_queue_and_ledger,
        test_nearest_existing_path_walks_to_closest_parent,
        test_rate_limit_signature_scanner_is_shared_and_literal,
        test_adapter_tokenize_args_shared_between_gate_and_validator,
    ]
    for test in tests:
        test()
    print(f"PASS tests/python/test_dry_leaf_extractions.py ({len(tests)} tests)")


if __name__ == "__main__":
    main()
