from __future__ import annotations

import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

import settings


HOURS = 18


def _cols(con: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(r[1]) for r in con.execute(f"PRAGMA table_info({table})").fetchall()}
    except sqlite3.Error:
        return set()


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
    now = datetime.now(timezone.utc)
    since = (now - timedelta(hours=HOURS)).isoformat()

    print("=" * 100)
    print(f"V8.5.1 OVERNIGHT EXECUTION AUDIT | LAST {HOURS} HOURS | STATUS ONLY")
    print("=" * 100)
    print(f"UTC now={now.isoformat()}")
    print(f"DB={settings.DATABASE_PATH}")
    print("This audit changes NO trades, NO settings and uses ZERO API tokens.")

    with sqlite3.connect(settings.DATABASE_PATH, timeout=30) as con:
        con.row_factory = sqlite3.Row

        # 1) Strategy/native carriers
        print("\n1) CARRIERS / ACTUAL BROKER POSITIONS")
        native_strats = _count(con, "SELECT COUNT(*) FROM strategies WHERE status='v8_native_demo'")
        demo_opened = _count(con, "SELECT COUNT(*) FROM demo_positions WHERE opened_at>=?", (since,))
        demo_closed = _count(con, "SELECT COUNT(*) FROM demo_positions WHERE opened_at>=? AND status='closed'", (since,))
        print(f"v8_native_demo strategy carriers={native_strats}")
        print(f"broker-DEMO positions opened={demo_opened} | closed={demo_closed}")

        last = _rows(con, """
            SELECT id,symbol,strategy_id,execution_tier,status,reward_r,pnl,opened_at,closed_at
            FROM demo_positions ORDER BY id DESC LIMIT 1
        """)
        if last:
            r=last[0]
            print(
                f"last broker trade: id={r['id']} {r['symbol']} s={r['strategy_id']} "
                f"tier={r['execution_tier']} status={r['status']} R={r['reward_r']} pnl={r['pnl']} "
                f"opened={r['opened_at']}"
            )
        else:
            print("last broker trade: none")

        # 2) Micro hunter
        print("\n2) MICRO HUNTER FUNNEL")
        micro_cols = _cols(con, "micro_hunter_setups")
        time_col = "updated_at" if "updated_at" in micro_cols else "created_at"
        micro = _rows(con, f"""
            SELECT symbol,playbook,phase,COUNT(*) n,
                   ROUND(AVG(score),1) avg_score,ROUND(MAX(score),1) max_score
            FROM micro_hunter_setups
            WHERE {time_col}>=?
            GROUP BY symbol,playbook,phase
            ORDER BY symbol,phase,n DESC
        """, (since,))
        if not micro:
            print("no micro hunter rows")
        else:
            totals=Counter()
            bysymbol=defaultdict(Counter)
            for r in micro:
                totals[str(r["phase"])] += int(r["n"])
                bysymbol[str(r["symbol"])][str(r["phase"])] += int(r["n"])
            print("TOTAL:", " | ".join(f"{k}={v}" for k,v in totals.most_common()))
            for sym,c in bysymbol.items():
                print(sym + ": " + " | ".join(f"{k}={v}" for k,v in c.most_common()))
            print("Top triggered playbooks:")
            trig=[r for r in micro if str(r["phase"])=="TRIGGERED"]
            for r in sorted(trig, key=lambda x:int(x["n"]), reverse=True)[:10]:
                print(
                    f"  {r['symbol']} {r['playbook']} n={r['n']} "
                    f"avg={r['avg_score']} max={r['max_score']}"
                )

        # 3) Decision logs stage reasons
        print("\n3) EXECUTION DECISION LOG - TOP BLOCKERS")
        dec = _rows(con, """
            SELECT reason,COUNT(*) n
            FROM decision_logs
            WHERE mode='demo' AND timestamp>=?
            GROUP BY reason ORDER BY n DESC LIMIT 25
        """, (since,))
        for r in dec:
            reason=str(r["reason"] or "")
            print(f"{int(r['n']):5d}  {reason[:155]}")
        if not dec:
            print("none")

        # 4) SuperLearner
        print("\n4) SUPERLEARNER")
        sl = _rows(con, """
            SELECT decision,COUNT(*) n,
                   ROUND(AVG(probability),3) avg_p,
                   ROUND(AVG(threshold),3) avg_thr
            FROM superlearner_decisions
            WHERE timestamp>=?
            GROUP BY decision ORDER BY n DESC
        """, (since,))
        if sl:
            for r in sl:
                print(f"{r['decision']}: n={r['n']} avgP={r['avg_p']} avgThr={r['avg_thr']}")
        else:
            print("not reached")

        # 5) Local pre-Luna
        print("\n5) LOCAL PRE-LUNA")
        gates = _rows(con, """
            SELECT allowed,reason,COUNT(*) n,
                   ROUND(AVG(local_score),1) avg_score,
                   ROUND(AVG(expected_r),2) avg_er
            FROM v8_alpha_gate_events
            WHERE timestamp>=?
            GROUP BY allowed,reason ORDER BY n DESC
        """, (since,))
        if gates:
            for r in gates:
                print(
                    f"{'PASS' if int(r['allowed']) else 'HOLD'} n={r['n']} "
                    f"avgScore={r['avg_score']} avgER={r['avg_er']}R | {r['reason']}"
                )
        else:
            print("not reached")

        # 6) Luna usage
        print("\n6) LUNA / API USAGE")
        usage = _rows(con, """
            SELECT call_type,source,COUNT(*) n,
                   COALESCE(SUM(input_tokens),0) input_tokens,
                   COALESCE(SUM(output_tokens),0) output_tokens
            FROM gpt_usage_events
            WHERE timestamp>=?
            GROUP BY call_type,source ORDER BY n DESC
        """, (since,))
        if usage:
            for r in usage:
                print(
                    f"{r['call_type']} source={r['source']} calls={r['n']} "
                    f"in={r['input_tokens']} out={r['output_tokens']}"
                )
        else:
            print("no paid API usage")

        # 7) Zero-token budget probes
        print("\n7) ZERO-TOKEN BUDGET PROBE POLICY")
        probes = _rows(con, """
            SELECT allowed,source,reason,COUNT(*) n,
                   ROUND(AVG(local_score),1) avg_local,
                   ROUND(AVG(micro_score),1) avg_micro,
                   ROUND(AVG(expected_r),2) avg_er,
                   ROUND(AVG(probability-break_even_probability),3) avg_edge
            FROM v8_budget_probe_events
            WHERE timestamp>=?
            GROUP BY allowed,source,reason
            ORDER BY n DESC
        """, (since,))
        if probes:
            for r in probes:
                print(
                    f"{'ALLOWED' if int(r['allowed']) else 'BLOCKED'} n={r['n']} "
                    f"source={r['source']} local={r['avg_local']} micro={r['avg_micro']} "
                    f"ER={r['avg_er']}R edge={r['avg_edge']} | {r['reason']}"
                )
        else:
            print("policy was never reached")

        # 8) Luna disagreement probe
        print("\n8) LUNA DISAGREEMENT PROBE POLICY")
        disag = _rows(con, """
            SELECT allowed,reason,COUNT(*) n,
                   ROUND(AVG(local_score),1) avg_local,
                   ROUND(AVG(expected_r),2) avg_er
            FROM v8_luna_disagreement_events
            WHERE timestamp>=?
            GROUP BY allowed,reason ORDER BY n DESC
        """, (since,))
        if disag:
            for r in disag:
                print(
                    f"{'ALLOWED' if int(r['allowed']) else 'BLOCKED'} n={r['n']} "
                    f"local={r['avg_local']} ER={r['avg_er']}R | {r['reason']}"
                )
        else:
            print("policy was never reached")

        # 9) Broker execution attempts
        print("\n9) BROKER ORDER_CHECK / ORDER_SEND")
        exec_cols = _cols(con, "execution_logs")
        if exec_cols:
            ex = _rows(con, """
                SELECT status,message,COUNT(*) n
                FROM execution_logs
                WHERE timestamp>=?
                GROUP BY status,message ORDER BY n DESC LIMIT 20
            """, (since,))
            if ex:
                for r in ex:
                    print(f"{int(r['n']):4d} {r['status']} | {str(r['message'] or '')[:160]}")
            else:
                print("NO broker execution attempt reached")
        else:
            print("execution_logs table not present")

        # 10) automated verdict
        print("\n10) AUTOMATED BOTTLENECK VERDICT")
        triggered = _count(con, f"SELECT COUNT(*) FROM micro_hunter_setups WHERE {time_col}>=? AND phase='TRIGGERED'", (since,))
        sl_count = _count(con, "SELECT COUNT(*) FROM superlearner_decisions WHERE timestamp>=?", (since,))
        local_pass = _count(con, "SELECT COUNT(*) FROM v8_alpha_gate_events WHERE timestamp>=? AND allowed=1", (since,))
        paid = _count(con, "SELECT COUNT(*) FROM gpt_usage_events WHERE timestamp>=? AND call_type='pretrade'", (since,))
        budget_events = _count(con, "SELECT COUNT(*) FROM v8_budget_probe_events WHERE timestamp>=?", (since,))
        budget_allowed = _count(con, "SELECT COUNT(*) FROM v8_budget_probe_events WHERE timestamp>=? AND allowed=1", (since,))
        broker_attempts = _count(con, "SELECT COUNT(*) FROM execution_logs WHERE timestamp>=?", (since,)) if exec_cols else 0

        print(
            f"triggered={triggered} -> superlearner={sl_count} -> localPASS={local_pass} "
            f"-> paidLuna={paid} -> budgetProbeEvents={budget_events} "
            f"(allowed={budget_allowed}) -> brokerAttempts={broker_attempts} -> positions={demo_opened}"
        )

        if triggered == 0:
            verdict = "PRIMARY: Micro Hunter did not create executable triggers."
        elif sl_count == 0:
            verdict = "PRIMARY: Triggers are dying before SuperLearner (spread/Spartan/adaptive/Edge gates)."
        elif local_pass == 0:
            verdict = "PRIMARY: SuperLearner is reached, but no candidate reaches/passes Local Pre-Luna."
        elif paid == 0 and budget_events == 0:
            verdict = "PRIMARY: Local PASS occurs, but Luna/budget-probe stage is not being reached."
        elif budget_events > 0 and budget_allowed == 0 and broker_attempts == 0:
            verdict = "PRIMARY: Zero-token probe policy is reached but rejects every setup; inspect section 7 reasons."
        elif (paid > 0 or budget_allowed > 0) and broker_attempts == 0:
            verdict = "PRIMARY: AI/Quant stage is reached, but execution is still blocked before order_check/order_send."
        elif broker_attempts > 0 and demo_opened == 0:
            verdict = "PRIMARY: Broker order_check/order_send is being attempted but MT5/broker rejects the orders."
        else:
            verdict = "Execution reached broker positions; inspect actual outcomes rather than entry starvation."
        print(verdict)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
