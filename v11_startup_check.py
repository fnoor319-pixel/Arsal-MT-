"""Local readiness checks: no broker connection and no paid API request."""
from __future__ import annotations

import settings


def main() -> int:
    if not bool(getattr(settings, "V11_ENABLED", False)):
        import subprocess
        import sys
        return subprocess.call([sys.executable, "gpt_connection_test.py", "--startup-local"])
    failures = []
    for name in ("DEMO_ONLY_HARD_LOCK", "ENABLE_DEMO_ORDER_EXECUTION", "PORTFOLIO_HARD_FREEZE_ENABLED"):
        if not bool(getattr(settings, name, False)):
            failures.append(name)
    if not (0 < settings.MAX_DAILY_LOSS_PCT <= .02 and 0 < settings.MAX_DRAWDOWN_PCT <= .05):
        failures.append("daily/peak loss limits must not exceed supplied 2%/5%")
    if not (0 < settings.V11_PROBE_HARD_RISK_PCT <= settings.V9_MAX_MIN_LOT_RISK_PCT):
        failures.append("probe risk ceiling")
    if failures:
        print("V11 START BLOCKED: " + ", ".join(failures))
        return 2
    print("V11 local configuration OK. DEMO-only; existing learning retained.")
    print("Luna: optional async pre-trade reviewer; no API call is needed for startup.")
    print("A completed CONFIRM/HOLD affects that candidate; unavailable/quota/error falls back to local checks.")
    if not str(getattr(settings, "OPENAI_API_KEY", "")).strip():
        print("No API key configured: local learning/execution remain available. 17_CONFIGURE_GPT.bat is optional.")
    print("Cold-start probes: capped evidence collection, not a profit or win-rate promise.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
