@echo off
setlocal
cd /d "%~dp0"
call "%~dp0_ENV.bat"

if not exist "%TM_PYTHON%" call "01_INSTALL.bat"
"%TM_PYTHON%" trading_machine.py paper --hours 1 --interval 10
pause
