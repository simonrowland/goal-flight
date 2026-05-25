#!/usr/bin/env python3
"""Versioned fleet persistence schemas and validators (Track A Phase 0)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
MIN_READER_VERSION = 1

FLEET_SCHEMA = "goalflight.fleet.v1"
BILLING_SCHEMA = "goalflight.fleet.billing-accounts.v1"
STEERING_SCHEMA = "goalflight.fleet.steering.v1"
ACCOUNT_LOCK_SCHEMA = "goalflight.fleet.account-lock.v1"
AGGREGATE_SCHEMA = "goalflight.fleet.register.aggregate.v1"

BILLING_LIMIT_CLASSES = frozenset(
    {"org", "account", "session", "api_project", "gateway_router", "machine_local"}
)

REQUIRED_TOP_LEVEL = ("schema", "schema_version", "min_reader_version")


class SchemaError(Exception):
    def __init__(self, path: str, message: str) -> None:
        self.path = path
        self.message = message
        super().__init__(f"{path}: {message}")


def _require_mapping(data: Any, label: str) -> dict:
    if not isinstance(data, dict):
        raise SchemaError(label, "expected object")
    return data


def _require_fields(data: dict, label: str, fields: tuple[str, ...]) -> None:
    for field in fields:
        if field not in data:
            raise SchemaError(label, f"missing field: {field}")


def _check_version_header(data: dict, label: str, schema_name: str) -> None:
    _require_fields(data, label, REQUIRED_TOP_LEVEL)
    if data["schema"] != schema_name:
        raise SchemaError(label, f"schema mismatch: {data.get('schema')}")
    if data["schema_version"] != SCHEMA_VERSION:
        raise SchemaError(label, f"unsupported schema_version: {data.get('schema_version')}")
    min_reader = data["min_reader_version"]
    if not isinstance(min_reader, int) or min_reader > MIN_READER_VERSION:
        raise SchemaError(label, f"min_reader_version too new: {min_reader}")


def validate_fleet(data: Any) -> None:
    doc = _require_mapping(data, "fleet")
    _check_version_header(doc, "fleet", FLEET_SCHEMA)
    if "nodes" not in doc or not isinstance(doc["nodes"], dict):
        raise SchemaError("fleet.nodes", "expected object")


def validate_billing_accounts(data: Any) -> None:
    doc = _require_mapping(data, "billing")
    _check_version_header(doc, "billing", BILLING_SCHEMA)
    accounts = doc.get("accounts")
    if not isinstance(accounts, list):
        raise SchemaError("billing.accounts", "expected array")
    for idx, account in enumerate(accounts):
        label = f"billing.accounts[{idx}]"
        if not isinstance(account, dict):
            raise SchemaError(label, "expected object")
        if "account_key" not in account:
            raise SchemaError(label, "missing field: account_key")
        limit_class = account.get("limit_class")
        if limit_class is not None and limit_class not in BILLING_LIMIT_CLASSES:
            raise SchemaError(label, f"invalid limit_class: {limit_class}")
        agent_labels = account.get("agent_labels")
        if agent_labels is not None and not isinstance(agent_labels, list):
            raise SchemaError(label, "agent_labels must be array")


def validate_steering(data: Any) -> None:
    doc = _require_mapping(data, "steering")
    _check_version_header(doc, "steering", STEERING_SCHEMA)
    if "node_policy" not in doc or not isinstance(doc["node_policy"], dict):
        raise SchemaError("steering.node_policy", "expected object")
    overrides = doc.get("conversation_overrides")
    if overrides is not None and not isinstance(overrides, list):
        raise SchemaError("steering.conversation_overrides", "expected array")


def validate_account_lock(data: Any) -> None:
    doc = _require_mapping(data, "account_lock")
    _check_version_header(doc, "account_lock", ACCOUNT_LOCK_SCHEMA)
    _require_fields(
        doc,
        "account_lock",
        ("account_key", "owner_dispatch_id", "fencing_token", "state"),
    )
    if doc["state"] not in {"active", "stale", "released"}:
        raise SchemaError("account_lock.state", "invalid state")


VALIDATORS = {
    FLEET_SCHEMA: validate_fleet,
    BILLING_SCHEMA: validate_billing_accounts,
    STEERING_SCHEMA: validate_steering,
    ACCOUNT_LOCK_SCHEMA: validate_account_lock,
}


def validate_document(data: Any) -> None:
    doc = _require_mapping(data, "document")
    schema_name = doc.get("schema")
    if schema_name not in VALIDATORS:
        raise SchemaError("document.schema", f"unknown schema: {schema_name}")
    VALIDATORS[schema_name](data)


def validate_file(path: Path) -> list[str]:
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        return [f"{path}: invalid JSON: {exc}"]
    try:
        validate_document(data)
    except SchemaError as exc:
        return [str(exc)]
    return []


def default_fleet_doc(controller_id: str) -> dict:
    return {
        "schema": FLEET_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "min_reader_version": MIN_READER_VERSION,
        "controller_id": controller_id,
        "nodes": {},
    }


def default_billing_doc() -> dict:
    return {
        "schema": BILLING_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "min_reader_version": MIN_READER_VERSION,
        "accounts": [
            {
                "account_key": "openai/default",
                "limit_pool_id": "openai-default",
                "limit_class": "account",
                "provider": "openai",
                "agent_labels": ["codex", "codex-acp", "codex-bash-tail"],
                "allow_shared_pool": True,
                "max_concurrent": 2,
            },
            {
                "account_key": "anthropic/session-local",
                "limit_pool_id": "anthropic-session-local",
                "limit_class": "session",
                "provider": "anthropic-session",
                "agent_labels": ["claude"],
                "allow_shared_pool": False,
                "max_concurrent": 1,
            },
        ],
    }


def default_steering_doc() -> dict:
    return {
        "schema": STEERING_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "min_reader_version": MIN_READER_VERSION,
        "node_policy": {"priority": []},
        "conversation_overrides": [],
    }
