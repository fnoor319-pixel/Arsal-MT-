from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

import settings


def _category(reason: str) -> str:
    reason = str(reason or "")
    if reason == "No approved/trial entry":
        return "NO_SIGNAL"
    if "Spread filter" in reason:
        return "SPREAD"
    if reason.startswith("Spartan-Pro veto"):
        return "SPARTAN"
    if reason.startswith("SuperLearner veto"):
        return "SUPER"
    if reason.startswith("V6.9 edge-recovery quarantine"):
        return "EDGE_Q"
    if "Borderline" in reason or "borderline" in reason:
        return "BORDERLINE"
    if reason.startswith("Spartan LLM final veto"):
        return "LUNA_HOLD"
    if reason == "Demo order sent":
        return "ORDER"
    if "risk" in reason.lower():
        return "RISK"
    if "learning" in reason.lower() or "adaptive" in reason.lower():
        return "LEARNING"
    return "OTHER"


def _window(con: sqlite3.Connection, minutes: int) -> None:
    since = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    rows = con.execute(
        """
        SELECT timestamp,symbol,reason
        FROM decision_logs
        WHERE mode='demo' AND timestamp>=?
        ORDER BY id
        """,
        (since,),
    ).fetchall()
    total = Counter()
    by_symbol: dict[str, Counter[str]] = defaultdict(Counter)
    for r in rows:
        c = _category(r["reason"])
        total[c] += 1
        by_symbol[str(r["symbol"] or "UNKNOWN")][c] += 1

    print(f"\nLAST {minutes} MIN | decisions={len(rows)}")
    print("TOTAL:", " | ".join(f"{k}={v}" for k, v in total.most_common()) or "none")
    for symbol in settings.SYMBOLS:
        c = by_symbol.get(symbol, Counter())
        print(f"{symbol}: " + (" | ".join(f"{k}={v}" for k, v in c.most_common()) or "none"))


def _latest_super(con: sqlite3.Connection) -> None:
    print("\nLATEST SUPERLEARNER DECISIONS")
    rows = con.execute(
        """
        SELECT id,timestamp,symbol,strategy_id,regime,decision,reason,
               probability,threshold,bayes_loss_probability,details_json
        FROM superlearner_decisions
        ORDER BY id DESC
        LIMIT 8
        """
    ).fetchall()
    if not rows:
        print("none")
        return
    for r in rows:
        expected_r = None
        rr = None
        try:
            d = json.loads(r["details_json"] or "{}")
            market = d.get("market") or {}
            expected_r = market.get("expected_r")
            rr = market.get("reward_to_risk")
        except Exception:
            pass
        er_txt = "?" if expected_r is None else f"{float(expected_r):+.3f}R"
        rr_txt = "?" if rr is None else f"{float(rr):.2f}"
        reason = str(r["reason"] or "")
        if len(reason) > 95:
            reason = reason[:92] + "..."
        print(
            f"{r['timestamp']} | {r['symbol']} s={r['strategy_id']} {r['decision']} "
            f"P={float(r['probability']):.3f}/{float(r['threshold']):.3f} "
            f"BayesLoss={float(r['bayes_loss_probability']):.3f} ER={er_txt} RR={rr_txt} | {reason}"
        )


def _gpt(con: sqlite3.Connection) -> None:
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    print("\nLUNA / GPT - LAST 24H")
    try:
        rows = con.execute(
            """
            SELECT call_type,COUNT(*) n,
                   COALESCE(SUM(input_tokens),0) tin,
                   COALESCE(SUM(output_tokens),0) tout
            FROM gpt_usage_events
            WHERE timestamp>=?
            GROUP BY call_type
            """,
            (since,),
        ).fetchall()
    except sqlite3.Error:
        rows = []
    if not rows:
        print("calls=0")
    else:
        for r in rows:
            print(f"{r['call_type']}: calls={r['n']} input={r['tin']} output={r['tout']}")


def main() -> int:
    print("=" * 78)
    print("V6.9.3 FRESH PIPELINE DIAGNOSTIC - STATUS ONLY")
    print("=" * 78)
    with sqlite3.connect(settings.DATABASE_PATH, timeout=30) as con:
        con.row_factory = sqlite3.Row
        _window(con, 15)
        _window(con, 60)
        _latest_super(con)
        _gpt(con)

        row = con.execute(
            """
            SELECT id,symbol,strategy_id,status,reward_r,pnl,opened_at,closed_at
            FROM demo_positions
            ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
        print("\nLAST DEMO BROKER TRADE")
        if row:
            print(
                f"id={row['id']} {row['symbol']} strategy={row['strategy_id']} "
                f"status={row['status']} R={row['reward_r']} pnl={row['pnl']} "
                f"opened={row['opened_at']} closed={row['closed_at']}"
            )
        else:
            print("none")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
