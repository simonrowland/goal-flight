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
  grep -q -- '--timeout-s 900' "$wake_file" \
    || fail "$wake_file must default listen --timeout-s 900"
  grep -q 'goalflight-grokbot' "$wake_file" \
    || fail "$wake_file must claim controller label goalflight-grokbot"
  grep -q 'relay --drain' "$wake_file" \
    || fail "$wake_file must drain on ring with relay --drain"
  grep -q '!COMPLETE' "$wake_file" \
    || fail "$wake_file must name !COMPLETE as the success terminal"
  grep -E 'exit 1|timeout \(exit 1\)' "$wake_file" >/dev/null \
    || fail "$wake_file must treat listen exit 1 as the frontier reminder"
  grep -q 'goalflight_task.py next' "$wake_file" \
    || fail "$wake_file must pull task next on the 900s frontier ping"
  if grep -q '!FINISHED' "$wake_file"; then
    fail "$wake_file must not reify the misremembered !FINISHED marker"
  fi
  grep -q 'exit 4' "$wake_file" \
    || fail "$wake_file must refuse detached listen (exit 4)"
  if grep -E 'arm unbounded `supervise`|Do \*\*not\*\* arm unbounded `supervise`' "$wake_file" >/dev/null; then
    :
  else
    fail "$wake_file must forbid unbounded supervise as the grok-bot arm"
  fi
  if grep -E 'Supervise heartbeats \(~120s\)|supervise heartbeats \(~120s\)' "$wake_file" >/dev/null; then
    fail "$wake_file must not treat the 120s Claude stream heartbeat as the grok-bot ping"
  fi
done
python3 - "$REPO_ROOT" <<'PY'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
wake = json.loads((root / "adapters" / "grok-bot.json").read_text())["host_projection"]["wake"]
if wake.get("path") != "goalflight_grok_bot_listen.py --report-pending --timeout-s 900":
    raise SystemExit("host_projection.wake.path must be goalflight_grok_bot_listen.py --report-pending --timeout-s 900")
if wake.get("contract") != "exit-as-wake":
    raise SystemExit("host_projection.wake.contract must be exit-as-wake")
if wake.get("controller_label") != "goalflight-grokbot":
    raise SystemExit("wake.controller_label must be goalflight-grokbot")
if wake.get("timeout_s") != 900:
    raise SystemExit("wake.timeout_s must be 900")
if "quote-check" not in str(wake.get("on_ring") or ""):
    raise SystemExit("wake.on_ring must quote-check after a ring")
if wake.get("on_timeout") != "handoff write, quote-check, re-arm, task next":
    raise SystemExit("wake.on_timeout must write handoff, quote-check, re-arm, task next")
if wake.get("mvp_depth") != 1 or wake.get("full_depth") != 4:
    raise SystemExit("wake depth must be MVP 1 / full 4")
if wake.get("finished_alias"):
    raise SystemExit("do not reify a FINISHED alias; success terminal is COMPLETE")
if "native mail transport" not in " ".join(wake.get("not") or []):
    raise SystemExit("wake.not must keep native mail transport out of this port")
forbidden = " ".join(wake.get("not") or [])
if "unbounded supervise" not in forbidden:
    raise SystemExit("wake.not must forbid unbounded supervise")
if "120s" not in forbidden:
    raise SystemExit("wake.not must reject the 120s Claude stream heartbeat as a grok-bot arm")
if "global supervise/follow cadence change" not in forbidden:
    raise SystemExit("wake.not must keep the 900s timeout host-local")
PY
echo "test8 pass: grok-bot wake path is listen doorbell + 900s frontier ping"

for ctx_file in "$wrapper" "$host_doc"; do
  grep -q 'protocols/subagent-preamble.md' "$ctx_file" \
    || fail "$ctx_file must open executor prompts with subagent-preamble.md"
  grep -q 'protocols/worker-context-package.md' "$ctx_file" \
    || fail "$ctx_file must apply worker-context-package.md when the lane is triggered"
  grep -Ei 'if the lane is triggered|If the lane is triggered' "$ctx_file" >/dev/null \
    || fail "$ctx_file must gate the lane package on a trigger, not apply it always"
  grep -q 'docs-private/rag/ORIENTATION.md' "$ctx_file" \
    || fail "$ctx_file must pointer to rag/ORIENTATION.md when present"
  grep -q 'machineId' "$ctx_file" \
    || fail "$ctx_file must name machineId for grok-executor Mac targeting"
  grep -q '/Users/simonrowland/Repos' "$ctx_file" \
    || fail "$ctx_file must cite Mac absolute paths under /Users/simonrowland/Repos"
  grep -q -- '--prompt-file' "$ctx_file" \
    || fail "$ctx_file must keep five-layer --prompt-file for CLI and paste it for executors"
  if grep -qi 'parallel package' "$ctx_file"; then
    :
  else
    fail "$ctx_file must refuse a parallel executor package format"
  fi
