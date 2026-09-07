from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import datetime, timedelta, timezone

import settings
import institutional_alpha as alpha


def main() -> int:
    print("=" * 84)
    print("V8.0 INSTITUTIONAL MICRO-SCALP ENGINE STATUS - DEMO ONLY")
    print("=" * 84)
    print("Architecture: TICK/M1 ALPHA -> ADAPTIVE WATCH/ARM/TRIGGER -> HARD GATES ->")
    print("              SUPERLEARNER ENSEMBLE -> EDGE RECOVERY -> LOCAL LUNA SHORTLIST -> LUNA -> DEMO")
    print(f"Luna caps: pretrade={int(getattr(settings,'SPARTAN_GPT_MAX_PRETRADE_API_CALLS_PER_DAY',0))}/day "
          f"posttrade={int(getattr(settings,'SPARTAN_GPT_MAX_POSTTRADE_API_CALLS_PER_DAY',0))}/day")
    print("Startup connectivity: LOCAL check only (0 API tokens). 18_TEST_GPT_CONNECTION.bat is manual paid network test.")

    with sqlite3.connect(settings.DATABASE_PATH, timeout=30) as con:
        con.row_factory = sqlite3.Row
        alpha.ensure_tables(con)
        state = alpha.status(con)
        print("\nADAPTIVE ALPHA THRESHOLDS")
        for symbol, item in state["thresholds"].items():
            print(f"{symbol:9s} WATCH={item['watch']:.1f} ARM={item['arm']:.1f} TRIGGER={item['trigger']:.1f} samples={int(item['samples'])}")

        since60 = (datetime.now(timezone.utc)-timedelta(minutes=60)).isoformat()
        print("\nLAST 60 MIN MICRO SETUPS")
        try:
            rows = con.execute(
                """SELECT symbol,phase,COUNT(*) n,ROUND(AVG(score),1) avg_score,ROUND(MAX(score),1) max_score
                   FROM micro_hunter_setups WHERE updated_at>=?
                   GROUP BY symbol,phase ORDER BY symbol,phase""", (since60,)
            ).fetchall()
        except sqlite3.Error:
            rows=[]
        if not rows:
            print("none yet")
        else:
            for r in rows:
                print(f"{r['symbol']:9s} {r['phase']:11s} n={r['n']:3d} avg={r['avg_score'] or 0:5.1f} max={r['max_score'] or 0:5.1f}")

        print("\nLAST 60 MIN PRE-LUNA LOCAL SHORTLIST")
        gates = state.get("gate60") or []
        if not gates:
            print("none yet")
        else:
            for r in gates[:12]:
                flag = "TO-LUNA" if int(r.get('allowed') or 0) else "LOCAL-HOLD"
                print(f"{r['timestamp']} | {r['symbol']} {r.get('playbook') or '-':24s} {flag:10s} "
                      f"score={float(r['local_score']):.1f}/{float(r['threshold']):.1f} "
                      f"ER={float(r['expected_r'] or 0):+.2f}R P={float(r['probability'] or 0):.2f} | {r['reason']}")

        print("\nV8 NATIVE ALPHA DEMO TRADES - TODAY")
        today=datetime.now(timezone.utc).date().isoformat()
        rows=con.execute(
            """SELECT symbol,status,COUNT(*) n,ROUND(COALESCE(SUM(reward_r),0),3) sum_r,
                      ROUND(COALESCE(AVG(reward_r),0),3) mean_r
               FROM demo_positions WHERE substr(opened_at,1,10)=? AND execution_tier='v8_native_alpha'
               GROUP BY symbol,status""", (today,)
        ).fetchall()
        if not rows: print("none yet")
        else:
            for r in rows: print(f"{r['symbol']:9s} {r['status']:6s} n={r['n']} sum={r['sum_r']:+.3f}R mean={r['mean_r']:+.3f}R")

        print("\nLUNA USAGE - LAST 24H")
        since24=(datetime.now(timezone.utc)-timedelta(hours=24)).isoformat()
        try:
            rows=con.execute(
                """SELECT call_type,COUNT(*) n,COALESCE(SUM(input_tokens),0) tin,COALESCE(SUM(output_tokens),0) tout
                   FROM gpt_usage_events WHERE timestamp>=? GROUP BY call_type""", (since24,)
            ).fetchall()
        except sqlite3.Error:
            rows=[]
        if not rows: print("recorded trading calls=0")
        else:
            for r in rows: print(f"{r['call_type']}: calls={r['n']} input={r['tin']} output={r['tout']}")

        print("\nNOTE")
        print("V8 native alpha is DEMO-only, max 0.30x risk, and cannot bypass Spartan/Bayesian/Edge/Luna/broker hard locks.")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
