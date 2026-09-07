@echo off
setlocal
cd /d "%~dp0"
call "%~dp0_ENV.bat"
echo.
echo MANUAL LUNA NETWORK TEST - this intentionally uses one small paid API request.
echo Normal 00_RUN_ME_DEMO startup does NOT use an API request in V8.
echo.
"%TM_PYTHON%" -u gpt_connection_test.py
set "RC=%ERRORLEVEL%"
pause
exit /b %RC%
