# V11.1.0 — Canonical DEMO Learning Scalper Repair

Tumhara uploaded bot update kiya gaya hai. Yeh **experimental DEMO build** hai,
guaranteed money-making machine nahi. Profit, 80% wins, 15–20% daily growth ya
sub-second broker fills ka koi verified claim nahi hai.

## Yeh kaunsa ZIP hai?

Yeh **CODE_UPDATE** hai, kyun ke tumhara bot upload ke baad bhi chalta raha.
Ismein code, tests aur instructions hain; database, market data, logs aur `.env`
nahi. Is tarah tumhari latest local learning replace nahi hoti. Kisi purane full
snapshot ka database apne newer running database par copy mat karna.

## Windows par chalana

1. MT5 positions aur pending orders check karo. Upgrade ke waqt flat account
   simplest hai. `09_STOP_ALL_WINDOWS.bat` se old supervisor/workers stop karo;
   purane folder ka backup rakho. Stopping software does not close positions.
2. CODE_UPDATE ko existing bot folder mein extract karo aur code replacement
   allow karo. Old aur updated copies ek saath mat chalao.
3. Upload ke baad custom settings badli hain to backed-up `settings.py` se
   compare karo. New code retains the uploaded hard limits, not unknown later edits.
4. MT5 desktop mein intended **DEMO** account select karo; Algo Trading aur
   Python/API trading permissions enable hon. Symbols `XAUUSDm`, `USOILm`,
   `BTCUSDm` broker par available hon; M1 history downloaded ho.
5. `00_RUN_ME_DEMO.bat` run karo. It checks dependencies, runs offline tests,
   checks the DEMO connection, and opens the existing five supervised engines.
6. `47_V11_CANONICAL_STATUS.bat` se outcomes, conversion funnel, tick-health,
   Luna state, exact hold metrics, heartbeat aur unresolved orders dekho.

API key optional hai. Package mein tumhara secret `.env` intentionally
include nahi kiya. Apni original local `.env` use karo, ya optional
`17_CONFIGURE_GPT.bat`. Key/config/quota/API unavailable ho to complete local
stack continue karta hai; configured Luna review async hota hai aur us candidate
par CONFIRM/HOLD ka real pre-trade effect hota hai.
No key value was printed or used during development testing.

## Kya materially badla?

| Area | V11 behavior |
| --- | --- |
| Fast loop | 0.5-second **target** poll; MT5 connection reused, closed-bar features cached; M1 refresh every 2 seconds. Broker calls/database contention can take longer. |
| Signal timing | Experts inspect closed M1 bars; armed Hunter triggers inspect current quotes. Polling faster does not turn an M1 pattern into HFT. |
| Observation | Every qualifying expert and fresh Hunter trigger can create paired virtual trials **before** portfolio/freeze/position entry locks. Untriggered patterns do not create pretend trades. |
| Entry economics | Direct estimate from realized **net R**, same policy version/source/playbook/side. A +0.1R winner is +0.1R, not a full-TP win. |
| Scores | Hunter/Expert raw scores are trigger scores, not calibrated win probabilities. Provenance is separated as `hunter:` and `expert:`. Cold-start selection balances evidence coverage, and a blocked first setup no longer starves the next viable candidate. |
| Exits | One frozen entry plan drives both virtual and broker policy: SL/TP, whole-position BE/trail, adverse quote-flow cut and enforced max-hold. New trades do not pass through the overlapping legacy exit layers. |
| Fill accounting | Uses actual filled entry and original cash risk. Later partial entry fills update the risk basis. Entry and exit commissions, fees and swaps are included in realized PnL. |
| Rejections | Durable pre-send intent. Retry another filling mode only on definite invalid-fill rejection. Unknown acknowledgement blocks another entry until broker reconciliation; empty history alone is not proof. |
| Exit learning | Balanced vs runner alternatives need at least 60 prospective paired outcomes across 3 UTC days, later holdout improvement, positive holdout payoff and bounded drawdown. At least 20 new pairs before retesting. |
| SuperLearner | The broker-DEMO net-outcome model plus adaptive/collective/session context now participates before sizing. Only an active strong conflict vetoes; weaker evidence de-risks. It is not treated as a TP-hit probability. |
| Luna | Up to 24 compact reviews/day under the independent $4 bot budget. Network calls run off-thread. A completed CONFIRM/HOLD controls that one candidate; missing key, quota, queue timeout or API error uses an explicit local fallback instead of freezing the engine. |
| Reporting | Full closed-at totals plus candidate→order→shadow funnel, per-symbol tick cursor health, Luna states and latest numeric hold details. Broker versus shadow and legacy versus V11 stay separate. |

This is bounded online adaptation of existing playbooks and two exit variants,
not an AI that autonomously invents a guaranteed profitable strategy. The online
model continues receiving broker outcomes and now has bounded veto/de-risk input;
its probability is not treated as a full-target-hit probability.

## Risk aur learning limits

The supplied 2% daily equity-loss limit, 5% peak-to-equity drawdown limit,
DEMO-only account lock, cooldown and symbol-local five-loss/900-second freeze
remain. A sixth consecutive loss after resuming can start a new freeze; the
history is not reset to hide that loss. Observation continues during entry pauses.

