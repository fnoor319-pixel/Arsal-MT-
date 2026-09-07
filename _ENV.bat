@echo off
rem Shared short Python environment path.
rem This avoids Windows WinError 206 when the project is inside a long OneDrive/Desktop path.
if not defined LOCALAPPDATA set "LOCALAPPDATA=%USERPROFILE%\AppData\Local"
set "TM_RUNTIME=%LOCALAPPDATA%\TMFINAL"
set "TM_VENV=%TM_RUNTIME%\venv"
set "TM_PYTHON=%TM_VENV%\Scripts\python.exe"
rem Supervisor/worker PID and READY files stay outside OneDrive.
set "TM_STATE_DIR=%LOCALAPPDATA%\TMFINAL\run"
exit /b 0
