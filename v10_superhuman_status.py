from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import settings
import v10_superhuman_scalper as v10


def main() -> int:
    since=(datetime.now(timezone.utc)-timedelta(hours=6)).isoformat()
    print('='*102)
    print('V10.1 PARALLEL SUPERHUMAN SCALP ROUTER STATUS - DEMO ONLY')
    print('='*102)
    print(f'enabled={bool(getattr(settings,"V10_SUPERHUMAN_SCALPER_ENABLED",False))} | '
          f'expert trigger>={float(getattr(settings,"V10_EXPERT_TRIGGER_MIN_SCORE",72.0)):.1f} | '
          f'capacity/symbol/day={int(getattr(settings,"V9_MAX_TRADES_PER_SYMBOL_PER_DAY",100))}')
    print('Fast brain: Micro Hunter + 8 local expert archetypes run IN PARALLEL on each new closed M1 bar; Luna stays selective.')
    print('Archetypes: '+', '.join(tuple(getattr(settings,'V10_EXPERT_ARCHETYPES',()))))

    with sqlite3.connect(settings.DATABASE_PATH,timeout=30) as con:
        con.row_factory=sqlite3.Row
        v10.ensure_tables(con)
        rows=con.execute('''
            SELECT playbook,side,COUNT(*) n,SUM(accepted) accepted,
                   ROUND(AVG(score),1) avg_score,ROUND(MAX(score),1) max_score
            FROM v10_expert_events WHERE timestamp>=?
            GROUP BY playbook,side ORDER BY accepted DESC,n DESC
        ''',(since,)).fetchall()
        print('\nV10 EXPERT RADAR - LAST 6H')
        if not rows: print('none yet')
        for r in rows:
            print(f"{r['playbook']:28s} {'BUY' if int(r['side'])>0 else 'SELL':4s} seen={r['n']} triggered={r['accepted']} avg={r['avg_score']} max={r['max_score']}")

        events=con.execute('''
            SELECT timestamp,symbol,regime,playbook,side,score,expert_scores_json
            FROM v10_expert_events WHERE accepted=1 AND timestamp>=?
            ORDER BY id DESC LIMIT 12
        ''',(since,)).fetchall()
        print('\nLATEST V10 EXPERT TRIGGERS')
        if not events: print('none yet')
        for r in events:
            try: scores=json.loads(r['expert_scores_json'] or '[]')
            except Exception: scores=[]
            runner=''
            if len(scores)>1:
                s=scores[1]; runner=f" runner={s.get('playbook')}:{float(s.get('score') or 0):.1f}"
            print(f"{r['timestamp']} | {r['symbol']} {r['playbook']} {'BUY' if int(r['side'])>0 else 'SELL'} score={float(r['score']):.1f} regime={r['regime']}{runner}")

        # V10.0.1: v9_execution_decisions stores the action as `execute`
        # (1=GO, 0=HOLD).  Older status code incorrectly queried a non-existent
        # `decision` column.  Keep this schema-adaptive in case a future DB adds it.
        router_rows=con.execute('''
            SELECT timestamp,symbol,hunter_playbook,hunter_score,expert_playbook,expert_score,
                   chosen_source,chosen_playbook,chosen_score,reason
            FROM v10_router_events
            WHERE timestamp>=?
            ORDER BY id DESC LIMIT 15
        ''',(since,)).fetchall()
        print('\nV10.1 PARALLEL ROUTER - LATEST')
        if not router_rows: print('none yet')
        for r in router_rows:
            print(
                f"{r['timestamp']} | {r['symbol']} | "
                f"hunter={r['hunter_playbook']}:{r['hunter_score']} | "
                f"expert={r['expert_playbook']}:{r['expert_score']} | "
                f"CHOSEN={r['chosen_source']} {r['chosen_playbook']}:{r['chosen_score']} | {r['reason']}"
            )

        decision_cols={str(x[1]) for x in con.execute("PRAGMA table_info(v9_execution_decisions)").fetchall()}
        action_expr=(
            "decision"
            if "decision" in decision_cols
            else "CASE WHEN execute=1 THEN 'GO' ELSE 'HOLD' END"
        )
        drows=con.execute(f'''
            SELECT timestamp,symbol,playbook,{action_expr} AS action,grade,final_score,
                   risk_multiplier,luna_decision,luna_confidence,reason
            FROM v9_execution_decisions WHERE timestamp>=?
            ORDER BY id DESC LIMIT 12
        ''',(since,)).fetchall()
        print('\nLATEST EXECUTION-FIRST DECISIONS')
        if not drows: print('none yet')
        for r in drows:
            print(
                f"{r['timestamp']} | {r['symbol']} {r['playbook']} {r['action']} "
                f"{r['grade']} score={r['final_score']} risk_x={r['risk_multiplier']} "
                f"Luna={r['luna_decision']}({r['luna_confidence']}) | {r['reason']}"
            )

        prows=con.execute('''
            SELECT id,symbol,strategy_id,status,reward_r,pnl,opened_at,closed_at,context_json
            FROM demo_positions WHERE status='closed' AND closed_at>=? AND execution_tier LIKE 'v9%'
            ORDER BY id DESC
        ''',(since,)).fetchall()
        print('\nBROKER-DEMO OUTCOMES - LAST 6H')
        if not prows: print('none yet')
        total=0; wins=0; sr=0.0; v10n=0
        for index, r in enumerate(prows):
            if str(r['status'])!='closed' or r['reward_r'] is None: continue
            rr=float(r['reward_r']); total+=1; wins+=1 if rr>0 else 0; sr+=rr
            try: ctx=json.loads(r['context_json'] or '{}')
            except Exception: ctx={}
            mh=ctx.get('micro_hunter') if isinstance(ctx.get('micro_hunter'),dict) else {}
            pb=str(mh.get('playbook') or '?')
            isv10=bool(mh.get('v10_superhuman') or ((mh.get('context') or {}).get('source')=='v10_superhuman_expert_ensemble' if isinstance(mh.get('context'),dict) else False))
            v10n+=1 if isv10 else 0
            if index < 20:
                source = 'V11' if ctx.get('v11') else ('V10' if isv10 else 'hunter')
                print(f"id={r['id']} {r['symbol']} {pb} {source} R={rr:+.3f} pnl={r['pnl']}")
        if total:
            print(f"FULL-WINDOW SUMMARY closed={total} wins={wins} win%={100*wins/total:.1f} sumR={sr:+.3f} avgR={sr/total:+.3f} v10_entries={v10n}; display limited to 20")
            print('Use 47_V11_CANONICAL_STATUS.bat for source-separated old/new policy results.')

        print('\nINTERPRETATION')
        print('V10.1 runs experts in parallel with the legacy hunter. Judge each archetype by real DEMO expectancy, PF and drawdown.')
        print('Do not increase base risk until V10 has enough closed broker evidence.')
    return 0

if __name__=='__main__':
    raise SystemExit(main())
