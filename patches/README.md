# Vendored Patches

## claude-code-cli-acp 2.1.169 TUI submit stopgap

- Upstream repo: https://github.com/moabualruz/claude-code-cli-acp
- Pinned base commit: `c93f4f4` (`claude-code-cli-acp@0.1.1`)
- Fix commit used to generate the patch: `136b97e`
- Upstream PR: https://github.com/moabualruz/claude-code-cli-acp/pull/1
- Vendored patch: `patches/claude-code-cli-acp-2.1.169-tui-submit.patch`

This is a STOPGAP for Claude Code 2.1.169 TUI submit behavior until upstream
merges the PR and ships a fixed `claude-code-cli-acp` release. Do not pin users
to the PR branch or mutable diff.

Apply manually with:

```sh
scripts/install_claude_acp_patch.sh
```

Requirements:

- `claude-code-cli-acp@0.1.1` or older installed through npm.
- `git`, `node`, `npm`, and Rust `cargo`.
- macOS requires ad-hoc re-signing after the binary swap; the apply script runs
  `xattr -c` and `codesign -s - --force` on Darwin.

Revert:

```sh
NPM_ROOT="$(npm root -g)"
RESOLVER_DIR="$(mktemp -d)"
trap 'rm -rf "$RESOLVER_DIR"' EXIT
ln -s "$NPM_ROOT" "$RESOLVER_DIR/node_modules"
BIN_PATH="$(
  cd "$RESOLVER_DIR" && node --input-type=module <<'NODE'
import { fileURLToPath } from "url";
const platform = process.platform;
const archMap = { x64: "x64", arm64: "arm64" };
const arch = archMap[process.arch];
const exe = platform === "win32" ? "claude-code-cli-acp.exe" : "claude-code-cli-acp";
const pkg = `claude-code-cli-acp-${platform}-${arch}`;
const url = await import.meta.resolve(`${pkg}/bin/${exe}`);
console.log(fileURLToPath(url));
NODE
)"
cp "$BIN_PATH.orig" "$BIN_PATH"
chmod 755 "$BIN_PATH"
```

If the npm package now reports a version newer than `0.1.1`, skip this stopgap:
upstream should carry the fix.
