from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable

import settings


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _utc_date() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def ensure_tables(connection: sqlite3.Connection) -> None:
    """Create additive GPT-learning tables without touching legacy learning data."""
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS gpt_candidate_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fingerprint TEXT NOT NULL UNIQUE,
            reviewed_at TEXT NOT NULL,
            symbol TEXT NOT NULL,
            strategy_id INTEGER,
            family TEXT,
            regime TEXT,
            side INTEGER NOT NULL,
            bar_time TEXT NOT NULL,
            api_called INTEGER NOT NULL DEFAULT 0,
            cache_reuses INTEGER NOT NULL DEFAULT 0,
            decision TEXT NOT NULL,
            approved INTEGER NOT NULL DEFAULT 0,
            confidence REAL NOT NULL DEFAULT 0,
            model TEXT,
            decision_source TEXT,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            total_tokens INTEGER NOT NULL DEFAULT 0,
            signature_json TEXT NOT NULL,
            snapshot_json TEXT NOT NULL,
            response_json TEXT NOT NULL,
            outcome_status TEXT NOT NULL DEFAULT 'pending',
            outcome_reward_r REAL,
            outcome_label TEXT,
            outcome_closed_at TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_gpt_candidate_reviews_symbol_time
            ON gpt_candidate_reviews(symbol, reviewed_at);
        CREATE INDEX IF NOT EXISTS idx_gpt_candidate_reviews_outcome
            ON gpt_candidate_reviews(outcome_status, symbol, reviewed_at);

        CREATE TABLE IF NOT EXISTS gpt_veto_shadow_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            review_id INTEGER NOT NULL UNIQUE,
            fingerprint TEXT NOT NULL UNIQUE,
            symbol TEXT NOT NULL,
            strategy_id INTEGER,
            family TEXT,
            regime TEXT,
            side INTEGER NOT NULL,
            entry_price REAL NOT NULL,
            stop_loss REAL NOT NULL,
            take_profit REAL NOT NULL,
            risk_distance REAL NOT NULL,
            opened_bar_time TEXT NOT NULL,
            opened_at TEXT NOT NULL,
            max_hold_bars INTEGER NOT NULL DEFAULT 15,
            status TEXT NOT NULL DEFAULT 'open',
            closed_at TEXT,
            exit_price REAL,
            reward_r REAL,
            close_reason TEXT,
            outcome_label TEXT,
            superlearner_features_json TEXT NOT NULL DEFAULT '{}',
            context_json TEXT NOT NULL DEFAULT '{}',
            FOREIGN KEY(review_id) REFERENCES gpt_candidate_reviews(id)
        );

        CREATE INDEX IF NOT EXISTS idx_gpt_veto_shadow_open
            ON gpt_veto_shadow_positions(status, symbol, opened_at);

        CREATE TABLE IF NOT EXISTS gpt_decision_memory (
            symbol TEXT NOT NULL,
            regime TEXT NOT NULL,
            side INTEGER NOT NULL,
            decision_type TEXT NOT NULL,
            observations INTEGER NOT NULL DEFAULT 0,
            positive_outcomes INTEGER NOT NULL DEFAULT 0,
            negative_outcomes INTEGER NOT NULL DEFAULT 0,
            neutral_outcomes INTEGER NOT NULL DEFAULT 0,
            reward_sum REAL NOT NULL DEFAULT 0,
            reward_mean REAL NOT NULL DEFAULT 0,
            quality_ewma REAL NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(symbol, regime, side, decision_type)
        );

        CREATE TABLE IF NOT EXISTS gpt_usage_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            call_type TEXT NOT NULL,
            symbol TEXT,
            fingerprint TEXT,
            model TEXT,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            total_tokens INTEGER NOT NULL DEFAULT 0,
            source TEXT NOT NULL DEFAULT 'openai'
        );

        CREATE INDEX IF NOT EXISTS idx_gpt_usage_events_time
            ON gpt_usage_events(timestamp, call_type);
        """
    )


def _bucket(value: Any, step: float, default: float = 0.0) -> float:
    number = _safe_float(value, default)
    if step <= 0:
        return number
    return round(number / step) * step


def _direction(value: Any) -> Any:
    if isinstance(value, dict):
        for key in ("direction", "side", "type", "bias", "state"):
            if key in value and value.get(key) is not None:
                return value.get(key)
    return value


def build_fingerprint(
    snapshot: dict[str, Any],
    *,
    strategy_id: int,
    family: str,
    regime: str,
    side: int,
    bar_time: Any,
) -> tuple[str, dict[str, Any]]:
    """Fingerprint material market state, not every tiny tick.

    Same symbol/candle/direction only re-calls GPT when a meaningful bucket changes:
    price ~0.25 ATR, order-flow ~5 percentage points, ML ~5 points, confluence,
    SMC structure, HTF direction, regime, or news/session state.
    """
    market = snapshot.get("market", {}) if isinstance(snapshot, dict) else {}
    technical = snapshot.get("technical", {}) if isinstance(snapshot, dict) else {}
    derived = snapshot.get("derived_context", {}) if isinstance(snapshot, dict) else {}
    order_flow = snapshot.get("order_flow", {}) if isinstance(snapshot, dict) else {}
    smc = snapshot.get("smc", {}) if isinstance(snapshot, dict) else {}
    htf = snapshot.get("higher_timeframes", {}) if isinstance(snapshot, dict) else {}
    news = snapshot.get("news", {}) if isinstance(snapshot, dict) else {}
    session = snapshot.get("session", {}) if isinstance(snapshot, dict) else {}
    candidate = snapshot.get("candidate", {}) if isinstance(snapshot, dict) else {}
    ml = snapshot.get("ml", {}) if isinstance(snapshot, dict) else {}

    atr = max(1e-9, _safe_float(technical.get("atr14"), 0.0))
    bid = _safe_float(market.get("bid"))
    ask = _safe_float(market.get("ask"))
    mid = (bid + ask) / 2.0 if bid and ask else max(bid, ask, _safe_float(technical.get("close")))
    price_bucket = int(round(mid / max(atr * 0.25, 1e-9))) if mid else 0

    htf_compact: dict[str, Any] = {}
    if isinstance(htf, dict):
        for tf, values in sorted(htf.items()):
            if isinstance(values, dict):
                htf_compact[str(tf)] = values.get("trend") or values.get("structure") or values.get("bias")

    signature = {
        "symbol": str(snapshot.get("symbol") or ""),
        "family": str(family),
        "regime": str(regime),
        "side": int(side),
        "bar_time": str(bar_time),
        "candidate_action": str(candidate.get("action") or ""),
        "review_mode": str(snapshot.get("review_mode") or "final"),
        "confluence": int(candidate.get("confluence_score") or 0),
        "price_bucket_025atr": price_bucket,
        "rsi_bucket": _bucket(technical.get("rsi7"), 2.5),
        "adx_bucket": _bucket(technical.get("adx14"), 2.5),
        "spread_atr_bucket": _bucket(market.get("spread_atr"), 0.02),
        "bid_share_bucket": _bucket(order_flow.get("bid_share"), 0.05),
        "imbalance_bucket": _bucket(order_flow.get("imbalance_pct"), 5.0),
        "flow_persistence_bucket": _bucket(order_flow.get("persistence"), 0.10),
        "ml_probability_bucket": _bucket(ml.get("probability"), 0.05),
        "ml_active": bool(ml.get("probability_active")),
        "price_vs_ema200_bucket": _bucket(derived.get("price_vs_ema200_pct"), 0.10),
        "flow_bias": derived.get("order_flow_bias"),
        "smc_bos": _direction(smc.get("bos")) if isinstance(smc, dict) else None,
        "smc_choch": _direction(smc.get("choch")) if isinstance(smc, dict) else None,
        "smc_fvg": _direction(smc.get("fvg")) if isinstance(smc, dict) else None,
        "smc_order_block": _direction(smc.get("order_block")) if isinstance(smc, dict) else None,
        "smc_liquidity_sweep": _direction(smc.get("liquidity_sweep")) if isinstance(smc, dict) else None,
        "htf": htf_compact,
        "news_known": bool(news.get("available")),
        "news_locked": bool(news.get("locked")),
        "session_allowed": bool(session.get("allowed", True)),
    }
    fingerprint = hashlib.sha256(_json(signature).encode("utf-8")).hexdigest()
    return fingerprint, signature


def decision_memory_snapshot(
    connection: sqlite3.Connection,
    symbol: str,
    regime: str,
    side: int,
) -> dict[str, Any]:
    ensure_tables(connection)
    result: dict[str, Any] = {}
    for decision_type in ("confirm", "veto"):
        row = connection.execute(
            """
            SELECT * FROM gpt_decision_memory
            WHERE symbol=? AND regime=? AND side=? AND decision_type=?
            """,
            (symbol, regime, int(side), decision_type),
        ).fetchone()
        if not row:
            result[decision_type] = {
                "observations": 0,
                "positive_rate": None,
                "negative_rate": None,
                "reward_mean": None,
                "quality_ewma": None,
            }
            continue
        observations = int(row["observations"] or 0)
        result[decision_type] = {
            "observations": observations,
            "positive_rate": (int(row["positive_outcomes"] or 0) / observations) if observations else None,
            "negative_rate": (int(row["negative_outcomes"] or 0) / observations) if observations else None,
            "neutral_rate": (int(row["neutral_outcomes"] or 0) / observations) if observations else None,
            "reward_mean": _safe_float(row["reward_mean"]),
            "quality_ewma": _safe_float(row["quality_ewma"]),
            "updated_at": row["updated_at"],
        }
    result["policy"] = {
        "meaning": "Historical GPT confirm/veto outcome calibration; advisory evidence only.",
        "automatic_threshold_mutation": False,
        "veto_shadow_learning": True,
    }
    return result


def _usage_dict(review: dict[str, Any]) -> dict[str, int]:
    usage = review.get("usage") if isinstance(review, dict) else None
    if not isinstance(usage, dict):
        usage = {}
    return {
        "input_tokens": max(0, int(usage.get("input_tokens") or 0)),
        "output_tokens": max(0, int(usage.get("output_tokens") or 0)),
        "total_tokens": max(0, int(usage.get("total_tokens") or 0)),
    }


def record_usage_event(
    connection: sqlite3.Connection,
    *,
    call_type: str,
    model: str,
    usage: dict[str, Any] | None,
    symbol: str | None = None,
    fingerprint: str | None = None,
    source: str = "openai",
) -> None:
    ensure_tables(connection)
    usage = usage or {}
    connection.execute(
        """
        INSERT INTO gpt_usage_events(
            timestamp, call_type, symbol, fingerprint, model,
            input_tokens, output_tokens, total_tokens, source
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            _utc_now(), str(call_type), symbol, fingerprint, str(model or ""),
            max(0, int(usage.get("input_tokens") or 0)),
            max(0, int(usage.get("output_tokens") or 0)),
            max(0, int(usage.get("total_tokens") or 0)),
            str(source),
        ),
    )


