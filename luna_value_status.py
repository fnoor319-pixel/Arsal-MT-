from __future__ import annotations
import sqlite3
import settings
import scalp_lab
import spartan_decision_memory as memory


def main() -> int:
    with sqlite3.connect(settings.DATABASE_PATH, timeout=30) as con:
        con.row_factory = sqlite3.Row
        memory.ensure_tables(con)
        value = scalp_lab.luna_value_add(con)
        usage = memory.usage_summary(con)
        print("=" * 72)
        print("V6.7 LUNA VALUE + BUDGET STATUS")
        print("=" * 72)
        b = usage.get("budget") or {}
        print(f"Model: {getattr(settings, "SPARTAN_LLM_MODEL", settings.GPT_MODEL)}")
        print(
            f"Bot budget estimate: ${float(b.get('estimated_spent_usd') or 0):.4f} / "
            f"${float(b.get('budget_usd') or 0):.2f} | remaining≈${float(b.get('estimated_remaining_usd') or 0):.4f}"
        )
        print(f"Today calls={usage['today']['calls']} tokens={usage['today']['total_tokens']}")
        print(f"Month calls={usage['month']['calls']} tokens={usage['month']['total_tokens']} cache_reuses={usage['cache_reuses_month']}")
        if value.get("available"):
            c=value['confirm']; v=value['veto_shadow']
            print(f"Luna confirmed: n={c['n']} total={c['total_r']:+.2f}R mean={c['mean_r']:+.3f}R")
            print(f"Luna HOLD counterfactual: n={v['n']} would-have={v['counterfactual_total_r']:+.2f}R saved_losses={v['saved_losses']} missed_winners={v['missed_winners']}")
            print(f"Enough outcomes for judgement: {value['enough_for_judgement']}")
        return 0

if __name__ == '__main__':
    raise SystemExit(main())
