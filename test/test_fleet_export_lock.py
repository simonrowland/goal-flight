#!/usr/bin/env python3
"""Tests for fleet export/import, registry lock, and account lock FSM."""

from __future__ import annotations

import json
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_fleet as fleet
import goalflight_fleet_schemas as schemas


def assert_true(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)


def test_export_import_round_trip_strict() -> None:
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        bundle = Path(td) / "bundle.tgz"
        fleet.bootstrap(fleet_dir)
        manifest = fleet.export_bundle(fleet_dir, bundle)
        assert_true("manifest has hashes", len(manifest["files"]) == 3)
        result = fleet.import_bundle(fleet_dir, bundle, merge="strict")
        assert_true("strict import ok", result["ok"] is True)


def test_strict_merge_rejects_drift() -> None:
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        bundle = Path(td) / "bundle.tgz"
        fleet.bootstrap(fleet_dir)
        fleet.export_bundle(fleet_dir, bundle)
        billing = fleet.read_json(fleet_dir / "billing-accounts.json")
        billing["accounts"][0]["max_concurrent"] = 99
        fleet._atomic_write_json(fleet_dir / "billing-accounts.json", billing)
        try:
            fleet.import_bundle(fleet_dir, bundle, merge="strict")
            assert_true("should reject drift", False)
        except fleet.FleetError:
            pass


def test_prefer_local_keeps_local_on_drift() -> None:
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        bundle = Path(td) / "bundle.tgz"
        fleet.bootstrap(fleet_dir)
        fleet.export_bundle(fleet_dir, bundle)
        billing = fleet.read_json(fleet_dir / "billing-accounts.json")
        billing["accounts"][0]["max_concurrent"] = 77
        fleet._atomic_write_json(fleet_dir / "billing-accounts.json", billing)
        fleet.import_bundle(fleet_dir, bundle, merge="prefer-local")
        after = fleet.read_json(fleet_dir / "billing-accounts.json")
        assert_true("local kept", after["accounts"][0]["max_concurrent"] == 77)


def test_registry_lock_blocks_second_writer() -> None:
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        fleet.bootstrap(fleet_dir)
        errors: list[str] = []

        lock_acquired = threading.Event()
        release_lock = threading.Event()

        def holder() -> None:
            with fleet.RegistryLock(fleet_dir):
                lock_acquired.set()
                release_lock.wait(timeout=5)

        t = threading.Thread(target=holder)
        t.start()
        assert_true("holder acquired", lock_acquired.wait(timeout=2))
        try:
            with fleet.RegistryLock(fleet_dir, blocking=False):
                errors.append("should not acquire")
        except fleet.RegistryLockError:
            pass
        release_lock.set()
        t.join(timeout=2)
        assert_true("second writer blocked", not errors)


def test_account_lock_duplicate_acquire_fails() -> None:
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        fleet.bootstrap(fleet_dir)
        with fleet.RegistryLock(fleet_dir):
            doc = fleet.acquire_account_lock(
                fleet_dir,
                account_key="openai/default",
                owner_dispatch_id="dispatch-a",
                ttl_s=60,
            )
        assert_true("active lock", doc["state"] == "active")
        try:
            with fleet.RegistryLock(fleet_dir):
                fleet.acquire_account_lock(
                    fleet_dir,
                    account_key="openai/default",
                    owner_dispatch_id="dispatch-b",
                    ttl_s=60,
                )
            assert_true("duplicate should fail", False)
        except fleet.AccountLockError:
            pass


def test_account_lock_release_requires_fencing() -> None:
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        fleet.bootstrap(fleet_dir)
        with fleet.RegistryLock(fleet_dir):
            doc = fleet.acquire_account_lock(
                fleet_dir,
                account_key="openai/default",
                owner_dispatch_id="dispatch-a",
            )
        try:
            fleet.release_account_lock(
                fleet_dir,
                account_key="openai/default",
                fencing_token="wrong-token",
            )
            assert_true("wrong fencing should fail", False)
        except fleet.AccountLockError:
            pass
        released = fleet.release_account_lock(
            fleet_dir,
            account_key="openai/default",
            fencing_token=doc["fencing_token"],
        )
        assert_true("released", released["state"] == "released")


def main() -> None:
    for test in (
        test_export_import_round_trip_strict,
        test_strict_merge_rejects_drift,
        test_prefer_local_keeps_local_on_drift,
        test_registry_lock_blocks_second_writer,
        test_account_lock_duplicate_acquire_fails,
        test_account_lock_release_requires_fencing,
    ):
        test()
        print(f"PASS {test.__name__}")


if __name__ == "__main__":
    main()
