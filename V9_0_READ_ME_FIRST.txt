V9.0 EXECUTION-FIRST SCALPER — DEMO ONLY

WHY V9 EXISTS
The overnight audit showed that the machine was not suffering from a lack of opportunities:
- 601 WATCH states
- 418 Micro Hunter TRIGGERs
- 124 SuperLearner APPROVEs
- 0 broker order_check/order_send attempts
- 0 new broker-DEMO positions

The architecture had become over-filtered. Good live alpha had to obtain sequential permission from too many research/reviewer layers. V9 changes the live execution philosophy instead of adding another veto.

V9 CORE PRINCIPLE
Alpha decides WHAT to trade.
Risk decides HOW MUCH.
The broker decides WHETHER the order is executable.
Research/Spartan/SuperLearner/Luna learn from the result.

LIVE V9 FLOW
Micro Hunter TRIGGER
 -> reward/cost-aware spread hard gate
 -> V9 native DEMO carrier
 -> Spartan snapshot (ADVISORY)
 -> adaptive learning memory (ADVISORY)
 -> SuperLearner probability/Expected-R (ADVISORY except severe negative Quant edge)
 -> Edge Recovery (ADVISORY risk reduction)
 -> Luna on only the best setups (ADVISORY second brain; HOLD reduces size, not a kill switch)
 -> ensemble score / risk grade
 -> fresh-price anti-chase hard lock
 -> account/portfolio risk and position caps
 -> broker-valid sizing
 -> MT5 order_check/order_send
 -> broker-DEMO position
 -> MFE/MAE/final-R learning after close

HARD BLOCKS THAT REMAIN
- DEMO account lock
- global daily-loss/drawdown guard
- maximum open positions / per-symbol position limit / cooldown
- transaction-cost spread gate
- authoritative news lock when available
- severe negative Quant expectancy (both Expected-R and probability edge materially negative)
- V9 daily/per-symbol trade caps
- fresh-price anti-chase drift
- invalid/minimum-lot risk ceiling
- MT5 order_check/order_send rejection

WHAT NO LONGER KILLS A GOOD MICRO TRADE BY ITSELF
- Spartan ADX/confluence/session disagreement
- SuperLearner binary REJECT when the overall evidence remains usable
- stale strategy-family Edge Recovery quarantine
- Luna HOLD
- Luna API quota/budget exhaustion
These signals reduce the ensemble score and/or risk instead.

RISK
V9 base target risk: 0.10% equity per trade before ensemble/risk multipliers.
Grades:
- A+ -> up to 1.00x base risk
- A  -> about 0.75x
- B  -> about 0.50x
- C  -> about 0.35x
Disagreement from Spartan/SuperLearner/adaptive/Edge/Luna can reduce this further.
Minimum risk multiplier remains bounded and broker minimum-lot risk still cannot exceed the hard DEMO ceiling.

FREQUENCY CAPS
- Max 15 V9 broker-DEMO trades/day total
- Max 6 per symbol/day
- Existing max-open-position and cooldown limits remain

LUNA / API
- Luna is advisory, not the supreme gatekeeper.
- Only higher-quality V9 candidates request Luna.
- Max 4 paid pre-trade calls/day.
- One compact packet in, tiny structured response out.
- If Luna is unavailable or quota is exhausted, V9 can still trade on local ensemble evidence.
- Luna never chooses lot/SL/TP.

SPREAD
Legacy hard ceiling is 0.18 ATR. V9 can accept a slightly wider spread only if the intended take-profit distance can absorb it. The absolute spread ceiling is 0.28 ATR. This is a reward/cost rule, not a blanket spread loosening.

EVIDENCE MILESTONE
Do not judge V9 from whether a single trade wins. The engineering milestone is the first 100 legitimate broker-DEMO entries with:
- setup/playbook
- each model's pre-trade evidence
- requested/fill price and latency
- MFE / MAE
- final R / PnL
Then the system can measure which reviewer actually adds edge and adjust future architecture from broker evidence instead of theoretical vetoes.

INSTALL
1. Run 09_STOP_ALL_WINDOWS.bat once.
2. Run 15_BACKUP_LEARNING.bat.
3. Open the V9 ZIP and enter TM_V6_SCALP_INTELLIGENCE_DEMO.
4. Copy ALL files from that folder into your existing TM_V6_SCALP_INTELLIGENCE_DEMO folder.
5. Choose Replace the files in the destination.
6. KEEP your existing .env, trading_machine.db, data, reports and backups.
7. Run 10_OFFLINE_SELF_TEST.bat. It MUST end with OFFLINE SELF TEST PASSED.
8. Run 42_V9_EXECUTION_FIRST_STATUS.bat.
9. Run 00_RUN_ME_DEMO.bat.

This update contains NO API key and NO trading database.
V9 is deliberately DEMO-only. Profit is the objective of the research, not a guaranteed outcome.
