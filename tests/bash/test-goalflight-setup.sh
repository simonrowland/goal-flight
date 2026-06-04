#!/usr/bin/env bash
set -eu

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
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
printf '%s\n' "$list_out" | grep -q 'addon autoreview' || fail "autoreview add-on not listed"
python3 - "$REPO_ROOT" <<'PY'
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
adapters = ["claude-code", "codex", "cursor", "grok", "opencode"]
missing = []
for name in adapters:
    manifest = json.loads((root / "adapters" / f"{name}.json").read_text())
    addons = manifest.get("setup", {}).get("addons", [])
    if not any(addon.get("id") == "autoreview" for addon in addons):
        missing.append(name)
if missing:
    raise SystemExit("missing autoreview addon: " + ", ".join(missing))
PY

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
printf '%s\n' "$cursor_dry" | grep -q 'register-context-mode-cursor.py --scope global' || fail "cursor context-mode MCP registration missing"
printf '%s\n' "$cursor_dry" | grep -q 'PLUGIN skip supported=false' || fail "cursor plugin must stay skipped"
[ ! -e "$HOME/.cursor/rules/goal-flight.mdc" ] || fail "dry-run mutated cursor rules"
[ ! -e "$HOME/.cursor/AGENTS.md" ] || fail "dry-run mutated cursor AGENTS"
[ ! -e "$HOME/.cursor/skills/goal-flight/SKILL.md" ] || fail "dry-run mutated cursor skill"
[ ! -e "$HOME/.cursor/mcp.json" ] || fail "dry-run mutated cursor MCP config"

cursor_alias_dry="$(run_setup --cursor --addons '')"
printf '%s\n' "$cursor_alias_dry" | grep -q 'DRY-RUN setup agent=cursor' || fail "cursor shortcut did not select cursor setup"
printf '%s\n' "$cursor_alias_dry" | grep -q 'DESTINATIONS selected=.*cursor-desktop-controller' || fail "cursor shortcut should always select desktop controller wrapper"
install_alias_dry="$(bash "$REPO_ROOT/install.sh" --cursor --addons '')"
printf '%s\n' "$install_alias_dry" | grep -q 'DRY-RUN setup agent=cursor' || fail "install.sh cursor alias did not select cursor setup"

CURSOR_PROJECT="$TMP_ROOT/cursor-project"
mkdir -p "$CURSOR_PROJECT"
CURSOR_PROJECT="$(cd "$CURSOR_PROJECT" && pwd -P)"
cursor_project_dry="$(run_setup --cursor-project "$CURSOR_PROJECT" --addons '')"
printf '%s\n' "$cursor_project_dry" | grep -q "$CURSOR_PROJECT/.cursor/skills/goal-flight/SKILL.md" || fail "cursor project skill action missing"
printf '%s\n' "$cursor_project_dry" | grep -q "$CURSOR_PROJECT/.cursor/rules/goal-flight.mdc" || fail "cursor project rule action missing"
cursor_project_apply="$(run_setup --apply --yes --cursor-project "$CURSOR_PROJECT" --addons '')"
cursor_project_manifest="$(printf '%s\n' "$cursor_project_apply" | awk '/^BACKUP_MANIFEST /{print $2}')"
[ -f "$CURSOR_PROJECT/.cursor/skills/goal-flight/SKILL.md" ] || fail "cursor project skill not installed"
[ -f "$CURSOR_PROJECT/.cursor/AGENTS.md" ] || fail "cursor project AGENTS not installed"
[ -f "$CURSOR_PROJECT/.cursor/rules/goal-flight.mdc" ] || fail "cursor project rule not installed"
run_setup --uninstall --from-manifest "$cursor_project_manifest" >/tmp/goal-flight-setup-cursor-project-uninstall.out
[ ! -e "$CURSOR_PROJECT/.cursor/skills/goal-flight/SKILL.md" ] || fail "cursor project uninstall left skill"
[ ! -e "$CURSOR_PROJECT/.cursor/AGENTS.md" ] || fail "cursor project uninstall left AGENTS"
[ ! -e "$CURSOR_PROJECT/.cursor/rules/goal-flight.mdc" ] || fail "cursor project uninstall left rule"

