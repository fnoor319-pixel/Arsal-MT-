from __future__ import annotations

import json
import math
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Any

import MetaTrader5 as mt5
import numpy as np
import pandas as pd

import settings

UTC = timezone.utc


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=max(2, int(length)), adjust=False).mean()


def _rsi(series: pd.Series, length: int = 7) -> pd.Series:
    delta = series.diff()
    gains = delta.clip(lower=0).ewm(alpha=1 / length, adjust=False).mean()
    losses = (-delta.clip(upper=0)).ewm(alpha=1 / length, adjust=False).mean()
    relative = gains / losses.replace(0, np.nan)
    value = 100.0 - (100.0 / (1.0 + relative))
    value = value.where(~((losses == 0) & (gains > 0)), 100.0)
    value = value.where(~((gains == 0) & (losses > 0)), 0.0)
    value = value.where(~((gains == 0) & (losses == 0)), 50.0)
    return value


def _atr(frame: pd.DataFrame, length: int = 14) -> pd.Series:
    previous_close = frame["close"].shift(1)
    ranges = pd.concat(
        [
            frame["high"] - frame["low"],
            (frame["high"] - previous_close).abs(),
            (frame["low"] - previous_close).abs(),
        ],
        axis=1,
    )
    return ranges.max(axis=1).ewm(alpha=1 / length, adjust=False).mean()


def _adx(frame: pd.DataFrame, length: int = 14) -> pd.Series:
    high = frame["high"]
    low = frame["low"]
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = pd.Series(
        np.where((up_move > down_move) & (up_move > 0), up_move, 0.0),
        index=frame.index,
        dtype=float,
    )
    minus_dm = pd.Series(
        np.where((down_move > up_move) & (down_move > 0), down_move, 0.0),
        index=frame.index,
        dtype=float,
    )
    atr_value = _atr(frame, length).replace(0, np.nan)
    plus_di = 100.0 * plus_dm.ewm(alpha=1 / length, adjust=False).mean() / atr_value
    minus_di = 100.0 * minus_dm.ewm(alpha=1 / length, adjust=False).mean() / atr_value
    denominator = (plus_di + minus_di).replace(0, np.nan)
    dx = 100.0 * (plus_di - minus_di).abs() / denominator
    return dx.ewm(alpha=1 / length, adjust=False).mean()


def ensure_features(frame: pd.DataFrame) -> pd.DataFrame:
    """Add the Spartan canonical indicators without changing existing columns."""
    result = frame.copy()
    for length in (5, 13, 20, 50, 200):
        name = f"ema_{length}"
        if name not in result.columns:
            result[name] = _ema(result["close"], length)
    if "rsi_7" not in result.columns:
        result["rsi_7"] = _rsi(result["close"], 7)
    if "rsi_14" not in result.columns:
        result["rsi_14"] = _rsi(result["close"], 14)
    if "atr_14" not in result.columns:
        result["atr_14"] = _atr(result, 14)
    if "adx_14" not in result.columns:
        result["adx_14"] = _adx(result, 14)
    return result.replace([np.inf, -np.inf], np.nan)


def symbol_kind(symbol: str) -> str:
    upper = str(symbol).upper()
    if "XAUUSD" in upper or upper.startswith("GOLD"):
        return "gold"
    if "USOIL" in upper or "WTI" in upper or upper.startswith("OIL"):
        return "oil"
    if "BTC" in upper or "ETH" in upper:
        return "crypto"
    return "other"


def _parse_hhmm(value: str) -> tuple[int, int]:
    hour, minute = str(value).strip().split(":", 1)
    return int(hour), int(minute)


def _in_utc_window(now: datetime, start: str, end: str) -> bool:
    sh, sm = _parse_hhmm(start)
    eh, em = _parse_hhmm(end)
    minute = now.hour * 60 + now.minute
    start_minute = sh * 60 + sm
    end_minute = eh * 60 + em
    if start_minute <= end_minute:
        return start_minute <= minute <= end_minute
    return minute >= start_minute or minute <= end_minute


def session_status(symbol: str, now: datetime | None = None) -> dict[str, Any]:
    now_utc = (now or datetime.now(UTC)).astimezone(UTC)
    session_tz_name = str(getattr(settings, "SPARTAN_SESSION_TIMEZONE", "Asia/Karachi"))
    try:
        session_tz = ZoneInfo(session_tz_name)
    except Exception:
        session_tz = ZoneInfo("Asia/Karachi")
    now_session = now_utc.astimezone(session_tz)
    kind = symbol_kind(symbol)
    if kind not in {"gold", "oil"}:
        return {"allowed": True, "session": "unrestricted", "timestamp_utc": now_utc.isoformat(), "timestamp_session": now_session.isoformat()}

    london = _in_utc_window(
        now_session,
        str(getattr(settings, "SPARTAN_GOLD_LONDON_START", getattr(settings, "LONDON_START", "15:00"))),
        str(getattr(settings, "SPARTAN_GOLD_LONDON_END", getattr(settings, "LONDON_END", "23:30"))),
    )
    new_york = _in_utc_window(
        now_session,
        str(getattr(settings, "SPARTAN_US_START", getattr(settings, "NY_START", "20:00"))),
        str(getattr(settings, "SPARTAN_US_END", getattr(settings, "NY_END", "03:00"))),
    )
    if kind == "oil":
        allowed = new_york
        label = "new_york" if allowed else "off_session"
    else:
        allowed = london or new_york
        if london and new_york:
            label = "london_new_york_overlap"
        elif london:
            label = "london"
        elif new_york:
            label = "new_york"
        else:
            label = "off_session"
    kill_zone = None
    if _in_utc_window(now_utc, str(getattr(settings, "SPARTAN_LONDON_KILL_START_UTC", "08:00")), str(getattr(settings, "SPARTAN_LONDON_KILL_END_UTC", "09:00"))):
        kill_zone = "london"
    if _in_utc_window(now_utc, str(getattr(settings, "SPARTAN_NY_KILL_START_UTC", "15:00")), str(getattr(settings, "SPARTAN_NY_KILL_END_UTC", "16:00"))):
        kill_zone = "new_york" if kill_zone is None else "overlap"
    if not bool(getattr(settings, "SPARTAN_SESSION_FILTER_ENABLED", True)):
        allowed = True
        label = f"advisory_{label}"
    return {"allowed": allowed, "session": label, "kill_zone": kill_zone, "timestamp_utc": now_utc.isoformat(), "timestamp_session": now_session.isoformat(), "timezone": session_tz_name}


