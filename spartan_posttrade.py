from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import settings
from spartan_decision_memory import record_usage_event, daily_api_call_count, api_budget_snapshot

SYSTEM_PROMPT = """You are Luna, a DEMO scalping post-trade analyst. Diagnose only the supplied compact trade evidence.
Classify the result, identify the most useful lesson, and give at most one testable research hypothesis.
Never change live risk, lot size, SL/TP, sessions, broker controls or thresholds. Do not invent hindsight data.
A loss is not automatically a bad setup. Return only the requested short structured result."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS gpt_post_trade_reviews (
            demo_position_id INTEGER PRIMARY KEY,
            position_ticket INTEGER,
            symbol TEXT NOT NULL,
            strategy_id INTEGER,
            reviewed_at TEXT NOT NULL,
            status TEXT NOT NULL,
            model TEXT,
            classification TEXT,
            confidence REAL,
            summary TEXT,
            strengths_json TEXT NOT NULL,
            weaknesses_json TEXT NOT NULL,
            execution_notes_json TEXT NOT NULL,
            research_hypotheses_json TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            response_json TEXT NOT NULL,
            error TEXT
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_gpt_post_trade_reviews_symbol_time ON gpt_post_trade_reviews(symbol, reviewed_at)"
    )
    connection.commit()


def _persist(payload: dict[str, Any], review: dict[str, Any]) -> None:
    db = Path(getattr(settings, "DATABASE_PATH", Path(settings.PROJECT_DIR) / "trading_machine.db"))
    with sqlite3.connect(db, timeout=30) as connection:
        _ensure_table(connection)
        connection.execute(
            """
            INSERT INTO gpt_post_trade_reviews(
                demo_position_id, position_ticket, symbol, strategy_id, reviewed_at,
                status, model, classification, confidence, summary, strengths_json,
                weaknesses_json, execution_notes_json, research_hypotheses_json,
                payload_json, response_json, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(demo_position_id) DO UPDATE SET
                position_ticket=excluded.position_ticket,
                symbol=excluded.symbol,
                strategy_id=excluded.strategy_id,
                reviewed_at=excluded.reviewed_at,
                status=excluded.status,
                model=excluded.model,
                classification=excluded.classification,
                confidence=excluded.confidence,
                summary=excluded.summary,
                strengths_json=excluded.strengths_json,
                weaknesses_json=excluded.weaknesses_json,
                execution_notes_json=excluded.execution_notes_json,
                research_hypotheses_json=excluded.research_hypotheses_json,
                payload_json=excluded.payload_json,
                response_json=excluded.response_json,
                error=excluded.error
            """,
            (
                int(payload.get("demo_position_id") or 0),
                int(payload.get("position_ticket") or 0),
                str(payload.get("symbol") or "UNKNOWN"),
                int(payload.get("strategy_id") or 0),
                str(review.get("reviewed_at") or _utc_now()),
                str(review.get("status") or "unknown"),
                str(review.get("model") or ""),
                str(review.get("classification") or "unknown"),
                float(review.get("confidence") or 0.0),
                str(review.get("summary") or ""),
                json.dumps(review.get("strengths") or [], default=str),
                json.dumps(review.get("weaknesses") or [], default=str),
                json.dumps(review.get("execution_notes") or [], default=str),
                json.dumps(review.get("research_hypotheses") or [], default=str),
                json.dumps(payload, default=str),
                json.dumps(review, default=str),
                str(review.get("error") or ""),
            ),
        )
        if str(review.get("status") or "") == "ok":
            record_usage_event(
                connection,
                call_type="posttrade",
                model=str(review.get("model") or ""),
                usage=review.get("usage") if isinstance(review.get("usage"), dict) else {},
                symbol=str(payload.get("symbol") or ""),
                fingerprint=str((payload.get("entry_context") or {}).get("spartan_llm", {}).get("fingerprint") or ""),
                source="openai",
            )
        connection.commit()

    reports = Path(getattr(settings, "REPORTS_DIR", Path(settings.PROJECT_DIR) / "reports")) / "post_trade"
    reports.mkdir(parents=True, exist_ok=True)
    position_id = int(payload.get("demo_position_id") or 0)
    target = reports / f"post_trade_{position_id:08d}.json"
    target.write_text(json.dumps({"trade": payload, "review": review}, indent=2, default=str), encoding="utf-8")
    latest = reports / "latest.json"
    latest.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")


def _n(value: Any, digits: int = 3) -> float | None:
    try:
        if value is None:
            return None
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


def compact_posttrade_payload(payload: dict[str, Any]) -> dict[str, Any]:
    entry = payload.get("entry_context") if isinstance(payload.get("entry_context"), dict) else {}
    sp = entry.get("spartan_pro") if isinstance(entry.get("spartan_pro"), dict) else {}
    sl = entry.get("superlearner") if isinstance(entry.get("superlearner"), dict) else {}
    llm = entry.get("spartan_llm") if isinstance(entry.get("spartan_llm"), dict) else {}
    candidate = sp.get("candidate") if isinstance(sp.get("candidate"), dict) else {}
    scores = sp.get("condition_scores") if isinstance(sp.get("condition_scores"), dict) else {}
    technical = sp.get("technical") if isinstance(sp.get("technical"), dict) else {}
    market = sl.get("market") if isinstance(sl.get("market"), dict) else {}
    return {
        "v": "6.6",
        "trade": {
            "symbol": payload.get("symbol"), "family": payload.get("strategy_family"),
            "regime": payload.get("regime"), "side": payload.get("side"),
            "tier": payload.get("execution_tier"), "rewardR": _n(payload.get("reward_r")),
            "mfeR": _n(payload.get("mfe_r")), "maeR": _n(payload.get("mae_r")),
            "pnl": _n(payload.get("pnl"), 2), "close": payload.get("close_reason"),
        },
        "entry": {
            "confluence8": int(candidate.get("confluence_score") or 0),
            "availableRatio": _n(scores.get("available_ratio")),
            "confidence": _n(candidate.get("confidence")),
            "rsi7": _n(technical.get("rsi7"), 1), "adx14": _n(technical.get("adx14"), 1),
            "spreadATR": _n((sp.get("market") or {}).get("spread_atr") if isinstance(sp.get("market"), dict) else None),
            "mlP": _n(sl.get("probability")), "mlThreshold": _n(sl.get("threshold")),
            "expectedR": _n(market.get("expected_r")),
            "gptDecision": llm.get("decision"), "gptConfidence": _n(llm.get("confidence")),
            "gptReason": str(llm.get("reasoning") or "")[:140],
        },
        "after": payload.get("adaptive_learning_after") or {},
    }


def review_closed_trade(payload: dict[str, Any]) -> dict[str, Any]:
    if not bool(getattr(settings, "SPARTAN_POST_TRADE_REVIEW_ENABLED", True)):
        return {"status": "disabled", "reviewed_at": _utc_now(), "classification": "unknown", "confidence": 0.0,
                "summary": "Post-trade GPT reviewer disabled", "strengths": [], "weaknesses": [],
                "execution_notes": [], "research_hypotheses": []}

    db = Path(getattr(settings, "DATABASE_PATH", Path(settings.PROJECT_DIR) / "trading_machine.db"))
    try:
        with sqlite3.connect(db, timeout=10) as budget_con:
            budget_con.row_factory = sqlite3.Row
            cap = max(0, int(getattr(settings, "SPARTAN_GPT_MAX_POSTTRADE_API_CALLS_PER_DAY", 8)))
            used = daily_api_call_count(budget_con, "posttrade")
            budget = api_budget_snapshot(budget_con)
        if cap == 0 or used >= cap:
            return {"status": "daily_cap", "reviewed_at": _utc_now(), "classification": "unknown", "confidence": 0.0,
                    "summary": f"Post-trade Luna cap reached ({used}/{cap})", "strengths": [], "weaknesses": [],
                    "execution_notes": [], "research_hypotheses": [], "budget": budget}
        if not bool(budget.get("allowed", True)):
            return {"status": "budget_guard", "reviewed_at": _utc_now(), "classification": "unknown", "confidence": 0.0,
                    "summary": "Post-trade Luna skipped by bot budget guard", "strengths": [], "weaknesses": [],
                    "execution_notes": [], "research_hypotheses": [], "budget": budget}
    except Exception:
        pass

    key = str(getattr(settings, "OPENAI_API_KEY", "") or "").strip()
    model = str(getattr(settings, "SPARTAN_POST_TRADE_MODEL", "") or getattr(settings, "GPT_MODEL", "") or "").strip()
    if not key or not model:
        return {"status": "configuration_error", "reviewed_at": _utc_now(), "model": model,
                "classification": "unknown", "confidence": 0.0,
                "summary": "Post-trade GPT review skipped because OpenAI configuration is missing",
                "strengths": [], "weaknesses": [], "execution_notes": [], "research_hypotheses": [],
                "error": "missing_openai_configuration"}

    started = time.perf_counter()
    try:
        from openai import OpenAI
        from pydantic import BaseModel, Field

        class PostTradeReview(BaseModel):
            classification: Literal["valid_win", "valid_loss", "setup_issue", "execution_issue", "data_issue", "mixed", "unknown"]
            confidence: float = Field(ge=0.0, le=1.0)
            lesson_code: str = Field(min_length=1, max_length=40)
            summary: str = Field(min_length=1, max_length=180)
            hypothesis: str = Field(default="", max_length=160)

        compact = compact_posttrade_payload(payload) if bool(getattr(settings, "SPARTAN_POST_TRADE_COMPACT_PACKET", True)) else payload
        client = OpenAI(api_key=key, timeout=float(getattr(settings, "SPARTAN_POST_TRADE_TIMEOUT_SECONDS", 15.0)))
        kwargs: dict[str, Any] = {
            "model": model,
            "instructions": SYSTEM_PROMPT,
            "input": json.dumps(compact, separators=(",", ":"), default=str),
            "text_format": PostTradeReview,
            "store": False,
            "max_output_tokens": max(80, int(getattr(settings, "SPARTAN_POST_TRADE_MAX_OUTPUT_TOKENS", 110))),
        }
        cache_key = str(getattr(settings, "SPARTAN_POST_TRADE_PROMPT_CACHE_KEY", "spartan-scalp-posttrade-v67") or "").strip()
        if cache_key:
            kwargs["prompt_cache_key"] = cache_key
        effort = str(getattr(settings, "SPARTAN_POST_TRADE_REASONING_EFFORT", "low") or "low").strip()
        if effort:
            kwargs["reasoning"] = {"effort": effort}
        response = client.responses.parse(**kwargs)
        parsed = response.output_parsed
        if parsed is None:
            raise RuntimeError("Structured post-trade response could not be parsed")
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
        hypothesis = str(parsed.hypothesis or "")[:160]
        return {
            "status": "ok",
            "reviewed_at": _utc_now(),
            "model": model,
            "classification": parsed.classification,
            "confidence": float(parsed.confidence),
            "summary": str(parsed.summary)[:180],
            "strengths": [str(parsed.lesson_code)] if parsed.classification == "valid_win" else [],
            "weaknesses": [str(parsed.lesson_code)] if parsed.classification not in {"valid_win", "valid_loss"} else [],
            "execution_notes": [str(parsed.lesson_code)] if parsed.classification == "execution_issue" else [],
            "research_hypotheses": [hypothesis] if hypothesis else [],
            "latency_ms": (time.perf_counter() - started) * 1000.0,
            "live_parameter_mutation": False,
            "usage": usage,
            "packet_chars": len(json.dumps(compact, separators=(",", ":"), default=str)),
        }
    except Exception as error:
        return {
            "status": "runtime_error", "reviewed_at": _utc_now(), "model": model,
            "classification": "unknown", "confidence": 0.0,
            "summary": f"Post-trade GPT review failed: {type(error).__name__}",
            "strengths": [], "weaknesses": [], "execution_notes": [], "research_hypotheses": [],
            "latency_ms": (time.perf_counter() - started) * 1000.0,
            "live_parameter_mutation": False,
            "error": str(error)[:600],
        }


def _worker(payload: dict[str, Any]) -> None:
    try:
        review = review_closed_trade(payload)
        _persist(payload, review)
        print(
            f"GPT POST-TRADE {payload.get('symbol')} id={payload.get('demo_position_id')} | "
            f"{review.get('classification')} conf={float(review.get('confidence') or 0.0):.2f} | {review.get('summary')}"
        )
    except Exception as error:
        print(f"GPT POST-TRADE WARNING: {type(error).__name__}: {error}")


def enqueue_closed_trade_review(payload: dict[str, Any]) -> None:
    """Queue a non-blocking advisory review. It never participates in order execution."""
    if not bool(getattr(settings, "SPARTAN_POST_TRADE_REVIEW_ENABLED", True)):
        return
    thread = threading.Thread(
        target=_worker,
        args=(dict(payload),),
        name=f"spartan-posttrade-{int(payload.get('demo_position_id') or 0)}",
        daemon=True,
    )
    thread.start()
