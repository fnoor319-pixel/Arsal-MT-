from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

import settings


def main(hours: int = 24) -> int:
    since = (datetime.now(timezone.utc) - timedelta(hours=max(1, hours))).isoformat()
    with sqlite3.connect(settings.DATABASE_PATH, timeout=30) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT timestamp,symbol,reason,details_json FROM decision_logs WHERE mode='demo' AND timestamp>=? ORDER BY id",
            (since,),
        ).fetchall()
        total = Counter()
        by_symbol: dict[str, Counter[str]] = defaultdict(Counter)
        for row in rows:
            reason = str(row["reason"] or "")
            if reason == "No approved/trial entry": category = "no_signal"
            elif reason.startswith("Spartan-Pro veto"): category = "spartan_veto"
            elif reason.startswith("SuperLearner veto"): category = "super_veto"
            elif reason.startswith("Spartan LLM final veto"): category = "gpt_veto"
            elif reason == "Demo order sent": category = "order_sent"
            elif "Spread filter" in reason: category = "spread"
            elif "adaptive" in reason.lower() or "learning" in reason.lower(): category = "learning"
            elif "risk" in reason.lower(): category = "risk"
            elif "error" in reason.lower(): category = "error"
            else: category = "other"
            total[category] += 1
            by_symbol[str(row["symbol"] or "UNKNOWN")][category] += 1

        print(f"SCALP FLOW STATUS | last {hours}h | decisions={len(rows)}")
        print("TOTAL:", " | ".join(f"{k}={v}" for k, v in total.most_common()))
        for symbol in settings.SYMBOLS:
            c = by_symbol.get(symbol, Counter())
            print(f"{symbol}: " + " | ".join(f"{k}={v}" for k, v in c.most_common()))

        try:
            gpt = con.execute(
                """
                SELECT call_type, COUNT(*) n, COALESCE(SUM(input_tokens),0) input_tokens,
                       COALESCE(SUM(output_tokens),0) output_tokens
                FROM gpt_usage_events WHERE timestamp>=? GROUP BY call_type ORDER BY call_type
                """,
                (since,),
            ).fetchall()
            if gpt:
                print("GPT:")
                for row in gpt:
                    print(f"  {row['call_type']}: calls={row['n']} in={row['input_tokens']} out={row['output_tokens']}")
            else:
                print("GPT: no recorded trading calls in this window")
        except sqlite3.Error:
            print("GPT: usage table unavailable")

        trade = con.execute(
            "SELECT id,symbol,strategy_id,reward_r,pnl,opened_at,closed_at,status FROM demo_positions ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if trade:
            print(
                "LAST DEMO TRADE: "
                f"id={trade['id']} {trade['symbol']} strategy={trade['strategy_id']} status={trade['status']} "
                f"R={trade['reward_r']} pnl={trade['pnl']} opened={trade['opened_at']}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
