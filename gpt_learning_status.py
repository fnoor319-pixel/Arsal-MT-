from __future__ import annotations

import sqlite3

import settings
import spartan_decision_memory as memory


def main() -> int:
    connection = sqlite3.connect(settings.DATABASE_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        memory.ensure_tables(connection)
        summary = memory.usage_summary(connection)
        print("=" * 72)
        print("SPARTAN GPT LEARNING / TOKEN STATUS")
        print("=" * 72)
        print(f"Model: {getattr(settings, "SPARTAN_LLM_MODEL", settings.GPT_MODEL)}")
        print(f"Pre-trade API-call safety cap/day: {getattr(settings, 'SPARTAN_GPT_MAX_PRETRADE_API_CALLS_PER_DAY', 60)}")
        budget = summary.get("budget") or {}
        if budget:
            print(
                f"Bot GPT budget guard: ${float(budget.get('estimated_spent_usd') or 0):.4f} / "
                f"${float(budget.get('budget_usd') or 0):.2f} used | "
                f"remaining≈${float(budget.get('estimated_remaining_usd') or 0):.4f}"
            )
        today = summary["today"]
        month = summary["month"]
        print(
            f"Today: calls={today['calls']} input_tokens={today['input_tokens']} "
            f"output_tokens={today['output_tokens']} total_tokens={today['total_tokens']}"
        )
        print(
            f"Month: calls={month['calls']} input_tokens={month['input_tokens']} "
            f"output_tokens={month['output_tokens']} total_tokens={month['total_tokens']}"
        )
        print(f"GPT reviews reused from fingerprint cache this month: {summary['cache_reuses_month']}")
        veto = summary["veto_shadow"]
        print(
            f"Veto shadow learning: open={veto['open']} closed={veto['closed']} "
            f"saved_losses={veto['saved_losses']} missed_winners={veto['missed_winners']}"
        )
        rows = connection.execute(
            """
            SELECT symbol, regime, side, decision_type, observations,
                   positive_outcomes, negative_outcomes, neutral_outcomes,
                   reward_mean, quality_ewma, updated_at
            FROM gpt_decision_memory
            ORDER BY observations DESC, symbol, regime
            LIMIT 30
            """
        ).fetchall()
        print("\nGPT decision calibration memory:")
        if not rows:
            print("  No closed confirm/veto outcomes yet. Memory will populate during DEMO operation.")
        for row in rows:
            side = "BUY" if int(row["side"]) == 1 else "SELL"
            print(
                f"  {row['symbol']} {row['regime']} {side} {row['decision_type'].upper()} | "
                f"n={row['observations']} +={row['positive_outcomes']} -={row['negative_outcomes']} "
                f"flat={row['neutral_outcomes']} avgR={float(row['reward_mean'] or 0):.3f} "
                f"qualityEWMA={float(row['quality_ewma'] or 0):.3f}"
            )
        print("\nPolicy: GPT memory is advisory calibration; hard risk/session/broker rules cannot be mutated.")
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
