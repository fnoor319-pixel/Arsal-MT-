@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" scalp_health.py
) else (
  python scalp_health.py
)
echo.
pause
