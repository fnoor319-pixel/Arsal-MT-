V9.2.1 SYMBOL-LOCAL LOSS FREEZE REPAIR — DEMO ONLY

ROOT CAUSE CONFIRMED
The V9.2 global risk_guard read the latest closed demo_positions across ALL symbols and ALL restarts.
If the last 5+ broker trades were losses, it hard-froze every symbol for 15 minutes from the latest loss.
A new losing trade restarted that timer. This is why the machine could take 2-3 trades and then sit idle.

V9.2.1 CHANGE
- Old broker losses stay in contextual/playbook learning.
- Old losses do NOT become V9.2.1 hard-freeze debt.
- A persistent V9.2.1 freeze epoch is created once on the first main-bot run.
- Hard streak freeze is PER SYMBOL after 5 consecutive losing V9 trades since that epoch.
- XAU freeze does not stop BTC or USOIL; BTC freeze does not stop XAU/OIL.
- A profitable/break-even trade resets that symbol's consecutive losing streak.
- Portfolio-wide consecutive losses still reduce risk softly.
- Account-level daily loss and peak drawdown remain hard global stops.
- 100 trades/symbol/day capacity is unchanged.
- V9.2 adaptive learning, DOM/tick-flow, exits, rejected-trade learning and champion/challenger are unchanged.
- No martingale and no forced trades.

INSTALL
1. Run 09_STOP_ALL_WINDOWS.bat once.
2. Run 15_BACKUP_LEARNING.bat.
3. Copy all files in this update's TM_V6_SCALP_INTELLIGENCE_DEMO folder into the existing same folder.
4. Choose Replace files in destination.
5. Do NOT delete .env, trading_machine.db, data, reports or backups.
6. Run 10_OFFLINE_SELF_TEST.bat and confirm OFFLINE SELF TEST PASSED.
7. Run 00_RUN_ME_DEMO.bat. The main bot initializes the V9.2.1 freeze epoch.
8. Run 45_V9_2_1_SYMBOL_FREEZE_STATUS.bat to verify per-symbol state.
