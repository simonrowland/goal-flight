#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

python3 "$REPO_ROOT/scripts/goalflight_validate_adapters.py" >/tmp/goal-flight-adapters-$$.out
grep -q "schema_validates=13/13" /tmp/goal-flight-adapters-$$.out || {
  cat /tmp/goal-flight-adapters-$$.out
  rm -f /tmp/goal-flight-adapters-$$.out
  exit 1
}
rm -f /tmp/goal-flight-adapters-$$.out

python3 - "$REPO_ROOT" <<'PY'
import copy
import json
import shutil
import sys
import tempfile
from pathlib import Path

repo = Path(sys.argv[1])
sys.path.insert(0, str(repo / "scripts"))

from goalflight_adapter_gate import validate_adapter_gate
from goalflight_validate_adapters import (
    validate_manifest,
    validate_no_host_tool_leaks,
)

schema = json.loads((repo / "adapters" / "agent-adapter.schema.json").read_text())


def load(name):
    return json.loads((repo / "adapters" / f"{name}.json").read_text())


codex = load("codex")


def expect_error(label, manifest, needle):
    errors = validate_manifest(manifest, schema, source=f"{label}.json", repo_root=repo)
    if not any(needle in error for error in errors):
        raise SystemExit(f"{label}: expected error containing {needle!r}; got {errors}")


bad = copy.deepcopy(codex)
del bad["support"]["worker"]
expect_error("missing-worker-schema-negative", bad, "missing required property worker")

errors = validate_manifest([], schema, source="non-object.json", repo_root=repo)
if not any("expected type" in error for error in errors):
    raise SystemExit(f"non-object manifest should return schema errors; got {errors}")

bad = copy.deepcopy(codex)
bad["discovery"]["probes"][0]["model_consuming"] = True
expect_error("model-consuming-probe", bad, "model-consuming")

bad = copy.deepcopy(codex)
bad["discovery"]["probes"][0]["network"] = True
expect_error("undeclared-network-probe", bad, "undeclared network")

bad = copy.deepcopy(codex)
bad["discovery"]["probes"][0]["argv"] = ["npm", "update", "codex"]
expect_error("update-probe", bad, "update/package-registry-like")

bad = copy.deepcopy(codex)
bad["discovery"]["budget"]["max_path_probes"] = 0
expect_error("unbounded-probe-budget", bad, "exceeds discovery budget")

bad = copy.deepcopy(codex)
bad["permission_surface"]["plugin_sandbox"]["mode"] = "broad"
bad["permission_surface"]["plugin_sandbox"]["declared_permissions"] = []
expect_error("undeclared-broad-plugin-sandbox", bad, "broad plugin sandbox")

bad = copy.deepcopy(codex)
bad["permission_surface"]["auto_approve_detection"]["strict_fail"] = False
expect_error("silent-auto-approve", bad, "strict_fail")

bad = copy.deepcopy(codex)
bad["invocation"]["exec"]["args"].append("--yolo")
expect_error("forbidden-invocation-arg", bad, "forbidden arg")

for token in ("--no-sandbox=true", "--auto-approve=true", "--sandbox-disable=1", "--sandbox=danger-full-access"):
    bad = copy.deepcopy(codex)
    bad["invocation"]["exec"]["args"].append(token)
    expect_error(f"forbidden-invocation-arg-value-{token}", bad, "forbidden arg")

tmp = Path(tempfile.mkdtemp(prefix="goal-flight-no-leak-"))
try:
    (tmp / "docs").mkdir()
    (tmp / "SKILL.md").write_text(
        "---\n"
        "allowed-tools:\n"
        "  - Skill\n"
        "  - AskUserQuestion\n"
        "---\n"
        "Portable wrapper body.\n"
    )
    (tmp / "docs" / "neutral.md").write_text("Do not mention functions.exec_command here.\n")
    (tmp / "docs" / "claude.md").write_text("Use AskUserQuestion here.\n")
    (tmp / "docs" / "raw-skill.md").write_text('Use Skill(skill: "review", args: "...") here.\n')
    (tmp / "docs" / "raw-agent.md").write_text("Use the Agent tool here.\n")
    errors = validate_no_host_tool_leaks(tmp)
    for needle in ("functions.exec_command", "AskUserQuestion", "Skill(", "Agent tool"):
        if not any(needle in error for error in errors):
            raise SystemExit(f"no-leak negative expected {needle!r}; got {errors}")
    if any(error.startswith("SKILL.md:") for error in errors):
        raise SystemExit(f"SKILL.md allowed-tools frontmatter should not leak; got {errors}")
finally:
    shutil.rmtree(tmp)