def estimated_usage_cost_usd(connection: sqlite3.Connection, *, since: str | None = None) -> float:
    """Conservative bot-only cost estimate from recorded token usage.

    Cached-token discounts are deliberately ignored. Known GPT-5.6 model prices
    are handled separately so an old Terra/Sol test is not priced as Luna.
    """
    ensure_tables(connection)
    where = "WHERE source='openai'"
    params: tuple[Any, ...] = ()
    if since:
        where += " AND timestamp>=?"
        params = (str(since),)
    rows = connection.execute(
        f"SELECT model,COALESCE(SUM(input_tokens),0) i,COALESCE(SUM(output_tokens),0) o "
        f"FROM gpt_usage_events {where} GROUP BY model", params,
    ).fetchall()
    total = 0.0
    for row in rows:
        model = str(row["model"] or "").lower()
        if "gpt-5.6-sol" in model or model == "gpt-5.6":
            in_per_m, out_per_m = 4.0, 20.0
        elif "gpt-5.6-terra" in model:
            in_per_m, out_per_m = 2.0, 12.0
        else:
            in_per_m = max(0.0, _safe_float(getattr(settings, "SPARTAN_GPT_INPUT_USD_PER_MILLION", 0.20), 0.20))
            out_per_m = max(0.0, _safe_float(getattr(settings, "SPARTAN_GPT_OUTPUT_USD_PER_MILLION", 1.20), 1.20))
        total += int(row["i"] or 0) / 1_000_000.0 * in_per_m
        total += int(row["o"] or 0) / 1_000_000.0 * out_per_m
    return total


