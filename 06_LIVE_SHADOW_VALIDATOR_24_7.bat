@echo off
setlocal
cd /d "%~dp0"
call "%~dp0_ENV.bat"

title TM 3 of 5 - LIVE SHADOW VALIDATOR - NO BROKER ORDERS
if not exist "%TM_PYTHON%" call "01_INSTALL.bat"
:loop
"%TM_PYTHON%" -u trading_machine.py shadow --hours 8760 --interval 10
if errorlevel 1 timeout /t 20 /nobreak >nul
goto loop