tmp_repo = Path(tempfile.mkdtemp(prefix="goal-flight-packaging-"))
try:
    (tmp_repo / "wrappers").mkdir()
    (tmp_repo / "wrappers" / "unsafe.sh").write_text("codex exec --sandbox-disable=1\n")
    bad = copy.deepcopy(codex)
    bad["packaging"]["checked_in_wrappers"] = ["wrappers/"]
    bad["packaging"]["generated_outputs"] = []
    errors = validate_manifest(bad, schema, source="codex.json", repo_root=tmp_repo)
    if not any("packaging.checked_in_wrappers" in error and "--sandbox-disable=1" in error for error in errors):
        raise SystemExit(f"packaging wrapper dir expected forbidden arg failure; got {errors}")

    (tmp_repo / "dist" / "codex").mkdir(parents=True)
    (tmp_repo / "dist" / "codex" / "generated.sh").write_text("codex exec --auto-approve=true\n")
    bad = copy.deepcopy(codex)
    bad["packaging"]["checked_in_wrappers"] = []
    bad["packaging"]["generated_outputs"] = ["dist/codex/"]
    errors = validate_manifest(bad, schema, source="codex.json", repo_root=tmp_repo)
    if not any("packaging.generated_outputs" in error and "--auto-approve=true" in error for error in errors):
        raise SystemExit(f"generated output dir expected forbidden arg failure; got {errors}")
finally:
    shutil.rmtree(tmp_repo)


def expect_denied(label, adapter, expected_reason, **kwargs):
    result = validate_adapter_gate(adapter, **kwargs)
    if result["allowed"]:
        raise SystemExit(f"{label}: expected denied, got {result}")
    if result["reason"] != expected_reason:
        raise SystemExit(f"{label}: expected {expected_reason}, got {result}")


def expect_denied_contract(label, adapter, blocked, **kwargs):
    result = validate_adapter_gate(adapter, **kwargs)
    if result["allowed"]:
        raise SystemExit(f"{label}: expected denied, got {result}")
    if blocked not in result["blocked_fields"]:
        raise SystemExit(f"{label}: expected blocked field {blocked!r}, got {result}")


allowed = validate_adapter_gate(
    codex,
    role="worker",
    requested_transport="cli_json",
    argv=["codex", "exec", "--json", "-C", str(repo), "-"],
)
if not allowed["allowed"]:
    raise SystemExit(f"codex safe worker dispatch should pass gate, got {allowed}")

bad = copy.deepcopy(codex)
del bad["live_gate"]
expect_denied_contract("missing-live-gate", bad, "live_gate", role="worker", requested_transport="cli_json")

bad = copy.deepcopy(codex)
bad["live_gate"]["default"] = "allow"
expect_denied_contract("bad-live-gate-default", bad, "live_gate.default", role="worker", requested_transport="cli_json")

bad = copy.deepcopy(codex)
del bad["status_contract"]
expect_denied_contract("missing-status-contract", bad, "status_contract", role="worker", requested_transport="cli_json")

minimal_ready = {
    "support": copy.deepcopy(codex["support"]),
    "local_readiness_state": copy.deepcopy(codex["local_readiness_state"]),
}
expect_denied_contract(
    "support-plus-readiness-only",
    minimal_ready,
    "live_gate",
    role="worker",
    requested_transport="cli_json",
)

expect_denied_contract(
    "non-dict-gate-manifest",
    [],
    "manifest",
    role="worker",
    requested_transport="cli_json",
)

expect_denied("unsupported", load("amp"), "unsupported", role="controller")
expect_denied(
    "candidate",
    load("amp"),
    "candidate",
    role="worker",
    requested_transport="cli_json",
)

config_only = copy.deepcopy(codex)
config_only["support"]["controller"]["capability"] = "unsupported"
config_only["support"]["controller"]["fallback"] = "config_only"
expect_denied("config-only", config_only, "config_only", role="controller")

expect_denied(
    "probe-required",
    load("claude-code"),
    "probe_required",
    role="worker",
    requested_transport="cli_json",
)
expect_denied(
    "not-installed",
    {**copy.deepcopy(codex), "local_readiness_state": {**copy.deepcopy(codex["local_readiness_state"]), "worker": "not_installed"}},
    "not_installed",
    role="worker",
    requested_transport="tail_file",
)
expect_denied(
    "forbidden-arg",
    codex,
    "forbidden-arg",
    role="worker",
    requested_transport="cli_json",
    argv=["codex", "exec", "--json", "--yolo"],
)

for token in ("--no-sandbox=true", "--auto-approve=true", "--sandbox-disable=1", "--sandbox=danger-full-access"):
    expect_denied(
        f"forbidden-arg-value-{token}",
        codex,
        "forbidden-arg",
        role="worker",
        requested_transport="cli_json",
        argv=["codex", "exec", token],
    )

print("agent adapter validation tests passed")
PY
