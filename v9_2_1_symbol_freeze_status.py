from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import settings


def _parse(value):
    if value is None:
        return None
    try:
        d = datetime.fromisoformat(str(value))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc)
    except Exception:
        return None


def _streak(rows):
    n = 0
    last = None
    for r in rows:
        if last is None:
            last = _parse(r["closed_at"] or r["opened_at"])
        try:
            rr = float(r["reward_r"] or 0.0)
        except Exception:
            rr = 0.0
        if rr < 0:
            n += 1
        else:
            break
    age = None
    if last is not None:
        age = max(0.0, (datetime.now(timezone.utc) - last).total_seconds())
    return n, age


def main() -> int:
    print("="*96)
    print("V9.2.1 SYMBOL-LOCAL LOSS FREEZE STATUS - DEMO ONLY")
    print("="*96)
    freeze_after = int(getattr(settings, "PORTFOLIO_FREEZE_AFTER_LOSSES", 5))
    freeze_seconds = int(getattr(settings, "PORTFOLIO_FREEZE_SECONDS", 900))
    print(f"hard freeze: {freeze_after} consecutive LOSSES PER SYMBOL | {freeze_seconds//60} minutes")
    print("portfolio streak: advisory risk reduction only; it does NOT stop every symbol")
    print("old pre-V9.2.1 losses: learning evidence only for hard-freeze purposes")

    with sqlite3.connect(settings.DATABASE_PATH, timeout=30) as con:
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT value FROM system_state WHERE key='v9_2_1_symbol_loss_epoch'"
        ).fetchone()
        epoch = str(row["value"]) if row else None
        print(f"\nV9.2.1 freeze epoch: {epoch or 'not initialized yet - main bot will create it on first run'}")

        global_rows = con.execute(
            """
            SELECT reward_r,closed_at,opened_at FROM demo_positions
            WHERE status='closed' AND reward_r IS NOT NULL
            ORDER BY COALESCE(closed_at,opened_at) DESC,id DESC LIMIT 20
            """
        ).fetchall()
        gs, ga = _streak(global_rows)
        print(f"portfolio advisory streak now: {gs} | last-close-age={ga if ga is not None else 'n/a'}s")

        print("\nPER-SYMBOL HARD FREEZE")
        for symbol in settings.SYMBOLS:
            if epoch:
                rows = con.execute(
                    """
                    SELECT reward_r,closed_at,opened_at FROM demo_positions
                    WHERE status='closed' AND reward_r IS NOT NULL
                      AND symbol=? AND COALESCE(closed_at,opened_at)>=?
                      AND execution_tier LIKE 'v9%'
                    ORDER BY COALESCE(closed_at,opened_at) DESC,id DESC LIMIT 50
                    """,
                    (symbol, epoch),
                ).fetchall()
            else:
                rows = []
            streak, age = _streak(rows)
            frozen = streak >= freeze_after and age is not None and age < freeze_seconds
            remain = max(0.0, freeze_seconds-age) if frozen and age is not None else 0.0
            print(
                f"{symbol}: streak={streak} | {'FROZEN' if frozen else 'ACTIVE'}"
                + (f" | remaining={remain:.0f}s" if frozen else "")
            )

        print("\nACCOUNT HARD SAFETY")
        today = datetime.now(timezone.utc).date().isoformat()
        key = f"risk_day:{today}"
        r = con.execute("SELECT value FROM system_state WHERE key=?", (key,)).fetchone()
        start = float(r["value"]) if r else None
        p = con.execute("SELECT value FROM system_state WHERE key='risk_peak_equity'").fetchone()
        peak = float(p["value"]) if p else None
        print(f"daily loss hard limit={float(settings.MAX_DAILY_LOSS_PCT)*100:.2f}% | peak DD hard limit={float(settings.MAX_DRAWDOWN_PCT)*100:.2f}%")
        print(f"stored day-start equity={start if start is not None else 'n/a'} | stored peak equity={peak if peak is not None else 'n/a'}")

    print("\nINTERPRETATION")
    print("A losing XAU streak can cool XAU without stopping BTC/USOIL. A win resets that symbol's consecutive streak.")
    print("Historical losses are still retained for V9.2 context/playbook learning.")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
