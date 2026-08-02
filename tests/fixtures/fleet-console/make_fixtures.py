#!/usr/bin/env python3
"""Generate fleet-console renderer fixtures for manual/browser UI testing.

The fleet projection (``goalflight_fleet_console.py fleet``) is currently
blocked by b-096 (hangs, 180 s timeout, zero bytes on stdout and stderr), so
renderer testing cannot use live data.  These fixtures are SYNTHETIC.

They are deliberately published through the real
``goalflight_fleet_console.publish_plane`` so that the bytes on disk are
identical to genuine projection output:

* the payload is validated against ``FLEET_FIELD_ALLOWLIST`` /
  ``ATTENTION_FIELD_ALLOWLIST`` (a fixture that drifts off-schema fails here
  rather than silently testing a shape the producer cannot emit), and
* it is written through ``goalflight_status.write_script_data_js``, which is
  the ``<script src>`` escaping boundary under test.

Timestamps are generated relative to run time, so ``current`` scenarios are
genuinely fresh every run and ``stale`` is genuinely several cadences old.

Usage:
    python3 make_fixtures.py --out-dir <dir> [--scenario <name>]
"""

from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_PROJECTION = REPO_ROOT / "scripts" / "goalflight_fleet_console.py"

fc = None  # bound by load_projection()


def load_projection(path: Path, deps_dir: Path | None = None):
    """Import the projection module under test from an explicit path.

    The allowlists ARE the schema, and they are still moving (the field pair
    ``projects_total`` / ``projects_sampled`` was renamed to ``registry_total``
    / ``registry_deep_sampled`` mid-test-run).  Binding fixtures to a named
    projection file -- rather than to whatever happens to be importable -- is
    what makes a fixture's validation result attributable to a known schema.
    """
    global fc
    path = Path(path).resolve()
    # The projection imports sibling goalflight_* modules. When the file under
    # test is a pinned snapshot outside the tree, its siblings still have to
    # resolve, so search a deps directory as well as the file's own directory.
    sys.path.insert(0, str(path.parent))
    if deps_dir is not None:
        sys.path.insert(1, str(Path(deps_dir).resolve()))
    spec = importlib.util.spec_from_file_location("goalflight_fleet_console", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load projection: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fc = module
    return module

FLEET_SCHEMA = "goalflight.fleet-console.fleet.v1"
ATTENTION_SCHEMA = "goalflight.fleet-console.attention.v1"

# Hostile strings. Worker-authored text reaches this page, so these are the
# shapes a malicious or merely careless worker can put into marker text,
# dispatch ids and project names.
XSS_SCRIPT_BREAKOUT = '</script><script>window.__GF_PWNED_SCRIPT=1;</script>'
XSS_IMG_ONERROR = '<img src=x onerror="window.__GF_PWNED_IMG=1">'
XSS_SVG_ONLOAD = "<svg/onload=window.__GF_PWNED_SVG=1>"
# No spaces and no '/' -- '/' would trip the projection's absolute-path denial,
# which is a producer-side control, not the layout property under test here.
LONG_UNBROKEN = "Zq7" + ("kQ9wZ2mX4vB8nR6tY1uP3sD5fG0hJ" * 14) + "End"
QUOTE_SOUP = "quote\" apos' amp& lt< gt> backtick` brace{} u2028 tail"


def _iso(offset_s: float) -> str:
    moment = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=offset_s)
    return moment.isoformat(timespec="seconds")


def _worker(
    dispatch_id: str,
    *,
    agent: str = "codex",
    classification: str = "expected_live",
    liveness: str = "alive",
    alive: bool | None = True,
    started_s: float = -420,
    ended_s: float | None = None,
    engine: str = "gpt-5.6-codex",
    shape: str = "acp",
    transport: str = "acp",
    sandbox: str = "workspace-write",
    state: str = "running",
    terminal: str | None = None,
) -> dict:
    return {
        "dispatch_id": dispatch_id,
        "agent": agent,
        "engine": engine,
        "shape": shape,
        "transport": transport,
        "os_sandbox": sandbox,
        "state": state,
        "classification": classification,
        "terminal_state": terminal,
        "liveness_state": liveness,
        "worker_alive": alive,
        "started_at": _iso(started_s),
        "ended_at": None if ended_s is None else _iso(ended_s),
    }