def api_budget_snapshot(connection: sqlite3.Connection) -> dict[str, Any]:
    budget = max(0.0, _safe_float(getattr(settings, "SPARTAN_GPT_BOT_BUDGET_USD", 4.0), 4.0))
    spent = estimated_usage_cost_usd(connection)
    return {
        "budget_usd": budget,
        "estimated_spent_usd": spent,
        "estimated_remaining_usd": max(0.0, budget - spent),
        "allowed": budget <= 0.0 or spent < budget,
    }


def daily_api_call_count(connection: sqlite3.Connection, call_type: str = "pretrade") -> int:
    ensure_tables(connection)
    row = connection.execute(
        "SELECT COUNT(*) AS n FROM gpt_usage_events WHERE substr(timestamp,1,10)=? AND call_type=? AND source='openai'",
        (_utc_date(), str(call_type)),
    ).fetchone()
    return int(row["n"] if row else 0)


def review_with_cache(
    connection: sqlite3.Connection,
    snapshot: dict[str, Any],
    *,
    strategy_id: int,
    family: str,
    regime: str,
    side: int,
    bar_time: Any,
    reviewer: Callable[[dict[str, Any]], dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Use one GPT review per material market state and persist its evidence."""
    ensure_tables(connection)
    fingerprint, signature = build_fingerprint(
        snapshot,
        strategy_id=strategy_id,
        family=family,
        regime=regime,
        side=side,
        bar_time=bar_time,
    )
    cached = connection.execute(
        "SELECT * FROM gpt_candidate_reviews WHERE fingerprint=?",
        (fingerprint,),
    ).fetchone()
    if cached:
        try:
            review = json.loads(str(cached["response_json"]))
        except Exception:
            review = {}
        connection.execute(
            "UPDATE gpt_candidate_reviews SET cache_reuses=cache_reuses+1 WHERE id=?",
            (int(cached["id"]),),
        )
        review = dict(review) if isinstance(review, dict) else {}
        review["cache_hit"] = True
        review["fingerprint"] = fingerprint
        review["review_id"] = int(cached["id"])
        review["original_decision_source"] = str(cached["decision_source"] or "")
        review["decision_source"] = "decision_fingerprint_cache"
        return review, {
            "cache_hit": True,
            "fingerprint": fingerprint,
            "review_id": int(cached["id"]),
            "signature": signature,
            "api_called": False,
        }

    budget = api_budget_snapshot(connection)
    if not bool(budget.get("allowed", True)):
        review = {
            "enabled": True, "approved": False, "action": "hold", "decision": "hold",
            "confidence": 0.0, "context_summary": "GPT bot budget guard reached.",
            "reasoning": f"Bot GPT budget reached (${budget['estimated_spent_usd']:.4f}/${budget['budget_usd']:.2f}); fail-closed HOLD.",
            "contradictions": ["gpt_bot_budget"], "risk_flags": ["gpt_budget_guard"],
            "agents_vote": snapshot.get("agents_vote", {}), "latency_ms": 0.0,
            "decision_source": "budget_guard", "cache_hit": False, "fingerprint": fingerprint,
            "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        }
        return review, {
            "cache_hit": False, "fingerprint": fingerprint, "review_id": None,
            "signature": signature, "api_called": False, "budget_guard": budget,
        }

    cap = max(1, int(getattr(settings, "SPARTAN_GPT_MAX_PRETRADE_API_CALLS_PER_DAY", 60)))
    used = daily_api_call_count(connection, "pretrade")
    if used >= cap:
        review = {
            "enabled": True,
            "approved": False,
            "action": "hold",
            "decision": "hold",
            "confidence": 0.0,
            "context_summary": "Daily GPT API-call safety cap reached.",
            "reasoning": f"Pre-trade GPT call cap reached ({used}/{cap}); fail-closed HOLD.",
            "contradictions": ["gpt_daily_call_cap"],
            "risk_flags": ["gpt_budget_guard"],
            "agents_vote": snapshot.get("agents_vote", {}),
            "latency_ms": 0.0,
            "decision_source": "daily_call_cap",
            "cache_hit": False,
            "fingerprint": fingerprint,
            "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        }
        return review, {
            "cache_hit": False,
            "fingerprint": fingerprint,
            "review_id": None,
            "signature": signature,
            "api_called": False,
            "daily_call_cap": cap,
            "daily_calls_used": used,
        }

    review = dict(reviewer(snapshot))
    review["cache_hit"] = False
    review["fingerprint"] = fingerprint
    source = str(review.get("decision_source") or "")
    api_called = source == "openai"
    usage = _usage_dict(review)
    if api_called:
        record_usage_event(
            connection,
            call_type="pretrade",
            model=str(review.get("model") or ""),
            usage=usage,
            symbol=str(snapshot.get("symbol") or ""),
            fingerprint=fingerprint,
            source="openai",
        )

    # Cache only valid OpenAI decisions. Runtime/configuration errors can recover
    # on a later cycle and therefore should not poison the whole candle.
    if source == "openai":
        cursor = connection.execute(
            """
            INSERT INTO gpt_candidate_reviews(
                fingerprint, reviewed_at, symbol, strategy_id, family, regime, side,
                bar_time, api_called, decision, approved, confidence, model,
                decision_source, input_tokens, output_tokens, total_tokens,
                signature_json, snapshot_json, response_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fingerprint, _utc_now(), str(snapshot.get("symbol") or "UNKNOWN"),
                int(strategy_id), str(family), str(regime), int(side), str(bar_time),
                str(review.get("decision") or "hold"), int(bool(review.get("approved", False))),
                _safe_float(review.get("confidence")), str(review.get("model") or ""), source,
                usage["input_tokens"], usage["output_tokens"], usage["total_tokens"],
                _json(signature), _json(snapshot), _json(review),
            ),
        )
        review_id = int(cursor.lastrowid)
        review["review_id"] = review_id
    else:
        review_id = None

    return review, {
        "cache_hit": False,
        "fingerprint": fingerprint,
        "review_id": review_id,
        "signature": signature,
        "api_called": api_called,
        "daily_call_cap": cap,
        "daily_calls_used": used + (1 if api_called else 0),
    }


def open_veto_shadow(
    connection: sqlite3.Connection,
    *,
    review_id: int | None,
    fingerprint: str,
    symbol: str,
    strategy_id: int,
    family: str,
    regime: str,
    side: int,
    entry: float,
    stop: float,
    take: float,
    opened_bar_time: Any,
    max_hold_bars: int,
    superlearner_features: dict[str, Any] | None,
    context: dict[str, Any] | None,
) -> int | None:
    ensure_tables(connection)
    if not review_id:
        return None
    risk_distance = abs(_safe_float(entry) - _safe_float(stop))
    if risk_distance <= 1e-12:
        return None
    existing = connection.execute(
        "SELECT id FROM gpt_veto_shadow_positions WHERE fingerprint=?",
        (fingerprint,),
    ).fetchone()
    if existing:
        return int(existing["id"])
    cursor = connection.execute(
        """
        INSERT INTO gpt_veto_shadow_positions(
            review_id, fingerprint, symbol, strategy_id, family, regime, side,
            entry_price, stop_loss, take_profit, risk_distance, opened_bar_time,
            opened_at, max_hold_bars, superlearner_features_json, context_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(review_id), fingerprint, symbol, int(strategy_id), family, regime, int(side),
            _safe_float(entry), _safe_float(stop), _safe_float(take), risk_distance,
            str(opened_bar_time), _utc_now(), max(1, int(max_hold_bars)),
            _json(superlearner_features or {}), _json(context or {}),
        ),
    )
    return int(cursor.lastrowid)


def _update_memory(
    connection: sqlite3.Connection,
    *,
    symbol: str,
    regime: str,
    side: int,
    decision_type: str,
    reward_r: float,
) -> None:
    ensure_tables(connection)
    row = connection.execute(
        "SELECT * FROM gpt_decision_memory WHERE symbol=? AND regime=? AND side=? AND decision_type=?",
        (symbol, regime, int(side), decision_type),
    ).fetchone()
    reward = _safe_float(reward_r)
    positive = 1 if reward > 0.05 else 0
    negative = 1 if reward < -0.05 else 0
    neutral = 1 if not positive and not negative else 0
    # For CONFIRM, positive R is good. For VETO, a negative hypothetical R means
    # GPT correctly avoided a loss, so quality has the opposite sign.
    quality = reward if decision_type == "confirm" else -reward
    alpha = min(1.0, max(0.01, _safe_float(getattr(settings, "SPARTAN_GPT_MEMORY_EWMA_ALPHA", 0.25), 0.25)))
    if row:
        observations = int(row["observations"] or 0) + 1
        reward_sum = _safe_float(row["reward_sum"]) + reward
        previous_ewma = _safe_float(row["quality_ewma"])
        ewma = quality if observations <= 1 else alpha * quality + (1.0 - alpha) * previous_ewma
        connection.execute(
            """
            UPDATE gpt_decision_memory
            SET observations=?, positive_outcomes=positive_outcomes+?,
                negative_outcomes=negative_outcomes+?, neutral_outcomes=neutral_outcomes+?,
                reward_sum=?, reward_mean=?, quality_ewma=?, updated_at=?
            WHERE symbol=? AND regime=? AND side=? AND decision_type=?
            """,
            (
                observations, positive, negative, neutral, reward_sum,
                reward_sum / observations, ewma, _utc_now(),
                symbol, regime, int(side), decision_type,
            ),
        )
    else:
        connection.execute(
            """
            INSERT INTO gpt_decision_memory(
                symbol, regime, side, decision_type, observations,
                positive_outcomes, negative_outcomes, neutral_outcomes,
                reward_sum, reward_mean, quality_ewma, updated_at
            ) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                symbol, regime, int(side), decision_type,
                positive, negative, neutral, reward, reward, quality, _utc_now(),
            ),
        )


