from __future__ import annotations

import json
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


def ensure_tables(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS v9_execution_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            symbol TEXT NOT NULL,
            strategy_id INTEGER,
            side INTEGER NOT NULL,
            playbook TEXT,
            execute INTEGER NOT NULL,
            grade TEXT,
            pre_score REAL NOT NULL,
            final_score REAL NOT NULL,
            risk_multiplier REAL NOT NULL,
            micro_score REAL,
            expected_r REAL,
            probability REAL,
            break_even_probability REAL,
            probability_edge REAL,
            super_approved INTEGER,
            spartan_approved INTEGER,
            adaptive_approved INTEGER,
            edge_allowed INTEGER,
            luna_decision TEXT,
            luna_confidence REAL,
            luna_source TEXT,
            reason TEXT,
            context_json TEXT
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_v9_execution_recent ON v9_execution_decisions(timestamp,symbol)"
    )


def daily_trade_counts(connection: sqlite3.Connection, symbol: str) -> tuple[int, int]:
    today = datetime.now(timezone.utc).date().isoformat()
    total = connection.execute(
        "SELECT COUNT(*) AS n FROM demo_positions WHERE substr(opened_at,1,10)=? AND execution_tier LIKE 'v9%'",
        (today,),
    ).fetchone()
    per_symbol = connection.execute(
        "SELECT COUNT(*) AS n FROM demo_positions WHERE substr(opened_at,1,10)=? AND execution_tier LIKE 'v9%' AND symbol=?",
        (today, str(symbol)),
    ).fetchone()
    def _n(row: Any) -> int:
        if row is None:
            return 0
        try:
            return int(row["n"] or 0)
        except Exception:
            return int(row[0] or 0)
    return _n(total), _n(per_symbol)


def trade_slot_available(connection: sqlite3.Connection, symbol: str) -> tuple[bool, dict[str, Any]]:
    total, per_symbol = daily_trade_counts(connection, symbol)
    total_cap = max(1, int(getattr(settings, "V9_MAX_TRADES_PER_DAY", 15)))
    symbol_cap = max(1, int(getattr(settings, "V9_MAX_TRADES_PER_SYMBOL_PER_DAY", 6)))
    allowed = total < total_cap and per_symbol < symbol_cap
    return allowed, {
        "total": total,
        "per_symbol": per_symbol,
        "total_cap": total_cap,
        "symbol_cap": symbol_cap,
    }


def spread_limit_atr(hunter_take_atr: float) -> float:
    """Cost-aware spread limit for V9 micro entries.

    The legacy 0.18 ATR ceiling remains the floor. V9 may accept a slightly wider
    spread only when the playbook's intended reward distance can absorb it. There
    is still a strict absolute ceiling, so this is not a blanket spread loosening.
    """
    legacy = _f(getattr(settings, "MAX_SPREAD_ATR_FRACTION", 0.18), 0.18)
    absolute = _f(getattr(settings, "V9_ABSOLUTE_MAX_SPREAD_ATR", 0.28), 0.28)
    cost_to_target = _f(getattr(settings, "V9_MAX_SPREAD_TO_TARGET_FRACTION", 0.16), 0.16)
    reward_supported = max(0.0, _f(hunter_take_atr, 0.0)) * max(0.01, cost_to_target)
    return _clamp(max(legacy, reward_supported), legacy, absolute)


def _break_even(super_decision: Any) -> float:
    market = getattr(super_decision, "market", {}) or {}
    raw = market.get("break_even_probability")
    if raw is not None:
        return _clamp(_f(raw, 0.5), 0.01, 0.99)
    rr = _f(market.get("reward_to_risk"), 0.0)
    return 1.0 / (1.0 + rr) if rr > 0 else 0.5




