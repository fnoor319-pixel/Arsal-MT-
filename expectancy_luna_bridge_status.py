from __future__ import annotations
import json, sqlite3
from types import SimpleNamespace
import settings
import scalp_ai_policy as policy


def main():
    print('='*78)
    print('V6.9.4 EXPECTANCY -> LUNA BRIDGE STATUS')
    print('='*78)
    print(f"enabled={getattr(settings,'SPARTAN_GPT_BORDERLINE_REVIEW_ENABLED',True)} "
          f"abs_floor={getattr(settings,'SPARTAN_GPT_BORDERLINE_MIN_PROBABILITY',0.30):.2f} "
          f"max_gap={getattr(settings,'SPARTAN_GPT_BORDERLINE_MAX_GAP',0.08):.2f} "
          f"BE_margin={getattr(settings,'SPARTAN_GPT_BORDERLINE_BREAK_EVEN_MARGIN',0.05):.2f} "
          f"min_ER={getattr(settings,'SPARTAN_GPT_BORDERLINE_MIN_EXPECTED_R',0.15):.2f}R")
    print(f"Luna rescue cap/day={getattr(settings,'SPARTAN_GPT_BORDERLINE_MAX_TRADES_PER_DAY',2)} "
          f"risk_mult={getattr(settings,'SPARTAN_GPT_BORDERLINE_RISK_MULTIPLIER',0.30):.2f} "
          f"min_Luna_conf={getattr(settings,'SPARTAN_GPT_BORDERLINE_MIN_CONFIDENCE',0.82):.2f}")
    with sqlite3.connect(settings.DATABASE_PATH, timeout=30) as con:
        con.row_factory=sqlite3.Row
        rows=con.execute('''
            SELECT timestamp,symbol,strategy_id,decision,reason,probability,threshold,
                   bayes_loss_probability,details_json
            FROM superlearner_decisions ORDER BY id DESC LIMIT 12
        ''').fetchall()
        print('\nLATEST SUPERLEARNER SOFT-REJECT AUDIT')
        found=0
        for r in rows:
            if str(r['decision']).upper() != 'REJECT':
                continue
            d={}
            try: d=json.loads(r['details_json'] or '{}')
            except Exception: pass
            market=(d.get('market') or {}) if isinstance(d,dict) else {}
            obj=SimpleNamespace(
                approved=False, probability_active=True,
                probability=float(r['probability'] or 0), threshold=float(r['threshold'] or 1),
                reason=str(r['reason'] or ''), market=market,
            )
            ok, info=policy.superlearner_borderline_eligible(obj)
            if 'probability' not in obj.reason.lower() and 'expected edge' not in obj.reason.lower():
                continue
            found += 1
            print(f"{r['timestamp']} {r['symbol']} s={r['strategy_id']} "
                  f"P={obj.probability:.3f}/{obj.threshold:.3f} "
                  f"ER={float(market.get('expected_r') or 0):+.3f}R "
                  f"BE={float(market.get('break_even_probability') or 0):.3f} "
                  f"dynamic_floor={float(info.get('min_probability') or 0):.3f} "
                  f"=> {'LUNA-ELIGIBLE' if ok else 'HOLD-BEFORE-LUNA'}")
        if not found: print('no recent soft rejects found')
    return 0

if __name__=='__main__':
    raise SystemExit(main())
