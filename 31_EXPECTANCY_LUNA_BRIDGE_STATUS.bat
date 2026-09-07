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
"%TM_PYTHON%" -u "%~dp0expectancy_luna_bridge_status.py"
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" echo ERROR: expectancy_luna_bridge_status.py exited with code %RC%.
pause
exit /b %RC%
