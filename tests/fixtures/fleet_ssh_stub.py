#!/usr/bin/env python3
"""Hermetic SSH stub harness for fleet watch + reconcile tests (Track A goal 10d)."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class FleetSshStubScenario:
    name: str
    responses: list[tuple[int, str, str]] = field(default_factory=list)
    call_index: int = 0
    flap_after: int | None = None
    flap_duration_calls: int = 0

    def runner(self) -> Callable[[list[str]], tuple[int, str, str]]:
        stub = self

        def _run(_argv: list[str]) -> tuple[int, str, str]:
            if stub.flap_after is not None and stub.call_index >= stub.flap_after:
                if stub.call_index < stub.flap_after + stub.flap_duration_calls:
                    stub.call_index += 1
                    return 255, "", "ssh: connect failed"
            if stub.call_index >= len(stub.responses):
                stub.call_index += 1
                return 0, stub.responses[-1][1] if stub.responses else "{}", ""
            code, stdout, stderr = stub.responses[stub.call_index]
            stub.call_index += 1
            return code, stdout, stderr

        return _run


def status_json(*, dispatch_id: str, seq: int, state: str = "running", worker_pid: int = 4242) -> str:
    return json.dumps(
        {
            "schema": "goalflight.acp-run.v1",
            "seq": seq,
            "dispatch_id": dispatch_id,
            "state": state,
            "worker_pid": worker_pid,
            "updated_at": "2026-05-24T12:00:00+00:00",
        }
    )


def partial_json(*, dispatch_id: str) -> str:
    return json.dumps(
        {
            "schema": "goalflight.acp-run.v1",
            "seq": 2,
            "dispatch_id": dispatch_id,
            "state": "running",
        }
    )[:-10]


def scaled_sleep(seconds: float, *, scale: float = 0.01) -> None:
    """Scale wall-clock waits for CI (30s flap -> 0.3s)."""
    time.sleep(max(0.001, seconds * scale))
