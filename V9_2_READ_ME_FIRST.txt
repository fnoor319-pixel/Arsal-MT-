V9.2 ADAPTIVE SCALPING INTELLIGENCE — DEMO ONLY

WHY V9.2
V9/V9.1 finally solved the critical problem: the machine places real MT5 DEMO
orders and learns from broker outcomes. V9.2 preserves that execution-first path.
It does NOT rebuild the old serial-veto architecture.

CAPACITY
- 100 trades per symbol per day safety ceiling.
- 300 trades per day total safety ceiling for XAU/BTC/OIL.
- This is capacity, NOT a quota. The bot never manufactures trades to reach 100.
- Existing 5-consecutive-loss portfolio freeze remains 15 minutes.

WHAT V9.2 ADDS
1) MICROSTRUCTURE INTELLIGENCE
   - Existing MT5 DOM imbalance is combined with broker tick-flow delta and tick speed.
   - If MarketBook/DOM is unavailable, tick flow/proxy evidence is used instead.
   - Flow changes SCORE/RISK; it is not a blind veto.

2) DYNAMIC ATR + MFE/MAE EXIT PLAN
   - Stop/target distances adapt to current price speed and real broker MFE/MAE memory.
   - Slow market banks a closer target; strong impulse gives a runner more room.
   - The plan is bounded; it cannot expand risk without limit.

3) PARTIAL SCALE-OUT
   - Target plan supports 50% first objective, 30% second, 20% runner.
   - If the broker minimum lot (for example 0.01) makes a partial close impossible,
     the machine converts that stage into a tighter profit-lock instead.
   - V9.2 does NOT average down a losing position.

4) LOSS-TRADE MANAGEMENT / STOP-AND-REASSESS
   - If an open trade is materially negative AND DOM+tick flow turn strongly against
     it, V9.2 can cut the trade early instead of waiting blindly for the full SL.
   - A later opposite trade still requires a fresh independent Micro Hunter signal.
   - No automatic hedge, no loss doubling, no martingale.

5) SESSION-SPECIFIC SCALPING
   - London/New-York/overlap and crypto US/Europe receive slightly different score,
     risk and target behavior from Asia/off-hours.
   - Session is an adaptive input, not a reason to force a trade.

6) VOLUME PROFILE / POC
   - Existing broker real/tick-volume POC/VAH/VAL evidence is now used directly by
     the V9 entry overlay. Momentum and reversal playbooks interpret it differently.

7) M1/M5/HTF ALIGNMENT
   - Existing M5/M15/H1 snapshots are converted to a bounded alignment score.
   - Alignment increases confidence; disagreement reduces risk/score rather than
     automatically deleting the trade.

8) EXHAUSTION INTELLIGENCE
   - RSI/exhaustion context is playbook-aware: reversal setups benefit from genuine
     overextension; continuation setups are penalized if they are extremely late.

9) CONTEXTUAL ONLINE LEARNING
   - Real broker outcomes learn separately by:
       symbol + playbook + BUY/SELL + regime + session
   - Stored: win rate, mean R, recent EWMA R, MFE and MAE.
   - Recent evidence changes future score, risk and exit policy within hard bounds.

10) NEW-PLAYBOOK RISK-FREE TRIAL RAMP
   - First 5 real broker trades in a new context cell: max 0.25x allocation.
   - Trades 6-10: max 0.50x.
   - Trades 11-20: max 0.75x.
   - Mature positive context can use normal grade allocation.

11) REJECTED-TRADE LEARNING
   - V9.2 logs rejected candidates as VIRTUAL trials (no MT5 order).
   - It observes whether their virtual TP/SL/TTL outcome would have won or lost.
   - Rejected evidence can nudge the execution floor only +/-1.5 points, preventing
     the optimizer from drifting wildly or overfitting one day.

12) CHAMPION / CHALLENGER
   - Current dynamic exit profile is champion.
   - A small bounded challenger profile is evaluated from actual broker MFE/MAE.
   - Challenger needs >=12 comparable outcomes and a meaningful mean-R advantage
     before automatic promotion.

13) EXISTING COST / LATENCY / CORRELATION / NEWS CONTROLS REMAIN
   - V9 already records execution quality and de-risks poor slippage/latency.
   - V9 already adjusts portfolio risk for correlated positions.
   - Authoritative configured high-impact news lock remains hard.
   - Spread/cost and fresh-price anti-chase remain hard execution locks.

14) LOW-COST LUNA
   - Luna remains a compact second brain for selected strong candidates only.
   - Luna HOLD is advisory; API quota exhaustion does not stop local trading.

WHAT IS DELIBERATELY NOT ADDED
- No martingale or loss-doubling.
- No blind BUY hedge just because a SELL is losing.
- No forced 80% win-rate logic.
- No default stale pending-limit orders. V9.2 keeps cost-aware market execution and
  spread waiting because a missed/stale limit can be worse for this retail MT5 scalp.
- No forced 100 trades/symbol. 100 is only the safety ceiling.

WIN-RATE OBJECTIVE
80% remains a dashboard aspiration, not a promise. V9.2 optimizes win rate together
with mean R, profit factor, MFE/MAE, execution cost and drawdown. A high win rate
with occasional huge losses is not considered a successful scalper.

INSTALL
1. Run 09_STOP_ALL_WINDOWS.bat once.
2. Run 15_BACKUP_LEARNING.bat.
3. Copy ALL files from this update's TM_V6_SCALP_INTELLIGENCE_DEMO folder into
   the existing same folder and choose Replace.
4. KEEP .env, trading_machine.db, data, reports and backups.
5. Run 10_OFFLINE_SELF_TEST.bat -> must finish OFFLINE SELF TEST PASSED.
6. Run 44_V9_2_ADAPTIVE_SCALPER_STATUS.bat.
7. Run 00_RUN_ME_DEMO.bat.

DO NOT DELETE THE DATABASE. V9.2 bootstraps its new context memory from already
closed V9 broker trades so the learning gathered today is not thrown away.
