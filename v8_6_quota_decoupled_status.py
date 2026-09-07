from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import datetime, timedelta, timezone

import settings


def _count(con, sql, params=()):
    try:
        row = con.execute(sql, params).fetchone()
        return int(row[0] or 0) if row else 0
    except sqlite3.Error:
        return 0


def _rows(con, sql, params=()):
    try:
        return con.execute(sql, params).fetchall()
    except sqlite3.Error:
        return []


def main() -> int:
    since = (datetime.now(timezone.utc) - timedelta(hours=4)).isoformat()
    print("=" * 96)
    print("V8.6 QUOTA-DECOUPLED EXECUTION STATUS - LAST 4H")
    print("=" * 96)
    print(
        f"paid Luna cap/day={int(getattr(settings,'SPARTAN_GPT_MAX_PRETRADE_API_CALLS_PER_DAY',0))} | "
        f"local shortlist soft cap={int(getattr(settings,'V8_PRE_LUNA_LOCAL_DAILY_PASS_CAP',0))} | "
        f"zero-token probes/day={int(getattr(settings,'V8_BUDGET_PROBE_MAX_PER_DAY',0))} | "
        f"per-symbol={int(getattr(settings,'V8_BUDGET_PROBE_MAX_PER_SYMBOL_PER_DAY',0))}"
    )
    print(f"quota-decoupled execution: {bool(getattr(settings,'V8_QUOTA_DECOUPLED_EXECUTION',False))}")

    with sqlite3.connect(settings.DATABASE_PATH, timeout=30) as con:
        con.row_factory = sqlite3.Row

        sl_approve = _count(con, "SELECT COUNT(*) FROM superlearner_decisions WHERE timestamp>=? AND decision='APPROVE'", (since,))
        sl_reject = _count(con, "SELECT COUNT(*) FROM superlearner_decisions WHERE timestamp>=? AND decision='REJECT'", (since,))
        print(f"\nSUPERLEARNER: APPROVE={sl_approve} REJECT={sl_reject}")

        gates = _rows(con, """
            SELECT allowed,reason,COUNT(*) n,
                   ROUND(AVG(local_score),1) avg_score,
                   ROUND(AVG(expected_r),2) avg_er
            FROM v8_alpha_gate_events
            WHERE timestamp>=?
            GROUP BY allowed,reason ORDER BY n DESC
        """, (since,))
        print("\nLOCAL QUANT GATE")
        if not gates:
            print("none")
        else:
            for r in gates:
                print(
                    f"{'QUALIFIED' if int(r['allowed']) else 'HOLD'} n={r['n']} "
                    f"avgScore={r['avg_score']} avgER={r['avg_er']}R | {r['reason']}"
                )

        probes = _rows(con, """
            SELECT allowed,source,reason,COUNT(*) n,
                   ROUND(AVG(local_score),1) avg_local,
                   ROUND(AVG(micro_score),1) avg_micro,
                   ROUND(AVG(expected_r),2) avg_er,
                   ROUND(AVG(probability-break_even_probability),3) avg_edge
            FROM v8_budget_probe_events
            WHERE timestamp>=?
            GROUP BY allowed,source,reason ORDER BY n DESC
        """, (since,))
        print("\nZERO-TOKEN QUANT EVIDENCE LANE")
        if not probes:
            print("not reached yet")
        else:
            for r in probes:
                print(
                    f"{'PROBE' if int(r['allowed']) else 'HOLD'} n={r['n']} source={r['source']} "
                    f"local={r['avg_local']} micro={r['avg_micro']} ER={r['avg_er']}R "
                    f"edge={r['avg_edge']} | {r['reason']}"
                )

        usage = _rows(con, """
            SELECT call_type,COUNT(*) n,COALESCE(SUM(input_tokens),0) tin,
                   COALESCE(SUM(output_tokens),0) tout
            FROM gpt_usage_events
            WHERE timestamp>=?
            GROUP BY call_type
        """, (since,))
        print("\nLUNA API")
        if not usage:
            print("calls=0")
        else:
            for r in usage:
                print(f"{r['call_type']}: calls={r['n']} input={r['tin']} output={r['tout']}")

        exec_rows = _rows(con, """
            SELECT status,message,COUNT(*) n
            FROM execution_logs
            WHERE timestamp>=?
            GROUP BY status,message ORDER BY n DESC LIMIT 12
        """, (since,))
        print("\nBROKER ORDER_CHECK / ORDER_SEND")
        if not exec_rows:
            print("none yet")
        else:
            for r in exec_rows:
                print(f"{r['n']} | {r['status']} | {str(r['message'] or '')[:150]}")

        positions = _rows(con, """
            SELECT id,symbol,strategy_id,execution_tier,status,reward_r,pnl,opened_at,closed_at
            FROM demo_positions
            WHERE opened_at>=?
            ORDER BY id DESC LIMIT 12
        """, (since,))
        print("\nBROKER-DEMO POSITIONS")
        if not positions:
            print("none yet")
        else:
            for r in positions:
                print(
                    f"id={r['id']} {r['symbol']} s={r['strategy_id']} tier={r['execution_tier']} "
                    f"status={r['status']} R={r['reward_r']} pnl={r['pnl']} opened={r['opened_at']}"
                )

        quota_qualified = _count(
            con,
            """
            SELECT COUNT(*) FROM v8_alpha_gate_events
            WHERE timestamp>=? AND allowed=1
              AND reason='local alpha qualified; paid Luna shortlist quota exhausted'
            """,
            (since,),
        )
        probe_allowed = _count(con, "SELECT COUNT(*) FROM v8_budget_probe_events WHERE timestamp>=? AND allowed=1", (since,))
        broker_attempts = _count(con, "SELECT COUNT(*) FROM execution_logs WHERE timestamp>=?", (since,))
        opened = len(positions)

        print("\nFUNNEL")
        print(
            f"SL_APPROVE={sl_approve} -> quotaQualified={quota_qualified} "
            f"-> zeroTokenProbeAllowed={probe_allowed} -> brokerAttempts={broker_attempts} -> positions={opened}"
        )
        if quota_qualified > 0 and probe_allowed == 0:
            print("NEXT CHECK: zero-token policy is reached/eligible; inspect HOLD reasons above.")
        elif probe_allowed > 0 and broker_attempts == 0:
            print("NEXT CHECK: Quant probe passed; remaining blocker is risk/sizing/fresh-price before broker send.")
        elif broker_attempts > 0 and opened == 0:
            print("NEXT CHECK: MT5/broker is rejecting order_check/order_send.")
        elif opened > 0:
            print("EXECUTION PATH ACTIVE: evaluate broker-DEMO outcomes now.")
        else:
            print("Waiting for a fresh qualified setup under V8.6.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