def _event_time(value: Any) -> datetime | None:
    try:
        parsed = pd.Timestamp(value)
        if parsed.tzinfo is None:
            parsed = parsed.tz_localize("UTC")
        return parsed.tz_convert("UTC").to_pydatetime()
    except Exception:
        return None


def load_news_events() -> tuple[list[dict[str, Any]], str]:
    """Read a provider-neutral local calendar file.

    A real provider/bridge can overwrite config/news_events.json atomically.
    Keeping this adapter provider-neutral avoids pretending a currency-rate
    library is an economic calendar.
    """
    path = Path(getattr(settings, "SPARTAN_NEWS_FILE", Path(settings.PROJECT_DIR) / "config" / "news_events.json"))
    if not path.exists():
        return [], "missing"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return [], "invalid"
    events = payload.get("events", []) if isinstance(payload, dict) else payload
    if not isinstance(events, list):
        return [], "invalid"
    clean = [item for item in events if isinstance(item, dict)]
    provider = str(payload.get("provider", "") if isinstance(payload, dict) else "").strip().upper()
    generated = payload.get("generated_at") if isinstance(payload, dict) else None
    if provider in {"", "UNCONFIGURED", "NONE"} or not generated:
        return clean, "unconfigured"
    max_age = int(getattr(settings, "SPARTAN_NEWS_FEED_MAX_AGE_MINUTES", 180))
    if generated:
        generated_at = _event_time(generated)
        if generated_at and datetime.now(UTC) - generated_at > timedelta(minutes=max_age):
            return clean, "stale"
    return clean, "ok"


def _event_relevant(symbol: str, event: dict[str, Any]) -> bool:
    kind = symbol_kind(symbol)
    text = " ".join(
        str(event.get(key, ""))
        for key in ("name", "title", "currency", "category", "symbol", "tags")
    ).upper()
    if kind == "gold":
        return any(token in text for token in ("USD", "CPI", "NFP", "FOMC", "PCE", "FED", "POWELL", "GDP"))
    if kind == "oil":
        return any(token in text for token in ("USD", "EIA", "OPEC", "API", "CRUDE", "OIL", "FOMC", "CPI", "NFP"))
    return False


def news_status(symbol: str, now: datetime | None = None) -> dict[str, Any]:
    now = (now or datetime.now(UTC)).astimezone(UTC)
    events, feed_status = load_news_events()
    pre = int(getattr(settings, "SPARTAN_NEWS_PRE_LOCK_MINUTES", 30))
    post = int(getattr(settings, "SPARTAN_NEWS_POST_LOCK_MINUTES", 20))
    upcoming: list[tuple[float, dict[str, Any], datetime]] = []
    locked: list[tuple[float, dict[str, Any], datetime]] = []
    for event in events:
        if str(event.get("impact", "high")).lower() not in {"high", "major", "3", "red"}:
            continue
        if not _event_relevant(symbol, event):
            continue
        dt = _event_time(event.get("time_utc") or event.get("time") or event.get("datetime"))
        if dt is None:
            continue
        minutes = (dt - now).total_seconds() / 60.0
        if minutes >= 0:
            upcoming.append((minutes, event, dt))
        if -post <= minutes <= pre:
            locked.append((minutes, event, dt))
    upcoming.sort(key=lambda item: item[0])
    locked.sort(key=lambda item: abs(item[0]))
    nearest = upcoming[0] if upcoming else None
    active = locked[0] if locked else None
    return {
        "feed_status": feed_status,
        "available": feed_status == "ok",
        "locked": bool(active),
        "lock_event": (
            {
                "name": str(active[1].get("name") or active[1].get("title") or "major_event"),
                "time_utc": active[2].isoformat(),
                "minutes": round(active[0], 2),
            }
            if active else None
        ),
        "next_event": (
            {
                "name": str(nearest[1].get("name") or nearest[1].get("title") or "major_event"),
                "time_utc": nearest[2].isoformat(),
                "minutes": round(nearest[0], 2),
            }
            if nearest else None
        ),
    }


