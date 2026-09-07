from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import settings


def _rows(con, sql, params=()):
    try:
        return con.execute(sql, params).fetchall()
    except sqlite3.Error:
        return []


def _count(con, sql, params=()):
    try:
        r = con.execute(sql, params).fetchone()
        return int((r[0] if r else 0) or 0)
    except sqlite3.Error:
        return 0


def _pf(gw, gl):
    gw = float(gw or 0.0)
    gl = abs(float(gl or 0.0))
    if gl <= 1e-12:
        return 99.0 if gw > 0 else 0.0
    return gw / gl


def main() -> int:
    now = datetime.now(timezone.utc)
    today = now.date().isoformat()
    since = (now - timedelta(hours=6)).isoformat()
    print('=' * 104)
    print('V9.1 HIGH-PRECISION LEARNING OPTIMIZER STATUS - DEMO ONLY')
    print('=' * 104)
    print(
        f"capacity/day={int(getattr(settings,'V9_MAX_TRADES_PER_DAY',0))} | "
        f"per-symbol={int(getattr(settings,'V9_MAX_TRADES_PER_SYMBOL_PER_DAY',0))} | "
        f"freeze-after-losses={int(getattr(settings,'PORTFOLIO_FREEZE_AFTER_LOSSES',0))} | "
        f"freeze={int(getattr(settings,'PORTFOLIO_FREEZE_SECONDS',0))//60}m"
    )
    print(
        f"precision execute score>={float(getattr(settings,'V9_EXECUTE_SCORE_MIN',0)):.1f} | "
        f"ER>={float(getattr(settings,'V9_HARD_MIN_EXPECTED_R',0)):+.2f}R | "
        f"P-BE>={float(getattr(settings,'V9_HARD_MIN_PROB_EDGE',0)):+.2f} | "
        f"win-rate goal={float(getattr(settings,'V9_PRECISION_TARGET_WIN_RATE',0))*100:.0f}% (goal, NOT guarantee)"
    )
    print(
        f"recovery-mode={bool(getattr(settings,'V9_RECOVERY_MODE_ENABLED',False))} | "
        f"recovery risk cap={float(getattr(settings,'V9_RECOVERY_MAX_RISK_MULTIPLIER',0)):.2f}x | "
        f"martingale={bool(getattr(settings,'V9_MARTINGALE_ENABLED',False))}"
    )

    with sqlite3.connect(settings.DATABASE_PATH, timeout=30) as con:
        con.row_factory = sqlite3.Row

        print('\nTODAY V9 BROKER RESULTS BY SYMBOL')
        rows = _rows(con, """
            SELECT symbol, COUNT(*) n,
                   SUM(CASE WHEN reward_r>0 THEN 1 ELSE 0 END) wins,
                   ROUND(SUM(reward_r),3) sum_r,
                   ROUND(AVG(reward_r),3) avg_r,
                   ROUND(SUM(CASE WHEN reward_r>0 THEN reward_r ELSE 0 END),3) gross_win_r,
                   ROUND(SUM(CASE WHEN reward_r<0 THEN reward_r ELSE 0 END),3) gross_loss_r
            FROM demo_positions
            WHERE substr(opened_at,1,10)=? AND execution_tier='v9_execution_first' AND status='closed'
            GROUP BY symbol ORDER BY sum_r DESC
        """, (today,))
        if not rows:
            print('none yet')
        else:
            for r in rows:
                n=int(r['n'] or 0); w=int(r['wins'] or 0)
                print(
                    f"{r['symbol']}: n={n} wins={w} win%={(100*w/max(1,n)):.1f} "
                    f"sumR={float(r['sum_r'] or 0):+.3f} avgR={float(r['avg_r'] or 0):+.3f} "
                    f"PF={_pf(r['gross_win_r'],r['gross_loss_r']):.2f}"
                )

        total = _rows(con, """
            SELECT COUNT(*) n,
                   SUM(CASE WHEN reward_r>0 THEN 1 ELSE 0 END) wins,
                   ROUND(SUM(reward_r),3) sum_r,
                   ROUND(AVG(reward_r),3) avg_r,
                   ROUND(AVG(mfe_r),3) avg_mfe,
                   ROUND(AVG(mae_r),3) avg_mae,
                   ROUND(SUM(CASE WHEN reward_r>0 THEN reward_r ELSE 0 END),3) gross_win_r,
                   ROUND(SUM(CASE WHEN reward_r<0 THEN reward_r ELSE 0 END),3) gross_loss_r
            FROM demo_positions
            WHERE execution_tier='v9_execution_first' AND status='closed'
        """)
        print('\nALL V9 BROKER EVIDENCE')
        if total and int(total[0]['n'] or 0) > 0:
            r=total[0]; n=int(r['n'] or 0); w=int(r['wins'] or 0)
            print(
                f"closed={n} wins={w} win%={100*w/max(1,n):.1f} sumR={float(r['sum_r'] or 0):+.3f} "
                f"avgR={float(r['avg_r'] or 0):+.3f} PF={_pf(r['gross_win_r'],r['gross_loss_r']):.2f} "
                f"avgMFE={float(r['avg_mfe'] or 0):.3f}R avgMAE={float(r['avg_mae'] or 0):.3f}R"
            )
        else:
            print('none yet')

        print('\nPLAYBOOK BROKER MEMORY - MOST EXPERIENCED')
        mem = _rows(con, """
            SELECT symbol,playbook,side,observations,wins,losses,reward_sum,reward_ewma,mfe_sum,mae_sum
            FROM micro_playbook_memory
            ORDER BY observations DESC, reward_ewma DESC LIMIT 18
        """)
        if not mem:
            print('none')
        else:
            for r in mem:
                n=max(1,int(r['observations'] or 0)); w=int(r['wins'] or 0)
                print(
                    f"{r['symbol']} {r['playbook']} {'BUY' if int(r['side'])==1 else 'SELL'} "
                    f"n={int(r['observations'] or 0)} win%={100*w/n:.1f} "
                    f"meanR={float(r['reward_sum'] or 0)/n:+.3f} ewma={float(r['reward_ewma'] or 0):+.3f}R "
                    f"MFE={float(r['mfe_sum'] or 0)/n:.2f} MAE={float(r['mae_sum'] or 0)/n:.2f}"
                )

        print('\nLATEST V9.1 PRECISION DECISIONS - LAST 6H')
        dec = _rows(con, """
            SELECT timestamp,symbol,playbook,execute,grade,final_score,risk_multiplier,
                   expected_r,probability,break_even_probability,reason,context_json
            FROM v9_execution_decisions WHERE timestamp>=?
            ORDER BY id DESC LIMIT 18
        """, (since,))
        if not dec:
            print('none')
        else:
            for r in dec:
                ctx={}
                try: ctx=json.loads(r['context_json'] or '{}')
                except Exception: pass
                pm=ctx.get('precision_memory') if isinstance(ctx,dict) else {}
                pm=pm if isinstance(pm,dict) else {}
                print(
                    f"{r['symbol']} {r['playbook']} {'GO' if int(r['execute']) else 'HOLD'} {r['grade']} "
                    f"score={float(r['final_score'] or 0):.1f} risk_x={float(r['risk_multiplier'] or 0):.2f} "
                    f"ER={float(r['expected_r'] or 0):+.2f} P={float(r['probability'] or 0):.2f}/BE={float(r['break_even_probability'] or 0):.2f} "
                    f"mem={pm.get('memory_state','legacy')} n={pm.get('observations','?')} "
                    f"dScore={pm.get('score_delta','?')} recovery={bool(ctx.get('recovery_mode'))} | {r['reason']}"
                )

        print('\nBROKER EXECUTION - LAST 6H')
        ex = _rows(con, """
            SELECT status,message,COUNT(*) n FROM execution_logs
            WHERE timestamp>=? GROUP BY status,message ORDER BY n DESC LIMIT 10
        """, (since,))
        if not ex:
            print('none')
        else:
            for r in ex:
                print(f"{r['n']} | {r['status']} | {str(r['message'] or '')[:150]}")

        print('\nLEARNING INTERPRETATION')
        n = _count(con, "SELECT COUNT(*) FROM demo_positions WHERE execution_tier='v9_execution_first' AND status='closed'")
        if n < 25:
            print(f"Evidence sample={n}. Precision memory is active, but keep conclusions provisional until ~25-30 closed trades.")
        else:
            print(f"Evidence sample={n}. Compare playbook/symbol win%, meanR, PF, MFE/MAE and recovery-mode outcomes before tuning again.")
        print('V9.1 does NOT use martingale. Loss recovery uses a fresh setup with capped/reduced risk, never loss-doubling.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
