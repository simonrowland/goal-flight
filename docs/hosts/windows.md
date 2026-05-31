# Windows Host Notes

Goal Flight supports native Windows for the read/plan control plane only.
**WSL is the required baseline for full worker dispatch.** Native dispatch is
intentionally refused; Goal Flight does not ship a native-Win32 dispatch port.

## Native Windows Support

Native Windows can run:

- activation/status helpers
- doctor/readiness checks
- action routing
- capacity and ledger reads
- documentation and planning commands

Native Windows cannot run live worker dispatch. Dispatch entry points refuse
with a copy-pasteable WSL path:

```powershell
wsl --install
```

Then reboot if Windows asks, open the Linux distro once so it finishes
installation, install Goal Flight there, and re-run dispatch inside WSL.

Native cleanup is degraded by design: when the control plane finds stale worker
PIDs it tracks, it kills those individual PIDs only. POSIX process-group reaping
is available in WSL, not native Windows.

## WSL Required Dispatch Baseline

Doctor reports a top-level `wsl` JSON field. A native Windows machine is ready
for full functionality only after the operator runs inside an installed WSL
distro. The native probe is strict:

- `wsl.exe` must exist.
- `wsl -l -q` must list at least one installed distro.
- `wsl.exe` present with no distros is **not** usable.

`wsl -l -q` can emit UTF-16LE with NUL bytes. If doctor reports zero distros on
a machine that obviously has one, inspect `wsl.probe.stdout` / `stderr` first;
the probe decodes and strips NULs before splitting lines.

Init must ask before running `wsl --install`. The prompt must state that install
can require admin elevation, downloads a Linux distro, and may require a reboot.
If the operator declines, Goal Flight writes
`docs-private/windows-wsl-install-declined.json` so init does not nag every run.
Decline or pending install does not block read/plan mode; dispatch remains
honestly refused on native Windows.

## Install

PowerShell native read/plan install:

```powershell
git clone https://github.com/simonrowland/goal-flight.git "$env:USERPROFILE\.goal-flight"
cd "$env:USERPROFILE\.goal-flight"
.\bin\goalflight.ps1 core doctor read
py -3 .\scripts\goalflight_doctor.py --project-root C:\path\to\project --text
```

Command Prompt launcher:

```cmd
git clone https://github.com/simonrowland/goal-flight.git "%USERPROFILE%\.goal-flight"
cd "%USERPROFILE%\.goal-flight"
bin\goalflight.cmd core doctor read
py -3 scripts\goalflight_doctor.py --project-root C:\path\to\project --text
```

Do not use `~` in `cmd.exe`; it creates a literal `~` directory. Use
`%USERPROFILE%` in `cmd.exe` or `$env:USERPROFILE` in PowerShell.

## Python Command Mapping

POSIX command docs intentionally use literal `python3`. On native Windows,
prefer the launcher wrappers:

```powershell
.\bin\goalflight.ps1 <domain> <resource> <verb>
```

```cmd
bin\goalflight.cmd <domain> <resource> <verb>
```

The native launchers probe `GOALFLIGHT_PYTHON`, then `py -3`, then `python`,
then `python3`. For direct script calls, translate POSIX `python3 script.py`
to `py -3 script.py` unless your install exposes `python` or `python3`.

## WSL Two-Install Procedure

Keep native Windows and WSL as two separate installs:

1. Native Windows install under `%USERPROFILE%\.goal-flight` for read/plan and
   host documentation.
2. WSL install under `$HOME/.goal-flight` inside the Linux distro for dispatch.
3. After updating one install, update or re-clone the other. They do not share
   symlinks, virtualenvs, Git config, or line-ending settings.

Inside WSL:

```bash
git clone https://github.com/simonrowland/goal-flight.git "$HOME/.goal-flight"
cd "$HOME/.goal-flight"
./install.sh codex
python${GOALFLIGHT_PYTHON_MAJOR:-3} scripts/goalflight_doctor.py --project-root /path/to/project --text
```

Keep the WSL checkout, target project, `GOALFLIGHT_STATE_DIR`, fleet directory,
and dispatch `worktrees/` on the WSL-native filesystem, such as under `$HOME`.
Do not dispatch from `/mnt/<drive>`; DrvFs can make POSIX `flock` unreliable.
Build the ACP virtualenv inside WSL with Linux `python3` and use
`$HOME/.goal-flight/venvs/acp-0.10/bin/python`. Never share a Windows checkout
or `Scripts/python.exe` virtualenv with the WSL dispatch install.

## Capability Matrix

| Capability | Native Windows | WSL |
|---|---:|---:|
| Read activation/status | yes | yes |
| Doctor/readiness checks | yes | yes |
| Action router launcher | yes | yes |
| Capacity/ledger read layer | yes | yes |
| Live ACP dispatch | no, refused | yes, supported baseline |
| File-backed review dispatch | no, refused | yes, supported baseline |
| Bash-tail watcher | skipped | yes |
| OS sandbox | no; macOS-only | no; use worktree + worker sandbox |
| Context-discipline hooks | skipped | yes, POSIX/Git-Bash path |
| Stale worker cleanup | degraded tracked-pid only | POSIX process group |

## Manual Acceptance Gate

Native Windows first action is the Python suite:

```powershell
py -3 .\tests\run_python.py
```

The native runner executes `tests/python/test_*.py`. Windows-activated tests run
against the real host (`wsl.exe`, Windows `os.kill`, Windows paths, native
dispatch refusal). POSIX-only tests skip on native Windows with a visible
reminder:

- no usable WSL: install WSL with `wsl --install`, then run the POSIX suite in WSL
- usable WSL: run the POSIX suite inside WSL

The bash suite (`tests/bash/*.sh`) is POSIX/WSL-only and is intentionally not
part of the native Windows entry point. After native Python signal is collected,
run the POSIX layer from inside the distro:

```bash
./tests/run.sh
```

## OS Sandbox

`--os-sandbox` is macOS-only because it depends on `sandbox-exec`. On native
Windows, Linux, and WSL, Goal Flight leaves the dispatch-level OS sandbox off
or refuses explicit `read-only` / `workspace-write` requests with structured
`os_sandbox_platform_unsupported` readiness output. This is expected; use
dispatch worktree isolation plus the worker's own sandbox/approval policy.

## Context Discipline Hooks

Hook installation is skipped on native Windows. The hooks are POSIX/Git-Bash
only; context protection is advisory in native Windows sessions. Use WSL when
you need enforced hook behavior during dispatch.

## Git Line Endings

The repository carries `.gitattributes` LF normalization. Automatic update
checks use `git -c core.autocrlf=false` on Windows so line-ending conversion
does not make a clean checkout look dirty.
