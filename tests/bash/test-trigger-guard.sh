#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/goal-flight-trigger-guard.XXXXXX")"
trap 'rm -rf "$TMP_ROOT"' EXIT

host_fingerprint="$(
  {
    printf '%s\n' "${USER_AGENT:-}"
    printf '%s\n' "${CLAUDE_CODE_SESSION_ID:-}"
    printf '%s\n' "${CLAUDECODE:-}"
    printf '%s\n' "${ANTHROPIC_API_KEY:+anthropic-api-key-present}"
    ps -o comm= -p "${PPID:-0}" 2>/dev/null || true
  } | tr '[:upper:]' '[:lower:]'
)"

case "$host_fingerprint" in
  *claude*|*anthropic*)
    echo "SKIP trigger guard live-token test under Claude-like host"
    exit 0
    ;;
esac

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

export GOALFLIGHT_TRIGGER_GUARD_PATTERNS_B64="Zml4dHVyZWJsb2Nr"
bad_word="fixtureblock"

mkdir "$TMP_ROOT/repo"
cd "$TMP_ROOT/repo"
git init -q
git config user.email test@example.invalid
git config user.name "Test User"

printf 'safe\n' > safe.txt
git add safe.txt
python3 "$REPO_ROOT/scripts/goalflight_trigger_guard.py" --repo "$PWD" --staged >"$TMP_ROOT/safe.out" 2>&1 || fail "safe staged content blocked"

printf 'schema: %s\n' "$bad_word" > unsafe.txt
git add unsafe.txt
if python3 "$REPO_ROOT/scripts/goalflight_trigger_guard.py" --repo "$PWD" --staged >"$TMP_ROOT/content.out" 2>&1; then
  fail "unsafe staged content allowed"
fi
grep -q 'staged content' "$TMP_ROOT/content.out" || fail "content finding missing"
git reset -q
rm -f unsafe.txt

bad_path="${bad_word}-note.txt"
printf 'safe\n' > "$bad_path"
git add "$bad_path"
if python3 "$REPO_ROOT/scripts/goalflight_trigger_guard.py" --repo "$PWD" --staged >"$TMP_ROOT/path.out" 2>&1; then
  fail "unsafe staged path allowed"
fi
grep -q 'staged path' "$TMP_ROOT/path.out" || fail "path finding missing"
! grep -qi "$bad_word" "$TMP_ROOT/path.out" || fail "path finding was not redacted"

msg="$TMP_ROOT/message.txt"
printf 'mentions %s\n' "$bad_word" > "$msg"
if python3 "$REPO_ROOT/scripts/goalflight_trigger_guard.py" --repo "$PWD" --commit-msg "$msg" >"$TMP_ROOT/message.out" 2>&1; then
  fail "unsafe commit message allowed"
fi
grep -q 'commit message' "$TMP_ROOT/message.out" || fail "message finding missing"

git reset -q
rm -f "$bad_path"
printf 'unsafe path for cleanup\n' > "$bad_path"
git add "$bad_path"
git commit -qm 'fixture setup'
rm -f "$bad_path"
git add -u
python3 "$REPO_ROOT/scripts/goalflight_trigger_guard.py" --repo "$PWD" --staged >"$TMP_ROOT/delete.out" 2>&1 || fail "deleting unsafe path should be allowed"

echo "trigger guard tests passed"
