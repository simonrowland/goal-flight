# Pinned Upstream Builds

## claude-code-cli-acp 2.1.169 TUI submit fix

- Upstream repo: https://github.com/moabualruz/claude-code-cli-acp
- Pinned merged fix commit: `14a5b0c`
- Upstream PR: https://github.com/moabualruz/claude-code-cli-acp/pull/1

`claude-code-cli-acp@0.1.1` is the current npm release and its per-platform
binary predates the Claude Code 2.1.169 TUI submit fix. Until npm publishes a
version newer than `0.1.1`, Goal Flight installs the npm package to lay down the
launcher, then builds upstream commit `14a5b0c` from source and swaps only the
installed platform binary.

Default install:

```sh
./install.sh claude-acp
```

Requirements:

- `claude-code-cli-acp@0.1.1` or older installed through npm.
- `git`, `node`, `npm`, and Rust `cargo`.
- macOS requires ad-hoc re-signing after the binary swap; the build script runs
  `xattr -c` and `codesign -s - --force` on Darwin.
- The compatibility script name remains `scripts/install_claude_acp_patch.sh`
  for older callers, but it no longer applies a local patch.

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

If the npm package reports a version newer than `0.1.1`, the pinned build is
skipped: the npm release should carry the fix.