CURSOR_PROJECT_MCP="$TMP_ROOT/cursor-project-mcp"
mkdir -p "$CURSOR_PROJECT_MCP"
CURSOR_PROJECT_MCP="$(cd "$CURSOR_PROJECT_MCP" && pwd -P)"
cursor_project_mcp_apply="$(run_setup --apply --yes --cursor-project "$CURSOR_PROJECT_MCP")"
cursor_project_mcp_manifest="$(printf '%s\n' "$cursor_project_mcp_apply" | awk '/^BACKUP_MANIFEST /{print $2}')"
[ -f "$CURSOR_PROJECT_MCP/.cursor/mcp.json" ] || fail "cursor project MCP config not registered"
grep -q 'context-mode' "$CURSOR_PROJECT_MCP/.cursor/mcp.json" || fail "cursor project MCP content missing"
[ ! -e "$HOME/.cursor/mcp.json" ] || fail "cursor project-only setup should not write global MCP config"
run_setup --uninstall --from-manifest "$cursor_project_mcp_manifest" >/tmp/goal-flight-setup-cursor-project-mcp-uninstall.out
[ ! -e "$CURSOR_PROJECT_MCP/.cursor/mcp.json" ] || fail "cursor project MCP uninstall left config"

cursor_agents_apply="$(run_setup --apply --yes --cursor-agents-standard --addons '')"
cursor_agents_manifest="$(printf '%s\n' "$cursor_agents_apply" | awk '/^BACKUP_MANIFEST /{print $2}')"
[ -f "$HOME/.agents/skills/goal-flight/SKILL.md" ] || fail "standard agents skill not installed"
run_setup --uninstall --from-manifest "$cursor_agents_manifest" >/tmp/goal-flight-setup-cursor-agents-uninstall.out
[ ! -e "$HOME/.agents/skills/goal-flight/SKILL.md" ] || fail "standard agents uninstall left skill"

if run_setup --apply --yes --cursor-link-claude --addons '' >/tmp/goal-flight-setup-cursor-link-missing.out 2>&1; then
  fail "cursor link should fail when Claude skill source is missing"
fi
grep -q 'setup source missing' /tmp/goal-flight-setup-cursor-link-missing.out || fail "missing Claude link source reason missing"

mkdir -p "$HOME/.claude/skills/goal-flight"
printf 'linked skill\n' > "$HOME/.claude/skills/goal-flight/SKILL.md"
cursor_link_apply="$(run_setup --apply --yes --cursor-link-claude --addons '')"
cursor_link_manifest="$(printf '%s\n' "$cursor_link_apply" | awk '/^BACKUP_MANIFEST /{print $2}')"
[ -L "$HOME/.cursor/skills/goal-flight" ] || fail "cursor link from claude not installed"
cursor_link_expected="$(cd "$HOME/.claude/skills/goal-flight" && pwd -P)"
cursor_link_actual="$(cd "$(readlink "$HOME/.cursor/skills/goal-flight")" && pwd -P)"
[ "$cursor_link_actual" = "$cursor_link_expected" ] || fail "cursor link target mismatch"
run_setup --uninstall --from-manifest "$cursor_link_manifest" >/tmp/goal-flight-setup-cursor-link-uninstall.out
[ ! -e "$HOME/.cursor/skills/goal-flight" ] || fail "cursor link uninstall left skill"
rm -rf "$HOME/.claude"

cursor_apply_out="$(run_setup --apply --yes --agent cursor)"
cursor_manifest="$(printf '%s\n' "$cursor_apply_out" | awk '/^BACKUP_MANIFEST /{print $2}')"
[ -n "$cursor_manifest" ] || fail "cursor backup manifest path missing"
[ -f "$cursor_manifest" ] || fail "cursor backup manifest not written"
[ -f "$HOME/.cursor/AGENTS.md" ] || fail "cursor global AGENTS not installed"
[ -f "$HOME/.cursor/skills/goal-flight/SKILL.md" ] || fail "cursor skill not installed"
[ -f "$HOME/.cursor/rules/goal-flight.mdc" ] || fail "cursor rule not installed"
[ -f "$HOME/.cursor/mcp.json" ] || fail "cursor MCP config not registered"
grep -q 'goal-flight' "$HOME/.cursor/AGENTS.md" || fail "cursor AGENTS content missing"
grep -q 'goal-flight' "$HOME/.cursor/skills/goal-flight/SKILL.md" || fail "cursor skill content missing"
grep -q 'goal-flight' "$HOME/.cursor/rules/goal-flight.mdc" || fail "cursor rule content missing"
grep -q 'context-mode' "$HOME/.cursor/mcp.json" || fail "cursor MCP content missing"
grep -q "$(command -v npx)" "$HOME/.cursor/mcp.json" || fail "cursor MCP should pin resolved npx path"
if ls "$HOME/.cursor"/mcp.json.bak.* >/dev/null 2>&1; then
  fail "cursor setup should use Goal Flight manifest backups, not sidecar MCP backups"
