@echo off
setlocal
cd /d "%~dp0"
call "%~dp0_ENV.bat"
if not exist "%TM_PYTHON%" call "01_INSTALL.bat"
"%TM_PYTHON%" -u demo_autotrade_readiness.py
set RC=%ERRORLEVEL%
echo.
if not "%RC%"=="0" (
  echo Readiness FAILED. Fix the item shown above before 00_START_ALL_24_7_DEMO.bat.
) else (
  echo Demo autotrade readiness PASSED.
)
pause
exit /b %RC%
