$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$Script = Join-Path $Root "scripts\goalflight_actions.py"

# GOALFLIGHT_PYTHON is accepted-watch per the SC-13 sweep: interpreter selector only.
if ($env:GOALFLIGHT_PYTHON) {
  try {
    & $env:GOALFLIGHT_PYTHON --version *> $null
    if ($LASTEXITCODE -eq 0) {
      & $env:GOALFLIGHT_PYTHON $Script route --exec @args
      exit $LASTEXITCODE
    }
  } catch {}
}

$candidates = @(
  @{ Command = "py"; Args = @("-3") },
  @{ Command = "python"; Args = @() },
  @{ Command = "python3"; Args = @() }
)

foreach ($candidate in $candidates) {
  $extra = $candidate.Args
  try {
    & $candidate.Command @extra --version *> $null
    if ($LASTEXITCODE -ne 0) {
      continue
    }
    & $candidate.Command @extra $Script route --exec @args
    exit $LASTEXITCODE
  } catch {}
}

Write-Error "goalflight: Python 3 not found; set GOALFLIGHT_PYTHON"
exit 127