def tick_flow_snapshot(symbol: str, seconds: int | None = None) -> dict[str, Any]:
    """Recent cumulative delta + compact footprint from broker ticks.

    Uses explicit BUY/SELL tick flags when supplied by the broker. If those
    flags are absent, an uptick/downtick proxy is used and clearly labelled.
    """
    seconds = max(5, int(seconds or getattr(settings, "SPARTAN_TICK_FLOW_SECONDS", 60)))
    now = datetime.now(UTC)
    start = now - timedelta(seconds=seconds)
    flags_all = int(getattr(mt5, "COPY_TICKS_ALL", -1))
    try:
        ticks = mt5.copy_ticks_range(symbol, start, now, flags_all)
    except Exception as error:
        return {"available": False, "source": "mt5_ticks", "error": str(error)[:160]}
    if ticks is None or len(ticks) < 3:
        return {"available": False, "source": "mt5_ticks", "count": 0 if ticks is None else len(ticks)}
    data = pd.DataFrame(ticks)
    volume_col = "volume_real" if "volume_real" in data.columns and _safe_float(data["volume_real"].sum()) > 0 else "volume" if "volume" in data.columns else None
    volumes = data[volume_col].astype(float).fillna(0.0).to_numpy() if volume_col else np.ones(len(data), dtype=float)
    buy = np.zeros(len(data), dtype=bool)
    sell = np.zeros(len(data), dtype=bool)
    source = "broker_aggressor_flags"
    buy_flag = int(getattr(mt5, "TICK_FLAG_BUY", 32))
    sell_flag = int(getattr(mt5, "TICK_FLAG_SELL", 64))
    if "flags" in data.columns:
        raw_flags = data["flags"].fillna(0).astype(int).to_numpy()
        buy = (raw_flags & buy_flag) != 0
        sell = (raw_flags & sell_flag) != 0
    if not buy.any() and not sell.any():
        source = "tick_direction_proxy"
        if "last" in data.columns and (data["last"].astype(float) > 0).any():
            price = data["last"].astype(float).replace(0, np.nan).ffill().bfill().to_numpy()
        else:
            bid = data["bid"].astype(float).to_numpy() if "bid" in data.columns else np.zeros(len(data))
            ask = data["ask"].astype(float).to_numpy() if "ask" in data.columns else bid
            price = (bid + ask) / 2.0
        changes = np.diff(price, prepend=price[0])
        buy = changes > 0
        sell = changes < 0
        # Carry the last non-zero direction through unchanged ticks.
        direction = 0
        for i, change in enumerate(changes):
            if change > 0:
                direction = 1
            elif change < 0:
                direction = -1
            if direction == 1 and not buy[i] and not sell[i]:
                buy[i] = True
            elif direction == -1 and not buy[i] and not sell[i]:
                sell[i] = True
    buy_volume = float(volumes[buy].sum())
    sell_volume = float(volumes[sell].sum())
    total = buy_volume + sell_volume
    delta = buy_volume - sell_volume
    buy_share = buy_volume / total if total > 0 else 0.0
    sell_share = sell_volume / total if total > 0 else 0.0

    info = mt5.symbol_info(symbol)
    point = max(1e-9, _safe_float(getattr(info, "point", 0.01), 0.01)) if info is not None else 0.01
    bin_size = point * max(1, int(getattr(settings, "SPARTAN_FOOTPRINT_BIN_POINTS", 5)))
    if "last" in data.columns and (data["last"].astype(float) > 0).any():
        prices = data["last"].astype(float).replace(0, np.nan).ffill().bfill().to_numpy()
    else:
        bid = data["bid"].astype(float).to_numpy() if "bid" in data.columns else np.zeros(len(data))
        ask = data["ask"].astype(float).to_numpy() if "ask" in data.columns else bid
        prices = (bid + ask) / 2.0
    levels: dict[float, list[float]] = {}
    for price, volume, is_buy, is_sell in zip(prices, volumes, buy, sell):
        if not math.isfinite(float(price)) or float(price) <= 0:
            continue
        level = round(round(float(price) / bin_size) * bin_size, 8)
        cell = levels.setdefault(level, [0.0, 0.0])
        if is_buy:
            cell[0] += float(volume)
        if is_sell:
            cell[1] += float(volume)
    ranked = sorted(levels.items(), key=lambda item: item[1][0] + item[1][1], reverse=True)[:12]
    footprint = [
        {"price": level, "buy": values[0], "sell": values[1], "delta": values[0] - values[1]}
        for level, values in ranked
    ]
    return {
        "available": total > 0, "source": source, "count": len(data), "seconds": seconds,
        "buy_volume": buy_volume, "sell_volume": sell_volume, "cumulative_delta": delta,
        "buy_share": buy_share, "sell_share": sell_share,
        "delta_ratio": (delta / total) if total > 0 else 0.0,
        "footprint": footprint,
    }


def volume_profile(frame: pd.DataFrame, lookback: int | None = None) -> dict[str, Any]:
    lookback = max(50, int(lookback or getattr(settings, "SPARTAN_VOLUME_PROFILE_LOOKBACK", 300)))
    data = frame.tail(lookback).dropna(subset=["high", "low", "close"])
    if len(data) < 30:
        return {"available": False, "source": "insufficient_data"}
    price = (data["high"] + data["low"] + data["close"]) / 3.0
    if "real_volume" in data.columns and _safe_float(data["real_volume"].sum()) > 0:
        weight = data["real_volume"].astype(float).clip(lower=0)
        source = "broker_real_volume"
    elif "tick_volume" in data.columns:
        weight = data["tick_volume"].astype(float).clip(lower=0)
        source = "broker_tick_volume"
    else:
        weight = pd.Series(np.ones(len(data)), index=data.index)
        source = "bar_count_proxy"
    bins = max(16, min(64, int(math.sqrt(len(data)) * 2)))
    hist, edges = np.histogram(price.to_numpy(dtype=float), bins=bins, weights=weight.to_numpy(dtype=float))
    if float(hist.sum()) <= 0:
        return {"available": False, "source": source}
    centers = (edges[:-1] + edges[1:]) / 2.0
    poc_index = int(np.argmax(hist))
    poc = float(centers[poc_index])
    order = np.argsort(hist)[::-1]
    target = 0.70 * float(hist.sum())
    accumulated = 0.0
    selected: list[int] = []
    for idx in order:
        selected.append(int(idx))
        accumulated += float(hist[idx])
        if accumulated >= target:
            break
    vah = float(max(centers[idx] for idx in selected))
    val = float(min(centers[idx] for idx in selected))
    hvn_indices = [int(idx) for idx in order[:3]]
    positive = np.where(hist > 0)[0]
    lvn_indices = [int(idx) for idx in positive[np.argsort(hist[positive])[:3]]] if len(positive) else []
    return {
        "available": True,
        "source": source,
        "poc": poc,
        "vah": vah,
        "val": val,
        "hvn": [float(centers[idx]) for idx in hvn_indices],
        "lvn": [float(centers[idx]) for idx in lvn_indices],
        "lookback_bars": len(data),
    }


def _confirmed_swings(frame: pd.DataFrame, span: int = 3) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    highs: list[tuple[int, float]] = []
    lows: list[tuple[int, float]] = []
    high = frame["high"].to_numpy(dtype=float)
    low = frame["low"].to_numpy(dtype=float)
    for i in range(span, len(frame) - span):
        h = high[i]
        l = low[i]
        if h >= np.max(high[i - span:i + span + 1]):
            highs.append((i, float(h)))
        if l <= np.min(low[i - span:i + span + 1]):
            lows.append((i, float(l)))
    return highs, lows


