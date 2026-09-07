from __future__ import annotations

import sqlite3

import settings
import capital_growth


def _refresh_equity() -> float | None:
    try:
        import MetaTrader5 as mt5
        if not mt5.initialize():
            return None
        try:
            account = mt5.account_info()
            return float(account.equity) if account is not None else None
        finally:
            mt5.shutdown()
    except Exception:
        return None


def main() -> int:
    print("=== V6.8 CAPITAL GROWTH STATUS ===")
    con = sqlite3.connect(settings.DATABASE_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        equity = _refresh_equity()
        state = capital_growth.update_state(con, equity) if equity is not None else capital_growth.snapshot(con)
        con.commit()
    finally:
        con.close()

    if state.get("phase") == "waiting_for_equity":
        print("Waiting for first MT5 equity snapshot for today.")
        return 0

    print(f"Day/timezone: {state.get('day')} / {state.get('timezone')}")
    print(f"Start equity: {float(state.get('start_equity') or 0):.2f}")
    print(f"Current equity: {float(state.get('equity') or 0):.2f}")
    print(f"Peak equity: {float(state.get('peak_equity') or 0):.2f}")
    print(f"Today growth: {float(state.get('growth_pct') or 0)*100:+.2f}%")
    print(f"Peak growth: {float(state.get('peak_growth_pct') or 0)*100:+.2f}%")
    print(
        f"Objective: {float(state.get('target_pct') or 0)*100:.1f}% | "
        f"stretch: {float(state.get('stretch_pct') or 0)*100:.1f}%"
    )
    print(
        f"Phase: {state.get('phase')} | risk multiplier={float(state.get('risk_multiplier') or 0):.2f}x | "
        f"new entries={'YES' if state.get('allow_new_entries', True) else 'PAUSED'}"
    )
    print(f"Reason: {state.get('reason')}")
    print("Rule: target is never chased by increasing risk; position sizing compounds from current equity.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
