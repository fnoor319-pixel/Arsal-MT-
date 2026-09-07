from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

import settings


def _f(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_tables(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS v8_alpha_score_samples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            symbol TEXT NOT NULL,
            bar_time TEXT NOT NULL,
            playbook TEXT NOT NULL,
            side INTEGER NOT NULL,
            score REAL NOT NULL,
            UNIQUE(symbol,bar_time,playbook,side)
        );
        CREATE INDEX IF NOT EXISTS idx_v8_alpha_scores
            ON v8_alpha_score_samples(symbol,timestamp,score);

        CREATE TABLE IF NOT EXISTS v8_alpha_gate_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            symbol TEXT NOT NULL,
            strategy_id INTEGER,
            side INTEGER NOT NULL,
            playbook TEXT,
            local_score REAL NOT NULL,
            threshold REAL NOT NULL,
            allowed INTEGER NOT NULL,
            expected_r REAL,
            probability REAL,
            probability_threshold REAL,
            reason TEXT NOT NULL,
            context_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_v8_alpha_gate_recent
            ON v8_alpha_gate_events(timestamp,symbol,allowed);

        CREATE TABLE IF NOT EXISTS v8_luna_disagreement_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            symbol TEXT NOT NULL,
            strategy_id INTEGER,
            side INTEGER NOT NULL,
            playbook TEXT,
            allowed INTEGER NOT NULL,
            risk_multiplier REAL NOT NULL,
            local_score REAL,
            expected_r REAL,
            probability REAL,
            luna_confidence REAL,
            luna_reason TEXT,
            reason TEXT NOT NULL,
            context_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_v8_luna_disagreement_recent
            ON v8_luna_disagreement_events(timestamp,symbol,allowed);

        CREATE TABLE IF NOT EXISTS v8_budget_probe_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            symbol TEXT NOT NULL,
            strategy_id INTEGER,
            side INTEGER NOT NULL,
            playbook TEXT,
            allowed INTEGER NOT NULL,
            risk_multiplier REAL NOT NULL,
            local_score REAL,
            micro_score REAL,
            expected_r REAL,
            probability REAL,
            break_even_probability REAL,
            source TEXT,
            reason TEXT NOT NULL,
            context_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_v8_budget_probe_recent
            ON v8_budget_probe_events(timestamp,symbol,allowed);
        """
    )


def record_score_samples(
    connection: sqlite3.Connection,
    symbol: str,
    bar_time: str,
    items: list[tuple[str, int, float]],
) -> None:
    ensure_tables(connection)
    now = _now()
    for playbook, side, score in items:
        connection.execute(
            """
            INSERT INTO v8_alpha_score_samples(timestamp,symbol,bar_time,playbook,side,score)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(symbol,bar_time,playbook,side) DO UPDATE SET
                timestamp=excluded.timestamp,score=excluded.score
            """,
            (now, str(symbol), str(bar_time), str(playbook), int(side), _f(score)),
        )


def _quantile(values: list[float], q: float) -> float:
    values = sorted(_f(x) for x in values if math.isfinite(_f(x)))
    if not values:
        return 0.0
    q = _clamp(q, 0.0, 1.0)
    pos = (len(values) - 1) * q
    lo = int(math.floor(pos)); hi = int(math.ceil(pos))
    if lo == hi:
        return values[lo]
    weight = pos - lo
    return values[lo] * (1.0 - weight) + values[hi] * weight


def dynamic_thresholds(connection: sqlite3.Connection, symbol: str) -> dict[str, float]:
    """Adaptive opportunity thresholds from the symbol's own recent score distribution.

    This prevents one fixed score from permanently starving a quiet-but-clean market,
    while hard floors prevent the machine from manufacturing trades in noise.
    """
    ensure_tables(connection)
    lookback = max(60, int(getattr(settings, "V8_ALPHA_SCORE_LOOKBACK", 360)))
    rows = connection.execute(
        """
        SELECT score FROM v8_alpha_score_samples
        WHERE symbol=? ORDER BY id DESC LIMIT ?
        """,
        (str(symbol), lookback),
    ).fetchall()
    values = [_f(r["score"] if isinstance(r, sqlite3.Row) else r[0]) for r in rows]

    kind = "XAU" if "XAU" in str(symbol).upper() else "OIL" if "OIL" in str(symbol).upper() else "BTC" if "BTC" in str(symbol).upper() else "GEN"
    floors = {
        "XAU": (58.0, 66.0, 74.0),
        "OIL": (62.0, 70.0, 78.0),
        "BTC": (58.0, 66.0, 74.0),
        "GEN": (60.0, 68.0, 76.0),
    }[kind]
    caps = {
        "XAU": (82.0, 88.0, 93.0),
        "OIL": (84.0, 90.0, 95.0),
        "BTC": (82.0, 88.0, 93.0),
        "GEN": (82.0, 88.0, 93.0),
    }[kind]

    min_samples = max(20, int(getattr(settings, "V8_ALPHA_MIN_SCORE_SAMPLES", 45)))
    if len(values) < min_samples:
        return {"watch": floors[0], "arm": floors[1], "trigger": floors[2], "samples": float(len(values))}

    watch = _clamp(_quantile(values, _f(getattr(settings, "V8_ALPHA_WATCH_QUANTILE", 0.70), 0.70)), floors[0], caps[0])
    arm = _clamp(_quantile(values, _f(getattr(settings, "V8_ALPHA_ARM_QUANTILE", 0.84), 0.84)), max(floors[1], watch + 4.0), caps[1])
    trigger = _clamp(_quantile(values, _f(getattr(settings, "V8_ALPHA_TRIGGER_QUANTILE", 0.94), 0.94)), max(floors[2], arm + 4.0), caps[2])
    return {"watch": round(watch,2), "arm": round(arm,2), "trigger": round(trigger,2), "samples": float(len(values))}


def _recent_allowed_count(connection: sqlite3.Connection) -> int:
    since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    row = connection.execute(
        """
        SELECT COUNT(*) AS n
        FROM v8_alpha_gate_events
        WHERE timestamp>=? AND allowed=1
          AND reason='local alpha shortlist passed'
        """,
        (since,),
    ).fetchone()
    return int((row["n"] if isinstance(row, sqlite3.Row) else row[0]) or 0) if row else 0




def _norm_direction(value: Any) -> int:
    text = str(value or "").strip().lower()
    if text in {"buy", "bullish", "up", "uptrend", "trending_up", "long"}:
        return 1
    if text in {"sell", "bearish", "down", "downtrend", "trending_down", "short"}:
        return -1
    return 0


def directional_coherence(
    *, side: int, playbook: str, micro: dict[str, Any], spartan_snapshot: dict[str, Any] | None, expected_r: float
) -> tuple[bool, float, dict[str, Any]]:
    """Cheap deterministic context check before Luna.

    Continuation playbooks should broadly agree with structure/HTF/flow. Reversal
    playbooks are allowed to be counter-trend only when the micro reversal evidence
    is genuinely strong. This saves API calls on contradictions Luna would almost
    certainly veto while preserving deliberate high-quality reversal scalps.
    """
    snap = spartan_snapshot or {}
    derived = snap.get("derived_context") if isinstance(snap.get("derived_context"), dict) else {}
    smc = snap.get("smc") if isinstance(snap.get("smc"), dict) else {}
    agents = snap.get("agents_vote") if isinstance(snap.get("agents_vote"), dict) else {}
    htf = snap.get("higher_timeframes") if isinstance(snap.get("higher_timeframes"), dict) else {}
    evidence = (micro.get("context") or {}).get("evidence") if isinstance(micro.get("context"), dict) else {}
    evidence = evidence if isinstance(evidence, dict) else {}

    votes = []
    for key in ("technical", "smc", "flow", "htf", "volume_profile", "regime"):
        if key in agents:
            votes.append(_norm_direction(agents.get(key)))
    regime_vote = _norm_direction(derived.get("regime"))
    smc_vote = _norm_direction(smc.get("choch") or smc.get("structure"))
    htf_votes = [
        _norm_direction(item.get("trend"))
        for item in htf.values() if isinstance(item, dict) and item.get("available")
    ]
    htf_balance = sum(htf_votes)
    htf_vote = 1 if htf_balance >= 2 else -1 if htf_balance <= -2 else 0

    aligned = sum(1 for v in votes if v == int(side))
    opposed = sum(1 for v in votes if v == -int(side))
    if regime_vote == int(side): aligned += 1
    elif regime_vote == -int(side): opposed += 1
    if smc_vote == int(side): aligned += 1
    elif smc_vote == -int(side): opposed += 1
    if htf_vote == int(side): aligned += 1
    elif htf_vote == -int(side): opposed += 1

    continuation = str(playbook) in {"momentum_burst", "impulse_pullback_resume", "squeeze_breakout"}
    reversal = str(playbook) in {"wick_rejection", "exhaustion_snapback"}
    reversal_strength = max(
        _f(evidence.get("wick"), 0.0),
        _f(evidence.get("rsi"), 0.0),
        _f(evidence.get("extension"), 0.0),
        _f(evidence.get("reversal"), 0.0),
    )
    micro_score = _f(micro.get("score"), 0.0)
    strong_reversal = bool(
        reversal and micro_score >= 84.0 and reversal_strength >= 0.68 and expected_r >= 0.15
    )

    contradiction = False
    if continuation:
        contradiction = bool(opposed >= 3 and opposed >= aligned + 1)
    elif reversal:
        contradiction = bool(opposed >= 4 and not strong_reversal)
    else:
        contradiction = bool(opposed >= 4 and opposed >= aligned + 2)

    # 0..100 local direction quality. Neutral/unknown evidence is not punished.
    known = aligned + opposed
    direction_score = 55.0 if known == 0 else 100.0 * aligned / max(1, known)
    if strong_reversal and reversal:
        direction_score = max(direction_score, 62.0)

    meta = {
        "aligned_votes": aligned, "opposed_votes": opposed, "htf_vote": htf_vote,
        "regime_vote": regime_vote, "smc_vote": smc_vote,
        "continuation": continuation, "reversal": reversal,
        "strong_reversal": strong_reversal, "reversal_strength": round(reversal_strength, 3),
        "direction_score": round(direction_score, 2),
    }
    return (not contradiction), round(direction_score, 2), meta


def local_pre_luna_gate(
    connection: sqlite3.Connection,
    *,
    symbol: str,
    strategy_id: int,
    side: int,
    details: dict[str, Any],
    super_decision: Any,
    spartan_snapshot: dict[str, Any] | None,
) -> tuple[bool, float, dict[str, Any]]:
    """Cheap local quality/ranking gate before any paid Luna call.

    The score combines live alpha quality, expectancy, probability edge and
    deterministic confluence. It does not replace any hard gate. Its job is to
    spend Luna tokens only on serious candidates and to prefer the best local
    opportunities rather than calling the API on every marginal pass.
    """
    ensure_tables(connection)
    micro = details.get("micro_hunter") if isinstance(details.get("micro_hunter"), dict) else {}
    micro_score = _f(micro.get("score"), 0.0)
    expected_r = _f((getattr(super_decision, "market", {}) or {}).get("expected_r"), -1.0)
    probability = _f(getattr(super_decision, "probability", 0.0))
    probability_threshold = _f(getattr(super_decision, "threshold", 0.5), 0.5)
    prob_edge = probability - probability_threshold
    playbook = str(micro.get("playbook") or "")
    direction_ok, direction_score, direction_meta = directional_coherence(
        side=int(side), playbook=playbook, micro=micro, spartan_snapshot=spartan_snapshot, expected_r=expected_r
    )

    candidate = (spartan_snapshot or {}).get("candidate") if isinstance((spartan_snapshot or {}).get("candidate"), dict) else {}
    condition_scores = (spartan_snapshot or {}).get("condition_scores") if isinstance((spartan_snapshot or {}).get("condition_scores"), dict) else {}
    conf = _f(candidate.get("confidence"), 0.5)
    ratio = _f(condition_scores.get("available_ratio"), 0.5)

    er_score = _clamp((expected_r + 0.10) / 0.80, 0.0, 1.0) * 100.0
    p_score = _clamp((prob_edge + 0.08) / 0.20, 0.0, 1.0) * 100.0
    det_score = _clamp(0.55 * conf + 0.45 * ratio, 0.0, 1.0) * 100.0
    if micro:
        local_score = 0.34 * micro_score + 0.28 * er_score + 0.12 * p_score + 0.14 * det_score + 0.12 * direction_score
    else:
        local_score = 0.44 * er_score + 0.20 * p_score + 0.24 * det_score + 0.12 * direction_score

    # High-R:R positive expectancy borderline cases are intentionally allowed
    # to reach Luna at a slightly lower local score than ordinary candidates.
    borderline = not bool(getattr(super_decision, "approved", False))
    threshold = _f(getattr(settings, "V8_PRE_LUNA_LOCAL_SCORE", 61.0), 61.0)
    if borderline and expected_r >= _f(getattr(settings, "SPARTAN_GPT_BORDERLINE_MIN_EXPECTED_R", 0.15), 0.15):
        threshold -= 4.0

    # V8.6: local alpha qualification and paid-Luna quota are deliberately
    # decoupled. The old design made the daily API shortlist cap an execution
    # veto, so strong Quant candidates could never reach the zero-token evidence
    # lane after the paid-review quota was exhausted.
    daily_soft_cap = max(1, int(getattr(settings, "V8_PRE_LUNA_LOCAL_DAILY_PASS_CAP", 18)))
    used = _recent_allowed_count(connection)
    api_quota_available = bool(used < daily_soft_cap)
    allowed = bool(direction_ok and local_score >= threshold and expected_r >= 0.0)
    if not direction_ok:
        reason = "direction/structure contradiction held locally"
    elif expected_r < 0.0:
        reason = "negative expected-R held locally"
    elif local_score < threshold:
        reason = f"local alpha score {local_score:.1f} below {threshold:.1f}"
    elif not api_quota_available:
        reason = "local alpha qualified; paid Luna shortlist quota exhausted"
    else:
        reason = "local alpha shortlist passed"

    context = {
        "micro_score": round(micro_score,2),
        "expected_r": round(expected_r,4),
        "probability": round(probability,4),
        "probability_threshold": round(probability_threshold,4),
        "prob_edge": round(prob_edge,4),
        "deterministic_score": round(det_score,2),
        "daily_shortlist_used": used,
        "daily_shortlist_cap": daily_soft_cap,
        "api_quota_available": bool(api_quota_available),
        "direction_ok": bool(direction_ok),
        "direction_score": round(direction_score, 2),
        "direction": direction_meta,
    }
    connection.execute(
        """
        INSERT INTO v8_alpha_gate_events(timestamp,symbol,strategy_id,side,playbook,local_score,threshold,allowed,
                                         expected_r,probability,probability_threshold,reason,context_json)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (_now(), str(symbol), int(strategy_id), int(side), str(micro.get("playbook") or ""),
         float(local_score), float(threshold), 1 if allowed else 0, float(expected_r), float(probability),
         float(probability_threshold), reason, json.dumps(context,separators=(",",":"))),
    )
    return allowed, round(local_score,2), {"threshold": threshold, "reason": reason, **context}




