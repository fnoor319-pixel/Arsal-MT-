@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Trading Machine FINAL V5.5 SUPERLEARNER - Install (Long Path Safe)
call "%~dp0_ENV.bat"

if not exist "%TM_RUNTIME%" mkdir "%TM_RUNTIME%" >nul 2>&1
if not exist "%TM_RUNTIME%\tmp" mkdir "%TM_RUNTIME%\tmp" >nul 2>&1
if not exist "%TM_RUNTIME%\pip-cache" mkdir "%TM_RUNTIME%\pip-cache" >nul 2>&1
set "TEMP=%TM_RUNTIME%\tmp"
set "TMP=%TM_RUNTIME%\tmp"
set "PIP_CACHE_DIR=%TM_RUNTIME%\pip-cache"

echo Python environment: %TM_VENV%
echo Project folder: %CD%
echo.

if exist "%TM_PYTHON%" (
  "%TM_PYTHON%" -V >nul 2>&1
  if not errorlevel 1 goto :packages
)

if exist "%TM_VENV%" rmdir /s /q "%TM_VENV%" >nul 2>&1
where py >nul 2>&1 || goto :try_python
py -3.13 -m venv "%TM_VENV%" >nul 2>&1
if exist "%TM_PYTHON%" goto :packages
py -3.12 -m venv "%TM_VENV%" >nul 2>&1
if exist "%TM_PYTHON%" goto :packages

:try_python
where python >nul 2>&1 || goto :nopython
python -m venv "%TM_VENV%"
if not exist "%TM_PYTHON%" goto :nopython

:packages
"%TM_PYTHON%" -m pip install --upgrade pip
if errorlevel 1 goto :fail
"%TM_PYTHON%" -m pip install -r requirements.txt
if errorlevel 1 goto :fail
"%TM_PYTHON%" trading_machine.py init
if errorlevel 1 goto :fail
"%TM_PYTHON%" offline_self_test.py
if errorlevel 1 goto :fail

echo.
echo Installation and offline self-test complete.
echo Short external environment used: %TM_VENV%
echo Any old local .venv folder is ignored and may be deleted after all old windows are closed.
if /I "%~1"=="--no-pause" exit /b 0
pause
exit /b 0

:nopython
echo Python 3.12 ya 3.13 64-bit install karein aur Add Python to PATH select karein.
if /I "%~1"=="--no-pause" exit /b 1
pause
exit /b 1

:fail
echo Installation/self-test failed. Upar exact error dekhein.
echo Runtime path: %TM_RUNTIME%
if /I "%~1"=="--no-pause" exit /b 2
pause
exit /b 2
