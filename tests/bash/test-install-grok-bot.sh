#!/usr/bin/env bash
# Hermetic grok-bot host-port install: dry-run, apply, uninstall.
# Does not require a Grok Bot box, Grok CLI, or other host binaries.

set -eu

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/goal-flight-grok-bot-test.XXXXXX")"
trap 'rm -rf "$TMP_ROOT"' EXIT

export HOME="$TMP_ROOT/home"
export XDG_STATE_HOME="$TMP_ROOT/state"
export GOALFLIGHT_SKIP_ACP_VENV_SETUP=1
export GOALFLIGHT_GROK_BOT_WORKFLOWS="$TMP_ROOT/workflows"
mkdir -p "$HOME" "$GOALFLIGHT_GROK_BOT_WORKFLOWS"

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

run_setup() {
  bash "$REPO_ROOT/setup.sh" "$@"
}

list_out="$(run_setup --list-agents)"
printf '%s\n' "$list_out" | grep -q 'controller grok-bot-workflows-controller' \
  || fail "grok-bot controller not listed"

grok_bot_dry="$(run_setup --grok-bot --addons '')"
printf '%s\n' "$grok_bot_dry" | grep -q 'DRY-RUN setup agent=grok-bot' \
  || fail "grok-bot dry-run header missing"
printf '%s\n' "$grok_bot_dry" | grep -q 'DESTINATIONS selected=grok-bot-workflows-controller' \
  || fail "grok-bot shortcut should select workflows controller"
printf '%s\n' "$grok_bot_dry" | grep -q 'CONTROLLER_SURFACE grok-bot desktop' \
  || fail "grok-bot controller surface missing"
printf '%s\n' "$grok_bot_dry" | grep -q "configs/grok-bot/skills/goal-flight/SKILL.md" \
  || fail "grok-bot wrapper source missing"
printf '%s\n' "$grok_bot_dry" | grep -q "$GOALFLIGHT_GROK_BOT_WORKFLOWS/goal-flight/SKILL.md" \
  || fail "grok-bot workflows target missing"
printf '%s\n' "$grok_bot_dry" | grep -q 'PLUGIN skip supported=false' \
  || fail "grok-bot plugin must stay skipped"
if printf '%s\n' "$grok_bot_dry" | grep -q 'WORKER_CHECK'; then
  fail "grok-bot controller-only setup should not plan a worker check"
fi
[ ! -e "$GOALFLIGHT_GROK_BOT_WORKFLOWS/goal-flight/SKILL.md" ] \
  || fail "dry-run mutated grok-bot workflows skill"
echo "test1 pass: grok-bot dry-run plans wrapper copy and mutates nothing"

install_alias_dry="$(bash "$REPO_ROOT/install.sh" --grok-bot --addons '')"
printf '%s\n' "$install_alias_dry" | grep -q 'DRY-RUN setup agent=grok-bot' \
  || fail "install.sh --grok-bot alias did not select grok-bot setup"
echo "test2 pass: install.sh --grok-bot is a dry-run alias"

agent_dry="$(run_setup --agent grok-bot --addons '')"
printf '%s\n' "$agent_dry" | grep -q 'DRY-RUN setup agent=grok-bot' \
  || fail "--agent grok-bot dry-run header missing"
printf '%s\n' "$agent_dry" | grep -q "$GOALFLIGHT_GROK_BOT_WORKFLOWS/goal-flight/SKILL.md" \
  || fail "--agent grok-bot should expand the workflows target"
echo "test3 pass: --agent grok-bot selects the default controller destination"

grok_bot_apply="$(run_setup --apply --yes --grok-bot --addons '')"
printf '%s\n' "$grok_bot_apply" | grep -q '^APPLY ' \
  || fail "grok-bot apply should write the wrapper"
grok_bot_manifest="$(printf '%s\n' "$grok_bot_apply" | awk '/^BACKUP_MANIFEST /{print $2}')"
[ -n "$grok_bot_manifest" ] || fail "grok-bot backup manifest path missing"
[ -f "$GOALFLIGHT_GROK_BOT_WORKFLOWS/goal-flight/SKILL.md" ] \
  || fail "grok-bot skill not installed"
grep -q 'Grok Bot' "$GOALFLIGHT_GROK_BOT_WORKFLOWS/goal-flight/SKILL.md" \
  || fail "grok-bot skill content missing"
echo "test4 pass: apply writes the workflows-library wrapper"

oneshot_root="$TMP_ROOT/oneshot-workflows"
oneshot_out="$(bash "$REPO_ROOT/install.sh" grok-bot "$oneshot_root" --addons '' 2>&1)"
printf '%s\n' "$oneshot_out" | grep -q '^APPLY ' \
  || fail "install.sh grok-bot <root> should apply writes"
