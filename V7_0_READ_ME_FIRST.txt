TRADING MACHINE V7.0 MICRO-SCALP HUNTER + LUNA
DEMO ONLY - REAL/CONTEST MT5 ACCOUNTS REMAIN HARD-BLOCKED

PURPOSE
-------
V7 separates the machine into two cooperating brains:

1) RESEARCH BRAIN (slow/deep)
   - strategy factory
   - historical/OOS/walk-forward validation
   - live shadow laboratory
   - champion/challenger ranking
   - Monte Carlo / edge recovery

2) MICRO-SCALP EXECUTION BRAIN (fast/selective)
   - hunts 2-5 candle scalp structures
   - WATCH -> ARM -> TRIGGER lifecycle
   - when ARMED, watches live ticks inside the next minute
   - uses specialist playbooks instead of waiting only for EMA/RSI row signals
   - can execute ONLY through an already qualified scalp carrier strategy
   - all existing hard safety, learning, SuperLearner, Edge Recovery, Luna and broker-risk gates remain downstream

SPECIALIST MICRO PLAYBOOKS
--------------------------
- momentum_burst
- impulse_pullback_resume
- squeeze_breakout
- wick_rejection
- exhaustion_snapback

LUNA / API COST DESIGN
----------------------
Luna remains the final second brain, not the signal generator and not the risk engine.
Python first does market search + mathematical filtering locally.
Only a serious candidate that reaches the final review layer can spend an API call.

One compact information-rich packet IN:
- symbol / side / strategy / regime
- micro playbook + phase + score
- 2-5 candle body/range/volume/momentum context
- confluence / ADX / RSI / ATR / spread
- structure / flow / HTF / session / news state when available
- SuperLearner probability / threshold / expected-R / R:R / Bayes loss
- recent playbook broker-DEMO memory incl. MFE/MAE
- portfolio and capital-growth state

One tiny structured response OUT:
- CONFIRM or HOLD
- confidence
- short reason code
- short reason

Cost controls retained/strengthened:
- local bot budget guard: US$4
- pre-trade paid Luna calls cap: 40/day
- post-trade calls cap: 4/day
- no chat history
- compact packet
- short output
- fingerprint cache
- no Luna polling on raw ticks/candles

V7 MICRO-SCALP LEARNING
-----------------------
For V7 broker-DEMO trades, the playbook brain learns from:
- final reward-R
- MFE (maximum favorable excursion)
- MAE (maximum adverse excursion)
- win/loss rate
- reward EWMA

Rejected research shadow trades do NOT train this broker-execution playbook memory.
The playbook memory can REDUCE risk when weak; it never increases risk above 1.0x.

V7 ENTRY FREQUENCY CALIBRATION
------------------------------
The trigger thresholds were calibrated against the bot's cached M1 bars to create a
meaningful opportunity stream BEFORE downstream safety/quality gates rather than
thousands of noisy triggers.
See V7_0_OFFLINE_CALIBRATION.txt.

IMPORTANT: these are candidate-score frequencies, not expected trades or profit forecasts.

INSTALL / UPGRADE FROM YOUR CURRENT V6.9.4 FOLDER
--------------------------------------------------
1. Run 09_STOP_ALL_WINDOWS.bat
2. Run 15_BACKUP_LEARNING.bat
3. Copy ALL files/folders from this update's TM_V6_SCALP_INTELLIGENCE_DEMO folder
   into your existing TM_V6_SCALP_INTELLIGENCE_DEMO folder.
4. Choose: Replace the files in the destination.
5. DO NOT delete/replace your existing:
   - .env
   - trading_machine.db
   - data folder
   - reports folder
   - backups folder
6. Run 10_OFFLINE_SELF_TEST.bat
7. You must see: OFFLINE SELF TEST PASSED
8. Run 32_V7_MICRO_HUNTER_STATUS.bat
9. Then run 00_RUN_ME_DEMO.bat

WHAT TO WATCH IN TM5
--------------------
New lines can include:
  V7 MICRO TRIGGER
  SPARTAN VETO
  SUPERLEARNER VETO
  BORDERLINE -> LUNA
  LUNA CONFIRM / LUNA HOLD
  DEMO ORDER

V7 does NOT force trades merely to create activity. The purpose is to search much more
like a scalper while keeping negative-expectancy, poor-spread and unsafe conditions out.

VALIDATION
----------
The complete offline regression/self-test passed after V7 integration.
See SELF_TEST_RESULTS_V7_0.txt.
