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
        print("=" * 78)
        print("V6.9 EDGE RECOVERY STATUS - ACTUAL DEMO EVIDENCE")
        print("=" * 78)
        overall = scalp_lab.demo_edge_profile(
            con, window=int(getattr(settings, "EDGE_OVERALL_WINDOW", 120)), monte_carlo=True
        )
        omc = overall.get("monte_carlo") if isinstance(overall.get("monte_carlo"), dict) else {}
        print(
            f"OVERALL n={overall.get('n',0)} mean={overall.get('mean_r',0):+.3f}R "
            f"PF={overall.get('profit_factor',0):.2f}"
        )
        if omc:
            print(
                f"        MC median={omc.get('p50_final_r',0):+.2f}R "
                f"P(final<0)={omc.get('probability_negative_final_r',0):.1%} "
                f"DD95={omc.get('p95_max_drawdown_r',0):.2f}R"
            )
        print("\nBY SYMBOL")
        for symbol in settings.SYMBOLS:
            edge = scalp_lab.demo_edge_profile(
                con, symbol=symbol, window=int(getattr(settings, "EDGE_SYMBOL_WINDOW", 60)), monte_carlo=True
            )
            mc = edge.get("monte_carlo") if isinstance(edge.get("monte_carlo"), dict) else {}
            print(
                f"  {symbol:<8} n={edge.get('n',0):<3} mean={edge.get('mean_r',0):+.3f}R "
                f"PF={edge.get('profit_factor',0):.2f}" +
                (f" MCmedian={mc.get('p50_final_r',0):+.2f}R P(loss)={mc.get('probability_negative_final_r',0):.1%}" if mc else " warming-up")
            )
        print("\nWHAT V6.9 DOES")
        print("  - repeatedly losing strategy: quarantine before Luna/API/order")
        print("  - weak family/symbol/portfolio: reduce risk, do not martingale")
        print("  - strong recent shadow evidence: ranks much higher")
        print("  - catastrophic shadow loser: skipped/demoted early")
        print("  - exceptional scalp shadow evidence: strict fast-track approval")
        print("  - Luna budget behavior is unchanged; edge filtering happens first")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