fi
run_setup --uninstall --from-manifest "$cursor_manifest" >/tmp/goal-flight-setup-cursor-uninstall.out
[ ! -e "$HOME/.cursor/rules/goal-flight.mdc" ] || fail "cursor uninstall did not remove new rule"
[ ! -e "$HOME/.cursor/AGENTS.md" ] || fail "cursor uninstall did not remove new AGENTS"
[ ! -e "$HOME/.cursor/skills/goal-flight/SKILL.md" ] || fail "cursor uninstall did not remove new skill"
[ ! -e "$HOME/.cursor/mcp.json" ] || fail "cursor uninstall did not remove new MCP config"

CURSOR_ONE_SHOT="$TMP_ROOT/cursor-one-shot"
mkdir -p "$CURSOR_ONE_SHOT"
CURSOR_ONE_SHOT="$(cd "$CURSOR_ONE_SHOT" && pwd -P)"
cursor_install_out="$(bash "$REPO_ROOT/install.sh" cursor "$CURSOR_ONE_SHOT" --addons '' 2>&1)"
printf '%s\n' "$cursor_install_out" | grep -q '^APPLY ' || fail "install.sh cursor should apply writes"
printf '%s\n' "$cursor_install_out" | grep -q 'CONTROLLER_SURFACE cursor desktop' || fail "cursor-install missing global controller"
printf '%s\n' "$cursor_install_out" | grep -q "$CURSOR_ONE_SHOT/.cursor/skills/goal-flight/SKILL.md" || fail "cursor-install missing project skill apply"
[ -f "$CURSOR_ONE_SHOT/.cursor/skills/goal-flight/SKILL.md" ] || fail "cursor-install project skill missing"
while read -r cursor_install_manifest; do
  [ -n "$cursor_install_manifest" ] || continue
  run_setup --uninstall --from-manifest "$cursor_install_manifest" >/tmp/goal-flight-setup-cursor-install-uninstall.out
done <<< "$(printf '%s\n' "$cursor_install_out" | awk '/^BACKUP_MANIFEST /{print $2}')"

NO_NPX_HOME="$TMP_ROOT/no-npx-home"
NO_NPX_STATE="$TMP_ROOT/no-npx-state"
NO_NPX_BIN="$TMP_ROOT/no-npx-bin"
mkdir -p "$NO_NPX_HOME" "$NO_NPX_BIN"
if HOME="$NO_NPX_HOME" XDG_STATE_HOME="$NO_NPX_STATE" PATH="$NO_NPX_BIN:/usr/bin:/bin" \
  run_setup --apply --yes --cursor --addons context-mode >/tmp/goal-flight-setup-cursor-no-npx.out 2>&1; then
  fail "cursor context-mode setup should fail before writing config when npx is missing"
fi
grep -q 'npx is required' /tmp/goal-flight-setup-cursor-no-npx.out || fail "missing npx failure reason missing"
[ ! -e "$NO_NPX_HOME/.cursor/mcp.json" ] || fail "missing npx path wrote cursor MCP config"

PARTIAL_HOME="$TMP_ROOT/partial-home"
PARTIAL_STATE="$TMP_ROOT/partial-state"
PARTIAL_PROJECT="$TMP_ROOT/partial-project"
mkdir -p "$PARTIAL_HOME" "$PARTIAL_PROJECT/.cursor"
printf 'not json\n' > "$PARTIAL_PROJECT/.cursor/mcp.json"
if HOME="$PARTIAL_HOME" XDG_STATE_HOME="$PARTIAL_STATE" \
  run_setup --apply --yes --cursor --cursor-project "$PARTIAL_PROJECT" >/tmp/goal-flight-setup-cursor-partial.out 2>&1; then
  fail "cursor partial MCP failure should fail"