done
[ ! -e "$REPO_ROOT/templates/grok-executor-prompt.md.tpl" ] \
  || fail "do not add a grok-executor template; Cursor/Grok CLI wrappers have none"
python3 - "$REPO_ROOT" <<'PY'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
routing = json.loads((root / "adapters" / "grok-bot.json").read_text())["host_projection"]["routing"]
pkg = routing.get("executor_context_package") or {}
if "protocols/subagent-preamble.md" not in (pkg.get("reuse") or []):
    raise SystemExit("executor_context_package must reuse subagent-preamble.md")
if pkg.get("triggered_lane_package") != "protocols/worker-context-package.md":
    raise SystemExit("triggered_lane_package must be worker-context-package.md")
if "protocols/worker-context-package.md" in (pkg.get("reuse") or []):
    raise SystemExit("do not always-on reuse the lane package; it is trigger-gated")
if pkg.get("default_computer") != "Grok Bot box":
    raise SystemExit("executors default to the Grok Bot box")
if pkg.get("work_computer") != "user registered computer via machineId":
    raise SystemExit("executor work computer must be the user Mac via machineId")
if not str(pkg.get("mac_path_prefix") or "").startswith("/Users/simonrowland/Repos"):
    raise SystemExit("mac_path_prefix must be /Users/simonrowland/Repos/")
if pkg.get("no_clone_onto_box") is not True:
    raise SystemExit("must forbid cloning onto the Grok Bot box")
if pkg.get("no_parallel_package_format") is not True:
    raise SystemExit("must refuse a parallel package format")
PY
echo "test9 pass: grok-executor Task prompts reuse the Claude host-subagent pin"

for compact_file in "$wrapper" "$host_doc"; do
  grep -q 'protocols/state-handoff.md' "$compact_file" \
    || fail "$compact_file must map directed compact to state-handoff.md"
  grep -q 'Before compact or sleep' "$compact_file" \
    || fail "$compact_file must name the Before compact or sleep write"
  grep -q 'ENVIRONMENT' "$compact_file" \
    || fail "$compact_file must name ENVIRONMENT in RESUME-NOTES slots"
  grep -q 'IDEAS' "$compact_file" \
    || fail "$compact_file must name IDEAS in RESUME-NOTES slots"
  grep -q 'DECISIONS' "$compact_file" \
    || fail "$compact_file must name DECISIONS in RESUME-NOTES slots"
  grep -q 'FACTS' "$compact_file" \
    || fail "$compact_file must name FACTS in RESUME-NOTES slots"
  grep -q 'CARRIERS' "$compact_file" \
    || fail "$compact_file must name CARRIERS in RESUME-NOTES slots"
  grep -q 'no task tables' "$compact_file" \
    || fail "$compact_file must forbid task tables in RESUME-NOTES"
  grep -q 'before long waves' "$compact_file" \
    || fail "$compact_file must write handoff before long waves"
  grep -q 'keep-vs-toss' "$compact_file" \
    || fail "$compact_file must state there is no keep-vs-toss prompt"
  grep -q 'Do not ask the user to compact' "$compact_file" \
    || fail "$compact_file must forbid asking the user to compact"
  grep -q 'Do not invent a compact UI' "$compact_file" \
    || fail "$compact_file must forbid inventing a compact UI"
  grep -q 'Skip' "$compact_file" \
    || fail "$compact_file must skip resume.md STEP 1.5 supervise"
  grep -q 'Chat summaries are hints' "$compact_file" \
    || fail "$compact_file must treat chat summaries as hints"
  grep -q 'Hard Invariants' "$compact_file" \
    || fail "$compact_file must require a fresh SKILL.md Hard Invariants disk read"
  grep -q 'update_state' "$compact_file" \
    || fail "$compact_file must call host profile/update_state/routines advisory"
done
python3 - "$REPO_ROOT" <<'PY'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
compact = json.loads((root / "adapters" / "grok-bot.json").read_text())["host_projection"]["compaction"]
if compact.get("controller_authored_compact") is not False:
    raise SystemExit("grok-bot has no controller-authored /compact")
if compact.get("keep_vs_toss_prompt") is not False:
    raise SystemExit("grok-bot has no keep-vs-toss prompt")
if compact.get("host_may_summarize_unannounced") is not True:
    raise SystemExit("host may summarize unannounced")
