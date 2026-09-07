V11.1.0 CODE UPDATE - CURRENT INSTRUCTIONS
=======================================
1. Check MT5 positions/orders, then stop the old bot with 09_STOP_ALL_WINDOWS.bat.
   Stopping the software does not close a broker position.
2. Back up the entire existing bot folder before replacing code.
3. Extract the CODE_UPDATE ZIP directly into that existing inner bot folder.
4. Allow replacement of code files. No database, WAL/SHM, data, logs or .env is
   included in this update, so your newest local learning and key remain there.
5. Read V11_1_UPGRADE_README.txt, then start 00_RUN_ME_DEMO.bat on a DEMO account.
6. After 10-15 minutes use 47_V11_CANONICAL_STATUS.bat. It now shows conversion,
   tick-health, Luna states and numeric hold details.
7. If you customized settings after uploading, compare your backup settings
   before starting. Do not weaken hard guards to force trades.

Do not run both the old and updated copies at the same time. Do not replace a
newer local database with the older snapshot from the FULL ZIP.
The text below is legacy packaging history only.

V6.0 SCALP INTELLIGENCE code update is already merged into this complete package.
Main changed files: trading_machine.py, settings.py, engine_window.py, machine_supervisor.py, 00_START_ALL_24_7_DEMO.bat, VERSION.txt, README_START_HERE.txt.
Existing trading_machine.db and cached data remain bundled so prior learning/history is retained.
See V6_SCALP_INTELLIGENCE_UPDATE_REPORT.txt for details.
