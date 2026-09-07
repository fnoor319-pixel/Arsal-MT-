@echo off
setlocal
cd /d "%~dp0"
call "%~dp0_ENV.bat"

if not exist "%TM_PYTHON%" call "01_INSTALL.bat"
"%TM_PYTHON%" trading_machine.py diagnose
if errorlevel 1 exit /b %errorlevel%
"%TM_PYTHON%" trading_machine.py verify-demo
exit /b %errorlevel%
