@echo off
setlocal
set "ROOT=%~dp0.."
set "PYTHON_EXE="
set "PYTHON_ARGS="

if defined GOALFLIGHT_PYTHON (
  "%GOALFLIGHT_PYTHON%" --version >nul 2>nul
  if not errorlevel 1 (
    set "PYTHON_EXE=%GOALFLIGHT_PYTHON%"
    goto run
  )
)

py -3 --version >nul 2>nul
if not errorlevel 1 (
  set "PYTHON_EXE=py"
  set "PYTHON_ARGS=-3"
  goto run
)

python --version >nul 2>nul
if not errorlevel 1 (
  set "PYTHON_EXE=python"
  goto run
)

python3 --version >nul 2>nul
if not errorlevel 1 (
  set "PYTHON_EXE=python3"
  goto run
)

echo goalflight: Python 3 not found; set GOALFLIGHT_PYTHON 1>&2
exit /b 127

:run
"%PYTHON_EXE%" %PYTHON_ARGS% "%ROOT%\scripts\goalflight_actions.py" route --exec %*
exit /b %ERRORLEVEL%
