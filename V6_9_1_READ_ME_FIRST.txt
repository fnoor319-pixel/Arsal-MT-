V6.9.1 SHADOW FILL INTEGRITY HOTFIX - DEMO ONLY

WHY
A shadow SL/TP is virtual research, not a broker order. The 10-second polling loop
could notice a crossed SL/TP after price had moved much farther, especially after a
restart/gap, and record the current quote as the virtual fill. This can manufacture
results such as -8.13R on a normal 1R stop and distort shadow rankings.

FIX
- Virtual shadow STOP now closes at the configured stop level = -1.00R.
- Virtual shadow TAKE closes at the configured target level = planned R:R.
- Time exits still use the current executable quote.
- One-time startup repair normalizes old shadow SL/TP overshoot records.
- candidate_live_scores and strategy_scores are rebuilt from corrected shadow ledger.
- Real DEMO broker trades, PnL, tickets, .env/API key and risk logic are NOT rewritten.

INSTALL
1) Run 09_STOP_ALL_WINDOWS.bat
2) Run 15_BACKUP_LEARNING.bat
3) Copy this folder's files into the existing TM_V6_SCALP_INTELLIGENCE_DEMO and Replace.
4) Keep .env, trading_machine.db, data, reports and backups.
5) Run 10_OFFLINE_SELF_TEST.bat
6) Run 29_SHADOW_FILL_INTEGRITY_STATUS.bat
7) Start 00_RUN_ME_DEMO.bat