def smc_snapshot(frame: pd.DataFrame) -> dict[str, Any]:
    data = ensure_features(frame).tail(max(120, int(getattr(settings, "SPARTAN_SMC_LOOKBACK", 240)))).reset_index(drop=True)
    if len(data) < 30:
        return {"available": False}
    row = data.iloc[-2]
    atr_value = max(1e-9, _safe_float(row.get("atr_14")))
    close = _safe_float(row.get("close"))
    swing_highs, swing_lows = _confirmed_swings(data.iloc[:-1], 3)
    last_high = swing_highs[-1] if swing_highs else None
    last_low = swing_lows[-1] if swing_lows else None
    previous_high = swing_highs[-2] if len(swing_highs) > 1 else None
    previous_low = swing_lows[-2] if len(swing_lows) > 1 else None
    if last_high and previous_high and last_low and previous_low:
        if last_high[1] > previous_high[1] and last_low[1] > previous_low[1]:
            structure = "bullish"
        elif last_high[1] < previous_high[1] and last_low[1] < previous_low[1]:
            structure = "bearish"
        else:
            structure = "mixed"
    else:
        structure = "unknown"

    bos = None
    if last_high and close > last_high[1]:
        bos = "bullish"
    elif last_low and close < last_low[1]:
        bos = "bearish"
    choch = None
    if structure == "bearish" and bos == "bullish":
        choch = "bullish"
    elif structure == "bullish" and bos == "bearish":
        choch = "bearish"

    fvg_candidates: list[dict[str, Any]] = []
    start = max(2, len(data) - 60)
    for i in range(start, len(data) - 1):
        prev2 = data.iloc[i - 2]
        current = data.iloc[i]
        if _safe_float(current["low"]) > _safe_float(prev2["high"]):
            low_bound = _safe_float(prev2["high"])
            high_bound = _safe_float(current["low"])
            later_low = _safe_float(data.iloc[i + 1:-1]["low"].min(), high_bound) if i + 1 < len(data) - 1 else high_bound
            mitigated = _clamp((high_bound - later_low) / max(1e-9, high_bound - low_bound), 0.0, 1.0)
            fvg_candidates.append({"direction": "bullish", "low": low_bound, "high": high_bound, "index": i, "mitigated_pct": mitigated * 100.0})
        if _safe_float(current["high"]) < _safe_float(prev2["low"]):
            low_bound = _safe_float(current["high"])
            high_bound = _safe_float(prev2["low"])
            later_high = _safe_float(data.iloc[i + 1:-1]["high"].max(), low_bound) if i + 1 < len(data) - 1 else low_bound
            mitigated = _clamp((later_high - low_bound) / max(1e-9, high_bound - low_bound), 0.0, 1.0)
            fvg_candidates.append({"direction": "bearish", "low": low_bound, "high": high_bound, "index": i, "mitigated_pct": mitigated * 100.0})
    active_fvg = None
    if fvg_candidates:
        active_fvg = min(
            fvg_candidates[-12:],
            key=lambda item: min(abs(close - item["low"]), abs(close - item["high"])),
        )
        active_fvg = dict(active_fvg)
        active_fvg["distance_atr"] = min(abs(close - active_fvg["low"]), abs(close - active_fvg["high"])) / atr_value
        active_fvg["age_bars"] = (len(data) - 2) - int(active_fvg.pop("index"))

    sweep = None
    recent = data.iloc[max(3, len(data) - 8):len(data) - 1]
    reference_highs = [item for item in swing_highs if item[0] < len(data) - 8]
    reference_lows = [item for item in swing_lows if item[0] < len(data) - 8]
    ref_high = reference_highs[-1][1] if reference_highs else (last_high[1] if last_high else None)
    ref_low = reference_lows[-1][1] if reference_lows else (last_low[1] if last_low else None)
    for offset, (_, candle) in enumerate(recent.iterrows()):
        if ref_high is not None and _safe_float(candle["high"]) > ref_high and _safe_float(candle["close"]) < ref_high:
            sweep = {"detected": True, "side": "buy_side", "level": ref_high, "bars_ago": len(recent) - 1 - offset}
        if ref_low is not None and _safe_float(candle["low"]) < ref_low and _safe_float(candle["close"]) > ref_low:
            sweep = {"detected": True, "side": "sell_side", "level": ref_low, "bars_ago": len(recent) - 1 - offset}

    order_block = None
    impulse_threshold = 1.0
    for i in range(len(data) - 3, max(2, len(data) - 40), -1):
        candle = data.iloc[i]
        body = abs(_safe_float(candle["close"]) - _safe_float(candle["open"]))
        if body < impulse_threshold * max(1e-9, _safe_float(candle.get("atr_14"), atr_value)):
            continue
        direction = 1 if _safe_float(candle["close"]) > _safe_float(candle["open"]) else -1
        for j in range(i - 1, max(-1, i - 6), -1):
            base = data.iloc[j]
            base_direction = 1 if _safe_float(base["close"]) > _safe_float(base["open"]) else -1
            if base_direction == -direction:
                order_block = {
                    "direction": "bullish" if direction == 1 else "bearish",
                    "low": _safe_float(base["low"]),
                    "high": _safe_float(base["high"]),
                    "age_bars": (len(data) - 2) - j,
                    "distance_atr": min(abs(close - _safe_float(base["low"])), abs(close - _safe_float(base["high"]))) / atr_value,
                    "heuristic": True,
                }
                break
        if order_block:
            break

    tolerance = 0.20 * atr_value
    support = None
    resistance = None
    if swing_lows:
        level = min((item[1] for item in swing_lows[-8:]), key=lambda value: abs(close - value))
        touches = sum(1 for _, value in swing_lows if abs(value - level) <= tolerance)
        support = {"level": float(level), "touches": touches, "distance_atr": abs(close - level) / atr_value}
    if swing_highs:
        level = min((item[1] for item in swing_highs[-8:]), key=lambda value: abs(close - value))
        touches = sum(1 for _, value in swing_highs if abs(value - level) <= tolerance)
        resistance = {"level": float(level), "touches": touches, "distance_atr": abs(close - level) / atr_value}

    return {
        "available": True,
        "structure": structure,
        "bos": bos,
        "choch": choch,
        "fvg": active_fvg,
        "order_block": order_block,
        "liquidity_sweep": sweep,
        "support": support,
        "resistance": resistance,
    }


