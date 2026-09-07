@echo off
setlocal
cd /d "%~dp0"
call "%~dp0_ENV.bat"

title Trading Machine - Offline Self Test
if not exist "%TM_PYTHON%" call "01_INSTALL.bat"
"%TM_PYTHON%" offline_self_test.py
pause
