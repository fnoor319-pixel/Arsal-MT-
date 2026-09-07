from __future__ import annotations

from typing import Any

import settings


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def superlearner_borderline_eligible(decision: Any) -> tuple[bool, dict[str, Any]]:
    """Allow only a narrow *soft* SuperLearner reject to ask Luna for a second opinion.

    Deterministic market/risk gates are upstream and remain non-overridable.  This
    function is intentionally conservative: a candidate must already have a
    positive expected-R estimate and be close to the calibrated probability gate.
    """
    enabled = bool(getattr(settings, "SPARTAN_GPT_BORDERLINE_REVIEW_ENABLED", True))
    probability = _f(getattr(decision, "probability", 0.0))
    threshold = _f(getattr(decision, "threshold", 1.0), 1.0)
    gap = max(0.0, threshold - probability)
    reason = str(getattr(decision, "reason", "") or "")
    approved = bool(getattr(decision, "approved", False))
    active = bool(getattr(decision, "probability_active", False))
    market = getattr(decision, "market", {}) or {}
    expected_r = _f(market.get("expected_r"), -999.0) if isinstance(market, dict) else -999.0

    hard_markers = (
        "persistent DOM/microstructure pressure",
        "simultaneously random, noisy and drifting",
        "Bayesian setup loss probability",
    )
    hard_reject = any(marker in reason for marker in hard_markers)
    soft_markers = (
        "calibrated scalp probability",
        "online model probability",
        "expected edge only",
    )
    has_soft_reason = any(marker in reason for marker in soft_markers)

    # V6.9.4: expectancy-aware Luna bridge.  The old fixed 0.44 minimum
    # could make a valid soft reject impossible to review when the calibrated
    # scalp threshold itself had fallen to ~0.40.  Use a dynamic floor that
    # remains above break-even, remains close to the SuperLearner threshold,
    # and still requires meaningful positive expected-R.
    max_gap = _f(getattr(settings, "SPARTAN_GPT_BORDERLINE_MAX_GAP", 0.08), 0.08)
    absolute_floor = _f(getattr(settings, "SPARTAN_GPT_BORDERLINE_MIN_PROBABILITY", 0.30), 0.30)
    break_even_margin = _f(getattr(settings, "SPARTAN_GPT_BORDERLINE_BREAK_EVEN_MARGIN", 0.05), 0.05)
    min_expected_r = _f(getattr(settings, "SPARTAN_GPT_BORDERLINE_MIN_EXPECTED_R", 0.15), 0.15)
    break_even_probability = (
        _f(market.get("break_even_probability"), 0.50)
        if isinstance(market, dict) else 0.50
    )
    dynamic_min_probability = max(
        absolute_floor,
        break_even_probability + break_even_margin,
        threshold - max_gap,
    )
    # A second opinion must still sit below the actual approval threshold; if
    # configuration collapses that lane, fail closed instead of widening it.
    lane_exists = dynamic_min_probability < threshold
    eligible = bool(
        enabled
        and not approved
        and active
        and has_soft_reason
        and not hard_reject
        and lane_exists
        and probability >= dynamic_min_probability
        and gap <= max_gap
        and expected_r >= min_expected_r
    )
    return eligible, {
        "eligible": eligible,
        "probability": probability,
        "threshold": threshold,
        "gap": gap,
        "expected_r": expected_r,
        "min_probability": dynamic_min_probability,
        "absolute_floor": absolute_floor,
        "break_even_probability": break_even_probability,
        "break_even_margin": break_even_margin,
        "max_gap": max_gap,
        "min_expected_r": min_expected_r,
        "lane_exists": lane_exists,
        "reason": reason,
        "hard_reject": hard_reject,
    }


def daily_rescue_trade_count(connection: Any) -> int:
    try:
        row = connection.execute(
            """
            SELECT COUNT(*) AS n
            FROM decision_logs
            WHERE mode='demo'
              AND reason='Demo order sent'
              AND substr(timestamp,1,10)=substr(datetime('now'),1,10)
              AND details_json LIKE '%\"gpt_borderline_rescue\":true%'
            """
        ).fetchone()
        return int(row["n"] if row else 0)
    except Exception:
        return 0


def rescue_trade_slot_available(connection: Any) -> tuple[bool, int, int]:
    used = daily_rescue_trade_count(connection)
    cap = max(0, int(getattr(settings, "SPARTAN_GPT_BORDERLINE_MAX_TRADES_PER_DAY", 2)))
    return used < cap, used, cap
