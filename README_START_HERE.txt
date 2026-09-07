CURRENT BUILD: V11.1.0 CANONICAL DEMO LEARNING SCALPER REPAIR
===================================================
Read V11_1_UPGRADE_README.txt for safe overlay; V11_START_HERE.md for full details.
Start: 00_RUN_ME_DEMO.bat    Current status: 47_V11_CANONICAL_STATUS.bat

If the old bot ran after you uploaded it, use the CODE_UPDATE ZIP in the existing
folder (after stopping/backing it up). It contains NO database or API key and
therefore preserves your newest local learning. Do not overwrite that database
with any older full snapshot.

Luna/API key is OPTIONAL. Network work never blocks the 0.5s execution thread;
an eligible candidate can await an async review for at most 14 seconds.
DEMO hard lock, 2% daily loss limit, 5% peak drawdown and five-loss freeze remain.
This is an experimental DEMO upgrade, not a guaranteed money-making machine.

The text BELOW is retained LEGACY DOCUMENTATION, not the active V11 pipeline.
Its GPT-final-review, old risk descriptions and version names are superseded.
==========================================================================

SPARTAN-SCALPER-PRO V6.5 GPT LEARNING + MT5 DEMO BUILD
====================================================

MAIN START FILE
---------------
Double-click: 00_RUN_ME_DEMO.bat

FIRST RUN
---------
1) MT5 desktop khula ho aur DEMO account login ho.
2) MT5 AutoTrading/Algo Trading enabled ho.
3) 00_RUN_ME_DEMO.bat double-click karein.
4) Agar OpenAI key configured nahi hai to hidden-input prompt khulega. Key source code me save nahi hoti; local .env me hoti hai.
5) Offline tests, GPT connectivity, MT5 DEMO hard-lock, strategy pools aur readiness pass honge.
6) Five supervised engines start honge. Valid setup na ho to HOLD/no order normal behavior hai.

FINAL PRE-TRADE FLOW
--------------------
MT5 data -> data quality/session/news/spread -> indicators -> SMC/market structure -> DOM/order flow/tick delta
-> volume profile + higher timeframes -> deterministic 6/8 confluence -> adaptive gate -> SuperLearner/ML
-> GPT FINAL REVIEWER (CONFIRM or HOLD only, confidence >= 0.70)
-> deterministic risk manager/lot sizing/fresh-price/broker constraints -> mt5.order_check -> mt5.order_send -> DEMO trade.

GPT ROLES
---------
Level 1 / Context Interpreter:
- GPT receives raw values AND Python-derived context (price-vs-EMA200 %, spread/ATR, regime label, RSI state,
  order-flow bias, distance from POC/VAH/VAL, SMC, HTF, news/session status).
- Python creates these deterministic labels; GPT does not invent market facts.

Level 2 / Final Signal Reviewer:
- Candidate BUY -> GPT can CONFIRM or HOLD only.
- Candidate SELL -> GPT can CONFIRM or HOLD only.
- GPT cannot reverse direction.
- API error, invalid structured output or confidence below 0.70 -> HOLD (fail-closed).
- ML/SuperLearner vote is calculated BEFORE GPT, so GPT sees the complete AI evidence.
- GPT never controls lot size, SL/TP arithmetic, margin, daily loss, broker settings or order_send.

Level 3 / Post-Trade Analyst:
- After a DEMO trade closes, GPT receives the entry context, exit/PnL/R result, MFE/MAE, execution facts and learning state.
- It classifies valid win/loss vs setup/execution/data issue and stores a post-mortem.
- It may write BACKTESTABLE RESEARCH HYPOTHESES only.
- No live strategy/risk parameter reads GPT post-trade suggestions automatically.
- Check 20_POST_TRADE_GPT_STATUS.bat or reports\post_trade\latest.json.

Level 4 / GPT Decision Memory (V6.5):
- Same symbol + same closed candle + same direction + materially unchanged market state reuses the saved GPT review; no duplicate API charge.
- Meaningful price/ATR, order-flow, SMC, HTF, ML, regime or news change creates a new fingerprint and allows a fresh GPT review.
- GPT HOLD/VETO creates a VIRTUAL shadow candidate only; no MT5 broker order is sent.
- That rejected setup is followed until hypothetical TP/SL/max-hold. Saved-loss and missed-winner outcomes are stored.
- Veto outcomes update GPT calibration plus SuperLearner/collective/session memory at reduced shadow weight.
- Executed GPT CONFIRM trades update confirm calibration after real DEMO close.
- Future GPT calls receive this historical calibration as advisory evidence; hard thresholds/risk remain Python controlled.
- Check 21_GPT_LEARNING_STATUS.bat for API calls/tokens/cache/veto-learning stats.

SAFETY / RISK
-------------
- Real/contest MT5 accounts: HARD BLOCKED.
- Existing normal target risk: 0.15% equity/trade.
- DEMO trial fallback target risk: 0.05%.
- Hard reference max risk: 0.5%.
- Daily loss hard stop is stricter than the 5% reference (current setting 2%).
- Peak/equity drawdown guard: 5%.
- Spartan max trades/day: 10.
- Gold: London 15:00-23:30 PKT + New York 20:00-03:00 PKT.
- Oil: New York 20:00-03:00 PKT only.
- ADX < 20 -> HOLD. Gold ATR < 0.50 -> HOLD.
- 60% bid share == +20% signed imbalance; 60% ask share == -20%.
- Broker CFD DOM is treated as broker-specific evidence, not global institutional order flow.

NEWS NOTE
---------
A provider-neutral economic-calendar adapter is included at config\news_events.json.
It never invents "no news" when the feed is unavailable. A reliable live calendar provider can write fresh events there.
SPARTAN_NEWS_HARD_GATE is advisory by default so DEMO testing can operate without a paid calendar credential.
For real-money use (not enabled in this package), a reliable live calendar feed and hard-gate validation should be mandatory.

OTHER COMPONENTS PRESERVED/INTEGRATED
--------------------------------------
- Historical/OOS/walk-forward backtesting
- Shadow approval pipeline
- DEMO-only historical-validated trial bridge
- Persistent adaptive loss memory / cooldown
- Collective + session/family memory
- SuperLearner online model / drift metrics
- SMC: FVG, Order Blocks, BOS, CHoCH, liquidity sweep
- Order book + persistent imbalance snapshots
- Broker tick cumulative delta + footprint-style bins when data supports it
- Volume Profile: POC, VAH, VAL, HVN, LVN
- M5/M15/H1/H4 context
- Break-even/trailing stop manager
- Optional Telegram alerts
- GPT structured Responses API / Pydantic parsing
- GPT material-state fingerprint cache + veto-shadow counterfactual learning
- GPT token usage ledger + 200 pre-trade API calls/day safety cap

IMPORTANT
---------
READY means the machine is allowed to place DEMO orders when all gates approve. It does NOT force an immediate trade.
No win-rate, daily-profit or "8 wins 2 losses" guarantee is made. Validate behavior and expectancy on DEMO.
