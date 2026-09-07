from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import settings


def _safe_query(con: sqlite3.Connection, sql: str, params=()):
    try:
        return con.execute(sql, params).fetchall()
    except sqlite3.Error:
        return []


def main() -> int:
    print("=" * 92)
    print("V8.0.1 TRIGGER -> ORDER PIPELINE DIAGNOSTIC  (STATUS ONLY)")
    print("=" * 92)
    since = (datetime.now(timezone.utc) - timedelta(minutes=90)).isoformat()

    with sqlite3.connect(settings.DATABASE_PATH, timeout=30) as con:
        con.row_factory = sqlite3.Row

        setups = _safe_query(
            con,
            """
            SELECT id,symbol,playbook,side,phase,score,strategy_id,execution_tier,
                   created_at,triggered_at,resolved_at,outcome_r
            FROM micro_hunter_setups
            WHERE COALESCE(triggered_at,updated_at)>=?
              AND phase IN ('TRIGGERED','RESOLVED','INVALIDATED')
            ORDER BY id DESC LIMIT 12
            """,
            (since,),
        )

        if not setups:
            print("No recent TRIGGERED/RESOLVED micro setups in the last 90 minutes.")
            print("Run 33_V8_INSTITUTIONAL_STATUS.bat to confirm WATCH/ARM/TRIGGER activity.")
            return 0

        for s in reversed(setups):
            side = "BUY" if int(s["side"] or 0) == 1 else "SELL"
            start = s["triggered_at"] or s["created_at"]
            print("\n" + "-" * 92)
            print(
                f"SETUP #{s['id']} | {s['symbol']} {side} | {s['playbook']} | "
                f"score={float(s['score'] or 0):.1f} | phase={s['phase']} | "
                f"strategy={s['strategy_id']} tier={s['execution_tier'] or '?'}"
            )
            print(f"triggered={start} outcomeR={s['outcome_r']}")

            # Decision path after trigger.
            params = [s["symbol"], start]
            strategy_clause = ""
            if s["strategy_id"] is not None:
                strategy_clause = " AND (strategy_id=? OR strategy_id IS NULL)"
                params.append(int(s["strategy_id"]))
            rows = _safe_query(
                con,
                f"""
                SELECT timestamp,strategy_id,reason
                FROM decision_logs
                WHERE mode='demo' AND symbol=? AND timestamp>=?
                {strategy_clause}
                ORDER BY id ASC LIMIT 12
                """,
                tuple(params),
            )
            print("DECISION PATH:")
            if not rows:
                print("  no matching decision-log row after trigger")
            else:
                for r in rows:
                    reason = str(r["reason"] or "")
                    if len(reason) > 120:
                        reason = reason[:117] + "..."
                    print(f"  {r['timestamp']} | s={r['strategy_id']} | {reason}")

            # SuperLearner stage.
            if s["strategy_id"] is not None:
                sl = _safe_query(
                    con,
                    """
                    SELECT timestamp,decision,probability,threshold,bayes_loss_probability,reason
                    FROM superlearner_decisions
                    WHERE symbol=? AND strategy_id=? AND timestamp>=?
                    ORDER BY id ASC LIMIT 3
                    """,
                    (s["symbol"], int(s["strategy_id"]), start),
                )
            else:
                sl = []
            print("SUPERLEARNER:")
            if not sl:
                print("  not reached / no matching record")
            else:
                for r in sl:
                    print(
                        f"  {r['timestamp']} | {r['decision']} | "
                        f"P={float(r['probability']):.3f}/{float(r['threshold']):.3f} "
                        f"BayesLoss={float(r['bayes_loss_probability']):.3f} | {r['reason']}"
                    )

            # V8 local pre-Luna stage.
            if s["strategy_id"] is not None:
                gates = _safe_query(
                    con,
                    """
                    SELECT timestamp,local_score,threshold,allowed,expected_r,probability,reason
                    FROM v8_alpha_gate_events
                    WHERE symbol=? AND strategy_id=? AND timestamp>=?
                    ORDER BY id ASC LIMIT 3
                    """,
                    (s["symbol"], int(s["strategy_id"]), start),
                )
            else:
                gates = []
            print("LOCAL PRE-LUNA:")
            if not gates:
                print("  not reached")
            else:
                for r in gates:
                    print(
                        f"  {r['timestamp']} | {'PASS' if int(r['allowed']) else 'HOLD'} | "
                        f"score={float(r['local_score']):.1f}/{float(r['threshold']):.1f} "
                        f"ER={float(r['expected_r'] or 0):+.3f}R P={float(r['probability'] or 0):.3f} | {r['reason']}"
                    )

            # GPT calls near/after trigger for this symbol.
            gpt = _safe_query(
                con,
                """
                SELECT timestamp,call_type,input_tokens,output_tokens,source
                FROM gpt_usage_events
                WHERE symbol=? AND timestamp>=?
                ORDER BY id ASC LIMIT 3
                """,
                (s["symbol"], start),
            )
            print("LUNA:")
            if not gpt:
                print("  no paid/cached Luna usage record after trigger")
            else:
                for r in gpt:
                    print(
                        f"  {r['timestamp']} | {r['call_type']} | "
                        f"in={r['input_tokens']} out={r['output_tokens']} source={r['source']}"
                    )

            # Execution attempt.
            if s["strategy_id"] is not None:
                ex = _safe_query(
                    con,
                    """
                    SELECT timestamp,status,message
                    FROM execution_logs
                    WHERE symbol=? AND strategy_id=? AND timestamp>=?
                    ORDER BY id ASC LIMIT 3
                    """,
                    (s["symbol"], int(s["strategy_id"]), start),
                )
            else:
                ex = []
            print("BROKER EXECUTION:")
            if not ex:
                print("  no order_check/order_send attempt")
            else:
                for r in ex:
                    print(f"  {r['timestamp']} | {r['status']} | {r['message']}")

        print("\n" + "=" * 92)
        print("Interpretation: the first section that says 'not reached' identifies where the")
        print("trigger stopped. This tool changes NO trading settings and uses NO Luna/API tokens.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
