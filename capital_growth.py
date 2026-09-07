from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

import settings

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore


def _f(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if number == number else default
    except (TypeError, ValueError):
        return default


def _now_local() -> datetime:
    name = str(getattr(settings, "SESSION_TIMEZONE", "UTC") or "UTC")
    if ZoneInfo is not None:
        try:
            return datetime.now(ZoneInfo(name))
        except Exception:
            pass
    return datetime.now(timezone.utc)


def ensure_tables(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS capital_growth_days (
            day_key TEXT PRIMARY KEY,
            timezone_name TEXT NOT NULL,
            start_equity REAL NOT NULL,
            peak_equity REAL NOT NULL,
            last_equity REAL NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_capital_growth_updated
            ON capital_growth_days(updated_at);
        """
    )


def update_state(connection: sqlite3.Connection, equity: float) -> dict[str, Any]:
    """Update/read today's capital-growth state.

    The growth target is a telemetry + profit-protection objective only. It never
    increases risk to chase a target and never overrides hard risk/drawdown gates.
    """
    ensure_tables(connection)
    equity = max(0.0, _f(equity))
    now = _now_local()
    day_key = now.date().isoformat()
    tz_name = str(getattr(settings, "SESSION_TIMEZONE", "UTC") or "UTC")
    stamp = now.isoformat()

    row = connection.execute(
        "SELECT * FROM capital_growth_days WHERE day_key=?",
        (day_key,),
    ).fetchone()
    if row is None:
        start = max(equity, 1e-9)
        peak = equity
        connection.execute(
            """
            INSERT INTO capital_growth_days(
                day_key,timezone_name,start_equity,peak_equity,last_equity,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?)
            """,
            (day_key, tz_name, start, peak, equity, stamp, stamp),
        )
    else:
        start = max(_f(row["start_equity"] if isinstance(row, sqlite3.Row) else row[2]), 1e-9)
        old_peak = _f(row["peak_equity"] if isinstance(row, sqlite3.Row) else row[3])
        peak = max(old_peak, equity)
        connection.execute(
            """
            UPDATE capital_growth_days
               SET peak_equity=?, last_equity=?, updated_at=?
             WHERE day_key=?
            """,
            (peak, equity, stamp, day_key),
        )

    growth = (equity / start) - 1.0 if start > 0 else 0.0
    peak_growth = (peak / start) - 1.0 if start > 0 else 0.0

    target = max(0.0, _f(getattr(settings, "CAPITAL_GROWTH_TARGET_DAILY_PCT", 0.15), 0.15))
    stretch = max(target, _f(getattr(settings, "CAPITAL_GROWTH_STRETCH_DAILY_PCT", 0.20), 0.20))
    target_mult = max(0.0, min(1.0, _f(getattr(settings, "CAPITAL_GROWTH_AFTER_TARGET_RISK_MULTIPLIER", 0.50), 0.50)))
    giveback_mult = max(0.0, min(1.0, _f(getattr(settings, "CAPITAL_GROWTH_GIVEBACK_RISK_MULTIPLIER", 0.35), 0.35)))
    peak_trigger = max(0.0, _f(getattr(settings, "CAPITAL_GROWTH_PEAK_PROTECT_TRIGGER_PCT", 0.08), 0.08))
    max_giveback = max(0.0, _f(getattr(settings, "CAPITAL_GROWTH_MAX_GIVEBACK_FROM_PEAK_PCT", 0.03), 0.03))

    risk_multiplier = 1.0
    allow_new_entries = True
    phase = "build"
    reason = "below daily target"

    if peak_growth >= peak_trigger and (peak_growth - growth) >= max_giveback:
        risk_multiplier = min(risk_multiplier, giveback_mult)
        phase = "giveback_protect"
        reason = "protecting intraday gains after material giveback"

    if growth >= target:
        risk_multiplier = min(risk_multiplier, target_mult)
        phase = "target_protect"
        reason = "daily target reached; reduced-risk compounding"

    if growth >= stretch:
        phase = "stretch_lock"
        reason = "daily stretch target reached; protecting achieved growth"
        if bool(getattr(settings, "CAPITAL_GROWTH_BLOCK_NEW_ENTRIES_AFTER_STRETCH", True)):
            allow_new_entries = False
            risk_multiplier = 0.0

    return {
        "enabled": bool(getattr(settings, "ENABLE_CAPITAL_GROWTH_CONTROLLER", True)),
        "day": day_key,
        "timezone": tz_name,
        "start_equity": start,
        "equity": equity,
        "peak_equity": peak,
        "growth_pct": growth,
        "peak_growth_pct": peak_growth,
        "target_pct": target,
        "stretch_pct": stretch,
        "remaining_to_target_pct": max(0.0, target - growth),
        "remaining_to_stretch_pct": max(0.0, stretch - growth),
        "risk_multiplier": risk_multiplier,
        "allow_new_entries": allow_new_entries,
        "phase": phase,
        "reason": reason,
        "never_chase_target": True,
        "compounding_basis": "current_equity",
    }


def snapshot(connection: sqlite3.Connection, equity: float | None = None) -> dict[str, Any]:
    """Return a current state. If equity is supplied, refresh it first."""
    if equity is not None:
        return update_state(connection, equity)
    ensure_tables(connection)
    now = _now_local()
    day_key = now.date().isoformat()
    row = connection.execute(
        "SELECT * FROM capital_growth_days WHERE day_key=?",
        (day_key,),
    ).fetchone()
    if row is None:
        return {
            "enabled": bool(getattr(settings, "ENABLE_CAPITAL_GROWTH_CONTROLLER", True)),
            "day": day_key,
            "phase": "waiting_for_equity",
        }
    equity_now = _f(row["last_equity"] if isinstance(row, sqlite3.Row) else row[4])
    return update_state(connection, equity_now)