def precision_memory_adjustment(
    *,
    micro_score: float,
    expected_r: float,
    probability_edge: float,
    playbook_memory: dict[str, Any] | None,
    adaptive_gate: dict[str, Any] | None,
) -> dict[str, Any]:
    """Convert broker-DEMO outcome memory into a bounded score/risk bias.

    Small samples stay close to neutral. Repeatedly weak symbol/playbook/side
    cells lose score; repeatedly strong cells gain only a modest bonus. After a
    loss streak, a strong fresh setup may enter recovery mode, but recovery
    never increases size (no martingale).
    """
    mem = playbook_memory or {}
    adaptive = adaptive_gate or {}
    snap = adaptive.get("snapshot") if isinstance(adaptive.get("snapshot"), dict) else {}

    n = max(0, int(mem.get("observations") or 0))
    raw_win = _clamp(_f(mem.get("win_rate"), 0.50), 0.0, 1.0)
    smoothed_win = ((raw_win * n) + 2.0) / (n + 4.0)  # Beta(2,2) prior
    ewma = _f(mem.get("reward_ewma"), 0.0)
    mean_r = _f(mem.get("reward_mean"), 0.0)
    mfe = max(0.0, _f(mem.get("mfe_mean"), 0.0))
    mae = max(0.0, _f(mem.get("mae_mean"), 0.0))
    streak = max(0, int(snap.get("consecutive_losses") or 0))

    min_n = max(1, int(getattr(settings, "V9_PRECISION_MEMORY_MIN_OBSERVATIONS", 4)))
    score_delta = 0.0
    memory_state = "neutral"
    if n >= min_n:
        score_delta += _clamp((smoothed_win - 0.50) * 20.0, -4.0, 4.0)
        score_delta += _clamp(ewma * 8.0, -4.0, 4.0)
        if mfe > 0 or mae > 0:
            score_delta += _clamp((mfe - mae) * 2.0, -2.0, 2.0)

        weak_win = _f(getattr(settings, "V9_PRECISION_WEAK_WIN_RATE", 0.35), 0.35)
        strong_win = _f(getattr(settings, "V9_PRECISION_STRONG_WIN_RATE", 0.58), 0.58)
        weak_ewma = _f(getattr(settings, "V9_PRECISION_WEAK_EWMA_R", -0.08), -0.08)
        strong_ewma = _f(getattr(settings, "V9_PRECISION_STRONG_EWMA_R", 0.10), 0.10)
        if smoothed_win <= weak_win and ewma < weak_ewma:
            score_delta -= 5.0
            memory_state = "weak"
        elif smoothed_win >= strong_win and ewma > strong_ewma and mean_r > 0:
            score_delta += 3.0
            memory_state = "strong"
        else:
            memory_state = "mixed"

    if streak >= 2:
        score_delta -= min(6.0, 2.0 * (streak - 1))

    recovery = bool(
        getattr(settings, "V9_RECOVERY_MODE_ENABLED", True)
        and streak > 0
        and _f(micro_score) >= _f(getattr(settings, "V9_RECOVERY_MIN_MICRO_SCORE", 86.0), 86.0)
        and _f(expected_r) >= _f(getattr(settings, "V9_RECOVERY_MIN_EXPECTED_R", 0.45), 0.45)
        and _f(probability_edge) >= _f(getattr(settings, "V9_RECOVERY_MIN_PROB_EDGE", 0.05), 0.05)
    )
    recovery_risk_cap = (
        _f(getattr(settings, "V9_RECOVERY_MAX_RISK_MULTIPLIER", 0.55), 0.55)
        if recovery else 1.0
    )

    hard_n = max(min_n, int(getattr(settings, "V9_PRECISION_HARD_HOLD_MIN_OBSERVATIONS", 10)))
    hard_win = _f(getattr(settings, "V9_PRECISION_HARD_HOLD_WIN_RATE", 0.25), 0.25)
    hard_ewma = _f(getattr(settings, "V9_PRECISION_HARD_HOLD_EWMA_R", -0.20), -0.20)
    memory_hard_hold = bool(
        n >= hard_n and smoothed_win < hard_win and ewma < hard_ewma
        and mean_r < 0.0 and not recovery
    )
    return {
        "observations": n,
        "raw_win_rate": round(raw_win, 4),
        "smoothed_win_rate": round(smoothed_win, 4),
        "reward_ewma": round(ewma, 4),
        "reward_mean": round(mean_r, 4),
        "mfe_mean": round(mfe, 4),
        "mae_mean": round(mae, 4),
        "strategy_loss_streak": streak,
        "score_delta": round(_clamp(score_delta, -14.0, 8.0), 2),
        "memory_state": memory_state,
        "hard_hold": memory_hard_hold,
        "recovery_mode": recovery,
        "recovery_risk_cap": round(_clamp(recovery_risk_cap, 0.20, 1.0), 4),
    }