def _recent_disagreement_probe_counts(connection: sqlite3.Connection, symbol: str) -> tuple[int, int]:
    since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    total = connection.execute(
        "SELECT COUNT(*) AS n FROM v8_luna_disagreement_events WHERE timestamp>=? AND allowed=1",
        (since,),
    ).fetchone()
    per_symbol = connection.execute(
        "SELECT COUNT(*) AS n FROM v8_luna_disagreement_events WHERE timestamp>=? AND symbol=? AND allowed=1",
        (since, str(symbol)),
    ).fetchone()
    def _n(row: Any) -> int:
        if row is None:
            return 0
        try:
            return int(row["n"] or 0)
        except Exception:
            return int(row[0] or 0)
    return _n(total), _n(per_symbol)


def luna_disagreement_probe_policy(
    connection: sqlite3.Connection,
    *,
    symbol: str,
    strategy_id: int,
    side: int,
    details: dict[str, Any],
    super_decision: Any,
    local_alpha_meta: dict[str, Any],
    llm_review: dict[str, Any],
    spartan_snapshot: dict[str, Any] | None,
) -> tuple[bool, float, dict[str, Any]]:
    """DEMO-only quant-vs-Luna disagreement lane.

    Institutional-style design should not make a language model a single point of
    failure. If the deterministic stack + SuperLearner strongly approve a setup,
    but Luna says HOLD without a locally verified hard contradiction, V8.2 may
    collect a *tiny-risk DEMO probe*. This is evidence collection, not a safety
    bypass: all broker/risk/session/Edge gates still apply after this function.
    """
    ensure_tables(connection)
    enabled = bool(getattr(settings, "V8_LUNA_DISAGREEMENT_PROBE_ENABLED", True))
    micro = details.get("micro_hunter") if isinstance(details.get("micro_hunter"), dict) else {}
    playbook = str(micro.get("playbook") or "")
    micro_score = _f(micro.get("score"), 0.0)
    local_score = _f(local_alpha_meta.get("score"), _f(details.get("v8_pre_luna", {}).get("score"), 0.0))
    if local_score <= 0:
        local_score = _f(details.get("v8_pre_luna", {}).get("score"), 0.0)
    market = (getattr(super_decision, "market", {}) or {})
    expected_r = _f(market.get("expected_r"), -1.0)
    probability = _f(getattr(super_decision, "probability", 0.0))
    break_even = _f(market.get("break_even_probability"), 0.50)
    approved_super = bool(getattr(super_decision, "approved", False))
    direction_ok = bool(local_alpha_meta.get("direction_ok", True))
    direction = local_alpha_meta.get("direction") if isinstance(local_alpha_meta.get("direction"), dict) else {}
    opposed = int(direction.get("opposed_votes") or 0)
    aligned = int(direction.get("aligned_votes") or 0)
    luna_conf = _f(llm_review.get("confidence"), 0.0)
    luna_reason = str(llm_review.get("reasoning") or llm_review.get("context_summary") or "hold")[:160]
    luna_source = str(llm_review.get("decision_source") or "")

    min_local = _f(getattr(settings, "V8_LUNA_DISAGREEMENT_MIN_LOCAL_SCORE", 82.0), 82.0)
    min_micro = _f(getattr(settings, "V8_LUNA_DISAGREEMENT_MIN_MICRO_SCORE", 80.0), 80.0)
    min_er = _f(getattr(settings, "V8_LUNA_DISAGREEMENT_MIN_EXPECTED_R", 0.10), 0.10)
    risk_mult = _clamp(_f(getattr(settings, "V8_LUNA_DISAGREEMENT_RISK_MULTIPLIER", 0.10), 0.10), 0.05, 0.20)
    total_cap = max(0, int(getattr(settings, "V8_LUNA_DISAGREEMENT_MAX_PER_DAY", 2)))
    symbol_cap = max(0, int(getattr(settings, "V8_LUNA_DISAGREEMENT_MAX_PER_SYMBOL_PER_DAY", 1)))
    used_total, used_symbol = _recent_disagreement_probe_counts(connection, symbol)

    # A locally verified contradiction remains authoritative. The probe exists
    # only for *model disagreement*, not to bypass deterministic safety.
    hard_context = bool((not direction_ok) or opposed >= max(3, aligned + 2))
    source_ok = luna_source in {"openai", "decision_fingerprint_cache"}
    luna_hold = str(llm_review.get("decision") or "hold").lower() == "hold"

    # V8.4 evidence-first DEMO lane: a high-quality scalp with positive expectancy
    # may still be worth a tiny broker probe even when SuperLearner is only
    # borderline rather than fully approved. This is mathematically anchored to
    # break-even probability and remains behind every deterministic hard gate.
    min_prob_edge = _f(getattr(settings, "V8_EVIDENCE_PROBE_MIN_PROB_EDGE", 0.03), 0.03)
    quant_positive = bool(
        approved_super or (
            expected_r >= _f(getattr(settings, "V8_EVIDENCE_PROBE_MIN_EXPECTED_R", 0.20), 0.20)
            and probability >= break_even + min_prob_edge
        )
    )

    allowed = bool(
        enabled
        and luna_hold
        and source_ok
        and quant_positive
        and not hard_context
        and local_score >= min_local
        and micro_score >= min_micro
        and expected_r >= min_er
        and used_total < total_cap
        and used_symbol < symbol_cap
    )
    if not enabled:
        reason = "disagreement probe disabled"
    elif not luna_hold or not source_ok:
        reason = "not a real/cached Luna HOLD"
    elif not quant_positive:
        reason = (
            "quant evidence below probe floor "
            f"(P={probability:.2f}, BE={break_even:.2f}, ER={expected_r:+.2f}R)"
        )
    elif hard_context:
        reason = "deterministic direction contradiction remains authoritative"
    elif local_score < min_local:
        reason = f"local score {local_score:.1f} below {min_local:.1f}"
    elif micro_score < min_micro:
        reason = f"micro score {micro_score:.1f} below {min_micro:.1f}"
    elif expected_r < min_er:
        reason = f"expected-R {expected_r:+.2f} below +{min_er:.2f}R"
    elif used_total >= total_cap:
        reason = "daily disagreement-probe cap reached"
    elif used_symbol >= symbol_cap:
        reason = "symbol disagreement-probe cap reached"
    else:
        reason = "high-quality quant/Luna disagreement -> tiny-risk DEMO evidence probe"

    context = {
        "micro_score": round(micro_score, 2),
        "local_score": round(local_score, 2),
        "expected_r": round(expected_r, 4),
        "probability": round(probability, 4),
        "approved_super": approved_super,
        "quant_positive": quant_positive,
        "break_even_probability": round(break_even, 4),
        "probability_edge": round(probability - break_even, 4),
        "direction_ok": direction_ok,
        "aligned_votes": aligned,
        "opposed_votes": opposed,
        "luna_source": luna_source,
        "luna_confidence": round(luna_conf, 4),
        "used_total": used_total,
        "used_symbol": used_symbol,
        "daily_cap": total_cap,
        "symbol_cap": symbol_cap,
    }
    connection.execute(
        """
        INSERT INTO v8_luna_disagreement_events(
            timestamp,symbol,strategy_id,side,playbook,allowed,risk_multiplier,
            local_score,expected_r,probability,luna_confidence,luna_reason,reason,context_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (_now(), str(symbol), int(strategy_id), int(side), playbook, 1 if allowed else 0,
         float(risk_mult), float(local_score), float(expected_r), float(probability), float(luna_conf),
         luna_reason, reason, json.dumps(context,separators=(",",":"))),
    )
    return allowed, risk_mult, {"reason": reason, **context}



def _recent_budget_probe_counts(connection: sqlite3.Connection, symbol: str) -> tuple[int, int]:
    since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    total = connection.execute(
        "SELECT COUNT(*) AS n FROM v8_budget_probe_events WHERE timestamp>=? AND allowed=1",
        (since,),
    ).fetchone()
    per_symbol = connection.execute(
        "SELECT COUNT(*) AS n FROM v8_budget_probe_events WHERE timestamp>=? AND symbol=? AND allowed=1",
        (since, str(symbol)),
    ).fetchone()
    def _n(row: Any) -> int:
        if row is None:
            return 0
        try:
            return int(row["n"] or 0)
        except Exception:
            return int(row[0] or 0)
    return _n(total), _n(per_symbol)


def budget_exhausted_quant_probe_policy(
    connection: sqlite3.Connection,
    *,
    symbol: str,
    strategy_id: int,
    side: int,
    details: dict[str, Any],
    super_decision: Any,
    local_alpha_meta: dict[str, Any],
    llm_review: dict[str, Any],
    spartan_snapshot: dict[str, Any] | None,
) -> tuple[bool, float, dict[str, Any]]:
    """Zero-token DEMO evidence lane when Luna cannot be called.

    A daily API-call cap or local dollar guard should stop API spending, not turn
    the whole execution engine into a permanent HOLD. Only a tiny broker-DEMO
    evidence probe can pass, and only when Quant remains convincingly above
    break-even after every deterministic hard gate.
    """
    ensure_tables(connection)
    enabled = bool(getattr(settings, "V8_BUDGET_EXHAUSTED_QUANT_PROBE_ENABLED", True))
    source = str(llm_review.get("decision_source") or "")
    unavailable = source in {"daily_call_cap", "budget_guard", "local_shortlist_cap"}

    micro = details.get("micro_hunter") if isinstance(details.get("micro_hunter"), dict) else {}
    playbook = str(micro.get("playbook") or "")
    micro_score = _f(micro.get("score"), 0.0)
    local_score = _f(local_alpha_meta.get("score"), _f(details.get("v8_pre_luna", {}).get("score"), 0.0))
    market = (getattr(super_decision, "market", {}) or {})
    expected_r = _f(market.get("expected_r"), -1.0)
    probability = _f(getattr(super_decision, "probability", 0.0))
    reward_to_risk = _f(market.get("reward_to_risk"), 0.0)
    be_raw = market.get("break_even_probability")
    if be_raw is None and reward_to_risk > 0:
        break_even = 1.0 / (1.0 + reward_to_risk)
    else:
        break_even = _f(be_raw, 0.50)

    direction_ok = bool(local_alpha_meta.get("direction_ok", True))
    direction = local_alpha_meta.get("direction") if isinstance(local_alpha_meta.get("direction"), dict) else {}
    opposed = int(direction.get("opposed_votes") or 0)
    aligned = int(direction.get("aligned_votes") or 0)
    hard_context = bool((not direction_ok) or opposed >= max(3, aligned + 2))

    min_local = _f(getattr(settings, "V8_BUDGET_PROBE_MIN_LOCAL_SCORE", 82.0), 82.0)
    min_micro = _f(getattr(settings, "V8_BUDGET_PROBE_MIN_MICRO_SCORE", 82.0), 82.0)
    min_er = _f(getattr(settings, "V8_BUDGET_PROBE_MIN_EXPECTED_R", 0.30), 0.30)
    min_edge = _f(getattr(settings, "V8_BUDGET_PROBE_MIN_PROB_EDGE", 0.04), 0.04)
    risk_mult = _clamp(_f(getattr(settings, "V8_BUDGET_PROBE_RISK_MULTIPLIER", 0.07), 0.07), 0.03, 0.10)
    total_cap = max(0, int(getattr(settings, "V8_BUDGET_PROBE_MAX_PER_DAY", 3)))
    symbol_cap = max(0, int(getattr(settings, "V8_BUDGET_PROBE_MAX_PER_SYMBOL_PER_DAY", 1)))
    used_total, used_symbol = _recent_budget_probe_counts(connection, symbol)

    probability_edge = probability - break_even
    quant_positive = bool(
        expected_r >= min_er
        and probability_edge >= min_edge
        and local_score >= min_local
        and micro_score >= min_micro
    )
    allowed = bool(
        enabled and unavailable and quant_positive and not hard_context
        and used_total < total_cap and used_symbol < symbol_cap
    )

    if not enabled:
        reason = "budget-exhausted quant probe disabled"
    elif not unavailable:
        reason = "Luna is available; budget fallback not applicable"
    elif hard_context:
        reason = "deterministic direction contradiction remains authoritative"
    elif expected_r < min_er:
        reason = f"expected-R {expected_r:+.2f} below +{min_er:.2f}R"
    elif probability_edge < min_edge:
        reason = f"probability edge {probability_edge:+.2f} below +{min_edge:.2f}"
    elif local_score < min_local:
        reason = f"local score {local_score:.1f} below {min_local:.1f}"
    elif micro_score < min_micro:
        reason = f"micro score {micro_score:.1f} below {min_micro:.1f}"
    elif used_total >= total_cap:
        reason = "daily zero-token evidence-probe cap reached"
    elif used_symbol >= symbol_cap:
        reason = "symbol zero-token evidence-probe cap reached"
    else:
        reason = "Luna budget/call cap -> tiny zero-token Quant DEMO evidence probe"

    context = {
        "source": source,
        "micro_score": round(micro_score, 2),
        "local_score": round(local_score, 2),
        "expected_r": round(expected_r, 4),
        "probability": round(probability, 4),
        "break_even_probability": round(break_even, 4),
        "probability_edge": round(probability_edge, 4),
        "direction_ok": direction_ok,
        "aligned_votes": aligned,
        "opposed_votes": opposed,
        "used_total": used_total,
        "used_symbol": used_symbol,
        "daily_cap": total_cap,
        "symbol_cap": symbol_cap,
    }
    connection.execute(
        """
        INSERT INTO v8_budget_probe_events(
            timestamp,symbol,strategy_id,side,playbook,allowed,risk_multiplier,
            local_score,micro_score,expected_r,probability,break_even_probability,
            source,reason,context_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (_now(), str(symbol), int(strategy_id), int(side), playbook, 1 if allowed else 0,
         float(risk_mult), float(local_score), float(micro_score), float(expected_r),
         float(probability), float(break_even), source, reason,
         json.dumps(context,separators=(",",":"))),
    )
    return allowed, risk_mult, {"reason": reason, **context}



def status(connection: sqlite3.Connection) -> dict[str, Any]:
    ensure_tables(connection)
    out: dict[str, Any] = {"thresholds": {}, "last60": [], "gate60": []}
    for symbol in getattr(settings, "SYMBOLS", []):
        out["thresholds"][str(symbol)] = dynamic_thresholds(connection, str(symbol))
    since = (datetime.now(timezone.utc) - timedelta(minutes=60)).isoformat()
    rows = connection.execute(
        """SELECT symbol,playbook,side,score,bar_time FROM v8_alpha_score_samples
           WHERE timestamp>=? ORDER BY id DESC LIMIT 30""", (since,)
    ).fetchall()
    out["last60"] = [dict(r) for r in rows]
    gates = connection.execute(
        """SELECT symbol,playbook,local_score,threshold,allowed,expected_r,probability,reason,timestamp
           FROM v8_alpha_gate_events WHERE timestamp>=? ORDER BY id DESC LIMIT 30""", (since,)
    ).fetchall()
    out["gate60"] = [dict(r) for r in gates]
    return out
