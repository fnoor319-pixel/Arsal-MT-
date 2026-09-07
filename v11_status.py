"""Read-only V11 status. Full-window totals, not the last 20 displayed rows."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Status is usable without installing MT5 on a read-only review machine.
try:
    import MetaTrader5
except ImportError:
    import types
    fake = types.ModuleType("MetaTrader5")
    fake.TIMEFRAME_M1 = 1
    sys.modules["MetaTrader5"] = fake

import settings
from scalp_policy import reward_summary


def collect(con: sqlite3.Connection, hours: float) -> dict:
    con.row_factory = sqlite3.Row
    since = datetime.now(timezone.utc)-timedelta(hours=hours)
    since_msc = int(since.timestamp()*1000)
    tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    broker_rows = con.execute("""SELECT symbol,reward_r,pnl,execution_tier FROM demo_positions
        WHERE status='closed' AND closed_at>=? ORDER BY closed_at,id""", (since.isoformat(),)).fetchall()
    summary = {}
    for symbol in sorted({r["symbol"] for r in broker_rows}):
        for group in ("legacy", "V11"):
            rows = [r for r in broker_rows if r["symbol"] == symbol
                    and (str(r["execution_tier"]) == "v9_v11_canonical") == (group == "V11")]
            if rows:
                summary[f"{symbol}:{group}:broker"] = {**reward_summary([float(r["reward_r"] or 0) for r in rows]),
                                                      "net_pnl": sum(float(r["pnl"] or 0) for r in rows)}
    virtual = {}
    events, event_details, intents, state, policies = [], [], [], {}, []
    funnel: dict[str, int] = {}
    luna: dict[str, int] = {}
    if "v11_outcomes" in tables:
        rows = con.execute("SELECT * FROM v11_outcomes WHERE closed_msc>=? AND source='shadow' AND quality='complete' ORDER BY closed_msc,id", (since_msc,)).fetchall()
        for cell in sorted({(r["symbol"], r["variant"]) for r in rows}):
            virtual[":".join(cell)] = reward_summary([float(r["reward_r"]) for r in rows if (r["symbol"], r["variant"]) == cell])
        events = [dict(r) for r in con.execute("""SELECT symbol,stage,reason,COUNT(*) AS events
            FROM v11_decision_events WHERE time_msc>=? GROUP BY symbol,stage,reason ORDER BY events DESC LIMIT 20""", (since_msc,))]
        event_details = [dict(r) for r in con.execute("""SELECT d.symbol,d.stage,d.reason,d.time_msc,d.details_json
            FROM v11_decision_events d JOIN (
                SELECT MAX(id) AS id FROM v11_decision_events WHERE time_msc>=?
                GROUP BY symbol,stage,reason
            ) latest ON latest.id=d.id
            ORDER BY d.time_msc DESC LIMIT 20""", (since_msc,))]
        intents = [dict(r) for r in con.execute("SELECT event_key,symbol,state,created_msc FROM v11_order_intents WHERE state IN ('reserved','sent','unknown')")]
        for r in con.execute("""SELECT name,updated_msc,value_json FROM v11_runtime_state
            WHERE name IN ('heartbeat','risk') OR name LIKE 'symbol_guard:%' OR name LIKE 'tick_health:%'"""):
            state[r["name"]] = {"updated_msc": r["updated_msc"], "value": json.loads(r["value_json"])}
        policies = [dict(r) for r in con.execute("SELECT symbol,playbook,side,active_variant,updated_msc FROM v11_policy_state")]
        funnel = {
            "candidates": int(con.execute("SELECT COUNT(*) FROM v11_candidates WHERE created_msc>=?", (since_msc,)).fetchone()[0]),
            "order_intents": int(con.execute("SELECT COUNT(*) FROM v11_order_intents WHERE created_msc>=?", (since_msc,)).fetchone()[0]),
            "shadow_outcomes": int(con.execute("SELECT COUNT(*) FROM v11_outcomes WHERE source='shadow' AND quality='complete' AND closed_msc>=?", (since_msc,)).fetchone()[0]),
            "open_shadow": int(con.execute("SELECT COUNT(*) FROM v11_virtual_positions WHERE status='open'").fetchone()[0]),
            "incomplete_shadow": int(con.execute("SELECT COUNT(*) FROM v11_virtual_positions WHERE status='incomplete' AND closed_msc>=?", (since_msc,)).fetchone()[0]),
        }
        if "v11_luna_reviews" in tables:
            luna = {str(r[0]): int(r[1]) for r in con.execute(
                "SELECT state,COUNT(*) FROM v11_luna_reviews WHERE created_msc>=? GROUP BY state", (since_msc,))}
    return dict(hours=hours, since_utc=since.isoformat(), broker=summary,
                shadow_estimates_not_broker_results=virtual, recent_reasons=events,
                recent_reason_details=event_details, unresolved_order_intents=intents,
                state=state, promoted_policy_state=policies, funnel=funnel, luna=luna)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=float, default=6)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    path = Path(settings.DATABASE_PATH).resolve()
    with sqlite3.connect(path.as_uri()+"?mode=ro", uri=True, timeout=5) as con:
        result = collect(con, max(.1, args.hours))
    if args.json:
        print(json.dumps(result, indent=2))
        return 0
    print("V11.1 CANONICAL SCALPER - DEMO ONLY - READ-ONLY STATUS")
    print(f"Full closed-at window: last {result['hours']:g}h, no LIMIT-20 truncation")
    print(f"Poll target={settings.V11_POLL_SECONDS}s | loss/day={settings.MAX_DAILY_LOSS_PCT*100:.2f}% | peak DD={settings.MAX_DRAWDOWN_PCT*100:.2f}%")
    print(f"Cold probes={settings.V11_PROBES_PER_SYMBOL_DAY}/symbol/day, {settings.V11_PROBES_TOTAL_DAY}/total/day | actual planned-risk ceiling={settings.V11_PROBE_HARD_RISK_PCT*100:.3f}%")
    print(f"Freeze={settings.PORTFOLIO_FREEZE_AFTER_LOSSES} consecutive losses / {settings.PORTFOLIO_FREEZE_SECONDS}s | observation continues")
    print("\nACTUAL BROKER NET OUTCOMES (fees included; old/new policy separated)")
    for name, row in result["broker"].items():
        pf = "no losses" if row["profit_factor"] is None else f"{row['profit_factor']:.2f}"
        print(f"{name}: n={row['n']} win={100*(row['win_rate'] or 0):.1f}% mean={row['mean_r']:+.3f}R PF={pf} net PnL={row['net_pnl']:+.2f} avgWin={row['avg_win_r']:.3f}R avgLoss={row['avg_loss_r']:.3f}R")
    if not result["broker"]:
        print("No closed trades in this time window.")
    print("\nPROSPECTIVE SHADOW ESTIMATES (not real trades; never combine paired variants)")
    for name, row in result["shadow_estimates_not_broker_results"].items():
        print(f"{name}: n={row['n']} mean={row['mean_r']:+.3f}R win={100*(row['win_rate'] or 0):.1f}%")
    if not result["shadow_estimates_not_broker_results"]:
        print("No complete V11 tick-path outcomes yet. No historical candles relabeled as V11 wins.")
    print("\nCONVERSION FUNNEL")
    print(json.dumps(result["funnel"], separators=(",", ":")))
    print(f"Luna pre-trade states: {json.dumps(result['luna'], separators=(',', ':'))}")
    print("\nCURRENT STATE")
    for name, state in result["state"].items():
        age = (datetime.now(timezone.utc).timestamp()*1000-state["updated_msc"])/1000
        print(f"{name} age={age:.0f}s: {json.dumps(state['value'], separators=(',', ':'))}")
    print("\nRECENT REASONS (throttled event counts, not every skipped poll)")
    for row in result["recent_reasons"]:
        print(f"{row['symbol']} {row['stage']}: {row['reason']} ({row['events']})")
    if result["recent_reason_details"]:
        print("\nLATEST REASON METRICS")
        for row in result["recent_reason_details"][:12]:
            try:
                details = json.dumps(json.loads(row["details_json"]), separators=(",", ":"))[:280]
            except (TypeError, ValueError):
                details = "{}"
            age = max(0.0, datetime.now(timezone.utc).timestamp()-row["time_msc"]/1000)
            print(f"{row['symbol']} {row['stage']}:{row['reason']} age={age:.0f}s {details}")
    if result["unresolved_order_intents"]:
        print("\nACTION REQUIRED: unresolved order acknowledgements. Inspect MT5 positions/orders/history. Do NOT reset/delete the journal to force retries.")
        for row in result["unresolved_order_intents"]:
            print(f"{row['symbol']} {row['state']} event={row['event_key']}")
    print("\nNo profit/win-rate guarantee. A fast polling target is not broker/network fill latency.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