fi
partial_manifest="$(awk '/^BACKUP_MANIFEST /{print $2}' /tmp/goal-flight-setup-cursor-partial.out | tail -1)"
[ -n "$partial_manifest" ] || partial_manifest="$(find "$PARTIAL_STATE/goal-flight/setup-backups" -name '*-cursor.json' -print | head -1)"
[ -f "$partial_manifest" ] || fail "partial cursor failure did not persist backup manifest"
grep -q '.cursor/mcp.json' "$partial_manifest" || fail "partial cursor failure manifest missing MCP backup record"
[ -f "$PARTIAL_HOME/.cursor/mcp.json" ] || fail "partial cursor failure did not create global MCP before failing"
HOME="$PARTIAL_HOME" XDG_STATE_HOME="$PARTIAL_STATE" run_setup --uninstall --from-manifest "$partial_manifest" >/tmp/goal-flight-setup-cursor-partial-uninstall.out
[ ! -e "$PARTIAL_HOME/.cursor/mcp.json" ] || fail "partial cursor uninstall did not remove global MCP config"

claude_dry="$(run_setup --agent claude-code)"
printf '%s\n' "$claude_dry" | grep -q 'PLUGIN skip selected_destinations' || fail "claude setup should stay discovery-only"
[ ! -e "$HOME/.claude" ] || fail "dry-run mutated claude config"

OPENCODE_BIN="$HOME/.local/bin"
mkdir -p "$OPENCODE_BIN"
rm -f "$OPENCODE_BIN/opencode"
cat > "$OPENCODE_BIN/opencode" <<'EOF'
#!/usr/bin/env bash
case "$1" in
  --version) echo "opencode test 0.0.0"; exit 0 ;;
  acp)
    if [ "${2:-}" = "--help" ]; then exit 0; fi
    exit 0
    ;;
  run) exit 0 ;;
esac
if [ "$1" = "--help" ]; then echo "acp run"; exit 0; fi
exit 1
EOF
chmod +x "$OPENCODE_BIN/opencode"

opencode_dry="$(run_setup --agent opencode)"
printf '%s\n' "$opencode_dry" | grep -q 'DRY-RUN setup agent=opencode' || fail "opencode dry-run header missing"
printf '%s\n' "$opencode_dry" | grep -q '.config/opencode/AGENTS.md' || fail "opencode global AGENTS action missing"
printf '%s\n' "$opencode_dry" | grep -q '.config/opencode/skills/goal-flight/SKILL.md' || fail "opencode personal skill action missing"
printf '%s\n' "$opencode_dry" | grep -q 'hosts/opencode/register_context_mode.py --scope global' || fail "opencode context-mode MCP registration missing"
printf '%s\n' "$opencode_dry" | grep -q 'PLUGIN skip supported=false' || fail "opencode plugin must stay skipped"
[ ! -e "$HOME/.config/opencode/AGENTS.md" ] || fail "dry-run mutated opencode AGENTS"
[ ! -e "$HOME/.config/opencode/skills/goal-flight/SKILL.md" ] || fail "dry-run mutated opencode skill"
[ ! -e "$HOME/.config/opencode/opencode.json" ] || fail "dry-run mutated opencode config"

opencode_alias_dry="$(run_setup --opencode --addons '')"
printf '%s\n' "$opencode_alias_dry" | grep -q 'DRY-RUN setup agent=opencode' || fail "opencode shortcut did not select opencode setup"
printf '%s\n' "$opencode_alias_dry" | grep -q 'DESTINATIONS selected=.*opencode-global-controller' || fail "opencode shortcut should select global controller wrapper"
install_opencode_alias_dry="$(bash "$REPO_ROOT/install.sh" --opencode --addons '')"
printf '%s\n' "$install_opencode_alias_dry" | grep -q 'DRY-RUN setup agent=opencode' || fail "install.sh opencode alias did not select opencode setup"

