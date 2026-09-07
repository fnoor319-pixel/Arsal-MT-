@echo off
setlocal
cd /d "%~dp0"
call "%~dp0_ENV.bat"

title Trading Machine - Maximum M1 History
if not exist "%TM_PYTHON%" call "01_INSTALL.bat"
echo.
echo Target: 2,700,000 M1 bars per symbol, roughly five years for 24/7 markets.
echo Broker/MT5 jitni history dega utni hi save hogi; missing data invent nahi ho sakta.
echo MT5: Tools ^> Options ^> Charts ^> Max bars in chart ko maximum karein.
echo.
"%TM_PYTHON%" trading_machine.py history --bars 2700000
pause