def pre_luna_score(
    *,
    micro_score: float,
    super_decision: Any,
    spartan_snapshot: dict[str, Any] | None,
    adaptive_allowed: bool,
    adaptive_gate: dict[str, Any] | None,
    edge_allowed: bool,
    playbook_memory: dict[str, Any] | None = None,
) -> dict[str, Any]:
    market = getattr(super_decision, "market", {}) or {}
    p = _clamp(_f(getattr(super_decision, "probability", 0.5), 0.5), 0.0, 1.0)
    be = _break_even(super_decision)
    edge = p - be
    er = _f(market.get("expected_r"), 0.0)

    snap = spartan_snapshot or {}
    candidate = snap.get("candidate") if isinstance(snap.get("candidate"), dict) else {}
    scores = snap.get("condition_scores") if isinstance(snap.get("condition_scores"), dict) else {}
    sp_conf = _clamp(_f(candidate.get("confidence"), 0.5), 0.0, 1.0)
    ratio = _clamp(_f(scores.get("available_ratio"), 0.5), 0.0, 1.0)
    sp_score = 100.0 * (0.55 * sp_conf + 0.45 * ratio)

    adaptive = adaptive_gate or {}
    snap_learning = adaptive.get("snapshot") if isinstance(adaptive.get("snapshot"), dict) else {}
    adaptive_conf = _clamp(_f(snap_learning.get("confidence"), 0.5), 0.0, 1.0)

    micro_component = _clamp(micro_score, 0.0, 100.0)
    er_component = 100.0 * _clamp((er + 0.20) / 1.20, 0.0, 1.0)
    edge_component = 100.0 * _clamp((edge + 0.12) / 0.30, 0.0, 1.0)
    adaptive_component = 100.0 * adaptive_conf
    edge_memory_component = 100.0 if edge_allowed else 45.0

    pre = (
        0.44 * micro_component
        + 0.22 * er_component
        + 0.12 * edge_component
        + 0.10 * sp_score
        + 0.06 * adaptive_component
        + 0.06 * edge_memory_component
    )

    memory = precision_memory_adjustment(
        micro_score=micro_component, expected_r=er, probability_edge=edge,
        playbook_memory=playbook_memory, adaptive_gate=adaptive_gate,
    )
    pre += _f(memory.get("score_delta"), 0.0)

    super_active = bool(getattr(super_decision, "probability_active", True))
    hard_er = _f(getattr(settings, "V9_HARD_MIN_EXPECTED_R", 0.10), 0.10)
    hard_edge = _f(getattr(settings, "V9_HARD_MIN_PROB_EDGE", 0.00), 0.00)
    quant_hard_hold = bool(super_active and (er < hard_er or edge < hard_edge))
    memory_hard_hold = bool(memory.get("hard_hold"))
    hard_hold = bool(quant_hard_hold or memory_hard_hold)
    if quant_hard_hold:
        reason = f"precision Quant hold (ER={er:+.2f}R edge={edge:+.2f})"
    elif memory_hard_hold:
        reason = (
            "precision memory hold "
            f"(n={int(memory.get('observations') or 0)} "
            f"win={_f(memory.get('smoothed_win_rate')):.2f} "
            f"ewma={_f(memory.get('reward_ewma')):+.2f}R)"
        )
    else:
        reason = "precision Quant/memory evidence usable"
    return {
        "pre_score": round(_clamp(pre, 0.0, 100.0), 2),
        "micro_score": round(micro_component, 2),
        "expected_r": round(er, 4),
        "probability": round(p, 4),
        "break_even_probability": round(be, 4),
        "probability_edge": round(edge, 4),
        "spartan_score": round(sp_score, 2),
        "spartan_approved": bool(snap.get("approved", True)),
        "adaptive_approved": bool(adaptive_allowed),
        "edge_allowed": bool(edge_allowed),
        "precision_memory": memory,
        "recovery_mode": bool(memory.get("recovery_mode")),
        "recovery_risk_cap": _f(memory.get("recovery_risk_cap"), 1.0),
        "hard_hold": hard_hold,
        "reason": reason,
    }


def should_call_luna(pre: dict[str, Any]) -> bool:
    return bool(
        getattr(settings, "V9_LUNA_ADVISORY_ENABLED", True)
        and not bool(pre.get("hard_hold"))
        and _f(pre.get("pre_score")) >= _f(getattr(settings, "V9_LUNA_ADVISORY_MIN_SCORE", 82.0), 82.0)
        and _f(pre.get("expected_r")) >= _f(getattr(settings, "V9_LUNA_ADVISORY_MIN_EXPECTED_R", 0.10), 0.10)
    )


