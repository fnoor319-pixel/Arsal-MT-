@echo off
setlocal
cd /d "%~dp0"
call "%~dp0_ENV.bat"
if errorlevel 1 goto :fail
if not defined TM_PYTHON goto :fail
if not exist "%TM_PYTHON%" goto :fail
"%TM_PYTHON%" -u "%~dp0v8_institutional_status.py"
set "RC=%ERRORLEVEL%"
pause
exit /b %RC%
:fail
echo ERROR: Python environment is not ready. Run 01_INSTALL.bat once.
pause
exit /b 1
