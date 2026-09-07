@echo off
setlocal
cd /d "%~dp0"
call "%~dp0_ENV.bat"
if errorlevel 1 (
  echo ERROR: _ENV.bat failed.
  pause
  exit /b 1
)
if not defined TM_PYTHON (
  echo ERROR: TM_PYTHON was not set by _ENV.bat.
  pause
  exit /b 1
)
if not exist "%TM_PYTHON%" (
  echo Python environment missing: %TM_PYTHON%
  pause
  exit /b 1
)
"%TM_PYTHON%" -u "%~dp0v9_2_adaptive_scalper_status.py"
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" echo ERROR: status exited with code %RC%.
pause
exit /b %RC%
