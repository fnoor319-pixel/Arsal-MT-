from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import pandas as pd

import settings
import institutional_alpha as institutional_alpha


def _f(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso_plus(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=max(1, int(seconds)))).isoformat()


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _side_name(side: int) -> str:
    return "BUY" if int(side) == 1 else "SELL"


@dataclass(frozen=True)
class MicroSetup:
    symbol: str
    playbook: str
    side: int
    phase: str
    score: float
    bar_time: str
    trigger_price: float
    invalidation_price: float
    stop_atr: float
    take_atr: float
    max_hold_bars: int
    preferred_families: tuple[str, ...]
    risk_multiplier: float
    context: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["preferred_families"] = list(self.preferred_families)
        return result


PLAYBOOK_FAMILIES: dict[str, tuple[str, ...]] = {
    "momentum_burst": ("micro_momentum", "super_scalp", "scalp"),
    "impulse_pullback_resume": ("pullback_scalp", "micro_momentum", "scalp"),
    "squeeze_breakout": ("breakout_scalp", "micro_momentum", "super_scalp"),
    "wick_rejection": ("mean_revert_scalp", "pullback_scalp", "scalp"),
    "exhaustion_snapback": ("mean_revert_scalp", "super_scalp", "scalp"),
}


PLAYBOOK_EXIT_PROFILES: dict[str, dict[str, float]] = {
    "momentum_burst": {"be_at_r": 0.55, "be_lock_r": 0.04, "trail_at_r": 0.85, "trail_distance_r": 0.55},
    "impulse_pullback_resume": {"be_at_r": 0.60, "be_lock_r": 0.05, "trail_at_r": 0.90, "trail_distance_r": 0.58},
    "squeeze_breakout": {"be_at_r": 0.55, "be_lock_r": 0.03, "trail_at_r": 0.90, "trail_distance_r": 0.60},
    "wick_rejection": {"be_at_r": 0.45, "be_lock_r": 0.03, "trail_at_r": 0.70, "trail_distance_r": 0.45},
    "exhaustion_snapback": {"be_at_r": 0.40, "be_lock_r": 0.02, "trail_at_r": 0.65, "trail_distance_r": 0.42},
}


