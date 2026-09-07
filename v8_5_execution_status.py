from __future__ import annotations
import json, sqlite3
from datetime import datetime, timedelta, timezone
import settings, institutional_alpha

def main() -> int:
    print("=" * 96)
    print("V8.5 EXECUTION-CONVERSION STATUS - DEMO ONLY")
    print("=" * 96)
    print(
        f"Luna paid cap/day={int(getattr(settings,'SPARTAN_GPT_MAX_PRETRADE_API_CALLS_PER_DAY',0))} | "
        f"zero-token probes/day={int(getattr(settings,'V8_BUDGET_PROBE_MAX_PER_DAY',0))} "
        f"per-symbol={int(getattr(settings,'V8_BUDGET_PROBE_MAX_PER_SYMBOL_PER_DAY',0))} "
        f"risk_x={float(getattr(settings,'V8_BUDGET_PROBE_RISK_MULTIPLIER',0)):.2f}"
    )
    print("Playbook-aware Spartan:", bool(getattr(settings,"V8_PLAYBOOK_AWARE_SPARTAN_ENABLED",False)))
    print(
        f"continuation ADX>={float(getattr(settings,'V8_MICRO_SPARTAN_CONT_ADX_MIN',0)):.0f} "
        f"ratio>={float(getattr(settings,'V8_MICRO_SPARTAN_CONT_MIN_CONFLUENCE_RATIO',0)):.2f} "
        f"core>={int(getattr(settings,'V8_MICRO_SPARTAN_CONT_MIN_CORE_ALIGNMENT',0))}"
    )
    print(
        f"reversal ADX_required={bool(getattr(settings,'V8_MICRO_SPARTAN_REV_REQUIRE_ADX',False))} "
        f"ratio>={float(getattr(settings,'V8_MICRO_SPARTAN_REV_MIN_CONFLUENCE_RATIO',0)):.2f}"
    )
    print(
        f"squeeze ADX_required={bool(getattr(settings,'V8_MICRO_SPARTAN_SQUEEZE_REQUIRE_ADX',False))} "
        f"ratio>={float(getattr(settings,'V8_MICRO_SPARTAN_SQUEEZE_MIN_CONFLUENCE_RATIO',0)):.2f}"
    )

    since = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    with sqlite3.connect(settings.DATABASE_PATH, timeout=30) as con:
        con.row_factory = sqlite3.Row
        institutional_alpha.ensure_tables(con)
        print("\nZERO-TOKEN BUDGET PROBES - LAST 2H")
        rows = con.execute(
            """SELECT timestamp,symbol,strategy_id,playbook,allowed,risk_multiplier,
                      local_score,micro_score,expected_r,probability,break_even_probability,
                      source,reason
               FROM v8_budget_probe_events WHERE timestamp>=?
               ORDER BY id DESC LIMIT 20""", (since,)
        ).fetchall()
        if not rows: print("none yet")
        for r in rows:
            print(
                f"{r['timestamp']} | {r['symbol']} s={r['strategy_id']} {r['playbook']} | "
                f"{'PROBE' if int(r['allowed']) else 'HOLD'} | "
                f"local={float(r['local_score'] or 0):.1f} micro={float(r['micro_score'] or 0):.1f} "
                f"ER={float(r['expected_r'] or 0):+.2f}R "
                f"P={float(r['probability'] or 0):.2f} BE={float(r['break_even_probability'] or 0):.2f} "
                f"src={r['source']} | {r['reason']}"
            )

        print("\nRECENT SPARTAN VETOS - LAST 2H")
        drows = con.execute(
            """SELECT timestamp,symbol,strategy_id,reason FROM decision_logs
               WHERE mode='demo' AND timestamp>=? AND reason LIKE 'Spartan-Pro veto:%'
               ORDER BY id DESC LIMIT 12""", (since,)
        ).fetchall()
        if not drows: print("none")
        for r in drows:
            print(f"{r['timestamp']} | {r['symbol']} s={r['strategy_id']} | {r['reason']}")

        print("\nRECENT BROKER-DEMO POSITIONS - LAST 2H")
        prows = con.execute(
            """SELECT id,symbol,strategy_id,execution_tier,status,reward_r,pnl,opened_at,closed_at,context_json
               FROM demo_positions WHERE opened_at>=? ORDER BY id DESC LIMIT 20""", (since,)
        ).fetchall()
        found = False
        for r in prows:
            try: ctx = json.loads(r["context_json"] or "{}")
            except Exception: ctx = {}
            if str(r["execution_tier"] or "") == "v8_native_alpha" or ctx.get("v8_budget_quant_probe") or ctx.get("v8_luna_disagreement_probe"):
                found = True
                tag = "BUDGET_PROBE" if ctx.get("v8_budget_quant_probe") else "LUNA_PROBE" if ctx.get("v8_luna_disagreement_probe") else "NATIVE"
                print(
                    f"id={r['id']} {r['symbol']} s={r['strategy_id']} {tag} "
                    f"status={r['status']} R={r['reward_r']} pnl={r['pnl']} opened={r['opened_at']}"
                )
        if not found: print("none yet")
    print("\nNOTE: API budget exhaustion now stops API spending, not the whole DEMO learning engine.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
