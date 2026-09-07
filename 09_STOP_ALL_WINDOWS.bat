@echo off
setlocal EnableExtensions
cd /d "%~dp0"
call "%~dp0_ENV.bat"

title Trading Machine - Stop All Five Engines

echo Hidden supervisor aur tamam five engine windows stop ki ja rahi hain...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$patterns = 'machine_supervisor.py|engine_window.py|04_LIVE_STRATEGY_GENERATOR_24_7.bat|05_HISTORICAL_BACKTESTER_24_7.bat|06_LIVE_SHADOW_VALIDATOR_24_7.bat|07_APPROVED_LIBRARY_SELECTOR_24_7.bat|08_MT5_DEMO_EXECUTOR_24_7.bat|trading_machine.py';" ^
  "$targets = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and $_.CommandLine -match $patterns } | Sort-Object ProcessId -Descending;" ^
  "foreach ($p in $targets) { try { Invoke-CimMethod -InputObject $p -MethodName Terminate | Out-Null } catch {} };" ^
  "Write-Host ('Stopped processes/windows: ' + @($targets).Count)"

for %%F in (machine_supervisor.ready machine_supervisor.pid machine_supervisor.lock engine_generator.pid engine_backtester.pid engine_shadow.pid engine_library.pid engine_executor.pid) do (
  if exist "%~dp0%%F" del /q "%~dp0%%F" >nul 2>&1
  if exist "%TM_STATE_DIR%\%%F" del /q "%TM_STATE_DIR%\%%F" >nul 2>&1
)
echo Runtime state cleaned: %TM_STATE_DIR%
echo Done.
pause
