#!/usr/bin/env python3
"""Hermetic tests for the context-window meter and PostToolUse hook.

Transcripts are constructed in a tempdir. This file never opens a real
session transcript.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
HOOK = ROOT / "scripts/hooks/goalflight-context-meter.sh"
METER = ROOT / "scripts/goalflight_context_meter.py"
sys.path.insert(0, str(SCRIPTS))

import goalflight_context_meter as meter  # noqa: E402


WINDOW = 1_000_000
METER_ENV = (
    "GOALFLIGHT_CONTEXT_WINDOW",
    "GOALFLIGHT_CONTEXT_MODEL",
    "GOALFLIGHT_CONTEXT_METER",
    "GOALFLIGHT_CONTEXT_METER_EVERY",
    "GOALFLIGHT_CONTEXT_METER_GROWTH",
    "GOALFLIGHT_CONTEXT_METER_STATE",
    "GOALFLIGHT_CONTEXT_METER_STATE_DIR",
    "GOALFLIGHT_CONTEXT_METER_CALLS",
)


@pytest.fixture(autouse=True)
def _isolate_meter_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in METER_ENV:
        monkeypatch.delenv(name, raising=False)


def _usage_record(
    input_tokens: int,
    cache_read: int = 0,
    cache_creation: int = 0,
    extra: dict | None = None,
    model: str | None = None,
) -> dict:
    message = {
        "role": "assistant",
        "usage": {
            "input_tokens": input_tokens,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_creation,
            "output_tokens": 12,
        },
    }
    if model is not None:
        message["model"] = model
    if extra:
        message.update(extra)
    return {"type": "assistant", "message": message}


def _write_transcript(path: Path, records: list[object]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            if isinstance(record, (bytes, bytearray)):
                handle.write(record.decode("utf-8"))
                if not record.endswith(b"\n"):
                    handle.write("\n")
            else:
                handle.write(json.dumps(record))
                handle.write("\n")
    return path


def _write_padded_transcript(
    path: Path,
    *,
    prefix_bytes: int,
    usage: dict,
    suffix_bytes: int = 0,
    pad: bytes | None = None,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = pad or (b'{"pad":"' + (b"n" * 200) + b'"}\n')
    usage_line = (json.dumps(usage) + "\n").encode("utf-8")
    with path.open("wb") as handle:
        written = 0
        while written < prefix_bytes:
            chunk = line if written + len(line) <= prefix_bytes else line[: prefix_bytes - written]
            handle.write(chunk)
            written += len(chunk)
        handle.write(usage_line)
        written = 0
        while written < suffix_bytes:
            chunk = line if written + len(line) <= suffix_bytes else line[: suffix_bytes - written]
            handle.write(chunk)
            written += len(chunk)
    return path


def _cli(tmp_path: Path, extra: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    return subprocess.run(
        [sys.executable, str(METER), *extra],
        cwd=ROOT,
        env=merged,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def _hook_env(tmp_path: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env["GOALFLIGHT_CONTEXT_METER_STATE"] = str(tmp_path / "meter-state.json")
    env["GOALFLIGHT_CONTEXT_METER_STATE_DIR"] = str(tmp_path / "meter-state-dir")
    env["GOALFLIGHT_CONTEXT_METER_EVERY"] = "1"
    env["GOALFLIGHT_CONTEXT_WINDOW"] = str(WINDOW)
    if extra:
        env.update(extra)
    return env


def _run_hook_script(
    tmp_path: Path,
    payload: dict,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(HOOK)],
        cwd=ROOT,
        env=env or _hook_env(tmp_path),
        input=json.dumps(payload),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def test_680k_on_1m_window_is_68_percent_ok(tmp_path: Path) -> None:
    path = _write_transcript(
        tmp_path / "t.jsonl",
        [_usage_record(0, cache_read=680_000)],
    )
    reading = meter.measure_transcript(path, WINDOW)
    assert reading.unknown is False
    assert reading.tokens == 680_000
    assert reading.pct == 68.0
    assert reading.verdict == "ok"
    assert reading.band is None
    assert meter.render_text(reading) == "68% ok"


def test_exact_80_is_band_80(tmp_path: Path) -> None:
    """80.0% must fire. A `>` comparison (instead of `>=`) fails this test."""
    path = _write_transcript(tmp_path / "t.jsonl", [_usage_record(800_000)])
    reading = meter.measure_transcript(path, WINDOW)
    assert reading.pct == 80.0
    assert meter.band_for(reading.pct) == 80
    assert reading.verdict == "band-80"


def test_exact_90_and_95_boundaries(tmp_path: Path) -> None:
    ninety = meter.measure_transcript(
        _write_transcript(tmp_path / "n.jsonl", [_usage_record(900_000)]),
        WINDOW,
    )
    assert ninety.pct == 90.0
    assert ninety.band == 90
    assert ninety.verdict == "band-90"
    ninety_five = meter.measure_transcript(
        _write_transcript(tmp_path / "f.jsonl", [_usage_record(950_000)]),
        WINDOW,
    )
    assert ninety_five.pct == 95.0
    assert ninety_five.band == 95
    assert ninety_five.verdict == "band-95"


def test_sums_input_and_both_cache_fields(tmp_path: Path) -> None:
    path = _write_transcript(
        tmp_path / "t.jsonl",
        [_usage_record(100_000, cache_read=50_000, cache_creation=50_000)],
    )
    assert meter.measure_transcript(path, WINDOW).tokens == 200_000


def test_newest_usage_wins(tmp_path: Path) -> None:
    path = _write_transcript(
        tmp_path / "t.jsonl",
        [
            _usage_record(100_000),
            {"type": "user", "message": {"content": "later"}},
            _usage_record(250_000),
        ],
    )
    assert meter.measure_transcript(path, WINDOW).tokens == 250_000


def test_missing_window_is_unknown_not_a_number(tmp_path: Path) -> None:
    path = _write_transcript(tmp_path / "t.jsonl", [_usage_record(800_000)])
    reading = meter.measure_transcript(path, None)
    assert reading.unknown is True
    assert reading.pct is None
    assert reading.verdict == "unknown"
    assert meter.resolve_window() is None


def test_unknown_model_is_not_a_number() -> None:
    assert meter.window_for_model("claude-opus-4-6") is None
    assert meter.window_for_model("mystery-model") is None
    assert meter.resolve_window(model="claude-opus-4-6") is None
    assert meter.resolve_window(model="mystery-model") is None


def test_opus_5_maps_to_fleet_1m() -> None:
    """1M is a fleet operator directive, not a vendor guarantee."""
    assert meter.KNOWN_MODEL_WINDOWS.get("claude-opus-5") == 1_000_000
    assert meter.window_for_model("claude-opus-5") == 1_000_000
    assert meter.resolve_window(model="claude-opus-5") == 1_000_000


def test_fable_5_maps_to_fleet_1m() -> None:
    """Same fleet directive as Opus. This fleet runs Fable too."""
    assert meter.KNOWN_MODEL_WINDOWS.get("claude-fable-5") == 1_000_000
    assert meter.window_for_model("claude-fable-5") == 1_000_000
    assert meter.resolve_window(model="claude-fable-5") == 1_000_000


def test_map_is_exactly_opus_and_fable() -> None:
    assert set(meter.KNOWN_MODEL_WINDOWS) == {"claude-opus-5", "claude-fable-5"}
    assert set(meter.KNOWN_MODEL_WINDOWS.values()) == {1_000_000}


def test_dated_family_suffix_inherits_fleet_window() -> None:
    assert meter.window_for_model("claude-opus-5-20260501") == 1_000_000
    assert meter.window_for_model("claude-fable-5-20260817") == 1_000_000
    assert meter.classify_model("claude-opus-5-20260501") == (
        "dated-family",
        1_000_000,
    )


def test_non_date_family_suffix_is_unmapped_variant_not_a_number() -> None:
    """claude-opus-5-mini must NOT inherit 1M (too-large / silent)."""
    assert meter.window_for_model("claude-opus-5-mini") is None
    kind, mapped = meter.classify_model("claude-opus-5-mini")
    assert kind == "unmapped-variant"
    assert mapped is None


def test_sonnet_and_haiku_stay_unknown() -> None:
    assert meter.window_for_model("claude-sonnet-4-5") is None
    assert meter.window_for_model("claude-haiku-4-5-20251001") is None
    assert meter.classify_model("claude-sonnet-4-5") == ("unknown", None)


def test_inflated_map_would_under_report_the_80_band() -> None:
    """Dangerous direction: a too-LARGE window is a silent under-report.

    800k / 1M = 80% (must fire). 800k / 2M = 40% (silent). If the fleet
    map is inflated this test goes red — that is the failure mode the
    meter exists to prevent.
    """
    tokens = 800_000
    for model_id in ("claude-opus-5", "claude-fable-5"):
        mapped = meter.window_for_model(model_id)
        assert mapped == 1_000_000, model_id
        assert meter.band_for((tokens / mapped) * 100.0) == 80
    inflated = 2_000_000
    assert meter.band_for((tokens / inflated) * 100.0) is None


def test_explicit_window_wins_over_unknown_model() -> None:
    assert meter.resolve_window(window=1_000_000, model="mystery-model") == 1_000_000


def test_explicit_window_wins_over_env_and_map(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOALFLIGHT_CONTEXT_WINDOW", "200000")
    assert (
        meter.resolve_window(window=500_000, model="claude-opus-5") == 500_000
    )


def test_env_window_wins_over_model_map(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOALFLIGHT_CONTEXT_WINDOW", "200000")
    assert meter.resolve_window(model="claude-opus-5") == 200_000
    assert meter.context_window() == 200_000


def test_garbage_window_raises() -> None:
    with pytest.raises(ValueError, match="positive integer"):
        meter.context_window("nonsense")
    with pytest.raises(ValueError, match="positive integer"):
        meter.context_window("0")
    with pytest.raises(ValueError, match="positive integer"):
        meter.context_window("-5")


def test_window_env_override_is_honored_and_rejects_garbage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOALFLIGHT_CONTEXT_WINDOW", "1000000")
    assert meter.context_window() == 1_000_000
    monkeypatch.setenv("GOALFLIGHT_CONTEXT_WINDOW", "nonsense")
    with pytest.raises(ValueError):
        meter.context_window()


def test_widen_once_finds_usage_just_outside_64k(tmp_path: Path) -> None:
    path = _write_padded_transcript(
        tmp_path / "t.jsonl",
        prefix_bytes=1024,
        usage=_usage_record(400_000),
        suffix_bytes=meter.TAIL_BYTES + 4096,
    )
    usage, bytes_read = meter.read_newest_usage(path)
    assert usage is not None
    assert meter.context_tokens(usage) == 400_000
    assert bytes_read > meter.TAIL_BYTES
    assert bytes_read <= meter.TAIL_BYTES + meter.WIDEN_BYTES


def test_widen_then_give_up(tmp_path: Path) -> None:
    path = _write_padded_transcript(
        tmp_path / "t.jsonl",
        prefix_bytes=1024,
        usage=_usage_record(400_000),
        suffix_bytes=meter.WIDEN_BYTES + 8192,
    )
    usage, bytes_read = meter.read_newest_usage(path)
    assert usage is None
    assert bytes_read <= meter.TAIL_BYTES + meter.WIDEN_BYTES
    reading = meter.measure_transcript(path, WINDOW)
    assert reading.unknown is True
    assert reading.reason == "no-usage"


def test_truncated_usage_does_not_crash(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    path.write_bytes(b'{"type":"assistant","message":{"usage":{"input_tokens":')
    usage, _ = meter.read_newest_usage(path)
    assert usage is None


def test_cli_json_and_text(tmp_path: Path) -> None:
    path = _write_transcript(tmp_path / "t.jsonl", [_usage_record(0, cache_read=680_000)])
    text = _cli(tmp_path, ["--transcript", str(path), "--window", str(WINDOW)])
    assert text.returncode == 0, text.stderr
    assert text.stdout.strip() == "68% ok"
    blob = _cli(tmp_path, ["--transcript", str(path), "--window", str(WINDOW), "--json"])
    assert blob.returncode == 0, blob.stderr
    payload = json.loads(blob.stdout)
    assert payload["pct"] == 68.0
    assert payload["verdict"] == "ok"
    assert payload["unknown"] is False


def test_cli_unknown_model_prints_unknown_not_a_percent(tmp_path: Path) -> None:
    path = _write_transcript(tmp_path / "t.jsonl", [_usage_record(800_000)])
    proc = _cli(tmp_path, ["--transcript", str(path), "--model", "claude-opus-4-6"])
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "unknown"
    assert "%" not in proc.stdout


def test_transcript_model_resolves_window_without_export(tmp_path: Path) -> None:
    path = _write_transcript(
        tmp_path / "t.jsonl",
        [_usage_record(0, cache_read=680_000, model="claude-opus-5")],
    )
    reading = meter.measure_transcript(path)
    assert reading.unknown is False
    assert reading.window == 1_000_000
    assert reading.tokens == 680_000
    assert reading.pct == 68.0
    assert reading.verdict == "ok"


def test_unknown_transcript_model_is_silent(tmp_path: Path) -> None:
    path = _write_transcript(
        tmp_path / "t.jsonl",
        [_usage_record(800_000, model="mystery-model")],
    )
    reading = meter.measure_transcript(path)
    assert reading.unknown is True
    assert reading.pct is None
    assert reading.reason == "unrecognized-model"
    assert meter.render_text(reading) == "unknown"


def test_unmapped_variant_is_observable_not_a_percent(tmp_path: Path) -> None:
    path = _write_transcript(
        tmp_path / "t.jsonl",
        [_usage_record(800_000, model="claude-opus-5-mini")],
    )
    reading = meter.measure_transcript(path)
    assert reading.unknown is True
    assert reading.pct is None
    assert reading.reason == "unmapped-variant"
    assert reading.model == "claude-opus-5-mini"
    rendered = meter.render_text(reading)
    assert rendered == "unmapped-variant: claude-opus-5-mini"
    assert "%" not in rendered


def test_dated_transcript_model_resolves_without_export(tmp_path: Path) -> None:
    path = _write_transcript(
        tmp_path / "t.jsonl",
        [_usage_record(0, cache_read=680_000, model="claude-fable-5-20260817")],
    )
    reading = meter.measure_transcript(path)
    assert reading.unknown is False
    assert reading.window == 1_000_000
    assert reading.pct == 68.0


def test_explicit_unknown_model_does_not_fall_through_to_transcript(
    tmp_path: Path,
) -> None:
    path = _write_transcript(
        tmp_path / "t.jsonl",
        [_usage_record(800_000, model="claude-opus-5")],
    )
    reading = meter.measure_transcript(path, model="mystery-model")
    assert reading.unknown is True
    assert reading.reason == "unrecognized-model"
    assert reading.pct is None


def test_newest_assistant_model_wins(tmp_path: Path) -> None:
    path = _write_transcript(
        tmp_path / "t.jsonl",
        [
            _usage_record(100_000, model="mystery-model"),
            {"type": "user", "message": {"content": "later"}},
            _usage_record(250_000, model="claude-opus-5"),
        ],
    )
    reading = meter.measure_transcript(path)
    assert reading.window == 1_000_000
    assert reading.tokens == 250_000


def test_newest_unknown_model_is_silent_even_if_older_is_known(
    tmp_path: Path,
) -> None:
    path = _write_transcript(
        tmp_path / "t.jsonl",
        [
            _usage_record(100_000, model="claude-opus-5"),
            _usage_record(250_000, model="mystery-model"),
        ],
    )
    reading = meter.measure_transcript(path)
    assert reading.unknown is True
    assert reading.reason == "unrecognized-model"


def test_stale_window_guard_when_tokens_exceed_window(tmp_path: Path) -> None:
    path = _write_transcript(tmp_path / "t.jsonl", [_usage_record(1_200_000)])
    reading = meter.measure_transcript(path, 1_000_000)
    assert reading.unknown is True
    assert reading.reason == "stale-window"
    assert reading.verdict == "stale-window"
    assert reading.tokens == 1_200_000
    assert reading.window == 1_000_000
    assert reading.pct is None
    assert reading.band is None
    rendered = meter.render_text(reading)
    assert "%" not in rendered
    assert "stale-window" in rendered
    assert "1200000" in rendered
    assert "1000000" in rendered


def test_tokens_equal_to_window_is_100_not_stale(tmp_path: Path) -> None:
    path = _write_transcript(tmp_path / "t.jsonl", [_usage_record(1_000_000)])
    reading = meter.measure_transcript(path, 1_000_000)
    assert reading.unknown is False
    assert reading.pct == 100.0
    assert reading.reason is None
    assert reading.band == 95


def test_cli_uses_transcript_model_without_window(tmp_path: Path) -> None:
    path = _write_transcript(
        tmp_path / "t.jsonl",
        [_usage_record(0, cache_read=680_000, model="claude-opus-5")],
    )
    proc = _cli(tmp_path, ["--transcript", str(path)])
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "68% ok"
    blob = _cli(tmp_path, ["--transcript", str(path), "--json"])
    payload = json.loads(blob.stdout)
    assert payload["window"] == 1_000_000
    assert payload["pct"] == 68.0
    assert payload["unknown"] is False


def test_cli_env_overrides_transcript_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write_transcript(
        tmp_path / "t.jsonl",
        [_usage_record(100_000, model="claude-opus-5")],
    )
    proc = _cli(
        tmp_path,
        ["--transcript", str(path), "--json"],
        env={"GOALFLIGHT_CONTEXT_WINDOW": "200000"},
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["window"] == 200_000
    assert payload["pct"] == 50.0


def test_cli_stale_window_is_not_a_percent(tmp_path: Path) -> None:
    path = _write_transcript(tmp_path / "t.jsonl", [_usage_record(1_200_000)])
    proc = _cli(tmp_path, ["--transcript", str(path), "--window", "1000000"])
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().startswith("stale-window:")
    assert "%" not in proc.stdout


def test_cli_garbage_window_exits_2(tmp_path: Path) -> None:
    path = _write_transcript(tmp_path / "t.jsonl", [_usage_record(1)])
    proc = _cli(tmp_path, ["--transcript", str(path), "--window", "nope"])
    assert proc.returncode == 2
    assert "positive integer" in proc.stderr


def test_hook_silent_below_80(tmp_path: Path) -> None:
    path = _write_transcript(tmp_path / "t.jsonl", [_usage_record(790_000)])
    out = meter.hook_tick(
        {"transcript_path": str(path), "session_id": "s1"},
        window=WINDOW,
        state_path=tmp_path / "state.json",
    )
    assert out is None


def test_hook_silent_without_window(tmp_path: Path) -> None:
    path = _write_transcript(tmp_path / "t.jsonl", [_usage_record(900_000)])
    out = meter.hook_tick(
        {"transcript_path": str(path), "session_id": "s1"},
        state_path=tmp_path / "state.json",
    )
    assert out is None


def test_hook_silent_on_unrecognized_model(tmp_path: Path) -> None:
    path = _write_transcript(tmp_path / "t.jsonl", [_usage_record(900_000)])
    out = meter.hook_tick(
        {"transcript_path": str(path), "session_id": "s1", "model": "mystery"},
        state_path=tmp_path / "state.json",
    )
    assert out is None


def test_hook_uses_transcript_model_without_export(tmp_path: Path) -> None:
    path = _write_transcript(
        tmp_path / "t.jsonl",
        [_usage_record(820_000, model="claude-opus-5")],
    )
    out = meter.hook_tick(
        {"transcript_path": str(path), "session_id": "s1"},
        state_path=tmp_path / "state.json",
        today=dt.date(2026, 8, 17),
    )
    assert out is not None
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert ctx.startswith("CONTEXT 82% (band 80):")
    assert "RESUME-NOTES-2026-08-17.md" in ctx


def test_hook_unmapped_variant_fires_once(tmp_path: Path) -> None:
    path = _write_transcript(
        tmp_path / "t.jsonl",
        [_usage_record(800_000, model="claude-opus-5-mini")],
    )
    payload = {"transcript_path": str(path), "session_id": "s1"}
    state = tmp_path / "state.json"
    first = meter.hook_tick(payload, state_path=state)
    assert first is not None
    ctx = first["hookSpecificOutput"]["additionalContext"]
    assert "unmapped-variant" in ctx
    assert "claude-opus-5-mini" in ctx
    second = meter.hook_tick(payload, state_path=state)
    assert second is None


def test_hook_stale_window_fires_once_not_a_percent(tmp_path: Path) -> None:
    path = _write_transcript(tmp_path / "t.jsonl", [_usage_record(1_200_000)])
    payload = {"transcript_path": str(path), "session_id": "s1"}
    state = tmp_path / "state.json"
    first = meter.hook_tick(
        payload, window=1_000_000, state_path=state, today=dt.date(2026, 8, 17)
    )
    assert first is not None
    ctx = first["hookSpecificOutput"]["additionalContext"]
    assert "stale-window" in ctx
    assert "1200000" in ctx
    assert "%" not in ctx.split(":")[0]
    second = meter.hook_tick(payload, window=1_000_000, state_path=state)
    assert second is None


def test_band_80_fires_once_even_after_down_and_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_CONTEXT_METER_EVERY", "1")
    path = tmp_path / "t.jsonl"
    state = tmp_path / "state.json"
    payload = {"transcript_path": str(path), "session_id": "s1"}

    _write_transcript(path, [_usage_record(820_000)])
    first = meter.hook_tick(
        payload, window=WINDOW, state_path=state, today=dt.date(2026, 8, 17)
    )
    assert first is not None
    ctx = first["hookSpecificOutput"]["additionalContext"]
    assert first["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert ctx.startswith("CONTEXT 82% (band 80):")
    assert "RESUME-NOTES-2026-08-17.md" in ctx
    assert "directed compaction prompt" in ctx
    assert ctx.count("\n") == 0

    second = meter.hook_tick(payload, window=WINDOW, state_path=state)
    assert second is None

    _write_transcript(path, [_usage_record(700_000)])
    down = meter.hook_tick(payload, window=WINDOW, state_path=state)
    assert down is None

    _write_transcript(path, [_usage_record(820_000)])
    up_again = meter.hook_tick(payload, window=WINDOW, state_path=state)
    assert up_again is None

    _write_transcript(path, [_usage_record(910_000)])
    next_band = meter.hook_tick(payload, window=WINDOW, state_path=state)
    assert next_band is not None
    assert "band 90" in next_band["hookSpecificOutput"]["additionalContext"]


def test_jump_to_95_fires_once_at_highest_band(tmp_path: Path) -> None:
    path = _write_transcript(tmp_path / "t.jsonl", [_usage_record(960_000)])
    first = meter.hook_tick(
        {"transcript_path": str(path), "session_id": "s1"},
        window=WINDOW,
        state_path=tmp_path / "state.json",
    )
    assert first is not None
    assert "band 95" in first["hookSpecificOutput"]["additionalContext"]
    second = meter.hook_tick(
        {"transcript_path": str(path), "session_id": "s1"},
        window=WINDOW,
        state_path=tmp_path / "state.json",
    )
    assert second is None


def test_should_recheck_interval_and_growth() -> None:
    """Helper still encodes 20-call / 1MB. Shell now owns the call skip."""
    state = {"calls": 5, "last_checked_calls": 5, "last_checked_size": 100}
    assert meter.should_recheck(state, 100) is False
    state["calls"] = 25
    assert meter.should_recheck(state, 100) is True
    state["calls"] = 5
    assert (
        meter.should_recheck(state, 100 + meter.RECHECK_GROWTH_BYTES + 1) is True
    )


def test_hook_tick_measures_every_call_and_band_dedupes(tmp_path: Path) -> None:
    """Once Python is invoked, measure. Band de-dupe is what stays quiet."""
    path = _write_transcript(tmp_path / "t.jsonl", [_usage_record(820_000)])
    state = tmp_path / "state.json"
    payload = {"transcript_path": str(path), "session_id": "s1"}
    first = meter.hook_tick(payload, window=WINDOW, state_path=state)
    assert first is not None
    assert "band 80" in first["hookSpecificOutput"]["additionalContext"]

    _write_transcript(path, [_usage_record(930_000)])
    next_band = meter.hook_tick(payload, window=WINDOW, state_path=state)
    assert next_band is not None
    assert "band 90" in next_band["hookSpecificOutput"]["additionalContext"]

    again = meter.hook_tick(payload, window=WINDOW, state_path=state)
    assert again is None


def test_hook_main_fail_silent_on_garbage() -> None:
    proc = subprocess.run(
        [sys.executable, str(METER), "--hook"],
        cwd=ROOT,
        input="{not-json",
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert proc.returncode == 0
    assert proc.stdout == ""


def test_o1_bytes_read_and_time_do_not_scale_with_file_size(tmp_path: Path) -> None:
    usage = _usage_record(321_000)
    small = _write_padded_transcript(
        tmp_path / "small.jsonl",
        prefix_bytes=2 * 1024 * 1024,
        usage=usage,
    )
    large = _write_padded_transcript(
        tmp_path / "large.jsonl",
        prefix_bytes=40 * 1024 * 1024,
        usage=usage,
    )
    assert large.stat().st_size > 30 * 1024 * 1024

    t0 = time.perf_counter()
    small_usage, small_bytes = meter.read_newest_usage(small)
    small_s = time.perf_counter() - t0
    t1 = time.perf_counter()
    large_usage, large_bytes = meter.read_newest_usage(large)
    large_s = time.perf_counter() - t1

    assert small_usage is not None and large_usage is not None
    assert meter.context_tokens(small_usage) == 321_000
    assert meter.context_tokens(large_usage) == 321_000
    bound = meter.TAIL_BYTES + meter.WIDEN_BYTES
    assert small_bytes <= bound
    assert large_bytes <= bound
    assert large_bytes == small_bytes
    # Generous wall bound: a full 40MB scan is tens of ms+ and grows with
    # size; a tail read stays in the same small bucket as the 2MB file.
    assert large_s < 0.25
    print(
        f"MEASURE o1 small={small_s:.6f}s bytes={small_bytes} "
        f"size={small.stat().st_size} large={large_s:.6f}s "
        f"bytes={large_bytes} size={large.stat().st_size}"
    )


def test_model_resolution_does_not_increase_bytes_read(tmp_path: Path) -> None:
    """Adding model lookup must not turn the tail read into a scan."""
    usage = _usage_record(321_000, model="claude-opus-5")
    large = _write_padded_transcript(
        tmp_path / "large.jsonl",
        prefix_bytes=40 * 1024 * 1024,
        usage=usage,
    )
    assert large.stat().st_size > 30 * 1024 * 1024
    usage_obj, found_model, bytes_read = meter.read_newest_assistant(large)
    assert usage_obj is not None
    assert found_model == "claude-opus-5"
    assert bytes_read == meter.TAIL_BYTES
    assert bytes_read == 65_536
    reading = meter.measure_transcript(large)
    assert reading.unknown is False
    assert reading.window == 1_000_000
    assert reading.tokens == 321_000
    assert reading.bytes_read == meter.TAIL_BYTES
    print(
        f"MEASURE model-o1 bytes_read={reading.bytes_read} "
        f"size={large.stat().st_size} model={found_model}"
    )


@pytest.mark.skipif(os.name == "nt", reason="hook script is POSIX")
def test_hook_script_exists_and_is_executable() -> None:
    assert HOOK.is_file()
    assert os.access(HOOK, os.X_OK)


@pytest.mark.skipif(os.name == "nt", reason="hook script is POSIX")
def test_hook_script_emits_one_line_then_stays_quiet(tmp_path: Path) -> None:
    path = _write_transcript(tmp_path / "t.jsonl", [_usage_record(850_000)])
    payload = {
        "hook_event_name": "PostToolUse",
        "transcript_path": str(path),
        "session_id": "script-s1",
        "tool_name": "Bash",
    }
    env = _hook_env(tmp_path)
    first = _run_hook_script(tmp_path, payload, env)
    assert first.returncode == 0, first.stderr
    data = json.loads(first.stdout)
    assert data["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert "band 80" in data["hookSpecificOutput"]["additionalContext"]
    assert data["hookSpecificOutput"]["additionalContext"].count("\n") == 0

    second = _run_hook_script(tmp_path, payload, env)
    assert second.returncode == 0
    assert second.stdout == ""


@pytest.mark.skipif(os.name == "nt", reason="hook script is POSIX")
def test_hook_script_fail_silent_on_malformed_and_missing(tmp_path: Path) -> None:
    env = _hook_env(tmp_path)
    malformed = subprocess.run(
        [str(HOOK)],
        cwd=ROOT,
        env=env,
        input="{not-json",
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert malformed.returncode == 0
    assert malformed.stdout == ""
    assert malformed.stderr == ""

    missing = _run_hook_script(
        tmp_path,
        {"transcript_path": str(tmp_path / "nope.jsonl"), "session_id": "x"},
        env,
    )
    assert missing.returncode == 0
    assert missing.stdout == ""


def test_parseable_corrupt_calls_does_not_mute_95(tmp_path: Path) -> None:
    """Valid JSON with a non-int calls field must reset and still fire 95."""
    path = _write_transcript(tmp_path / "t.jsonl", [_usage_record(960_000)])
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"calls": "x"}) + "\n", encoding="utf-8")
    out = meter.hook_tick(
        {"transcript_path": str(path), "session_id": "s1"},
        window=WINDOW,
        state_path=state,
    )
    assert out is not None
    assert "band 95" in out["hookSpecificOutput"]["additionalContext"]
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["last_fired_band"] == 95
    assert isinstance(saved["calls"], int)


def test_out_of_range_last_fired_band_does_not_mute_95(tmp_path: Path) -> None:
    """A parseable last_fired_band of 999 must not silence the 95% cue forever."""
    path = _write_transcript(tmp_path / "t.jsonl", [_usage_record(960_000)])
    state = tmp_path / "state.json"
    state.write_text(
        json.dumps(
            {
                "calls": 4,
                "last_checked_calls": 4,
                "last_checked_size": 1,
                "last_fired_band": 999,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    out = meter.hook_tick(
        {"transcript_path": str(path), "session_id": "s1"},
        window=WINDOW,
        state_path=state,
    )
    assert out is not None
    assert "band 95" in out["hookSpecificOutput"]["additionalContext"]
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["last_fired_band"] == 95


def test_load_state_resets_out_of_contract_values(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"calls": "x", "last_fired_band": 999}) + "\n", encoding="utf-8")
    assert meter.load_state(path) == {}
    path.write_text("{not-json\n", encoding="utf-8")
    assert meter.load_state(path) == {}
    path.write_text(json.dumps({"calls": 3, "last_fired_band": 90}) + "\n", encoding="utf-8")
    loaded = meter.load_state(path)
    assert loaded["calls"] == 3
    assert loaded["last_fired_band"] == 90


def test_hooks_json_wires_post_tool_use() -> None:
    config = json.loads((ROOT / "hooks/hooks.json").read_text(encoding="utf-8"))
    entries = config["hooks"].get("PostToolUse", [])
    matched = []
    for entry in entries:
        commands = [hook.get("command", "") for hook in entry.get("hooks", [])]
        timeouts = [hook.get("timeout") for hook in entry.get("hooks", [])]
        if any("goalflight-context-meter.sh" in command for command in commands):
            matched.append((entry.get("matcher", ""), timeouts))
    assert matched, "PostToolUse must register the context-meter hook"
    assert any(timeouts and timeouts[0] == 5 for _matcher, timeouts in matched)
    pre = config["hooks"]["PreToolUse"]
    assert any(
        "goalflight-context-discipline.sh" in hook.get("command", "")
        for entry in pre
        for hook in entry.get("hooks", [])
    )


def _python_recorder(tmp_path: Path) -> tuple[Path, Path]:
    """A PATH shim that records python3 invocations instead of performing one.

    Counting interpreter STARTS is the measurement that matters here: the
    meter's own 20-call/1MB throttle runs inside Python, so it can only skip
    the tail read, never the startup it has already paid for.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    marker = tmp_path / "python3-started"
    shim = bin_dir / "python3"
    shim.write_text(f'#!/bin/sh\nprintf x >> "{marker}"\nexit 0\n', encoding="utf-8")
    shim.chmod(0o755)
    return bin_dir, marker


