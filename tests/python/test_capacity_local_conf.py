"""Machine-local capacity override conf (capacity.local.json).

Per-machine concurrency tuning must live in a gitignored local conf, not in the
committed baseline (which would export one operator's settings to every user of
the skill). These cases verify that:

  - an absent/malformed conf degrades to the committed baseline (never raises),
  - a present conf merges agent_caps/agent_rss_mb over the baseline,
  - hard_cap + operating_total flow through goalflight_capacity, and the explicit
    GOALFLIGHT_CAPACITY_MAX_TOTAL env var still wins over the conf.

Each case drives GOALFLIGHT_CAPACITY_CONF at a temp path and reloads the leaf
module, so nothing here reads the operator's real ~/.goal-flight/capacity.local.json.
Repo convention: case_* functions invoked by main(), run as `python <file>.py`.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import goalflight_agent_limits as limits  # noqa: E402


def _reload_limits(conf_path: str | None):
    if conf_path is None:
        # A guaranteed-unique path that does NOT exist: mkstemp then unlink, so
        # the loader falls back to the committed baseline hermetically (never
        # races a stale fixed-name file).
        fd, absent = tempfile.mkstemp(prefix="capacity-absent-", suffix=".json")
        os.close(fd)
        os.unlink(absent)
        os.environ["GOALFLIGHT_CAPACITY_CONF"] = absent
    else:
        os.environ["GOALFLIGHT_CAPACITY_CONF"] = conf_path
    return importlib.reload(limits)


def _write_conf(body: str) -> str:
    fd, path = tempfile.mkstemp(prefix="capacity-local-", suffix=".json")
    with os.fdopen(fd, "w") as fh:
        fh.write(body)
    return path


def case_absent_conf_keeps_committed_baseline() -> None:
    mod = _reload_limits(None)
    assert mod.LOCAL_OVERRIDES == {}, mod.LOCAL_OVERRIDES
    assert mod.DEFAULT_AGENT_CAPS["grok"] == 30
    assert mod.DEFAULT_AGENT_CAPS["codex"] == 18
    assert mod.local_hard_cap(40) == 40
    assert mod.local_operating_total() is None


def case_conf_overrides_merge_over_baseline() -> None:
    path = _write_conf(
        json.dumps(
            {
                "hard_cap": 75,
                "operating_total": 75,
                "agent_caps": {"grok": 60, "codex": 15, "codex-acp": 15},
                "agent_rss_mb": {"grok": 250},
            }
        )
    )
    try:
        mod = _reload_limits(path)
        assert mod.DEFAULT_AGENT_CAPS["grok"] == 60
        assert mod.DEFAULT_AGENT_CAPS["codex"] == 15
        assert mod.DEFAULT_AGENT_CAPS["codex-acp"] == 15
        # A cap not named in the conf keeps its baseline.
        assert mod.DEFAULT_AGENT_CAPS["claude"] == 5
        assert mod.AGENT_RSS_MB["grok"] == 250
        assert mod.local_hard_cap(40) == 75
        assert mod.local_operating_total() == 75
    finally:
        os.unlink(path)


def case_conf_operating_cap_flows_through_capacity() -> None:
    path = _write_conf(json.dumps({"hard_cap": 75, "operating_total": 75}))
    old_env = os.environ.pop("GOALFLIGHT_CAPACITY_MAX_TOTAL", None)
    try:
        _reload_limits(path)
        import goalflight_capacity as cap

        cap = importlib.reload(cap)
        assert cap.DEFAULT_HARD_CAP == 75
        # >64GB box: conf operating_total governs; hard_cap lifts the raw ceiling.
        assert cap.operating_cap_for_ram(128 * 1024, raw_ceiling=75) == 75
        # Explicit env override still wins over the conf.
        os.environ["GOALFLIGHT_CAPACITY_MAX_TOTAL"] = "12"
        assert cap.operating_cap_for_ram(128 * 1024, raw_ceiling=75) == 12
    finally:
        os.environ.pop("GOALFLIGHT_CAPACITY_MAX_TOTAL", None)
        if old_env is not None:
            os.environ["GOALFLIGHT_CAPACITY_MAX_TOTAL"] = old_env
        os.unlink(path)
        _reload_limits(None)
        import goalflight_capacity as cap

        importlib.reload(cap)


def case_malformed_conf_degrades_to_baseline() -> None:
    path = _write_conf("{ not valid json ]")
    try:
        mod = _reload_limits(path)
        assert mod.LOCAL_OVERRIDES == {}
        assert mod.DEFAULT_AGENT_CAPS["grok"] == 30
    finally:
        os.unlink(path)


def case_non_positive_and_nonint_cap_values_ignored() -> None:
    path = _write_conf(
        json.dumps({"agent_caps": {"grok": 0, "codex": "lots", "claude": 7}})
    )
    try:
        mod = _reload_limits(path)
        assert mod.DEFAULT_AGENT_CAPS["grok"] == 30  # 0 rejected
        assert mod.DEFAULT_AGENT_CAPS["codex"] == 18  # non-int rejected
        assert mod.DEFAULT_AGENT_CAPS["claude"] == 7  # valid applied
    finally:
        os.unlink(path)


def main() -> None:
    try:
        case_absent_conf_keeps_committed_baseline()
        case_conf_overrides_merge_over_baseline()
        case_conf_operating_cap_flows_through_capacity()
        case_malformed_conf_degrades_to_baseline()
        case_non_positive_and_nonint_cap_values_ignored()
    finally:
        # Restore baseline module state for any later import in the same process.
        _reload_limits(None)
    print("test_capacity_local_conf: all cases passed")


if __name__ == "__main__":
    main()
