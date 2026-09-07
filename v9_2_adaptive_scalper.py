from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

import settings


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, float(value)))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _session_from_snapshot(snapshot: dict[str, Any] | None) -> str:
    snap = snapshot or {}
    session = snap.get("session") if isinstance(snap.get("session"), dict) else {}
    label = str(session.get("session") or "unknown").strip().lower()
    return label or "unknown"


def ensure_tables(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS v92_context_memory (
            symbol TEXT NOT NULL,
            playbook TEXT NOT NULL,
            side INTEGER NOT NULL,
            regime TEXT NOT NULL,
            session TEXT NOT NULL,
            observations REAL NOT NULL DEFAULT 0,
            win_mass REAL NOT NULL DEFAULT 0,
            loss_mass REAL NOT NULL DEFAULT 0,
            reward_mean REAL NOT NULL DEFAULT 0,
            reward_ewma REAL NOT NULL DEFAULT 0,
            mfe_mean REAL NOT NULL DEFAULT 0,
            mae_mean REAL NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(symbol,playbook,side,regime,session)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS v92_virtual_trials (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            opened_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            closed_at TEXT,
            trial_type TEXT NOT NULL,
            symbol TEXT NOT NULL,
            playbook TEXT NOT NULL,
            side INTEGER NOT NULL,
            regime TEXT NOT NULL,
            session TEXT NOT NULL,
            score REAL NOT NULL,
            entry REAL NOT NULL,
            stop REAL NOT NULL,
            take REAL NOT NULL,
            risk_distance REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            reward_r REAL,
            mfe_r REAL NOT NULL DEFAULT 0,
            mae_r REAL NOT NULL DEFAULT 0,
            reason TEXT,
            context_json TEXT
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_v92_virtual_open ON v92_virtual_trials(status,symbol,opened_at)"
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS v92_variant_memory (
            symbol TEXT NOT NULL,
            playbook TEXT NOT NULL,
            side INTEGER NOT NULL,
            regime TEXT NOT NULL,
            session TEXT NOT NULL,
            active_variant TEXT NOT NULL DEFAULT 'champion',
            champion_n INTEGER NOT NULL DEFAULT 0,
            champion_wins INTEGER NOT NULL DEFAULT 0,
            champion_sum_r REAL NOT NULL DEFAULT 0,
            challenger_n INTEGER NOT NULL DEFAULT 0,
            challenger_wins INTEGER NOT NULL DEFAULT 0,
            challenger_sum_r REAL NOT NULL DEFAULT 0,
            promoted_at TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(symbol,playbook,side,regime,session)
        )
        """
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS v92_meta(key TEXT PRIMARY KEY, value TEXT)"
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS v92_adaptive_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            symbol TEXT NOT NULL,
            playbook TEXT,
            side INTEGER,
            regime TEXT,
            session TEXT,
            event_type TEXT NOT NULL,
            score_delta REAL,
            risk_multiplier REAL,
            details_json TEXT
        )
        """
    )


def context_memory_snapshot(
    connection: sqlite3.Connection,
    *,
    symbol: str,
    playbook: str,
    side: int,
    regime: str,
    session: str,
) -> dict[str, Any]:
    ensure_tables(connection)
    row = connection.execute(
        """
        SELECT * FROM v92_context_memory
        WHERE symbol=? AND playbook=? AND side=? AND regime=? AND session=?
        """,
        (str(symbol), str(playbook), int(side), str(regime), str(session)),
    ).fetchone()
    if not row:
        return {
            "observations": 0.0,
            "win_rate": 0.50,
            "smoothed_win_rate": 0.50,
            "reward_mean": 0.0,
            "reward_ewma": 0.0,
            "mfe_mean": 0.0,
            "mae_mean": 0.0,
        }
    try:
        get = lambda key: row[key]
    except Exception:
        cols = [d[0] for d in connection.execute("PRAGMA table_info(v92_context_memory)").fetchall()]
        get = lambda key: row[cols.index(key)]
    n = max(0.0, _f(get("observations")))
    wins = max(0.0, _f(get("win_mass")))
    losses = max(0.0, _f(get("loss_mass")))
    win = wins / max(1e-9, wins + losses) if wins + losses > 0 else 0.50
    smooth = (wins + 2.0) / max(1e-9, wins + losses + 4.0)
    return {
        "observations": n,
        "win_rate": round(win, 4),
        "smoothed_win_rate": round(smooth, 4),
        "reward_mean": round(_f(get("reward_mean")), 4),
        "reward_ewma": round(_f(get("reward_ewma")), 4),
        "mfe_mean": round(max(0.0, _f(get("mfe_mean"))), 4),
        "mae_mean": round(max(0.0, _f(get("mae_mean"))), 4),
    }


def _context_memory_adjustment(mem: dict[str, Any]) -> tuple[float, float, str]:
    n = int(_f(mem.get("observations")))
    if n <= 0:
        return 0.0, 1.0, "new"
    win = _f(mem.get("smoothed_win_rate"), 0.50)
    ewma = _f(mem.get("reward_ewma"), 0.0)
    mean_r = _f(mem.get("reward_mean"), 0.0)
    mfe = _f(mem.get("mfe_mean"), 0.0)
    mae = _f(mem.get("mae_mean"), 0.0)
    confidence = min(1.0, n / max(4.0, _f(getattr(settings, "V92_CONTEXT_FULL_WEIGHT_N", 20), 20)))
    delta = 0.0
    delta += _clamp((win - 0.50) * 18.0, -5.0, 5.0)
    delta += _clamp(ewma * 7.0, -4.0, 4.0)
    delta += _clamp((mfe - mae) * 1.5, -2.0, 2.0)
    delta *= confidence
    state = "mixed"
    risk = 1.0
    if n >= 4 and win >= 0.58 and ewma > 0.08 and mean_r > 0:
        delta += 2.0 * confidence
        risk = 1.05
        state = "strong"
    elif n >= 4 and win <= 0.38 and ewma < -0.05:
        delta -= 3.0 * confidence
        risk = 0.72
        state = "weak"
    if n >= 10 and win < 0.30 and ewma < -0.15:
        risk = min(risk, 0.50)
        state = "poor"
    return _clamp(delta, -10.0, 7.0), _clamp(risk, 0.45, 1.05), state


def _aligned(value: float, side: int) -> float:
    return _clamp(_f(value) * (1.0 if int(side) > 0 else -1.0), -1.0, 1.0)


def _microstructure_overlay(
    *, side: int, playbook: str, snapshot: dict[str, Any] | None, micro: dict[str, Any] | None
) -> dict[str, Any]:
    snap = snapshot or {}
    order_flow = snap.get("order_flow") if isinstance(snap.get("order_flow"), dict) else {}
    tick_flow = snap.get("tick_flow") if isinstance(snap.get("tick_flow"), dict) else {}
    dom_score = _f(order_flow.get("score"), _f((micro or {}).get("score"), 0.0))
    tick_delta = _f(tick_flow.get("delta_ratio"), 0.0)
    dom_aligned = _aligned(dom_score, side)
    tick_aligned = _aligned(tick_delta, side)
    count = max(0, int(_f(tick_flow.get("count"), 0)))
    seconds = max(1.0, _f(tick_flow.get("seconds"), 30.0))
    tick_speed = count / seconds
    speed_score = _clamp((tick_speed - 0.8) / 3.0, -0.25, 1.0)
    combined = _clamp(0.50 * dom_aligned + 0.40 * tick_aligned + 0.10 * speed_score, -1.0, 1.0)
    score_delta = 5.0 * combined
    risk_mult = _clamp(1.0 + 0.18 * combined, 0.78, 1.08)
    return {
        "available": bool(order_flow.get("available") or tick_flow.get("available") or (micro or {}).get("available")),
        "dom_aligned": round(dom_aligned, 4),
        "tick_aligned": round(tick_aligned, 4),
        "tick_speed": round(tick_speed, 3),
        "combined": round(combined, 4),
        "score_delta": round(score_delta, 2),
        "risk_multiplier": round(risk_mult, 4),
        "source": str(tick_flow.get("source") or "dom_only"),
    }


def _mtf_overlay(side: int, snapshot: dict[str, Any] | None) -> dict[str, Any]:
    snap = snapshot or {}
    htf = snap.get("higher_timeframes") if isinstance(snap.get("higher_timeframes"), dict) else {}
    expected = "up" if int(side) > 0 else "down"
    opposite = "down" if int(side) > 0 else "up"
    weights = {"M5": 1.0, "M15": 0.65, "H1": 0.25}
    score = 0.0
    used = 0.0
    labels: dict[str, str] = {}
    for name, weight in weights.items():
        row = htf.get(name) if isinstance(htf.get(name), dict) else {}
        if not row.get("available"):
            continue
        trend = str(row.get("trend") or "neutral")
        labels[name] = trend
        used += weight
        if trend == expected:
            score += weight
        elif trend == opposite:
            score -= weight
    alignment = score / used if used > 0 else 0.0
    return {
        "alignment": round(_clamp(alignment, -1.0, 1.0), 4),
        "score_delta": round(3.0 * _clamp(alignment, -1.0, 1.0), 2),
        "risk_multiplier": round(_clamp(1.0 + 0.10 * alignment, 0.88, 1.06), 4),
        "trends": labels,
    }


def _volume_profile_overlay(side: int, playbook: str, snapshot: dict[str, Any] | None) -> dict[str, Any]:
    snap = snapshot or {}
    vp = snap.get("volume_profile") if isinstance(snap.get("volume_profile"), dict) else {}
    ctx = snap.get("derived_context") if isinstance(snap.get("derived_context"), dict) else {}
    if not vp.get("available"):
        return {"available": False, "score_delta": 0.0, "risk_multiplier": 1.0}
    poc_dist = _f(ctx.get("price_minus_poc_atr"), 0.0)
    val_dist = _f(ctx.get("price_minus_val_atr"), 0.0)
    vah_dist = _f(ctx.get("price_minus_vah_atr"), 0.0)
    p = str(playbook).lower()
    reversal = any(k in p for k in ("wick", "exhaust", "mean_revert", "snapback"))
    continuation = any(k in p for k in ("momentum", "breakout", "impulse", "squeeze"))
    delta = 0.0
    if reversal:
        # Value-area edge proximity helps reversals; chasing far outside value hurts.
        edge_near = min(abs(val_dist), abs(vah_dist))
        delta += _clamp((0.45 - edge_near) * 4.0, -2.0, 1.8)
    elif continuation:
        direction_from_poc = _aligned(_clamp(poc_dist / 1.2, -1.0, 1.0), side)
        delta += 2.0 * direction_from_poc
    return {
        "available": True,
        "poc_distance_atr": round(poc_dist, 4),
        "val_distance_atr": round(val_dist, 4),
        "vah_distance_atr": round(vah_dist, 4),
        "score_delta": round(_clamp(delta, -2.5, 2.5), 2),
        "risk_multiplier": round(_clamp(1.0 + 0.04 * delta, 0.90, 1.06), 4),
    }


def _exhaustion_overlay(side: int, playbook: str, snapshot: dict[str, Any] | None) -> dict[str, Any]:
    snap = snapshot or {}
    tech = snap.get("technical") if isinstance(snap.get("technical"), dict) else {}
    rsi = _f(tech.get("rsi7"), 50.0)
    p = str(playbook).lower()
    reversal = any(k in p for k in ("wick", "exhaust", "mean_revert", "snapback"))
    continuation = any(k in p for k in ("momentum", "breakout", "impulse", "squeeze"))
    delta = 0.0
    if reversal:
        if int(side) > 0 and rsi <= 30:
            delta += 2.5
        elif int(side) < 0 and rsi >= 70:
            delta += 2.5
        elif int(side) > 0 and rsi >= 72:
            delta -= 2.0
        elif int(side) < 0 and rsi <= 28:
            delta -= 2.0
    elif continuation:
        if int(side) > 0 and rsi >= 82:
            delta -= 1.5
        elif int(side) < 0 and rsi <= 18:
            delta -= 1.5
    return {"rsi7": round(rsi, 2), "score_delta": round(delta, 2)}


def _session_overlay(symbol: str, snapshot: dict[str, Any] | None) -> dict[str, Any]:
    session = _session_from_snapshot(snapshot)
    upper = str(symbol).upper()
    delta = 0.0
    floor_delta = 0.0
    risk = 1.0
    if "BTC" in upper or "ETH" in upper:
        # Crypto remains 24/7; US/Europe liquidity receives a tiny preference.
        if session in {"crypto_us", "crypto_europe", "unrestricted"}:
            delta = 0.8
        elif session == "crypto_asia":
            delta = 0.2
    else:
        if session in {"london", "new_york", "london_new_york_overlap", "london_newyork_overlap"}:
            delta = 1.0
            floor_delta = -1.0
            risk = 1.03
        elif "off" in session:
            delta = -2.0
            floor_delta = 2.0
            risk = 0.82
        elif session == "asia":
            delta = -0.5
            floor_delta = 1.0
            risk = 0.92
    return {
        "session": session,
        "score_delta": delta,
        "execute_floor_delta": floor_delta,
        "risk_multiplier": risk,
    }


def variant_snapshot(
    connection: sqlite3.Connection,
    *, symbol: str, playbook: str, side: int, regime: str, session: str
) -> dict[str, Any]:
    ensure_tables(connection)
    row = connection.execute(
        """
        SELECT * FROM v92_variant_memory
        WHERE symbol=? AND playbook=? AND side=? AND regime=? AND session=?
        """,
        (symbol, playbook, int(side), regime, session),
    ).fetchone()
    if not row:
        return {"active_variant": "champion", "champion_n": 0, "challenger_n": 0}
    keys = row.keys() if hasattr(row, "keys") else []
    def g(k: str, d: Any = 0):
        try:
            return row[k]
        except Exception:
            return d
    cn = int(g("champion_n", 0) or 0)
    csum = _f(g("champion_sum_r", 0.0))
    cwin = int(g("champion_wins", 0) or 0)
    hn = int(g("challenger_n", 0) or 0)
    hsum = _f(g("challenger_sum_r", 0.0))
    hwin = int(g("challenger_wins", 0) or 0)
    return {
        "active_variant": str(g("active_variant", "champion") or "champion"),
        "champion_n": cn,
        "champion_mean_r": csum / cn if cn else 0.0,
        "champion_win_rate": cwin / cn if cn else 0.0,
        "challenger_n": hn,
        "challenger_mean_r": hsum / hn if hn else 0.0,
        "challenger_win_rate": hwin / hn if hn else 0.0,
        "promoted_at": g("promoted_at", None),
    }


def entry_overlay(
    connection: sqlite3.Connection,
    *,
    symbol: str,
    playbook: str,
    side: int,
    regime: str,
    pre: dict[str, Any],
    spartan_snapshot: dict[str, Any] | None,
    micro: dict[str, Any] | None,
    session_hint: str | None = None,
) -> dict[str, Any]:
    ensure_tables(connection)
    session_data = _session_overlay(symbol, spartan_snapshot)
    session = str(session_data["session"])
    if session_hint and session in {"unknown", "unrestricted", "advisory_unrestricted"}:
        session = str(session_hint)
        session_data["session"] = session
        upper = str(symbol).upper()
        if "BTC" in upper or "ETH" in upper:
            if session in {"crypto_us", "crypto_europe"}:
                session_data["score_delta"] = 0.8
            elif session == "crypto_asia":
                session_data["score_delta"] = 0.2
    mem = context_memory_snapshot(
        connection, symbol=symbol, playbook=playbook, side=side, regime=regime, session=session
    )
    mem_delta, mem_risk, mem_state = _context_memory_adjustment(mem)
    microstructure = _microstructure_overlay(
        side=side, playbook=playbook, snapshot=spartan_snapshot, micro=micro
    )
    mtf = _mtf_overlay(side, spartan_snapshot)
    vp = _volume_profile_overlay(side, playbook, spartan_snapshot)
    exhaustion = _exhaustion_overlay(side, playbook, spartan_snapshot)
    variant = variant_snapshot(
        connection, symbol=symbol, playbook=playbook, side=side, regime=regime, session=session
    )

    score_delta = (
        mem_delta
        + _f(microstructure.get("score_delta"))
        + _f(mtf.get("score_delta"))
        + _f(vp.get("score_delta"))
        + _f(exhaustion.get("score_delta"))
        + _f(session_data.get("score_delta"))
    )
    score_delta = _clamp(score_delta, -14.0, 12.0)
    risk = (
        mem_risk
        * _f(microstructure.get("risk_multiplier"), 1.0)
        * _f(mtf.get("risk_multiplier"), 1.0)
        * _f(vp.get("risk_multiplier"), 1.0)
        * _f(session_data.get("risk_multiplier"), 1.0)
    )
    risk = _clamp(risk, 0.35, 1.08)

    # Risk-free trial ramp for new context cells: first five actual broker trades
    # are small, then size ramps as real evidence accumulates.
    n = int(_f(mem.get("observations"), 0))
    if n < 5:
        trial_cap = _f(getattr(settings, "V92_NEW_PLAYBOOK_FIRST5_RISK_CAP", 0.25), 0.25)
    elif n < 10:
        trial_cap = _f(getattr(settings, "V92_NEW_PLAYBOOK_6_10_RISK_CAP", 0.50), 0.50)
    elif n < 20:
        trial_cap = _f(getattr(settings, "V92_NEW_PLAYBOOK_11_20_RISK_CAP", 0.75), 0.75)
    else:
        trial_cap = 1.0

    floor = _f(getattr(settings, "V9_EXECUTE_SCORE_MIN", 62.0), 62.0) + _f(session_data.get("execute_floor_delta"))
    # Strong contextual evidence can lower the floor slightly; weak evidence raises it.
    if mem_state == "strong" and n >= 8:
        floor -= 1.5
    elif mem_state in {"weak", "poor"} and n >= 6:
        floor += 2.0
    floor = _clamp(floor, _f(getattr(settings, "V92_MIN_ADAPTIVE_EXECUTE_FLOOR", 59.0), 59.0),
                   _f(getattr(settings, "V92_MAX_ADAPTIVE_EXECUTE_FLOOR", 69.0), 69.0))

    overlay = {
        "session": session,
        "score_delta": round(score_delta, 2),
        "risk_multiplier": round(risk, 4),
        "risk_cap": round(_clamp(trial_cap, 0.20, 1.0), 4),
        "execute_floor": round(floor, 2),
        "context_memory": {**mem, "state": mem_state},
        "microstructure": microstructure,
        "mtf": mtf,
        "volume_profile": vp,
        "exhaustion": exhaustion,
        "variant": variant,
    }
    connection.execute(
        """
        INSERT INTO v92_adaptive_events(
            timestamp,symbol,playbook,side,regime,session,event_type,
            score_delta,risk_multiplier,details_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (
            _now(), symbol, playbook, int(side), regime, session, "entry_overlay",
            score_delta, risk, json.dumps(overlay, separators=(",", ":"), default=str),
        ),
    )
    return overlay


def apply_overlay(pre: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    out = dict(pre)
    out["pre_score"] = round(_clamp(_f(pre.get("pre_score")) + _f(overlay.get("score_delta")), 0.0, 100.0), 2)
    out["execute_floor"] = _f(overlay.get("execute_floor"), _f(getattr(settings, "V9_EXECUTE_SCORE_MIN", 62.0), 62.0))
    out["v92_risk_multiplier"] = _f(overlay.get("risk_multiplier"), 1.0)
    out["v92_risk_cap"] = _f(overlay.get("risk_cap"), 1.0)
    out["v92_overlay"] = overlay
    return out


def _frame_speed(frame: Any, atr_value: float) -> float:
    try:
        closes = frame["close"].astype(float).tail(6).tolist()
    except Exception:
        return 0.6
    if len(closes) < 4 or atr_value <= 0:
        return 0.6
    move = abs(closes[-1] - closes[-4]) / max(1e-12, atr_value)
    return _clamp(move / 3.0, 0.0, 2.5)


def dynamic_exit_plan(
    *,
    base_stop_atr: float,
    base_take_atr: float,
    atr_value: float,
    frame: Any,
    grade: str,
    playbook: str,
    memory: dict[str, Any] | None,
    overlay: dict[str, Any] | None,
) -> dict[str, Any]:
    mem = memory or {}
    ov = overlay or {}
    speed = _frame_speed(frame, atr_value)
    stop = _clamp(_f(base_stop_atr, 0.8), 0.35, 1.50)
    base_take = _clamp(_f(base_take_atr, 1.2), 0.50, 2.80)
    base_rr = base_take / max(1e-9, stop)

    # Volatility/speed adaptation: slow market banks a closer objective, strong
    # impulse gives a runner more room without widening risk aggressively.
    if speed < 0.35:
        rr = base_rr * 0.82
        stop *= 0.94
    elif speed > 1.10:
        rr = base_rr * 1.12
        stop *= 1.02
    else:
        rr = base_rr

    n = int(_f(mem.get("observations"), 0))
    mfe = max(0.0, _f(mem.get("mfe_mean"), 0.0))
    mae = max(0.0, _f(mem.get("mae_mean"), 0.0))
    if n >= 4 and mfe > 0:
        learned_rr = _clamp(0.75 * mfe, 0.55, 2.40)
        rr = 0.55 * rr + 0.45 * learned_rr
        if mae > 0.95:
            stop *= 0.96

    session = str(ov.get("session") or "unknown")
    if session in {"london", "new_york", "london_new_york_overlap", "london_newyork_overlap", "crypto_us", "crypto_europe"}:
        rr *= 1.05
    elif session in {"asia", "crypto_asia"}:
        rr *= 0.92
    elif "off" in session:
        rr *= 0.85

    variant = str((ov.get("variant") or {}).get("active_variant") or "champion")
    if variant == "challenger":
        # Promoted challenger is intentionally a small bounded perturbation.
        rr *= _f(getattr(settings, "V92_CHALLENGER_TAKE_MULTIPLIER", 1.10), 1.10)
        stop *= _f(getattr(settings, "V92_CHALLENGER_STOP_MULTIPLIER", 0.96), 0.96)

    rr = _clamp(rr, 0.55, 2.50)
    stop = _clamp(stop, 0.35, 1.45)
    take = _clamp(rr * stop, 0.45, 3.20)
    grade_u = str(grade or "B").upper()
    if grade_u == "A+":
        first_r, second_r = 0.55, min(1.15, max(0.85, 0.60 * rr))
        be_at, trail_at, trail_dist = 0.62, 0.95, 0.60
    elif grade_u == "A":
        first_r, second_r = 0.48, min(1.00, max(0.75, 0.55 * rr))
        be_at, trail_at, trail_dist = 0.52, 0.82, 0.52
    else:
        first_r, second_r = 0.40, min(0.85, max(0.65, 0.50 * rr))
        be_at, trail_at, trail_dist = 0.42, 0.70, 0.45

    if n >= 4 and (_f(mem.get("reward_ewma"), 0.0) < -0.08 or _f(mem.get("win_rate"), 0.5) < 0.35):
        first_r = min(first_r, 0.34)
        be_at = min(be_at, 0.34)
        trail_at = min(trail_at, 0.60)
        trail_dist = min(trail_dist, 0.40)

    return {
        "stop_atr": round(stop, 4),
        "take_atr": round(take, 4),
        "target_r": round(take / max(1e-9, stop), 4),
        "speed_atr_per_bar": round(speed, 4),
        "variant": variant,
        "partials": [
            {"stage": "p1", "at_r": round(first_r, 3), "fraction": 0.50, "lock_r": max(0.05, round(first_r * 0.20, 3))},
            {"stage": "p2", "at_r": round(second_r, 3), "fraction": 0.30, "lock_r": max(0.15, round(second_r * 0.35, 3))},
        ],
        "runner_fraction": 0.20,
        "be_at_r": round(be_at, 3),
        "trail_at_r": round(trail_at, 3),
        "trail_distance_r": round(trail_dist, 3),
    }


def record_broker_outcome(
    connection: sqlite3.Connection,
    *,
    symbol: str,
    side: int,
    regime: str,
    reward_r: float,
    mfe_r: float,
    mae_r: float,
    entry_context: dict[str, Any] | None,
    update_variant: bool = True,
) -> None:
    ensure_tables(connection)
    ctx = entry_context or {}
    v92 = ctx.get("v9_2") if isinstance(ctx.get("v9_2"), dict) else {}
    micro = ctx.get("micro_hunter") if isinstance(ctx.get("micro_hunter"), dict) else {}
    playbook = str(v92.get("playbook") or micro.get("playbook") or "unknown")
    session = str(v92.get("session") or "unknown")
    if playbook == "unknown":
        return
    row = connection.execute(
        """
        SELECT * FROM v92_context_memory
        WHERE symbol=? AND playbook=? AND side=? AND regime=? AND session=?
        """,
        (symbol, playbook, int(side), regime, session),
    ).fetchone()
    n = _f(row["observations"], 0.0) if row else 0.0
    wins = _f(row["win_mass"], 0.0) if row else 0.0
    losses = _f(row["loss_mass"], 0.0) if row else 0.0
    mean = _f(row["reward_mean"], 0.0) if row else 0.0
    ewma = _f(row["reward_ewma"], 0.0) if row else 0.0
    mfe_mean = _f(row["mfe_mean"], 0.0) if row else 0.0
    mae_mean = _f(row["mae_mean"], 0.0) if row else 0.0
    new_n = n + 1.0
    rr = _f(reward_r)
    mean = mean + (rr - mean) / new_n
    alpha = _clamp(_f(getattr(settings, "V92_CONTEXT_EWMA_ALPHA", 0.25), 0.25), 0.05, 0.60)
    ewma = rr if n <= 0 else alpha * rr + (1.0 - alpha) * ewma
    mfe_mean = mfe_mean + (max(0.0, _f(mfe_r)) - mfe_mean) / new_n
    mae_mean = mae_mean + (max(0.0, _f(mae_r)) - mae_mean) / new_n
    if rr > 0:
        wins += 1.0
    else:
        losses += 1.0
    connection.execute(
        """
        INSERT INTO v92_context_memory(
            symbol,playbook,side,regime,session,observations,win_mass,loss_mass,
            reward_mean,reward_ewma,mfe_mean,mae_mean,updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(symbol,playbook,side,regime,session) DO UPDATE SET
            observations=excluded.observations, win_mass=excluded.win_mass,
            loss_mass=excluded.loss_mass, reward_mean=excluded.reward_mean,
            reward_ewma=excluded.reward_ewma, mfe_mean=excluded.mfe_mean,
            mae_mean=excluded.mae_mean, updated_at=excluded.updated_at
        """,
        (symbol, playbook, int(side), regime, session, new_n, wins, losses,
         mean, ewma, mfe_mean, mae_mean, _now()),
    )
    if update_variant and bool(getattr(settings, "V92_CHALLENGER_ENABLED", True)):
        update_variant_outcome(
            connection, symbol=symbol, playbook=playbook, side=side, regime=regime,
            session=session, reward_r=rr, mfe_r=mfe_r, mae_r=mae_r,
            entry_context=entry_context,
        )


def bootstrap_existing_v9(connection: sqlite3.Connection) -> int:
    """Seed V9.2 contextual memory from already-closed V9 broker trades once."""
    ensure_tables(connection)
    marker = connection.execute("SELECT value FROM v92_meta WHERE key='bootstrap_v9_context'").fetchone()
    if marker:
        return 0
    rows = connection.execute(
        """
        SELECT symbol,side,regime,reward_r,mfe_r,mae_r,context_json
        FROM demo_positions
        WHERE status='closed' AND reward_r IS NOT NULL
          AND execution_tier LIKE 'v9%'
        ORDER BY id
        """
    ).fetchall()
    used = 0
    for row in rows:
        try:
            ctx = json.loads(str(row["context_json"] or "{}"))
        except Exception:
            ctx = {}
        micro = ctx.get("micro_hunter") if isinstance(ctx.get("micro_hunter"), dict) else {}
        playbook = str(micro.get("playbook") or "unknown")
        sp = ctx.get("spartan_pro") if isinstance(ctx.get("spartan_pro"), dict) else {}
        session = _session_from_snapshot(sp)
        if playbook == "unknown":
            continue
        synthetic = dict(ctx)
        synthetic["v9_2"] = {"playbook": playbook, "session": session}
        record_broker_outcome(
            connection, symbol=str(row["symbol"]), side=int(row["side"]),
            regime=str(row["regime"] or "unknown"), reward_r=_f(row["reward_r"]),
            mfe_r=_f(row["mfe_r"]), mae_r=_f(row["mae_r"]),
            entry_context=synthetic, update_variant=False,
        )
        used += 1
    connection.execute(
        "INSERT OR REPLACE INTO v92_meta(key,value) VALUES('bootstrap_v9_context',?)",
        (f"{_now()}|rows={used}",),
    )
    return used


def _variant_row(connection: sqlite3.Connection, symbol: str, playbook: str, side: int, regime: str, session: str):
    return connection.execute(
        """SELECT * FROM v92_variant_memory
           WHERE symbol=? AND playbook=? AND side=? AND regime=? AND session=?""",
        (symbol, playbook, int(side), regime, session),
    ).fetchone()


def update_variant_outcome(
    connection: sqlite3.Connection,
    *, symbol: str, playbook: str, side: int, regime: str, session: str,
    reward_r: float, mfe_r: float, mae_r: float, entry_context: dict[str, Any] | None,
) -> None:
    ensure_tables(connection)
    ctx = entry_context or {}
    exit_plan = ((ctx.get("v9_2") or {}).get("exit_plan") if isinstance(ctx.get("v9_2"), dict) else {}) or {}
    champion_r = _f(reward_r)
    target_r = max(0.1, _f(exit_plan.get("target_r"), 1.0))
    challenger_target = _clamp(target_r * _f(getattr(settings, "V92_CHALLENGER_TAKE_MULTIPLIER", 1.10), 1.10), 0.45, 2.8)
    challenger_stop = _clamp(_f(getattr(settings, "V92_CHALLENGER_STOP_MULTIPLIER", 0.96), 0.96), 0.75, 1.10)
    mfe = max(0.0, _f(mfe_r))
    mae = max(0.0, _f(mae_r))
    if mfe >= challenger_target and mae < challenger_stop:
        challenger_r = challenger_target
    elif mae >= challenger_stop and mfe < challenger_target:
        challenger_r = -challenger_stop
    else:
        # Ambiguous ordering: conservative counterfactual blend, not fake certainty.
        challenger_r = _clamp(champion_r, -challenger_stop, challenger_target)

    row = _variant_row(connection, symbol, playbook, side, regime, session)
    active = str(row["active_variant"] or "champion") if row else "champion"
    cn = int(row["champion_n"] or 0) if row else 0
    cw = int(row["champion_wins"] or 0) if row else 0
    cs = _f(row["champion_sum_r"], 0.0) if row else 0.0
    hn = int(row["challenger_n"] or 0) if row else 0
    hw = int(row["challenger_wins"] or 0) if row else 0
    hs = _f(row["challenger_sum_r"], 0.0) if row else 0.0
    cn += 1; cs += champion_r; cw += 1 if champion_r > 0 else 0
    hn += 1; hs += challenger_r; hw += 1 if challenger_r > 0 else 0
    min_n = max(8, int(getattr(settings, "V92_CHALLENGER_MIN_SAMPLES", 12)))
    margin = _f(getattr(settings, "V92_CHALLENGER_PROMOTION_MARGIN_R", 0.12), 0.12)
    promoted_at = row["promoted_at"] if row else None
    if hn >= min_n and cn >= min_n:
        cmean = cs / cn
        hmean = hs / hn
        cwin = cw / cn
        hwin = hw / hn
        if active == "champion" and hmean >= cmean + margin and hwin >= cwin - 0.05:
            active = "challenger"
            promoted_at = _now()
    connection.execute(
        """
        INSERT INTO v92_variant_memory(
            symbol,playbook,side,regime,session,active_variant,
            champion_n,champion_wins,champion_sum_r,
            challenger_n,challenger_wins,challenger_sum_r,promoted_at,updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(symbol,playbook,side,regime,session) DO UPDATE SET
            active_variant=excluded.active_variant,
            champion_n=excluded.champion_n,champion_wins=excluded.champion_wins,
            champion_sum_r=excluded.champion_sum_r,
            challenger_n=excluded.challenger_n,challenger_wins=excluded.challenger_wins,
            challenger_sum_r=excluded.challenger_sum_r,promoted_at=excluded.promoted_at,
            updated_at=excluded.updated_at
        """,
        (symbol, playbook, int(side), regime, session, active,
         cn, cw, cs, hn, hw, hs, promoted_at, _now()),
    )


def record_virtual_trial(
    connection: sqlite3.Connection,
    *, trial_type: str, symbol: str, playbook: str, side: int, regime: str,
    session: str, score: float, entry: float, stop: float, take: float,
    reason: str, context: dict[str, Any] | None = None,
) -> None:
    if not bool(getattr(settings, "V92_REJECTED_TRADE_LEARNING_ENABLED", True)):
        return
    ensure_tables(connection)
    risk = abs(_f(entry) - _f(stop))
    if risk <= 0 or _f(entry) <= 0:
        return
    # De-duplicate same symbol/playbook/side virtual entry during a short window.
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
    existing = connection.execute(
        """
        SELECT COUNT(*) AS n FROM v92_virtual_trials
        WHERE status='open' AND symbol=? AND playbook=? AND side=? AND opened_at>=?
        """,
        (symbol, playbook, int(side), cutoff),
    ).fetchone()
    try:
        if int(existing["n"] or 0) > 0:
            return
    except Exception:
        if existing and int(existing[0] or 0) > 0:
            return
    ttl = max(3, int(getattr(settings, "V92_REJECTED_TRIAL_TTL_MINUTES", 12)))
    expires = (datetime.now(timezone.utc) + timedelta(minutes=ttl)).isoformat()
    connection.execute(
        """
        INSERT INTO v92_virtual_trials(
            opened_at,expires_at,trial_type,symbol,playbook,side,regime,session,
            score,entry,stop,take,risk_distance,status,reason,context_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'open',?,?)
        """,
        (_now(), expires, trial_type, symbol, playbook, int(side), regime, session,
         _f(score), _f(entry), _f(stop), _f(take), risk, reason,
         json.dumps(context or {}, separators=(",", ":"), default=str)),
    )


def reconcile_virtual_trials(
    connection: sqlite3.Connection, *, symbol: str, bid: float, ask: float
) -> list[dict[str, Any]]:
    ensure_tables(connection)
    rows = connection.execute(
        "SELECT * FROM v92_virtual_trials WHERE status='open' AND symbol=? ORDER BY id",
        (symbol,),
    ).fetchall()
    now = datetime.now(timezone.utc)
    closed: list[dict[str, Any]] = []
    for row in rows:
        side = int(row["side"] or 0)
        entry = _f(row["entry"]); stop = _f(row["stop"]); take = _f(row["take"])
        risk = max(1e-12, _f(row["risk_distance"]))
        price = _f(bid if side > 0 else ask)
        current_r = ((price - entry) * side) / risk
        mfe = max(_f(row["mfe_r"]), max(0.0, current_r))
        mae = max(_f(row["mae_r"]), max(0.0, -current_r))
        outcome = None
        reason = None
        if side > 0 and price >= take or side < 0 and price <= take:
            outcome = abs(take - entry) / risk
            reason = "virtual_target"
        elif side > 0 and price <= stop or side < 0 and price >= stop:
            outcome = -abs(stop - entry) / risk
            reason = "virtual_stop"
        else:
            try:
                expiry = datetime.fromisoformat(str(row["expires_at"]))
            except Exception:
                expiry = now
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            if now >= expiry:
                outcome = _clamp(current_r, -1.0, 2.0)
                reason = "virtual_ttl"
        if outcome is None:
            connection.execute(
                "UPDATE v92_virtual_trials SET mfe_r=?, mae_r=? WHERE id=?",
                (mfe, mae, int(row["id"])),
            )
            continue
        connection.execute(
            """
            UPDATE v92_virtual_trials
            SET status='closed',closed_at=?,reward_r=?,mfe_r=?,mae_r=?,reason=?
            WHERE id=?
            """,
            (_now(), outcome, mfe, mae, reason, int(row["id"])),
        )
        closed.append({"id": int(row["id"]), "reward_r": outcome, "reason": reason})
    return closed


def rejected_threshold_bias(connection: sqlite3.Connection, symbol: str) -> dict[str, Any]:
    ensure_tables(connection)
    lookback = max(10, int(getattr(settings, "V92_REJECTED_ANALYSIS_WINDOW", 40)))
    rows = connection.execute(
        """
        SELECT reward_r FROM v92_virtual_trials
        WHERE symbol=? AND trial_type='rejected' AND status='closed' AND reward_r IS NOT NULL
        ORDER BY id DESC LIMIT ?
        """,
        (symbol, lookback),
    ).fetchall()
    values = [_f(r["reward_r"] if hasattr(r, "keys") else r[0]) for r in rows]
    n = len(values)
    if n < 8:
        return {"n": n, "mean_r": 0.0, "score_floor_delta": 0.0}
    mean_r = sum(values) / n
    win = sum(1 for v in values if v > 0) / n
    # If many rejected trades would have won, lower floor only slightly; if they
    # were bad, let the filter become a little stricter. Bounded to avoid drift.
    if mean_r > 0.18 and win > 0.55:
        delta = -1.5
    elif mean_r < -0.12 and win < 0.40:
        delta = 1.5
    else:
        delta = 0.0
    return {"n": n, "mean_r": round(mean_r, 4), "win_rate": round(win, 4), "score_floor_delta": delta}