def ensure_tables(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS micro_hunter_setups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            symbol TEXT NOT NULL,
            bar_time TEXT NOT NULL,
            playbook TEXT NOT NULL,
            side INTEGER NOT NULL,
            phase TEXT NOT NULL,
            score REAL NOT NULL,
            trigger_price REAL,
            invalidation_price REAL,
            stop_atr REAL,
            take_atr REAL,
            max_hold_bars INTEGER,
            preferred_families_json TEXT NOT NULL DEFAULT '[]',
            risk_multiplier REAL NOT NULL DEFAULT 1.0,
            context_json TEXT NOT NULL DEFAULT '{}',
            strategy_id INTEGER,
            execution_tier TEXT,
            triggered_at TEXT,
            resolved_at TEXT,
            outcome_r REAL,
            UNIQUE(symbol, bar_time, playbook, side)
        );
        CREATE INDEX IF NOT EXISTS idx_micro_hunter_symbol_phase
            ON micro_hunter_setups(symbol, phase, updated_at);

        CREATE TABLE IF NOT EXISTS micro_playbook_memory (
            symbol TEXT NOT NULL,
            playbook TEXT NOT NULL,
            side INTEGER NOT NULL,
            observations INTEGER NOT NULL DEFAULT 0,
            wins INTEGER NOT NULL DEFAULT 0,
            losses INTEGER NOT NULL DEFAULT 0,
            reward_sum REAL NOT NULL DEFAULT 0,
            reward_ewma REAL NOT NULL DEFAULT 0,
            mfe_sum REAL NOT NULL DEFAULT 0,
            mae_sum REAL NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(symbol, playbook, side)
        );
        """
    )


def _symbol_threshold(symbol: str, kind: str) -> float:
    prefix = "V7_MICRO_HUNTER"
    suffix = str(symbol).upper().replace("M", "")
    if "XAUUSD" in suffix:
        key = f"{prefix}_XAU_{kind}"
    elif "USOIL" in suffix or "OIL" in suffix:
        key = f"{prefix}_OIL_{kind}"
    elif "BTC" in suffix:
        key = f"{prefix}_BTC_{kind}"
    else:
        key = f"{prefix}_{kind}"
    defaults = {"WATCH_SCORE": 48.0, "ARM_SCORE": 58.0, "TRIGGER_SCORE": 66.0}
    return _f(getattr(settings, key, defaults.get(kind, 60.0)), defaults.get(kind, 60.0))


def _row_metrics(frame: pd.DataFrame, idx: int) -> dict[str, float]:
    row = frame.iloc[idx]
    atr = max(1e-12, _f(row.get("atr_14")))
    o, h, l, c = (_f(row.get(k)) for k in ("open", "high", "low", "close"))
    body = abs(c - o)
    rng = max(1e-12, h - l)
    upper = max(0.0, h - max(o, c))
    lower = max(0.0, min(o, c) - l)
    return {
        "open": o, "high": h, "low": l, "close": c, "atr": atr,
        "body_atr": body / atr,
        "range_atr": rng / atr,
        "upper_wick_body": upper / max(body, atr * 0.03),
        "lower_wick_body": lower / max(body, atr * 0.03),
        "close_pos": (c - l) / rng,
        "volume_ratio": _f(row.get("volume_ratio_20"), 1.0),
        "rsi5": _f(row.get("rsi_5"), 50.0),
        "rsi7": _f(row.get("rsi_7"), 50.0),
        "adx": _f(row.get("adx_14"), 0.0),
        "ema5": _f(row.get("ema_5"), c),
        "ema13": _f(row.get("ema_13"), c),
        "ema20": _f(row.get("ema_20"), c),
        "ema50": _f(row.get("ema_50"), c),
        "ema200": _f(row.get("ema_200"), c),
        "momentum3": _f(row.get("momentum_3_atr"), 0.0),
    }


def _trend_side(m: dict[str, float]) -> int:
    if m["ema5"] > m["ema13"] and m["close"] > m["ema20"]:
        return 1
    if m["ema5"] < m["ema13"] and m["close"] < m["ema20"]:
        return -1
    return 0


def _score_momentum_burst(ms: list[dict[str, float]], dom: float | None) -> tuple[int, float, dict[str, Any]]:
    cur = ms[-1]
    direction = 1 if cur["close"] > cur["open"] else -1
    trend = _trend_side(cur)
    body = _clamp((cur["body_atr"] - 0.25) / 0.75, 0.0, 1.0)
    volume = _clamp((cur["volume_ratio"] - 0.75) / 1.0, 0.0, 1.0)
    adx = _clamp((cur["adx"] - 15.0) / 20.0, 0.0, 1.0)
    momentum = _clamp(abs(cur["momentum3"]) / 1.2, 0.0, 1.0)
    trend_align = 1.0 if trend == direction else 0.35 if trend == 0 else 0.0
    close_strength = cur["close_pos"] if direction == 1 else (1.0 - cur["close_pos"])
    dom_align = 0.5 if dom is None else _clamp(0.5 + direction * _f(dom) * 1.5, 0.0, 1.0)
    score = 100.0 * (0.24*body + 0.17*volume + 0.16*adx + 0.16*momentum + 0.16*trend_align + 0.07*close_strength + 0.04*dom_align)
    if cur["body_atr"] < 0.22 or abs(cur["momentum3"]) < 0.15:
        score *= 0.65
    return direction, score, {"body": body, "volume": volume, "adx": adx, "momentum": momentum, "trend": trend_align}


def _score_impulse_pullback(ms: list[dict[str, float]], dom: float | None) -> tuple[int, float, dict[str, Any]]:
    a, b, c = ms[-3], ms[-2], ms[-1]
    impulse_side = 1 if a["close"] > a["open"] else -1
    resume_side = 1 if c["close"] > c["open"] else -1
    same = 1.0 if resume_side == impulse_side else 0.0
    impulse = _clamp((a["body_atr"] - 0.35) / 0.75, 0.0, 1.0)
    pullback_small = _clamp(1.0 - b["body_atr"] / max(0.20, a["body_atr"]), 0.0, 1.0)
    pullback_direction = 1.0 if ((b["close"]-b["open"]) * impulse_side <= 0) else 0.45
    resume = _clamp((c["body_atr"] - 0.10) / 0.45, 0.0, 1.0)
    structure = 1.0 if (c["close"] > b["high"] if impulse_side == 1 else c["close"] < b["low"]) else 0.45
    trend_align = 1.0 if _trend_side(c) == impulse_side else 0.35
    volume = _clamp((c["volume_ratio"] - 0.65) / 0.9, 0.0, 1.0)
    score = 100.0*(0.22*impulse + 0.15*pullback_small + 0.10*pullback_direction + 0.18*resume + 0.16*structure + 0.12*trend_align + 0.07*volume) * same
    return impulse_side, score, {"impulse": impulse, "pullback": pullback_small, "resume": resume, "structure": structure, "trend": trend_align}


def _score_squeeze_breakout(frame: pd.DataFrame, ms: list[dict[str, float]], idx: int, dom: float | None) -> tuple[int, float, dict[str, Any]]:
    cur = ms[-1]
    prev_ranges = [m["range_atr"] for m in ms[-4:-1]]
    squeeze = _clamp((0.90 - (sum(prev_ranges)/max(1,len(prev_ranges)))) / 0.55, 0.0, 1.0)
    expansion = _clamp((cur["range_atr"] - 0.55) / 0.85, 0.0, 1.0)
    prior = frame.iloc[max(0, idx-6):idx]
    prior_high = _f(prior["high"].max(), cur["high"])
    prior_low = _f(prior["low"].min(), cur["low"])
    if cur["close"] > prior_high:
        side = 1
        breakout = _clamp((cur["close"] - prior_high) / max(cur["atr"]*0.25, 1e-12), 0.0, 1.0)
    elif cur["close"] < prior_low:
        side = -1
        breakout = _clamp((prior_low - cur["close"]) / max(cur["atr"]*0.25, 1e-12), 0.0, 1.0)
    else:
        side = 1 if cur["close"] >= cur["open"] else -1
        breakout = 0.0
    volume = _clamp((cur["volume_ratio"] - 0.80) / 1.0, 0.0, 1.0)
    trend_align = 1.0 if _trend_side(cur) == side else 0.40
    score = 100.0*(0.24*squeeze + 0.22*expansion + 0.28*breakout + 0.14*volume + 0.12*trend_align)
    return side, score, {"squeeze": squeeze, "expansion": expansion, "breakout": breakout, "volume": volume, "trend": trend_align}


def _score_wick_rejection(frame: pd.DataFrame, ms: list[dict[str, float]], idx: int, dom: float | None) -> tuple[int, float, dict[str, Any]]:
    cur = ms[-1]
    prior = frame.iloc[max(0, idx-8):idx]
    prior_low = _f(prior["low"].min(), cur["low"])
    prior_high = _f(prior["high"].max(), cur["high"])
    lower_strength = _clamp(cur["lower_wick_body"] / 3.0, 0.0, 1.0) * cur["close_pos"]
    upper_strength = _clamp(cur["upper_wick_body"] / 3.0, 0.0, 1.0) * (1.0-cur["close_pos"])
    near_low = _clamp(1.0 - abs(cur["low"]-prior_low)/max(cur["atr"],1e-12), 0.0, 1.0)
    near_high = _clamp(1.0 - abs(cur["high"]-prior_high)/max(cur["atr"],1e-12), 0.0, 1.0)
    buy_rsi = _clamp((45.0-cur["rsi5"])/25.0, 0.0, 1.0)
    sell_rsi = _clamp((cur["rsi5"]-55.0)/25.0, 0.0, 1.0)
    buy_score = 100.0*(0.44*lower_strength + 0.24*near_low + 0.18*buy_rsi + 0.08*_clamp(cur["volume_ratio"]/1.4,0,1) + 0.06*(0.5 if dom is None else _clamp(0.5+_f(dom),0,1)))
    sell_score = 100.0*(0.44*upper_strength + 0.24*near_high + 0.18*sell_rsi + 0.08*_clamp(cur["volume_ratio"]/1.4,0,1) + 0.06*(0.5 if dom is None else _clamp(0.5-_f(dom),0,1)))
    if buy_score >= sell_score:
        return 1, buy_score, {"wick": lower_strength, "extreme": near_low, "rsi": buy_rsi}
    return -1, sell_score, {"wick": upper_strength, "extreme": near_high, "rsi": sell_rsi}


def _score_exhaustion_snapback(ms: list[dict[str, float]], dom: float | None) -> tuple[int, float, dict[str, Any]]:
    a, b, c = ms[-3], ms[-2], ms[-1]
    side_a = 1 if a["close"] > a["open"] else -1
    side_b = 1 if b["close"] > b["open"] else -1
    side_c = 1 if c["close"] > c["open"] else -1
    prior_same = 1.0 if side_a == side_b else 0.0
    reversal = 1.0 if side_c == -side_b else 0.0
    extension = _clamp((a["body_atr"] + b["body_atr"] - 0.65) / 1.1, 0.0, 1.0)
    reject_wick = _clamp((c["lower_wick_body"] if side_c == 1 else c["upper_wick_body"])/3.0,0,1)
    rsi_extreme = _clamp((45-c["rsi5"])/25,0,1) if side_c == 1 else _clamp((c["rsi5"]-55)/25,0,1)
    close_strength = c["close_pos"] if side_c == 1 else 1.0-c["close_pos"]
    score = 100.0*(0.30*extension + 0.30*reject_wick + 0.22*rsi_extreme + 0.18*close_strength)*prior_same*reversal
    return side_c, score, {"extension": extension, "wick": reject_wick, "rsi": rsi_extreme, "reversal": reversal}


def detect_playbooks(
    frame: pd.DataFrame,
    symbol: str,
    dom_imbalance: float | None = None,
    connection: sqlite3.Connection | None = None,
) -> list[MicroSetup]:
    """Score specialist 2-5 candle playbooks and apply adaptive symbol thresholds.

    V8 records the *whole* score distribution before filtering. This lets the
    hunter adapt to the current symbol/regime instead of permanently waiting
    for one fixed 84/88/92-style score that may never occur in a quieter market.
    Hard score floors remain in institutional_alpha.dynamic_thresholds().
    """
    if len(frame) < 220:
        return []
    idx = len(frame) - 2
    ms = [_row_metrics(frame, j) for j in range(idx-4, idx+1)]
    bar_time = pd.Timestamp(frame.iloc[idx]["time"]).isoformat()
    cur = ms[-1]
    playbooks: list[tuple[str, int, float, dict[str, Any]]] = []
    playbooks.append(("momentum_burst", *_score_momentum_burst(ms, dom_imbalance)))
    playbooks.append(("impulse_pullback_resume", *_score_impulse_pullback(ms, dom_imbalance)))
    playbooks.append(("squeeze_breakout", *_score_squeeze_breakout(frame, ms, idx, dom_imbalance)))
    playbooks.append(("wick_rejection", *_score_wick_rejection(frame, ms, idx, dom_imbalance)))
    playbooks.append(("exhaustion_snapback", *_score_exhaustion_snapback(ms, dom_imbalance)))

    if connection is not None:
        institutional_alpha.record_score_samples(
            connection,
            symbol,
            bar_time,
            [(name, side, round(_clamp(score,0.0,100.0),2)) for name,side,score,_ in playbooks if side in (-1,1)],
        )
        thresholds = institutional_alpha.dynamic_thresholds(connection, symbol)
        watch_score = float(thresholds["watch"])
        arm_score = float(thresholds["arm"])
        trigger_score = float(thresholds["trigger"])
    else:
        watch_score = _symbol_threshold(symbol, "WATCH_SCORE")
        arm_score = _symbol_threshold(symbol, "ARM_SCORE")
        trigger_score = _symbol_threshold(symbol, "TRIGGER_SCORE")
        thresholds = {"watch":watch_score,"arm":arm_score,"trigger":trigger_score,"samples":0}

    results: list[MicroSetup] = []
    continuation_playbooks = {"momentum_burst", "impulse_pullback_resume", "squeeze_breakout"}
    reversal_playbooks = {"wick_rejection", "exhaustion_snapback"}
    current_trend = _trend_side(cur)
    for playbook, side, score, evidence in playbooks:
        raw_score = float(score)
        if side in (-1, 1) and playbook in continuation_playbooks:
            if current_trend == side:
                score += 4.0
            elif current_trend == -side:
                score *= 0.82
        elif side in (-1, 1) and playbook in reversal_playbooks:
            reversal_strength = max(_f(evidence.get("wick")), _f(evidence.get("rsi")), _f(evidence.get("extension")), _f(evidence.get("reversal")))
            if current_trend == -side and reversal_strength >= 0.70:
                score += 3.0
            elif current_trend == -side and reversal_strength < 0.45:
                score *= 0.86
        score = round(_clamp(score, 0.0, 100.0), 2)
        evidence = {**evidence, "raw_score": round(_clamp(raw_score,0.0,100.0),2), "trend_side": int(current_trend), "alignment_adjusted": score}
        if score < watch_score or side not in (-1, 1):
            continue
        if score >= trigger_score:
            phase = "TRIGGER"
        elif score >= arm_score:
            phase = "ARM"
        else:
            phase = "WATCH"

        buffer_atr = _f(getattr(settings, "V7_MICRO_HUNTER_TRIGGER_BUFFER_ATR", 0.03), 0.03)
        if side == 1:
            trigger = cur["high"] + cur["atr"] * buffer_atr
            invalidation = cur["low"] - cur["atr"] * 0.05
        else:
            trigger = cur["low"] - cur["atr"] * buffer_atr
            invalidation = cur["high"] + cur["atr"] * 0.05

        profile = {
            "momentum_burst": (0.55, 1.05, 4),
            "impulse_pullback_resume": (0.60, 1.20, 6),
            "squeeze_breakout": (0.65, 1.35, 6),
            "wick_rejection": (0.55, 0.95, 5),
            "exhaustion_snapback": (0.60, 0.90, 4),
        }[playbook]
        take_atr = profile[1] * (1.0 + max(0.0, score-70.0)/120.0)
        risk_mult = _clamp(0.50 + (score-55.0)/90.0, 0.35, 0.90)
        context = {
            "evidence": evidence,
            "regime_hint": "trend" if _trend_side(cur) else "neutral",
            "bar_body_atr": round(cur["body_atr"], 3),
            "bar_range_atr": round(cur["range_atr"], 3),
            "volume_ratio": round(cur["volume_ratio"], 3),
            "adx": round(cur["adx"], 2),
            "rsi5": round(cur["rsi5"], 2),
            "momentum3_atr": round(cur["momentum3"], 3),
            "dom": None if dom_imbalance is None else round(_f(dom_imbalance), 3),
            "exit_profile": PLAYBOOK_EXIT_PROFILES.get(playbook, {}),
            "adaptive_thresholds": thresholds,
        }
        results.append(MicroSetup(
            symbol=str(symbol), playbook=playbook, side=int(side), phase=phase,
            score=score, bar_time=bar_time, trigger_price=float(trigger),
            invalidation_price=float(invalidation), stop_atr=float(profile[0]),
            take_atr=float(take_atr), max_hold_bars=int(profile[2]),
            preferred_families=PLAYBOOK_FAMILIES[playbook],
            risk_multiplier=float(risk_mult), context=context,
        ))
    return sorted(results, key=lambda x: x.score, reverse=True)

def _persist_setup(connection: sqlite3.Connection, setup: MicroSetup, phase: str | None = None) -> int:
    ensure_tables(connection)
    now = _utc_now()
    actual_phase = str(phase or setup.phase)
    connection.execute(
        """
        INSERT INTO micro_hunter_setups(
            created_at,updated_at,symbol,bar_time,playbook,side,phase,score,
            trigger_price,invalidation_price,stop_atr,take_atr,max_hold_bars,
            preferred_families_json,risk_multiplier,context_json,triggered_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(symbol,bar_time,playbook,side) DO UPDATE SET
            updated_at=excluded.updated_at,phase=excluded.phase,score=excluded.score,
            trigger_price=excluded.trigger_price,invalidation_price=excluded.invalidation_price,
            stop_atr=excluded.stop_atr,take_atr=excluded.take_atr,
            max_hold_bars=excluded.max_hold_bars,
            preferred_families_json=excluded.preferred_families_json,
            risk_multiplier=excluded.risk_multiplier,context_json=excluded.context_json,
            triggered_at=COALESCE(micro_hunter_setups.triggered_at,excluded.triggered_at)
        """,
        (
            now, now, setup.symbol, setup.bar_time, setup.playbook, int(setup.side),
            actual_phase, float(setup.score), float(setup.trigger_price),
            float(setup.invalidation_price), float(setup.stop_atr), float(setup.take_atr),
            int(setup.max_hold_bars), json.dumps(list(setup.preferred_families)),
            float(setup.risk_multiplier), json.dumps(setup.context, separators=(",",":"), default=str),
            now if actual_phase == "TRIGGERED" else None,
        ),
    )
    row = connection.execute(
        "SELECT id FROM micro_hunter_setups WHERE symbol=? AND bar_time=? AND playbook=? AND side=?",
        (setup.symbol, setup.bar_time, setup.playbook, int(setup.side)),
    ).fetchone()
    return int(row["id"] if row else 0)


def observe_closed_bar(
    connection: sqlite3.Connection,
    symbol: str,
    frame: pd.DataFrame,
    regime: str,
    dom_imbalance: float | None = None,
) -> MicroSetup | None:
    """Observe one newly closed M1 bar and update WATCH/ARM/TRIGGER state."""
    if not bool(getattr(settings, "V7_MICRO_HUNTER_ENABLED", True)):
        return None
    ensure_tables(connection)
    setups = detect_playbooks(frame, symbol, dom_imbalance, connection=connection)
    if not setups:
        return None
    keep = max(1, int(getattr(settings, "V7_MICRO_HUNTER_MAX_STATES_PER_BAR", 3)))
    for setup in setups[:keep]:
        context = {**setup.context, "regime": str(regime)}
        setup = MicroSetup(**{**setup.as_dict(), "preferred_families": setup.preferred_families, "context": context})
        phase = "TRIGGERED" if setup.phase == "TRIGGER" else setup.phase
        _persist_setup(connection, setup, phase=phase)
        if setup.phase == "TRIGGER":
            return setup
    return None


def _row_to_setup(row: sqlite3.Row, phase: str = "TRIGGER") -> MicroSetup:
    try:
        preferred = tuple(json.loads(str(row["preferred_families_json"] or "[]")))
    except Exception:
        preferred = PLAYBOOK_FAMILIES.get(str(row["playbook"]), ("micro_momentum",))
    try:
        context = json.loads(str(row["context_json"] or "{}"))
    except Exception:
        context = {}
    return MicroSetup(
        symbol=str(row["symbol"]), playbook=str(row["playbook"]), side=int(row["side"]),
        phase=phase, score=_f(row["score"]), bar_time=str(row["bar_time"]),
        trigger_price=_f(row["trigger_price"]), invalidation_price=_f(row["invalidation_price"]),
        stop_atr=_f(row["stop_atr"], 0.60), take_atr=_f(row["take_atr"], 1.0),
        max_hold_bars=int(row["max_hold_bars"] or 5), preferred_families=preferred,
        risk_multiplier=_f(row["risk_multiplier"], 0.70), context=context,
    )


def intrabar_trigger(
    connection: sqlite3.Connection,
    symbol: str,
    frame: pd.DataFrame,
    tick: Any,
) -> MicroSetup | None:
    """High-attention mode: once a pattern is ARMED, watch live ticks for continuation.

    This is intentionally cheap. It does not call Luna or ML itself; it only turns
    an already-observed 2-3 candle setup into a candidate. All existing hard gates
    remain downstream.
    """
    if not bool(getattr(settings, "V7_MICRO_HUNTER_ENABLED", True)):
        return None
    ensure_tables(connection)
    ttl = max(30, int(getattr(settings, "V7_MICRO_HUNTER_ARM_TTL_SECONDS", 150)))
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=ttl)).isoformat()
    rows = connection.execute(
        """
        SELECT * FROM micro_hunter_setups
        WHERE symbol=? AND phase='ARM' AND updated_at>=?
        ORDER BY score DESC,id DESC LIMIT 5
        """,
        (str(symbol), cutoff),
    ).fetchall()
    if not rows:
        return None
    bid = _f(getattr(tick, "bid", 0.0)); ask = _f(getattr(tick, "ask", 0.0))
    for row in rows:
        side = int(row["side"])
        mark = ask if side == 1 else bid
        trigger = _f(row["trigger_price"])
        invalid = _f(row["invalidation_price"])
        invalidated = (mark <= invalid if side == 1 else mark >= invalid)
        if invalidated:
            connection.execute(
                "UPDATE micro_hunter_setups SET phase='INVALIDATED',updated_at=?,resolved_at=? WHERE id=?",
                (_utc_now(), _utc_now(), int(row["id"])),
            )
            continue
        crossed = (mark >= trigger if side == 1 else mark <= trigger)
        if not crossed:
            continue
        setup = _row_to_setup(row, phase="TRIGGER")
        connection.execute(
            "UPDATE micro_hunter_setups SET phase='TRIGGERED',updated_at=?,triggered_at=? WHERE id=?",
            (_utc_now(), _utc_now(), int(row["id"])),
        )
        return setup
    return None


def mark_wait_spread(
    connection: sqlite3.Connection,
    setup: MicroSetup,
    spread: float,
    atr_value: float,
) -> None:
    """Preserve a valid trigger briefly when only the live spread is too wide.

    This is deliberately not an approval bypass.  The setup is merely parked;
    on resume the complete deterministic/ML/Luna/risk pipeline runs again.
    """
    if not bool(getattr(settings, "V8_SPREAD_WAIT_ENABLED", True)):
        return
    ensure_tables(connection)
    row = connection.execute(
        """
        SELECT id,context_json FROM micro_hunter_setups
        WHERE symbol=? AND bar_time=? AND playbook=? AND side=?
        ORDER BY id DESC LIMIT 1
        """,
        (setup.symbol, setup.bar_time, setup.playbook, int(setup.side)),
    ).fetchone()
    if not row:
        return
    try:
        context = json.loads(str(row["context_json"] or "{}"))
    except Exception:
        context = {}
    context["spread_wait"] = {
        "started_at": _utc_now(),
        "spread": _f(spread),
        "atr": _f(atr_value),
        "trigger_price": _f(setup.trigger_price),
    }
    connection.execute(
        """
        UPDATE micro_hunter_setups
        SET phase='WAIT_SPREAD',updated_at=?,context_json=?
        WHERE id=?
        """,
        (_utc_now(), json.dumps(context, separators=(",", ":")), int(row["id"])),
    )


def spread_wait_resume(
    connection: sqlite3.Connection,
    symbol: str,
    frame: pd.DataFrame,
    tick: Any,
) -> MicroSetup | None:
    """Resume only after spread normalizes while the original trigger is fresh.

    A missed move is never chased: invalidation, excessive drift, TTL expiry or
    (optionally) a new closed bar expires the parked trigger locally at zero API
    cost.
    """
    if not bool(getattr(settings, "V8_SPREAD_WAIT_ENABLED", True)):
        return None
    ensure_tables(connection)
    ttl = max(5, int(getattr(settings, "V8_SPREAD_WAIT_TTL_SECONDS", 35)))
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=ttl)).isoformat()
    rows = connection.execute(
        """
        SELECT * FROM micro_hunter_setups
        WHERE symbol=? AND phase='WAIT_SPREAD' AND updated_at>=?
        ORDER BY score DESC,id DESC LIMIT 3
        """,
        (str(symbol), cutoff),
    ).fetchall()
    # Expire older parked setups so status is explicit.
    connection.execute(
        """
        UPDATE micro_hunter_setups SET phase='SPREAD_EXPIRED',updated_at=?,resolved_at=?
        WHERE symbol=? AND phase='WAIT_SPREAD' AND updated_at<?
        """,
        (_utc_now(), _utc_now(), str(symbol), cutoff),
    )
    if not rows:
        return None

    bid = _f(getattr(tick, "bid", 0.0)); ask = _f(getattr(tick, "ask", 0.0))
    if bid <= 0 or ask <= 0:
        return None
    spread = max(0.0, ask - bid)
    row_bar_time = str(frame.iloc[-2].get("time")) if len(frame) >= 2 else ""
    current_bar_iso = ""
    try:
        current_bar_iso = pd.Timestamp(frame.iloc[-2]["time"]).isoformat()
    except Exception:
        current_bar_iso = row_bar_time
    atr = max(1e-12, _f(frame.iloc[-2].get("atr_14")))
    max_spread = atr * _f(getattr(settings, "MAX_SPREAD_ATR_FRACTION", 0.18), 0.18)
    max_drift = atr * _f(getattr(settings, "V8_SPREAD_WAIT_MAX_DRIFT_ATR", 0.18), 0.18)

    for row in rows:
        setup = _row_to_setup(row, phase="TRIGGER")
        mark = ask if int(setup.side) == 1 else bid
        # Do not carry a micro trigger across a new closed M1 bar unless explicitly enabled.
        if bool(getattr(settings, "V8_SPREAD_WAIT_REQUIRE_SAME_BAR", True)):
            try:
                if pd.Timestamp(setup.bar_time).isoformat() != pd.Timestamp(current_bar_iso).isoformat():
                    connection.execute(
                        "UPDATE micro_hunter_setups SET phase='SPREAD_EXPIRED',updated_at=?,resolved_at=? WHERE id=?",
                        (_utc_now(), _utc_now(), int(row["id"])),
                    )
                    continue
            except Exception:
                pass
        invalidated = (mark <= setup.invalidation_price if setup.side == 1 else mark >= setup.invalidation_price)
        if invalidated:
            connection.execute(
                "UPDATE micro_hunter_setups SET phase='INVALIDATED',updated_at=?,resolved_at=? WHERE id=?",
                (_utc_now(), _utc_now(), int(row["id"])),
            )
            continue
        favorable_drift = (mark - setup.trigger_price) if setup.side == 1 else (setup.trigger_price - mark)
        if favorable_drift > max_drift:
            connection.execute(
                "UPDATE micro_hunter_setups SET phase='SPREAD_MISSED',updated_at=?,resolved_at=? WHERE id=?",
                (_utc_now(), _utc_now(), int(row["id"])),
            )
            continue
        if spread > max_spread:
            continue
        try:
            context = json.loads(str(row["context_json"] or "{}"))
        except Exception:
            context = {}
        wait_info = context.get("spread_wait") if isinstance(context.get("spread_wait"), dict) else {}
        wait_info = {**wait_info, "resumed_at": _utc_now(), "resumed_spread": spread, "resumed_atr": atr}
        context["spread_wait"] = wait_info
        connection.execute(
            "UPDATE micro_hunter_setups SET phase='TRIGGERED',updated_at=?,context_json=? WHERE id=?",
            (_utc_now(), json.dumps(context, separators=(",", ":")), int(row["id"])),
        )
        return MicroSetup(**{**setup.as_dict(), "preferred_families": setup.preferred_families, "context": context})
    return None


def select_carrier(candidates: Iterable[tuple[Any, float]], setup: MicroSetup) -> tuple[Any | None, float]:
    ranked = list(candidates)
    for family in setup.preferred_families:
        matches = [(d, s) for d, s in ranked if str(getattr(d, "family", "")) == family]
        if matches:
            return max(matches, key=lambda x: x[1])
    scalp = [(d, s) for d, s in ranked if str(getattr(d, "family", "")) in {
        "super_scalp","scalp","micro_momentum","pullback_scalp","breakout_scalp","mean_revert_scalp"
    }]
    if scalp:
        return max(scalp, key=lambda x: x[1])
    # V7 hunter is scalp-only by design. A non-scalp trend/reversion carrier may
    # still execute through the legacy pipeline, but it cannot be repurposed as
    # a micro-scalp trigger.
    return None, 0.0


def bind_execution(
    connection: sqlite3.Connection,
    setup: MicroSetup,
    strategy_id: int,
    execution_tier: str,
) -> None:
    ensure_tables(connection)
    connection.execute(
        """
        UPDATE micro_hunter_setups
        SET strategy_id=?,execution_tier=?,updated_at=?
        WHERE symbol=? AND bar_time=? AND playbook=? AND side=?
        """,
        (int(strategy_id), str(execution_tier), _utc_now(), setup.symbol, setup.bar_time, setup.playbook, int(setup.side)),
    )


def record_demo_outcome(
    connection: sqlite3.Connection,
    *,
    symbol: str,
    side: int,
    reward_r: float,
    mfe_r: float,
    mae_r: float,
    context: dict[str, Any] | None,
) -> None:
    if not isinstance(context, dict):
        return
    hunter = context.get("micro_hunter")
    if not isinstance(hunter, dict) or not hunter.get("playbook"):
        return
    ensure_tables(connection)
    playbook = str(hunter.get("playbook"))
    reward = _f(reward_r)
    alpha = _clamp(_f(getattr(settings, "V7_MICRO_PLAYBOOK_EWMA_ALPHA", 0.22), 0.22), 0.01, 1.0)
    row = connection.execute(
        "SELECT * FROM micro_playbook_memory WHERE symbol=? AND playbook=? AND side=?",
        (str(symbol), playbook, int(side)),
    ).fetchone()
    if row:
        obs = int(row["observations"] or 0) + 1
        ewma = alpha * reward + (1.0-alpha) * _f(row["reward_ewma"])
        connection.execute(
            """
            UPDATE micro_playbook_memory SET observations=?,wins=?,losses=?,reward_sum=?,reward_ewma=?,
                   mfe_sum=?,mae_sum=?,updated_at=?
            WHERE symbol=? AND playbook=? AND side=?
            """,
            (
                obs, int(row["wins"] or 0)+(1 if reward>0 else 0), int(row["losses"] or 0)+(1 if reward<=0 else 0),
                _f(row["reward_sum"])+reward, ewma, _f(row["mfe_sum"])+max(0.0,_f(mfe_r)),
                _f(row["mae_sum"])+max(0.0,_f(mae_r)), _utc_now(), str(symbol), playbook, int(side),
            ),
        )
    else:
        connection.execute(
            """
            INSERT INTO micro_playbook_memory(symbol,playbook,side,observations,wins,losses,reward_sum,reward_ewma,mfe_sum,mae_sum,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (str(symbol),playbook,int(side),1,1 if reward>0 else 0,1 if reward<=0 else 0,reward,reward,max(0.0,_f(mfe_r)),max(0.0,_f(mae_r)),_utc_now()),
        )
    # Resolve most recent triggered setup matching this playbook.
    connection.execute(
        """
        UPDATE micro_hunter_setups SET phase='RESOLVED',resolved_at=?,updated_at=?,outcome_r=?
        WHERE id=(SELECT id FROM micro_hunter_setups
                  WHERE symbol=? AND playbook=? AND side=? AND phase IN ('TRIGGERED','EXECUTED')
                  ORDER BY id DESC LIMIT 1)
        """,
        (_utc_now(), _utc_now(), reward, str(symbol), playbook, int(side)),
    )


def memory_snapshot(connection: sqlite3.Connection, symbol: str, playbook: str, side: int) -> dict[str, Any]:
    ensure_tables(connection)
    row = connection.execute(
        "SELECT * FROM micro_playbook_memory WHERE symbol=? AND playbook=? AND side=?",
        (str(symbol), str(playbook), int(side)),
    ).fetchone()
    if not row:
        return {"observations": 0, "reward_mean": 0.0, "reward_ewma": 0.0, "win_rate": 0.5, "mfe_mean": 0.0, "mae_mean": 0.0}
    n = max(1, int(row["observations"] or 0))
    return {
        "observations": int(row["observations"] or 0),
        "reward_mean": _f(row["reward_sum"])/n,
        "reward_ewma": _f(row["reward_ewma"]),
        "win_rate": int(row["wins"] or 0)/n,
        "mfe_mean": _f(row["mfe_sum"])/n,
        "mae_mean": _f(row["mae_sum"])/n,
    }


def execution_risk_multiplier(connection: sqlite3.Connection, setup: MicroSetup) -> tuple[float, dict[str, Any]]:
    """Soft de-risking only; never raises risk above the setup's own <=1.0 multiplier."""
    mem = memory_snapshot(connection, setup.symbol, setup.playbook, setup.side)
    mult = _clamp(setup.risk_multiplier, 0.35, 1.0)
    n = int(mem.get("observations") or 0)
    if n >= 8 and _f(mem.get("reward_ewma")) < -0.10:
        mult *= 0.45
    elif n >= 6 and _f(mem.get("reward_ewma")) < 0.0:
        mult *= 0.70
    elif n >= 12 and _f(mem.get("reward_ewma")) > 0.15 and _f(mem.get("reward_mean")) > 0.08:
        mult *= 1.0  # never upscale from hunter memory alone
    return _clamp(mult, 0.25, 1.0), mem


def status_summary(connection: sqlite3.Connection) -> dict[str, Any]:
    ensure_tables(connection)
    recent = connection.execute(
        """
        SELECT symbol,playbook,side,phase,score,bar_time,triggered_at,outcome_r
        FROM micro_hunter_setups ORDER BY id DESC LIMIT 30
        """
    ).fetchall()
    memory = connection.execute(
        """
        SELECT symbol,playbook,side,observations,wins,losses,reward_sum,reward_ewma,mfe_sum,mae_sum
        FROM micro_playbook_memory ORDER BY observations DESC,reward_ewma DESC
        """
    ).fetchall()
    return {
        "recent": [dict(x) for x in recent],
        "memory": [dict(x) for x in memory],
    }


def mark_executed(connection: sqlite3.Connection, setup: MicroSetup) -> None:
    ensure_tables(connection)
    connection.execute(
        """
        UPDATE micro_hunter_setups
        SET phase='EXECUTED',updated_at=?
        WHERE symbol=? AND bar_time=? AND playbook=? AND side=?
        """,
        (_utc_now(), setup.symbol, setup.bar_time, setup.playbook, int(setup.side)),
    )
