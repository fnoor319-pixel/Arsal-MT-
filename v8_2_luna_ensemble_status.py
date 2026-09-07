from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import settings


def _since(minutes: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


def _rows(con, sql, params=()):
    try:
        return con.execute(sql, params).fetchall()
    except sqlite3.Error:
        return []


def main() -> int:
    print("=" * 88)
    print("V8.2 QUANT-LUNA ENSEMBLE STATUS - DEMO ONLY")
    print("=" * 88)
    print(
        f"Luna paid cap/day={int(getattr(settings,'SPARTAN_GPT_MAX_PRETRADE_API_CALLS_PER_DAY',8))} | "
        f"disagreement probe={bool(getattr(settings,'V8_LUNA_DISAGREEMENT_PROBE_ENABLED',True))} "
        f"risk_x={float(getattr(settings,'V8_LUNA_DISAGREEMENT_RISK_MULTIPLIER',0.10)):.2f} "
        f"cap/day={int(getattr(settings,'V8_LUNA_DISAGREEMENT_MAX_PER_DAY',2))} "
        f"cap/symbol={int(getattr(settings,'V8_LUNA_DISAGREEMENT_MAX_PER_SYMBOL_PER_DAY',1))}"
    )

    with sqlite3.connect(settings.DATABASE_PATH, timeout=30) as con:
        con.row_factory = sqlite3.Row
        since60 = _since(60)
        since24 = _since(24*60)

        gates = _rows(con, """
            SELECT timestamp,symbol,strategy_id,playbook,local_score,threshold,allowed,
                   expected_r,probability,reason
            FROM v8_alpha_gate_events
            WHERE timestamp>=?
            ORDER BY id DESC LIMIT 8
        """, (since60,))
        print("\nLOCAL PRE-LUNA - LAST 60 MIN")
        if not gates:
            print("none")
        else:
            for r in reversed(gates):
                print(
                    f"{r['timestamp']} {r['symbol']} s={r['strategy_id']} {r['playbook'] or '?'} "
                    f"{'PASS' if int(r['allowed']) else 'HOLD'} "
                    f"score={float(r['local_score']):.1f}/{float(r['threshold']):.1f} "
                    f"ER={float(r['expected_r'] or 0):+.2f}R P={float(r['probability'] or 0):.2f} | {r['reason']}"
                )

        probes = _rows(con, """
            SELECT timestamp,symbol,strategy_id,playbook,allowed,risk_multiplier,
                   local_score,expected_r,probability,luna_confidence,luna_reason,reason
            FROM v8_luna_disagreement_events
            WHERE timestamp>=?
            ORDER BY id DESC LIMIT 10
        """, (since60,))
        print("\nLUNA DISAGREEMENT DECISIONS - LAST 60 MIN")
        if not probes:
            print("none yet")
        else:
            for r in reversed(probes):
                print(
                    f"{r['timestamp']} {r['symbol']} s={r['strategy_id']} {r['playbook'] or '?'} "
                    f"{'PROBE' if int(r['allowed']) else 'NO-PROBE'} "
                    f"local={float(r['local_score'] or 0):.1f} ER={float(r['expected_r'] or 0):+.2f}R "
                    f"LunaHold={float(r['luna_confidence'] or 0):.2f} risk_x={float(r['risk_multiplier'] or 0):.2f} | {r['reason']}"
                )

        calls = _rows(con, """
            SELECT call_type,COUNT(*) n,COALESCE(SUM(input_tokens),0) tin,COALESCE(SUM(output_tokens),0) tout
            FROM gpt_usage_events
            WHERE timestamp>=?
            GROUP BY call_type
        """, (since24,))
        print("\nLUNA USAGE - LAST 24H")
        if not calls:
            print("calls=0")
        else:
            for r in calls:
                print(f"{r['call_type']}: calls={r['n']} input={r['tin']} output={r['tout']}")

        # Recent orders whose stored entry context identifies a disagreement probe.
        demo = _rows(con, """
            SELECT id,symbol,strategy_id,execution_tier,status,reward_r,pnl,opened_at,closed_at,context_json
            FROM demo_positions
            WHERE opened_at>=?
            ORDER BY id DESC LIMIT 30
        """, (since24,))
        probe_demo = []
        for r in demo:
            try:
                ctx = json.loads(r['context_json'] or '{}')
            except Exception:
                ctx = {}
            if isinstance(ctx, dict) and ctx.get('v8_luna_disagreement_probe'):
                probe_demo.append(r)

        print("\nACTUAL DEMO DISAGREEMENT PROBES - LAST 24H")
        if not probe_demo:
            print("none yet")
        else:
            for r in reversed(probe_demo[-8:]):
                print(
                    f"id={r['id']} {r['symbol']} s={r['strategy_id']} tier={r['execution_tier']} "
                    f"status={r['status']} R={r['reward_r']} pnl={r['pnl']} opened={r['opened_at']}"
                )

        last = _rows(con, """
            SELECT id,symbol,strategy_id,execution_tier,status,reward_r,pnl,opened_at,closed_at
            FROM demo_positions ORDER BY id DESC LIMIT 1
        """)
        print("\nLAST DEMO BROKER TRADE")
        if last:
            r=last[0]
            print(
                f"id={r['id']} {r['symbol']} s={r['strategy_id']} tier={r['execution_tier']} "
                f"status={r['status']} R={r['reward_r']} pnl={r['pnl']} opened={r['opened_at']} closed={r['closed_at']}"
            )
        else:
            print("none")

    print("\nNOTE: A Luna HOLD is still a HOLD for normal-risk trading. V8.2 only permits a capped")
    print("tiny-risk DEMO evidence probe when Quant+SuperLearner are strong and no deterministic")
    print("hard contradiction exists. Real/contest accounts remain blocked.")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