OPENCODE_PROJECT="$TMP_ROOT/opencode-project"
mkdir -p "$OPENCODE_PROJECT"
opencode_project_dry="$(run_setup --opencode-project "$OPENCODE_PROJECT" --addons '')"
printf '%s\n' "$opencode_project_dry" | grep -q '.opencode/skills/goal-flight/SKILL.md' || fail "opencode project skill action missing"
printf '%s\n' "$opencode_project_dry" | grep -q '/AGENTS.md' || fail "opencode project AGENTS action missing"
opencode_project_apply="$(run_setup --apply --yes --opencode-project "$OPENCODE_PROJECT" --addons '')"
opencode_project_manifest="$(printf '%s\n' "$opencode_project_apply" | awk '/^BACKUP_MANIFEST /{print $2}')"
[ -f "$OPENCODE_PROJECT/.opencode/skills/goal-flight/SKILL.md" ] || fail "opencode project skill not installed"
[ -f "$OPENCODE_PROJECT/AGENTS.md" ] || fail "opencode project AGENTS not installed"
run_setup --uninstall --from-manifest "$opencode_project_manifest" >/tmp/goal-flight-setup-opencode-project-uninstall.out
[ ! -e "$OPENCODE_PROJECT/.opencode/skills/goal-flight/SKILL.md" ] || fail "opencode project uninstall left skill"
[ ! -e "$OPENCODE_PROJECT/AGENTS.md" ] || fail "opencode project uninstall left AGENTS"

OPENCODE_PROJECT_MCP="$TMP_ROOT/opencode-project-mcp"
mkdir -p "$OPENCODE_PROJECT_MCP"
opencode_project_mcp_apply="$(run_setup --apply --yes --opencode-project "$OPENCODE_PROJECT_MCP")"
opencode_project_mcp_manifest="$(printf '%s\n' "$opencode_project_mcp_apply" | awk '/^BACKUP_MANIFEST /{print $2}')"
[ -f "$OPENCODE_PROJECT_MCP/opencode.json" ] || fail "opencode project config not registered"
grep -q 'context-mode' "$OPENCODE_PROJECT_MCP/opencode.json" || fail "opencode project MCP content missing"
grep -q 'goal-flight' "$OPENCODE_PROJECT_MCP/opencode.json" || fail "opencode project skill permission missing"
[ ! -e "$HOME/.config/opencode/opencode.json" ] || fail "opencode project-only setup should not write global config"
run_setup --uninstall --from-manifest "$opencode_project_mcp_manifest" >/tmp/goal-flight-setup-opencode-project-mcp-uninstall.out
[ ! -e "$OPENCODE_PROJECT_MCP/opencode.json" ] || fail "opencode project MCP uninstall left config"

opencode_agents_apply="$(run_setup --apply --yes --opencode-agents-standard --addons '')"
opencode_agents_manifest="$(printf '%s\n' "$opencode_agents_apply" | awk '/^BACKUP_MANIFEST /{print $2}')"
[ -f "$HOME/.agents/skills/goal-flight/SKILL.md" ] || fail "opencode agents-standard skill not installed"
run_setup --uninstall --from-manifest "$opencode_agents_manifest" >/tmp/goal-flight-setup-opencode-agents-uninstall.out
[ ! -e "$HOME/.agents/skills/goal-flight/SKILL.md" ] || fail "opencode agents-standard uninstall left skill"

if run_setup --apply --yes --opencode-link-claude --addons '' >/tmp/goal-flight-setup-opencode-link-missing.out 2>&1; then
  fail "opencode link should fail when Claude skill source is missing"
fi
grep -q 'setup source missing' /tmp/goal-flight-setup-opencode-link-missing.out || fail "missing Claude link source reason missing"

mkdir -p "$HOME/.claude/skills/goal-flight"
printf 'claude skill\n' > "$HOME/.claude/skills/goal-flight/SKILL.md"
opencode_link_apply="$(run_setup --apply --yes --opencode-link-claude --addons '')"
opencode_link_manifest="$(printf '%s\n' "$opencode_link_apply" | awk '/^BACKUP_MANIFEST /{print $2}')"
[ -L "$HOME/.config/opencode/skills/goal-flight" ] || fail "opencode link from claude not installed"
opencode_link_expected="$(cd "$HOME/.claude/skills/goal-flight" && pwd -P)"
opencode_link_actual="$(cd "$(readlink "$HOME/.config/opencode/skills/goal-flight")" && pwd -P)"
[ "$opencode_link_actual" = "$opencode_link_expected" ] || fail "opencode link target mismatch"
run_setup --uninstall --from-manifest "$opencode_link_manifest" >/tmp/goal-flight-setup-opencode-link-uninstall.out
[ ! -e "$HOME/.config/opencode/skills/goal-flight" ] || fail "opencode link uninstall left skill"

