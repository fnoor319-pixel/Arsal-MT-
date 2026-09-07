from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path


SCALP_FAMILIES = (
    "super_scalp", "scalp", "micro_momentum", "pullback_scalp",
    "breakout_scalp", "mean_revert_scalp",
)


def table_exists(con: sqlite3.Connection, name: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def reason_bucket(reason: str) -> str:
    text = (reason or "").lower()
    if "no approved/trial" in text or "no ensemble" in text:
        return "NO_SIGNAL"
    if "spread filter" in text or "spread" in text and "veto" not in text:
        return "SPREAD"
    if "superlearner veto" in text:
        return "SUPERLEARNER"
    if "spartan-pro veto" in text or "spartan veto" in text:
        return "SPARTAN"
    if "llm final veto" in text or "gpt" in text and "veto" in text:
        return "GPT"
    if "daily loss" in text or "drawdown" in text or "risk" in text:
        return "RISK"
    if "maximum" in text and "position" in text:
        return "POSITION_LIMIT"
    return "OTHER"


def pct(v: float) -> str:
    return f"{100.0*v:.1f}%"


def main(hours: int = 24) -> None:
    db = Path(__file__).resolve().with_name("trading_machine.db")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=20)
    con.row_factory = sqlite3.Row
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    since7 = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()

    print("=" * 78)
    print("SPARTAN-SCALPER-PRO V6.6 | SCALP HEALTH / BOTTLENECK REPORT")
    print(f"Database: {db}")
    print(f"Window: last {hours}h | generated {datetime.now(timezone.utc).isoformat()}")
    print("=" * 78)

    statuses = con.execute(
        "SELECT status, COUNT(*) n FROM strategies GROUP BY status ORDER BY n DESC"
    ).fetchall()
    print("\nSTRATEGY PIPELINE")
    print(" | ".join(f"{r['status']}={r['n']}" for r in statuses))
    pending = con.execute(
        """
        SELECT COUNT(*) n FROM strategies s
        WHERE s.status IN ('generated','needs_revalidation','backtest_error')
          AND (s.status='needs_revalidation' OR NOT EXISTS(
              SELECT 1 FROM backtests b WHERE b.strategy_id=s.id))
        """
    ).fetchone()["n"]
    print(f"pending_backtest={pending}/1200")

    print("\nSCALP INVENTORY")
    marks = ",".join("?" for _ in SCALP_FAMILIES)
    symbols = [str(r[0]) for r in con.execute("SELECT DISTINCT symbol FROM strategies ORDER BY symbol").fetchall()]
    for symbol in symbols:
        rows = con.execute(
            f"""
            SELECT status, COUNT(*) n FROM strategies
            WHERE symbol=? AND family IN ({marks}) GROUP BY status
            """,
            (symbol, *SCALP_FAMILIES),
        ).fetchall()
        m = {str(r["status"]): int(r["n"]) for r in rows}
        print(
            f"{symbol:9} generated={m.get('generated',0):4} "
            f"historical={m.get('historical_validated',0):3} "
            f"approved={m.get('shadow_approved',0):3} "
            f"shadow_rejected={m.get('shadow_rejected',0):3} "
            f"backtest_rejected={m.get('backtest_rejected',0):4}"
        )

    print(f"\nDECISION FUNNEL - LAST {hours}H")
    rows = con.execute(
        "SELECT reason FROM decision_logs WHERE mode='demo' AND timestamp>=?", (since,)
    ).fetchall()
    buckets = Counter(reason_bucket(str(r["reason"])) for r in rows)
    total = sum(buckets.values())
    if not total:
        print("No DEMO decisions in this window.")
    else:
        for name, n in buckets.most_common():
            print(f"{name:18} {n:7}  {pct(n/total)}")
        print(f"TOTAL              {total:7}")

    if table_exists(con, "superlearner_decisions"):
        sl = con.execute(
            """
            SELECT decision, COUNT(*) n, AVG(probability) p, AVG(threshold) t
            FROM superlearner_decisions WHERE timestamp>=? GROUP BY decision
            """,
            (since,),
        ).fetchall()
        print("\nSUPERLEARNER")
        if sl:
            for r in sl:
                print(f"{r['decision']:8} n={r['n']:5} avgP={float(r['p'] or 0):.3f} avgThreshold={float(r['t'] or 0):.3f}")
        else:
            print("No candidate reached SuperLearner in this window.")

    print("\nGPT / LUNA")
    if table_exists(con, "gpt_usage_events"):
        u = con.execute(
            """
            SELECT call_type, COUNT(*) calls, COALESCE(SUM(input_tokens),0) inp,
                   COALESCE(SUM(output_tokens),0) outp
            FROM gpt_usage_events WHERE timestamp>=? GROUP BY call_type
            """,
            (since,),
        ).fetchall()
        if u:
            for r in u:
                print(f"{r['call_type']:10} calls={r['calls']:4} input={r['inp']:6} output={r['outp']:5}")
        else:
            print("No Luna API call reached in this window (candidate stopped earlier).")
    else:
        print("GPT usage table not created yet.")

    if table_exists(con, "gpt_candidate_reviews"):
        reviews = con.execute(
            """
            SELECT decision, COUNT(*) n, AVG(confidence) conf
            FROM gpt_candidate_reviews WHERE reviewed_at>=? GROUP BY decision
            """,
            (since,),
        ).fetchall()
        for r in reviews:
            print(f"review {r['decision']:7} n={r['n']:4} avg_conf={float(r['conf'] or 0):.2f}")

    print("\nREAL DEMO OUTCOMES - LAST 7 DAYS")
    outcomes = con.execute(
        """
        SELECT COALESCE(s.family,'unknown') family, COUNT(*) n,
               AVG(d.reward_r) mean_r, SUM(d.reward_r) sum_r,
               AVG(CASE WHEN d.reward_r>0 THEN 1.0 ELSE 0.0 END) win_rate
        FROM demo_positions d LEFT JOIN strategies s ON s.id=d.strategy_id
        WHERE d.status='closed' AND d.reward_r IS NOT NULL AND d.closed_at>=?
        GROUP BY COALESCE(s.family,'unknown') ORDER BY sum_r DESC
        """,
        (since7,),
    ).fetchall()
    if outcomes:
        for r in outcomes:
            print(
                f"{r['family']:18} n={r['n']:3} mean={float(r['mean_r'] or 0):+.3f}R "
                f"sum={float(r['sum_r'] or 0):+.2f}R win={pct(float(r['win_rate'] or 0))}"
            )
    else:
        print("No closed DEMO broker trades in last 7 days.")

    rebuilt = None
    if table_exists(con, "system_state"):
        row = con.execute("SELECT value FROM system_state WHERE key='v66_execution_memory_rebuilt'").fetchone()
        rebuilt = str(row["value"]) if row else "0"
    print(f"\nV6.6 execution-memory repair applied: {'YES' if rebuilt == '1' else 'NO (will run once in DEMO engine)'}")

    # One-line bottleneck diagnosis.
    print("\nPRIMARY BOTTLENECK")
    if total and buckets.get("NO_SIGNAL", 0) / total >= 0.70:
        print("Candidate/signal starvation: strategy pool is running, but most closed bars have no executable scalp signal.")
    elif buckets.get("SPARTAN", 0) > max(buckets.get("SUPERLEARNER", 0), buckets.get("GPT", 0)):
        print("Spartan deterministic gate is the dominant blocker.")
    elif buckets.get("SUPERLEARNER", 0) > buckets.get("GPT", 0):
        print("SuperLearner calibration is the dominant blocker.")
    elif buckets.get("GPT", 0):
        print("Luna is receiving candidates and is the dominant final veto.")
    else:
        print("No single dominant blocker identified in this window; inspect the funnel above.")
    print("=" * 78)


if __name__ == "__main__":
    main()
