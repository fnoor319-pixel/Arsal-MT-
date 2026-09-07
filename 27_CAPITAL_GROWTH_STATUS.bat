@echo off
setlocal
cd /d "%~dp0"
call "%~dp0_ENV.bat"
if not exist "%TM_PYTHON%" call "01_INSTALL.bat"
"%TM_PYTHON%" -u capital_growth_status.py
pause
