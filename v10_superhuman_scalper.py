from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd

import settings


def _f(value: Any, default: float = 0.0) -> float:
    try:
        x = float(value)
        return x if math.isfinite(x) else float(default)
    except (TypeError, ValueError):
        return float(default)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, float(value)))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class V10Setup:
    symbol: str
    playbook: str
    side: int
    score: float
    phase: str = "TRIGGERED"
    stop_atr: float = 0.65
    take_atr: float = 1.15
    max_hold_bars: int = 5
    context: dict[str, Any] = field(default_factory=dict)
    v10_synthetic: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "playbook": self.playbook,
            "side": int(self.side),
            "score": round(float(self.score), 2),
            "phase": self.phase,
            "stop_atr": round(float(self.stop_atr), 4),
            "take_atr": round(float(self.take_atr), 4),
            "max_hold_bars": int(self.max_hold_bars),
            "context": self.context,
            "v10_synthetic": True,
        }


def ensure_tables(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS v10_expert_events(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            symbol TEXT NOT NULL,
            regime TEXT,
            playbook TEXT,
            side INTEGER,
            score REAL,
            accepted INTEGER NOT NULL DEFAULT 0,
            expert_scores_json TEXT,
            context_json TEXT
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_v10_expert_events_time ON v10_expert_events(timestamp,symbol)"
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS v10_router_events(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            symbol TEXT NOT NULL,
            hunter_playbook TEXT,
            hunter_side INTEGER,
            hunter_score REAL,
            expert_playbook TEXT,
            expert_side INTEGER,
            expert_score REAL,
            chosen_source TEXT NOT NULL,
            chosen_playbook TEXT,
            chosen_side INTEGER,
            chosen_score REAL,
            reason TEXT
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_v10_router_events_time ON v10_router_events(timestamp,symbol)"
    )


def _rolling_vwap(frame: pd.DataFrame, n: int = 60) -> float:
    f = frame.tail(max(5, int(n)))
    typical = (f["high"].astype(float) + f["low"].astype(float) + f["close"].astype(float)) / 3.0
    if "tick_volume" not in f.columns:
        return _f(typical.mean(), _f(f["close"].iloc[-1]))
    vol = f["tick_volume"].astype(float).clip(lower=0.0)
    total = _f(vol.sum(), 0.0)
    if total <= 0:
        return _f(typical.mean(), _f(f["close"].iloc[-1]))
    return _f((typical * vol).sum() / total, _f(f["close"].iloc[-1]))


def _bandwidth_series(frame: pd.DataFrame, n: int = 20) -> pd.Series:
    close = frame["close"].astype(float)
    ma = close.rolling(n).mean().replace(0, np.nan)
    sd = close.rolling(n).std()
    return (4.0 * sd / ma).replace([np.inf, -np.inf], np.nan)


def _regime_context(frame: pd.DataFrame) -> dict[str, Any]:
    row = frame.iloc[-2]
    atr = max(1e-12, _f(row.get("atr_14")))
    e20 = _f(row.get("ema_20"), _f(row.get("close")))
    e50 = _f(row.get("ema_50"), e20)
    trend_strength = abs(e20 - e50) / atr
    trend_side = 1 if e20 > e50 else -1 if e20 < e50 else 0
    atr_ratio = _f(row.get("atr_ratio"), 0.0)
    hist = frame["atr_ratio"].astype(float).dropna().tail(240) if "atr_ratio" in frame.columns else pd.Series(dtype=float)
    vol_pct = 0.5
    if len(hist) >= 30:
        vol_pct = _f((hist <= atr_ratio).mean(), 0.5)
    if trend_strength >= 0.80 and vol_pct >= 0.65:
        regime = "expansion_trend"
    elif trend_strength >= 0.55:
        regime = "trend"
    elif vol_pct <= 0.35:
        regime = "compression_range"
    elif vol_pct >= 0.75:
        regime = "volatile_range"
    else:
        regime = "range"
    return {
        "regime": regime,
        "atr": atr,
        "trend_strength": round(trend_strength, 4),
        "trend_side": trend_side,
        "volatility_percentile": round(vol_pct, 4),
    }


def _score_components(frame: pd.DataFrame, dom: float | None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if len(frame) < 80:
        return [], {"reason": "insufficient_bars"}
    row = frame.iloc[-2]
    prev = frame.iloc[-3]
    hist20 = frame.iloc[-22:-2]
    hist30 = frame.iloc[-32:-2]
    atr = max(1e-12, _f(row.get("atr_14")))
    close = _f(row.get("close")); open_ = _f(row.get("open")); high = _f(row.get("high")); low = _f(row.get("low"))
    prev_close = _f(prev.get("close")); prev_high = _f(prev.get("high")); prev_low = _f(prev.get("low"))
    e20 = _f(row.get("ema_20"), close); e50 = _f(row.get("ema_50"), close)
    pe20 = _f(prev.get("ema_20"), prev_close)
    rsi5 = _f(row.get("rsi_5"), 50.0); rsi7 = _f(row.get("rsi_7"), 50.0)
    mom3 = _f(row.get("momentum_3_atr"), 0.0)
    body = _f(row.get("bar_body_atr"), abs(close-open_)/atr)
    vr = _f(row.get("volume_ratio_20"), 1.0)
    lower_wick = max(0.0, min(open_, close) - low) / atr
    upper_wick = max(0.0, high - max(open_, close)) / atr
    prior_high20 = _f(hist20["high"].max(), high)
    prior_low20 = _f(hist20["low"].min(), low)
    prior_high30 = _f(hist30["high"].max(), high)
    prior_low30 = _f(hist30["low"].min(), low)
    range20 = max(atr, prior_high20-prior_low20)
    vwap = _rolling_vwap(frame.iloc[:-1], 60)
    vwap_dist = (close - vwap) / atr
    trend_strength = abs(e20-e50)/atr
    domv = _clamp(_f(dom, 0.0), -1.0, 1.0) if dom is not None else 0.0
    bw = _bandwidth_series(frame)
    bw_now = _f(bw.iloc[-2], 0.0)
    bw_hist = bw.iloc[-102:-2].dropna()
    bw_pct = _f((bw_hist <= bw_now).mean(), 0.5) if len(bw_hist) >= 20 else 0.5

    candidates: list[dict[str, Any]] = []

    def add(name: str, side: int, raw: float, stop: float, take: float, hold: int, why: dict[str, Any]) -> None:
        if side not in (-1, 1):
            return
        score = _clamp(raw, 0.0, 100.0)
        candidates.append({
            "playbook": name, "side": side, "score": score,
            "stop_atr": stop, "take_atr": take, "max_hold_bars": hold,
            "why": why,
        })

    # 1) Trend pullback continuation: direction + pullback into EMA/fair value + resume.
    tside = 1 if e20 > e50 else -1
    touch = (prev_low <= pe20 + 0.28*atr) if tside == 1 else (prev_high >= pe20 - 0.28*atr)
    resumed = close > e20 and close > prev_close if tside == 1 else close < e20 and close < prev_close
    aligned_mom = mom3*tside
    raw = 42 + min(22, 18*trend_strength) + (14 if touch else 0) + (12 if resumed else 0) + _clamp(aligned_mom*8, -8, 8) + _clamp(domv*tside*5, -5, 5)
    if trend_strength >= 0.38 and touch and resumed:
        add("trend_pullback", tside, raw, 0.66, 1.22, 6, {"trend_strength":trend_strength,"touch":touch,"momentum":aligned_mom})

    # 2) Momentum breakout/expansion.
    bside = 1 if close > prior_high20 else -1 if close < prior_low20 else 0
    if bside:
        extension = ((close-prior_high20)/atr) if bside == 1 else ((prior_low20-close)/atr)
        raw = 50 + _clamp(extension*30, 0, 18) + _clamp(abs(mom3)*10, 0, 12) + _clamp((vr-1.0)*14, -5, 12) + _clamp((body-0.25)*16, -4, 10) + _clamp(domv*bside*6, -6, 6)
        add("momentum_breakout", bside, raw, 0.72, 1.48, 6, {"extension_atr":extension,"volume_ratio":vr,"body_atr":body})

    # 3) Compression -> squeeze release.
    short_high = _f(frame["high"].iloc[-10:-2].max(), prior_high20)
    short_low = _f(frame["low"].iloc[-10:-2].min(), prior_low20)
    sside = 1 if close > short_high else -1 if close < short_low else 0
    if sside and bw_pct <= 0.42:
        release = ((close-short_high)/atr) if sside == 1 else ((short_low-close)/atr)
        raw = 58 + (14*(0.42-bw_pct)/0.42) + _clamp(release*28, 0, 14) + _clamp(abs(mom3)*8,0,10) + _clamp(domv*sside*5,-5,5)
        add("squeeze_release", sside, raw, 0.70, 1.55, 7, {"bandwidth_percentile":bw_pct,"release_atr":release})

    # 4) Liquidity sweep + reclaim of a visible recent extreme.
    long_sweep = low < prior_low20 - 0.02*atr and close > prior_low20 and lower_wick >= 0.20
    short_sweep = high > prior_high20 + 0.02*atr and close < prior_high20 and upper_wick >= 0.20
    if long_sweep:
        depth = (prior_low20-low)/atr
        raw = 63 + _clamp(depth*18,0,10) + _clamp(lower_wick*16,0,12) + _clamp((45-rsi5)*0.25,-4,6) + _clamp(domv*5,-5,5)
        add("liquidity_sweep_reclaim", 1, raw, 0.56, 1.08, 5, {"sweep_depth_atr":depth,"wick_atr":lower_wick})
    if short_sweep:
        depth = (high-prior_high20)/atr
        raw = 63 + _clamp(depth*18,0,10) + _clamp(upper_wick*16,0,12) + _clamp((rsi5-55)*0.25,-4,6) + _clamp(-domv*5,-5,5)
        add("liquidity_sweep_reclaim", -1, raw, 0.56, 1.08, 5, {"sweep_depth_atr":depth,"wick_atr":upper_wick})

    # 5) Failed breakout / trap reversal: previous bar broke, current bar re-entered range.
    prev_hist = frame.iloc[-23:-3]
    p_high = _f(prev_hist["high"].max(), prior_high20); p_low = _f(prev_hist["low"].min(), prior_low20)
    failed_up = prev_high > p_high + 0.03*atr and close < p_high and close < open_
    failed_dn = prev_low < p_low - 0.03*atr and close > p_low and close > open_
    if failed_up:
        raw = 65 + _clamp(upper_wick*14,0,9) + _clamp(-mom3*7,-4,8) + _clamp(-domv*5,-5,5)
        add("failed_breakout_reversal", -1, raw, 0.60, 1.12, 5, {"failed_level":p_high,"upper_wick_atr":upper_wick})
    if failed_dn:
        raw = 65 + _clamp(lower_wick*14,0,9) + _clamp(mom3*7,-4,8) + _clamp(domv*5,-5,5)
        add("failed_breakout_reversal", 1, raw, 0.60, 1.12, 5, {"failed_level":p_low,"lower_wick_atr":lower_wick})

    # 6) VWAP/fair-value mean reversion when trend is weak enough.
    if trend_strength <= 0.58 and abs(vwap_dist) >= 0.55:
        side = -1 if vwap_dist > 0 else 1
        turn = (close < prev_close) if side == -1 else (close > prev_close)
        raw = 54 + _clamp(abs(vwap_dist)*14,0,18) + (10 if turn else 0) + _clamp((rsi5-50)*0.20*side*-1,-5,7) + _clamp(domv*side*4,-4,4)
        if turn:
            add("vwap_mean_reversion", side, raw, 0.58, 0.95, 5, {"vwap_dist_atr":vwap_dist,"trend_strength":trend_strength})

    # 7) Range edge rotation.
    if trend_strength <= 0.42:
        dist_low = (close-prior_low30)/max(1e-12, prior_high30-prior_low30)
        dist_high = (prior_high30-close)/max(1e-12, prior_high30-prior_low30)
        if dist_low <= 0.14 and rsi7 <= 42 and close >= open_:
            raw = 60 + _clamp((0.14-dist_low)*70,0,9) + _clamp((42-rsi7)*0.35,0,8) + _clamp(domv*4,-4,4)
            add("range_edge_rotation", 1, raw, 0.52, 0.86, 4, {"range_position":dist_low,"rsi7":rsi7})
        if dist_high <= 0.14 and rsi7 >= 58 and close <= open_:
            raw = 60 + _clamp((0.14-dist_high)*70,0,9) + _clamp((rsi7-58)*0.35,0,8) + _clamp(-domv*4,-4,4)
            add("range_edge_rotation", -1, raw, 0.52, 0.86, 4, {"range_position":1.0-dist_high,"rsi7":rsi7})

    # 8) Exhaustion snapback after an outsized micro move + rejection.
    if mom3 <= -1.05 and rsi5 <= 28 and lower_wick >= 0.16 and close > low + 0.45*(high-low):
        raw = 62 + _clamp(abs(mom3)*7,0,12) + _clamp((28-rsi5)*0.4,0,8) + _clamp(lower_wick*12,0,8) + _clamp(domv*4,-4,4)
        add("exhaustion_snapback", 1, raw, 0.62, 1.02, 5, {"momentum_3_atr":mom3,"rsi5":rsi5,"wick_atr":lower_wick})
    if mom3 >= 1.05 and rsi5 >= 72 and upper_wick >= 0.16 and close < low + 0.55*(high-low):
        raw = 62 + _clamp(abs(mom3)*7,0,12) + _clamp((rsi5-72)*0.4,0,8) + _clamp(upper_wick*12,0,8) + _clamp(-domv*4,-4,4)
        add("exhaustion_snapback", -1, raw, 0.62, 1.02, 5, {"momentum_3_atr":mom3,"rsi5":rsi5,"wick_atr":upper_wick})

    context = _regime_context(frame)
    context.update({
        "close": close, "atr": atr, "rsi5": rsi5, "rsi7": rsi7,
        "momentum_3_atr": mom3, "volume_ratio": vr, "body_atr": body,
        "vwap": vwap, "vwap_dist_atr": round(vwap_dist,4), "dom": domv,
        "bandwidth_percentile": round(bw_pct,4), "range20_atr": round(range20/atr,4),
    })
    return candidates, context



def choose_parallel_setup(
    hunter_setup: Any | None,
    expert_setup: Any | None,
) -> tuple[Any | None, str, str]:
    """Choose between legacy hunter and V10 expert setup."""
    if hunter_setup is None and expert_setup is None:
        return None, "none", "no_setup"
    if hunter_setup is None:
        return expert_setup, "v10", "v10_only"
    if expert_setup is None:
        return hunter_setup, "hunter", "hunter_only"

    hs = _f(getattr(hunter_setup, "score", 0.0))
    es = _f(getattr(expert_setup, "score", 0.0))
    hside = int(getattr(hunter_setup, "side", 0) or 0)
    eside = int(getattr(expert_setup, "side", 0) or 0)

    if hside == eside:
        if es >= hs:
            return expert_setup, "v10", "same_side_v10_stronger"
        return hunter_setup, "hunter", "same_side_hunter_stronger"

    margin = _f(getattr(settings, "V10_OPPOSITE_OVERRIDE_MARGIN", 4.0), 4.0)
    if es >= hs + margin:
        return expert_setup, "v10", "opposite_v10_clear_edge"
    return hunter_setup, "hunter", "opposite_hunter_kept"


def record_router_decision(
    connection: sqlite3.Connection,
    *,
    symbol: str,
    hunter_setup: Any | None,
    expert_setup: Any | None,
    chosen_setup: Any | None,
    chosen_source: str,
    reason: str,
) -> None:
    ensure_tables(connection)
    connection.execute(
        """
        INSERT INTO v10_router_events(
            timestamp,symbol,hunter_playbook,hunter_side,hunter_score,
            expert_playbook,expert_side,expert_score,
            chosen_source,chosen_playbook,chosen_side,chosen_score,reason
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            _now(), symbol,
            getattr(hunter_setup, "playbook", None) if hunter_setup is not None else None,
            int(getattr(hunter_setup, "side", 0) or 0) if hunter_setup is not None else None,
            _f(getattr(hunter_setup, "score", 0.0)) if hunter_setup is not None else None,
            getattr(expert_setup, "playbook", None) if expert_setup is not None else None,
            int(getattr(expert_setup, "side", 0) or 0) if expert_setup is not None else None,
            _f(getattr(expert_setup, "score", 0.0)) if expert_setup is not None else None,
            str(chosen_source or "none"),
            getattr(chosen_setup, "playbook", None) if chosen_setup is not None else None,
            int(getattr(chosen_setup, "side", 0) or 0) if chosen_setup is not None else None,
            _f(getattr(chosen_setup, "score", 0.0)) if chosen_setup is not None else None,
            str(reason or ""),
        ),
    )


def best_setup(
    connection: sqlite3.Connection,
    *, symbol: str, frame: pd.DataFrame, dom: float | None = None,
) -> V10Setup | None:
    if not bool(getattr(settings, "V10_SUPERHUMAN_SCALPER_ENABLED", False)):
        return None
    ensure_tables(connection)
    candidates, context = _score_components(frame, dom)
    if not candidates:
        return None
    candidates.sort(key=lambda x: _f(x.get("score")), reverse=True)
    top = dict(candidates[0])
    second = candidates[1] if len(candidates) > 1 else None

    # Expert consensus: independent archetypes agreeing on side strengthen a setup;
    # near-tied disagreement reduces confidence instead of hard-vetoing it.
    same_side = [x for x in candidates[1:4] if int(x["side"]) == int(top["side"]) and _f(x["score"]) >= _f(top["score"])-12]
    opposite_close = [x for x in candidates[1:4] if int(x["side"]) != int(top["side"]) and _f(x["score"]) >= _f(top["score"])-5]
    consensus_bonus = min(6.0, 2.0 * len(same_side))
    conflict_penalty = min(8.0, 4.0 * len(opposite_close))
    final_score = _clamp(_f(top["score"]) + consensus_bonus - conflict_penalty, 0.0, 100.0)

    # Regime-aware preference. This is a router, not a serial permission stack.
    regime = str(context.get("regime") or "unknown")
    pb = str(top["playbook"])
    if regime in {"trend", "expansion_trend"} and pb in {"trend_pullback","momentum_breakout","squeeze_release"}:
        final_score = _clamp(final_score + 3.0, 0, 100)
    if regime in {"range","compression_range","volatile_range"} and pb in {"range_edge_rotation","vwap_mean_reversion","liquidity_sweep_reclaim","failed_breakout_reversal","exhaustion_snapback"}:
        final_score = _clamp(final_score + 3.0, 0, 100)

    minimum = _f(getattr(settings, "V10_EXPERT_TRIGGER_MIN_SCORE", 72.0), 72.0)
    accepted = final_score >= minimum
    connection.execute(
        """
        INSERT INTO v10_expert_events(timestamp,symbol,regime,playbook,side,score,accepted,expert_scores_json,context_json)
        VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (
            _now(), symbol, regime, pb, int(top["side"]), final_score, 1 if accepted else 0,
            json.dumps(candidates[:8], separators=(",",":"), default=str),
            json.dumps({**context, "consensus_bonus":consensus_bonus,"conflict_penalty":conflict_penalty}, separators=(",",":"), default=str),
        ),
    )
    if not accepted:
        return None

    return V10Setup(
        symbol=symbol,
        playbook=pb,
        side=int(top["side"]),
        score=final_score,
        stop_atr=_f(top.get("stop_atr"),0.65),
        take_atr=_f(top.get("take_atr"),1.15),
        max_hold_bars=int(top.get("max_hold_bars") or 5),
        context={
            "source": "v10_superhuman_expert_ensemble",
            "regime_router": regime,
            "expert_scores": candidates[:8],
            "top_reason": top.get("why") or {},
            "consensus_bonus": consensus_bonus,
            "conflict_penalty": conflict_penalty,
            "exit_profile": {
                "be_at_r": 0.45 if pb in {"range_edge_rotation","vwap_mean_reversion"} else 0.55,
                "trail_at_r": 0.70 if pb in {"range_edge_rotation","vwap_mean_reversion"} else 0.85,
            },
        },
    )


def recent_status(connection: sqlite3.Connection, hours: int = 6) -> dict[str, Any]:
    ensure_tables(connection)
    rows = connection.execute(
        """
        SELECT playbook,side,COUNT(*) n,SUM(accepted) accepted,AVG(score) avg_score,MAX(score) max_score
        FROM v10_expert_events
        WHERE timestamp>=datetime('now', ?)
        GROUP BY playbook,side ORDER BY accepted DESC,n DESC
        """,
        (f"-{int(hours)} hours",),
    ).fetchall()
    return {"rows": [dict(r) if hasattr(r,"keys") else list(r) for r in rows]}
