from __future__ import annotations

import json
import sys
import types
from pathlib import Path

try:
    import MetaTrader5  # noqa: F401
except ImportError:
    fake = types.ModuleType("MetaTrader5")
    fake.TIMEFRAME_M1 = 1
    sys.modules["MetaTrader5"] = fake

import settings


def main() -> int:
    print("=" * 78)
    print("SPARTAN-SCALPER-PRO STATUS")
    print("=" * 78)
    print(f"Enabled: {getattr(settings, 'ENABLE_SPARTAN_PRO', False)}")
    print(f"Session filter: {getattr(settings, 'SPARTAN_SESSION_FILTER_ENABLED', False)}")
    print(f"News hard gate: {getattr(settings, 'SPARTAN_NEWS_HARD_GATE', False)}")
    print(f"LLM reviewer: {getattr(settings, 'SPARTAN_LLM_REVIEW_ENABLED', False)}")
    print(f"GPT model: {getattr(settings, 'GPT_MODEL', '')}")
    print(f"OpenAI key configured: {bool(str(getattr(settings, 'OPENAI_API_KEY', '') or '').strip())}")
    print(f"LLM fail closed: {getattr(settings, 'SPARTAN_LLM_FAIL_CLOSED', True)}")
    print(f"Post-trade GPT analyst: {getattr(settings, 'SPARTAN_POST_TRADE_REVIEW_ENABLED', False)}")
    print(f"Session timezone: {getattr(settings, 'SESSION_TIMEZONE', 'Asia/Karachi')}")
    print(f"Gold London: {getattr(settings, 'LONDON_START', '')}-{getattr(settings, 'LONDON_END', '')}")
    print(f"Gold/Oil NY: {getattr(settings, 'NY_START', '')}-{getattr(settings, 'NY_END', '')}")
    print(f"Target risk/trade: {getattr(settings, 'RISK_PER_TRADE', 0.0) * 100:.3f}% | hard reference max: {getattr(settings, 'RISK_PERCENT', 0.5)}%")
    print(f"Daily loss stop: {getattr(settings, 'MAX_DAILY_LOSS_PCT', 0.0) * 100:.2f}% | peak DD stop: {getattr(settings, 'MAX_DRAWDOWN_PCT', 0.0) * 100:.2f}%")
    print(f"Trailing manager: {getattr(settings, 'SPARTAN_TRAILING_ENABLED', False)}")
    print(f"Min confluence: {getattr(settings, 'SPARTAN_MIN_CONFLUENCE_SCORE', None)}/8")
    print(f"Min confidence: {getattr(settings, 'SPARTAN_MIN_CONFIDENCE', None)}")
    print(f"Max trades/day: {getattr(settings, 'SPARTAN_MAX_TRADES_PER_DAY', None)}")
    print()
    news_file = Path(getattr(settings, "SPARTAN_NEWS_FILE", ""))
    print(f"News file: {news_file}")
    if news_file.exists():
        try:
            payload = json.loads(news_file.read_text(encoding="utf-8"))
            print(f"News provider: {payload.get('provider') if isinstance(payload, dict) else 'unknown'}")
            print(f"News generated_at: {payload.get('generated_at') if isinstance(payload, dict) else 'unknown'}")
            print(f"News events: {len(payload.get('events', [])) if isinstance(payload, dict) else 'unknown'}")
        except Exception as error:
            print(f"News file parse error: {error}")
    print()
    for symbol in getattr(settings, "SYMBOLS", []):
        path = Path(settings.REPORTS_DIR) / f"spartan_pro_last_{symbol}.json"
        if not path.exists():
            print(f"{symbol}: no Spartan snapshot yet")
            continue
        try:
            snap = json.loads(path.read_text(encoding="utf-8"))
            candidate = snap.get("candidate", {})
            print(
                f"{symbol}: approved={snap.get('approved')} action={candidate.get('action')} "
                f"score={candidate.get('confluence_score')}/8 conf={candidate.get('confidence')} "
                f"reason={snap.get('reason')}"
            )
        except Exception as error:
            print(f"{symbol}: snapshot read error: {error}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
