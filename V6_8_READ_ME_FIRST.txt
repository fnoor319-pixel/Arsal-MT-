TRADING MACHINE V6.8 — CAPITAL GROWTH + LUNA SCALP LAB
DEMO ONLY — CODE UPDATE
=======================================================

PURPOSE
-------
This build keeps the V6.7 scalp laboratory, execution-quality learning, champion/
challenger research, Monte Carlo risk lab, opportunity scoring, Luna value audit,
self-healing supervisor and $5 API budget brain, then adds a capital-growth controller.

The machine's objective is compounding capital through selective scalping and learning.
The 15% daily target and 20% stretch target are tracked as objectives; they are NOT used
to force trades, martingale, average down or increase risk after losses.

V6.8 CAPITAL-GROWTH LOGIC
-------------------------
- Position sizing already uses CURRENT equity, so profitable equity compounds naturally.
- Daily anchor uses the configured SESSION_TIMEZONE (Asia/Karachi by default).
- Below +15%: normal evidence-based risk logic remains in control.
- At +15%: risk is reduced to 0.50x while the machine may continue toward +20%.
- At +20%: new entries pause for the rest of that local day; open positions are still managed.
- If intraday peak gain is >= +8% and >3 percentage points are given back, risk is cut to 0.35x.
- The controller NEVER raises risk to chase the target.

LUNA / $5 API DESIGN
--------------------
- Model: gpt-5.6-luna.
- Current pricing constants in settings: $0.20/M input, $1.20/M output.
- Bot-local spend guard: $4.00, leaving about $1.00 reserve from a $5 funded key.
- Pre-trade API calls: max 80/day.
- Post-trade API calls: max 8/day.
- Reasoning effort: none by default.
- One compact, information-rich JSON packet in.
- One tiny structured answer out: CONFIRM/HOLD + confidence + reason code + short reason.
- No chat history is sent.
- Material-state fingerprint cache prevents repeat paid calls for effectively the same setup.
- Luna cannot choose lot size, SL/TP, bypass hard gates or reverse BUY/SELL.

SAFE INSTALL
------------
1) Run 09_STOP_ALL_WINDOWS.bat
2) Run 15_BACKUP_LEARNING.bat
3) Extract this ZIP.
4) Copy its TM_V6_SCALP_INTELLIGENCE_DEMO files into your EXISTING same folder.
5) Choose Replace files in destination.
6) DO NOT delete or replace your existing:
   .env
   trading_machine.db
   data/
   reports/
   backups/
7) Run 10_OFFLINE_SELF_TEST.bat — must end OFFLINE SELF TEST PASSED.
8) Run 26_MACHINE_HEALTH.bat.
9) Run 27_CAPITAL_GROWTH_STATUS.bat.
10) Start 00_RUN_ME_DEMO.bat.

NEW TOOL
--------
27_CAPITAL_GROWTH_STATUS.bat
Shows today's start equity, current/peak equity, daily growth, 15% target, 20% stretch,
current phase, growth-controller risk multiplier and whether new entries are allowed.

IMPORTANT
---------
Keep V6.8 on DEMO until enough forward trades prove positive net expectancy after spread,
slippage and execution costs. This code tracks the 15/20 growth objective without creating
an unsafe target-chasing loop.
