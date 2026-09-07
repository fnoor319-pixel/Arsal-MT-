from __future__ import annotations
import trading_machine as tm


def main() -> int:
    tm.init_database()
    with tm.db_connect() as con:
        marker = tm.state_get(con, "shadow_fill_integrity_repair") or "not-run"
        repaired = tm.state_get(con, "shadow_fill_integrity_repaired_positions") or "0"
        events = tm.state_get(con, "shadow_fill_integrity_repaired_events") or "0"
        extremes = con.execute(
            """
            SELECT COUNT(*) AS n FROM shadow_positions
            WHERE status='closed' AND close_reason='stop'
              AND reward_r IS NOT NULL AND reward_r < -1.000001
            """
        ).fetchone()["n"]
        take_overs = con.execute(
            """
            SELECT COUNT(*) AS n FROM shadow_positions
            WHERE status='closed' AND close_reason='take'
              AND reward_r IS NOT NULL
              AND ABS(reward_r - ((take_profit-entry_price)*side / NULLIF(ABS(entry_price-stop_loss),0))) > 0.000001
            """
        ).fetchone()["n"]
    print("=" * 72)
    print("V6.9.1 SHADOW FILL INTEGRITY STATUS")
    print("=" * 72)
    print(f"repair marker: {marker}")
    print(f"historical shadow positions normalized: {repaired}")
    print(f"shadow reward events synchronized: {events}")
    print(f"remaining stop overshoot artifacts: {int(extremes)}")
    print(f"remaining take overshoot artifacts: {int(take_overs)}")
    print("Real DEMO broker outcomes are not modified by this repair.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
