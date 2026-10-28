#!/usr/bin/env bash
# Install the pinned per-box remote-CI admitter LaunchAgent.

set -euo pipefail

LABEL="com.goalflight.remote-ci-node"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
TEMPLATE="${REPO_ROOT}/templates/remote-ci-node.plist.tmpl"
CONFIG_PATH="${GOALFLIGHT_REMOTE_CI_CONFIG:-${REPO_ROOT}/.goal-flight/remote-ci.json}"
BOX_ALIAS="${1:-}"
shift || true
DRY_RUN=0
UNINSTALL=0

usage() {
  cat <<'EOF'
Usage:
  scripts/install-remote-ci-node-agent.sh <box-ssh-alias> [--dry-run]
  scripts/install-remote-ci-node-agent.sh <box-ssh-alias> --uninstall

The installer archives the current Git commit into
~/.goal-flight/remote-ci-node/<commit>, switches the current symlink, and
bootstraps com.goalflight.remote-ci-node on the remote box.

GOALFLIGHT_REMOTE_CI_CONFIG may name a config containing the box's
python_interpreter, managed_run_directory, and expected_hostname.
EOF
}

if [ -z "$BOX_ALIAS" ] || [[ "$BOX_ALIAS" == -* ]]; then
  usage >&2
  exit 2
fi

while [ "$#" -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --uninstall) UNINSTALL=1 ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'ERROR: unknown argument: %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

if [ "$DRY_RUN" -eq 1 ] && [ "$UNINSTALL" -eq 1 ]; then
  echo "ERROR: --dry-run and --uninstall cannot be combined" >&2
  exit 2
fi

command -v ssh >/dev/null 2>&1 || { echo "ERROR: ssh not found" >&2; exit 2; }
command -v python3 >/dev/null 2>&1 || { echo "ERROR: python3 not found" >&2; exit 2; }

quote_arg() {
  python3 -c 'import shlex,sys; print(shlex.quote(sys.argv[1]))' "$1"
}

