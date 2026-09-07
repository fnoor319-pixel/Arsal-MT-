V8.0 INSTITUTIONAL MICRO-SCALP ENGINE + LUNA
DEMO ONLY - CODE UPDATE FOR THE EXISTING V7 MACHINE

WHY THIS VERSION EXISTS
-----------------------
V7 successfully added a micro hunter, but a strong live setup still depended on a
matching approved/trial scalp strategy before it could reach the rest of the live
pipeline. With approved scalp=0 and only a few trial scalp carriers, that could
leave the executor at NO_SIGNAL for hours even while the research factory was busy.

V8 changes the live architecture from strategy-first to opportunity-first:

  live 2-5 candle/tick alpha
      -> adaptive WATCH / ARM / TRIGGER
      -> qualified carrier when available
         OR V8 DEMO-only native alpha carrier
      -> spread/session/Spartan hard gates
      -> adaptive/Bayesian learning
      -> SuperLearner ensemble
      -> Edge Recovery
      -> cheap LOCAL pre-Luna shortlist
      -> ONE compact Luna review
      -> deterministic risk/broker checks
      -> MT5 DEMO order
      -> MFE/MAE + playbook learning

CORE CHANGES
------------
1. ADAPTIVE OPPORTUNITY THRESHOLDS
   V7 fixed 84/88/92-style thresholds are no longer the primary live thresholds.
   V8 records the symbol's real playbook score distribution and adapts WATCH/ARM/
   TRIGGER thresholds with hard floors. This makes a clean quiet market tradable
   without manufacturing signals in pure noise.

2. NATIVE DEMO ALPHA CARRIER
   A high-quality V8 micro setup can reach the complete downstream intelligence
   stack even if the library currently has zero matching approved scalp strategies.
   This carrier is marked v8_native_demo, is DEMO-only and does NOT pretend to be
   historically approved.

3. INSTITUTIONAL ENSEMBLE
   The micro-alpha score becomes one bounded component of SuperLearner alongside
   the online model, adaptive/Bayesian evidence, collective/session memory and
   learned playbook outcomes. One model no longer owns the entire decision.

4. POSITIVE-EXPECTANCY SCALP CALIBRATION
   For scalp setups, the probability threshold is tied more closely to break-even
   probability and expected-R. Negative expected-R is still rejected.

5. EXPLORATION / EXPLOITATION
   Old family-level losses may not permanently suppress a genuinely new exceptional
   V8 playbook. Only very high alpha scores can enter this DEMO exploration lane,
   and downstream risk is heavily reduced.

6. API WASTE FIX
   00_RUN_ME_DEMO no longer sends a paid OpenAI health request every time the bot is
   restarted. Startup performs a LOCAL 0-token key/model/import readiness check.
   18_TEST_GPT_CONNECTION.bat remains available for an intentional real API test.

7. LOCAL PRE-LUNA SHORTLIST
   Luna is still the final second brain, but Python first ranks the serious candidate
   using micro score + expected-R + probability edge + deterministic confluence.
   Low-value candidates are held locally without spending API tokens.

8. LUNA COST CONTROLS
   - model remains gpt-5.6-luna
   - one compact input packet
   - one tiny structured response
   - reasoning effort remains none
   - pre-trade API cap: 12/day
   - post-trade API cap: 2/day
   - output cap: 56 tokens
   - local bot spend guard remains active

9. NATIVE ALPHA RISK
   Native V8 probes are capped at 0.30x risk and at most 6/day total, 3/symbol/day.
   No martingale or target-chasing risk increase is added.

OFFLINE CALIBRATION
-------------------
Using the machine's cached recent M1 histories, before downstream hard gates:
  XAUUSDm: WATCH 243 | ARM 186 | immediate TRIGGER 189 / ~24h
  USOILm:  WATCH 188 | ARM 142 | immediate TRIGGER 140 / ~24h
  BTCUSDm: WATCH 263 | ARM 168 | immediate TRIGGER 202 / ~24h

These are opportunity detections, NOT promised broker trades. Spread, Spartan,
Bayesian learning, SuperLearner, Edge Recovery, local Luna shortlist, Luna itself
and broker/risk checks still decide whether a DEMO order is allowed.

INSTALL
-------
1. Run 09_STOP_ALL_WINDOWS.bat
2. Run 15_BACKUP_LEARNING.bat
3. Copy every file from this update's TM_V6_SCALP_INTELLIGENCE_DEMO folder into
   your existing TM_V6_SCALP_INTELLIGENCE_DEMO folder.
4. Choose "Replace the files in the destination".
5. DO NOT delete/replace your existing .env, trading_machine.db, data, reports or backups.
6. Run 10_OFFLINE_SELF_TEST.bat. It must end with OFFLINE SELF TEST PASSED.
7. Run 33_V8_INSTITUTIONAL_STATUS.bat.
8. Start with 00_RUN_ME_DEMO.bat.

WHAT TO MONITOR
---------------
33_V8_INSTITUTIONAL_STATUS.bat shows:
- adaptive WATCH/ARM/TRIGGER thresholds per symbol
- recent micro setups
- local pre-Luna shortlist decisions
- V8 native-alpha DEMO trades today
- actual Luna trading calls/tokens

IMPORTANT
---------
This version is deliberately DEMO-only. Its purpose is to increase high-quality
opportunity throughput and measure whether the new live alpha architecture creates
positive forward expectancy. It cannot guarantee a fixed daily return.
