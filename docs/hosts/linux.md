# Linux Host Notes

Goal Flight treats Linux and WSL as POSIX dispatch hosts. Worker dispatch runs
inside the Linux environment; native Windows remains read/plan only.

## OS Sandbox

`--os-sandbox` is macOS-only because the runner implementation uses
`sandbox-exec`. On Linux and WSL, Goal Flight leaves the dispatch-level OS
sandbox off. This is expected, not a readiness failure.

Use project worktrees plus the worker's own sandbox/approval policy for Linux
isolation. If an adapter explicitly requests `read-only` or `workspace-write`
OS sandbox on Linux/WSL, readiness should refuse it structurally and report
`os_sandbox_platform_unsupported`.

## WSL Filesystem Baseline

Run Goal Flight entirely inside WSL for live dispatch. Keep the checkout,
`GOALFLIGHT_STATE_DIR`, fleet directory, fleet locks, and `worktrees/` on the
WSL-native filesystem, such as under `$HOME`. Do not use `/mnt/<drive>` for
state or dispatch worktrees; DrvFs does not provide reliable POSIX `flock`
semantics for Goal Flight locks.

Build the ACP venv inside Linux:

```bash
ACP_VENV="$HOME/.goal-flight/venvs/acp-0.10"
python3 -m venv "$ACP_VENV"
"$ACP_VENV/bin/python" -m pip install -r "$HOME/.goal-flight/requirements.txt"
```

Do not share a Windows checkout, Windows Git worktree, or Windows
`Scripts/python.exe` virtualenv with the WSL dispatch install.
