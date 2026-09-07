@echo off
setlocal
cd /d "%~dp0"
call "%~dp0_ENV.bat"

title TM 2 of 5 - HISTORICAL OOS WALK-FORWARD BACKTESTER
if not exist "%TM_PYTHON%" call "01_INSTALL.bat"
:loop
"%TM_PYTHON%" -u trading_machine.py backtest-loop --hours 8760 --limit 180 --interval 15
if errorlevel 1 timeout /t 20 /nobreak >nul
goto loop
