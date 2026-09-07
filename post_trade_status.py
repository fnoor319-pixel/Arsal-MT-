from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import settings


def main() -> int:
    db = Path(settings.DATABASE_PATH)
    print("=== SPARTAN GPT POST-TRADE STATUS ===")
    print(f"Enabled: {bool(getattr(settings, 'SPARTAN_POST_TRADE_REVIEW_ENABLED', False))}")
    print(f"Model: {getattr(settings, 'SPARTAN_POST_TRADE_MODEL', getattr(settings, 'GPT_MODEL', ''))}")
    if not db.exists():
        print("Database not found.")
        return 2
    with sqlite3.connect(db) as con:
        exists = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='gpt_post_trade_reviews'").fetchone()
        if not exists:
            print("No post-trade review table yet. It is created automatically after the first closed DEMO trade.")
            return 0
        rows = con.execute(
            """
            SELECT demo_position_id, symbol, reviewed_at, status, classification, confidence, summary,
                   research_hypotheses_json
            FROM gpt_post_trade_reviews
            ORDER BY reviewed_at DESC LIMIT 5
            """
        ).fetchall()
    if not rows:
        print("No closed-trade GPT reviews yet.")
        return 0
    for row in rows:
        hypotheses = []
        try:
            hypotheses = json.loads(row[7] or "[]")
        except Exception:
            pass
        print(
            f"id={row[0]} {row[1]} | {row[2]} | {row[3]} | {row[4]} "
            f"conf={float(row[5] or 0):.2f} | {row[6]}"
        )
        for item in hypotheses[:3]:
            print(f"  research: {item}")
    latest = Path(settings.REPORTS_DIR) / "post_trade" / "latest.json"
    if latest.exists():
        print(f"Latest JSON: {latest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