def _envelope(schema: str, plane: str, *, age_s: float, generation: str) -> dict:
    """Plane envelope fields shared by both allowlists.

    ``age_s`` is how long ago the sample finished; 0 means 'just now'.
    """
    return {
        "schema": schema,
        "generation_id": generation,
        "sample_started_at": _iso(-age_s - 3),
        "sample_finished_at": _iso(-age_s),
        "last_success_at": _iso(-age_s),
        "producer": {"name": "goalflight_fleet_console", "plane": plane},
        "last_error": None,
    }


def _machine() -> dict:
    return {
        "operating_cap": 75,
        "active_leases": 11,
        "local_workers": 4,
        "queue_depth": 6,
        "rate_pressure": [
            {"provider": "grok", "scope": "machine", "count": 2},
        ],
        "warnings": [
            {"code": "worker_dead_no_terminal_marker", "severity": "warn", "count": 3},
        ],
    }


def _queue(depth: int = 4, agent: str = "codex", age_s: float = -1900) -> dict:
    return {
        "depth": depth,
        "lanes": [
            {"agent": agent, "count": depth},
            {"agent": "grok", "count": 2},
        ],
        "oldest_created_at": _iso(age_s),
    }


def _vendors() -> list[dict]:
    return [
        {
            "provider": "codex",
            "seat_index": 0,
            "remaining": "71%",
            "reset_at": _iso(3600 * 20),
            "flags": ["active"],
        },
        {
            "provider": "codex",
            "seat_index": 1,
            "remaining": "8%",
            "reset_at": _iso(3600 * 51),
            "flags": ["near-exhaustion"],
        },
        {
            "provider": "grok",
            "seat_index": 0,
            "remaining": "94%",
            "reset_at": _iso(3600 * 6),
            "flags": [],
        },
    ]


def _remote() -> dict:
    return {
        "available": True,
        "nodes": [
            {"node_id": "mac-studio-256-1", "dispatches": 3, "auth_states": ["ok"]},
            {"node_id": "mac-studio-256-2", "dispatches": 1, "auth_states": ["ok"]},
            {"node_id": "mac-studio-256-3", "dispatches": 0, "auth_states": ["unknown"]},
        ],
        "workers": [
            {
                "dispatch_id": "d-remote-3311",
                "node_id": "mac-studio-256-1",
                "state": "running",
                "quarantine_reason": None,
                "ssh_reachable": True,
                "may_release": False,
            },
            {
                "dispatch_id": "d-remote-3312",
                "node_id": "mac-studio-256-3",
                "state": "unknown",
                "quarantine_reason": "ssh_unreachable",
                "ssh_reachable": False,
                "may_release": True,
            },
        ],
    }


def _project(
    project_id: str,
    name: str,
    workers: list[dict],
    *,
    registered: bool | None = True,
    queue: dict | None = None,
) -> dict:
    return {
        "project_id": project_id,
        "name": name,
        "registered": registered,
        "last_seen": _iso(-95),
        "skill_version": "0.42.1",
        "queue": queue if queue is not None else _queue(),
        "session": {
            "available": True,
            "active": True,
            "queue_state": "executing",
            "queue_last_touched": _iso(-140),
            "active_leases": 3,
        },
        "milestone": {
            "available": True,
            "active_cadence": True,
            "commits_since": 7,
            "cadence": 10,
            "due": False,
        },
        "workers": workers,
    }


# --------------------------------------------------------------------------
# scenarios
# --------------------------------------------------------------------------

