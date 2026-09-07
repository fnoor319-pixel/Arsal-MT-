V10.1 PARALLEL SUPERHUMAN SCALP ROUTER — DEMO ONLY

ROOT CAUSE CONFIRMED BY YOUR STATUS
Recent broker outcomes: closed=20, wins=3, sumR=-13.895R, v10_entries=0.
Those losses were legacy Hunter trades, NOT V10 expert trades.

V10.0 wiring problem:
The 8-expert V10 engine was fallback-only. It only got a chance when the legacy
Micro Hunter had no setup, so active legacy signals could monopolise the live path.

V10.1
- Existing Micro Hunter and V10 expert ensemble run in PARALLEL on each new closed M1 bar.
- Same-side signals compete by score.
- Opposite V10 signal needs a bounded score advantage to override.
- Every comparison is logged to v10_router_events.
- Intrabar hunter triggers remain active between M1 closes.
- 100 trades/symbol/day stays a safety ceiling, not a quota.
- V9.2 adaptive trade management and V9.2.1 symbol-local freeze remain.
- No martingale / loss doubling.
- Luna remains selective meta-reasoning, not a latency gate.

INSTALL
1. 09_STOP_ALL_WINDOWS.bat
2. 15_BACKUP_LEARNING.bat
3. Copy all update files into existing TM_V6_SCALP_INTELLIGENCE_DEMO and Replace.
4. Keep .env, trading_machine.db, data, reports, backups.
5. 10_OFFLINE_SELF_TEST.bat -> OFFLINE SELF TEST PASSED.
6. 00_RUN_ME_DEMO.bat
7. 46_V10_SUPERHUMAN_SCALPER_STATUS.bat after fresh activity.

Proof to look for:
- V10 EXPERT ...
- V10.1 PARALLEL ROUTER - LATEST
- CHOSEN=v10 ...
- eventually broker outcomes tagged V10
