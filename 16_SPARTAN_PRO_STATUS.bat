@echo off
setlocal
cd /d "%~dp0"
call "%~dp0_ENV.bat"
title Spartan-Scalper-Pro Status
if not exist "%TM_PYTHON%" call "01_INSTALL.bat"
"%TM_PYTHON%" -u spartan_status.py
pause
