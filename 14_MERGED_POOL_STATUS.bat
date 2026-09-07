@echo off
setlocal
cd /d "%~dp0"
call "%~dp0_ENV.bat"
title Trading Machine FINAL V5.5 SUPERLEARNER - Merged Pool Status
if not exist "%TM_PYTHON%" call "01_INSTALL.bat"
"%TM_PYTHON%" -u "merged_pool_status.py"
pause
