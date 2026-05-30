# Windows Host Notes

Goal Flight Phase 1 supports native Windows for the read/plan layer only.
Native dispatch is intentionally refused until Phase 2.

## Native Windows Support

Native Windows can run:

- activation/status helpers
- doctor/readiness checks
- action routing
- capacity and ledger reads
- documentation and planning commands

Native Windows cannot run live worker dispatch yet. Dispatch entry points refuse
with a copy-pasteable path:

```powershell
wsl --install
```

Then open the Linux distro, install Goal Flight there, and re-run the dispatch
inside WSL. Phase 2 enables native Windows dispatch.

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

## Capability Matrix

| Capability | Native Windows | WSL |
|---|---:|---:|
| Read activation/status | yes | yes |
| Doctor/readiness checks | yes | yes |
| Action router launcher | yes | yes |
| Capacity/ledger read layer | yes | yes |
| Live ACP dispatch | no, refused | yes |
| File-backed review dispatch | no, refused | yes |
| Bash-tail watcher | skipped | yes |
| OS sandbox | no; macOS-only | no; use worktree + worker sandbox |
| Context-discipline hooks | skipped | yes, POSIX/Git-Bash path |

## OS Sandbox

`--os-sandbox` is macOS-only because it depends on `sandbox-exec`. On Windows,
use dispatch worktree isolation plus the worker's own `--sandbox`. Drop
`--os-sandbox` to proceed, or use WSL for the dispatch.

## Context Discipline Hooks

Hook installation is skipped on native Windows. The hooks are POSIX/Git-Bash
only; context protection is advisory in native Windows sessions. Use WSL when
you need enforced hook behavior during dispatch.

## Git Line Endings

The repository carries `.gitattributes` LF normalization. Automatic update
checks use `git -c core.autocrlf=false` on Windows so line-ending conversion
does not make a clean checkout look dirty.
