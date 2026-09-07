V9.1 HIGH-PRECISION LEARNING OPTIMIZER — DEMO ONLY

WHY V9.1
V9.0 solved the critical execution problem and started placing real MT5 DEMO trades.
The first 13 closed V9 trades produced 5 winners / 8 losers, about +0.767R total,
+0.059R average and roughly 1.15 R-profit-factor. That is a useful positive start,
but the win rate is only ~38.5%, so the next job is not “more random trades”; it
is better selection, better learning and better profit protection.

IMPORTANT ABOUT THE 80% REQUEST
V9.1 stores 80% as a precision GOAL for monitoring. No software setting can
truthfully guarantee 40-42 winners out of every 50 market trades. V9.1 tries to
move the distribution in that direction by removing low-quality C-grade entries,
requiring positive Quant expectancy, learning from broker outcomes, and protecting
open profit earlier when a playbook is weak.

WHAT CHANGED
1) CAPACITY, NOT A QUOTA
   - 50 trades/symbol/day safety ceiling.
   - 150 trades/day total safety ceiling for 3 symbols.
   - The bot never forces 50 trades. If only 7 setups qualify, it takes only 7.

2) FIVE-LOSS PORTFOLIO FREEZE
   - Global hard freeze now starts after 5 consecutive closed losses, not 3.
   - Freeze duration = 15 minutes.
   - Before five losses, loss memory de-risks/penalizes weak setups instead of
     unnecessarily stopping the whole machine.

3) HIGH-PRECISION ENTRY MODE
   - Execute score floor: 62 (B grade or better; old floor was 55/C-grade).
   - Expected-R hard floor: +0.10R.
   - Probability edge over break-even hard floor: 0.00.
   - Severe negative / below-break-even Quant trades do not enter merely because
     the Micro Hunter fired.

4) REAL BROKER-OUTCOME MEMORY IN THE ENTRY SCORE
   V9.1 uses existing symbol + playbook + side broker-DEMO memory:
   - observations
   - win rate (Bayesian-smoothed so tiny samples do not dominate)
   - mean R
   - EWMA recent R
   - MFE / MAE
   It also uses strategy/regime/side consecutive-loss memory.

   Strong cells get a SMALL bounded score bonus.
   Weak cells get a larger score penalty.
   A deeply bad cell can be held only after enough real observations.

5) LOSS-RECOVERY MODE — NO MARTINGALE
   If a strategy recently lost but a genuinely new independent setup is very
   strong (high Micro score, positive ER, positive P-vs-BE edge), it can still
   execute in recovery mode.

   Recovery risk is CAPPED at 0.55x. It NEVER doubles the lot after a loss.
   V9_MARTINGALE_ENABLED=False by design.

   Martingale can make a short sequence look better while increasing tail risk
   and account-ruin probability. It does not create trading edge. V9.1 instead
   tries to recover a loss through a fresh positive-expectancy setup at controlled
   size.

6) DYNAMIC PROFIT PROTECTION
   A+/A setups keep more room for runners.
   B-grade and weak-memory setups move toward break-even / trailing protection
   earlier. The manager only TIGHTENS the stop; it cannot widen original risk or
   add exposure.

7) LUNA REMAINS LOW-COST ADVISORY
   Luna remains a second brain for top candidates. A Luna HOLD adjusts risk/score;
   it is not a universal kill switch. Existing paid call caps remain.

UNCHANGED HARD SAFETY
- DEMO-only account lock remains.
- Daily loss / drawdown limits remain.
- Maximum open-position and one-position-per-symbol controls remain.
- Spread/cost, fresh-price, sizing and MT5 broker validity remain.
- No martingale / no loss doubling.

INSTALL
1. 09_STOP_ALL_WINDOWS.bat once.
2. 15_BACKUP_LEARNING.bat.
3. Copy all files from this update's TM_V6_SCALP_INTELLIGENCE_DEMO into the
   existing same folder and choose Replace.
4. KEEP .env, trading_machine.db, data, reports and backups.
5. Run 10_OFFLINE_SELF_TEST.bat. It must end with OFFLINE SELF TEST PASSED.
6. Run 43_V9_1_LEARNING_OPTIMIZER_STATUS.bat.
7. Run 00_RUN_ME_DEMO.bat.

Do not delete the existing database. V9.1 needs the accumulated broker evidence
in that database to improve its decisions.