Cold start is explicitly **unproven**, with at most 8 probe entries per symbol
and 24 total per UTC day. Probe target risk is 0.025% before existing defensive
multipliers, and the **actual planned SL risk** is capped at 0.10% equity.
Minimum-lot bridging is allowed only inside that cap and is reported honestly.
If the broker minimum lot is too large, the bot holds and keeps observing.
Do not increase risk just to remove this hold.

After sufficient same-policy evidence, at least five broker outcomes, and
positive estimated net R, the evidence lane can use up to 0.05% target risk
before defensive multipliers. The original 0.35% minimum-lot hard ceiling is
still the outside ceiling for this lane. The original 100/symbol/day and
300/total/day limits are **ceilings, never targets**. Stops/slippage/market gaps
mean realized losses can exceed planned SL risk; an SL is not a guarantee.

Adverse-flow here means a rolling **quote-direction proxy**. It is not real
exchange volume delta or order-book flow; missing DOM is not invented. News
locks honor the existing local news adapter. An unconfigured news file is not
a live news service, and V11 does not secretly add a paid feed. V9-style
session/confluence/model votes are not reintroduced as serial entry vetoes.

## Tick evidence and limitations

Both virtual variants start prospectively on the same executable quote. Their
ordered bid/ask path checks broker-side SL/TP on every returned quote; client-side
stop amendments/time/adverse exits only occur on the actual polling decision.
No M1 OHLC order is invented. A temporarily lagging/empty MT5 history response is
retried without jumping the cursor or deleting valid trials. The cursor advances
only to the last returned tick. Unordered, boundary-incomplete, or more than
120-second unverifiable catch-up paths are marked incomplete and excluded.

Virtual outcomes reserve estimated commission/slippage, using available broker
cost history plus conservative floors. Spread is already in executable prices
and is not deducted twice. These remain **simulated estimates**: broker delay,
rejections, variable fees and queue behavior differ. Broker outcomes supersede
the matching shadow event for economic evidence. Paired variants must not be
added together as if they were independent trades or actual account profit.

The later holdout screen reduces obvious overfitting; it is not statistical
proof of future profitability. Promotion never changes an already-open trade's
frozen policy. Signals blocked longer than the 20-second candidate TTL expire;
the bot waits for a fresh trigger instead of chasing an old one.

## If a status says HOLD

- `bounded_probe_capacity_reached`: daily experiment cap; observation continues.
- `negative_same_policy_net_evidence`: current policy/playbook has poor net
  evidence; keep it virtual until newer evidence supports an entry.
- `Minimum broker lot exceeds ...`: desired risk is below the broker's size
  constraint. Check the logged target/actual risk; don't force a larger lot.
- `loss-streak freeze active`: the original per-symbol protection is working.
- `stale_tick`, `stale_M1_history`: check terminal, symbol feed and UTC clock.
- `tick_history_retry_pending` / `tick_history_catchup_pending`: terminal history
  is temporarily behind; V11.1 retains the trial and retries. Check `tick_health:*`.
- `tick_catchup_limit_exceeded` / `incomplete_or_unordered_tick_history`: the path
  could not be proven safely and is excluded; it never becomes a fabricated win.
- `candidate_veto`: Luna completed a real HOLD for that one setup; another viable
  candidate may still be tried. `*_local_fallback` means no remote veto occurred.
- `unresolved_previous_order` / `awaiting_definitive_broker_reconciliation`:
  inspect positions, pending orders and history in MT5. Don't reset the journal
  or resend manually just because the acknowledgement was missing. Positive
  order/deal evidence can reconcile it; otherwise operator verification is needed.
- `previous_close_awaiting_broker_confirmation`: a close was submitted but its
  result is not fully known. Inspect the position. Server SL/TP remains the
  protection when present; don't bypass the duplicate-close guard.

## Validation and honest handoff

`python -m unittest discover -s tests -p "test_v11*.py" -v`

`python offline_self_test.py`

The package includes offline policy, SQLite, acknowledgement and mocked-MT5
integration tests. They do not connect to a terminal or spend API credits.
See `V11_VALIDATION.md` for the exact completed checks and limitations.

**Not verified here:** Windows console launch on your PC, your broker's live
fill/stop behavior, actual polling latency under all five processes, forward
profitability, win rate, or the availability/price of your configured API model.
Initially run only on DEMO and inspect actual broker net results. The delivered
history is preserved; no historical outcomes were relabeled as V11 evidence.

## Source layout / compatibility

`scalp_policy.py` is pure exit/economics logic. `scalp_learning.py` is an additive
SQLite evidence journal. `scalp_execution.py` classifies broker acknowledgements.
`scalp_runtime.py` owns DEMO observation, sizing, management and reconciliation.
`scalp_advisor.py` owns bounded asynchronous pre-trade review. `trading_machine.py`
dispatches to V11 and retains legacy management for already-open legacy trades.
The other four research engines and historical learning tables remain present.
Older V9.2 shortcut virtual trials are retained as legacy records, not promoted
into V11 evidence. Old documentation/reports are archival; this guide is current.

Technical API contracts were checked against MetaQuotes primary documentation:
[tick history/UTC](https://www.mql5.com/en/docs/python_metatrader5/mt5copyticksrange_py),
[order submission](https://www.mql5.com/en/docs/python_metatrader5/mt5ordersend_py),
[position reads](https://www.mql5.com/en/docs/python_metatrader5/mt5positionsget_py),
[specific order history](https://www.mql5.com/en/docs/python_metatrader5/mt5historyordersget_py).
