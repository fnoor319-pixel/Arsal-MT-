from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import settings
import v9_execution_first as v9


def _rows(con, sql, params=()):
    try:
        return con.execute(sql, params).fetchall()
    except sqlite3.Error:
        return []


def _count(con, sql, params=()):
    try:
        r = con.execute(sql, params).fetchone()
        return int((r[0] if not isinstance(r, sqlite3.Row) else r[0]) or 0) if r else 0
    except sqlite3.Error:
        return 0


def main() -> int:
    since = (datetime.now(timezone.utc) - timedelta(hours=4)).isoformat()
    today = datetime.now(timezone.utc).date().isoformat()
    print("=" * 100)
    print("V9.0 EXECUTION-FIRST SCALPER STATUS — DEMO ONLY")
    print("=" * 100)
    print(
        f"enabled={bool(getattr(settings,'V9_EXECUTION_FIRST_ENABLED',False))} | "
        f"trades/day={int(getattr(settings,'V9_MAX_TRADES_PER_DAY',0))} | "
        f"per-symbol={int(getattr(settings,'V9_MAX_TRADES_PER_SYMBOL_PER_DAY',0))} | "
        f"base-risk={float(getattr(settings,'V9_BASE_RISK_PER_TRADE',0))*100:.3f}%"
    )
    print(
        f"execute score>={float(getattr(settings,'V9_EXECUTE_SCORE_MIN',0)):.1f} | "
        f"Luna advisory score>={float(getattr(settings,'V9_LUNA_ADVISORY_MIN_SCORE',0)):.1f} | "
        f"paid Luna cap/day={int(getattr(settings,'SPARTAN_GPT_MAX_PRETRADE_API_CALLS_PER_DAY',0))}"
    )
    print(
        f"spread: legacy={float(getattr(settings,'MAX_SPREAD_ATR_FRACTION',0)):.2f} ATR | "
        f"reward/cost max={float(getattr(settings,'V9_ABSOLUTE_MAX_SPREAD_ATR',0)):.2f} ATR | "
        f"cost/target={float(getattr(settings,'V9_MAX_SPREAD_TO_TARGET_FRACTION',0)):.2f}"
    )

    with sqlite3.connect(settings.DATABASE_PATH, timeout=30) as con:
        con.row_factory = sqlite3.Row
        v9.ensure_tables(con)

        print("\nV9 DECISIONS — LAST 4H")
        rows = _rows(con, """
            SELECT execute,grade,COUNT(*) n,ROUND(AVG(pre_score),1) pre,
                   ROUND(AVG(final_score),1) final,ROUND(AVG(risk_multiplier),2) risk,
                   ROUND(AVG(expected_r),2) er
            FROM v9_execution_decisions WHERE timestamp>=?
            GROUP BY execute,grade ORDER BY execute DESC,grade
        """, (since,))
        if not rows:
            print("none yet")
        else:
            for r in rows:
                print(
                    f"{'EXECUTE' if int(r['execute']) else 'HOLD'} grade={r['grade']} n={r['n']} "
                    f"pre={r['pre']} final={r['final']} risk_x={r['risk']} avgER={r['er']}R"
                )

        print("\nLATEST V9 DECISIONS")
        recent = _rows(con, """
            SELECT timestamp,symbol,playbook,execute,grade,final_score,risk_multiplier,
                   expected_r,probability,break_even_probability,luna_decision,luna_confidence,reason
            FROM v9_execution_decisions WHERE timestamp>=?
            ORDER BY id DESC LIMIT 12
        """, (since,))
        if not recent:
            print("none")
        else:
            for r in recent:
                print(
                    f"{r['timestamp']} | {r['symbol']} {r['playbook']} | "
                    f"{'GO' if int(r['execute']) else 'HOLD'} {r['grade']} score={float(r['final_score']):.1f} "
                    f"risk_x={float(r['risk_multiplier']):.2f} ER={float(r['expected_r']):+.2f}R "
                    f"P={float(r['probability']):.2f} BE={float(r['break_even_probability']):.2f} "
                    f"Luna={r['luna_decision']}({float(r['luna_confidence'] or 0):.2f}) | {r['reason']}"
                )

        print("\nBROKER ORDER_CHECK / ORDER_SEND — LAST 4H")
        ex = _rows(con, """
            SELECT status,message,COUNT(*) n FROM execution_logs
            WHERE timestamp>=? GROUP BY status,message ORDER BY n DESC LIMIT 12
        """, (since,))
        if not ex:
            print("none yet")
        else:
            for r in ex:
                print(f"{r['n']} | {r['status']} | {str(r['message'] or '')[:150]}")

        print("\nV9 BROKER-DEMO POSITIONS — TODAY")
        positions = _rows(con, """
            SELECT id,symbol,strategy_id,status,reward_r,pnl,opened_at,closed_at
            FROM demo_positions
            WHERE substr(opened_at,1,10)=? AND execution_tier='v9_execution_first'
            ORDER BY id DESC LIMIT 20
        """, (today,))
        if not positions:
            print("none yet")
        else:
            for r in positions:
                print(
                    f"id={r['id']} {r['symbol']} s={r['strategy_id']} {r['status']} "
                    f"R={r['reward_r']} pnl={r['pnl']} opened={r['opened_at']}"
                )

        print("\nLUNA — LAST 4H")
        luna = _rows(con, """
            SELECT call_type,COUNT(*) n,COALESCE(SUM(input_tokens),0) tin,
                   COALESCE(SUM(output_tokens),0) tout
            FROM gpt_usage_events WHERE timestamp>=? GROUP BY call_type
        """, (since,))
        if not luna:
            print("calls=0")
        else:
            for r in luna:
                print(f"{r['call_type']}: calls={r['n']} input={r['tin']} output={r['tout']}")

        total_demo = _count(con, "SELECT COUNT(*) FROM demo_positions WHERE execution_tier='v9_execution_first'")
        closed_demo = _count(con, "SELECT COUNT(*) FROM demo_positions WHERE execution_tier='v9_execution_first' AND status='closed'")
        avg_r = _rows(con, """
            SELECT ROUND(AVG(reward_r),3) avg_r,ROUND(SUM(reward_r),3) sum_r,
                   SUM(CASE WHEN reward_r>0 THEN 1 ELSE 0 END) wins
            FROM demo_positions WHERE execution_tier='v9_execution_first' AND status='closed'
        """)
        print("\n100-TRADE DEMO EVIDENCE MILESTONE")
        if avg_r and avg_r[0][0] is not None:
            a=avg_r[0]
            print(f"opened={total_demo}/100 closed={closed_demo} avgR={a['avg_r']} sumR={a['sum_r']} wins={a['wins']}")
        else:
            print(f"opened={total_demo}/100 closed={closed_demo} | outcome evidence not available yet")

        print("\nINTERPRETATION")
        v9_go = _count(con, "SELECT COUNT(*) FROM v9_execution_decisions WHERE timestamp>=? AND execute=1", (since,))
        broker = _count(con, "SELECT COUNT(*) FROM execution_logs WHERE timestamp>=?", (since,))
        pos4 = _count(con, "SELECT COUNT(*) FROM demo_positions WHERE opened_at>=? AND execution_tier='v9_execution_first'", (since,))
        if v9_go == 0:
            print("No V9 GO decision yet: inspect latest ensemble scores; do not loosen blindly.")
        elif broker == 0:
            print("V9 is issuing GO decisions but sizing/fresh-price is stopping before broker send.")
        elif pos4 == 0:
            print("Broker attempts are occurring but MT5 is rejecting/not creating positions.")
        else:
            print("V9 execution path is active. Judge the machine from actual DEMO R/MFE/MAE evidence now.")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
