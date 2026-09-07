from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

import settings


def _dt(value: Any) -> datetime | None:
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if result.tzinfo is None:
            result = result.replace(tzinfo=timezone.utc)
        return result.astimezone(timezone.utc)
    except Exception:
        return None


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def first_hit(ticks: Any, side: int, stop: float, take: float) -> tuple[str | None, float | None]:
    """Return the first SL/TP hit seen in real bid/ask ticks."""
    for tick in ticks or []:
        price = _f(getattr(tick, "bid", 0.0) if side == 1 else getattr(tick, "ask", 0.0))
        if price <= 0:
            continue
        timestamp = _f(getattr(tick, "time_msc", 0.0)) / 1000.0 or _f(getattr(tick, "time", 0.0))
        if side == 1:
            if price <= stop:
                return "stop", timestamp
            if price >= take:
                return "take", timestamp
        else:
            if price >= stop:
                return "stop", timestamp
            if price <= take:
                return "take", timestamp
    return None, None


def main() -> int:
    try:
        import MetaTrader5 as mt5
    except Exception as error:
        print(f"MetaTrader5 package unavailable: {error}")
        return 2

    if not mt5.initialize():
        print(f"MT5 initialize failed: {mt5.last_error()}")
        return 3
    try:
        limit = max(1, int(getattr(settings, "LAB_TICK_REPLAY_MAX_TRADES", 20)))
        with sqlite3.connect(settings.DATABASE_PATH, timeout=30) as con:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                """
                SELECT id,symbol,side,entry_price,stop_loss,take_profit,opened_at,closed_at,
                       close_reason,reward_r
                FROM demo_positions
                WHERE status='closed' AND closed_at IS NOT NULL
                ORDER BY id DESC LIMIT ?
                """, (limit,)
            ).fetchall()

        print("=" * 78)
        print("V6.7 REAL-TICK PATH AUDIT - RECENT CLOSED DEMO TRADES")
        print("This does not change trades; it checks SL/TP path ordering where broker ticks exist.")
        print("=" * 78)
        checked = matched = unavailable = 0
        for row in rows:
            opened = _dt(row["opened_at"]); closed = _dt(row["closed_at"])
            if not opened or not closed:
                continue
            # Small padding helps include the first/last broker tick around saved timestamps.
            ticks = mt5.copy_ticks_range(
                str(row["symbol"]), opened - timedelta(seconds=2), closed + timedelta(seconds=2),
                getattr(mt5, "COPY_TICKS_ALL", 0),
            )
            if ticks is None or len(ticks) == 0:
                unavailable += 1
                print(f"id={row['id']} {row['symbol']}: ticks unavailable for saved interval")
                continue
            hit, ts = first_hit(ticks, int(row["side"]), _f(row["stop_loss"]), _f(row["take_profit"]))
            saved = str(row["close_reason"] or "").lower()
            expected = "take" if "take" in saved else "stop" if "stop" in saved else None
            ok = (hit == expected) if expected else None
            checked += 1
            if ok:
                matched += 1
            at = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else "-"
            print(
                f"id={row['id']} {row['symbol']} saved={saved or '-'} tick_first={hit or '-'} "
                f"match={ok} at={at} R={_f(row['reward_r']):+.2f}"
            )
        print(f"\nchecked={checked} exact_saved_reason_matches={matched} tick_unavailable={unavailable}")
        if checked:
            print(f"match rate (where saved close reason is SL/TP): {matched/checked:.1%}")
        return 0
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
