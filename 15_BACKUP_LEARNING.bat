@echo off
setlocal
cd /d "%~dp0"
call "%~dp0_ENV.bat"
title Trading Machine FINAL V5.5 SUPERLEARNER - Backup Learning
if not exist "%TM_PYTHON%" call "01_INSTALL.bat"
"%TM_PYTHON%" -u "backup_learning.py"
pause