REMOTE_HOME=""
if [ "$UNINSTALL" -eq 1 ]; then
  REMOTE_HOME="$(ssh "$BOX_ALIAS" 'printf "%s\n" "$HOME"')"
  [ -n "$REMOTE_HOME" ] && [[ "$REMOTE_HOME" == /* ]] || {
    echo "ERROR: could not verify remote home directory" >&2
    exit 2
  }
  PLIST_PATH="${REMOTE_HOME}/Library/LaunchAgents/${LABEL}.plist"
  REMOTE_UID="$(ssh "$BOX_ALIAS" id -u)"
  [[ "$REMOTE_UID" =~ ^[0-9]+$ ]] || { echo "ERROR: invalid remote uid" >&2; exit 2; }
  DOMAIN="gui/${REMOTE_UID}"
  PLIST_Q="$(quote_arg "$PLIST_PATH")"
  ssh "$BOX_ALIAS" "launchctl bootout ${DOMAIN} ${PLIST_Q} >/dev/null 2>&1 || true; rm -f ${PLIST_Q}"
  printf 'Remote hostname: %s\n' "$(ssh "$BOX_ALIAS" hostname)"
  printf '%s: uninstalled on %s\n' "$LABEL" "$BOX_ALIAS"
  exit 0
fi

if [ -f "$CONFIG_PATH" ]; then
  CONFIG_VALUES="$(python3 - "$CONFIG_PATH" "$BOX_ALIAS" <<'PY'
import json
import sys
from pathlib import Path

path, alias = Path(sys.argv[1]), sys.argv[2]
config = json.loads(path.read_text(encoding="utf-8"))
boxes = config.get("boxes")
if not isinstance(boxes, dict):
    raise SystemExit("ERROR: remote-CI config has no boxes map")
matches = [
    (name, box) for name, box in boxes.items()
    if isinstance(box, dict)
    and (name == alias or box.get("host") == alias or box.get("ssh_alias") == alias)
]
if len(matches) != 1:
    raise SystemExit(f"ERROR: expected one configured box matching {alias!r}; found {len(matches)}")
_, box = matches[0]
values = (
    box.get("python_interpreter", "/opt/homebrew/bin/python3"),
    box.get("managed_run_directory", "~/.goal-flight/remote-ci-managed"),
    box.get("expected_hostname", ""),
)
if any(not isinstance(value, str) or "\n" in value or "\t" in value for value in values):
    raise SystemExit("ERROR: configured node paths and hostname must be single-line strings")
print("\t".join(values))
PY
)"
  IFS=$'\t' read -r PYTHON_BIN MANAGED_ROOT EXPECTED_HOST <<< "$CONFIG_VALUES"
else
  PYTHON_BIN="/opt/homebrew/bin/python3"
  MANAGED_ROOT="~/.goal-flight/remote-ci-managed"
  EXPECTED_HOST=""
fi

if [[ "$PYTHON_BIN" != /* ]]; then
  echo "ERROR: configured python_interpreter must be absolute" >&2
  exit 2
fi

COMMIT="$(git -C "$REPO_ROOT" rev-parse --verify HEAD)"
[[ "$COMMIT" =~ ^[0-9a-f]{40}$|^[0-9a-f]{64}$ ]] || {
  echo "ERROR: current Git commit is not a full object id" >&2
  exit 2
}

REMOTE_NODE_ROOT='~/.goal-flight/remote-ci-node'
REMOTE_TARGET="${REMOTE_NODE_ROOT}/${COMMIT}"
REMOTE_LOG='~/.goal-flight/remote-ci-managed/logs/remote-ci-node.log'
REMOTE_PLIST='~/Library/LaunchAgents/com.goalflight.remote-ci-node.plist'

if [ "$DRY_RUN" -eq 1 ]; then
  printf 'Pinned commit: %s\n' "$COMMIT"
  printf 'Box alias: %s\n' "$BOX_ALIAS"
  printf 'Python interpreter: %s\n' "$PYTHON_BIN"
  printf 'Managed root: %s\n' "$MANAGED_ROOT"
  printf '1. Verify the far-end hostname: ssh %s hostname\n' "$BOX_ALIAS"
  printf '2. Verify the configured interpreter: ssh %s test -x %s\n' "$BOX_ALIAS" "$PYTHON_BIN"
  printf '3. Create the pinned checkout directory: ssh %s mkdir -p %s\n' "$BOX_ALIAS" "$REMOTE_TARGET"
  printf '4. Deploy the pinned archive: git -C %s archive --format=tar %s | ssh %s tar -xf - -C %s\n' "$REPO_ROOT" "$COMMIT" "$BOX_ALIAS" "$REMOTE_TARGET"
  printf '5. Atomically replace %s/current with a symlink to %s using Python os.replace; verify its resolved target\n' "$REMOTE_NODE_ROOT" "$REMOTE_TARGET"
  printf '6. Render and install %s at %s\n' "${TEMPLATE}" "$REMOTE_PLIST"
  printf '7. Bootstrap and kickstart %s with launchctl\n' "$LABEL"
  printf 'Log path: %s\n' "$REMOTE_LOG"
  exit 0
fi

REMOTE_HOSTNAME="$(ssh "$BOX_ALIAS" hostname)"
[ -n "$REMOTE_HOSTNAME" ] || { echo "ERROR: remote hostname was empty" >&2; exit 2; }
if [ -n "$EXPECTED_HOST" ] && [ "$REMOTE_HOSTNAME" != "$EXPECTED_HOST" ]; then
  printf 'ERROR: hostname mismatch for %s: expected %s, received %s\n' \
    "$BOX_ALIAS" "$EXPECTED_HOST" "$REMOTE_HOSTNAME" >&2
  exit 2
fi
printf 'Remote hostname: %s\n' "$REMOTE_HOSTNAME"
ssh "$BOX_ALIAS" "test -x $(quote_arg "$PYTHON_BIN")" || {
  printf 'ERROR: configured Python interpreter is not executable on %s: %s\n' \
    "$BOX_ALIAS" "$PYTHON_BIN" >&2
  exit 2
}

REMOTE_HOME="$(ssh "$BOX_ALIAS" 'printf "%s\n" "$HOME"')"
[ -n "$REMOTE_HOME" ] && [[ "$REMOTE_HOME" == /* ]] || {
  echo "ERROR: could not read an absolute remote home directory" >&2
  exit 2
}
case "$MANAGED_ROOT" in
  '~') MANAGED_ROOT_ABS="$REMOTE_HOME" ;;
  '~/'*) MANAGED_ROOT_ABS="${REMOTE_HOME}/${MANAGED_ROOT#~/}" ;;
  /*) MANAGED_ROOT_ABS="$MANAGED_ROOT" ;;
  *) echo "ERROR: managed_run_directory must be absolute or home-relative" >&2; exit 2 ;;
esac
REMOTE_NODE_ROOT_ABS="${REMOTE_HOME}/.goal-flight/remote-ci-node"
REMOTE_TARGET_ABS="${REMOTE_NODE_ROOT_ABS}/${COMMIT}"
REMOTE_CURRENT_ABS="${REMOTE_NODE_ROOT_ABS}/current"
LOG_PATH="${REMOTE_HOME}/.goal-flight/remote-ci-managed/logs/remote-ci-node.log"
PLIST_PATH="${REMOTE_HOME}/Library/LaunchAgents/${LABEL}.plist"
MARKER_PATH="${REMOTE_TARGET_ABS}/.goalflight-commit"

ssh "$BOX_ALIAS" "mkdir -p $(quote_arg "$REMOTE_TARGET_ABS") $(quote_arg "$(dirname "$LOG_PATH")") $(quote_arg "$(dirname "$PLIST_PATH")")"
REMOTE_MARKER="$(ssh "$BOX_ALIAS" "if test -f $(quote_arg "$MARKER_PATH"); then cat $(quote_arg "$MARKER_PATH"); fi")"
if [ -n "$REMOTE_MARKER" ] && [ "$REMOTE_MARKER" != "$COMMIT" ]; then
  echo "ERROR: pinned checkout marker does not match its commit directory" >&2
  exit 2
fi
if [ "$REMOTE_MARKER" != "$COMMIT" ] || ! ssh "$BOX_ALIAS" "test -f $(quote_arg "${REMOTE_TARGET_ABS}/scripts/goalflight_remote_ci_node.py")"; then
  git -C "$REPO_ROOT" archive --format=tar "$COMMIT" \
    | ssh "$BOX_ALIAS" "tar -xf - -C $(quote_arg "$REMOTE_TARGET_ABS")"
  ssh "$BOX_ALIAS" "printf '%s\\n' '$COMMIT' > $(quote_arg "$MARKER_PATH")"
fi
REPLACE_LINK_CODE='import os,sys; temporary,current,target=sys.argv[1:]; os.symlink(target,temporary); os.replace(temporary,current); resolved=os.path.realpath(current); resolved==os.path.realpath(target) or sys.exit("current symlink target mismatch"); print("Current checkout resolves to: "+resolved)'
ssh "$BOX_ALIAS" "rm -f $(quote_arg "${REMOTE_NODE_ROOT_ABS}/.current-${COMMIT}") && $(quote_arg "$PYTHON_BIN") -c $(quote_arg "$REPLACE_LINK_CODE") $(quote_arg "${REMOTE_NODE_ROOT_ABS}/.current-${COMMIT}") $(quote_arg "$REMOTE_CURRENT_ABS") $(quote_arg "$REMOTE_TARGET_ABS")"

: "${TMPDIR:?TMPDIR must be set for the installer scratch file}"
PLIST_TMP="$(mktemp "${TMPDIR%/}/remote-ci-node-plist.XXXXXX")"
trap 'rm -f "$PLIST_TMP"' EXIT
NODE_SCRIPT="${REMOTE_CURRENT_ABS}/scripts/goalflight_remote_ci_node.py"
python3 - "$TEMPLATE" "$PYTHON_BIN" "$NODE_SCRIPT" "$MANAGED_ROOT_ABS" \
  "$REMOTE_HOME" "$REMOTE_TARGET_ABS" "$LOG_PATH" > "$PLIST_TMP" <<'PY'
import html
import sys
from pathlib import Path

template, python, node_script, managed_root, home, checkout, log_path = sys.argv[1:]
text = Path(template).read_text(encoding="utf-8")
values = {
    "PYTHON": python,
    "NODE_SCRIPT": node_script,
    "MANAGED_ROOT": managed_root,
    "HOME": home,
    "WORKING_DIRECTORY": checkout,
    "LOG_PATH": log_path,
}
for key, value in values.items():
    text = text.replace(f"@{key}@", html.escape(value, quote=True))
print(text, end="")
PY
scp "$PLIST_TMP" "$BOX_ALIAS:$PLIST_PATH"
ssh "$BOX_ALIAS" "plutil -lint $(quote_arg "$PLIST_PATH") >/dev/null"
REMOTE_UID="$(ssh "$BOX_ALIAS" id -u)"
[[ "$REMOTE_UID" =~ ^[0-9]+$ ]] || { echo "ERROR: invalid remote uid" >&2; exit 2; }
DOMAIN="gui/${REMOTE_UID}"
PLIST_Q="$(quote_arg "$PLIST_PATH")"
ssh "$BOX_ALIAS" "launchctl bootout ${DOMAIN} ${PLIST_Q} >/dev/null 2>&1 || true; launchctl bootstrap ${DOMAIN} ${PLIST_Q} && launchctl kickstart -k ${DOMAIN}/${LABEL}"
printf '%s: installed commit %s on %s\n' "$LABEL" "$COMMIT" "$REMOTE_HOSTNAME"
printf 'Current checkout: %s\n' "$REMOTE_CURRENT_ABS"
printf 'Log path: %s\n' "$LOG_PATH"
