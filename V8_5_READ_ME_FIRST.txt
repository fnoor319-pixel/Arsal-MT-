V8.5 EXECUTION-CONVERSION / PLAYBOOK-AWARE MICRO SCALP UPDATE

LATEST DIAGNOSTIC FINDINGS
- USOIL triggers were stopped by spread before SuperLearner; V8.3 spread-wait remains.
- BTC had repeated cases where SuperLearner APPROVED and Local Pre-Luna PASSED, but
  the daily Luna call cap had already been exceeded, so the old engine failed closed
  and never reached order_check/order_send.
- XAU micro triggers were still judged by one generic Gold ADX>=20 /
  confluence>=0.72 / confidence>=0.70 profile, even for reversal/squeeze playbooks.

V8.5 CHANGES
1) PLAYBOOK-AWARE SPARTAN
   - Legacy candidates keep the old strict profile.
   - Native momentum/impulse continuation uses ADX>=16 plus direction/confluence.
   - Native wick/exhaustion reversal is not rejected just because ADX<20.
   - Native squeeze breakout is not rejected just because pre-breakout ADX is low.
   - Session/news/stale tick/spread/ATR/trade-count/direction/risk hard locks remain.

2) ZERO-TOKEN BUDGET FALLBACK
   - Luna paid cap is 4 serious calls/day.
   - Once the call cap or dollar guard is reached, NO more API tokens are spent.
   - A tiny 0.07x broker-DEMO evidence probe is possible only when:
       local score >=82
       micro score >=82
       Expected-R >= +0.30R
       probability >= break-even +0.04
       no deterministic direction contradiction
       all upstream hard gates passed
   - Max 3/day total, 1/symbol/day.
   - This does not martingale or bypass broker/risk locks.

PURPOSE
Collect actual MT5 DEMO execution evidence so the machine can learn from fills,
MFE/MAE, slippage and exits instead of only historical/shadow data.

INSTALL
1. 09_STOP_ALL_WINDOWS.bat
2. 15_BACKUP_LEARNING.bat
3. Copy all V8.5 files into existing TM_V6_SCALP_INTELLIGENCE_DEMO
4. Replace files in destination
5. KEEP .env, trading_machine.db, data, reports, backups
6. Run 10_OFFLINE_SELF_TEST.bat
7. Run 39_V8_5_EXECUTION_CONVERSION_STATUS.bat
8. Run 00_RUN_ME_DEMO.bat