[ -f "$oneshot_root/goal-flight/SKILL.md" ] \
  || fail "install.sh grok-bot oneshot did not write SKILL.md"
echo "test5 pass: install.sh grok-bot <workflows-root> applies"

run_setup --uninstall --from-manifest "$grok_bot_manifest" \
  >/tmp/goal-flight-setup-grok-bot-uninstall.out
[ ! -e "$GOALFLIGHT_GROK_BOT_WORKFLOWS/goal-flight/SKILL.md" ] \
  || fail "grok-bot uninstall left skill"
echo "test6 pass: uninstall restores the workflows library"

wrapper="$REPO_ROOT/configs/grok-bot/skills/goal-flight/SKILL.md"
if grep -q 'LAST RESORT' "$wrapper"; then
  fail "grok-bot wrapper must not copy the Claude-host LAST RESORT executor rule"
fi
grep -q 'first-class' "$wrapper" \
  || fail "grok-bot wrapper must call Executors first-class"
grep -q -- '--agent moonshot' "$wrapper" \
  || fail "grok-bot wrapper must route kimi3 reviews through --agent moonshot"
python3 - "$REPO_ROOT" <<'PY'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
manifest = json.loads((root / "adapters" / "grok-bot.json").read_text())
delegate = manifest["tool_name_map"]["delegate"]
if "Grok Bot Task/executor" not in delegate["host_tools"]:
    raise SystemExit("delegate host_tools must name Grok Bot Task/executor")
joined = " ".join(delegate["constraints"])
if "first-class" not in joined:
    raise SystemExit("delegate constraints must call Executor first-class")
if "last-resort" not in joined:
    raise SystemExit("delegate constraints must reject last-resort framing")
if "moonshot" not in joined:
    raise SystemExit("delegate constraints must name --agent moonshot for kimi3")
routing = manifest.get("host_projection", {}).get("routing") or {}
if routing.get("executor_is_first_class") is not True:
    raise SystemExit("host_projection.routing.executor_is_first_class must be true")
if "kimi3" in json.dumps(manifest.get("agent_id")):
    raise SystemExit("must not invent a kimi3 agent_id")
PY
echo "test7 pass: grok-bot routing treats Executor as first-class; kimi3 is moonshot"

host_doc="$REPO_ROOT/docs/hosts/grok-bot.md"
for wake_file in "$wrapper" "$host_doc"; do
  grep -q 'listen --report-pending' "$wake_file" \
    || fail "$wake_file must arm listen --report-pending"
  grep -q 'relay --drain' "$wake_file" \
    || fail "$wake_file must drain on ring with relay --drain"
  grep -q '!COMPLETE' "$wake_file" \
    || fail "$wake_file must name !COMPLETE as a worker terminal"
  grep -E 'no `!FINISHED`|There is no `!FINISHED`' "$wake_file" >/dev/null \
    || fail "$wake_file must reject !FINISHED as a first-class marker"
  grep -Ei 'alias|treat it as `!COMPLETE`' "$wake_file" >/dev/null \
    || fail "$wake_file must alias FINISHED to !COMPLETE"
  grep -q 'exit 4' "$wake_file" \
    || fail "$wake_file must refuse detached listen (exit 4)"
  if grep -E 'arm unbounded `supervise`|Do \*\*not\*\* arm unbounded `supervise`' "$wake_file" >/dev/null; then
    :
  else
    fail "$wake_file must forbid unbounded supervise as the grok-bot arm"
  fi
done
python3 - "$REPO_ROOT" <<'PY'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
wake = json.loads((root / "adapters" / "grok-bot.json").read_text())["host_projection"]["wake"]
if wake.get("path") != "goalflight_messages.py listen --report-pending":
    raise SystemExit("host_projection.wake.path must be listen --report-pending")
if wake.get("contract") != "exit-as-wake":
    raise SystemExit("host_projection.wake.contract must be exit-as-wake")
if wake.get("mvp_depth") != 1 or wake.get("full_depth") != 4:
    raise SystemExit("wake depth must be MVP 1 / full 4")
if wake.get("finished_alias") != "COMPLETE":
    raise SystemExit("FINISHED must alias to COMPLETE")
if "native mail transport" not in " ".join(wake.get("not") or []):
    raise SystemExit("wake.not must keep native mail transport out of this port")
forbidden = " ".join(wake.get("not") or [])
if "unbounded supervise" not in forbidden:
    raise SystemExit("wake.not must forbid unbounded supervise")
PY
echo "test8 pass: grok-bot wake path is listen doorbell, not supervise or a new bus"

echo "goal-flight grok-bot host install tests passed"
