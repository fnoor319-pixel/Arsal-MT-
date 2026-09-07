@echo off
setlocal EnableExtensions
cd /d "%~dp0"
call "%~dp0_ENV.bat"

title Trading Machine V11 CANONICAL DEMO SCALPER - Start Five Engines

if not exist "trading_machine.py" goto :badfolder
if not exist "machine_supervisor.py" goto :badfolder
if not exist "start_supervisor.py" goto :badfolder
if not exist "engine_window.py" goto :badfolder

if not exist "%TM_PYTHON%" (
  echo Python environment missing. Installer is starting...
  call "01_INSTALL.bat" --no-pause
  if errorlevel 1 goto :installfail
)

echo.
echo Checking/repairing required Python dependencies...
"%TM_PYTHON%" -u ensure_dependencies.py
if errorlevel 1 (
  echo.
  echo ERROR: Python dependency check/repair failed.
  echo Internet connection check karein aur 01_INSTALL.bat run karein.
  pause
  exit /b 7
)

if not exist "logs" mkdir "logs"

echo.
echo Checking MT5 DEMO connection...
call "02_MT5_DIAGNOSTIC.bat"
if errorlevel 1 (
  echo.
  echo ERROR: MT5 diagnostic failed.
  echo MT5 open karein, DEMO account login karein, phir dobara start karein.
  pause
  exit /b 2
)

echo.
echo Checking V11 configuration locally - zero API tokens...
"%TM_PYTHON%" v11_startup_check.py
if errorlevel 1 (
  echo.
  echo ERROR: V11 safety configuration check failed.
  echo Upar shown settings theek karein. GPT key V11 ke liye optional hai.
  pause
  exit /b 4
)

echo.
echo Checking DEMO autotrade candidate pools and hard locks...
"%TM_PYTHON%" -u demo_autotrade_readiness.py
if errorlevel 1 (
  echo.
  echo ERROR: DEMO autotrade readiness failed.
  echo Upar shown item fix karein; koi order send nahi hoga.
  pause
  exit /b 6
)

echo.
echo ========================================================================
echo TRADING MACHINE V11 CANONICAL LEARNING SCALPER - FIVE ENGINES - DEMO HARD LOCK
echo 1. Scalp-first strategy generator + regime/session memory
echo 2. Fast prescreen + historical + OOS + walk-forward backtester
echo 3. Current-market live shadow validator + learning
echo 4. Scalp-priority library + symbol/regime/session selector
echo 5. V11 canonical exits + continuous tick learning + optional async Luna + risk + MT5 DEMO
echo Hidden supervisor closed/crashed engine ko restart karega.
echo ========================================================================
echo.

"%TM_PYTHON%" -u "start_supervisor.py"
if errorlevel 1 (
  echo.
  echo START FAILED. Upar exact reason aur supervisor log diya gaya hai.
  echo Extra log: "%CD%\logs\supervisor_hidden.log"
  pause
  exit /b 5
)

echo.
echo Trading Machine start command completed successfully.
echo Five engine windows successfully khul chuki hain.
echo Stop karne ke liye 09_STOP_ALL_WINDOWS.bat use karein.
timeout /t 4 /nobreak >nul
exit /b 0

:installfail
echo Installation failed. 01_INSTALL.bat ka error dekhein.
pause
exit /b 1

:badfolder
echo ERROR: Complete ZIP ko Extract All karein aur isi folder se BAT chalayein.
pause
exit /b 3