def _bars_to_frame(rates: Any) -> pd.DataFrame | None:
    if rates is None or len(rates) == 0:
        return None
    frame = pd.DataFrame(rates)
    if "time" in frame.columns:
        frame["time"] = pd.to_datetime(frame["time"], unit="s", utc=True)
    return frame


def higher_timeframe_snapshot(symbol: str) -> dict[str, Any]:
    mapping = {
        "M5": getattr(mt5, "TIMEFRAME_M5", 5),
        "M15": getattr(mt5, "TIMEFRAME_M15", 15),
        "H1": getattr(mt5, "TIMEFRAME_H1", 16385),
        "H4": getattr(mt5, "TIMEFRAME_H4", 16388),
    }
    bars = max(220, int(getattr(settings, "SPARTAN_HTF_BARS", 260)))
    output: dict[str, Any] = {}
    for name, timeframe in mapping.items():
        try:
            rates = mt5.copy_rates_from_pos(symbol, timeframe, 1, bars)
            frame = _bars_to_frame(rates)
            if frame is None or len(frame) < 60:
                output[name] = {"available": False}
                continue
            featured = ensure_features(frame)
            row = featured.iloc[-1]
            ema20 = _safe_float(row.get("ema_20"))
            ema50 = _safe_float(row.get("ema_50"))
            close = _safe_float(row.get("close"))
            if close > ema20 > ema50:
                trend = "up"
            elif close < ema20 < ema50:
                trend = "down"
            else:
                trend = "neutral"
            output[name] = {
                "available": True,
                "trend": trend,
                "close": close,
                "ema20": ema20,
                "ema50": ema50,
                "rsi14": _safe_float(row.get("rsi_14"), 50.0),
                "adx14": _safe_float(row.get("adx_14")),
            }
        except Exception as error:
            output[name] = {"available": False, "error": str(error)[:160]}
    return output


def _side_vote(value: int) -> str:
    return "buy" if value > 0 else "sell" if value < 0 else "neutral"


def _agent_votes(side: int, technical_score: int, smc: dict[str, Any], micro: dict[str, Any], tick_flow: dict[str, Any], news: dict[str, Any], htf: dict[str, Any], vp: dict[str, Any]) -> dict[str, str]:
    technical = side if technical_score >= 4 else 0
    smc_side = 0
    structure = str(smc.get("structure") or "")
    if structure == "bullish" or smc.get("choch") == "bullish" or (smc.get("liquidity_sweep") or {}).get("side") == "sell_side":
        smc_side += 1
    if structure == "bearish" or smc.get("choch") == "bearish" or (smc.get("liquidity_sweep") or {}).get("side") == "buy_side":
        smc_side -= 1
    dom_score = _safe_float(micro.get("score", micro.get("imbalance", 0.0)))
    delta_score = _safe_float(tick_flow.get("delta_ratio")) if tick_flow.get("available") else 0.0
    flow_score = 0.65 * dom_score + 0.35 * delta_score if micro.get("available") else delta_score
    flow_side = 1 if flow_score >= 0.15 else -1 if flow_score <= -0.15 else 0
    macro_side = 0 if news.get("available") and not news.get("locked") else (0 if not news.get("locked") else -side)
    htf_values = [str(item.get("trend")) for item in htf.values() if isinstance(item, dict) and item.get("available")]
    htf_balance = htf_values.count("up") - htf_values.count("down")
    htf_side = 1 if htf_balance >= 2 else -1 if htf_balance <= -2 else 0
    vp_side = 0
    if vp.get("available"):
        price = _safe_float(vp.get("current_price"))
        poc = _safe_float(vp.get("poc"))
        if price and poc:
            vp_side = 1 if price > poc else -1 if price < poc else 0
    return {
        "technical": _side_vote(technical),
        "smc": _side_vote(smc_side),
        "flow": _side_vote(flow_side),
        "macro": _side_vote(macro_side),
        "htf": _side_vote(htf_side),
        "volume_profile": _side_vote(vp_side),
        "ml": "neutral",  # existing SuperLearner supplies the live ML probability later.
    }



