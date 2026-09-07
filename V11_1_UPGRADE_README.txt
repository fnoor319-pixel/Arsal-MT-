V11.1.0 SAFE CODE UPDATE - DEMO ONLY
===================================

WHAT THIS REPAIRS
-----------------
1. MT5 tick history may lag the current snapshot. V11.0 jumped its cursor forward
   and invalidated shadow trials. V11.1 retains them, advances only through ticks
   actually returned, and retries. Truly unordered/boundary-missing paths or a
   >120-second unverifiable gap are still excluded from learning.
2. V11.0 tried only one selected candidate. If that candidate failed cost, spread,
   stop-distance, sizing, negative-evidence, SuperLearner or Luna checks, another
   viable setup was starved. V11.1 safely falls through candidate-specific holds.
3. P90 slippage is converted to R using each setup's actual stop ATR. The hard
   net-cost-to-target gate remains 35%; the old fixed 0.35-ATR conversion was an
   over-estimate for wider-stop setups.
4. The existing broker-outcome SuperLearner now has real strong-veto/de-risk input.
5. Configured Luna now reviews before an order. Completed CONFIRM/HOLD affects that
   candidate. Calls are asynchronous, compact, capped at 24/day and still stopped
   by the independent $4 bot budget. Missing key/quota/API/timeout falls back to
   all local deterministic checks, so an external service cannot freeze the bot.
6. Status 47 now shows conversion funnel, tick health, Luna states, and exact cost,
   spread, drift and stop-distance metrics behind recent HOLDs.

SAFE INSTALL OVER YOUR RUNNING COPY
-----------------------------------
1. In MT5 inspect open positions and pending orders.
2. Run 09_STOP_ALL_WINDOWS.bat. Confirm old engine windows stop. This does NOT
   close an MT5 position; broker SL/TP stays authoritative.
3. Copy the whole current inner bot folder to a backup location.
4. Extract this ZIP into that same inner folder and approve file replacement.
5. Never delete or replace your existing trading_machine.db, .env, data or logs.
   They are deliberately absent from this ZIP.
6. Start 00_RUN_ME_DEMO.bat. It remains hard-locked to an MT5 DEMO account.
7. After 10-15 minutes run 47_V11_CANONICAL_STATUS.bat. Temporary
   tick_history_catchup_pending is normal; persistent >120-second resets are not.

ROLLBACK
--------
Stop the bot and restore the backed-up code files. The only database change is an
additive v11_luna_reviews audit table; old code can ignore it. Never restore an old
database over newer learning merely to roll back code.

VALIDATION
----------
Run: python -m unittest tests.test_v11 -v
All tests are offline with stubbed MT5/API. They do not place orders or spend API.

No profit, win-rate or latency is guaranteed. Keep this build on DEMO until actual
broker NET results (fees/slippage included) show enough forward evidence.
