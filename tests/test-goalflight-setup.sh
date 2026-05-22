#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/goal-flight-setup-test.XXXXXX")"
trap 'rm -rf "$TMP_ROOT"' EXIT

export HOME="$TMP_ROOT/home"
export XDG_STATE_HOME="$TMP_ROOT/state"
mkdir -p "$HOME"

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

run_setup() {
  bash "$REPO_ROOT/setup.sh" "$@"
}

list_out="$(run_setup --list-agents)"
printf '%s\n' "$list_out" | grep -q 'controller codex-desktop-controller' || fail "codex desktop controller not listed"
printf '%s\n' "$list_out" | grep -q 'worker codex-cli-worker' || fail "codex cli worker not listed"
printf '%s\n' "$list_out" | grep -q 'addon context-mode' || fail "context-mode add-on not listed"
printf '%s\n' "$list_out" | grep -q 'addon gstack' || fail "gstack add-on not listed"

cli_dry="$(run_setup --controllers codex-cli-controller --workers codex-cli-worker --addons context-mode)"
printf '%s\n' "$cli_dry" | grep -q 'DESTINATIONS selected=codex-cli-controller,codex-cli-worker' || fail "cli destination selection missing"
printf '%s\n' "$cli_dry" | grep -q 'CONTROLLER_SURFACE codex cli' || fail "codex cli controller marker missing"
if printf '%s\n' "$cli_dry" | grep -q 'CONTROLLER_SURFACE codex desktop'; then
  fail "cli-only selection should not claim desktop controller"
fi
printf '%s\n' "$cli_dry" | grep -q 'PLUGIN skip selected_destinations' || fail "cli controller should not register desktop plugin"
printf '%s\n' "$cli_dry" | grep -q 'WORKER_CHECK codex exec --help' || fail "cli worker check missing"
no_addons_dry="$(run_setup --controllers codex-cli-controller --workers codex-cli-worker --addons '')"
if printf '%s\n' "$no_addons_dry" | grep -q 'register-context-mode-codex.py'; then
  fail "empty --addons should skip context-mode bootstrap"
fi
mixed_dry="$(run_setup --controllers codex-desktop-controller,grok-cli-controller --workers codex-cli-worker,grok-acp-worker --addons context-mode,gstack)"
printf '%s\n' "$mixed_dry" | grep -q 'DRY-RUN setup agent=codex' || fail "mixed selection should plan codex"
printf '%s\n' "$mixed_dry" | grep -q 'DRY-RUN setup agent=grok' || fail "mixed selection should plan grok"
printf '%s\n' "$mixed_dry" | grep -q 'ADDON_SKIP grok context-mode reason=incompatible_destinations' || fail "mixed selection should skip incompatible add-on per agent"
printf '%s\n' "$mixed_dry" | grep -q 'WORKER_CHECK grok agent stdio' || fail "mixed grok worker check missing"
combined_codex_dry="$(run_setup --controllers codex-desktop-controller,codex-cli-controller --workers codex-cli-worker --addons '')"
printf '%s\n' "$combined_codex_dry" | grep -q 'ACTION register_plugin' || fail "combined codex setup should register desktop plugin"
printf '%s\n' "$combined_codex_dry" | grep -q 'CLEANUP remove_tree target=.*.codex/skills/goal-flight' || fail "combined codex setup should clean duplicate personal skill"

codex_dry="$(run_setup --agent codex)"
printf '%s\n' "$codex_dry" | grep -q 'DRY-RUN setup agent=codex' || fail "codex dry-run header missing"
if printf '%s\n' "$codex_dry" | grep -q 'ACTION copy source=.*.codex/skills/goal-flight/SKILL.md'; then
  fail "codex desktop setup should not install duplicate personal skill"