def micro_gate_profile(playbook: str | None, micro_score: float | None, execution_tier: str | None) -> dict[str, Any]:
    """Return the deterministic Spartan profile for a V8 native micro playbook.

    Legacy/non-native candidates keep the original generic Spartan thresholds.
    The profile only softens indicator-shape gates that are inappropriate for
    some micro playbooks. Session/news/spread/stale-tick/ATR/trade-count safety
    gates are not changed here.
    """
    base = {
        "active": False,
        "playbook": str(playbook or ""),
        "category": "legacy",
        "require_adx": True,
        "adx_min": _safe_float(getattr(settings, "SPARTAN_MIN_ADX", 20.0), 20.0),
        "min_ratio": _safe_float(getattr(settings, "SPARTAN_MIN_AVAILABLE_CONFLUENCE_RATIO", 0.72), 0.72),
        "min_core": int(getattr(settings, "SPARTAN_MIN_CORE_ALIGNMENT", 4)),
        "min_conf": _safe_float(getattr(settings, "SPARTAN_MIN_CONFIDENCE", 0.70), 0.70),
    }
    if not bool(getattr(settings, "V8_PLAYBOOK_AWARE_SPARTAN_ENABLED", True)):
        return base
    if str(execution_tier or "") != "v8_native_alpha":
        return base
    score = _safe_float(micro_score, 0.0)
    if score < _safe_float(getattr(settings, "V8_MICRO_SPARTAN_MIN_SCORE_FOR_PROFILE", 68.0), 68.0):
        return base

    pb = str(playbook or "")
    continuation = pb in {"momentum_burst", "impulse_pullback_resume"}
    reversal = pb in {"wick_rejection", "exhaustion_snapback"}
    squeeze = pb == "squeeze_breakout"
    if not (continuation or reversal or squeeze):
        return base

    out = dict(base)
    out["active"] = True
    if continuation:
        out.update({
            "category": "continuation",
            "require_adx": True,
            "adx_min": _safe_float(getattr(settings, "V8_MICRO_SPARTAN_CONT_ADX_MIN", 16.0), 16.0),
            "min_ratio": _safe_float(getattr(settings, "V8_MICRO_SPARTAN_CONT_MIN_CONFLUENCE_RATIO", 0.60), 0.60),
            "min_core": int(getattr(settings, "V8_MICRO_SPARTAN_CONT_MIN_CORE_ALIGNMENT", 3)),
            "min_conf": _safe_float(getattr(settings, "V8_MICRO_SPARTAN_CONT_MIN_CONFIDENCE", 0.60), 0.60),
        })
    elif reversal:
        out.update({
            "category": "reversal",
            "require_adx": bool(getattr(settings, "V8_MICRO_SPARTAN_REV_REQUIRE_ADX", False)),
            "adx_min": _safe_float(getattr(settings, "V8_MICRO_SPARTAN_CONT_ADX_MIN", 16.0), 16.0),
            "min_ratio": _safe_float(getattr(settings, "V8_MICRO_SPARTAN_REV_MIN_CONFLUENCE_RATIO", 0.55), 0.55),
            "min_core": int(getattr(settings, "V8_MICRO_SPARTAN_REV_MIN_CORE_ALIGNMENT", 2)),
            "min_conf": _safe_float(getattr(settings, "V8_MICRO_SPARTAN_REV_MIN_CONFIDENCE", 0.56), 0.56),
        })
    else:
        out.update({
            "category": "squeeze",
            "require_adx": bool(getattr(settings, "V8_MICRO_SPARTAN_SQUEEZE_REQUIRE_ADX", False)),
            "adx_min": _safe_float(getattr(settings, "V8_MICRO_SPARTAN_CONT_ADX_MIN", 16.0), 16.0),
            "min_ratio": _safe_float(getattr(settings, "V8_MICRO_SPARTAN_SQUEEZE_MIN_CONFLUENCE_RATIO", 0.58), 0.58),
            "min_core": int(getattr(settings, "V8_MICRO_SPARTAN_SQUEEZE_MIN_CORE_ALIGNMENT", 2)),
            "min_conf": _safe_float(getattr(settings, "V8_MICRO_SPARTAN_SQUEEZE_MIN_CONFIDENCE", 0.58), 0.58),
        })
    return out


