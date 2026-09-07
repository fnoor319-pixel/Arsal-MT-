V8.6 QUOTA-DECOUPLED EXECUTION

ROOT CAUSE FOUND FROM THE 18-HOUR AUDIT
- 418 Micro Hunter triggers
- 124 SuperLearner APPROVEs
- 96 otherwise-valid candidates were stopped only because the local Luna shortlist cap was reached
- only 16 local PASSes
- zero-token budget probe policy was NEVER reached
- zero broker order_check/order_send attempts

This was an architecture deadlock: the fallback lane existed AFTER a quota gate
that prevented candidates from ever reaching it.

V8.6 FIX
1. Local Quant qualification is now independent from the paid-Luna shortlist quota.
2. A candidate that fails direction/Expected-R/local quality is still a real HOLD.
3. A candidate that QUALIFIES but the Luna shortlist quota is full remains Quant-qualified.
4. It goes directly to the strict zero-token DEMO evidence policy without an API call.
5. The zero-token lane remains capped at 3/day total and 1/symbol/day, with positive
   Expected-R, strong local/micro scores, probability edge above break-even and
   deterministic direction coherence required.
6. If the zero-token policy rejects it, no trade is forced.
7. If the policy passes, the normal risk/sizing/fresh-price/broker checks remain.
8. Paid Luna still handles the best candidates while quota is available.

No martingale. No real/contest execution. No hard spread/session/news/risk/broker bypass.

INSTALL
1. 09_STOP_ALL_WINDOWS.bat
2. 15_BACKUP_LEARNING.bat
3. Copy all files in this update's TM_V6_SCALP_INTELLIGENCE_DEMO into the existing same folder.
4. Choose Replace files in destination.
5. Keep .env, trading_machine.db, data, reports and backups.
6. Run 10_OFFLINE_SELF_TEST.bat -> OFFLINE SELF TEST PASSED.
7. Run 41_V8_6_QUOTA_DECOUPLED_STATUS.bat.
8. Run 00_RUN_ME_DEMO.bat.

The update contains NO API key and NO trading database.
