@echo off
setlocal EnableExtensions
cd /d "%~dp0"
call "%~dp0_ENV.bat"
if not exist "%TM_PYTHON%" (
  echo Python environment missing. Run 01_INSTALL.bat first.
  pause
  exit /b 2
)
"%TM_PYTHON%" configure_gpt.py
if errorlevel 1 (
  echo GPT configuration failed.
  pause
  exit /b 2
)
echo.
echo Configuration saved. Testing connection now...
"%TM_PYTHON%" gpt_connection_test.py
pause
