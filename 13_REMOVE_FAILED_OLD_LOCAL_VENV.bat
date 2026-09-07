@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Trading Machine - Remove Old Local Venv
if not exist ".venv" (
  echo Is folder mein old local .venv nahi mila.
  pause
  exit /b 0
)
echo Pehle tamam purani Trading Machine windows band karein.
echo Old local .venv delete ho raha hai; FINAL V5.5 SUPERLEARNER isay use nahi karta.
rmdir /s /q ".venv"
if exist ".venv" (
  echo Delete nahi hua. Purani Python/CMD windows close karke dobara chalayein.
  pause
  exit /b 1
)
echo Old local .venv removed.
pause
exit /b 0
