from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import datetime, timedelta, timezone

import settings


def main() -> int:
    print("=" * 86)
    print("V8.1 EXECUTION QUALITY + LUNA EFFICIENCY STATUS  (DEMO ONLY)")
    print("=" * 86)
    since = (datetime.now(timezone.utc) - timedelta(minutes=60)).isoformat()
    today = datetime.now(timezone.utc).date().isoformat()
    with sqlite3.connect(settings.DATABASE_PATH, timeout=30) as con:
        con.row_factory = sqlite3.Row
        try:
            rows = con.execute(
                "SELECT symbol,playbook,phase,score,strategy_id,execution_tier,triggered_at,updated_at FROM micro_hunter_setups WHERE COALESCE(triggered_at,updated_at)>=? ORDER BY id DESC LIMIT 80",
                (since,),
            ).fetchall()
        except sqlite3.Error:
            rows=[]
        phases=Counter(str(r['phase']) for r in rows)
        print("\nLAST 60 MIN MICRO HUNTER")
        print(" | ".join(f"{k}={v}" for k,v in phases.items()) or "none")
        for r in rows[:8]:
            print(f"  {r['symbol']} {r['playbook']} {r['phase']} score={float(r['score'] or 0):.1f} s={r['strategy_id']} tier={r['execution_tier'] or '?'}")

        try:
            gates=con.execute(
                "SELECT timestamp,symbol,strategy_id,allowed,local_score,threshold,reason,context_json FROM v8_alpha_gate_events WHERE timestamp>=? ORDER BY id DESC LIMIT 50",
                (since,),
            ).fetchall()
        except sqlite3.Error:
            gates=[]
        print("\nLOCAL PRE-LUNA - LAST 60 MIN")
        if not gates:
            print("none")
        else:
            counts=Counter('PASS' if int(r['allowed']) else str(r['reason']) for r in gates)
            for k,v in counts.most_common(): print(f"  {k}: {v}")
            print("LATEST:")
            for r in gates[:8]:
                print(f"  {r['timestamp']} {r['symbol']} s={r['strategy_id']} {'PASS' if int(r['allowed']) else 'HOLD'} score={float(r['local_score']):.1f}/{float(r['threshold']):.1f} | {r['reason']}")

        try:
            reviews=con.execute(
                "SELECT reviewed_at,symbol,strategy_id,decision,confidence,input_tokens,output_tokens,decision_source FROM gpt_candidate_reviews WHERE reviewed_at>=? ORDER BY id DESC LIMIT 30",
                (since,),
            ).fetchall()
        except sqlite3.Error:
            reviews=[]
        print("\nLUNA REVIEWS - LAST 60 MIN")
        if not reviews:
            print("none")
        else:
            rc=Counter(str(r['decision']).upper() for r in reviews)
            print(" | ".join(f"{k}={v}" for k,v in rc.items()))
            tin=sum(int(r['input_tokens'] or 0) for r in reviews); tout=sum(int(r['output_tokens'] or 0) for r in reviews)
            print(f"tokens input={tin} output={tout}")
            for r in reviews[:6]:
                print(f"  {r['reviewed_at']} {r['symbol']} s={r['strategy_id']} {str(r['decision']).upper()} conf={float(r['confidence'] or 0):.2f} in={r['input_tokens']} out={r['output_tokens']}")

        try:
            trades=con.execute(
                "SELECT id,symbol,strategy_id,status,reward_r,pnl,opened_at,closed_at,entry_context_json FROM demo_positions WHERE opened_at>=? ORDER BY id DESC LIMIT 20",
                (today,),
            ).fetchall()
        except sqlite3.Error:
            trades=[]
        native=[]
        for r in trades:
            txt=str(r['entry_context_json'] or '')
            if 'v8_native_alpha' in txt or 'v8_institutional_alpha' in txt or 'micro_hunter' in txt:
                native.append(r)
        print("\nV8 MICRO/ALPHA BROKER-DEMO TRADES - TODAY")
        if not native:
            print("none yet")
        else:
            for r in native[:8]:
                print(f"  id={r['id']} {r['symbol']} s={r['strategy_id']} {r['status']} R={r['reward_r']} pnl={r['pnl']} opened={r['opened_at']}")

        try:
            last=con.execute("SELECT id,symbol,strategy_id,status,reward_r,pnl,opened_at,closed_at FROM demo_positions ORDER BY id DESC LIMIT 1").fetchone()
        except sqlite3.Error:
            last=None
        print("\nLAST DEMO BROKER TRADE")
        if last:
            print(f"id={last['id']} {last['symbol']} s={last['strategy_id']} {last['status']} R={last['reward_r']} pnl={last['pnl']} opened={last['opened_at']} closed={last['closed_at']}")
        else:
            print("none")

    print("\nNOTE: Direction/HTF contradictions are now held locally before Luna. Strong reversal")
    print("playbooks can still reach Luna when reversal evidence and expected-R are strong.")
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
