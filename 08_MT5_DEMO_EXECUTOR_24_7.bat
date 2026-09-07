@echo off
setlocal
cd /d "%~dp0"
call "%~dp0_ENV.bat"

title TM 5 of 5 - MT5 DEMO EXECUTOR - REAL ACCOUNT HARD BLOCKED
if not exist "%TM_PYTHON%" call "01_INSTALL.bat"
:loop
"%TM_PYTHON%" -u trading_machine.py demo --hours 8760 --interval 5
if errorlevel 1 timeout /t 20 /nobreak >nul
goto loop