if "Before compact or sleep" not in str(compact.get("directed_compact_is") or ""):
    raise SystemExit("directed compact is the state-handoff Before compact write")
if compact.get("canonical_memory") != "repo_files":
    raise SystemExit("canonical memory remains repo_files")
if "STEP 1.5" not in str(compact.get("resume_wake") or ""):
    raise SystemExit("resume_wake must skip commands/resume.md STEP 1.5")
if compact.get("no_task_tables_in_resume_notes") is not True:
    raise SystemExit("RESUME-NOTES must not carry task tables")
if "before long waves" not in " ".join(compact.get("write_on") or []):
    raise SystemExit("must write handoff before long waves")
if compact.get("strategy") != "autocompact + keep handoff current":
    raise SystemExit("grok-bot strategy must be autocompact + keep handoff current")
if compact.get("context_meter") is not False:
    raise SystemExit("must not port the Claude context-meter")
if compact.get("session_start_hook") is not False:
    raise SystemExit("must not add a SessionStart hook on grok-bot")
if compact.get("eighty_percent_substitute") != "listen --timeout-s 900":
    raise SystemExit("900s listen timeout is the 80% hint substitute")
if compact.get("mini_resume_on_wake") is not True:
    raise SystemExit("every listen wake must be a mini-resume")
forbidden = " ".join(compact.get("not") or [])
if "invent a compact UI" not in forbidden:
    raise SystemExit("must not invent a compact UI")
if "ask the user to compact" not in forbidden:
    raise SystemExit("must not ask the user to compact")
if "emulate Claude compact prompt" not in forbidden:
    raise SystemExit("must not emulate Claude compact prompt")
if "goalflight-context-meter.sh" not in forbidden:
    raise SystemExit("must refuse porting goalflight-context-meter.sh")
if "fake window-percent meter" not in forbidden:
    raise SystemExit("must refuse a fake window-percent meter")
PY
echo "test10 pass: grok-bot compaction is write-early handoff, not /compact"

for meter_file in "$wrapper" "$host_doc"; do
  grep -q 'autocompact + keep handoff current' "$meter_file" \
    || fail "$meter_file must choose autocompact + keep handoff current"
  grep -q 'goalflight-context-meter.sh' "$meter_file" \
    || fail "$meter_file must name the Claude context-meter only to refuse it"
  grep -q 'SessionStart' "$meter_file" \
    || fail "$meter_file must refuse a SessionStart hook"
  grep -q 'QUOTE-CHECK:' "$meter_file" \
    || fail "$meter_file must externalize the Hard Invariants quote-check banner"
  grep -q 'goalflight_grok_bot_listen.py' "$meter_file" \
    || fail "$meter_file must arm the grok-bot listen wrapper"
  grep -q 'mini-resume' "$meter_file" \
    || fail "$meter_file must treat every listen wake as a mini-resume"
  grep -q '80%' "$meter_file" \
    || fail "$meter_file must name the 900s timeout as the 80% hint substitute"
done
banner_out="$(python3 "$REPO_ROOT/scripts/goalflight_grok_bot_listen.py" --help 2>&1)" || true
printf '%s\n' "$banner_out" | grep -q 'QUOTE-CHECK: disk-read SKILL.md Hard Invariants' \
  || fail "goalflight_grok_bot_listen.py must print the quote-check banner after listen exits"
python3 - "$REPO_ROOT" <<'PY'
import importlib.util
import sys
from pathlib import Path

root = Path(sys.argv[1])
path = root / "scripts" / "goalflight_grok_bot_listen.py"
spec = importlib.util.spec_from_file_location("grok_bot_listen", path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
if mod._with_host_timeout([]) != ["--timeout-s", "900"]:
    raise SystemExit("grok-bot listen wrapper must default --timeout-s 900")
if mod._with_host_timeout(["--timeout-s", "0"]) != ["--timeout-s", "0"]:
    raise SystemExit("explicit --timeout-s 0 must stay mail-only")
if mod._with_host_timeout(["--timeout-s=0"]) != ["--timeout-s=0"]:
    raise SystemExit("equals-form --timeout-s=0 must not be rewritten")
PY
if grep -q 'PostToolUse' "$REPO_ROOT/scripts/goalflight_grok_bot_listen.py"; then
  grep -q 'Do not port' "$REPO_ROOT/scripts/goalflight_grok_bot_listen.py" \
    || fail "listen wrapper must not implement PostToolUse"
fi
echo "test11 pass: grok-bot uses listen-exit quote-check, not a context meter"

echo "goal-flight grok-bot host install tests passed"
