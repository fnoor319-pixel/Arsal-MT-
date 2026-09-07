@echo off
setlocal
cd /d "%~dp0"
call "%~dp0_ENV.bat"
if errorlevel 1 goto :fail
"%TM_PYTHON%" -u "%~dp0shadow_fill_integrity_status.py"
set "RC=%ERRORLEVEL%"
pause
exit /b %RC%
:fail
echo ERROR: environment setup failed.
pause
exit /b 1
