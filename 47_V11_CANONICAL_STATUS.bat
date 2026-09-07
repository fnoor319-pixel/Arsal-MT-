@echo off
setlocal EnableExtensions
cd /d "%~dp0"
call "%~dp0_ENV.bat"
"%TM_PYTHON%" -u v11_status.py --hours 6
pause
