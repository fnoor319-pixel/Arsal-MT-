V8.3 - SPREAD-AWARE TRIGGER PRESERVATION

Live V8.2 evidence showed a genuine USOIL micro trigger reaching the execution
pipeline and being rejected only because spread/ATR was temporarily above the hard
spread threshold.  A scalper should not blindly accept the expensive fill, but it
also should not throw away the whole 2-3 candle setup on one wide-spread tick.

V8.3 behavior:
- High-quality micro trigger + wide spread -> WAIT_SPREAD (zero Luna/API tokens).
- Re-checks live spread during the same setup/bar for up to 35 seconds.
- If spread normalizes, the ORIGINAL trigger resumes through the FULL pipeline.
- If price invalidates, runs >0.18 ATR away, a new bar arrives, or TTL expires,
  setup is abandoned. No chase.
- Existing spread threshold is NOT loosened.
- Spartan, Bayesian, SuperLearner, Edge Recovery, Luna, risk and broker locks remain.
- DEMO only. No martingale. No forced trade.