def build_gate_snapshot(
    symbol: str,
    frame: pd.DataFrame,
    tick: Any,
    side: int,
    micro: dict[str, Any],
    daily_trade_count: int = 0,
    now: datetime | None = None,
    fetch_htf: bool = True,
    playbook: str | None = None,
    micro_score: float | None = None,
    execution_tier: str | None = None,
) -> dict[str, Any]:
    """Build one deterministic Spartan-Pro snapshot and hard-gate decision."""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    kind = symbol_kind(symbol)
    if kind not in {"gold", "oil", "crypto"}:
        return {
            "enabled": False,
            "approved": True,
            "reason": "symbol_not_governed_by_spartan_pro",
            "symbol": symbol,
        }
    hard_gate_enabled = kind in {"gold", "oil"}

    featured = ensure_features(frame)
    if len(featured) < 210:
        return {"enabled": True, "approved": False, "reason": "insufficient_indicator_history", "symbol": symbol}
    row = featured.iloc[-2]
    bid = _safe_float(getattr(tick, "bid", 0.0))
    ask = _safe_float(getattr(tick, "ask", 0.0))
    spread = max(0.0, ask - bid)
    entry_reference = ask if side == 1 else bid
    atr_value = _safe_float(row.get("atr_14"))
    adx_value = _safe_float(row.get("adx_14"))
    tick_epoch = _safe_float(getattr(tick, "time_msc", 0.0)) / 1000.0 or _safe_float(getattr(tick, "time", 0.0))
    tick_age = max(0.0, now.timestamp() - tick_epoch) if tick_epoch > 0 else float("inf")

    session = session_status(symbol, now)
    news = news_status(symbol, now)
    smc = smc_snapshot(featured)
    vp = volume_profile(featured)
    vp["current_price"] = entry_reference
    tick_flow = tick_flow_snapshot(symbol) if fetch_htf else {"available": False, "source": "offline_not_fetched"}
    htf = higher_timeframe_snapshot(symbol) if fetch_htf else {}

    bid_volume = _safe_float(micro.get("buy_volume"))
    ask_volume = _safe_float(micro.get("sell_volume"))
    total_volume = bid_volume + ask_volume
    if total_volume > 0:
        bid_share = bid_volume / total_volume
        ask_share = ask_volume / total_volume
    else:
        imbalance = _safe_float(micro.get("imbalance"))
        bid_share = (1.0 + imbalance) / 2.0 if micro.get("available") else 0.0
        ask_share = 1.0 - bid_share if micro.get("available") else 0.0
    imbalance_pct = (bid_share - ask_share) * 100.0 if (bid_share or ask_share) else None

    close = _safe_float(row.get("close"))
    ema5 = _safe_float(row.get("ema_5"))
    ema13 = _safe_float(row.get("ema_13"))
    ema200 = _safe_float(row.get("ema_200"))
    rsi7 = _safe_float(row.get("rsi_7"), 50.0)
    if kind == "gold":
        atr_min = _safe_float(getattr(settings, "SPARTAN_GOLD_MIN_ATR", 0.50), 0.50)
    elif kind == "oil":
        atr_min = _safe_float(getattr(settings, "SPARTAN_OIL_MIN_ATR", 0.0), 0.0)
    else:
        atr_min = 0.0

    # V6.6: unavailable optional evidence is UNKNOWN, not a failed condition.
    # News and broker DOM remain usable when present, while their absence does
    # not artificially turn an otherwise 5/6 core setup into 5/8.
    news_condition: bool | None = (not bool(news.get("locked"))) if bool(news.get("available")) else None
    dom_available = bool(micro.get("available")) and total_volume > 0
    long_conditions: dict[str, bool | None] = {
        "ema_5_13": ema5 > ema13,
        "rsi": rsi7 > 50.0,
        "ema_200": close > ema200,
        "atr": atr_value >= atr_min,
        "order_book": (bid_share >= _safe_float(getattr(settings, "SPARTAN_ORDERBOOK_SHARE_THRESHOLD", 0.60), 0.60)) if dom_available else None,
        "adx": adx_value > _safe_float(getattr(settings, "SPARTAN_TREND_ADX", 25.0), 25.0),
        "news": news_condition,
        "session": bool(session.get("allowed")),
    }
    short_conditions: dict[str, bool | None] = {
        "ema_5_13": ema5 < ema13,
        "rsi": rsi7 < 50.0,
        "ema_200": close < ema200,
        "atr": atr_value >= atr_min,
        "order_book": (ask_share >= _safe_float(getattr(settings, "SPARTAN_ORDERBOOK_SHARE_THRESHOLD", 0.60), 0.60)) if dom_available else None,
        "adx": adx_value > _safe_float(getattr(settings, "SPARTAN_TREND_ADX", 25.0), 25.0),
        "news": news_condition,
        "session": bool(session.get("allowed")),
    }
    side_conditions = long_conditions if side == 1 else short_conditions
    available_conditions = {key: value for key, value in side_conditions.items() if value is not None}
    available_total = max(1, len(available_conditions))
    available_passed = sum(1 for value in available_conditions.values() if bool(value))
    available_ratio = available_passed / available_total
    core_keys = ("ema_5_13", "rsi", "ema_200", "atr", "adx", "session")
    core_passed = sum(1 for key in core_keys if bool(side_conditions.get(key)))
    # Keep a normalized /8 score for existing logs/UI while gating on actual
    # available evidence. This preserves compatibility without penalising nulls.
    score = int(round(available_ratio * 8.0))

    gate_profile = micro_gate_profile(playbook, micro_score, execution_tier)

    hard_reasons: list[str] = []
    if tick_age > _safe_float(getattr(settings, "SPARTAN_MAX_TICK_AGE_SECONDS", 10.0), 10.0):
        hard_reasons.append(f"stale_tick_{tick_age:.1f}s")
    if hard_gate_enabled and not bool(session.get("allowed")):
        hard_reasons.append("outside_allowed_session")
    if hard_gate_enabled and bool(gate_profile.get("require_adx", True)) and adx_value < _safe_float(gate_profile.get("adx_min"), 20.0):
        hard_reasons.append(f"adx_below_{_safe_float(gate_profile.get('adx_min'), 20.0):.0f}")
    if hard_gate_enabled and atr_value < atr_min:
        hard_reasons.append("atr_below_symbol_minimum")
    if hard_gate_enabled and bool(news.get("locked")):
        hard_reasons.append("major_news_lock")
    if hard_gate_enabled and bool(getattr(settings, "SPARTAN_NEWS_HARD_GATE", False)) and not bool(news.get("available")):
        hard_reasons.append(f"news_feed_{news.get('feed_status', 'unavailable')}")
    if hard_gate_enabled and bool(getattr(settings, "SPARTAN_REQUIRE_DOM", False)) and not bool(micro.get("available")):
        hard_reasons.append("dom_unavailable")
    if daily_trade_count >= int(getattr(settings, "SPARTAN_MAX_TRADES_PER_DAY", 10)):
        hard_reasons.append("max_trades_per_day_reached")

    spread_atr = spread / max(1e-9, atr_value)
    if spread_atr > _safe_float(getattr(settings, "MAX_SPREAD_ATR_FRACTION", 0.18), 0.18):
        hard_reasons.append("spread_atr_too_high")
    if kind == "gold" and spread > _safe_float(getattr(settings, "SPARTAN_GOLD_MAX_ABS_SPREAD", 0.50), 0.50):
        hard_reasons.append("gold_absolute_spread_too_high")

    min_ratio = _safe_float(gate_profile.get("min_ratio"), 0.72)
    min_core = int(gate_profile.get("min_core", 4))
    if hard_gate_enabled and core_passed < min_core:
        hard_reasons.append(f"core_alignment_{core_passed}_below_{min_core}")
    if hard_gate_enabled and available_ratio < min_ratio:
        hard_reasons.append(
            f"confluence_{available_passed}of{available_total}_ratio_{available_ratio:.2f}_below_{min_ratio:.2f}"
        )

    agents = _agent_votes(side, score, smc, micro, tick_flow, news, htf, vp)
    if adx_value > _safe_float(getattr(settings, "SPARTAN_TREND_ADX", 25.0), 25.0) and ema5 > ema13 and close > ema200:
        agents["regime"] = "buy"
    elif adx_value > _safe_float(getattr(settings, "SPARTAN_TREND_ADX", 25.0), 25.0) and ema5 < ema13 and close < ema200:
        agents["regime"] = "sell"
    else:
        agents["regime"] = "neutral"
    aligned = sum(1 for value in agents.values() if value == _side_vote(side))
    opposed = sum(1 for value in agents.values() if value == _side_vote(-side))
    confidence = available_ratio + min(0.08, aligned * 0.015) - min(0.08, opposed * 0.025)
    confidence = _clamp(confidence, 0.0, 0.99)
    min_conf = _safe_float(gate_profile.get("min_conf"), 0.70)
    if hard_gate_enabled and confidence < min_conf:
        hard_reasons.append(f"confidence_{confidence:.2f}_below_{min_conf:.2f}")

    # Deterministic context for the LLM.  These labels are calculated by Python
    # from the same numbers and are not subjective market predictions.
    if adx_value < _safe_float(getattr(settings, "SPARTAN_MIN_ADX", 20.0), 20.0):
        regime_label = "ranging_or_weak"
    elif ema5 > ema13 and close > ema200:
        regime_label = "trending_up"
    elif ema5 < ema13 and close < ema200:
        regime_label = "trending_down"
    else:
        regime_label = "mixed_trend"
    if rsi7 >= 70.0:
        rsi_state = "high_momentum_overbought_zone"
    elif rsi7 <= 30.0:
        rsi_state = "low_momentum_oversold_zone"
    elif rsi7 > 50.0:
        rsi_state = "bullish_half"
    elif rsi7 < 50.0:
        rsi_state = "bearish_half"
    else:
        rsi_state = "neutral_50"
    if bid_share >= _safe_float(getattr(settings, "SPARTAN_ORDERBOOK_SHARE_THRESHOLD", 0.60), 0.60):
        flow_bias = "bid_heavy"
    elif ask_share >= _safe_float(getattr(settings, "SPARTAN_ORDERBOOK_SHARE_THRESHOLD", 0.60), 0.60):
        flow_bias = "ask_heavy"
    else:
        flow_bias = "balanced_or_weak"
    derived_context = {
        "regime": regime_label,
        "rsi_state": rsi_state,
        "price_vs_ema200_pct": ((entry_reference - ema200) / abs(ema200) * 100.0) if abs(ema200) > 1e-12 else None,
        "ema5_minus_ema13_atr": ((ema5 - ema13) / atr_value) if atr_value > 1e-12 else None,
        "spread_atr_ratio": spread_atr,
        "order_flow_bias": flow_bias,
        "price_minus_poc_atr": ((entry_reference - _safe_float(vp.get("poc"))) / atr_value) if vp.get("available") and atr_value > 1e-12 else None,
        "price_minus_vah_atr": ((entry_reference - _safe_float(vp.get("vah"))) / atr_value) if vp.get("available") and atr_value > 1e-12 else None,
        "price_minus_val_atr": ((entry_reference - _safe_float(vp.get("val"))) / atr_value) if vp.get("available") and atr_value > 1e-12 else None,
        "news_known": bool(news.get("available")),
        "news_locked": bool(news.get("locked")),
        "session_allowed": bool(session.get("allowed")),
        "candidate_alignment_agents": aligned,
        "candidate_opposed_agents": opposed,
    }

    snapshot = {
        "schema_version": "spartan-pro-1.1",
        "enabled": True,
        "hard_gate_enabled": hard_gate_enabled,
        "approved": not hard_reasons,
        "reason": (
            ("all_spartan_pro_gates_passed" if hard_gate_enabled else "context_only_ai_review")
            if not hard_reasons else ";".join(hard_reasons)
        ),
        "symbol": symbol,
        "symbol_kind": kind,
        "timestamp_utc": now.isoformat(),
        "candidate": {"side": side, "action": _side_vote(side), "confluence_score": score, "confidence": confidence},
        "market": {"bid": bid, "ask": ask, "spread": spread, "spread_atr": spread_atr, "tick_age_seconds": tick_age},
        "technical": {
            "close": close, "ema5": ema5, "ema13": ema13, "ema200": ema200,
            "rsi7": rsi7, "atr14": atr_value, "adx14": adx_value,
        },
        "derived_context": derived_context,
        "conditions": side_conditions,
        "condition_scores": {
            "long": sum(1 for value in long_conditions.values() if value is True),
            "short": sum(1 for value in short_conditions.values() if value is True),
            "available_passed": available_passed,
            "available_total": available_total,
            "available_ratio": available_ratio,
            "core_passed": core_passed,
            "core_total": len(core_keys),
        },
        "order_flow": {
            "available": bool(micro.get("available")), "bid_share": bid_share, "ask_share": ask_share,
            "imbalance_pct": imbalance_pct, "score": _safe_float(micro.get("score")),
            "persistence": _safe_float(micro.get("persistence")), "change": _safe_float(micro.get("change")),
            "avg_5s": micro.get("avg_5s"), "avg_15s": micro.get("avg_15s"), "avg_30s": micro.get("avg_30s"),
        },
        "smc": smc,
        "tick_flow": tick_flow,
        "volume_profile": vp,
        "higher_timeframes": htf,
        "session": session,
        "news": news,
        "agents_vote": agents,
        "daily_trade_count": int(daily_trade_count),
        "micro_gate_profile": gate_profile,
        "hard_reasons": hard_reasons,
    }
    persist_snapshot(snapshot)
    return snapshot


