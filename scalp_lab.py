from __future__ import annotations

import json
import math
import random
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

import settings


def _f(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_tables(connection: sqlite3.Connection) -> None:
    """Add V6.7 laboratory tables without changing legacy data."""
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS execution_quality (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            symbol TEXT NOT NULL,
            strategy_id INTEGER,
            side INTEGER NOT NULL,
            requested_entry REAL,
            filled_entry REAL,
            slippage_price REAL,
            slippage_atr REAL,
            spread_price REAL,
            spread_atr REAL,
            latency_ms REAL,
            retcode INTEGER,
            execution_tier TEXT,
            details_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_execution_quality_symbol_time
            ON execution_quality(symbol, timestamp);

        CREATE TABLE IF NOT EXISTS scalp_lab_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            snapshot_type TEXT NOT NULL,
            symbol TEXT,
            strategy_id INTEGER,
            score REAL,
            details_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_scalp_lab_snapshots_type_time
            ON scalp_lab_snapshots(snapshot_type, timestamp);
        """
    )


def conservative_cost_stress_r(
    spread_price: float,
    stop_distance: float,
    *,
    commission_r: float = 0.0,
) -> float:
    """Return a *stress-test* friction estimate in R, not a broker fact.

    The existing backtester already includes its own spread convention.  This
    function therefore adds only an extra configurable friction reserve used for
    robustness reporting.  It is not a live-order surcharge and is not enabled
    as a hard validation gate by default.
    """
    stop = abs(_f(stop_distance))
    if stop <= 1e-12:
        return 0.0
    spread = max(0.0, _f(spread_price))
    extra_spread = max(0.0, _f(getattr(settings, "LAB_COST_STRESS_EXTRA_SPREAD_MULTIPLIER", 0.35), 0.35))
    slippage_mult = max(0.0, _f(getattr(settings, "LAB_COST_STRESS_SLIPPAGE_SPREAD_MULTIPLIER", 0.20), 0.20))
    return max(0.0, spread * (extra_spread + slippage_mult) / stop + max(0.0, _f(commission_r)))


def stress_adjusted_metrics(metrics: dict[str, Any], spread_atr: float, stop_atr: float) -> dict[str, Any]:
    stop_atr = max(1e-9, _f(stop_atr, 1.0))
    spread_atr = max(0.0, _f(spread_atr))
    friction_r = conservative_cost_stress_r(spread_atr, stop_atr)
    raw_exp = _f(metrics.get("expectancy_r"))
    adjusted = raw_exp - friction_r
    return {
        "stress_friction_r_per_trade": friction_r,
        "raw_expectancy_r": raw_exp,
        "stress_expectancy_r": adjusted,
        "stress_positive": adjusted > 0,
    }


def opportunity_score(
    super_decision: Any,
    *,
    confluence_ratio: float = 0.0,
    spread_atr: float = 0.0,
    session_quality: float = 0.5,
) -> float:
    """0-100 ranking score for diagnostics/capital allocation research.

    This is advisory in V6.7; hard risk controls and existing approval gates stay
    authoritative.  It combines expected-R, confidence, confluence, cost and
    Bayesian loss evidence into one comparable number across symbols.
    """
    probability = _f(getattr(super_decision, "probability", 0.5), 0.5)
    market = getattr(super_decision, "market", {}) or {}
    expected_r = _f(market.get("expected_r")) if isinstance(market, dict) else 0.0
    bayes_loss = _f(getattr(super_decision, "bayes_loss_probability", 0.5), 0.5)
    ratio = min(1.0, max(0.0, _f(confluence_ratio)))
    spread_penalty = min(1.0, max(0.0, _f(spread_atr)) / 0.20)
    session = min(1.0, max(0.0, _f(session_quality, 0.5)))
    edge_component = min(1.0, max(0.0, expected_r / 0.35))
    score = 100.0 * (
        0.30 * edge_component
        + 0.20 * probability
        + 0.20 * ratio
        + 0.15 * (1.0 - bayes_loss)
        + 0.10 * session
        + 0.05 * (1.0 - spread_penalty)
    )
    return round(min(100.0, max(0.0, score)), 2)


def record_execution_quality(
    connection: sqlite3.Connection,
    *,
    symbol: str,
    strategy_id: int | None,
    side: int,
    requested_entry: float,
    filled_entry: float | None,
    spread_price: float,
    atr_value: float,
    latency_ms: float,
    retcode: int | None,
    execution_tier: str,
    details: dict[str, Any] | None = None,
) -> None:
    ensure_tables(connection)
    requested = _f(requested_entry)
    filled = _f(filled_entry, requested)
    # Positive number means worse fill for the requested direction.
    slippage = (filled - requested) * (1 if int(side) == 1 else -1)
    atr = max(1e-12, abs(_f(atr_value)))
    spread = max(0.0, _f(spread_price))
    connection.execute(
        """
        INSERT INTO execution_quality(
            timestamp,symbol,strategy_id,side,requested_entry,filled_entry,
            slippage_price,slippage_atr,spread_price,spread_atr,latency_ms,
            retcode,execution_tier,details_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            _utc_now(), str(symbol), int(strategy_id or 0), int(side), requested,
            filled, slippage, slippage / atr, spread, spread / atr,
            max(0.0, _f(latency_ms)), int(retcode or 0), str(execution_tier),
            json.dumps(details or {}, separators=(",", ":"), default=str),
        ),
    )


def execution_quality_summary(connection: sqlite3.Connection, limit: int = 100) -> dict[str, Any]:
    ensure_tables(connection)
    rows = connection.execute(
        """
        SELECT symbol,slippage_atr,spread_atr,latency_ms,retcode
        FROM execution_quality ORDER BY id DESC LIMIT ?
        """, (max(1, int(limit)),)
    ).fetchall()
    if not rows:
        return {"samples": 0, "by_symbol": {}}
    grouped: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        grouped[str(row["symbol"])].append(row)
    result: dict[str, Any] = {}
    for symbol, items in grouped.items():
        n = len(items)
        result[symbol] = {
            "samples": n,
            "avg_slippage_atr": sum(_f(x["slippage_atr"]) for x in items) / n,
            "avg_spread_atr": sum(_f(x["spread_atr"]) for x in items) / n,
            "avg_latency_ms": sum(_f(x["latency_ms"]) for x in items) / n,
            "nonzero_retcode_rate": sum(1 for x in items if int(x["retcode"] or 0) != 0) / n,
        }
    return {"samples": len(rows), "by_symbol": result}


def _load_params(text: Any) -> dict[str, float]:
    try:
        raw = json.loads(str(text or "{}"))
    except Exception:
        return {}
    result: dict[str, float] = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(number):
                result[str(key)] = number
    return result


def parameter_stability_neighbors(connection: sqlite3.Connection, strategy_id: int, max_neighbors: int = 12) -> dict[str, Any]:
    """Estimate whether nearby parameter sets also work (anti-overfit evidence)."""
    row = connection.execute(
        "SELECT id,symbol,family,params_json FROM strategies WHERE id=?", (int(strategy_id),)
    ).fetchone()
    if not row:
        return {"strategy_id": strategy_id, "neighbors": 0, "stability": None}
    target = _load_params(row["params_json"])
    peers = connection.execute(
        """
        SELECT s.id,s.params_json,MAX(b.score) score,MAX(b.profit_factor) pf,
               COALESCE(d.oos_profit_factor,0) oos_pf
        FROM strategies s
        LEFT JOIN backtests b ON b.strategy_id=s.id
        LEFT JOIN strategy_diagnostics d ON d.strategy_id=s.id
        WHERE s.symbol=? AND s.family=? AND s.id<>?
        GROUP BY s.id
        ORDER BY MAX(b.tested_at) DESC LIMIT 400
        """, (str(row["symbol"]), str(row["family"]), int(strategy_id))
    ).fetchall()
    distances: list[tuple[float, sqlite3.Row]] = []
    for peer in peers:
        params = _load_params(peer["params_json"])
        common = sorted(set(target) & set(params))
        if not common:
            continue
        diffs = []
        for key in common:
            scale = max(1.0, abs(target[key]))
            diffs.append(abs(params[key] - target[key]) / scale)
        distances.append((sum(diffs) / len(diffs), peer))
    distances.sort(key=lambda x: x[0])
    chosen = distances[: max(1, int(max_neighbors))]
    if not chosen:
        return {"strategy_id": strategy_id, "neighbors": 0, "stability": None}
    good = 0.0
    weight_sum = 0.0
    details = []
    for distance, peer in chosen:
        weight = 1.0 / (0.05 + distance)
        quality = 1.0 if _f(peer["pf"]) >= 1.05 and _f(peer["oos_pf"]) >= 1.0 else 0.0
        good += quality * weight
        weight_sum += weight
        details.append({"id": int(peer["id"]), "distance": round(distance,4), "pf": _f(peer["pf"]), "oos_pf": _f(peer["oos_pf"])})
    stability = good / weight_sum if weight_sum > 0 else 0.0
    return {
        "strategy_id": int(strategy_id), "neighbors": len(chosen),
        "stability": round(stability, 4), "nearest": details[:6],
    }


def champion_challenger(connection: sqlite3.Connection, symbol: str, limit: int = 8) -> dict[str, Any]:
    """Rank proven and emerging strategies without mutating their statuses."""
    rows = connection.execute(
        """
        SELECT s.id,s.family,s.status,MAX(b.score) backtest_score,
               MAX(b.profit_factor) pf,COALESCE(d.oos_profit_factor,0) oos_pf,
               COALESCE(d.stability_score,0) stability,
               COALESCE(c.observations,0) live_n,COALESCE(c.reward_mean,0) live_mean,
               COALESCE(c.max_drawdown_r,0) live_dd
        FROM strategies s
        LEFT JOIN backtests b ON b.strategy_id=s.id
        LEFT JOIN strategy_diagnostics d ON d.strategy_id=s.id
        LEFT JOIN candidate_live_scores c ON c.strategy_id=s.id
        WHERE s.symbol=? AND s.status IN ('shadow_approved','historical_validated')
        GROUP BY s.id
        """, (str(symbol),)
    ).fetchall()
    ranked = []
    for row in rows:
        status = str(row["status"])
        live_n = int(row["live_n"] or 0)
        live_mean = _f(row["live_mean"])
        evidence_scale = min(1.0, live_n / 15.0)
        quarantine = bool(
            live_n >= int(getattr(settings, "SCALP_SHADOW_EARLY_QUARANTINE_TRADES", 8))
            and live_mean <= _f(getattr(settings, "SCALP_SHADOW_EARLY_QUARANTINE_MEAN_R", -0.25), -0.25)
        )
        score = (
            0.20 * _f(row["backtest_score"]) +
            8.0 * min(2.0, _f(row["pf"])) +
            8.0 * min(2.0, _f(row["oos_pf"])) +
            0.35 * _f(row["stability"]) +
            _f(getattr(settings, "SCALP_SHADOW_SCORE_WEIGHT", 30.0), 30.0) * live_mean * evidence_scale +
            0.10 * min(40.0, float(live_n)) -
            1.5 * _f(row["live_dd"]) -
            (30.0 if quarantine else 0.0)
        )
        ranked.append({
            "id": int(row["id"]), "family": str(row["family"]), "status": status,
            "score": round(score, 3), "pf": _f(row["pf"]), "oos_pf": _f(row["oos_pf"]),
            "live_n": live_n, "live_mean_r": live_mean, "quarantined": quarantine,
            "role": "champion" if status == "shadow_approved" else "challenger",
        })
    ranked.sort(key=lambda x: x["score"], reverse=True)
    return {"symbol": str(symbol), "ranked": ranked[: max(1, int(limit))]}


def monte_carlo_rewards(rewards: list[float], samples: int | None = None, seed: int = 6701) -> dict[str, Any]:
    clean = [_f(x) for x in rewards if math.isfinite(_f(x))]
    if len(clean) < 5:
        return {"trades": len(clean), "samples": 0, "reason": "need_at_least_5_trades"}
    n_samples = max(200, int(samples or getattr(settings, "LAB_MONTE_CARLO_SAMPLES", 3000)))
    rng = random.Random(seed)
    final_r: list[float] = []
    max_dd: list[float] = []
    longest_loss: list[int] = []
    for _ in range(n_samples):
        path = [rng.choice(clean) for _ in range(len(clean))]
        cumulative = 0.0
        peak = 0.0
        dd = 0.0
        streak = 0
        max_streak = 0
        for reward in path:
            cumulative += reward
            peak = max(peak, cumulative)
            dd = max(dd, peak - cumulative)
            if reward <= 0:
                streak += 1
                max_streak = max(max_streak, streak)
            else:
                streak = 0
        final_r.append(cumulative)
        max_dd.append(dd)
        longest_loss.append(max_streak)
    def pct(values: list[float], p: float) -> float:
        ordered = sorted(values)
        idx = min(len(ordered)-1, max(0, int(round((len(ordered)-1)*p))))
        return ordered[idx]
    return {
        "trades": len(clean), "samples": n_samples,
        "mean_final_r": sum(final_r)/len(final_r),
        "p05_final_r": pct(final_r,0.05), "p50_final_r": pct(final_r,0.50), "p95_final_r": pct(final_r,0.95),
        "p50_max_drawdown_r": pct(max_dd,0.50), "p95_max_drawdown_r": pct(max_dd,0.95),
        "p95_longest_loss_streak": int(round(pct([float(x) for x in longest_loss],0.95))),
        "probability_negative_final_r": sum(1 for x in final_r if x < 0)/len(final_r),
    }


def demo_monte_carlo(connection: sqlite3.Connection, window: int = 120) -> dict[str, Any]:
    rows = connection.execute(
        "SELECT reward_r FROM demo_positions WHERE status='closed' AND reward_r IS NOT NULL ORDER BY id DESC LIMIT ?",
        (max(5, int(window)),)
    ).fetchall()
    return monte_carlo_rewards([_f(row["reward_r"]) for row in rows])


def _reward_summary(rewards: list[float]) -> dict[str, Any]:
    clean = [_f(x) for x in rewards if math.isfinite(_f(x))]
    if not clean:
        return {"n": 0, "mean_r": 0.0, "total_r": 0.0, "profit_factor": 0.0, "win_rate": 0.0}
    wins = [x for x in clean if x > 0]
    losses = [x for x in clean if x <= 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    pf = gross_win / gross_loss if gross_loss > 1e-12 else (5.0 if gross_win > 0 else 0.0)
    return {
        "n": len(clean),
        "mean_r": sum(clean) / len(clean),
        "total_r": sum(clean),
        "profit_factor": pf,
        "win_rate": len(wins) / len(clean),
    }


def demo_edge_profile(
    connection: sqlite3.Connection,
    *,
    symbol: str | None = None,
    family: str | None = None,
    strategy_id: int | None = None,
    window: int = 60,
    monte_carlo: bool = False,
) -> dict[str, Any]:
    """Recent *actual DEMO* edge for one strategy/family/symbol or the portfolio.

    This intentionally does not mix rejected research/shadow outcomes into broker
    execution evidence. The most recent rows are used so the guard can recover
    when market conditions improve rather than being permanently poisoned by old
    losses.
    """
    clauses = ["d.status='closed'", "d.reward_r IS NOT NULL"]
    args: list[Any] = []
    if symbol is not None:
        clauses.append("d.symbol=?")
        args.append(str(symbol))
    if strategy_id is not None:
        clauses.append("d.strategy_id=?")
        args.append(int(strategy_id))
    if family is not None:
        clauses.append("s.family=?")
        args.append(str(family))
    args.append(max(5, int(window)))
    rows = connection.execute(
        f"""
        SELECT d.reward_r
        FROM demo_positions d
        JOIN strategies s ON s.id=d.strategy_id
        WHERE {' AND '.join(clauses)}
        ORDER BY d.id DESC LIMIT ?
        """,
        tuple(args),
    ).fetchall()
    rewards = [_f(row["reward_r"]) for row in rows]
    result = _reward_summary(rewards)
    if monte_carlo and len(rewards) >= 5:
        result["monte_carlo"] = monte_carlo_rewards(rewards)
    return result


def demo_monte_carlo_by_symbol(connection: sqlite3.Connection, window: int = 60) -> dict[str, dict[str, Any]]:
    symbols = [str(row[0]) for row in connection.execute(
        "SELECT DISTINCT symbol FROM demo_positions WHERE status='closed' AND reward_r IS NOT NULL"
    ).fetchall()]
    return {
        symbol: demo_edge_profile(connection, symbol=symbol, window=window, monte_carlo=True)
        for symbol in symbols
    }


def edge_recovery_guard(
    connection: sqlite3.Connection,
    *,
    symbol: str,
    family: str,
    strategy_id: int,
    execution_tier: str,
) -> tuple[bool, float, dict[str, Any]]:
    """Self-correct recent weak edge before a paid Luna call or broker order.

    The hierarchy is deliberate: repeated losses from the *same strategy* can
    quarantine it, while weak family/symbol/portfolio evidence only de-risks.
    This prevents a bad aggregate Monte-Carlo number from freezing a genuinely
    improving symbol forever. Risk is never increased by this function.
    """
    if not bool(getattr(settings, "EDGE_RECOVERY_ENABLED", True)):
        return True, 1.0, {"enabled": False, "state": "disabled"}

    strategy = demo_edge_profile(
        connection, symbol=symbol, strategy_id=int(strategy_id),
        window=int(getattr(settings, "EDGE_STRATEGY_WINDOW", 30)),
    )
    family_edge = demo_edge_profile(
        connection, symbol=symbol, family=family,
        window=int(getattr(settings, "EDGE_FAMILY_WINDOW", 60)),
    )
    symbol_edge = demo_edge_profile(
        connection, symbol=symbol,
        window=int(getattr(settings, "EDGE_SYMBOL_WINDOW", 60)),
        monte_carlo=True,
    )
    overall = demo_edge_profile(
        connection, window=int(getattr(settings, "EDGE_OVERALL_WINDOW", 120)),
        monte_carlo=True,
    )

    multiplier = 1.0
    reasons: list[str] = []
    hard_veto = False

    hard_n = int(getattr(settings, "EDGE_HARD_QUARANTINE_SAMPLES", 8))
    hard_mean = _f(getattr(settings, "EDGE_HARD_QUARANTINE_MEAN_R", -0.15), -0.15)
    hard_pf = _f(getattr(settings, "EDGE_HARD_QUARANTINE_PF", 0.85), 0.85)
    if int(strategy.get("n", 0)) >= hard_n and _f(strategy.get("mean_r")) <= hard_mean and _f(strategy.get("profit_factor")) <= hard_pf:
        hard_veto = True
        reasons.append("strategy_recent_demo_edge_quarantined")
    elif int(strategy.get("n", 0)) >= int(getattr(settings, "EDGE_MIN_STRATEGY_SAMPLES", 6)) and _f(strategy.get("mean_r")) < 0:
        multiplier = min(multiplier, _f(getattr(settings, "EDGE_RECOVERY_STRATEGY_CAUTION_RISK_MULTIPLIER", 0.50), 0.50))
        reasons.append("strategy_recent_demo_edge_negative")

    if int(family_edge.get("n", 0)) >= int(getattr(settings, "EDGE_FAMILY_CAUTION_SAMPLES", 12)) and _f(family_edge.get("mean_r")) <= _f(getattr(settings, "EDGE_FAMILY_CAUTION_MEAN_R", -0.05), -0.05):
        multiplier = min(multiplier, _f(getattr(settings, "EDGE_RECOVERY_FAMILY_RISK_MULTIPLIER", 0.60), 0.60))
        reasons.append("family_recent_demo_edge_weak")

    smc = symbol_edge.get("monte_carlo") if isinstance(symbol_edge.get("monte_carlo"), dict) else {}
    if int(symbol_edge.get("n", 0)) >= int(getattr(settings, "EDGE_SYMBOL_MC_MIN_SAMPLES", 20)) and smc:
        pneg = _f(smc.get("probability_negative_final_r"))
        median = _f(smc.get("p50_final_r"))
        if pneg >= _f(getattr(settings, "EDGE_SYMBOL_MC_POOR_PROB_NEG", 0.75), 0.75) and median < 0:
            multiplier = min(multiplier, _f(getattr(settings, "EDGE_RECOVERY_SYMBOL_POOR_RISK_MULTIPLIER", 0.40), 0.40))
            reasons.append("symbol_monte_carlo_poor")
        elif pneg >= _f(getattr(settings, "EDGE_SYMBOL_MC_CAUTION_PROB_NEG", 0.60), 0.60) or median < 0:
            multiplier = min(multiplier, _f(getattr(settings, "EDGE_RECOVERY_SYMBOL_CAUTION_RISK_MULTIPLIER", 0.65), 0.65))
            reasons.append("symbol_monte_carlo_caution")

    omc = overall.get("monte_carlo") if isinstance(overall.get("monte_carlo"), dict) else {}
    if int(overall.get("n", 0)) >= int(getattr(settings, "EDGE_OVERALL_CAUTION_MIN_SAMPLES", 40)) and omc:
        if _f(omc.get("probability_negative_final_r")) >= 0.60 or _f(omc.get("p50_final_r")) < 0:
            multiplier = min(multiplier, _f(getattr(settings, "EDGE_RECOVERY_OVERALL_RISK_MULTIPLIER", 0.75), 0.75))
            reasons.append("portfolio_recent_edge_caution")

    state = "quarantine" if hard_veto else ("recovery" if multiplier < 0.999 else "normal")
    details = {
        "enabled": True, "state": state, "allow": not hard_veto,
        "risk_multiplier": max(0.0, min(1.0, multiplier)),
        "execution_tier": str(execution_tier), "reasons": reasons,
        "strategy": strategy, "family": family_edge, "symbol": symbol_edge, "overall": overall,
    }
    return (not hard_veto), max(0.0, min(1.0, multiplier)), details


def luna_value_add(connection: sqlite3.Connection) -> dict[str, Any]:
    """Audit Luna confirms and veto counterfactuals from persisted evidence."""
    try:
        confirms = connection.execute(
            """
            SELECT COUNT(*) n,COALESCE(SUM(outcome_reward_r),0) total_r,
                   COALESCE(AVG(outcome_reward_r),0) mean_r
            FROM gpt_candidate_reviews
            WHERE approved=1 AND outcome_status='closed' AND outcome_reward_r IS NOT NULL
            """
        ).fetchone()
        vetoes = connection.execute(
            """
            SELECT COUNT(*) n,COALESCE(SUM(reward_r),0) total_r,
                   COALESCE(AVG(reward_r),0) mean_r,
                   COALESCE(SUM(CASE WHEN reward_r<0 THEN 1 ELSE 0 END),0) saved_losses,
                   COALESCE(SUM(CASE WHEN reward_r>0 THEN 1 ELSE 0 END),0) missed_winners
            FROM gpt_veto_shadow_positions
            WHERE status='closed' AND reward_r IS NOT NULL
            """
        ).fetchone()
    except sqlite3.Error:
        return {"available": False}
    confirm_n = int(confirms["n"] or 0) if confirms else 0
    veto_n = int(vetoes["n"] or 0) if vetoes else 0
    # Avoid pretending a few examples prove value.  The useful interpretation is
    # the two separate series: confirmed real R and veto counterfactual R.
    return {
        "available": True,
        "confirm": {"n": confirm_n, "total_r": _f(confirms["total_r"] if confirms else 0), "mean_r": _f(confirms["mean_r"] if confirms else 0)},
        "veto_shadow": {
            "n": veto_n, "counterfactual_total_r": _f(vetoes["total_r"] if vetoes else 0),
            "counterfactual_mean_r": _f(vetoes["mean_r"] if vetoes else 0),
            "saved_losses": int(vetoes["saved_losses"] or 0) if vetoes else 0,
            "missed_winners": int(vetoes["missed_winners"] or 0) if vetoes else 0,
        },
        "enough_for_judgement": (confirm_n + veto_n) >= int(getattr(settings, "LAB_LUNA_MIN_VALUE_SAMPLES", 40)),
    }


def persist_snapshot(connection: sqlite3.Connection, snapshot_type: str, details: dict[str, Any], symbol: str | None = None, strategy_id: int | None = None, score: float | None = None) -> None:
    ensure_tables(connection)
    connection.execute(
        "INSERT INTO scalp_lab_snapshots(timestamp,snapshot_type,symbol,strategy_id,score,details_json) VALUES(?,?,?,?,?,?)",
        (_utc_now(), str(snapshot_type), symbol, int(strategy_id or 0), None if score is None else _f(score), json.dumps(details, separators=(",", ":"), default=str)),
    )


def portfolio_correlation_risk(
    symbol: str,
    side: int,
    candidate_frame: Any,
    machine_positions: list[Any],
    history_loader: Any,
) -> tuple[float, dict[str, Any]]:
    """Reduce *soft* risk when an open position duplicates the same factor bet.

    We use recent M1 close-return correlation. If corr * candidate_side *
    existing_side is strongly positive, both trades tend to win/lose together.
    This never increases risk; missing/insufficient data is neutral.
    """
    if not bool(getattr(settings, "LAB_CORRELATION_RISK_ENABLED", True)):
        return 1.0, {"enabled": False}
    others = [p for p in machine_positions if str(getattr(p, "symbol", "")) != str(symbol)]
    if not others:
        return 1.0, {"enabled": True, "multiplier": 1.0, "exposures": []}
    try:
        import numpy as np
        import pandas as pd
        cf = candidate_frame[["time", "close"]].tail(int(getattr(settings, "LAB_CORRELATION_BARS", 240))).copy()
        cf["time"] = pd.to_datetime(cf["time"], utc=True)
        cf["ret"] = cf["close"].pct_change()
        c = cf.set_index("time")["ret"].dropna()
    except Exception:
        return 1.0, {"enabled": True, "multiplier": 1.0, "reason": "candidate_returns_unavailable"}

    threshold = _f(getattr(settings, "LAB_CORRELATION_EXPOSURE_THRESHOLD", 0.72), 0.72)
    multiplier = 1.0
    exposures: list[dict[str, Any]] = []
    for pos in others:
        other_symbol = str(getattr(pos, "symbol", ""))
        try:
            of = history_loader(other_symbol, int(getattr(settings, "LAB_CORRELATION_BARS", 240)) + 5)
            of = of[["time", "close"]].copy()
            of["time"] = pd.to_datetime(of["time"], utc=True)
            of["ret"] = of["close"].pct_change()
            o = of.set_index("time")["ret"].dropna()
            joined = pd.concat([c.rename("c"), o.rename("o")], axis=1, join="inner").dropna().tail(200)
            if len(joined) < 60:
                continue
            corr = float(np.corrcoef(joined["c"].to_numpy(), joined["o"].to_numpy())[0, 1])
            # MT5 POSITION_TYPE_BUY is normally 0, SELL 1.
            existing_side = 1 if int(getattr(pos, "type", 0)) == 0 else -1
            exposure = corr * int(side) * existing_side
            exposures.append({"symbol": other_symbol, "corr": round(corr, 3), "existing_side": existing_side, "exposure": round(exposure, 3)})
            if exposure >= 0.90:
                multiplier = min(multiplier, 0.50)
            elif exposure >= threshold:
                multiplier = min(multiplier, 0.70)
        except Exception:
            continue
    return multiplier, {"enabled": True, "multiplier": multiplier, "threshold": threshold, "exposures": exposures}


def execution_quality_risk_multiplier(connection: sqlite3.Connection, symbol: str, window: int = 40) -> tuple[float, dict[str, Any]]:
    """Softly de-risk if recent *actual* DEMO fills show poor execution quality."""
    ensure_tables(connection)
    rows = connection.execute(
        """
        SELECT slippage_atr,spread_atr,latency_ms FROM execution_quality
        WHERE symbol=? ORDER BY id DESC LIMIT ?
        """, (str(symbol), max(5, int(window)))
    ).fetchall()
    min_n = max(5, int(getattr(settings, "LAB_EXECUTION_MIN_SAMPLES_FOR_RISK", 10)))
    if len(rows) < min_n:
        return 1.0, {"samples": len(rows), "multiplier": 1.0, "reason": "warming_up"}
    adverse = [max(0.0, _f(row["slippage_atr"])) for row in rows]
    spreads = [max(0.0, _f(row["spread_atr"])) for row in rows]
    latency = [max(0.0, _f(row["latency_ms"])) for row in rows]
    avg_slip = sum(adverse) / len(adverse)
    avg_spread = sum(spreads) / len(spreads)
    avg_latency = sum(latency) / len(latency)
    mult = 1.0
    state = "normal"
    if avg_slip >= 0.050 or avg_latency >= 3500:
        mult, state = 0.50, "poor_execution"
    elif avg_slip >= 0.025 or avg_latency >= 2000:
        mult, state = 0.75, "execution_caution"
    elif avg_spread >= 0.12:
        mult, state = 0.85, "spread_caution"
    return mult, {
        "samples": len(rows), "multiplier": mult, "state": state,
        "avg_adverse_slippage_atr": avg_slip, "avg_spread_atr": avg_spread,
        "avg_latency_ms": avg_latency,
    }
