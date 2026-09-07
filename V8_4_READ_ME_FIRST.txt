V8.4 EXECUTION-FIRST DEMO EVIDENCE UPDATE

WHY THIS VERSION EXISTS
Eight hours of development produced many WATCH/ARM/TRIGGER events but no new MT5
broker-DEMO orders. The machine was learning mainly from historical backtests and
shadow trades, not from actual broker execution. That means its execution brain
had too little real evidence to learn from.

CORE ARCHITECTURE CHANGE
1. A live Micro Hunter trigger now uses its OWN persistent DEMO-only native-alpha
   strategy carrier by default. It no longer inherits stale trial/approved strategy
   memory just because a legacy candidate happens to exist.
2. The old two-vote recovery rule does not fit a one-playbook micro state machine.
   A very strong fresh Micro Hunter trigger can recover at reduced risk using its
   live alpha score instead of being permanently blocked for having only one vote.
3. Luna remains the second brain, but a Luna HOLD is not always the end of the
   experiment. If every upstream hard gate has already passed and Quant remains
   above break-even with positive Expected-R, V8.4 can collect a TINY capped DEMO
   evidence probe.
4. The probe remains behind spread, direction/structure, Spartan, adaptive,
   Edge-Recovery, broker and risk hard locks.

DEMO EVIDENCE PROBE
- Risk multiplier: 0.10x
- Maximum 3/day total
- Maximum 1/symbol/day
- Positive Expected-R required
- Probability must remain above break-even by at least 0.03
- Strong local/micro scores required
- Deterministic direction contradiction still blocks
- No martingale
- Real/contest hard lock remains

LUNA COST
- Max 6 paid pre-trade calls/day
- Max 1 post-trade call/day
- Local shortlist and cache run before API
- Short structured output remains

INSTALL
1. Run 09_STOP_ALL_WINDOWS.bat
2. Run 15_BACKUP_LEARNING.bat
3. Copy all files inside TM_V6_SCALP_INTELLIGENCE_DEMO into your existing same folder
4. Choose Replace the files in destination
5. KEEP .env, trading_machine.db, data, reports and backups
6. Run 10_OFFLINE_SELF_TEST.bat -> must show OFFLINE SELF TEST PASSED
7. Run 38_V8_4_EXECUTION_FIRST_STATUS.bat
8. Run 00_RUN_ME_DEMO.bat

This package contains NO API key and NO trading database.