fi
printf '%s\n' "$codex_dry" | grep -q 'CLEANUP remove_tree target=.*.codex/skills/goal-flight' || fail "codex legacy personal skill cleanup missing"
printf '%s\n' "$codex_dry" | grep -q 'ACTION register_plugin' || fail "codex plugin registration action missing"
printf '%s\n' "$codex_dry" | grep -q 'codex plugin marketplace add' || fail "codex marketplace command missing"
printf '%s\n' "$codex_dry" | grep -q 'codex plugin add goal-flight@goal-flight' || fail "codex plugin add command missing"
printf '%s\n' "$codex_dry" | grep -q 'CONTROLLER_SURFACE codex desktop' || fail "codex desktop controller marker missing"
if printf '%s\n' "$codex_dry" | grep -q 'CONTROLLER_SURFACE codex cli'; then
  fail "codex default setup should not claim optional cli controller"
fi
printf '%s\n' "$codex_dry" | grep -q 'WORKER_CHECK codex exec --help' || fail "codex cli worker check missing"
printf '%s\n' "$codex_dry" | grep -q 'register-context-mode-codex.py --check' || fail "codex context-mode check missing"
printf '%s\n' "$codex_dry" | grep -q 'register-context-mode-codex.py' || fail "codex context-mode bootstrap missing"
printf '%s\n' "$codex_dry" | grep -q 'RESTART_REQUIRED codex' || fail "codex restart notice missing"
printf '%s\n' "$codex_dry" | grep -q 'ACTION copy_or_merge' || fail "codex dry-run action missing"
printf '%s\n' "$codex_dry" | grep -q 'configs/codex/config.toml' || fail "codex dry-run source missing"
printf '%s\n' "$codex_dry" | grep -q 'PLUGIN register_plugin source=plugins/goal-flight/.codex-plugin/plugin.json' || fail "codex plugin registration not shown"
[ ! -e "$HOME/.codex/config.toml" ] || fail "dry-run mutated codex config"
[ ! -e "$HOME/.codex/skills/goal-flight/SKILL.md" ] || fail "dry-run mutated codex skill"

cursor_dry="$(run_setup --agent cursor)"
printf '%s\n' "$cursor_dry" | grep -q 'DRY-RUN setup agent=cursor' || fail "cursor dry-run header missing"
printf '%s\n' "$cursor_dry" | grep -q '.cursor/AGENTS.md' || fail "cursor global AGENTS action missing"
printf '%s\n' "$cursor_dry" | grep -q '.cursor/skills/goal-flight/SKILL.md' || fail "cursor personal skill action missing"
printf '%s\n' "$cursor_dry" | grep -q 'goal-flight.mdc' || fail "cursor rule action missing"
printf '%s\n' "$cursor_dry" | grep -q 'PLUGIN skip supported=false' || fail "cursor plugin must stay skipped"
[ ! -e "$HOME/.cursor/rules/goal-flight.mdc" ] || fail "dry-run mutated cursor rules"
[ ! -e "$HOME/.cursor/AGENTS.md" ] || fail "dry-run mutated cursor AGENTS"
[ ! -e "$HOME/.cursor/skills/goal-flight/SKILL.md" ] || fail "dry-run mutated cursor skill"

cursor_apply_out="$(run_setup --apply --yes --agent cursor)"
cursor_manifest="$(printf '%s\n' "$cursor_apply_out" | awk '/^BACKUP_MANIFEST /{print $2}')"
[ -n "$cursor_manifest" ] || fail "cursor backup manifest path missing"
[ -f "$cursor_manifest" ] || fail "cursor backup manifest not written"
[ -f "$HOME/.cursor/AGENTS.md" ] || fail "cursor global AGENTS not installed"
[ -f "$HOME/.cursor/skills/goal-flight/SKILL.md" ] || fail "cursor skill not installed"
[ -f "$HOME/.cursor/rules/goal-flight.mdc" ] || fail "cursor rule not installed"
grep -q 'goal-flight' "$HOME/.cursor/AGENTS.md" || fail "cursor AGENTS content missing"
grep -q 'goal-flight' "$HOME/.cursor/skills/goal-flight/SKILL.md" || fail "cursor skill content missing"
grep -q 'goal-flight' "$HOME/.cursor/rules/goal-flight.mdc" || fail "cursor rule content missing"
run_setup --uninstall --from-manifest "$cursor_manifest" >/tmp/goal-flight-setup-cursor-uninstall.out
[ ! -e "$HOME/.cursor/rules/goal-flight.mdc" ] || fail "cursor uninstall did not remove new rule"
[ ! -e "$HOME/.cursor/AGENTS.md" ] || fail "cursor uninstall did not remove new AGENTS"
[ ! -e "$HOME/.cursor/skills/goal-flight/SKILL.md" ] || fail "cursor uninstall did not remove new skill"