def persist_snapshot(snapshot: dict[str, Any]) -> None:
    try:
        reports = Path(getattr(settings, "REPORTS_DIR", Path(__file__).resolve().parent / "reports"))
        reports.mkdir(parents=True, exist_ok=True)
        symbol = str(snapshot.get("symbol") or "UNKNOWN").replace("/", "_")
        target = reports / f"spartan_pro_last_{symbol}.json"
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_text(json.dumps(snapshot, indent=2, default=str), encoding="utf-8")
        temporary.replace(target)
    except Exception:
        pass


def offline_self_test() -> dict[str, Any]:
    rng = np.random.default_rng(240827)
    count = 600
    drift = np.linspace(0.0, 14.0, count)
    close = 2400.0 + drift + np.cumsum(rng.normal(0.0, 0.12, count))
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) + rng.uniform(0.08, 0.35, count)
    low = np.minimum(open_, close) - rng.uniform(0.08, 0.35, count)
    frame = pd.DataFrame({
        "time": pd.date_range("2026-01-01", periods=count, freq="min", tz="UTC"),
        "open": open_, "high": high, "low": low, "close": close,
        "tick_volume": rng.integers(50, 500, count), "real_volume": np.zeros(count),
    })
    featured = ensure_features(frame)
    assert {"ema_5", "ema_13", "ema_200", "rsi_7", "atr_14", "adx_14"}.issubset(featured.columns)
    profile = volume_profile(featured)
    assert profile.get("available")
    smc = smc_snapshot(featured)
    assert smc.get("available")
    assert session_status("BTCUSDm")["allowed"]
    # Fixed Pakistan-time windows: 15:30 PKT is London-only; 21:00 PKT is NY and Gold overlap.
    london_utc = datetime(2026, 1, 15, 10, 30, tzinfo=UTC)
    ny_utc = datetime(2026, 1, 15, 16, 0, tzinfo=UTC)
    off_utc = datetime(2026, 1, 15, 23, 30, tzinfo=UTC)  # 04:30 PKT
    assert session_status("XAUUSDm", london_utc)["allowed"]
    assert not session_status("USOILm", london_utc)["allowed"]
    assert session_status("XAUUSDm", ny_utc)["allowed"]
    assert session_status("USOILm", ny_utc)["allowed"]
    assert not session_status("XAUUSDm", off_utc)["allowed"]
    return {"features": True, "volume_profile": True, "smc": True, "session": True}
