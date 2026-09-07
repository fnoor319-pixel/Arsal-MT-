from __future__ import annotations

import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB = ROOT / "trading_machine.db"
SYMBOLS = ("XAUUSDm", "USOILm", "BTCUSDm")

TRIAL_QUERY = """
SELECT COUNT(*) FROM (
    SELECT s.id
    FROM strategies s
    JOIN backtests b ON b.strategy_id=s.id
    JOIN strategy_diagnostics d ON d.strategy_id=s.id
    WHERE s.symbol=?
      AND (
          s.status='historical_validated'
          OR (
              s.status='needs_revalidation'
              AND LOWER(COALESCE(d.validation_reason, '')) LIKE 'validated%'
          )
      )
    GROUP BY s.id
    HAVING MAX(b.profit_factor)>=1.10
       AND d.oos_profit_factor>=1.00
       AND d.oos_trades>=20
       AND d.stability_score>=0
       AND MAX(b.score)>=0
)
"""


def main() -> int:
    if not DB.exists():
        print(f"ERROR: Database not found: {DB}")
        return 1
    connection = sqlite3.connect(DB)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        print(f"Database: {DB}")
        print(f"Integrity: {integrity}")
        print("Statuses:")
        for status, count in connection.execute(
            "SELECT status, COUNT(*) FROM strategies GROUP BY status ORDER BY status"
        ):
            print(f"  {status}: {count}")
        print("\nExecution pools:")
        for symbol in SYMBOLS:
            approved = connection.execute(
                "SELECT COUNT(*) FROM strategies WHERE symbol=? AND status='shadow_approved'",
                (symbol,),
            ).fetchone()[0]
            trial = connection.execute(TRIAL_QUERY, (symbol,)).fetchone()[0]
            print(f"  {symbol}: shadow_approved={approved} | demo_trial_eligible={trial}")
        print("\nPriority: shadow_approved first; demo_trial only when approved pool is empty.")
        return 0 if integrity == "ok" else 2
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
