from __future__ import annotations

import sqlite3
from pathlib import Path

import settings
import scalp_lab


def main() -> int:
    db = Path(settings.DATABASE_PATH)
    if not db.exists():
        print(f"Database not found: {db}")
        return 2
    with sqlite3.connect(db, timeout=30) as con:
        con.row_factory = sqlite3.Row
        scalp_lab.ensure_tables(con)
        print("=" * 78)
        print("V6.9 SCALP LAB STATUS - EDGE RECOVERY / CAPITAL-GROWTH EVIDENCE")
        print("=" * 78)
        for symbol in settings.SYMBOLS:
            ranking = scalp_lab.champion_challenger(con, symbol)
            print(f"\n{symbol} CHAMPION / CHALLENGER")
            if not ranking["ranked"]:
                print("  no historically-qualified strategies yet")
            for item in ranking["ranked"][:5]:
                print(
                    f"  {item['role']:<10} id={item['id']:<6} {item['family']:<18} "
                    f"score={item['score']:>7.2f} PF={item['pf']:.2f} OOS={item['oos_pf']:.2f} "
                    f"shadow_n={item['live_n']} mean={item['live_mean_r']:+.3f}R" + (" QUARANTINE" if item.get('quarantined') else "")
                )
            if ranking["ranked"]:
                best_id = int(ranking["ranked"][0]["id"])
                stability = scalp_lab.parameter_stability_neighbors(con, best_id)
                print(f"  parameter-neighbour stability: {stability.get('stability')} across {stability.get('neighbors')} neighbours")

        print("\nSYMBOL-LEVEL RECENT DEMO EDGE")
        symbol_edges = scalp_lab.demo_monte_carlo_by_symbol(
            con, int(getattr(settings, "EDGE_SYMBOL_WINDOW", 60))
        )
        for symbol in settings.SYMBOLS:
            edge = symbol_edges.get(symbol, {"n": 0})
            smc = edge.get("monte_carlo") if isinstance(edge.get("monte_carlo"), dict) else {}
            if not smc:
                print(f"  {symbol}: warming up (n={edge.get('n',0)})")
            else:
                print(
                    f"  {symbol}: n={edge['n']} mean={edge['mean_r']:+.3f}R PF={edge['profit_factor']:.2f} "
                    f"MCmedian={smc['p50_final_r']:+.2f}R P(loss)={smc['probability_negative_final_r']:.1%}"
                )

        mc = scalp_lab.demo_monte_carlo(con, int(getattr(settings, "LAB_MONTE_CARLO_WINDOW", 120)))
        print("\nMONTE CARLO - RECENT DEMO REWARDS")
        if not mc.get("samples"):
            print(f"  unavailable: {mc.get('reason')} (trades={mc.get('trades',0)})")
        else:
            print(
                f"  trades={mc['trades']} simulations={mc['samples']} | "
                f"finalR p05={mc['p05_final_r']:+.2f} p50={mc['p50_final_r']:+.2f} p95={mc['p95_final_r']:+.2f} | "
                f"DD95={mc['p95_max_drawdown_r']:.2f}R | loss-streak95={mc['p95_longest_loss_streak']} | "
                f"P(final<0)={mc['probability_negative_final_r']:.1%}"
            )

        eq = scalp_lab.execution_quality_summary(con, 150)
        print("\nEXECUTION QUALITY")
        if not eq["samples"]:
            print("  no V6.7 execution-quality samples yet")
        else:
            for symbol, item in eq["by_symbol"].items():
                print(
                    f"  {symbol}: n={item['samples']} slip={item['avg_slippage_atr']:+.4f} ATR "
                    f"spread={item['avg_spread_atr']:.4f} ATR latency={item['avg_latency_ms']:.0f}ms"
                )

        luna = scalp_lab.luna_value_add(con)
        print("\nLUNA VALUE-ADD AUDIT")
        if not luna.get("available"):
            print("  GPT decision-memory tables unavailable")
        else:
            c = luna["confirm"]; v = luna["veto_shadow"]
            print(f"  confirmed outcomes: n={c['n']} total={c['total_r']:+.2f}R mean={c['mean_r']:+.3f}R")
            print(
                f"  veto counterfactuals: n={v['n']} would-have={v['counterfactual_total_r']:+.2f}R "
                f"saved_losses={v['saved_losses']} missed_winners={v['missed_winners']}"
            )
            print(f"  enough samples for judgement: {luna['enough_for_judgement']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
