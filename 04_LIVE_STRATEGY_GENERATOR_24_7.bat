@echo off
setlocal
cd /d "%~dp0"
call "%~dp0_ENV.bat"

title TM 1 of 5 - LIVE STRATEGY GENERATOR - DEMO SAFE
if not exist "%TM_PYTHON%" call "01_INSTALL.bat"
:loop
"%TM_PYTHON%" -u trading_machine.py generator-loop --hours 8760 --random 8 --guided 8 --children 1 --interval 60
if errorlevel 1 timeout /t 20 /nobreak >nul
goto loop
