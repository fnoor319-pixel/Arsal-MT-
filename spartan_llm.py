from __future__ import annotations

import json
import os
import time
from typing import Any, Literal

import settings

# Static instructions stay short. The dynamic market state is sent as ONE compact
# JSON packet so each final review is cheap, fast and deterministic to parse.
SYSTEM_PROMPT = """You are Luna, a concise final second brain for a DEMO execution-first micro-scalper.
The live Micro Hunter already found a real 2-5 candle/tick setup and Python has passed account/risk/cost execution locks. Spartan, SuperLearner and your opinion are ensemble evidence.
CONFIRM when the requested side is materially coherent. HOLD only for a meaningful contradiction or unusually poor setup; HOLD vetoes this candidate and must name the single strongest concern. Python may try a different candidate, while API unavailability is handled locally.
For continuation playbooks, strong multi-timeframe/SMC opposition is a concern. For reversal playbooks, countertrend is expected when rejection/exhaustion evidence is strong.
Do not reverse direction, invent data, chase a target, or choose lot/SL/TP. Unknown DOM/news is neutral. Return only the tiny structured result."""


def _num(value: Any, digits: int = 3) -> float | None:
    try:
        if value is None:
            return None
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


def _compact_memory(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {}
    return {
        "n": int(item.get("observations") or 0),
        "positive": _num(item.get("positive_rate")),
        "rewardR": _num(item.get("reward_mean")),
        "quality": _num(item.get("quality_ewma")),
    }


def compact_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Reduce the full deterministic snapshot to one decision-sized packet."""
    candidate = snapshot.get("candidate") if isinstance(snapshot.get("candidate"), dict) else {}
    strategy = snapshot.get("strategy_context") if isinstance(snapshot.get("strategy_context"), dict) else {}
    technical = snapshot.get("technical") if isinstance(snapshot.get("technical"), dict) else {}
    derived = snapshot.get("derived_context") if isinstance(snapshot.get("derived_context"), dict) else {}
    scores = snapshot.get("condition_scores") if isinstance(snapshot.get("condition_scores"), dict) else {}
    flow = snapshot.get("order_flow") if isinstance(snapshot.get("order_flow"), dict) else {}
    smc = snapshot.get("smc") if isinstance(snapshot.get("smc"), dict) else {}
    session = snapshot.get("session") if isinstance(snapshot.get("session"), dict) else {}
    news = snapshot.get("news") if isinstance(snapshot.get("news"), dict) else {}
    ml = snapshot.get("ml") if isinstance(snapshot.get("ml"), dict) else {}
    learning = snapshot.get("learning_context") if isinstance(snapshot.get("learning_context"), dict) else {}
    portfolio = snapshot.get("portfolio_context") if isinstance(snapshot.get("portfolio_context"), dict) else {}
    memory = snapshot.get("gpt_calibration_context") if isinstance(snapshot.get("gpt_calibration_context"), dict) else {}
    htf = snapshot.get("higher_timeframes") if isinstance(snapshot.get("higher_timeframes"), dict) else {}
    vp = snapshot.get("volume_profile") if isinstance(snapshot.get("volume_profile"), dict) else {}
    tick_flow = snapshot.get("tick_flow") if isinstance(snapshot.get("tick_flow"), dict) else {}
    capital = snapshot.get("capital_growth") if isinstance(snapshot.get("capital_growth"), dict) else {}
    micro = snapshot.get("micro_hunter") if isinstance(snapshot.get("micro_hunter"), dict) else {}
    plan = snapshot.get("v11_plan") if isinstance(snapshot.get("v11_plan"), dict) else {}
    risk_distance = abs(float(plan.get("risk_distance") or 0.0))
    target_r = abs(float(plan.get("take") or 0.0)-float(plan.get("entry") or 0.0)) / risk_distance if risk_distance > 0 else None
    total_cost_r = sum(float(plan.get(name) or 0.0) for name in ("commission_r", "slippage_r", "spread_r"))

    htf_trends: dict[str, str] = {}
    for name, item in htf.items():
        if isinstance(item, dict) and item.get("available"):
            htf_trends[str(name)] = str(item.get("trend") or "neutral")

    sweep = smc.get("liquidity_sweep") if isinstance(smc.get("liquidity_sweep"), dict) else {}
    packet = {
        "v": "8.0",
        "mode": str(snapshot.get("review_mode") or "final_review"),
        "symbol": str(snapshot.get("symbol") or ""),
        "side": str(candidate.get("action") or "hold"),
        "opportunity": _num(snapshot.get("opportunity_score"), 1),
        "localAlpha": _num(snapshot.get("v8_local_alpha_score"), 1),
        "strategy": {
            "id": int(strategy.get("strategy_id") or 0),
            "family": str(strategy.get("family") or "unknown"),
            "regime": str(strategy.get("regime") or derived.get("regime") or "unknown"),
            "tier": str(strategy.get("execution_tier") or "unknown"),
        },
        "gate": {
            "score8": int(candidate.get("confluence_score") or 0),
            "available": f"{int(scores.get('available_passed') or 0)}/{int(scores.get('available_total') or 0)}",
            "ratio": _num(scores.get("available_ratio")),
            "core": f"{int(scores.get('core_passed') or 0)}/{int(scores.get('core_total') or 0)}",
            "confidence": _num(candidate.get("confidence")),
        },
        "tech": {
            "regime": derived.get("regime"),
            "rsi7": _num(technical.get("rsi7"), 1),
            "adx14": _num(technical.get("adx14"), 1),
            "atr14": _num(technical.get("atr14")),
            "emaSpreadATR": _num(derived.get("ema5_minus_ema13_atr")),
            "priceVsEma200Pct": _num(derived.get("price_vs_ema200_pct")),
            "spreadATR": _num(derived.get("spread_atr_ratio")),
        },
        "flow": {
            "dom": bool(flow.get("available")),
            "imbalancePct": _num(flow.get("imbalance_pct"), 1),
            "persistence": _num(flow.get("persistence")),
            "tickDelta": _num(tick_flow.get("delta_ratio")) if tick_flow.get("available") else None,
        },
        "structure": {
            "smc": smc.get("structure"),
            "choch": smc.get("choch"),
            "sweep": sweep.get("side"),
            "htf": htf_trends,
            "vpPosATR": _num(derived.get("price_minus_poc_atr")) if vp.get("available") else None,
        },
        "context": {
            "sessionAllowed": bool(session.get("allowed")),
            "session": session.get("session") or session.get("name") or session.get("label"),
            "newsKnown": bool(news.get("available")),
            "newsLocked": bool(news.get("locked")),
        },
        "ml": {
            "p": _num(ml.get("probability")),
            "rawP": _num(ml.get("model_probability_raw")),
            "threshold": _num(ml.get("threshold")),
            "expectedR": _num(ml.get("expected_r")),
            "rr": _num(ml.get("reward_to_risk")),
            "bayesLoss": _num(ml.get("bayes_loss_probability")),
            "active": bool(ml.get("probability_active")),
        },
        "economics": {
            "targetR": _num(target_r),
            "costR": _num(total_cost_r),
            "costToTarget": _num(total_cost_r/target_r) if target_r else None,
            "maxHoldSec": _num(plan.get("max_hold_seconds"), 0),
        },
        "micro": {
            "playbook": micro.get("playbook"),
            "phase": micro.get("phase"),
            "score": _num(micro.get("score"), 1),
            "bodyATR": _num((micro.get("context") or {}).get("bar_body_atr")),
            "rangeATR": _num((micro.get("context") or {}).get("bar_range_atr")),
            "vol": _num((micro.get("context") or {}).get("volume_ratio")),
            "mom3": _num((micro.get("context") or {}).get("momentum3_atr")),
            "memN": int(((micro.get("playbook_memory") or {}).get("observations") or 0)),
            "memR": _num((micro.get("playbook_memory") or {}).get("reward_ewma")),
            "mfe": _num((micro.get("playbook_memory") or {}).get("mfe_mean")),
            "mae": _num((micro.get("playbook_memory") or {}).get("mae_mean")),
        } if micro else {},
        "learning": {
            "confidence": _num(learning.get("confidence")),
            "riskAdj": _num(learning.get("risk_multiplier_advisory")),
        },
        "portfolio": {
            "dailyLossPct": _num(portfolio.get("daily_loss_pct"), 4),
            "drawdownPct": _num(portfolio.get("drawdown_pct"), 4),
            "open": int(portfolio.get("open_machine_positions") or 0),
            "today": int(portfolio.get("daily_trade_count") or 0),
        },
        "capital": {
            "dayGrowthPct": _num(capital.get("growth_pct"), 4),
            "peakGrowthPct": _num(capital.get("peak_growth_pct"), 4),
            "targetPct": _num(capital.get("target_pct"), 4),
            "stretchPct": _num(capital.get("stretch_pct"), 4),
            "phase": capital.get("phase"),
        },
        "memory": {
            "confirm": _compact_memory(memory.get("confirm")),
            "veto": _compact_memory(memory.get("veto")),
        },
    }
    return packet


def review_candidate(snapshot: dict[str, Any]) -> dict[str, Any]:
    enabled = bool(getattr(settings, "SPARTAN_LLM_REVIEW_ENABLED", True))
    candidate = str(snapshot.get("candidate", {}).get("action") or "hold").lower()
    if candidate not in {"buy", "sell"}:
        return {
            "enabled": enabled, "approved": False, "action": "hold", "decision": "hold",
            "confidence": 0.0, "context_summary": "invalid_candidate",
            "reasoning": "No executable BUY/SELL candidate.", "contradictions": ["invalid_candidate"],
            "risk_flags": [], "agents_vote": snapshot.get("agents_vote", {}),
            "latency_ms": 0.0, "decision_source": "input_validation",
        }

    if not enabled:
        return {
            "enabled": False, "approved": True, "action": candidate, "decision": "confirm",
            "confidence": float(snapshot.get("candidate", {}).get("confidence", 0.0) or 0.0),
            "context_summary": "llm_disabled", "reasoning": "LLM reviewer disabled.",
            "contradictions": [], "risk_flags": [], "agents_vote": snapshot.get("agents_vote", {}),
            "latency_ms": 0.0, "decision_source": "disabled",
        }

    model = str(
        getattr(settings, "SPARTAN_LLM_MODEL", "")
        or getattr(settings, "GPT_MODEL", "")
        or os.environ.get("OPENAI_MODEL", "")
    ).strip()
    key = str(getattr(settings, "OPENAI_API_KEY", "") or os.environ.get("OPENAI_API_KEY", "")).strip()
    fail_closed = bool(getattr(settings, "SPARTAN_LLM_FAIL_CLOSED", True))
    if not key or not model:
        return {
            "enabled": True, "approved": not fail_closed,
            "action": "hold" if fail_closed else candidate,
            "decision": "hold" if fail_closed else "confirm", "confidence": 0.0,
            "context_summary": "missing_configuration", "reasoning": "OpenAI configuration missing.",
            "contradictions": ["missing_openai_configuration"], "risk_flags": ["llm_unavailable"],
            "agents_vote": snapshot.get("agents_vote", {}), "latency_ms": 0.0,
            "decision_source": "configuration_error", "error": "missing_configuration",
        }

    started = time.perf_counter()
    try:
        from openai import OpenAI
        from pydantic import BaseModel, Field

        max_reason = max(32, int(getattr(settings, "SPARTAN_LLM_MAX_REASON_CHARS", 80)))

        class Review(BaseModel):
            decision: Literal["confirm", "hold"]
            confidence: float = Field(ge=0.0, le=1.0)
            reason_code: str = Field(min_length=1, max_length=40)
            reason: str = Field(min_length=1, max_length=max_reason)

        packet = compact_snapshot(snapshot) if bool(getattr(settings, "SPARTAN_LLM_COMPACT_PACKET", True)) else snapshot
        client = OpenAI(api_key=key, timeout=float(getattr(settings, "SPARTAN_LLM_TIMEOUT_SECONDS", 12.0)))
        kwargs: dict[str, Any] = {
            "model": model,
            "instructions": SYSTEM_PROMPT,
            # ONE compact dynamic message in; no conversation history is sent.
            "input": json.dumps(packet, separators=(",", ":"), default=str),
            "text_format": Review,
            "store": False,
            "max_output_tokens": max(48, int(getattr(settings, "SPARTAN_LLM_MAX_OUTPUT_TOKENS", 80))),
        }
        cache_key = str(getattr(settings, "SPARTAN_GPT_PROMPT_CACHE_KEY", "spartan-scalp-pretrade-v68") or "").strip()
        if cache_key:
            kwargs["prompt_cache_key"] = cache_key
        effort = str(getattr(settings, "SPARTAN_GPT_PRETRADE_REASONING_EFFORT", "none") or "none").strip()
        if effort:
            kwargs["reasoning"] = {"effort": effort}
        response = client.responses.parse(**kwargs)
        parsed = response.output_parsed
        if parsed is None:
            raise RuntimeError("Structured response could not be parsed")

        usage_obj = getattr(response, "usage", None)

        def _usage_value(name: str) -> int:
            if usage_obj is None:
                return 0
            if isinstance(usage_obj, dict):
                return max(0, int(usage_obj.get(name) or 0))
            return max(0, int(getattr(usage_obj, name, 0) or 0))

        usage = {
            "input_tokens": _usage_value("input_tokens"),
            "output_tokens": _usage_value("output_tokens"),
            "total_tokens": _usage_value("total_tokens"),
        }
        min_conf = float(getattr(settings, "SPARTAN_MIN_CONFIDENCE", 0.70))
        approved = bool(parsed.decision == "confirm" and parsed.confidence >= min_conf)
        reason_code = str(parsed.reason_code)[:40]
        contradictions: list[str] = [] if approved else [reason_code]
        if parsed.decision == "confirm" and parsed.confidence < min_conf:
            contradictions.append("llm_confidence_below_threshold")

        return {
            "enabled": True,
            "approved": approved,
            "action": candidate if approved else "hold",
            "decision": parsed.decision,
            "confidence": float(parsed.confidence),
            "context_summary": reason_code,
            "reasoning": str(parsed.reason)[:max_reason],
            "contradictions": contradictions,
            "risk_flags": [],
            "agents_vote": dict(snapshot.get("agents_vote", {})),
            "latency_ms": (time.perf_counter() - started) * 1000.0,
            "decision_source": "openai",
            "model": model,
            "usage": usage,
            "packet_chars": len(json.dumps(packet, separators=(",", ":"), default=str)),
        }
    except Exception as error:
        return {
            "enabled": True, "approved": not fail_closed,
            "action": "hold" if fail_closed else candidate,
            "decision": "hold" if fail_closed else "confirm", "confidence": 0.0,
            "context_summary": "llm_runtime_error", "reasoning": f"Luna error: {type(error).__name__}",
            "contradictions": ["llm_runtime_error"], "risk_flags": ["llm_unavailable"],
            "agents_vote": snapshot.get("agents_vote", {}),
            "latency_ms": (time.perf_counter() - started) * 1000.0,
            "decision_source": "runtime_error", "model": model, "error": str(error)[:500],
        }