def scenario_current() -> tuple[dict, dict]:
    """Fresh, populated. Baseline for theme, width, focus and motion checks."""
    fleet = _envelope(FLEET_SCHEMA, "fleet", age_s=12, generation="gen-fleet-0001")
    fleet.update(
        {
            "registry_total": 3,
            "registry_deep_sampled": 2,
            "machine": _machine(),
            "vendors": _vendors(),
            "remote": _remote(),
            "projects": [
                _project(
                    "p-goal-flight",
                    "goal-flight",
                    [
                        _worker("d-4401", agent="codex"),
                        _worker(
                            "d-4402",
                            agent="grok",
                            engine="grok-code",
                            shape="bash-tail",
                            transport="bash-tail",
                            classification="running_user_confirm",
                            liveness="alive",
                            state="user-confirm",
                            started_s=-2540,
                        ),
                        _worker(
                            "d-4403",
                            agent="cursor",
                            engine="kimi-k3-high",
                            classification="complete",
                            liveness="exited",
                            alive=False,
                            state="done",
                            terminal="COMPLETE",
                            started_s=-5100,
                            ended_s=-240,
                        ),
                    ],
                ),
                _project(
                    "p-kiln",
                    "kiln",
                    [
                        _worker(
                            "d-4501",
                            agent="codex",
                            classification="worker_dead",
                            liveness="dead",
                            alive=False,
                            state="unknown",
                            sandbox="read-only",
                            started_s=-8800,
                            ended_s=-600,
                        ),
                    ],
                ),
            ],
            "unassigned_workers": [
                _worker(
                    "d-9901",
                    agent="grok",
                    classification="unknown_no_pid",
                    liveness="unknown",
                    alive=None,
                    state="unknown",
                    started_s=-1800,
                ),
            ],
        }
    )

    attention = _envelope(ATTENTION_SCHEMA, "attention", age_s=3, generation="gen-attn-0001")
    attention.update(
        {
            "age_granularity": "minute",
            "items": [
                {
                    "dispatch_id": "d-4402",
                    "seq": 118,
                    "kind": "user_confirm",
                    "action": "Review",
                    "observed_at": _iso(-2400),
                    "headline": "Worker asked to run a command outside the sandbox",
                },
                {
                    "dispatch_id": "d-4501",
                    "seq": 121,
                    "kind": "blocked",
                    "action": "Salvage",
                    "observed_at": _iso(-620),
                    "headline": "No terminal marker and no live process",
                },
                {
                    "dispatch_id": "d-4407",
                    "seq": 124,
                    "kind": "user_need",
                    "action": "Review",
                    "observed_at": _iso(-95),
                    "headline": "Needs a decision on the migration ordering",
                },
            ],
        }
    )
    return fleet, attention


def scenario_stale() -> tuple[dict, dict]:
    """Both planes set back several cadences.

    Fleet cadence is 60 s and attention 5 s. These ages are ~7 fleet cadences
    and ~150 attention cadences -- far past 'a couple of missed sweeps'. The
    numbers in the body are the same ones the ``current`` fixture shows, so if
    the page renders them as live it is presenting old data as current.
    """
    fleet, attention = scenario_current()
    fleet.update(
        _envelope(FLEET_SCHEMA, "fleet", age_s=430, generation="gen-fleet-0007")
    )
    attention.update(
        _envelope(ATTENTION_SCHEMA, "attention", age_s=760, generation="gen-attn-0031")
    )
    return fleet, attention


def scenario_stale_mild() -> tuple[dict, dict]:
    """Just over two missed cadences on each plane.

    The design doc's rule is 'after ~2 missed cadences replace semantic states
    with STALE'. This fixture sits at ~2.5 fleet cadences (150 s) and ~2.5
    attention cadences (12 s) -- the first point at which the doc says the
    console must stop asserting the numbers are current.
    """
    fleet, attention = scenario_current()
    fleet.update(
        _envelope(FLEET_SCHEMA, "fleet", age_s=150, generation="gen-fleet-0003")
    )
    attention.update(
        _envelope(ATTENTION_SCHEMA, "attention", age_s=12, generation="gen-attn-0009")
    )
    return fleet, attention


