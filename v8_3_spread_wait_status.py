from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import settings


def main() -> int:
    print("=" * 84)
    print("V8.3 SPREAD-AWARE TRIGGER STATUS - DEMO ONLY")
    print("=" * 84)
    print(
        f"hard spread/ATR max={float(getattr(settings,'MAX_SPREAD_ATR_FRACTION',0.18)):.3f} | "
        f"wait TTL={int(getattr(settings,'V8_SPREAD_WAIT_TTL_SECONDS',35))}s | "
        f"max chase drift={float(getattr(settings,'V8_SPREAD_WAIT_MAX_DRIFT_ATR',0.18)):.2f} ATR"
    )
    since = (datetime.now(timezone.utc) - timedelta(minutes=90)).isoformat()
    with sqlite3.connect(settings.DATABASE_PATH, timeout=30) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            """
            SELECT id,symbol,playbook,side,phase,score,updated_at,context_json,strategy_id,execution_tier
            FROM micro_hunter_setups
            WHERE updated_at>=?
            ORDER BY id DESC LIMIT 80
            """,
            (since,),
        ).fetchall()
        counts = {}
        events = []
        for r in rows:
            counts[r['phase']] = counts.get(r['phase'],0)+1
            try:
                c=json.loads(r['context_json'] or '{}')
            except Exception:
                c={}
            w=c.get('spread_wait') if isinstance(c.get('spread_wait'),dict) else None
            if w:
                events.append((r,w))
        print("\nLAST 90 MIN PHASES")
        if counts:
            print(" | ".join(f"{k}={v}" for k,v in sorted(counts.items())))
        else:
            print("none")
        print("\nSPREAD-WAIT EVENTS")
        if not events:
            print("none yet")
        else:
            for r,w in events[:15]:
                side='BUY' if int(r['side'])==1 else 'SELL'
                started=w.get('started_at','?')
                initial=float(w.get('spread') or 0)
                atr=float(w.get('atr') or 0)
                ratio=initial/atr if atr>0 else 0
                resumed=w.get('resumed_at')
                if resumed:
                    rs=float(w.get('resumed_spread') or 0); ra=float(w.get('resumed_atr') or 0)
                    rratio=rs/ra if ra>0 else 0
                    result=f"RESUMED spread/ATR={rratio:.3f}"
                else:
                    result=str(r['phase'])
                print(
                    f"#{r['id']} {r['symbol']} {side} {r['playbook']} score={float(r['score']):.1f} "
                    f"initial spread/ATR={ratio:.3f} -> {result} | s={r['strategy_id']} tier={r['execution_tier'] or '?'}"
                )
        print("\nNOTE")
        print("V8.3 never widens the hard spread threshold. It waits briefly for a cheaper fill;")
        print("if the move runs away, invalidates, expires, or a new bar arrives, the setup is abandoned.")
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