claude_dry="$(run_setup --agent claude-code)"
printf '%s\n' "$claude_dry" | grep -q 'PLUGIN skip selected_destinations' || fail "claude setup should stay discovery-only"
[ ! -e "$HOME/.claude" ] || fail "dry-run mutated claude config"

if run_setup --apply --agent codex >/tmp/goal-flight-setup-denied.out 2>&1; then
  fail "apply without --yes should fail"
fi
grep -q 'refusing mutation without --yes' /tmp/goal-flight-setup-denied.out || fail "apply refusal reason missing"

mkdir -p "$HOME/.codex"
printf 'existing = true\n' > "$HOME/.codex/config.toml"
mkdir -p "$HOME/.codex/skills/goal-flight"
printf 'legacy skill\n' > "$HOME/.codex/skills/goal-flight/SKILL.md"
cp "$HOME/.codex/config.toml" "$TMP_ROOT/original-config.toml"
export GOALFLIGHT_SETUP_FAKE_CODEX_LOG="$TMP_ROOT/fake-codex.log"
export GOALFLIGHT_SETUP_FAKE_CONTEXT_MODE_LOG="$TMP_ROOT/fake-context-mode.log"

fail_log="$TMP_ROOT/fake-codex-fail.log"
if GOALFLIGHT_SETUP_FAKE_CODEX_LOG="$fail_log" GOALFLIGHT_SETUP_FAKE_CODEX_FAIL_VERIFY=1 \
  run_setup --apply --yes --controllers codex-desktop-controller --addons '' >/tmp/goal-flight-setup-plugin-fail.out 2>&1; then
  fail "plugin registration failure should fail"
fi
grep -q 'codex plugin remove goal-flight@goal-flight' "$fail_log" || fail "failed plugin apply did not unregister plugin"
grep -q 'codex plugin marketplace remove goal-flight' "$fail_log" || fail "failed plugin apply did not unregister marketplace"

COMBINED_HOME="$TMP_ROOT/combined-home"
COMBINED_STATE="$TMP_ROOT/combined-state"
mkdir -p "$COMBINED_HOME/.codex/skills/goal-flight"
printf 'legacy skill\n' > "$COMBINED_HOME/.codex/skills/goal-flight/SKILL.md"
printf 'existing = true\n' > "$COMBINED_HOME/.codex/config.toml"
combined_log="$TMP_ROOT/fake-codex-combined.log"
combined_context_log="$TMP_ROOT/fake-context-mode-combined.log"
combined_apply_out="$(
  HOME="$COMBINED_HOME" XDG_STATE_HOME="$COMBINED_STATE" \
  GOALFLIGHT_SETUP_FAKE_CODEX_LOG="$combined_log" \
  GOALFLIGHT_SETUP_FAKE_CONTEXT_MODE_LOG="$combined_context_log" \
  run_setup --apply --yes --controllers codex-desktop-controller,codex-cli-controller --workers codex-cli-worker --addons ''
)"
combined_manifest="$(printf '%s\n' "$combined_apply_out" | awk '/^BACKUP_MANIFEST /{print $2}')"
[ -n "$combined_manifest" ] || fail "combined codex backup manifest path missing"
grep -q 'codex plugin add goal-flight@goal-flight' "$combined_log" || fail "combined codex plugin add missing"
[ ! -e "$COMBINED_HOME/.codex/skills/goal-flight" ] || fail "combined codex apply left duplicate personal skill"
HOME="$COMBINED_HOME" XDG_STATE_HOME="$COMBINED_STATE" \
  GOALFLIGHT_SETUP_FAKE_CODEX_LOG="$combined_log" \
  run_setup --uninstall --from-manifest "$combined_manifest" >/tmp/goal-flight-setup-combined-uninstall.out
