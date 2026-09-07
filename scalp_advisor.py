"""Bounded asynchronous Luna pre-trade review for the V11 DEMO executor.

The execution thread never waits on a network request. A reviewed CONFIRM or
HOLD is authoritative for that one candidate; missing configuration, exhausted
quota, queue pressure and remote failures explicitly fall back to the complete
local deterministic stack instead of freezing the trading engine.
"""
from __future__ import annotations

import json
import os
import queue
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

import settings
import scalp_learning as learning


_QUEUE: queue.Queue[dict[str, Any]] = queue.Queue(
    maxsize=max(1, int(getattr(settings, "V11_LUNA_QUEUE_SIZE", 6)))
)
_THREADS: list[threading.Thread] = []
_LOCK = threading.Lock()


def _configured() -> bool:
    key = str(getattr(settings, "OPENAI_API_KEY", "") or os.environ.get("OPENAI_API_KEY", "")).strip()
    model = str(getattr(settings, "SPARTAN_LLM_MODEL", "") or getattr(settings, "GPT_MODEL", "")).strip()
    return bool(
        getattr(settings, "V11_ASYNC_LUNA_ENABLED", True)
        and getattr(settings, "SPARTAN_LLM_REVIEW_ENABLED", False)
        and key and model
    )


def _decode(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def _start_workers() -> None:
    workers = max(1, min(3, int(getattr(settings, "V11_LUNA_WORKERS", 2))))
    with _LOCK:
        _THREADS[:] = [thread for thread in _THREADS if thread.is_alive()]
        while len(_THREADS) < workers:
            thread = threading.Thread(
                target=_worker,
                daemon=True,
                name=f"v11-luna-pretrade-{len(_THREADS)+1}",
            )
            thread.start()
            _THREADS.append(thread)


def enqueue(payload: dict[str, Any]) -> str:
    """Queue an immutable review packet without ever blocking the caller."""
    if not _configured():
        return "optional_advisor_disabled_or_no_key"
    item = json.loads(json.dumps(payload, default=str))
    _start_workers()
    try:
        _QUEUE.put_nowait(item)
        return "queued_nonblocking_pretrade"
    except queue.Full:
        return "pretrade_queue_full_local_fallback"


def _available_capacity(connection: sqlite3.Connection) -> tuple[bool, str, dict[str, Any]]:
    import spartan_decision_memory as memory

    date = datetime.now(timezone.utc).date().isoformat()
    attempts = int(learning.get_state(connection, f"luna_attempts:{date}", 0))
    cap = max(1, int(getattr(settings, "SPARTAN_GPT_MAX_PRETRADE_API_CALLS_PER_DAY", 24)))
    if attempts >= cap:
        return False, "daily_attempt_cap", {"attempts": attempts, "cap": cap}
    calls = memory.daily_api_call_count(connection, "pretrade")
    if calls >= cap:
        return False, "daily_api_call_cap", {"calls": calls, "cap": cap}
    budget = memory.api_budget_snapshot(connection)
    if not bool(budget.get("allowed", True)):
        return False, "bot_dollar_budget", budget
    return True, "available", {"attempts": attempts, "calls": calls, "cap": cap, "budget": budget}


def pretrade_gate(connection: sqlite3.Connection, payload: dict[str, Any]) -> dict[str, Any]:
    """Return ``pending``, ``confirm``, ``veto`` or explicit local ``bypass``."""
    if not _configured():
        return {"state": "bypass", "reason": "disabled_or_missing_configuration"}

    learning.ensure_tables(connection)
    key = str(payload["key"])
    row = connection.execute(
        "SELECT * FROM v11_luna_reviews WHERE event_key=?", (key,)
    ).fetchone()
    if row is not None:
        state = str(row["state"])
        review = _decode(row["response_json"])
        cache = _decode(row["cache_json"])
        if state == "complete":
            approved = bool(row["approved"])
            return {
                "state": "confirm" if approved else "veto",
                "reason": str(row["decision"] or ("confirm" if approved else "hold")),
                "review": review,
                "cache": cache,
                "fingerprint": str(row["fingerprint"] or ""),
                "review_id": row["review_id"],
            }
        if state in {"queued", "running"}:
            age = max(0, learning.now_msc() - int(row["created_msc"]))
            max_wait = max(2.0, float(getattr(settings, "V11_LUNA_MAX_WAIT_SECONDS", 14.0))) * 1000
            if age <= max_wait:
                return {"state": "pending", "reason": state, "age_ms": age}
            connection.execute(
                "UPDATE v11_luna_reviews SET state='timed_out',updated_msc=?,error_code='review_wait_timeout' "
                "WHERE event_key=? AND state IN ('queued','running')",
                (learning.now_msc(), key),
            )
            return {"state": "bypass", "reason": "review_wait_timeout_local_fallback", "age_ms": age}
        return {"state": "bypass", "reason": f"{state}_local_fallback", "review": review, "cache": cache}

    available, reason, capacity = _available_capacity(connection)
    if not available:
        return {"state": "bypass", "reason": f"{reason}_local_fallback", "capacity": capacity}

    stamp = learning.now_msc()
    snapshot = payload.get("snapshot") if isinstance(payload.get("snapshot"), dict) else {}
    connection.execute(
        """INSERT INTO v11_luna_reviews(
            event_key,symbol,created_msc,updated_msc,state,snapshot_json)
            VALUES(?,?,?,?,'queued',?)""",
        (key, str(payload["symbol"]), stamp, stamp, learning.json_text(snapshot)),
    )
    # Make the durable queue reservation visible before a worker opens its own
    # connection. No broker intent/order exists at this point.
    connection.commit()
    queued = enqueue(payload)
    if queued != "queued_nonblocking_pretrade":
        connection.execute(
            "UPDATE v11_luna_reviews SET state='queue_full',updated_msc=?,error_code=? WHERE event_key=?",
            (learning.now_msc(), queued, key),
        )
        return {"state": "bypass", "reason": queued}
    return {"state": "pending", "reason": queued, "age_ms": 0}


def _reserve_attempt(connection: sqlite3.Connection) -> tuple[bool, str]:
    """Atomically reserve one daily attempt across concurrent workers."""
    date = datetime.now(timezone.utc).date().isoformat()
    name = f"luna_attempts:{date}"
    cap = max(1, int(getattr(settings, "SPARTAN_GPT_MAX_PRETRADE_API_CALLS_PER_DAY", 24)))
    connection.execute("BEGIN IMMEDIATE")
    try:
        attempts = int(learning.get_state(connection, name, 0))
        if attempts >= cap:
            connection.rollback()
            return False, "daily_attempt_cap"
        learning.set_state(connection, name, attempts + 1)
        connection.commit()
        return True, "reserved"
    except Exception:
        connection.rollback()
        raise


def _worker() -> None:
    import spartan_decision_memory as memory
    from spartan_llm import review_candidate

    while True:
        item = _QUEUE.get()
        connection: sqlite3.Connection | None = None
        try:
            # Autocommit means no SQLite writer lock is held during the API call.
            connection = sqlite3.connect(item["database"], timeout=5, isolation_level=None)
            connection.row_factory = sqlite3.Row
            claimed = connection.execute(
                "UPDATE v11_luna_reviews SET state='running',updated_msc=? "
                "WHERE event_key=? AND state='queued'",
                (learning.now_msc(), item["key"]),
            ).rowcount
            if not claimed:
                continue
            reserved, reserve_reason = _reserve_attempt(connection)
            if not reserved:
                connection.execute(
                    "UPDATE v11_luna_reviews SET state='skipped',updated_msc=?,error_code=? WHERE event_key=?",
                    (learning.now_msc(), reserve_reason, item["key"]),
                )
                continue

            snapshot = item["snapshot"]
            snapshot["gpt_calibration_context"] = memory.decision_memory_snapshot(
                connection, item["symbol"], item["regime"], int(item["side"])
            )
            review, cache = memory.review_with_cache(
                connection,
                snapshot,
                strategy_id=int(item["strategy_id"]),
                family=item["family"],
                regime=item["regime"],
                side=int(item["side"]),
                bar_time=item["bar_time"],
                reviewer=review_candidate,
            )
            source = str(review.get("decision_source") or "")
            authoritative = source in {"openai", "decision_fingerprint_cache"}
            approved = bool(review.get("approved", False))
            decision = "confirm" if approved else "hold"
            state = "complete" if authoritative else "error"
            error_code = None if authoritative else f"non_authoritative_{source or 'unknown'}"
            applied = connection.execute(
                """UPDATE v11_luna_reviews SET state=?,updated_msc=?,decision=?,approved=?,
                    confidence=?,fingerprint=?,review_id=?,response_json=?,cache_json=?,error_code=?
                    WHERE event_key=? AND state='running'""",
                (
                    state, learning.now_msc(), decision, int(approved),
                    float(review.get("confidence") or 0.0), str(cache.get("fingerprint") or ""),
                    cache.get("review_id"), learning.json_text(review), learning.json_text(cache),
                    error_code, item["key"],
                ),
            )
            learning.record_event(
                connection,
                item["symbol"],
                "luna_pretrade",
                (decision if authoritative else "local_fallback") if applied.rowcount else "late_review_ignored",
                {
                    "event_key": item["key"],
                    "review": review,
                    "cache": cache,
                    "execution_effect": ("candidate_confirm_or_veto" if authoritative else "none_local_fallback")
                                        if applied.rowcount else "none_review_arrived_after_local_timeout",
                },
                item["key"],
            )
        except Exception as error:
            if connection is not None:
                connection.execute(
                    "UPDATE v11_luna_reviews SET state='error',updated_msc=?,error_code=? "
                    "WHERE event_key=? AND state IN ('queued','running')",
                    (learning.now_msc(), type(error).__name__, item.get("key")),
                )
            # Never print API payloads, key material or raw remote exceptions.
            print(f"V11 Luna local fallback: {type(error).__name__}")
        finally:
            if connection is not None:
                connection.close()
            _QUEUE.task_done()
