@echo off
setlocal EnableExtensions
cd /d "%~dp0"
call "%~dp0_ENV.bat"
title Trading Machine V11.1 CANONICAL LEARNING - ONE CLICK DEMO START

echo.
echo ========================================================================
echo TRADING MACHINE V11.1 CANONICAL LEARNING SCALPER - ONE CLICK DEMO START
echo DEMO ONLY: real/contest MT5 accounts are hard-blocked.
echo ========================================================================
echo.

if not exist "%TM_PYTHON%" (
  echo Python environment missing. Installing dependencies...
  call "01_INSTALL.bat" --no-pause
  if errorlevel 1 goto :fail
)

echo Checking/repairing required Python dependencies...
"%TM_PYTHON%" -u ensure_dependencies.py
if errorlevel 1 goto :fail

"%TM_PYTHON%" -u v11_startup_check.py
if errorlevel 1 goto :fail

echo.
echo Running offline regression/self-test...
"%TM_PYTHON%" -u offline_self_test.py
if errorlevel 1 goto :fail
"%TM_PYTHON%" -m unittest discover -s tests -p "test_v11*.py"
if errorlevel 1 goto :fail

echo.
echo Starting V11 local learning + optional Luna + MT5 DEMO pipeline...
call "00_START_ALL_24_7_DEMO.bat"
if errorlevel 1 goto :fail

exit /b 0

:fail
echo.
echo START ABORTED. Upar exact error fix karein; fail-closed mode mein order send nahi hoga.
pause
exit /b 2
