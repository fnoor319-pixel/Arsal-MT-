@echo off
setlocal
cd /d "%~dp0"
call "%~dp0_ENV.bat"
"%TM_PYTHON%" -u post_trade_status.py
pause