opencode_apply_out="$(run_setup --apply --yes --agent opencode)"
opencode_manifest="$(printf '%s\n' "$opencode_apply_out" | awk '/^BACKUP_MANIFEST /{print $2}')"
[ -n "$opencode_manifest" ] || fail "opencode backup manifest path missing"
[ -f "$opencode_manifest" ] || fail "opencode backup manifest not written"
[ -f "$HOME/.config/opencode/AGENTS.md" ] || fail "opencode global AGENTS not installed"
[ -f "$HOME/.config/opencode/skills/goal-flight/SKILL.md" ] || fail "opencode skill not installed"
[ -f "$HOME/.config/opencode/opencode.json" ] || fail "opencode config not registered"
grep -q 'goal-flight' "$HOME/.config/opencode/AGENTS.md" || fail "opencode AGENTS content missing"
grep -q 'goal-flight' "$HOME/.config/opencode/skills/goal-flight/SKILL.md" || fail "opencode skill content missing"
grep -q 'context-mode' "$HOME/.config/opencode/opencode.json" || fail "opencode MCP content missing"
grep -q 'goal-flight' "$HOME/.config/opencode/opencode.json" || fail "opencode skill permission missing"
if ls "$HOME/.config/opencode"/opencode.json.bak.* >/dev/null 2>&1; then
  fail "opencode setup should use Goal Flight manifest backups, not sidecar config backups"
fi
run_setup --uninstall --from-manifest "$opencode_manifest" >/tmp/goal-flight-setup-opencode-uninstall.out
[ ! -e "$HOME/.config/opencode/AGENTS.md" ] || fail "opencode uninstall did not remove new AGENTS"
[ ! -e "$HOME/.config/opencode/skills/goal-flight/SKILL.md" ] || fail "opencode uninstall did not remove new skill"
[ ! -e "$HOME/.config/opencode/opencode.json" ] || fail "opencode uninstall did not remove new config"

OPENCODE_ONE_SHOT="$TMP_ROOT/opencode-one-shot"
mkdir -p "$OPENCODE_ONE_SHOT"
OPENCODE_ONE_SHOT="$(cd "$OPENCODE_ONE_SHOT" && pwd -P)"
opencode_install_out="$(bash "$REPO_ROOT/install.sh" opencode "$OPENCODE_ONE_SHOT" --addons '' 2>&1)"
printf '%s\n' "$opencode_install_out" | grep -q '^APPLY ' || fail "install.sh opencode should apply writes"
printf '%s\n' "$opencode_install_out" | grep -q 'CONTROLLER_SURFACE opencode' || fail "opencode-install missing global controller"
printf '%s\n' "$opencode_install_out" | grep -q "$OPENCODE_ONE_SHOT/.opencode/skills/goal-flight/SKILL.md" || fail "opencode-install missing project skill apply"
[ -f "$OPENCODE_ONE_SHOT/.opencode/skills/goal-flight/SKILL.md" ] || fail "opencode-install project skill missing"
while read -r opencode_install_manifest; do
  [ -n "$opencode_install_manifest" ] || continue
  run_setup --uninstall --from-manifest "$opencode_install_manifest" >/tmp/goal-flight-setup-opencode-install-uninstall.out
done <<< "$(printf '%s\n' "$opencode_install_out" | awk '/^BACKUP_MANIFEST /{print $2}')"

NO_NPX_OPENCODE_HOME="$TMP_ROOT/no-npx-opencode-home"
NO_NPX_OPENCODE_STATE="$TMP_ROOT/no-npx-opencode-state"
mkdir -p "$NO_NPX_OPENCODE_HOME"
if HOME="$NO_NPX_OPENCODE_HOME" XDG_STATE_HOME="$NO_NPX_OPENCODE_STATE" PATH="$OPENCODE_BIN:/usr/bin:/bin" \
  run_setup --apply --yes --opencode --addons context-mode >/tmp/goal-flight-setup-opencode-no-npx.out 2>&1; then
  fail "opencode context-mode setup should fail before writing config when npx is missing"
fi
grep -q 'npx is required' /tmp/goal-flight-setup-opencode-no-npx.out || fail "missing npx failure reason missing for opencode"
[ ! -e "$NO_NPX_OPENCODE_HOME/.config/opencode/opencode.json" ] || fail "missing npx path wrote opencode config"

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
