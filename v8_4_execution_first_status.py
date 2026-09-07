from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import settings
import institutional_alpha


def _columns(con: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(r[1]) for r in con.execute(f"PRAGMA table_info({table})").fetchall()}
    except sqlite3.Error:
        return set()


def _json(value):
    try:
        return json.loads(value or "{}")
    except Exception:
        return {}


def main() -> int:
    print("=" * 94)
    print("V8.4.1 EXECUTION-FIRST DEMO EVIDENCE STATUS")
    print("=" * 94)
    print(f"native-alpha primary for hunter: {bool(getattr(settings,'V8_NATIVE_ALPHA_PRIMARY_FOR_HUNTER',False))}")
    print(
        f"Luna cap/day={int(getattr(settings,'SPARTAN_GPT_MAX_PRETRADE_API_CALLS_PER_DAY',0))} | "
        f"evidence probes/day={int(getattr(settings,'V8_LUNA_DISAGREEMENT_MAX_PER_DAY',0))} | "
        f"per-symbol={int(getattr(settings,'V8_LUNA_DISAGREEMENT_MAX_PER_SYMBOL_PER_DAY',0))}"
    )
    print(
        f"probe ER floor=+{float(getattr(settings,'V8_EVIDENCE_PROBE_MIN_EXPECTED_R',0.20)):.2f}R | "
        f"prob edge over break-even={float(getattr(settings,'V8_EVIDENCE_PROBE_MIN_PROB_EDGE',0.03)):.2f} | "
        f"probe risk_x={float(getattr(settings,'V8_LUNA_DISAGREEMENT_RISK_MULTIPLIER',0.10)):.2f}"
    )

    with sqlite3.connect(settings.DATABASE_PATH, timeout=30) as con:
        con.row_factory = sqlite3.Row
        institutional_alpha.ensure_tables(con)
        since = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()

        rows = con.execute(
            """
            SELECT timestamp,symbol,strategy_id,playbook,allowed,risk_multiplier,
                   local_score,expected_r,probability,luna_confidence,reason,context_json
            FROM v8_luna_disagreement_events
            WHERE timestamp>=?
            ORDER BY id DESC LIMIT 20
            """,
            (since,),
        ).fetchall()

        print("\nQUANT vs LUNA EVIDENCE DECISIONS - LAST 2H")
        if not rows:
            print("none yet")
        else:
            for r in rows:
                ctx = _json(r["context_json"])
                be = ctx.get("break_even_probability")
                edge = ctx.get("probability_edge")
                legacy = be is None
                be_txt = "n/a" if legacy else f"{float(be):.2f}"
                edge_txt = "n/a" if edge is None else f"{float(edge):+.2f}"
                tag = "LEGACY/PRE-V8.4" if legacy else ("PROBE" if int(r["allowed"]) else "HOLD")
                print(
                    f"{r['timestamp']} | {r['symbol']} s={r['strategy_id']} {r['playbook']} | "
                    f"{tag} | local={float(r['local_score'] or 0):.1f} "
                    f"ER={float(r['expected_r'] or 0):+.2f}R "
                    f"P={float(r['probability'] or 0):.2f} BE={be_txt} edge={edge_txt} | "
                    f"{r['reason']}"
                )

        # demo_positions uses context_json in this database lineage.
        cols = _columns(con, "demo_positions")
        context_col = "context_json" if "context_json" in cols else None
        tier_col = "execution_tier" if "execution_tier" in cols else None

        select_parts = [
            "id", "symbol", "strategy_id", "status", "reward_r", "pnl",
            "opened_at", "closed_at"
        ]
        if tier_col:
            select_parts.append("execution_tier")
        if context_col:
            select_parts.append("context_json")

        positions = con.execute(
            f"""
            SELECT {",".join(select_parts)}
            FROM demo_positions
            WHERE opened_at>=?
            ORDER BY id DESC LIMIT 30
            """,
            (since,),
        ).fetchall()

        print("\nACTUAL BROKER-DEMO POSITIONS - LAST 2H")
        found = False
        for r in positions:
            keys = set(r.keys())
            ctx = _json(r["context_json"]) if "context_json" in keys else {}
            tier = str(r["execution_tier"] or "") if "execution_tier" in keys else ""
            is_probe = bool(ctx.get("v8_luna_disagreement_probe"))
            is_native = tier == "v8_native_alpha"
            if is_probe or is_native:
                found = True
                label = "EVIDENCE_PROBE" if is_probe else "NATIVE_ALPHA"
                print(
                    f"id={r['id']} {r['symbol']} s={r['strategy_id']} tier={tier or '?'} "
                    f"{label} status={r['status']} R={r['reward_r']} pnl={r['pnl']} "
                    f"opened={r['opened_at']} closed={r['closed_at']}"
                )
        if not found:
            print("none yet")

        setups = con.execute(
            """
            SELECT symbol,playbook,phase,score,strategy_id,execution_tier,updated_at
            FROM micro_hunter_setups
            WHERE updated_at>=?
            ORDER BY id DESC LIMIT 15
            """,
            (since,),
        ).fetchall()
        print("\nRECENT MICRO HUNTER")
        if not setups:
            print("none")
        else:
            for r in setups:
                print(
                    f"{r['symbol']} {r['playbook']} {r['phase']} "
                    f"score={float(r['score'] or 0):.1f} "
                    f"s={r['strategy_id']} tier={r['execution_tier'] or '?'}"
                )

        # Show local pre-Luna evidence too.
        gates = con.execute(
            """
            SELECT timestamp,symbol,strategy_id,playbook,allowed,local_score,
                   threshold,expected_r,probability,reason
            FROM v8_alpha_gate_events
            WHERE timestamp>=?
            ORDER BY id DESC LIMIT 12
            """,
            (since,),
        ).fetchall()
        print("\nLOCAL PRE-LUNA - LAST 2H")
        if not gates:
            print("none")
        else:
            for r in gates:
                print(
                    f"{r['timestamp']} | {r['symbol']} s={r['strategy_id']} {r['playbook']} | "
                    f"{'PASS' if int(r['allowed']) else 'HOLD'} "
                    f"{float(r['local_score'] or 0):.1f}/{float(r['threshold'] or 0):.1f} "
                    f"ER={float(r['expected_r'] or 0):+.2f}R "
                    f"P={float(r['probability'] or 0):.2f} | {r['reason']}"
                )

    print("\nNOTE")
    print("This is a STATUS-ONLY hotfix. It changes no trading logic, risk, DB rows, or Luna usage.")
    print("Rows marked LEGACY/PRE-V8.4 were created before the new break-even context was stored.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
