@echo off
setlocal
cd /d "%~dp0"
call "%~dp0_ENV.bat"

title TM 4 of 5 - APPROVED STRATEGY LIBRARY AND MARKET SELECTOR
if not exist "%TM_PYTHON%" call "01_INSTALL.bat"
:loop
"%TM_PYTHON%" -u trading_machine.py library --hours 8760 --interval 30
if errorlevel 1 timeout /t 20 /nobreak >nul
goto loop
