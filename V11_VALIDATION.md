# V11.1.0 validation record

Validation date: 2026-09-04 UTC  
Scope: offline source, policy, SQLite, acknowledgement and mocked-MT5 checks.

## Completed checks

- `python -m compileall -q .` — passed.
- `python -m unittest discover -s tests -p "test_v11*.py" -v` — 73/73 passed.
- `python offline_self_test.py` — passed (`OFFLINE SELF TEST PASSED`).
- A separate asynchronous Luna integration check used a stubbed reviewer and a
  temporary SQLite database. It reached `pending -> complete -> confirm`, wrote
  one usage record, and made no network request.

The V11 suite covers, among other cases:

- hard rejection of real/contest accounts;
- stale, future, invalid and out-of-order quotes;
- temporary tick-history lag, bounded catch-up and incomplete-path exclusion;
- bid/ask-aware BUY and SELL exits, fees, stop safety and time exits;
- blocked-candidate fall-through to a second viable setup;
- broker-net SuperLearner veto/de-risk behavior;
- Luna pending, CONFIRM and HOLD execution effects;
- durable pre-send intent, partial fill recovery, unknown acknowledgement and
  duplicate-order prevention;
- probe/risk caps, news locks, position locks and five-loss freeze behavior;
- policy promotion only from prospective paired paths plus later holdout.

## Deliberate compatibility choice

The runtime/build version is `11.1.0`, while the unchanged economic/exit policy
identifier remains `11.0.0`. This preserves comparable V11 evidence because the
entry/exit payoff definition was not silently relabeled. The repair changes data
catch-up, candidate routing, cost conversion and pre-trade evidence use; it does
not rewrite historical rows.

## Not verified in this environment

- Windows batch launching and OneDrive behavior on the user's PC;
- connection to the user's MetaTrader 5 terminal or broker;
- actual broker fills, variable spread, fees, slippage or stop execution;
- live OpenAI response latency/availability or API account configuration;
- forward profitability, win rate, trade frequency or daily return.

Those items require forward observation on the user's existing MT5 **DEMO**
account. Offline tests neither place orders nor spend API credits. No profit,
win-rate or latency guarantee is made.

## First forward check

After safely overlaying the code update, run `00_RUN_ME_DEMO.bat`. After 10–15
minutes run `47_V11_CANONICAL_STATUS.bat`; after a 6–24 hour DEMO window run
`40_V8_5_OVERNIGHT_EXECUTION_AUDIT.bat`. Review broker-net outcomes separately
from paired shadow estimates.
