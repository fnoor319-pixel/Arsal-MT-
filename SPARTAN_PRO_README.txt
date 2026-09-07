SPARTAN-SCALPER-PRO INTEGRATION
===============================

This is an additive upgrade over the existing V6/V7 learning pipeline. The old
strategy factory, historical/OOS/walk-forward backtester, live-shadow lab,
SuperLearner, sizing and DEMO-only hard lock are retained.

NEW LIVE PIPELINE FOR XAUUSD / USOIL
-------------------------------------
Existing approved strategy signal
 -> Spartan data-quality/session/news gate
 -> Canonical EMA5/13/200 + RSI7 + ATR14 + ADX14
 -> SMC snapshot (confirmed swings, BOS/CHoCH, FVG, heuristic OB, sweep)
 -> Volume profile (POC/VAH/VAL/HVN/LVN; source is explicitly labelled)
 -> DOM bid/ask shares + signed imbalance + short persistence windows
 -> M5/M15/H1/H4 context
 -> 6/8 directional confluence + confidence >= 0.70
 -> optional OpenAI structured reviewer (veto only)
 -> existing adaptive gate + SuperLearner (ML)
 -> existing risk manager / broker sizing
 -> fresh broker entry + DEMO order_check/order_send
 -> optional break-even/trailing management

IMPORTANT COMPATIBILITY CHOICES
-------------------------------
1. BTCUSDm is not governed by the new XAU/OIL session/news gate, so existing BTC
   research/execution behavior is preserved.
2. Existing strategy SL/TP parameters are not silently replaced because doing
   that live-only would create backtest/live mismatch. SPARTAN_FORCE_FIXED_ATR_EXITS
   exists but defaults False. New dedicated Spartan strategies can be backtested
   with 1.0 ATR SL / 1.5 ATR TP before that switch is enabled.
3. News hard-gating defaults OFF until a real fresh calendar feed is connected.
   No fake news/sentiment data is generated.
4. LLM review defaults OFF. Set OPENAI_API_KEY, OPENAI_MODEL and
   SPARTAN_LLM_REVIEW_ENABLED=True to enable. Python hard gates always remain
   authoritative; LLM may confirm or veto, never size risk or override safety.
5. Broker MT5 DOM is treated as broker-specific liquidity, not the full global
   COMEX/NYMEX order book.

FILES
-----
spartan_pro.py       deterministic features, SMC, VP, sessions/news, hard gate
spartan_llm.py       optional OpenAI structured reviewer
spartan_alerts.py    optional Telegram alerts
config/news_events.json provider-neutral calendar input
reports/spartan_pro_last_<symbol>.json last full snapshot for inspection

SAFETY
------
All broker order paths remain DEMO-only through the existing verify_demo_account()
hard lock. Real/contest accounts stay blocked.

=== GPT ACTIVE SETUP (V6.2) ===
GPT reviewer is ENABLED by default and is fail-closed.
1) Run 01_INSTALL.bat
2) Run 17_CONFIGURE_GPT.bat and paste your OpenAI API key
3) Run 18_TEST_GPT_CONNECTION.bat
4) Run 12_PAPER_TEST_NO_ORDERS.bat
5) Run 00_START_ALL_24_7_DEMO.bat

The key is stored only in local .env and is never hardcoded in source.
Default model: gpt-5.6-luna (override with OPENAI_MODEL).
GPT is the final AI confirm/HOLD gate; Python still owns session/news/risk/SL/TP/broker safety and cannot be overridden.
MT5 order execution remains DEMO-only hard locked.

V6.3 DEMO AUTOTRADE NOTE
------------------------
- ENABLE_DEMO_ORDER_EXECUTION=True and DEMO_ONLY_HARD_LOCK=True.
- GPT final reviewer is mandatory/fail-closed.
- DEMO trial bridge is ON so symbols with no shadow-approved strategy may use historical-validated strategies at DEMO_TRIAL_RISK_PER_TRADE (0.05%) only.
- XAUUSDm still prefers shadow-approved strategies whenever available.
- 19_DEMO_AUTOTRADE_READINESS.bat verifies settings, candidate pools, DEMO account and terminal trade permission without sending an order.
- 00_START_ALL_24_7_DEMO.bat runs this readiness check automatically before launching the five engines.
- No valid setup means HOLD/no order; the bot never forces a trade simply because it started.
