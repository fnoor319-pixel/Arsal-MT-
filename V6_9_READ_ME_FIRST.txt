V6.9 EDGE RECOVERY SCALP LAB - DEMO ONLY

WHY THIS UPDATE EXISTS
The V6.8 laboratory exposed a weak aggregate recent-demo Monte Carlo distribution.
V6.9 does not pretend that old losses can be erased. It changes what the machine
does NEXT: weak recent strategies are quarantined, weak symbols/families are
de-risked, strong shadow evidence receives much more ranking weight, and only
exceptionally strong scalp shadow evidence can fast-track approval.

IMPORTANT
- DEMO ONLY hard lock remains.
- No martingale / no risk-up to chase a daily target.
- Luna/API budget behavior is unchanged. Edge recovery happens BEFORE Luna, so
  weak candidates do not waste paid API calls.
- .env and trading_machine.db are NOT included in this update. Keep your existing
  local files so the machine retains its key and learning history.

INSTALL
1) Run 09_STOP_ALL_WINDOWS.bat
2) Run 15_BACKUP_LEARNING.bat
3) Copy all files from this update's TM_V6_SCALP_INTELLIGENCE_DEMO folder into
   your existing folder and choose Replace files in destination.
4) Do NOT delete .env, trading_machine.db, data, reports or backups.
5) Run 10_OFFLINE_SELF_TEST.bat
6) Run 28_EDGE_RECOVERY_STATUS.bat
7) Start with 00_RUN_ME_DEMO.bat

WHAT CHANGED
- Per-strategy/family/symbol/portfolio recent DEMO edge profiles.
- Symbol-level Monte Carlo instead of judging the whole machine only as one blob.
- Repeatedly losing strategy is quarantined before Luna/order.
- Weak family/symbol/portfolio only de-risks; it does not permanently freeze an
  improving symbol.
- Shadow evidence has materially more influence on candidate ranking.
- Early catastrophic shadow loser rejection.
- Strict scalp fast-track approval after strong shadow + OOS + stability evidence.
- Trial pool widened carefully: more OOS opportunities, with OOS PF >= 1.05.
- 28_EDGE_RECOVERY_STATUS.bat added.
