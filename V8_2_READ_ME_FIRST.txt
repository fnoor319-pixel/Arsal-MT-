V8.2 QUANT-LUNA ENSEMBLE + DEMO DISAGREEMENT LEARNING

Problem found in V8.1 live evidence:
- Micro Hunter triggered strong setups.
- Local pre-Luna shortlist passed them.
- Luna HOLDed all reviewed candidates at high confidence.
- No broker-DEMO order was attempted.

Architecture correction:
Luna remains a compact second brain, but is no longer a single point of failure.
A normal-risk order still requires Luna CONFIRM. However, when Quant + SuperLearner
strongly approve, Expected-R is positive, micro/local scores are high and the local
deterministic direction check finds no hard contradiction, a Luna HOLD may collect
ONE tiny-risk DEMO disagreement probe.

Safety:
- DEMO ONLY. Existing real/contest hard lock remains.
- Probe risk multiplier: 0.10x.
- Maximum 2 probes/day total and 1/symbol/day.
- SuperLearner must fully APPROVE (borderline candidates cannot probe).
- Positive Expected-R required.
- High local alpha and micro score required.
- Deterministic direction contradiction prevents probe.
- All remaining risk, sizing, broker and portfolio guards still run.
- No martingale and no risk increase.
- Probe does not create a duplicate Luna-veto shadow sample, avoiding double learning.

API efficiency:
- Pretrade paid Luna cap reduced to 8/day.
- Output remains tiny.
- Local pre-Luna shortlist remains mandatory.

Use 36_V8_2_LUNA_ENSEMBLE_STATUS.bat to inspect live behavior.