def scenario_empty() -> tuple[dict, dict]:
    """Fresh sample, nothing needs the operator, no projects in flight."""
    fleet = _envelope(FLEET_SCHEMA, "fleet", age_s=8, generation="gen-fleet-0100")
    fleet.update(
        {
            "registry_total": 0,
            "registry_deep_sampled": 0,
            "machine": {
                "operating_cap": 75,
                "active_leases": 0,
                "local_workers": 0,
                "queue_depth": 0,
                "rate_pressure": [],
                "warnings": [],
            },
            "vendors": [],
            "remote": {"available": True, "nodes": [], "workers": []},
            "projects": [],
            "unassigned_workers": [],
        }
    )
    attention = _envelope(ATTENTION_SCHEMA, "attention", age_s=2, generation="gen-attn-0100")
    attention.update({"age_granularity": "minute", "items": []})
    return fleet, attention


def scenario_hostile() -> tuple[dict, dict]:
    """Worker-authored hostile text in marker text, dispatch ids, project names."""
    fleet = _envelope(FLEET_SCHEMA, "fleet", age_s=9, generation="gen-fleet-" + XSS_IMG_ONERROR)
    fleet.update(
        {
            "registry_total": 2,
            "registry_deep_sampled": 2,
            "machine": _machine(),
            "vendors": [
                {
                    "provider": XSS_SVG_ONLOAD,
                    "seat_index": 0,
                    "remaining": LONG_UNBROKEN,
                    "reset_at": _iso(3600),
                    "flags": [XSS_IMG_ONERROR, LONG_UNBROKEN],
                }
            ],
            "remote": {
                "available": True,
                "nodes": [
                    {
                        "node_id": XSS_SCRIPT_BREAKOUT,
                        "dispatches": 1,
                        "auth_states": [XSS_IMG_ONERROR],
                    }
                ],
                "workers": [
                    {
                        "dispatch_id": LONG_UNBROKEN,
                        "node_id": XSS_SVG_ONLOAD,
                        "state": QUOTE_SOUP,
                        "quarantine_reason": XSS_SCRIPT_BREAKOUT,
                        "ssh_reachable": False,
                        "may_release": True,
                    }
                ],
            },
            "projects": [
                _project(
                    XSS_SCRIPT_BREAKOUT,
                    XSS_IMG_ONERROR,
                    queue=_queue(depth=3, agent=XSS_IMG_ONERROR),
                    workers=[
                        _worker(
                            XSS_SCRIPT_BREAKOUT,
                            agent=XSS_IMG_ONERROR,
                            engine=XSS_SVG_ONLOAD,
                            shape=QUOTE_SOUP,
                            transport=LONG_UNBROKEN,
                            sandbox=XSS_IMG_ONERROR,
                            state=XSS_SCRIPT_BREAKOUT,
                            classification=XSS_IMG_ONERROR,
                            terminal=XSS_SVG_ONLOAD,
                            liveness=LONG_UNBROKEN,
                        ),
                    ],
                ),
                _project(
                    "p-longname",
                    LONG_UNBROKEN,
                    [_worker(LONG_UNBROKEN, agent=LONG_UNBROKEN, engine=LONG_UNBROKEN)],
                    registered=None,
                ),
            ],
            "unassigned_workers": [],
        }
    )

    attention = _envelope(
        ATTENTION_SCHEMA, "attention", age_s=4, generation="gen-attn-" + XSS_SCRIPT_BREAKOUT
    )
    attention.update(
        {
            "age_granularity": "minute",
            "items": [
                {
                    "dispatch_id": XSS_SCRIPT_BREAKOUT,
                    "seq": 1,
                    "kind": XSS_IMG_ONERROR,
                    "action": XSS_SVG_ONLOAD,
                    "observed_at": _iso(-900),
                    "headline": XSS_SCRIPT_BREAKOUT + " " + XSS_IMG_ONERROR,
                },
                {
                    "dispatch_id": LONG_UNBROKEN,
                    "seq": 2,
                    "kind": "user_need",
                    "action": LONG_UNBROKEN,
                    "observed_at": _iso(-120),
                    "headline": LONG_UNBROKEN,
                },
                {
                    "dispatch_id": "d-quote",
                    "seq": 3,
                    "kind": "blocked",
                    "action": "Salvage",
                    "observed_at": _iso(-60),
                    "headline": QUOTE_SOUP,
                },
            ],
        }
    )
    return fleet, attention