grep -q 'legacy skill' "$COMBINED_HOME/.codex/skills/goal-flight/SKILL.md" || fail "combined codex uninstall did not restore legacy personal skill"

apply_out="$(run_setup --apply --yes --agent codex)"
manifest="$(printf '%s\n' "$apply_out" | awk '/^BACKUP_MANIFEST /{print $2}')"
[ -n "$manifest" ] || fail "backup manifest path missing"
[ -f "$manifest" ] || fail "backup manifest not written"
grep -q 'codex plugin marketplace add' "$GOALFLIGHT_SETUP_FAKE_CODEX_LOG" || fail "fake marketplace command missing"
grep -q 'codex plugin add goal-flight@goal-flight' "$GOALFLIGHT_SETUP_FAKE_CODEX_LOG" || fail "fake plugin add command missing"
grep -q 'register-context-mode-codex.py' "$GOALFLIGHT_SETUP_FAKE_CONTEXT_MODE_LOG" || fail "fake context-mode command missing"
[ ! -e "$HOME/.codex/skills/goal-flight" ] || fail "codex legacy personal skill not cleaned up"
grep -q 'existing = true' "$HOME/.codex/config.toml" || fail "existing codex config lost"
[ "$(grep -c '# >>> goal-flight codex' "$HOME/.codex/config.toml")" -eq 1 ] || fail "goal-flight block missing after apply"

run_setup --apply --yes --agent codex >/tmp/goal-flight-setup-apply2.out
[ "$(grep -c '# >>> goal-flight codex' "$HOME/.codex/config.toml")" -eq 1 ] || fail "codex setup not idempotent"

run_setup --uninstall --from-manifest "$manifest" >/tmp/goal-flight-setup-uninstall.out
grep -q 'codex plugin remove goal-flight@goal-flight' "$GOALFLIGHT_SETUP_FAKE_CODEX_LOG" || fail "fake plugin remove command missing"
grep -q 'codex plugin marketplace remove goal-flight' "$GOALFLIGHT_SETUP_FAKE_CODEX_LOG" || fail "fake marketplace remove command missing"
cmp "$TMP_ROOT/original-config.toml" "$HOME/.codex/config.toml" >/dev/null || fail "uninstall did not restore original config"
grep -q 'legacy skill' "$HOME/.codex/skills/goal-flight/SKILL.md" || fail "uninstall did not restore legacy personal skill"

FIXTURE_REPO="$TMP_ROOT/fixture-repo"
mkdir -p "$FIXTURE_REPO/adapters" "$FIXTURE_REPO/configs/codex"
cp "$REPO_ROOT/adapters/codex.json" "$FIXTURE_REPO/adapters/codex.json"
cp "$REPO_ROOT/configs/codex/config.toml" "$FIXTURE_REPO/configs/codex/config.toml"
cp -R "$REPO_ROOT/.agents" "$FIXTURE_REPO/.agents"
mkdir -p "$FIXTURE_REPO/plugins"
cp -R "$REPO_ROOT/plugins/goal-flight" "$FIXTURE_REPO/plugins/goal-flight"
python3 - "$FIXTURE_REPO/adapters/codex.json" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
data = json.loads(path.read_text())
for action in data["packaging"]["install_actions"]:
    if action["kind"] == "register_plugin":
        action["user_gate"] = False
        break
else:
    raise SystemExit("register_plugin action missing")
path.write_text(json.dumps(data, indent=2) + "\n")
PY
if python3 "$REPO_ROOT/scripts/goalflight_setup.py" --repo-root "$FIXTURE_REPO" --agent codex --apply --yes >/tmp/goal-flight-setup-ungated.out 2>&1; then
  fail "ungated write action should fail"
fi
grep -q 'refusing ungated write action' /tmp/goal-flight-setup-ungated.out || fail "ungated write refusal missing"

echo "goal-flight setup registrar tests passed"
