SPARTAN SCALP MACHINE V6.6 - SCALP AI OPPORTUNITY HOTFIX
==========================================================
DEMO ONLY. The existing real/contest-account hard lock is not removed.

WHY THIS PATCH EXISTS
---------------------
The supplied V6.5 database was healthy and busy, but the broker-entry pipeline was effectively starved:
- 273,538 demo decisions from 2026-08-30 onward.
- 263,157 were "No approved/trial entry".
- 3,054 were spread vetoes.
- 456 were Spartan hard-gate vetoes.
- 1,086 candidates reached SuperLearner; every one was REJECT.
- gpt_candidate_reviews = 0 and gpt_usage_events = 0 in the trading DB.
- No new DEMO order after position #396 opened 2026-08-27 09:50 UTC.

So the main fault was not "GPT is rejecting trades". Trading candidates were not reaching GPT at all.

WHAT V6.6 CHANGES
-----------------
1) COMPACT LUNA FINAL REVIEW
   - Keeps model gpt-5.6-luna from your existing .env/settings.
   - One compact JSON input per material candidate; one tiny structured response.
   - Response is only: confirm/hold + confidence + reason_code + short note.
   - Pre-trade reasoning defaults to "none" for scalp latency/token control.
   - max_output_tokens is capped and prompt_cache_key is used.
   - store=False remains enabled.
   - Existing material-state fingerprint cache remains active, so repeated same-candle state does not re-call the API.

2) GPT NOW SUPPORTS ALL CONFIGURED SYMBOLS
   - Gold/Oil keep their full Spartan deterministic snapshot.
   - BTC gets a compact deterministic snapshot instead of bypassing the GPT reviewer.
   - Missing news/DOM is explicitly sent as unknown/not-used; GPT is not allowed to invent it.

3) SAFE BORDERLINE SECOND OPINION
   - Hard Python gates are still non-overridable.
   - Only a SOFT SuperLearner rejection close to its probability threshold can reach GPT as borderline_review.
   - Hard DOM/noisy-drift/Bayesian-loss rejects remain hard rejects.
   - Default eligibility: P >= 0.54 and threshold gap <= 0.12.
   - GPT must CONFIRM with >= 0.78 confidence.
   - Rescue trades are DEMO-only, capped at 2/day, and use 0.35x risk multiplier.
   - This is specifically designed to unlock the ML dead-zone without simply lowering every filter.

4) TEMPORARY GPT OUTAGE HANDLING
   - Missing key/configuration remains fail-closed.
   - For a normal candidate that ALREADY passed deterministic + SuperLearner gates, a temporary API/runtime outage may use deterministic fallback at 0.50x DEMO risk.
   - Borderline candidates never use runtime fallback.
   - Circuit breaker opens after repeated API errors and retries after cooldown.

5) SCALP FLOW TELEMETRY
   - Console prints a compact 60-second SCALP FLOW summary.
   - Tracks signal, Spartan veto, Super veto, GPT calls, GPT holds, rescue confirms, spread blocks and orders.
   - reports/scalp_flow_live.json is refreshed for machine-readable status.
   - New 22_SCALP_FLOW_STATUS.bat summarizes last 24h directly from SQLite.

6) CLOSED-BAR NO-SIGNAL DEDUP + LESS CONSOLE/DB NOISE
   - Entry signals are based on the last CLOSED M1 candle. If that exact candle already produced no signal, V6.6 does not recompute/log the identical no-signal decision every 2 seconds.
   - Candidates that actually reach spread/ML/GPT are NOT suppressed and can still retry during the minute.
   - Routine 1500/1500 live-history progress lines are suppressed by default.
   - Large historical/backtest downloads still report progress.

7) GPT POST-TRADE ANALYST IS ALSO COMPACT
   - Sends a distilled closed-trade payload instead of the full bulky context.
   - Keeps post-trade learning advisory-only; no autonomous live parameter mutation.

HOW TO APPLY SAFELY
-------------------
1. STOP all 5 Trading Machine windows.
2. Keep your existing bot folder, trading_machine.db and .env exactly where they are.
3. Make a backup copy of the current source files if you want easy rollback.
4. Copy the files from this hotfix folder into your existing TM_V6_SCALP_INTELLIGENCE_DEMO folder.
5. Choose Replace when Windows asks.
6. DO NOT delete or replace your existing .env or trading_machine.db. This package intentionally does not include them.
7. Run 10_OFFLINE_SELF_TEST.bat. It should end with OFFLINE SELF TEST PASSED.
8. Run 00_RUN_ME_DEMO.bat.
9. After 30-60 minutes, optionally run 22_SCALP_FLOW_STATUS.bat.

NEW CONSOLE LINES TO WATCH
--------------------------
BORDERLINE -> GPT          = soft ML reject was close enough for an AI second opinion
GPT CONFIRM                = normal final reviewer confirmed
GPT RESCUE CONFIRM         = borderline candidate confirmed under tiny-risk rescue rules
GPT HOLD                   = AI held the candidate
GPT TEMP UNAVAILABLE       = normal already-approved candidate used reduced-risk deterministic fallback
SCALP FLOW ...             = 60-second gate telemetry
DEMO ORDER [...]           = broker DEMO order path was reached

IMPORTANT
---------
This patch improves opportunity flow and diagnosis; it does not force a trade and does not promise profitability.
Do not lower the hard market/risk gates blindly. The supplied DB shows recent historical performance was mixed,
so V6.6 opens only a narrow, measured second-opinion path rather than disabling protection.