def scenario_join() -> tuple[dict, dict]:
    """Cross-plane disagreement.

    The attention plane references dispatches the fleet plane does not contain,
    and the two planes carry generation ids from different sweeps. Per the
    design doc the planes are published independently and must be joined
    tolerantly -- this must degrade, not throw.
    """
    fleet, attention = scenario_current()
    fleet["generation_id"] = "gen-fleet-0042"
    attention["generation_id"] = "gen-attn-9999-divergent"
    attention["items"] = [
        {
            "dispatch_id": "d-does-not-exist-in-fleet",
            "seq": 500,
            "kind": "user_confirm",
            "action": "Review",
            "observed_at": _iso(-1500),
            "headline": "Dispatch absent from the fleet generation",
        },
        {
            "dispatch_id": None,
            "seq": None,
            "kind": None,
            "action": None,
            "observed_at": None,
            "headline": None,
        },
        {
            "dispatch_id": "d-4402",
            "seq": 501,
            "kind": "user_need",
            "action": "Review",
            "observed_at": "not-a-timestamp",
            "headline": "Unparseable envelope time",
        },
    ]
    return fleet, attention


def scenario_null() -> tuple[None, None]:
    """Both globals null -- the state the repo mirrors ship in."""
    return None, None


SCENARIOS = {
    "current": scenario_current,
    "stale": scenario_stale,
    "stale-mild": scenario_stale_mild,
    "empty": scenario_empty,
    "hostile": scenario_hostile,
    "join": scenario_join,
    "null": scenario_null,
}


def write_scenario(name: str, out_dir: Path) -> None:
    fleet, attention = SCENARIOS[name]()
    target = out_dir / name
    target.mkdir(parents=True, exist_ok=True)
    for payload, plane, filename, global_name in (
        (fleet, "fleet", "fleet-data.js", "GF_FLEET"),
        (attention, "attention", "attention-data.js", "GF_ATTENTION"),
    ):
        path = target / filename
        if payload is None:
            path.write_text(f"window.{global_name} = null;\n", encoding="utf-8")
            continue
        # Validates against the allowlist and writes through the real
        # script-include escaping boundary.
        fc.publish_plane(path, payload, plane)
    print(f"wrote {name} -> {target}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--scenario", action="append", choices=sorted(SCENARIOS))
    parser.add_argument(
        "--projection",
        type=Path,
        default=DEFAULT_PROJECTION,
        help="projection module whose allowlists validate these fixtures",
    )
    parser.add_argument(
        "--deps-dir",
        type=Path,
        default=DEFAULT_PROJECTION.parent,
        help="directory holding the projection's sibling goalflight_* modules",
    )
    args = parser.parse_args(argv)
    module = load_projection(args.projection, args.deps_dir)
    print(f"validating against {args.projection}")
    print(f"  fleet allowlist keys: {sorted(module.FLEET_FIELD_ALLOWLIST)}")
    for name in args.scenario or sorted(SCENARIOS):
        write_scenario(name, args.out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