def final_decision(
    *,
    pre: dict[str, Any],
    super_decision: Any,
    spartan_snapshot: dict[str, Any] | None,
    adaptive_allowed: bool,
    edge_allowed: bool,
    luna_review: dict[str, Any] | None,
) -> dict[str, Any]:
    final_score = _f(pre.get("pre_score"), 0.0)
    review = luna_review or {}
    luna_decision = str(review.get("decision") or "not_requested").lower()
    luna_conf = _clamp(_f(review.get("confidence"), 0.0), 0.0, 1.0)
    luna_source = str(review.get("decision_source") or "not_requested")

    # Luna is a second brain, not a kill switch. Strong confirmations add weight;
    # strong HOLDs reduce size/score but do not erase an otherwise valid DEMO alpha.
    if luna_decision == "confirm":
        final_score += 4.0 + 5.0 * luna_conf
    elif luna_decision == "hold":
        final_score -= 3.0 + 5.0 * luna_conf

    spartan_ok = bool((spartan_snapshot or {}).get("approved", True))
    super_ok = bool(getattr(super_decision, "approved", False))
    if not super_ok:
        final_score -= 4.0
    if not spartan_ok:
        final_score -= 4.0
    if not adaptive_allowed:
        final_score -= 3.0
    if not edge_allowed:
        final_score -= 3.0

    final_score = _clamp(final_score, 0.0, 100.0)
    min_score = _f(pre.get("execute_floor"), _f(getattr(settings, "V9_EXECUTE_SCORE_MIN", 55.0), 55.0))
    hard_hold = bool(pre.get("hard_hold"))
    execute = bool(not hard_hold and final_score >= min_score)

    if final_score >= 82.0:
        grade, risk = "A+", 1.00
    elif final_score >= 72.0:
        grade, risk = "A", 0.75
    elif final_score >= 62.0:
        grade, risk = "B", 0.50
    else:
        grade, risk = "C", 0.35

    # Advisory disagreements reduce risk rather than vetoing the trade.
    if not super_ok:
        risk *= 0.70
    if not spartan_ok:
        risk *= 0.78
    if not adaptive_allowed:
        risk *= 0.75
    if not edge_allowed:
        risk *= 0.70
    if luna_decision == "hold":
        risk *= max(0.55, 1.0 - 0.35 * luna_conf)
    elif luna_decision == "confirm":
        risk *= min(1.05, 0.95 + 0.10 * luna_conf)

    # Recovery after a loss is never martingale. A strong new setup can trade,
    # but its risk is capped until broker evidence improves.
    risk = min(risk, _f(pre.get("recovery_risk_cap"), 1.0))
    # V9.2 contextual/microstructure/session learning adjusts allocation without
    # becoming a new serial veto layer. New context cells are trial-capped.
    risk *= _clamp(_f(pre.get("v92_risk_multiplier"), 1.0), 0.35, 1.08)
    risk = min(risk, _clamp(_f(pre.get("v92_risk_cap"), 1.0), 0.20, 1.0))
    risk = _clamp(risk, _f(getattr(settings, "V9_MIN_RISK_MULTIPLIER", 0.20), 0.20), 1.0)
    reason = (
        str(pre.get("reason") or "hard quant hold")
        if hard_hold
        else (f"ensemble {final_score:.1f} below execute floor {min_score:.1f}" if not execute
              else f"execution-first {grade} ensemble")
    )
    return {
        **pre,
        "execute": execute,
        "final_score": round(final_score, 2),
        "grade": grade,
        "risk_multiplier": round(risk, 4),
        "luna_decision": luna_decision,
        "luna_confidence": round(luna_conf, 4),
        "luna_source": luna_source,
        "super_approved": super_ok,
        "spartan_approved": spartan_ok,
        "adaptive_approved": bool(adaptive_allowed),
        "edge_allowed": bool(edge_allowed),
        "reason": reason,
    }


def record_decision(
    connection: sqlite3.Connection,
    *,
    symbol: str,
    strategy_id: int,
    side: int,
    playbook: str,
    decision: dict[str, Any],
    context: dict[str, Any] | None = None,
) -> None:
    ensure_tables(connection)
    connection.execute(
        """
        INSERT INTO v9_execution_decisions(
            timestamp,symbol,strategy_id,side,playbook,execute,grade,pre_score,final_score,
            risk_multiplier,micro_score,expected_r,probability,break_even_probability,
            probability_edge,super_approved,spartan_approved,adaptive_approved,edge_allowed,
            luna_decision,luna_confidence,luna_source,reason,context_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            _now(), str(symbol), int(strategy_id), int(side), str(playbook),
            1 if decision.get("execute") else 0, str(decision.get("grade") or ""),
            _f(decision.get("pre_score")), _f(decision.get("final_score")),
            _f(decision.get("risk_multiplier")), _f(decision.get("micro_score")),
            _f(decision.get("expected_r")), _f(decision.get("probability")),
            _f(decision.get("break_even_probability")), _f(decision.get("probability_edge")),
            1 if decision.get("super_approved") else 0,
            1 if decision.get("spartan_approved") else 0,
            1 if decision.get("adaptive_approved") else 0,
            1 if decision.get("edge_allowed") else 0,
            str(decision.get("luna_decision") or ""), _f(decision.get("luna_confidence")),
            str(decision.get("luna_source") or ""), str(decision.get("reason") or ""),
            json.dumps(context or {}, separators=(",", ":"), default=str),
        ),
    )