def record_confirm_outcome(
    connection: sqlite3.Connection,
    *,
    fingerprint: str | None,
    symbol: str,
    regime: str,
    side: int,
    reward_r: float,
    closed_at: str,
) -> None:
    ensure_tables(connection)
    _update_memory(
        connection,
        symbol=symbol,
        regime=regime,
        side=side,
        decision_type="confirm",
        reward_r=reward_r,
    )
    if fingerprint:
        connection.execute(
            """
            UPDATE gpt_candidate_reviews
            SET outcome_status='closed', outcome_reward_r=?, outcome_label=?, outcome_closed_at=?
            WHERE fingerprint=?
            """,
            (
                _safe_float(reward_r),
                "confirmed_win" if reward_r > 0.05 else ("confirmed_loss" if reward_r < -0.05 else "confirmed_flat"),
                str(closed_at), str(fingerprint),
            ),
        )


def reconcile_veto_shadows(
    connection: sqlite3.Connection,
    *,
    symbol: str,
    bid: float,
    ask: float,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Mark GPT-vetoed candidates to market and learn their counterfactual outcome."""
    ensure_tables(connection)
    now = now or datetime.now(timezone.utc)
    rows = connection.execute(
        "SELECT * FROM gpt_veto_shadow_positions WHERE status='open' AND symbol=? ORDER BY id",
        (symbol,),
    ).fetchall()
    closed: list[dict[str, Any]] = []
    for row in rows:
        side = int(row["side"])
        entry = _safe_float(row["entry_price"])
        stop = _safe_float(row["stop_loss"])
        take = _safe_float(row["take_profit"])
        risk_distance = max(1e-12, _safe_float(row["risk_distance"]))
        close_price = _safe_float(bid if side == 1 else ask)
        close_reason: str | None = None
        exit_price = close_price
        reward_r: float | None = None

        if side == 1 and close_price <= stop:
            close_reason, exit_price, reward_r = "stop_loss", stop, -1.0
        elif side == 1 and close_price >= take:
            close_reason, exit_price = "take_profit", take
            reward_r = (take - entry) / risk_distance
        elif side == -1 and close_price >= stop:
            close_reason, exit_price, reward_r = "stop_loss", stop, -1.0
        elif side == -1 and close_price <= take:
            close_reason, exit_price = "take_profit", take
            reward_r = (entry - take) / risk_distance
        else:
            try:
                opened = datetime.fromisoformat(str(row["opened_at"]))
                if opened.tzinfo is None:
                    opened = opened.replace(tzinfo=timezone.utc)
                held_seconds = (now - opened.astimezone(timezone.utc)).total_seconds()
            except Exception:
                held_seconds = 0.0
            if held_seconds >= max(1, int(row["max_hold_bars"] or 1)) * 60:
                close_reason = "max_hold_timeout"
                reward_r = ((close_price - entry) / risk_distance) * (1 if side == 1 else -1)

        if close_reason is None or reward_r is None:
            continue

        if reward_r >= 0.25:
            outcome_label = "missed_winner"
        elif reward_r <= -0.25:
            outcome_label = "veto_saved_loss"
        else:
            outcome_label = "veto_neutral"
        closed_at = now.isoformat()
        connection.execute(
            """
            UPDATE gpt_veto_shadow_positions
            SET status='closed', closed_at=?, exit_price=?, reward_r=?, close_reason=?, outcome_label=?
            WHERE id=?
            """,
            (closed_at, exit_price, reward_r, close_reason, outcome_label, int(row["id"])),
        )
        connection.execute(
            """
            UPDATE gpt_candidate_reviews
            SET outcome_status='closed', outcome_reward_r=?, outcome_label=?, outcome_closed_at=?
            WHERE id=?
            """,
            (reward_r, outcome_label, closed_at, int(row["review_id"])),
        )
        _update_memory(
            connection,
            symbol=str(row["symbol"]),
            regime=str(row["regime"]),
            side=side,
            decision_type="veto",
            reward_r=reward_r,
        )
        try:
            features = json.loads(str(row["superlearner_features_json"] or "{}"))
        except Exception:
            features = {}
        try:
            context = json.loads(str(row["context_json"] or "{}"))
        except Exception:
            context = {}
        closed.append({
            "id": int(row["id"]),
            "review_id": int(row["review_id"]),
            "fingerprint": str(row["fingerprint"]),
            "symbol": str(row["symbol"]),
            "strategy_id": int(row["strategy_id"] or 0),
            "family": str(row["family"] or "unknown"),
            "regime": str(row["regime"] or "unknown"),
            "side": side,
            "reward_r": _safe_float(reward_r),
            "outcome_label": outcome_label,
            "close_reason": close_reason,
            "closed_at": closed_at,
            "superlearner_features": features if isinstance(features, dict) else {},
            "context": context if isinstance(context, dict) else {},
        })
    return closed


def usage_summary(connection: sqlite3.Connection) -> dict[str, Any]:
    ensure_tables(connection)
    today = _utc_date()
    month = today[:7]
    today_row = connection.execute(
        """
        SELECT COUNT(*) AS calls, COALESCE(SUM(input_tokens),0) AS input_tokens,
               COALESCE(SUM(output_tokens),0) AS output_tokens,
               COALESCE(SUM(total_tokens),0) AS total_tokens
        FROM gpt_usage_events WHERE substr(timestamp,1,10)=?
        """,
        (today,),
    ).fetchone()
    month_row = connection.execute(
        """
        SELECT COUNT(*) AS calls, COALESCE(SUM(input_tokens),0) AS input_tokens,
               COALESCE(SUM(output_tokens),0) AS output_tokens,
               COALESCE(SUM(total_tokens),0) AS total_tokens
        FROM gpt_usage_events WHERE substr(timestamp,1,7)=?
        """,
        (month,),
    ).fetchone()
    cache_row = connection.execute(
        "SELECT COALESCE(SUM(cache_reuses),0) AS n FROM gpt_candidate_reviews WHERE substr(reviewed_at,1,7)=?",
        (month,),
    ).fetchone()
    shadow_row = connection.execute(
        """
        SELECT
            SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) AS open_n,
            SUM(CASE WHEN outcome_label='veto_saved_loss' THEN 1 ELSE 0 END) AS saved_losses,
            SUM(CASE WHEN outcome_label='missed_winner' THEN 1 ELSE 0 END) AS missed_winners,
            SUM(CASE WHEN status='closed' THEN 1 ELSE 0 END) AS closed_n
        FROM gpt_veto_shadow_positions
        """
    ).fetchone()
    def pack(row: sqlite3.Row | None) -> dict[str, int]:
        return {
            "calls": int((row["calls"] if row else 0) or 0),
            "input_tokens": int((row["input_tokens"] if row else 0) or 0),
            "output_tokens": int((row["output_tokens"] if row else 0) or 0),
            "total_tokens": int((row["total_tokens"] if row else 0) or 0),
        }
    return {
        "today": pack(today_row),
        "month": pack(month_row),
        "budget": api_budget_snapshot(connection),
        "cache_reuses_month": int((cache_row["n"] if cache_row else 0) or 0),
        "veto_shadow": {
            "open": int((shadow_row["open_n"] if shadow_row else 0) or 0),
            "closed": int((shadow_row["closed_n"] if shadow_row else 0) or 0),
            "saved_losses": int((shadow_row["saved_losses"] if shadow_row else 0) or 0),
            "missed_winners": int((shadow_row["missed_winners"] if shadow_row else 0) or 0),
        },
    }
