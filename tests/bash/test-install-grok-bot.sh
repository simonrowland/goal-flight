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

echo "goal-flight grok-bot host install tests passed"