def _run_hook(
    tmp_path: Path,
    bin_dir: Path | None,
    *,
    window: str | None = None,
    extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.pop("GOALFLIGHT_CONTEXT_WINDOW", None)
    env.pop("GOALFLIGHT_CONTEXT_METER", None)
    env.pop("GOALFLIGHT_CONTEXT_METER_EVERY", None)
    if window is not None:
        env["GOALFLIGHT_CONTEXT_WINDOW"] = window
    env["GOALFLIGHT_CONTEXT_METER_STATE_DIR"] = str(tmp_path / "meter-state-dir")
    env["GOALFLIGHT_CONTEXT_METER_STATE"] = str(tmp_path / "meter-state.json")
    if bin_dir is not None:
        env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    if extra:
        env.update(extra)
    return subprocess.run(
        ["sh", str(HOOK)],
        input=json.dumps({"transcript_path": str(tmp_path / "missing.jsonl")}),
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )


def test_hook_does_not_start_python_when_opted_out(tmp_path: Path) -> None:
    bin_dir, marker = _python_recorder(tmp_path)
    _run_hook(tmp_path, bin_dir, extra={"GOALFLIGHT_CONTEXT_METER": "0"})
    assert not marker.exists(), "opt-out must skip the interpreter"


def test_hook_starts_python_without_window_on_first_call(tmp_path: Path) -> None:
    """Default-on: the first tool call must measure even with no export."""
    bin_dir, marker = _python_recorder(tmp_path)
    _run_hook(tmp_path, bin_dir, window=None)
    assert marker.exists(), "first call must start python without GOALFLIGHT_CONTEXT_WINDOW"


def test_hook_starts_python_when_window_is_set(tmp_path: Path) -> None:
    bin_dir, marker = _python_recorder(tmp_path)
    _run_hook(tmp_path, bin_dir, window=str(WINDOW))
    assert marker.exists(), "hook must still run the meter when a window is configured"


def test_hook_shell_throttle_skips_python_on_non_sampled_calls(tmp_path: Path) -> None:
    bin_dir, marker = _python_recorder(tmp_path)
    extra = {"GOALFLIGHT_CONTEXT_METER_EVERY": "20"}
    _run_hook(tmp_path, bin_dir, extra=extra)
    assert marker.exists(), "call 1 must be sampled"
    marker.unlink()
    for _ in range(19):
        _run_hook(tmp_path, bin_dir, extra=extra)
        assert not marker.exists(), "calls 2-20 must not start python"
    _run_hook(tmp_path, bin_dir, extra=extra)
    assert marker.exists(), "call 21 must be sampled again"


def test_hook_throttled_path_stays_cheap(tmp_path: Path) -> None:
    """Shell skip must not regress to a ~43ms python start per tool call.

    The number that matters is wall time of the hook process Claude
    already launches. A bare ``sh -c exit`` is the floor; spawning the
    meter used to cost ~43ms. Stay well below that.
    """
    extra = {"GOALFLIGHT_CONTEXT_METER_EVERY": "20"}
    _run_hook(tmp_path, None, extra=extra)
    n = 40
    t0 = time.perf_counter()
    for _ in range(n):
        _run_hook(tmp_path, None, extra=extra)
    hook_per = (time.perf_counter() - t0) / n
    t1 = time.perf_counter()
    for _ in range(n):
        subprocess.run(["sh", "-c", "exit 0"], check=False, capture_output=True)
    bare_per = (time.perf_counter() - t1) / n
    print(
        f"MEASURE hook-throttled per-call={hook_per * 1000:.3f}ms "
        f"bare-sh={bare_per * 1000:.3f}ms n={n}"
    )
    assert hook_per < 0.030, f"throttled hook path too slow: {hook_per * 1000:.3f}ms"
