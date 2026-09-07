from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import settings
import v9_2_adaptive_scalper as v92


def _rows(con, sql, params=()):
    try:
        return con.execute(sql, params).fetchall()
    except sqlite3.Error:
        return []


def _count(con, sql, params=()):
    try:
        row = con.execute(sql, params).fetchone()
        return int(row[0] or 0) if row else 0
    except sqlite3.Error:
        return 0


def _f(v, d=0.0):
    try:
        return float(v)
    except Exception:
        return float(d)


def main() -> int:
    now = datetime.now(timezone.utc)
    today = now.date().isoformat()
    since6 = (now - timedelta(hours=6)).isoformat()
    print("=" * 104)
    print("V9.2 ADAPTIVE SCALPING INTELLIGENCE STATUS — DEMO ONLY")
    print("=" * 104)
    print(
        f"capacity/day={int(getattr(settings,'V9_MAX_TRADES_PER_DAY',0))} | "
        f"per-symbol={int(getattr(settings,'V9_MAX_TRADES_PER_SYMBOL_PER_DAY',0))} | "
        f"freeze-after-losses={int(getattr(settings,'PORTFOLIO_FREEZE_AFTER_LOSSES',0))} | "
        f"freeze={int(getattr(settings,'PORTFOLIO_FREEZE_SECONDS',0))//60}m"
    )
    print(
        "microstructure=ON | dynamic ATR/MFE exits=ON | partial exits=ON | M1/M5/HTF score=ON | "
        "POC/volume-profile=ON | session-context=ON"
    )
    print(
        "rejected-trade learning=ON | champion/challenger=ON | new-context trial risk=0.25x | "
        "martingale=OFF"
    )
    print("100/symbol is a SAFETY CAP, not a forced quota; 80% win rate is a monitoring goal, not a promise.")

    with sqlite3.connect(settings.DATABASE_PATH, timeout=30) as con:
        con.row_factory = sqlite3.Row
        v92.ensure_tables(con)

        print("\nTODAY V9/V9.2 BROKER RESULTS BY SYMBOL")
        rows = _rows(con, """
            SELECT symbol,COUNT(*) n,
                   SUM(CASE WHEN reward_r>0 THEN 1 ELSE 0 END) wins,
                   ROUND(SUM(COALESCE(reward_r,0)),3) sum_r,
                   ROUND(AVG(COALESCE(reward_r,0)),3) avg_r,
                   ROUND(AVG(COALESCE(mfe_r,0)),3) avg_mfe,
                   ROUND(AVG(COALESCE(mae_r,0)),3) avg_mae
            FROM demo_positions
            WHERE status='closed' AND substr(closed_at,1,10)=? AND execution_tier LIKE 'v9%'
            GROUP BY symbol ORDER BY symbol
        """, (today,))
        if not rows:
            print("none yet")
        for r in rows:
            n=int(r['n'] or 0); wins=int(r['wins'] or 0)
            print(
                f"{r['symbol']}: n={n} wins={wins} win%={(100*wins/n if n else 0):.1f} "
                f"sumR={_f(r['sum_r']):+.3f} avgR={_f(r['avg_r']):+.3f} "
                f"MFE={_f(r['avg_mfe']):.2f}R MAE={_f(r['avg_mae']):.2f}R"
            )

        print("\nCONTEXT MEMORY — MOST EXPERIENCED")
        mem = _rows(con, """
            SELECT symbol,playbook,side,regime,session,observations,win_mass,loss_mass,
                   reward_mean,reward_ewma,mfe_mean,mae_mean
            FROM v92_context_memory
            ORDER BY observations DESC, updated_at DESC LIMIT 18
        """)
        if not mem:
            print("none yet")
        for r in mem:
            n=_f(r['observations']); w=_f(r['win_mass']); l=_f(r['loss_mass']); wr=w/max(1e-9,w+l)
            print(
                f"{r['symbol']} {r['playbook']} {'BUY' if int(r['side'])==1 else 'SELL'} "
                f"{r['regime']}/{r['session']} | n={n:.0f} win%={wr*100:.1f} "
                f"meanR={_f(r['reward_mean']):+.3f} ewma={_f(r['reward_ewma']):+.3f} "
                f"MFE={_f(r['mfe_mean']):.2f} MAE={_f(r['mae_mean']):.2f}"
            )

        print("\nLATEST V9.2 ADAPTIVE ENTRY EVENTS — LAST 6H")
        events = _rows(con, """
            SELECT timestamp,symbol,playbook,side,regime,session,score_delta,risk_multiplier,details_json
            FROM v92_adaptive_events
            WHERE timestamp>=? AND event_type='entry_overlay'
            ORDER BY id DESC LIMIT 12
        """, (since6,))
        if not events:
            print("none yet")
        for r in events:
            try:
                d=json.loads(r['details_json'] or '{}')
            except Exception:
                d={}
            micro=d.get('microstructure') or {}; mtf=d.get('mtf') or {}; ctx=d.get('context_memory') or {}
            print(
                f"{r['symbol']} {r['playbook']} {'BUY' if int(r['side'])==1 else 'SELL'} "
                f"{r['regime']}/{r['session']} | dScore={_f(r['score_delta']):+.1f} "
                f"risk_x={_f(r['risk_multiplier']):.2f} cap={_f(d.get('risk_cap'),1):.2f} "
                f"flow={_f(micro.get('combined')):+.2f} MTF={_f(mtf.get('alignment')):+.2f} "
                f"mem_n={int(_f(ctx.get('observations')))} mem={ctx.get('state','?')}"
            )

        print("\nCHAMPION / CHALLENGER")
        variants = _rows(con, """
            SELECT * FROM v92_variant_memory
            ORDER BY MAX(champion_n,challenger_n) DESC, updated_at DESC LIMIT 12
        """)
        if not variants:
            print("no completed V9.2 broker outcomes yet")
        for r in variants:
            cn=int(r['champion_n'] or 0); hn=int(r['challenger_n'] or 0)
            cm=_f(r['champion_sum_r'])/cn if cn else 0; hm=_f(r['challenger_sum_r'])/hn if hn else 0
            cw=int(r['champion_wins'] or 0); hw=int(r['challenger_wins'] or 0)
            print(
                f"{r['symbol']} {r['playbook']} {'BUY' if int(r['side'])==1 else 'SELL'} "
                f"{r['regime']}/{r['session']} active={r['active_variant']} | "
                f"champ n={cn} meanR={cm:+.2f} win%={(100*cw/cn if cn else 0):.0f} | "
                f"chall n={hn} meanR={hm:+.2f} win%={(100*hw/hn if hn else 0):.0f}"
            )

        print("\nREJECTED-TRADE COUNTERFACTUALS")
        vr = _rows(con, """
            SELECT symbol,COUNT(*) n,SUM(CASE WHEN reward_r>0 THEN 1 ELSE 0 END) wins,
                   ROUND(AVG(reward_r),3) avg_r
            FROM v92_virtual_trials
            WHERE trial_type='rejected' AND status='closed'
            GROUP BY symbol ORDER BY symbol
        """)
        if not vr:
            print("none closed yet")
        for r in vr:
            n=int(r['n'] or 0); w=int(r['wins'] or 0)
            print(f"{r['symbol']}: n={n} virtual-win%={(100*w/n if n else 0):.1f} avgR={_f(r['avg_r']):+.3f}")

        print("\nV9.2 EXIT / EXECUTION EVENTS — LAST 6H")
        ex = _rows(con, """
            SELECT status,message,COUNT(*) n
            FROM execution_logs
            WHERE timestamp>=? AND (message LIKE 'v9_2_%' OR message LIKE 'v9_execution_first:%')
            GROUP BY status,message ORDER BY n DESC LIMIT 15
        """, (since6,))
        if not ex:
            print("none yet")
        for r in ex:
            print(f"{int(r['n'])} | {r['status']} | {r['message']}")

        print("\nOPEN V9 POSITIONS")
        op = _rows(con, """
            SELECT id,symbol,strategy_id,side,volume,opened_at,execution_tier,mfe_r,mae_r
            FROM demo_positions WHERE status='open' AND execution_tier LIKE 'v9%'
            ORDER BY id DESC
        """)
        if not op:
            print("none")
        for r in op:
            print(
                f"id={r['id']} {r['symbol']} {'BUY' if int(r['side'])==1 else 'SELL'} "
                f"vol={r['volume']} tier={r['execution_tier']} MFE={_f(r['mfe_r']):.2f} MAE={_f(r['mae_r']):.2f}"
            )

        total = _count(con, "SELECT COUNT(*) FROM demo_positions WHERE status='closed' AND execution_tier LIKE 'v9%'")
        wins = _count(con, "SELECT COUNT(*) FROM demo_positions WHERE status='closed' AND execution_tier LIKE 'v9%' AND reward_r>0")
        sumrow = con.execute("SELECT COALESCE(SUM(reward_r),0),COALESCE(AVG(reward_r),0) FROM demo_positions WHERE status='closed' AND execution_tier LIKE 'v9%'").fetchone()
        sumr=_f(sumrow[0]); avgr=_f(sumrow[1])
        print("\nLEARNING / EARLY WARNING")
        print(f"all V9 closed={total} wins={wins} win%={(100*wins/total if total else 0):.1f} sumR={sumr:+.3f} avgR={avgr:+.3f}")
        if total < 25:
            print("Evidence still small: keep conclusions provisional until >=25-30 broker trades.")
        elif avgr <= 0:
            print("WARNING: recent architecture is not showing positive mean R yet; do not increase base risk.")
        elif total >= 30:
            print("Execution/learning evidence is meaningful enough for symbol/playbook optimization review.")
        print("V9.2 never doubles size after a loss. Adverse trades can be cut early; a new opposite trade requires a fresh signal.")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
