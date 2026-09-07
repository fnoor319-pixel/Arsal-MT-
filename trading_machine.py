from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import sqlite3
import sys
import time
import traceback
from contextlib import contextmanager
from collections import deque
from dataclasses import asdict as dataclass_asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

import MetaTrader5 as mt5
import numpy as np
import pandas as pd

import settings
import spartan_pro as spartan
from spartan_llm import review_candidate as spartan_llm_review
from spartan_alerts import telegram_alert as spartan_telegram_alert
from spartan_posttrade import enqueue_closed_trade_review as spartan_posttrade_enqueue
import spartan_decision_memory as gpt_memory
import scalp_ai_policy as scalp_ai
import scalp_diagnostics as scalp_diag
import scalp_lab
import capital_growth
import micro_scalp_hunter as micro_hunter
import institutional_alpha as institutional_alpha
import v9_execution_first as v9_exec
import v9_2_adaptive_scalper as v92
import v10_superhuman_scalper as v10
import scalp_learning as scalp_learning_v11
import scalp_runtime as scalp_v11


# ============================================================
# Utility
# ============================================================


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def utc_date() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def ensure_folders() -> None:
    for folder in (settings.REPORTS_DIR, settings.DATA_DIR, settings.LOGS_DIR):
        folder.mkdir(parents=True, exist_ok=True)


def json_text(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def strategy_hash(symbol: str, family: str, params: dict[str, Any]) -> str:
    payload = f"{symbol}|{family}|{json_text(params)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def as_dict(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "_asdict"):
        return {k: as_dict(v) for k, v in value._asdict().items()}
    if isinstance(value, dict):
        return {k: as_dict(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [as_dict(v) for v in value]
    return value


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


# ============================================================
# Database
# ============================================================


_INITIALIZED_DATABASE_PATH: Path | None = None


@contextmanager
def db_connect() -> Iterator[sqlite3.Connection]:
    """Open a short-lived SQLite transaction and always release the file.

    ``sqlite3.Connection`` commits/rolls back when used directly as a context
    manager, but it does *not* close the connection on exit.  That behaviour
    leaves ``test.db`` locked on Windows and can also accumulate handles in the
    24/7 worker loops.  This wrapper preserves transaction semantics and then
    explicitly closes the connection in every path.
    """
    connection = sqlite3.connect(settings.DATABASE_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=30000")
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        yield connection
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()
    finally:
        connection.close()


def init_database() -> None:
    global _INITIALIZED_DATABASE_PATH
    current_path = Path(settings.DATABASE_PATH).resolve()
    if _INITIALIZED_DATABASE_PATH == current_path and current_path.exists():
        return

    ensure_folders()
    schema = """
    PRAGMA journal_mode=WAL;

    CREATE TABLE IF NOT EXISTS strategies (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        strategy_hash TEXT NOT NULL UNIQUE,
        symbol TEXT NOT NULL,
        family TEXT NOT NULL,
        params_json TEXT NOT NULL,
        generation INTEGER NOT NULL DEFAULT 0,
        parent_id INTEGER,
        status TEXT NOT NULL DEFAULT 'generated',
        created_at TEXT NOT NULL,
        FOREIGN KEY(parent_id) REFERENCES strategies(id)
    );

    CREATE INDEX IF NOT EXISTS idx_strategies_symbol_status
        ON strategies(symbol, status);

    CREATE TABLE IF NOT EXISTS backtests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        strategy_id INTEGER NOT NULL,
        symbol TEXT NOT NULL,
        timeframe TEXT NOT NULL,
        bars INTEGER NOT NULL,
        trades INTEGER NOT NULL,
        wins INTEGER NOT NULL,
        losses INTEGER NOT NULL,
        win_rate REAL NOT NULL,
        net_profit REAL NOT NULL,
        return_pct REAL NOT NULL,
        max_drawdown_pct REAL NOT NULL,
        profit_factor REAL NOT NULL,
        score REAL NOT NULL,
        tested_at TEXT NOT NULL,
        metrics_json TEXT NOT NULL,
        FOREIGN KEY(strategy_id) REFERENCES strategies(id)
    );

    CREATE INDEX IF NOT EXISTS idx_backtests_strategy
        ON backtests(strategy_id, tested_at);

    CREATE TABLE IF NOT EXISTS strategy_diagnostics (
        strategy_id INTEGER PRIMARY KEY,
        symbol TEXT NOT NULL,
        train_trades INTEGER NOT NULL,
        train_profit_factor REAL NOT NULL,
        train_score REAL NOT NULL,
        oos_trades INTEGER NOT NULL,
        oos_profit_factor REAL NOT NULL,
        oos_score REAL NOT NULL,
        stability_score REAL NOT NULL,
        validation_reason TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(strategy_id) REFERENCES strategies(id)
    );

    CREATE TABLE IF NOT EXISTS backtest_errors (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        strategy_id INTEGER,
        symbol TEXT NOT NULL,
        stage TEXT NOT NULL,
        error_type TEXT NOT NULL,
        error_message TEXT NOT NULL,
        traceback_text TEXT NOT NULL,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS strategy_scores (
        strategy_id INTEGER NOT NULL,
        symbol TEXT NOT NULL,
        regime TEXT NOT NULL,
        observations INTEGER NOT NULL DEFAULT 0,
        reward_mean REAL NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(strategy_id, symbol, regime),
        FOREIGN KEY(strategy_id) REFERENCES strategies(id)
    );

    CREATE TABLE IF NOT EXISTS paper_positions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        strategy_id INTEGER NOT NULL,
        symbol TEXT NOT NULL,
        regime TEXT NOT NULL,
        side INTEGER NOT NULL,
        volume REAL NOT NULL,
        entry_price REAL NOT NULL,
        stop_loss REAL NOT NULL,
        take_profit REAL NOT NULL,
        opened_at TEXT NOT NULL,
        closed_at TEXT,
        exit_price REAL,
        pnl REAL,
        reward_r REAL,
        status TEXT NOT NULL DEFAULT 'open',
        close_reason TEXT,
        FOREIGN KEY(strategy_id) REFERENCES strategies(id)
    );

    CREATE INDEX IF NOT EXISTS idx_paper_open
        ON paper_positions(status, symbol);

    CREATE TABLE IF NOT EXISTS demo_positions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        position_ticket INTEGER NOT NULL,
        strategy_id INTEGER NOT NULL,
        symbol TEXT NOT NULL,
        regime TEXT NOT NULL,
        side INTEGER NOT NULL,
        volume REAL NOT NULL,
        entry_price REAL NOT NULL,
        stop_loss REAL NOT NULL,
        take_profit REAL NOT NULL,
        risk_cash REAL NOT NULL,
        opened_at TEXT NOT NULL,
        closed_at TEXT,
        pnl REAL,
        reward_r REAL,
        status TEXT NOT NULL DEFAULT 'open',
        close_reason TEXT,
        result_json TEXT NOT NULL,
        FOREIGN KEY(strategy_id) REFERENCES strategies(id)
    );

    CREATE INDEX IF NOT EXISTS idx_demo_positions_open
        ON demo_positions(status, position_ticket, symbol);

    CREATE TABLE IF NOT EXISTS decision_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL,
        mode TEXT NOT NULL,
        symbol TEXT NOT NULL,
        regime TEXT,
        strategy_id INTEGER,
        signal INTEGER,
        reason TEXT NOT NULL,
        details_json TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS execution_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL,
        symbol TEXT NOT NULL,
        strategy_id INTEGER,
        request_json TEXT,
        result_json TEXT,
        status TEXT NOT NULL,
        message TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS system_state (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS rl_reward_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL,
        strategy_id INTEGER NOT NULL,
        symbol TEXT NOT NULL,
        regime TEXT NOT NULL,
        reward REAL NOT NULL,
        source TEXT NOT NULL,
        details_json TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS adaptive_trade_memory (
        strategy_id INTEGER NOT NULL,
        symbol TEXT NOT NULL,
        regime TEXT NOT NULL,
        side INTEGER NOT NULL,
        observations INTEGER NOT NULL DEFAULT 0,
        wins INTEGER NOT NULL DEFAULT 0,
        losses INTEGER NOT NULL DEFAULT 0,
        consecutive_losses INTEGER NOT NULL DEFAULT 0,
        gross_win_r REAL NOT NULL DEFAULT 0,
        gross_loss_r REAL NOT NULL DEFAULT 0,
        reward_mean REAL NOT NULL DEFAULT 0,
        reward_ewma REAL NOT NULL DEFAULT 0,
        confidence REAL NOT NULL DEFAULT 0.5,
        last_reward REAL NOT NULL DEFAULT 0,
        last_closed_at TEXT,
        last_loss_at TEXT,
        blocked_until TEXT,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(strategy_id, symbol, regime, side),
        FOREIGN KEY(strategy_id) REFERENCES strategies(id)
    );

    CREATE INDEX IF NOT EXISTS idx_adaptive_trade_memory_lookup
        ON adaptive_trade_memory(symbol, regime, strategy_id, side);

    CREATE TABLE IF NOT EXISTS adaptive_learning_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL,
        strategy_id INTEGER NOT NULL,
        symbol TEXT NOT NULL,
        regime TEXT NOT NULL,
        side INTEGER NOT NULL,
        reward_r REAL NOT NULL,
        confidence_before REAL NOT NULL,
        confidence_after REAL NOT NULL,
        consecutive_losses INTEGER NOT NULL,
        blocked_until TEXT,
        source TEXT NOT NULL,
        details_json TEXT NOT NULL,
        FOREIGN KEY(strategy_id) REFERENCES strategies(id)
    );

    CREATE INDEX IF NOT EXISTS idx_adaptive_learning_events_recent
        ON adaptive_learning_events(symbol, timestamp);

    CREATE TABLE IF NOT EXISTS collective_market_memory (
        symbol TEXT NOT NULL,
        family TEXT NOT NULL,
        regime TEXT NOT NULL,
        side INTEGER NOT NULL,
        effective_observations REAL NOT NULL DEFAULT 0,
        win_mass REAL NOT NULL DEFAULT 0,
        loss_mass REAL NOT NULL DEFAULT 0,
        reward_mean REAL NOT NULL DEFAULT 0,
        reward_ewma REAL NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(symbol, family, regime, side)
    );

    CREATE INDEX IF NOT EXISTS idx_collective_market_memory_lookup
        ON collective_market_memory(symbol, regime, family, side);

    CREATE TABLE IF NOT EXISTS session_family_memory (
        symbol TEXT NOT NULL,
        family TEXT NOT NULL,
        regime TEXT NOT NULL,
        session TEXT NOT NULL,
        side INTEGER NOT NULL,
        effective_observations REAL NOT NULL DEFAULT 0,
        win_mass REAL NOT NULL DEFAULT 0,
        loss_mass REAL NOT NULL DEFAULT 0,
        reward_mean REAL NOT NULL DEFAULT 0,
        reward_ewma REAL NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(symbol, family, regime, session, side)
    );

    CREATE INDEX IF NOT EXISTS idx_session_family_memory_lookup
        ON session_family_memory(symbol, session, regime, family, side);

    CREATE TABLE IF NOT EXISTS superlearner_model_state (
        model_key TEXT PRIMARY KEY,
        feature_names_json TEXT NOT NULL,
        weights_json TEXT NOT NULL,
        mean_json TEXT NOT NULL,
        m2_json TEXT NOT NULL,
        scaler_count INTEGER NOT NULL DEFAULT 0,
        updates INTEGER NOT NULL DEFAULT 0,
        bias REAL NOT NULL DEFAULT 0,
        last_probability REAL,
        last_logloss REAL,
        updated_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS superlearner_decisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL,
        symbol TEXT NOT NULL,
        strategy_id INTEGER,
        regime TEXT NOT NULL,
        side INTEGER NOT NULL,
        decision TEXT NOT NULL,
        reason TEXT NOT NULL,
        probability REAL NOT NULL,
        probability_active INTEGER NOT NULL DEFAULT 0,
        threshold REAL NOT NULL,
        bayes_loss_probability REAL NOT NULL,
        drift_score REAL NOT NULL,
        hurst REAL NOT NULL,
        entropy REAL NOT NULL,
        microstructure_score REAL NOT NULL,
        sl_multiplier REAL NOT NULL,
        tp_multiplier REAL NOT NULL,
        risk_multiplier REAL NOT NULL,
        latency_ms REAL NOT NULL DEFAULT 0,
        features_json TEXT NOT NULL,
        details_json TEXT NOT NULL,
        FOREIGN KEY(strategy_id) REFERENCES strategies(id)
    );

    CREATE INDEX IF NOT EXISTS idx_superlearner_decisions_recent
        ON superlearner_decisions(symbol, timestamp);

    CREATE TABLE IF NOT EXISTS shadow_positions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        strategy_id INTEGER NOT NULL,
        symbol TEXT NOT NULL,
        family TEXT NOT NULL,
        regime TEXT NOT NULL,
        side INTEGER NOT NULL,
        entry_price REAL NOT NULL,
        stop_loss REAL NOT NULL,
        take_profit REAL NOT NULL,
        opened_bar_time TEXT NOT NULL,
        opened_at TEXT NOT NULL,
        max_hold INTEGER NOT NULL,
        closed_at TEXT,
        exit_price REAL,
        reward_r REAL,
        status TEXT NOT NULL DEFAULT 'open',
        close_reason TEXT,
        FOREIGN KEY(strategy_id) REFERENCES strategies(id)
    );

    CREATE INDEX IF NOT EXISTS idx_shadow_positions_open
        ON shadow_positions(status, symbol, strategy_id);

    CREATE TABLE IF NOT EXISTS candidate_live_scores (
        strategy_id INTEGER PRIMARY KEY,
        symbol TEXT NOT NULL,
        observations INTEGER NOT NULL DEFAULT 0,
        wins INTEGER NOT NULL DEFAULT 0,
        losses INTEGER NOT NULL DEFAULT 0,
        gross_win_r REAL NOT NULL DEFAULT 0,
        gross_loss_r REAL NOT NULL DEFAULT 0,
        reward_mean REAL NOT NULL DEFAULT 0,
        cumulative_r REAL NOT NULL DEFAULT 0,
        peak_r REAL NOT NULL DEFAULT 0,
        max_drawdown_r REAL NOT NULL DEFAULT 0,
        promoted_at TEXT,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(strategy_id) REFERENCES strategies(id)
    );
    """
    with db_connect() as connection:
        connection.executescript(schema)
        gpt_memory.ensure_tables(connection)
        scalp_lab.ensure_tables(connection)
        capital_growth.ensure_tables(connection)
        micro_hunter.ensure_tables(connection)
        institutional_alpha.ensure_tables(connection)
        scalp_learning_v11.ensure_tables(connection)
        migrate_database_state(connection)
    repair_existing_strategies()
    _INITIALIZED_DATABASE_PATH = current_path
    print(f"Database ready: {settings.DATABASE_PATH}")


def migrate_database_state(connection: sqlite3.Connection) -> None:
    """Apply idempotent status migrations for the stricter approval pipeline."""
    previous_version_row = connection.execute(
        "SELECT value FROM system_state WHERE key='schema_pipeline_version'"
    ).fetchone()
    previous_version = str(previous_version_row["value"]) if previous_version_row else ""

    # Old packages used `validated` for historical-only approval. The parallel
    # machine requires a second live shadow gate before any strategy can be
    # considered executable. Previously shadow-promoted rows retain approval
    # only when their backtest metrics already contain walk-forward evidence.
    connection.execute(
        """
        UPDATE strategies
        SET status='shadow_approved'
        WHERE status='validated'
          AND id IN (
              SELECT strategy_id FROM candidate_live_scores
              WHERE promoted_at IS NOT NULL
          )
        """
    )
    connection.execute(
        "UPDATE strategies SET status='historical_validated' WHERE status='validated'"
    )

    # Historical passers created by older builds did not contain anchored
    # walk-forward folds. Queue them for strict revalidation instead of
    # allowing historical-only evidence into the shadow/execution pipeline.
    connection.execute(
        """
        UPDATE strategies
        SET status='needs_revalidation'
        WHERE status IN ('historical_validated', 'shadow_approved')
          AND NOT EXISTS (
              SELECT 1 FROM backtests b
              WHERE b.strategy_id=strategies.id
                AND b.metrics_json LIKE '%"walk_forward"%'
          )
        """
    )

    # One-time V5.1 approval reset: old live observations remain preserved in
    # rl_reward_events/strategy_scores for learning, but approval counters are
    # restarted so fresh shadow evidence follows the new walk-forward pass.
    if previous_version != "parallel-v5.1":
        connection.execute(
            """
            UPDATE candidate_live_scores
            SET observations=0, wins=0, losses=0, gross_win_r=0,
                gross_loss_r=0, reward_mean=0, cumulative_r=0, peak_r=0,
                max_drawdown_r=0, promoted_at=NULL, updated_at=?
            WHERE strategy_id IN (
                SELECT id FROM strategies WHERE status='needs_revalidation'
            )
            """,
            (utc_now(),),
        )
        connection.execute(
            """
            UPDATE shadow_positions
            SET status='cancelled', closed_at=?, close_reason='v5_revalidation_reset'
            WHERE status='open'
              AND strategy_id IN (
                  SELECT id FROM strategies WHERE status='needs_revalidation'
              )
            """,
            (utc_now(),),
        )

    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_strategy_scores_strategy "
        "ON strategy_scores(strategy_id, symbol, regime)"
    )

    # V5.4 adds execution-context columns without rebuilding demo_positions, so
    # every existing row/ticket/PnL record remains intact.
    ensure_table_column(connection, "demo_positions", "execution_tier", "TEXT")
    ensure_table_column(connection, "demo_positions", "setup_key", "TEXT")
    ensure_table_column(connection, "demo_positions", "context_json", "TEXT")
    ensure_table_column(connection, "demo_positions", "learning_confidence", "REAL")
    ensure_table_column(connection, "demo_positions", "risk_multiplier", "REAL")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_demo_positions_setup_open "
        "ON demo_positions(status, strategy_id, symbol, regime, side)"
    )

    # V5.5 SuperLearner adds only nullable/additive columns. Existing DEMO rows,
    # tickets, PnL and the historical data cache are never rebuilt or deleted.
    ensure_table_column(connection, "demo_positions", "mfe_r", "REAL")
    ensure_table_column(connection, "demo_positions", "mae_r", "REAL")
    ensure_table_column(connection, "demo_positions", "super_probability", "REAL")
    ensure_table_column(connection, "demo_positions", "super_decision_json", "TEXT")
    ensure_table_column(connection, "demo_positions", "sl_multiplier", "REAL")
    ensure_table_column(connection, "demo_positions", "tp_multiplier", "REAL")
    ensure_table_column(connection, "shadow_positions", "context_json", "TEXT")
    ensure_table_column(connection, "superlearner_decisions", "latency_ms", "REAL NOT NULL DEFAULT 0")

    # V6.9.1 normalizes virtual shadow SL/TP fills.  The 10-second shadow loop
    # can wake up after price has moved far beyond a stop/target (especially
    # after a restart or market gap).  Using the current quote as the virtual
    # fill created artificial outcomes such as -8R for a normal 1R stop.
    # Repair is shadow-only: real DEMO broker outcomes are never rewritten.
    repair_shadow_fill_integrity(connection)

    # Replay old closed DEMO positions into the new adaptive memory exactly once.
    # The original demo_positions and reward tables are never deleted/reset.
    bootstrap_adaptive_learning(connection)
    bootstrap_collective_memory(connection)
    bootstrap_session_memory(connection)
    bootstrap_superlearner_model(connection)
    state_set(connection, "schema_pipeline_version", "parallel-v5.1")
    state_set(connection, "adaptive_learning_version", "v5.4")
    state_set(connection, "superlearner_version", "v5.5")
    state_set(connection, "scalp_intelligence_version", "v6.0")
    connection.commit()


def ensure_table_column(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    declaration: str,
) -> None:
    columns = {str(row["name"]) for row in connection.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


def state_get(connection: sqlite3.Connection, key: str) -> str | None:
    row = connection.execute(
        "SELECT value FROM system_state WHERE key = ?", (key,)
    ).fetchone()
    return str(row["value"]) if row else None


def state_set(connection: sqlite3.Connection, key: str, value: Any) -> None:
    connection.execute(
        """
        INSERT INTO system_state(key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            value=excluded.value,
            updated_at=excluded.updated_at
        """,
        (key, str(value), utc_now()),
    )


def parse_utc_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def adaptive_setup_key(
    strategy_id: int, symbol: str, regime: str, side: int
) -> str:
    raw = f"{int(strategy_id)}|{symbol}|{regime}|{int(side)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def adaptive_confidence(
    observations: int,
    wins: int,
    reward_mean: float,
    reward_ewma: float,
    consecutive_losses: int,
) -> float:
    """Return a conservative 0..1 confidence score with a neutral prior.

    The score intentionally reacts quickly to repeated live losses, while a
    small prior prevents one result from permanently banning a setup.
    """
    n = max(0, int(observations))
    w = max(0, min(int(wins), n))
    smoothed_win_rate = (w + 2.0) / (n + 4.0)
    mean_component = math.tanh(safe_float(reward_mean) / 1.25)
    ewma_component = math.tanh(safe_float(reward_ewma))
    value = (
        0.50
        + 0.55 * (smoothed_win_rate - 0.50)
        + 0.13 * mean_component
        + 0.17 * ewma_component
        - 0.045 * min(max(0, int(consecutive_losses)), 4)
    )
    return clamp(value, 0.05, 0.95)


def _adaptive_snapshot_from_row(row: sqlite3.Row | None) -> dict[str, Any]:
    if row is None:
        return {
            "observations": 0, "wins": 0, "losses": 0,
            "consecutive_losses": 0, "reward_mean": 0.0,
            "reward_ewma": 0.0, "stored_confidence": 0.50,
            "confidence": 0.50, "last_reward": 0.0,
            "last_closed_at": None, "blocked_until": None,
            "blocked": False, "blocked_seconds": 0.0, "age_hours": None,
        }

    now = datetime.now(timezone.utc)
    last_closed = parse_utc_timestamp(row["last_closed_at"])
    age_hours: float | None = None
    if last_closed is not None:
        age_hours = max(0.0, (now - last_closed).total_seconds() / 3600.0)

    stored_confidence = clamp(safe_float(row["confidence"], 0.50), 0.05, 0.95)
    half_life = max(0.25, safe_float(
        getattr(settings, "LEARNING_MEMORY_HALF_LIFE_HOURS", 18.0), 18.0
    ))
    decay = 1.0 if age_hours is None else 0.5 ** (age_hours / half_life)
    effective_confidence = 0.50 + (stored_confidence - 0.50) * decay

    streak = int(row["consecutive_losses"] or 0)
    reset_hours = max(0.0, safe_float(
        getattr(settings, "LEARNING_STREAK_RESET_HOURS", 4.0), 4.0
    ))
    if age_hours is not None and age_hours >= reset_hours:
        streak = 0

    blocked_until_dt = parse_utc_timestamp(row["blocked_until"])
    blocked_seconds = (
        max(0.0, (blocked_until_dt - now).total_seconds())
        if blocked_until_dt is not None else 0.0
    )
    return {
        "observations": int(row["observations"] or 0),
        "wins": int(row["wins"] or 0),
        "losses": int(row["losses"] or 0),
        "consecutive_losses": streak,
        "stored_consecutive_losses": int(row["consecutive_losses"] or 0),
        "reward_mean": safe_float(row["reward_mean"]),
        "reward_ewma": safe_float(row["reward_ewma"]),
        "stored_confidence": stored_confidence,
        "confidence": clamp(effective_confidence, 0.05, 0.95),
        "last_reward": safe_float(row["last_reward"]),
        "last_closed_at": str(row["last_closed_at"]) if row["last_closed_at"] else None,
        "blocked_until": str(row["blocked_until"]) if row["blocked_until"] else None,
        "blocked": blocked_seconds > 0.0,
        "blocked_seconds": blocked_seconds,
        "age_hours": age_hours,
    }


def adaptive_learning_snapshot(
    connection: sqlite3.Connection,
    strategy_id: int,
    symbol: str,
    regime: str,
    side: int,
) -> dict[str, Any]:
    if not bool(getattr(settings, "ENABLE_ADAPTIVE_EXECUTION_LEARNING", True)):
        return _adaptive_snapshot_from_row(None)
    row = connection.execute(
        """
        SELECT * FROM adaptive_trade_memory
        WHERE strategy_id=? AND symbol=? AND regime=? AND side=?
        """,
        (int(strategy_id), symbol, regime, int(side)),
    ).fetchone()
    return _adaptive_snapshot_from_row(row)


def update_adaptive_learning(
    connection: sqlite3.Connection,
    strategy_id: int,
    symbol: str,
    regime: str,
    side: int,
    reward_r: float,
    source: str,
    details: dict[str, Any] | None = None,
    closed_at: str | None = None,
    write_event: bool = True,
) -> dict[str, Any]:
    """Persist one live outcome into direction/regime-specific memory."""
    reward = safe_float(reward_r)
    old = connection.execute(
        """
        SELECT * FROM adaptive_trade_memory
        WHERE strategy_id=? AND symbol=? AND regime=? AND side=?
        """,
        (int(strategy_id), symbol, regime, int(side)),
    ).fetchone()
    before = _adaptive_snapshot_from_row(old)
    old_n = int(old["observations"] or 0) if old else 0
    observations = old_n + 1
    wins = (int(old["wins"] or 0) if old else 0) + (1 if reward > 0 else 0)
    losses = (int(old["losses"] or 0) if old else 0) + (1 if reward <= 0 else 0)
    consecutive_losses = (
        0 if reward > 0
        else (int(old["consecutive_losses"] or 0) if old else 0) + 1
    )
    old_mean = safe_float(old["reward_mean"]) if old else 0.0
    reward_mean = old_mean + (reward - old_mean) / observations
    alpha = clamp(safe_float(getattr(settings, "LEARNING_EWMA_ALPHA", 0.35)), 0.05, 1.0)
    reward_ewma = reward if old_n == 0 else (
        alpha * reward + (1.0 - alpha) * safe_float(old["reward_ewma"])
    )
    gross_win_r = (safe_float(old["gross_win_r"]) if old else 0.0) + max(0.0, reward)
    gross_loss_r = (safe_float(old["gross_loss_r"]) if old else 0.0) + abs(min(0.0, reward))
    confidence = adaptive_confidence(
        observations, wins, reward_mean, reward_ewma, consecutive_losses
    )

    close_dt = parse_utc_timestamp(closed_at) or datetime.now(timezone.utc)
    close_text = close_dt.isoformat()
    last_loss_at: str | None = str(old["last_loss_at"]) if old and old["last_loss_at"] else None
    blocked_until: str | None = None
    if reward <= 0:
        last_loss_at = close_text
        base = max(0, int(getattr(settings, "LEARNING_LOSS_COOLDOWN_BASE_SECONDS", 180)))
        maximum = max(base, int(getattr(settings, "LEARNING_LOSS_COOLDOWN_MAX_SECONDS", 7200)))
        cooldown = min(maximum, base * (2 ** max(0, consecutive_losses - 1)))
        blocked_until = (close_dt + timedelta(seconds=cooldown)).isoformat()

    connection.execute(
        """
        INSERT INTO adaptive_trade_memory(
            strategy_id, symbol, regime, side, observations, wins, losses,
            consecutive_losses, gross_win_r, gross_loss_r, reward_mean,
            reward_ewma, confidence, last_reward, last_closed_at, last_loss_at,
            blocked_until, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(strategy_id, symbol, regime, side) DO UPDATE SET
            observations=excluded.observations,
            wins=excluded.wins, losses=excluded.losses,
            consecutive_losses=excluded.consecutive_losses,
            gross_win_r=excluded.gross_win_r, gross_loss_r=excluded.gross_loss_r,
            reward_mean=excluded.reward_mean, reward_ewma=excluded.reward_ewma,
            confidence=excluded.confidence, last_reward=excluded.last_reward,
            last_closed_at=excluded.last_closed_at, last_loss_at=excluded.last_loss_at,
            blocked_until=excluded.blocked_until, updated_at=excluded.updated_at
        """,
        (
            int(strategy_id), symbol, regime, int(side), observations, wins, losses,
            consecutive_losses, gross_win_r, gross_loss_r, reward_mean, reward_ewma,
            confidence, reward, close_text, last_loss_at, blocked_until, utc_now(),
        ),
    )
    if write_event:
        connection.execute(
            """
            INSERT INTO adaptive_learning_events(
                timestamp, strategy_id, symbol, regime, side, reward_r,
                confidence_before, confidence_after, consecutive_losses,
                blocked_until, source, details_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                utc_now(), int(strategy_id), symbol, regime, int(side), reward,
                safe_float(before["confidence"], 0.50), confidence, consecutive_losses,
                blocked_until, source, json_text(details or {}),
            ),
        )
    return adaptive_learning_snapshot(
        connection, int(strategy_id), symbol, regime, int(side)
    )


def bootstrap_adaptive_learning(connection: sqlite3.Connection) -> None:
    marker = state_get(connection, "adaptive_learning_bootstrap")
    if marker == "demo_positions_v5.4":
        return
    existing = int(connection.execute(
        "SELECT COUNT(*) AS n FROM adaptive_trade_memory"
    ).fetchone()["n"])
    if existing == 0:
        rows = connection.execute(
            """
            SELECT id, strategy_id, symbol, regime, side, reward_r, pnl,
                   risk_cash, opened_at, closed_at
            FROM demo_positions
            WHERE status='closed' AND reward_r IS NOT NULL
            ORDER BY COALESCE(closed_at, opened_at), id
            """
        ).fetchall()
        for row in rows:
            update_adaptive_learning(
                connection,
                int(row["strategy_id"]),
                str(row["symbol"]),
                str(row["regime"]),
                int(row["side"]),
                safe_float(row["reward_r"]),
                "bootstrap_existing_demo",
                {
                    "demo_position_id": int(row["id"]),
                    "pnl": safe_float(row["pnl"]),
                    "risk_cash": safe_float(row["risk_cash"]),
                    "preserved_existing_row": True,
                },
                closed_at=str(row["closed_at"]) if row["closed_at"] else None,
                write_event=False,
            )
    state_set(connection, "adaptive_learning_bootstrap", "demo_positions_v5.4")


def adaptive_candidate_adjustment(
    connection: sqlite3.Connection,
    strategy_id: int,
    symbol: str,
    regime: str,
) -> tuple[float, dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT * FROM adaptive_trade_memory
        WHERE strategy_id=? AND symbol=? AND regime=?
        """,
        (int(strategy_id), symbol, regime),
    ).fetchall()
    if not rows:
        return 0.0, {"observations": 0, "confidence": 0.50, "loss_streak": 0}
    snapshots = [_adaptive_snapshot_from_row(row) for row in rows]
    total_n = sum(max(0, int(item["observations"])) for item in snapshots)
    if total_n <= 0:
        return 0.0, {"observations": 0, "confidence": 0.50, "loss_streak": 0}
    confidence = sum(
        safe_float(item["confidence"], 0.50) * int(item["observations"])
        for item in snapshots
    ) / total_n
    streak = max(int(item["consecutive_losses"]) for item in snapshots)
    weight = safe_float(getattr(settings, "LEARNING_CANDIDATE_SCORE_WEIGHT", 10.0), 10.0)
    loss_penalty = safe_float(
        getattr(settings, "LEARNING_CONSECUTIVE_LOSS_SCORE_PENALTY", 1.75), 1.75
    )
    adjustment = (confidence - 0.50) * weight - min(streak, 4) * loss_penalty
    return adjustment, {
        "observations": total_n,
        "confidence": confidence,
        "loss_streak": streak,
    }


def adaptive_risk_multiplier(
    snapshot: dict[str, Any], execution_tier: str
) -> float:
    if not bool(getattr(settings, "ENABLE_ADAPTIVE_EXECUTION_LEARNING", True)):
        return 1.0
    confidence = safe_float(snapshot.get("confidence"), 0.50)
    observations = int(snapshot.get("observations") or 0)
    wins = int(snapshot.get("wins") or 0)
    losses = int(snapshot.get("losses") or 0)
    streak = int(snapshot.get("consecutive_losses") or 0)
    minimum = clamp(
        safe_float(getattr(settings, "LEARNING_RISK_MIN_MULTIPLIER", 0.50), 0.50),
        0.10, 1.0,
    )
    caution = safe_float(getattr(settings, "LEARNING_CAUTION_CONFIDENCE", 0.42), 0.42)
    if streak >= 1 or confidence < caution:
        return minimum
    if confidence < 0.50:
        return max(minimum, 0.75)

    min_n = max(1, int(getattr(settings, "LEARNING_RISK_UPSCALE_MIN_OBSERVATIONS", 8)))
    min_conf = clamp(
        safe_float(getattr(settings, "LEARNING_RISK_UPSCALE_MIN_CONFIDENCE", 0.62), 0.62),
        0.50, 0.90,
    )
    if (
        observations < min_n
        or confidence < min_conf
        or safe_float(snapshot.get("reward_mean")) <= 0
        or wins <= losses
    ):
        return 1.0
    if execution_tier == "v8_native_alpha":
        # Native alpha probes are discovery trades, never an excuse to upscale.
        return 1.0
    maximum = (
        safe_float(getattr(settings, "LEARNING_RISK_MAX_APPROVED_MULTIPLIER", 3.0), 3.0)
        if execution_tier == "shadow_approved"
        else safe_float(getattr(settings, "LEARNING_RISK_MAX_TRIAL_MULTIPLIER", 2.0), 2.0)
    )
    maximum = max(1.0, maximum)
    strength = clamp((confidence - min_conf) / max(0.05, 0.90 - min_conf), 0.0, 1.0)
    return 1.0 + strength * (maximum - 1.0)


def adaptive_learning_gate(
    connection: sqlite3.Connection,
    strategy: StrategyDefinition,
    regime: str,
    side: int,
    execution_tier: str,
    signal_details: dict[str, Any],
) -> tuple[bool, str, dict[str, Any]]:
    snapshot = adaptive_learning_snapshot(
        connection, strategy.strategy_id, strategy.symbol, regime, side
    )
    rolling = rolling_setup_performance(
        connection, strategy.strategy_id, strategy.symbol, regime, side,
        max(5, int(getattr(settings, "SUPERLEARNER_ROLLING_WINDOW", 20))),
    )
    bayes_loss = bayesian_loss_probability(snapshot, rolling)
    gate: dict[str, Any] = {
        "setup_key": adaptive_setup_key(strategy.strategy_id, strategy.symbol, regime, side),
        "snapshot": snapshot,
        "rolling": rolling,
        "bayes_loss_probability": bayes_loss,
        "risk_multiplier": 1.0,
    }
    if not bool(getattr(settings, "ENABLE_ADAPTIVE_EXECUTION_LEARNING", True)):
        return True, "Adaptive learning disabled", gate

    if bool(getattr(settings, "LEARNING_DUPLICATE_SETUP_BLOCK", True)):
        duplicate = connection.execute(
            """
            SELECT id FROM demo_positions
            WHERE status='open' AND strategy_id=? AND symbol=? AND regime=? AND side=?
            LIMIT 1
            """,
            (strategy.strategy_id, strategy.symbol, regime, int(side)),
        ).fetchone()
        if duplicate:
            gate["duplicate_demo_position_id"] = int(duplicate["id"])
            return False, "Learning gate: identical setup already open", gate

    if bool(snapshot.get("blocked")):
        return False, (
            "Learning gate: recent loss cooldown "
            f"({safe_float(snapshot.get('blocked_seconds')):.0f}s remaining)"
        ), gate

    # V5.5 Bayesian cooldown reacts to the estimated probability of another loss.
    # It never permanently bans a setup; a later win moves the posterior back.
    observations = int(snapshot.get("observations") or 0)
    last_loss = parse_utc_timestamp(snapshot.get("last_closed_at"))
    if observations >= max(3, int(getattr(settings, "BAYESIAN_COOLDOWN_MIN_OBSERVATIONS", 4))):
        warn_p = safe_float(getattr(settings, "BAYESIAN_COOLDOWN_START_PROB", 0.55), 0.55)
        heavy_p = safe_float(getattr(settings, "BAYESIAN_HEAVY_QUARANTINE_PROB", 0.70), 0.70)
        if safe_float(snapshot.get("last_reward")) <= 0 and bayes_loss >= warn_p and last_loss is not None:
            base = max(60, int(getattr(settings, "LEARNING_LOSS_COOLDOWN_BASE_SECONDS", 180)))
            dynamic_seconds = int(base * max(1.0, bayes_loss / 0.50))
            if bayes_loss >= heavy_p:
                dynamic_seconds = max(
                    dynamic_seconds,
                    int(getattr(settings, "BAYESIAN_HEAVY_QUARANTINE_SECONDS", 3600)),
                )
            dynamic_seconds = min(
                dynamic_seconds,
                int(getattr(settings, "LEARNING_LOSS_COOLDOWN_MAX_SECONDS", 7200)),
            )
            elapsed = max(0.0, (datetime.now(timezone.utc) - last_loss).total_seconds())
            remaining = max(0.0, dynamic_seconds - elapsed)
            gate["bayesian_cooldown_seconds"] = dynamic_seconds
            gate["bayesian_cooldown_remaining"] = remaining
            if remaining > 0:
                return False, (
                    "Learning gate: Bayesian loss-risk cooldown "
                    f"(P(loss)={bayes_loss:.2f}, {remaining:.0f}s remaining)"
                ), gate

    confidence = safe_float(snapshot.get("confidence"), 0.50)
    min_obs = max(1, int(getattr(settings, "LEARNING_HARD_BLOCK_MIN_OBSERVATIONS", 3)))
    hard_conf = safe_float(getattr(settings, "LEARNING_HARD_BLOCK_CONFIDENCE", 0.22), 0.22)
    if observations >= min_obs and confidence < hard_conf:
        return False, (
            f"Learning gate: confidence too low ({confidence:.2f}) after "
            f"{observations} live outcomes"
        ), gate

    # V7: sparse strategy IDs now share actual execution experience through a
    # hierarchy (strategy -> family/regime/side -> family/side). A repeatedly
    # losing direction is therefore stopped even when a new strategy ID has
    # little individual history.
    v7_perf = v7_execution_performance(connection, strategy, regime, side)
    gate["v7_execution_performance"] = v7_perf
    negative_reason = v7_negative_edge_reason(v7_perf)
    if negative_reason:
        micro_ctx = signal_details.get("micro_hunter") if isinstance(signal_details.get("micro_hunter"), dict) else {}
        alpha_score = safe_float(micro_ctx.get("score"), 0.0)
        v8_thresholds = institutional_alpha.dynamic_thresholds(connection, strategy.symbol) if execution_tier == "v8_native_alpha" else {}
        exploration_floor = safe_float(v8_thresholds.get("trigger"), 100.0) + safe_float(getattr(settings, "V8_NEGATIVE_EDGE_EXPLORATION_SCORE_BONUS", 4.0), 4.0)
        if execution_tier == "v8_native_alpha" and alpha_score >= exploration_floor:
            # Institutional exploration/exploitation lane: old family-level losses
            # may not permanently suppress a *new* live playbook. Allow only an
            # exceptional alpha score and de-risk heavily downstream.
            gate["v8_negative_edge_exploration"] = {
                "legacy_reason": negative_reason, "alpha_score": alpha_score,
                "required_score": exploration_floor,
            }
            gate["risk_multiplier"] = min(0.50, safe_float(gate.get("risk_multiplier"), 1.0))
        else:
            return False, f"V7 learning gate: {negative_reason}", gate

    streak = int(snapshot.get("consecutive_losses") or 0)
    confirm_after = max(1, int(
        getattr(settings, "LEARNING_RECOVERY_CONFIRM_AFTER_LOSSES", 2)
    ))
    if streak >= confirm_after:
        micro_ctx = signal_details.get("micro_hunter") if isinstance(signal_details.get("micro_hunter"), dict) else {}
        micro_score = safe_float(micro_ctx.get("score"), 0.0)
        if micro_ctx and execution_tier == "v8_native_alpha":
            thresholds = institutional_alpha.dynamic_thresholds(connection, strategy.symbol)
            recovery_floor = safe_float(thresholds.get("trigger"), 100.0) + safe_float(
                getattr(settings, "V8_MICRO_RECOVERY_SCORE_BONUS", 6.0), 6.0
            )
            gate["recovery_confirmation"] = {
                "mode": "micro_alpha", "score": micro_score, "required_score": recovery_floor,
                "loss_streak": streak,
            }
            if micro_score < recovery_floor:
                return False, (
                    "Learning gate: repeated-loss micro setup needs stronger live alpha "
                    f"(score={micro_score:.1f}/{recovery_floor:.1f})"
                ), gate
            # A high-quality fresh micro trigger may collect evidence at reduced risk;
            # it does not erase the loss memory and can never upscale risk.
            gate["risk_multiplier"] = min(
                safe_float(gate.get("risk_multiplier"), 1.0),
                safe_float(getattr(settings, "V8_MICRO_RECOVERY_RISK_MULTIPLIER", 0.35), 0.35),
            )
        else:
            if side == 1:
                own_weight = safe_float(signal_details.get("buy_weight"))
                other_weight = safe_float(signal_details.get("sell_weight"))
                votes = int(signal_details.get("buy_votes") or 0)
            else:
                own_weight = safe_float(signal_details.get("sell_weight"))
                other_weight = safe_float(signal_details.get("buy_weight"))
                votes = int(signal_details.get("sell_votes") or 0)
            dominance = own_weight / max(0.10, other_weight)
            required_ratio = safe_float(
                getattr(settings, "LEARNING_RECOVERY_CONSENSUS_RATIO", 1.60), 1.60
            )
            required_votes = max(2, int(getattr(settings, "ENSEMBLE_MIN_AGREE", 2)))
            gate["recovery_confirmation"] = {
                "votes": votes, "dominance": dominance,
                "required_votes": required_votes, "required_ratio": required_ratio,
            }
            if votes < required_votes or dominance < required_ratio:
                return False, (
                    "Learning gate: repeated-loss setup needs stronger confirmation "
                    f"(votes={votes}/{required_votes}, ratio={dominance:.2f}/{required_ratio:.2f})"
                ), gate

    gate["risk_multiplier"] = adaptive_risk_multiplier(snapshot, execution_tier)
    return True, "Learning gate passed", gate


def learning_entry_context(
    row: pd.Series,
    atr_value: float,
    spread: float,
    dom: float | None,
    signal_details: dict[str, Any],
    execution_tier: str,
    risk_multiplier: float,
) -> dict[str, Any]:
    atr_value = max(0.0, safe_float(atr_value))
    ema20 = safe_float(row.get("ema_20"))
    ema50 = safe_float(row.get("ema_50"))
    return {
        "execution_tier": execution_tier,
        "rsi_5": safe_float(row.get("rsi_5")),
        "rsi_7": safe_float(row.get("rsi_7")),
        "rsi_14": safe_float(row.get("rsi_14")),
        "atr_14": atr_value,
        "atr_ratio": safe_float(row.get("atr_ratio")),
        "volatility_30": safe_float(row.get("volatility_30")),
        "ema_20": ema20,
        "ema_50": ema50,
        "trend_strength_atr": abs(ema20 - ema50) / atr_value if atr_value > 0 else 0.0,
        "spread": safe_float(spread),
        "spread_atr_fraction": safe_float(spread) / atr_value if atr_value > 0 else 0.0,
        "dom_imbalance": dom,
        "buy_votes": int(signal_details.get("buy_votes") or 0),
        "sell_votes": int(signal_details.get("sell_votes") or 0),
        "buy_weight": safe_float(signal_details.get("buy_weight")),
        "sell_weight": safe_float(signal_details.get("sell_weight")),
        "trial_signal_mode": signal_details.get("trial_signal_mode"),
        "risk_multiplier": safe_float(risk_multiplier, 1.0),
    }


# ============================================================
# V5.5 SuperLearner: proactive prediction + collective memory
# ============================================================

SUPER_FEATURE_NAMES = (
    "side", "rsi14", "atr_ratio", "volatility", "trend_strength",
    "spread_atr", "dom_imbalance", "dom_persistence", "dom_change",
    "microstructure", "hurst", "entropy", "drift", "ensemble_dominance",
    "vote_balance", "adaptive_confidence", "bayes_loss", "collective_win_prob",
    "collective_reward", "hour_sin", "hour_cos", "regime_trend",
    "regime_range", "regime_high_vol", "family_super_scalp", "family_scalp",
    "family_trend", "family_mean_reversion", "family_breakout",
)

_DOM_HISTORY: dict[str, deque[dict[str, float]]] = {}


def rolling_setup_performance(
    connection: sqlite3.Connection,
    strategy_id: int,
    symbol: str,
    regime: str,
    side: int,
    window: int = 20,
) -> dict[str, Any]:
    window = max(1, min(int(window), 200))
    rows = connection.execute(
        """
        SELECT reward_r FROM adaptive_learning_events
        WHERE strategy_id=? AND symbol=? AND regime=? AND side=?
        ORDER BY id DESC LIMIT ?
        """,
        (int(strategy_id), symbol, regime, int(side), window),
    ).fetchall()
    rewards = [safe_float(row["reward_r"]) for row in rows]
    n = len(rewards)
    wins = sum(1 for value in rewards if value > 0)
    losses = n - wins
    return {
        "observations": n,
        "wins": wins,
        "losses": losses,
        "win_rate": wins / n if n else 0.50,
        "reward_mean": sum(rewards) / n if n else 0.0,
    }


def _reward_metrics(rewards: list[float]) -> dict[str, float]:
    n = len(rewards)
    wins = sum(1 for value in rewards if value > 0)
    gross_win = sum(max(0.0, value) for value in rewards)
    gross_loss = sum(abs(min(0.0, value)) for value in rewards)
    pf = gross_win / gross_loss if gross_loss > 0 else (5.0 if gross_win > 0 else 0.0)
    return {
        "observations": float(n),
        "wins": float(wins),
        "win_rate": wins / n if n else 0.50,
        "reward_mean": sum(rewards) / n if n else 0.0,
        "profit_factor": pf,
    }


def v7_execution_performance(
    connection: sqlite3.Connection,
    strategy: StrategyDefinition,
    regime: str,
    side: int,
) -> dict[str, dict[str, float]]:
    """Hierarchical performance memory from actual closed broker-DEMO trades."""
    window = max(10, int(getattr(settings, "V7_PERFORMANCE_WINDOW", 80)))

    def recent_rewards(sql: str, params: tuple[Any, ...]) -> list[float]:
        rows = connection.execute(sql, (*params, window)).fetchall()
        return [safe_float(row["reward_r"]) for row in rows]

    strategy_rewards = recent_rewards(
        """SELECT reward_r FROM demo_positions
           WHERE status='closed' AND reward_r IS NOT NULL
             AND strategy_id=? AND side=?
           ORDER BY id DESC LIMIT ?""",
        (int(strategy.strategy_id), int(side)),
    )
    regime_family_rewards = recent_rewards(
        """SELECT d.reward_r FROM demo_positions d
           JOIN strategies s ON s.id=d.strategy_id
           WHERE d.status='closed' AND d.reward_r IS NOT NULL
             AND d.symbol=? AND s.family=? AND d.regime=? AND d.side=?
           ORDER BY d.id DESC LIMIT ?""",
        (strategy.symbol, strategy.family, regime, int(side)),
    )
    family_rewards = recent_rewards(
        """SELECT d.reward_r FROM demo_positions d
           JOIN strategies s ON s.id=d.strategy_id
           WHERE d.status='closed' AND d.reward_r IS NOT NULL
             AND d.symbol=? AND s.family=? AND d.side=?
           ORDER BY d.id DESC LIMIT ?""",
        (strategy.symbol, strategy.family, int(side)),
    )
    return {
        "strategy_side": _reward_metrics(strategy_rewards),
        "regime_family_side": _reward_metrics(regime_family_rewards),
        "family_side": _reward_metrics(family_rewards),
    }


def v7_negative_edge_reason(performance: dict[str, dict[str, float]]) -> str | None:
    checks = (
        ("strategy_side", int(getattr(settings, "V7_STRATEGY_BLOCK_MIN_TRADES", 8)),
         safe_float(getattr(settings, "V7_STRATEGY_BLOCK_MAX_MEAN_R", -0.12), -0.12),
         safe_float(getattr(settings, "V7_STRATEGY_BLOCK_MAX_PROFIT_FACTOR", 0.82), 0.82)),
        ("regime_family_side", int(getattr(settings, "V7_REGIME_FAMILY_SIDE_BLOCK_MIN_TRADES", 10)),
         safe_float(getattr(settings, "V7_REGIME_FAMILY_SIDE_BLOCK_MAX_MEAN_R", -0.10), -0.10),
         safe_float(getattr(settings, "V7_REGIME_FAMILY_SIDE_BLOCK_MAX_PROFIT_FACTOR", 0.85), 0.85)),
        ("family_side", int(getattr(settings, "V7_FAMILY_SIDE_BLOCK_MIN_TRADES", 20)),
         safe_float(getattr(settings, "V7_FAMILY_SIDE_BLOCK_MAX_MEAN_R", -0.08), -0.08),
         safe_float(getattr(settings, "V7_FAMILY_SIDE_BLOCK_MAX_PROFIT_FACTOR", 0.90), 0.90)),
    )
    for label, min_n, max_mean, max_pf in checks:
        item = performance.get(label, {})
        n = int(safe_float(item.get("observations")))
        mean_r = safe_float(item.get("reward_mean"))
        pf = safe_float(item.get("profit_factor"))
        if n >= min_n and mean_r <= max_mean and pf <= max_pf:
            return f"{label} negative edge: n={n}, mean={mean_r:.2f}R, PF={pf:.2f}"
    return None


def bayesian_loss_probability(
    snapshot: dict[str, Any], rolling: dict[str, Any] | None = None
) -> float:
    """Beta-Bernoulli posterior with recent outcomes receiving extra weight."""
    prior_loss = max(0.01, safe_float(getattr(settings, "BAYESIAN_PRIOR_LOSS", 1.0), 1.0))
    prior_win = max(0.01, safe_float(getattr(settings, "BAYESIAN_PRIOR_WIN", 1.0), 1.0))
    losses = safe_float(snapshot.get("losses"))
    wins = safe_float(snapshot.get("wins"))
    recent_weight = max(0.0, safe_float(getattr(settings, "BAYESIAN_RECENT_WEIGHT", 1.0), 1.0))
    if rolling:
        losses += recent_weight * safe_float(rolling.get("losses"))
        wins += recent_weight * safe_float(rolling.get("wins"))
    probability = (losses + prior_loss) / max(1e-9, losses + wins + prior_loss + prior_win)
    return clamp(probability, 0.02, 0.98)


def update_collective_memory(
    connection: sqlite3.Connection,
    symbol: str,
    family: str,
    regime: str,
    side: int,
    reward_r: float,
    source_weight: float = 1.0,
) -> None:
    raw_weight = max(0.0, safe_float(source_weight, 1.0))
    if raw_weight <= 0.0:
        return
    weight = clamp(raw_weight, 0.0, 1.0)
    reward = safe_float(reward_r)
    row = connection.execute(
        """
        SELECT * FROM collective_market_memory
        WHERE symbol=? AND family=? AND regime=? AND side=?
        """,
        (symbol, family, regime, int(side)),
    ).fetchone()
    old_n = safe_float(row["effective_observations"]) if row else 0.0
    old_mean = safe_float(row["reward_mean"]) if row else 0.0
    old_ewma = safe_float(row["reward_ewma"]) if row else 0.0
    new_n = old_n + weight
    reward_mean = old_mean + weight * (reward - old_mean) / max(new_n, 1e-9)
    alpha = clamp(safe_float(getattr(settings, "COLLECTIVE_EWMA_ALPHA", 0.15), 0.15), 0.02, 1.0)
    effective_alpha = clamp(alpha * weight, 0.01, 1.0)
    reward_ewma = reward if old_n <= 0 else effective_alpha * reward + (1.0 - effective_alpha) * old_ewma
    win_mass = (safe_float(row["win_mass"]) if row else 0.0) + (weight if reward > 0 else 0.0)
    loss_mass = (safe_float(row["loss_mass"]) if row else 0.0) + (weight if reward <= 0 else 0.0)
    connection.execute(
        """
        INSERT INTO collective_market_memory(
            symbol, family, regime, side, effective_observations,
            win_mass, loss_mass, reward_mean, reward_ewma, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(symbol, family, regime, side) DO UPDATE SET
            effective_observations=excluded.effective_observations,
            win_mass=excluded.win_mass, loss_mass=excluded.loss_mass,
            reward_mean=excluded.reward_mean, reward_ewma=excluded.reward_ewma,
            updated_at=excluded.updated_at
        """,
        (
            symbol, family, regime, int(side), new_n, win_mass, loss_mass,
            reward_mean, reward_ewma, utc_now(),
        ),
    )


def collective_memory_snapshot(
    connection: sqlite3.Connection,
    symbol: str,
    family: str,
    regime: str,
    side: int,
) -> dict[str, float]:
    row = connection.execute(
        """
        SELECT * FROM collective_market_memory
        WHERE symbol=? AND family=? AND regime=? AND side=?
        """,
        (symbol, family, regime, int(side)),
    ).fetchone()
    if not row:
        return {"observations": 0.0, "win_probability": 0.50, "reward_mean": 0.0, "reward_ewma": 0.0}
    wins = safe_float(row["win_mass"])
    losses = safe_float(row["loss_mass"])
    win_probability = (wins + 2.0) / max(1e-9, wins + losses + 4.0)
    return {
        "observations": safe_float(row["effective_observations"]),
        "win_probability": clamp(win_probability, 0.05, 0.95),
        "reward_mean": safe_float(row["reward_mean"]),
        "reward_ewma": safe_float(row["reward_ewma"]),
    }



def market_session_tag(symbol: str, timestamp: Any | None = None) -> str:
    """Compact UTC session context for per-symbol scalping memory.

    This is a market-hours context label, not a news-calendar feed. Crypto is
    24/7, while metals/energy receive Asia/London/New-York style buckets.
    """
    if timestamp is None:
        dt = datetime.now(timezone.utc)
    elif isinstance(timestamp, datetime):
        dt = timestamp if timestamp.tzinfo else timestamp.replace(tzinfo=timezone.utc)
        dt = dt.astimezone(timezone.utc)
    else:
        try:
            parsed = pd.Timestamp(timestamp)
            if parsed.tzinfo is None:
                parsed = parsed.tz_localize("UTC")
            dt = parsed.tz_convert("UTC").to_pydatetime()
        except Exception:
            dt = datetime.now(timezone.utc)
    hour = dt.hour + dt.minute / 60.0
    symbol_upper = str(symbol).upper()
    if "BTC" in symbol_upper or "ETH" in symbol_upper or "CRYPTO" in symbol_upper:
        if 0 <= hour < 7:
            return "crypto_asia"
        if 7 <= hour < 13:
            return "crypto_europe"
        if 13 <= hour < 21:
            return "crypto_us"
        return "crypto_transition"
    if 0 <= hour < 6:
        return "asia"
    if 6 <= hour < 12:
        return "london"
    if 12 <= hour < 16:
        return "london_newyork_overlap"
    if 16 <= hour < 21:
        return "new_york"
    return "offhours"


def update_session_family_memory(
    connection: sqlite3.Connection,
    symbol: str,
    family: str,
    regime: str,
    session: str,
    side: int,
    reward_r: float,
    source_weight: float = 1.0,
) -> None:
    raw_weight = max(0.0, safe_float(source_weight, 1.0))
    if raw_weight <= 0.0:
        return
    weight = clamp(raw_weight, 0.0, 1.0)
    reward = safe_float(reward_r)
    row = connection.execute(
        """
        SELECT * FROM session_family_memory
        WHERE symbol=? AND family=? AND regime=? AND session=? AND side=?
        """,
        (symbol, family, regime, session, int(side)),
    ).fetchone()
    old_n = safe_float(row["effective_observations"]) if row else 0.0
    old_mean = safe_float(row["reward_mean"]) if row else 0.0
    old_ewma = safe_float(row["reward_ewma"]) if row else 0.0
    new_n = old_n + weight
    reward_mean = old_mean + weight * (reward - old_mean) / max(new_n, 1e-9)
    alpha = clamp(
        safe_float(getattr(settings, "SESSION_MEMORY_EWMA_ALPHA", 0.18), 0.18),
        0.02, 1.0,
    )
    effective_alpha = clamp(alpha * weight, 0.01, 1.0)
    reward_ewma = reward if old_n <= 0 else (
        effective_alpha * reward + (1.0 - effective_alpha) * old_ewma
    )
    win_mass = (safe_float(row["win_mass"]) if row else 0.0) + (weight if reward > 0 else 0.0)
    loss_mass = (safe_float(row["loss_mass"]) if row else 0.0) + (weight if reward <= 0 else 0.0)
    connection.execute(
        """
        INSERT INTO session_family_memory(
            symbol, family, regime, session, side, effective_observations,
            win_mass, loss_mass, reward_mean, reward_ewma, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(symbol, family, regime, session, side) DO UPDATE SET
            effective_observations=excluded.effective_observations,
            win_mass=excluded.win_mass, loss_mass=excluded.loss_mass,
            reward_mean=excluded.reward_mean, reward_ewma=excluded.reward_ewma,
            updated_at=excluded.updated_at
        """,
        (
            symbol, family, regime, session, int(side), new_n,
            win_mass, loss_mass, reward_mean, reward_ewma, utc_now(),
        ),
    )


def session_family_memory_snapshot(
    connection: sqlite3.Connection,
    symbol: str,
    family: str,
    regime: str,
    session: str,
    side: int,
) -> dict[str, float]:
    row = connection.execute(
        """
        SELECT * FROM session_family_memory
        WHERE symbol=? AND family=? AND regime=? AND session=? AND side=?
        """,
        (symbol, family, regime, session, int(side)),
    ).fetchone()
    if not row:
        return {
            "observations": 0.0, "win_probability": 0.50,
            "reward_mean": 0.0, "reward_ewma": 0.0,
        }
    wins = safe_float(row["win_mass"])
    losses = safe_float(row["loss_mass"])
    probability = (wins + 2.0) / max(1e-9, wins + losses + 4.0)
    return {
        "observations": safe_float(row["effective_observations"]),
        "win_probability": clamp(probability, 0.05, 0.95),
        "reward_mean": safe_float(row["reward_mean"]),
        "reward_ewma": safe_float(row["reward_ewma"]),
    }


def bootstrap_session_memory(connection: sqlite3.Connection) -> None:
    if state_get(connection, "session_memory_bootstrap") == "demo_v6":
        return
    existing = int(connection.execute(
        "SELECT COUNT(*) AS n FROM session_family_memory"
    ).fetchone()["n"])
    if existing == 0:
        rows = connection.execute(
            """
            SELECT d.symbol, s.family, d.regime, d.side, d.reward_r,
                   d.opened_at
            FROM demo_positions d JOIN strategies s ON s.id=d.strategy_id
            WHERE d.status='closed' AND d.reward_r IS NOT NULL
            ORDER BY d.id
            """
        ).fetchall()
        for row in rows:
            update_session_family_memory(
                connection, str(row["symbol"]), str(row["family"]),
                str(row["regime"]),
                market_session_tag(str(row["symbol"]), row["opened_at"]),
                int(row["side"]), safe_float(row["reward_r"]), 1.0,
            )
    state_set(connection, "session_memory_bootstrap", "demo_v6")


def bootstrap_collective_memory(connection: sqlite3.Connection) -> None:
    if state_get(connection, "collective_memory_bootstrap") == "demo_v5.5":
        return
    existing = int(connection.execute("SELECT COUNT(*) AS n FROM collective_market_memory").fetchone()["n"])
    if existing == 0:
        rows = connection.execute(
            """
            SELECT d.symbol, s.family, d.regime, d.side, d.reward_r
            FROM demo_positions d JOIN strategies s ON s.id=d.strategy_id
            WHERE d.status='closed' AND d.reward_r IS NOT NULL
            ORDER BY d.id
            """
        ).fetchall()
        for row in rows:
            update_collective_memory(
                connection, str(row["symbol"]), str(row["family"]),
                str(row["regime"]), int(row["side"]), safe_float(row["reward_r"]), 1.0,
            )
    state_set(connection, "collective_memory_bootstrap", "demo_v5.5")


def _model_default(model_key: str) -> dict[str, Any]:
    n = len(SUPER_FEATURE_NAMES)
    return {
        "model_key": model_key,
        "weights": np.zeros(n, dtype=float),
        "mean": np.zeros(n, dtype=float),
        "m2": np.zeros(n, dtype=float),
        "scaler_count": 0,
        "updates": 0,
        "bias": 0.0,
        "last_probability": 0.50,
        "last_logloss": 0.0,
    }


def _load_online_model(connection: sqlite3.Connection, model_key: str) -> dict[str, Any]:
    row = connection.execute(
        "SELECT * FROM superlearner_model_state WHERE model_key=?", (model_key,)
    ).fetchone()
    if not row:
        return _model_default(model_key)
    try:
        names = tuple(json.loads(str(row["feature_names_json"])))
        if names != SUPER_FEATURE_NAMES:
            return _model_default(model_key)
        state = _model_default(model_key)
        state.update({
            "weights": np.asarray(json.loads(str(row["weights_json"])), dtype=float),
            "mean": np.asarray(json.loads(str(row["mean_json"])), dtype=float),
            "m2": np.asarray(json.loads(str(row["m2_json"])), dtype=float),
            "scaler_count": int(row["scaler_count"] or 0),
            "updates": int(row["updates"] or 0),
            "bias": safe_float(row["bias"]),
            "last_probability": safe_float(row["last_probability"], 0.50),
            "last_logloss": safe_float(row["last_logloss"]),
        })
        if len(state["weights"]) != len(SUPER_FEATURE_NAMES):
            return _model_default(model_key)
        return state
    except Exception:
        return _model_default(model_key)


def _save_online_model(connection: sqlite3.Connection, state: dict[str, Any]) -> None:
    connection.execute(
        """
        INSERT INTO superlearner_model_state(
            model_key, feature_names_json, weights_json, mean_json, m2_json,
            scaler_count, updates, bias, last_probability, last_logloss, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(model_key) DO UPDATE SET
            feature_names_json=excluded.feature_names_json,
            weights_json=excluded.weights_json, mean_json=excluded.mean_json,
            m2_json=excluded.m2_json, scaler_count=excluded.scaler_count,
            updates=excluded.updates, bias=excluded.bias,
            last_probability=excluded.last_probability,
            last_logloss=excluded.last_logloss, updated_at=excluded.updated_at
        """,
        (
            state["model_key"], json_text(list(SUPER_FEATURE_NAMES)),
            json_text([float(x) for x in state["weights"]]),
            json_text([float(x) for x in state["mean"]]),
            json_text([float(x) for x in state["m2"]]),
            int(state["scaler_count"]), int(state["updates"]), safe_float(state["bias"]),
            safe_float(state["last_probability"], 0.50), safe_float(state["last_logloss"]), utc_now(),
        ),
    )


def _feature_vector(features: dict[str, Any]) -> np.ndarray:
    return np.asarray([safe_float(features.get(name)) for name in SUPER_FEATURE_NAMES], dtype=float)


def _standardize_for_model(state: dict[str, Any], vector: np.ndarray) -> np.ndarray:
    count = int(state["scaler_count"])
    if count < 2:
        return np.clip(vector, -6.0, 6.0)
    variance = state["m2"] / max(1, count - 1)
    std = np.sqrt(np.maximum(variance, 1e-6))
    return np.clip((vector - state["mean"]) / std, -6.0, 6.0)


def _sigmoid(value: float) -> float:
    value = clamp(safe_float(value), -30.0, 30.0)
    return 1.0 / (1.0 + math.exp(-value))


def online_model_probability(
    connection: sqlite3.Connection,
    symbol: str,
    features: dict[str, Any],
) -> dict[str, Any]:
    vector = _feature_vector(features)
    minimum = max(5, int(getattr(settings, "SUPERLEARNER_MIN_ONLINE_UPDATES", 24)))
    states = [_load_online_model(connection, "global"), _load_online_model(connection, f"symbol:{symbol}")]
    predictions: list[tuple[float, float, int]] = []
    for index, state in enumerate(states):
        z = _standardize_for_model(state, vector)
        probability = _sigmoid(float(np.dot(state["weights"], z)) + safe_float(state["bias"]))
        updates = int(state["updates"])
        active = updates >= (minimum if index == 0 else max(10, minimum // 2))
        if active:
            predictions.append((probability, 0.70 if index == 0 else 0.30, updates))
    if not predictions:
        return {
            "probability": 0.50,
            "active": False,
            "global_updates": int(states[0]["updates"]),
            "symbol_updates": int(states[1]["updates"]),
        }
    total_weight = sum(weight for _, weight, _ in predictions)
    probability = sum(p * weight for p, weight, _ in predictions) / max(total_weight, 1e-9)
    return {
        "probability": clamp(probability, 0.02, 0.98),
        "active": True,
        "global_updates": int(states[0]["updates"]),
        "symbol_updates": int(states[1]["updates"]),
    }


def update_online_model(
    connection: sqlite3.Connection,
    symbol: str,
    features: dict[str, Any],
    reward_r: float,
    source_weight: float = 1.0,
) -> None:
    if not bool(getattr(settings, "ENABLE_SUPERLEARNER_ONLINE_MODEL", True)):
        return
    vector = _feature_vector(features)
    label = 1.0 if safe_float(reward_r) > 0 else 0.0
    sample_weight = clamp(abs(safe_float(reward_r)), 0.25, 2.0) * clamp(source_weight, 0.05, 1.0)
    learning_rate = clamp(safe_float(getattr(settings, "SUPERLEARNER_LEARNING_RATE", 0.05), 0.05), 0.001, 0.25)
    l2 = clamp(safe_float(getattr(settings, "SUPERLEARNER_L2", 0.001), 0.001), 0.0, 0.10)
    drift = clamp(safe_float(features.get("drift")), 0.0, 1.0)
    forgetting = 1.0 - clamp(drift * safe_float(getattr(settings, "SUPERLEARNER_DRIFT_FORGET_RATE", 0.025), 0.025), 0.0, 0.10)

    for model_key in ("global", f"symbol:{symbol}"):
        state = _load_online_model(connection, model_key)
        z = _standardize_for_model(state, vector)
        probability = _sigmoid(float(np.dot(state["weights"], z)) + safe_float(state["bias"]))
        gradient = (label - probability) * sample_weight
        state["weights"] = state["weights"] * forgetting * (1.0 - learning_rate * l2) + learning_rate * gradient * z
        state["bias"] = clamp(safe_float(state["bias"]) + learning_rate * gradient, -8.0, 8.0)
        # Update scaler after gradient so the current sample cannot normalize itself to zero.
        count = int(state["scaler_count"]) + 1
        delta = vector - state["mean"]
        mean = state["mean"] + delta / count
        delta2 = vector - mean
        state["m2"] = state["m2"] + delta * delta2
        state["mean"] = mean
        state["scaler_count"] = count
        state["updates"] = int(state["updates"]) + 1
        state["last_probability"] = probability
        eps = 1e-8
        state["last_logloss"] = -(label * math.log(max(eps, probability)) + (1.0 - label) * math.log(max(eps, 1.0 - probability)))
        _save_online_model(connection, state)


def bootstrap_superlearner_model(connection: sqlite3.Connection) -> None:
    """V7: rebuild the execution classifier from real broker-DEMO outcomes only.

    V5.5/V6 allowed thousands of discounted shadow outcomes to update the same
    classifier used to approve broker-demo entries. In the supplied database
    shadow samples numerically dominated executed outcomes, so the classifier
    could learn the laboratory rather than execution reality. V7 performs a
    one-time clean rebuild while preserving every trade/history row.
    """
    marker = state_get(connection, "superlearner_model_bootstrap")
    if marker == "feature_context_v7_demo_only":
        return
    connection.execute("DELETE FROM superlearner_model_state")
    rows = connection.execute(
        """
        SELECT symbol, reward_r, context_json FROM demo_positions
        WHERE status='closed' AND reward_r IS NOT NULL
          AND context_json IS NOT NULL AND context_json<>''
        ORDER BY id
        """
    ).fetchall()
    used = 0
    for row in rows:
        try:
            context = json.loads(str(row["context_json"]))
            super_data = context.get("superlearner", {}) if isinstance(context, dict) else {}
            features = super_data.get("features", {}) if isinstance(super_data, dict) else {}
            if features and all(name in features for name in SUPER_FEATURE_NAMES):
                update_online_model(connection, str(row["symbol"]), features, safe_float(row["reward_r"]), 1.0)
                used += 1
        except Exception:
            continue
    state_set(connection, "superlearner_model_bootstrap", "feature_context_v7_demo_only")
    state_set(connection, "superlearner_model_bootstrap_rows", used)
    state_set(connection, "v7_learning_repair_installed", utc_now())

def order_book_microstructure(symbol: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "available": False, "imbalance": None, "imbalance_pct": None,
        "bid_share": 0.0, "ask_share": 0.0, "persistence": 0.0,
        "change": 0.0, "score": 0.0, "buy_volume": 0.0, "sell_volume": 0.0,
        "avg_5s": None, "avg_15s": None, "avg_30s": None,
    }
    if not mt5.market_book_add(symbol):
        return result
    try:
        time.sleep(0.02)
        book = mt5.market_book_get(symbol)
        if not book:
            return result
        buy_types = {getattr(mt5, "BOOK_TYPE_BUY", 2), getattr(mt5, "BOOK_TYPE_BUY_MARKET", 4)}
        sell_types = {getattr(mt5, "BOOK_TYPE_SELL", 1), getattr(mt5, "BOOK_TYPE_SELL_MARKET", 3)}
        buy_volume = sell_volume = 0.0
        for item in book:
            volume = safe_float(getattr(item, "volume_dbl", getattr(item, "volume", 0.0)))
            if item.type in buy_types:
                buy_volume += volume
            elif item.type in sell_types:
                sell_volume += volume
        total = buy_volume + sell_volume
        if total <= 0:
            return result
        imbalance = (buy_volume - sell_volume) / total
        history = _DOM_HISTORY.setdefault(symbol, deque(maxlen=max(15, int(getattr(settings, "DOM_HISTORY_SNAPSHOTS", 30)))))
        previous = safe_float(history[-1].get("imbalance")) if history else imbalance
        now_ts = time.time()
        history.append({"imbalance": imbalance, "timestamp": now_ts})
        persistence = sum(safe_float(item.get("imbalance")) for item in history) / max(1, len(history))
        def window_average(seconds: float) -> float | None:
            values = [
                safe_float(item.get("imbalance")) for item in history
                if now_ts - safe_float(item.get("timestamp"), now_ts) <= seconds
            ]
            return (sum(values) / len(values)) if values else None
        avg_5s = window_average(5.0)
        avg_15s = window_average(15.0)
        avg_30s = window_average(30.0)
        change = imbalance - previous
        persistence_15 = safe_float(avg_15s, persistence)
        score = clamp(0.55 * imbalance + 0.30 * persistence_15 + 0.15 * change, -1.0, 1.0)
        bid_share = buy_volume / total
        ask_share = sell_volume / total
        result.update({
            "available": True, "imbalance": imbalance, "imbalance_pct": imbalance * 100.0,
            "bid_share": bid_share, "ask_share": ask_share, "persistence": persistence_15,
            "change": change, "score": score, "buy_volume": buy_volume,
            "sell_volume": sell_volume, "snapshots": len(history),
            "avg_5s": avg_5s, "avg_15s": avg_15s, "avg_30s": avg_30s,
        })
        return result
    finally:
        mt5.market_book_release(symbol)


def hurst_exponent(series: pd.Series, window: int = 160) -> float:
    values = np.asarray(series.dropna().tail(max(80, int(window))), dtype=float)
    if len(values) < 80:
        return 0.50
    lags = np.arange(2, min(30, len(values) // 4))
    tau = []
    valid_lags = []
    for lag in lags:
        diff = values[lag:] - values[:-lag]
        std = float(np.std(diff))
        if std > 0 and math.isfinite(std):
            valid_lags.append(float(lag))
            tau.append(std)
    if len(tau) < 6:
        return 0.50
    try:
        slope = float(np.polyfit(np.log(valid_lags), np.log(tau), 1)[0])
    except Exception:
        return 0.50
    return clamp(slope, 0.05, 0.95)


def normalized_return_entropy(series: pd.Series, window: int = 180, bins: int = 10) -> float:
    returns = pd.Series(series).pct_change().replace([np.inf, -np.inf], np.nan).dropna().tail(max(60, int(window)))
    values = np.asarray(returns, dtype=float)
    if len(values) < 50 or float(np.std(values)) <= 0:
        return 0.50
    clipped = np.clip(values, np.quantile(values, 0.02), np.quantile(values, 0.98))
    hist, _ = np.histogram(clipped, bins=max(6, int(bins)))
    total = float(hist.sum())
    if total <= 0:
        return 0.50
    probabilities = hist[hist > 0].astype(float) / total
    entropy = -float(np.sum(probabilities * np.log(probabilities)))
    maximum = math.log(max(2, len(hist)))
    return clamp(entropy / maximum if maximum > 0 else 0.50, 0.0, 1.0)



def market_sentiment_proxy(
    frame: pd.DataFrame,
    micro: dict[str, Any] | None = None,
) -> float:
    """Price/volume/DOM sentiment proxy in [-1, 1].

    It deliberately does not pretend to be external news sentiment. It combines
    short momentum, EMA structure, RSI, volume participation and available DOM.
    """
    if len(frame) < 8:
        return 0.0
    row = frame.iloc[-2]
    atr_value = max(1e-12, safe_float(row.get("atr_14")))
    momentum = clamp(safe_float(row.get("momentum_3_atr")), -2.0, 2.0) / 2.0
    trend = clamp(
        (safe_float(row.get("ema_20")) - safe_float(row.get("ema_50"))) / atr_value,
        -2.0, 2.0,
    ) / 2.0
    rsi_score = clamp((safe_float(row.get("rsi_14"), 50.0) - 50.0) / 25.0, -1.0, 1.0)
    volume = clamp((safe_float(row.get("volume_ratio_20"), 1.0) - 1.0) / 1.5, -1.0, 1.0)
    dom = clamp(safe_float((micro or {}).get("score")), -1.0, 1.0)
    return clamp(
        0.32 * momentum + 0.28 * trend + 0.18 * rsi_score
        + 0.10 * volume + 0.12 * dom,
        -1.0, 1.0,
    )


def market_quality_metrics(
    frame: pd.DataFrame,
    atr_value: float,
    spread: float,
    micro: dict[str, Any],
) -> dict[str, Any]:
    clean = frame.dropna(subset=["close", "atr_ratio", "return_1"])
    hurst = hurst_exponent(clean["close"] if not clean.empty else frame["close"])
    entropy = normalized_return_entropy(clean["close"] if not clean.empty else frame["close"])
    short_vol = safe_float(clean["return_1"].tail(30).std()) if len(clean) >= 30 else 0.0
    long_vol = safe_float(clean["return_1"].tail(300).std()) if len(clean) >= 80 else short_vol
    short_atr = safe_float(clean["atr_ratio"].tail(30).mean()) if len(clean) >= 30 else 0.0
    long_atr = safe_float(clean["atr_ratio"].tail(300).mean()) if len(clean) >= 80 else short_atr
    def ratio_drift(short: float, long: float) -> float:
        if short <= 0 or long <= 0:
            return 0.0
        return clamp(abs(math.log(short / long)) / math.log(2.0), 0.0, 1.0)
    vol_drift = ratio_drift(short_vol, long_vol)
    atr_drift = ratio_drift(short_atr, long_atr)
    spread_atr = safe_float(spread) / max(1e-12, safe_float(atr_value))
    spread_stress = clamp(spread_atr / max(1e-9, safe_float(getattr(settings, "MAX_SPREAD_ATR_FRACTION", 0.18))), 0.0, 1.5)
    drift = clamp(max(vol_drift, atr_drift, max(0.0, spread_stress - 0.50) / 1.50), 0.0, 1.0)
    price_move = 0.0
    if len(clean) >= 6:
        price_move = safe_float(clean.iloc[-1]["close"] - clean.iloc[-6]["close"])
    absorption = 0.0
    persistence = safe_float(micro.get("persistence"))
    if price_move < 0 and persistence > 0.20:
        absorption = min(0.25, persistence * 0.50)
    elif price_move > 0 and persistence < -0.20:
        absorption = max(-0.25, persistence * 0.50)
    micro_score = clamp(safe_float(micro.get("score")) + absorption, -1.0, 1.0)
    random_zone = bool(0.45 <= hurst <= 0.55)
    sentiment = market_sentiment_proxy(frame, micro)
    return {
        "hurst": hurst, "entropy": entropy, "drift": drift,
        "volatility_drift": vol_drift, "atr_drift": atr_drift,
        "spread_atr": spread_atr, "microstructure_score": micro_score,
        "dom_absorption": absorption, "random_zone": random_zone,
        "sentiment_score": sentiment,
    }


def dynamic_ensemble_multiplier(
    connection: sqlite3.Connection,
    strategy: StrategyDefinition,
    regime: str,
    side: int,
) -> tuple[float, dict[str, Any]]:
    snapshot = adaptive_learning_snapshot(connection, strategy.strategy_id, strategy.symbol, regime, side)
    rolling = rolling_setup_performance(
        connection, strategy.strategy_id, strategy.symbol, regime, side,
        max(5, int(getattr(settings, "SUPERLEARNER_ROLLING_WINDOW", 20))),
    )
    collective = collective_memory_snapshot(connection, strategy.symbol, strategy.family, regime, side)
    session = market_session_tag(strategy.symbol)
    session_memory = session_family_memory_snapshot(
        connection, strategy.symbol, strategy.family, regime, session, side
    )
    bayes_loss = bayesian_loss_probability(snapshot, rolling)
    confidence = safe_float(snapshot.get("confidence"), 0.50)
    rolling_win = safe_float(rolling.get("win_rate"), 0.50)
    collective_win = safe_float(collective.get("win_probability"), 0.50)
    session_win = safe_float(session_memory.get("win_probability"), 0.50)
    reward = (
        0.35 * safe_float(snapshot.get("reward_ewma"))
        + 0.20 * safe_float(rolling.get("reward_mean"))
        + 0.25 * safe_float(collective.get("reward_ewma"))
        + 0.20 * safe_float(session_memory.get("reward_ewma"))
    )
    quality = (
        0.35 * confidence + 0.25 * rolling_win
        + 0.20 * collective_win + 0.20 * session_win
    )
    multiplier = 1.0 + (quality - 0.50) * 1.8 + 0.22 * math.tanh(reward) - 0.40 * max(0.0, bayes_loss - 0.50)
    if family_is_scalp(strategy.family) and bool(getattr(settings, "SCALP_PRIMARY_MODE", True)):
        multiplier *= 1.08
    multiplier -= 0.10 * min(3, int(snapshot.get("consecutive_losses") or 0))
    minimum = safe_float(getattr(settings, "DYNAMIC_ENSEMBLE_MIN_MULTIPLIER", 0.40), 0.40)
    maximum = safe_float(getattr(settings, "DYNAMIC_ENSEMBLE_MAX_MULTIPLIER", 1.80), 1.80)
    return clamp(multiplier, minimum, maximum), {
        "adaptive_confidence": confidence,
        "rolling_win_rate": rolling_win,
        "collective_win_probability": collective_win,
        "collective_observations": safe_float(collective.get("observations")),
        "session": session,
        "session_win_probability": session_win,
        "session_observations": safe_float(session_memory.get("observations")),
        "bayes_loss_probability": bayes_loss,
        "reward_blend": reward,
    }


def recent_loss_pattern_penalty(
    connection: sqlite3.Connection,
    symbol: str,
    flags: dict[str, bool],
) -> tuple[float, dict[str, float]]:
    rows = connection.execute(
        """
        SELECT context_json FROM demo_positions
        WHERE symbol=? AND status='closed' AND reward_r<=0
          AND context_json IS NOT NULL AND context_json<>''
        ORDER BY id DESC LIMIT ?
        """,
        (symbol, max(3, int(getattr(settings, "XAI_RECENT_LOSS_WINDOW", 8)))),
    ).fetchall()
    if len(rows) < 3:
        return 0.0, {}
    counts: dict[str, int] = {key: 0 for key in flags}
    usable = 0
    for row in rows:
        try:
            context = json.loads(str(row["context_json"]))
            previous = context.get("superlearner", {}).get("condition_flags", {})
            if not isinstance(previous, dict):
                continue
            usable += 1
            for key in counts:
                if bool(previous.get(key)):
                    counts[key] += 1
        except Exception:
            continue
    if usable < 3:
        return 0.0, {}
    frequencies = {key: value / usable for key, value in counts.items()}
    penalty = 0.0
    threshold = safe_float(getattr(settings, "XAI_COMMON_LOSS_FREQUENCY", 0.60), 0.60)
    for key, active in flags.items():
        if active and frequencies.get(key, 0.0) >= threshold:
            penalty += 0.02
    return min(0.08, penalty), frequencies


def build_super_features(
    strategy: StrategyDefinition,
    row: pd.Series,
    regime: str,
    side: int,
    signal_details: dict[str, Any],
    adaptive_snapshot: dict[str, Any],
    bayes_loss: float,
    collective: dict[str, float],
    market: dict[str, Any],
    micro: dict[str, Any],
) -> dict[str, float]:
    buy_weight = safe_float(signal_details.get("buy_weight"))
    sell_weight = safe_float(signal_details.get("sell_weight"))
    own_weight = buy_weight if side == 1 else sell_weight
    other_weight = sell_weight if side == 1 else buy_weight
    dominance = own_weight / max(0.10, other_weight)
    buy_votes = int(signal_details.get("buy_votes") or 0)
    sell_votes = int(signal_details.get("sell_votes") or 0)
    vote_balance = (buy_votes - sell_votes) / max(1, buy_votes + sell_votes)
    now = datetime.now(timezone.utc)
    hour_angle = 2.0 * math.pi * (now.hour + now.minute / 60.0) / 24.0
    trend_strength = abs(safe_float(row.get("ema_20")) - safe_float(row.get("ema_50"))) / max(1e-12, safe_float(row.get("atr_14")))
    regime_trend = 1.0 if regime in {"trend", "volatile_trend", "extreme_trend"} else 0.0
    regime_range = 1.0 if regime in {"range", "volatile_range"} else 0.0
    high_vol = 1.0 if regime in {"volatile_range", "volatile_trend", "extreme_trend"} else 0.0
    features = {
        "side": float(side),
        "rsi14": (safe_float(row.get("rsi_14"), 50.0) - 50.0) / 25.0,
        "atr_ratio": safe_float(row.get("atr_ratio")) * 1000.0,
        "volatility": safe_float(row.get("volatility_30")) * 1000.0,
        "trend_strength": trend_strength,
        "spread_atr": safe_float(market.get("spread_atr")),
        "dom_imbalance": safe_float(micro.get("imbalance")),
        "dom_persistence": safe_float(micro.get("persistence")),
        "dom_change": safe_float(micro.get("change")),
        "microstructure": safe_float(market.get("microstructure_score")),
        "hurst": safe_float(market.get("hurst"), 0.50),
        "entropy": safe_float(market.get("entropy"), 0.50),
        "drift": safe_float(market.get("drift")),
        "ensemble_dominance": min(5.0, dominance),
        "vote_balance": vote_balance,
        "adaptive_confidence": safe_float(adaptive_snapshot.get("confidence"), 0.50),
        "bayes_loss": bayes_loss,
        "collective_win_prob": safe_float(collective.get("win_probability"), 0.50),
        "collective_reward": safe_float(collective.get("reward_ewma")),
        "hour_sin": math.sin(hour_angle), "hour_cos": math.cos(hour_angle),
        "regime_trend": regime_trend, "regime_range": regime_range,
        "regime_high_vol": high_vol,
        "family_super_scalp": 1.0 if strategy.family == "super_scalp" else 0.0,
        # Keep the V5.5 feature vector backward-compatible: all new V6 scalp
        # archetypes map into the existing family_scalp feature so learned
        # model state does not need to be discarded.
        "family_scalp": 1.0 if family_is_scalp(strategy.family) and strategy.family != "super_scalp" else 0.0,
        "family_trend": 1.0 if strategy.family == "trend" else 0.0,
        "family_mean_reversion": 1.0 if strategy.family in {"mean_reversion", "mean_revert_scalp"} else 0.0,
        "family_breakout": 1.0 if strategy.family in {"breakout", "breakout_scalp"} else 0.0,
    }
    return {name: safe_float(features.get(name)) for name in SUPER_FEATURE_NAMES}


def exit_efficiency_profile(
    connection: sqlite3.Connection,
    strategy_id: int,
    symbol: str,
    regime: str,
    side: int,
    window: int = 30,
) -> dict[str, float]:
    rows = connection.execute(
        """
        SELECT reward_r, mfe_r, mae_r FROM demo_positions
        WHERE strategy_id=? AND symbol=? AND regime=? AND side=?
          AND status='closed' AND reward_r IS NOT NULL
          AND mfe_r IS NOT NULL AND mae_r IS NOT NULL
        ORDER BY id DESC LIMIT ?
        """,
        (int(strategy_id), symbol, regime, int(side), max(5, min(100, int(window)))),
    ).fetchall()
    if not rows:
        return {
            "observations": 0.0, "reward_mean": 0.0, "mfe_mean": 0.0,
            "mae_mean": 0.0, "capture_ratio": 0.50,
        }
    rewards = [safe_float(row["reward_r"]) for row in rows]
    mfes = [max(0.0, safe_float(row["mfe_r"])) for row in rows]
    maes = [max(0.0, safe_float(row["mae_r"])) for row in rows]
    positive_reward = sum(max(0.0, reward) for reward in rewards) / len(rewards)
    mfe_mean = sum(mfes) / len(mfes)
    return {
        "observations": float(len(rows)),
        "reward_mean": sum(rewards) / len(rewards),
        "mfe_mean": mfe_mean,
        "mae_mean": sum(maes) / len(maes),
        "capture_ratio": clamp(positive_reward / max(0.05, mfe_mean), 0.0, 2.0),
    }


def adaptive_exit_multipliers(
    strategy: StrategyDefinition,
    side: int,
    probability: float,
    probability_active: bool,
    market: dict[str, Any],
    bayes_loss: float,
    collective: dict[str, float],
    exit_profile: dict[str, float] | None = None,
) -> tuple[float, float, float]:
    sl_mult = 1.0
    tp_mult = 1.0
    risk_mult = 1.0
    drift = safe_float(market.get("drift"))
    entropy = safe_float(market.get("entropy"))
    hurst = safe_float(market.get("hurst"), 0.50)
    if drift >= 0.65:
        sl_mult *= 1.08
        tp_mult *= 1.05
        risk_mult *= 0.70
    if entropy >= 0.88:
        sl_mult *= 1.05
        risk_mult *= 0.82
    if strategy.family in {"trend", "breakout"} and hurst > 0.56:
        sl_mult *= 1.04
        tp_mult *= 1.14
    elif strategy.family == "mean_reversion" and hurst < 0.44:
        tp_mult *= 1.08
    if probability_active:
        if probability >= 0.68 and bayes_loss <= 0.45:
            tp_mult *= 1.10
            risk_mult *= 1.08
        elif probability < 0.52:
            risk_mult *= 0.75
            tp_mult *= 0.95
    if safe_float(collective.get("reward_ewma")) < -0.15:
        risk_mult *= 0.80
    profile = exit_profile or {}
    if safe_float(profile.get("observations")) >= 5:
        capture = safe_float(profile.get("capture_ratio"), 0.50)
        mfe_mean = safe_float(profile.get("mfe_mean"))
        mae_mean = safe_float(profile.get("mae_mean"))
        profile_reward = safe_float(profile.get("reward_mean"))
        # If trades repeatedly travel in our favour but give most of it back,
        # harvest a little earlier. If they consistently sustain >1R favourable
        # excursion and still close positively, allow the target to breathe.
        if mfe_mean >= 0.60 and capture < 0.45:
            tp_mult *= 0.93
        elif mfe_mean >= 1.20 and capture >= 0.60 and profile_reward > 0:
            tp_mult *= 1.07
        # High adverse excursion plus negative expectancy is treated as a risk
        # problem, not an excuse to blindly widen the stop.
        if mae_mean >= 0.80 and profile_reward < 0:
            risk_mult *= 0.82
    sl_mult = clamp(sl_mult, safe_float(getattr(settings, "ADAPTIVE_SL_MIN_MULTIPLIER", 0.80), 0.80), safe_float(getattr(settings, "ADAPTIVE_SL_MAX_MULTIPLIER", 1.25), 1.25))
    tp_mult = clamp(tp_mult, safe_float(getattr(settings, "ADAPTIVE_TP_MIN_MULTIPLIER", 0.80), 0.80), safe_float(getattr(settings, "ADAPTIVE_TP_MAX_MULTIPLIER", 1.40), 1.40))
    risk_mult = clamp(risk_mult, 0.45, 1.12)
    return sl_mult, tp_mult, risk_mult


@dataclass
class SuperLearnerDecision:
    approved: bool
    reason: str
    probability: float
    probability_active: bool
    threshold: float
    bayes_loss_probability: float
    sl_multiplier: float
    tp_multiplier: float
    risk_multiplier: float
    features: dict[str, float]
    market: dict[str, Any]
    microstructure: dict[str, Any]
    collective: dict[str, float]
    exit_profile: dict[str, float]
    condition_flags: dict[str, bool]
    xai_penalty: float
    xai_loss_frequencies: dict[str, float]
    latency_ms: float

    def as_dict(self) -> dict[str, Any]:
        return dataclass_asdict(self)


class SuperLearner:
    """Final DEMO approval authority layered over the existing five-stage system."""

    def predict(
        self,
        connection: sqlite3.Connection,
        strategy: StrategyDefinition,
        frame: pd.DataFrame,
        row: pd.Series,
        regime: str,
        side: int,
        signal_details: dict[str, Any],
        adaptive_gate: dict[str, Any],
        spread: float,
        atr_value: float,
        micro: dict[str, Any],
    ) -> SuperLearnerDecision:
        started_at = time.perf_counter()
        snapshot = dict(adaptive_gate.get("snapshot") or {})
        bayes_loss = safe_float(adaptive_gate.get("bayes_loss_probability"), 0.50)
        collective = collective_memory_snapshot(connection, strategy.symbol, strategy.family, regime, side)
        exit_profile = exit_efficiency_profile(
            connection, strategy.strategy_id, strategy.symbol, regime, side,
            max(5, int(getattr(settings, "MFE_MAE_ROLLING_WINDOW", 30))),
        )
        market = market_quality_metrics(frame, atr_value, spread, micro)
        session = market_session_tag(
            strategy.symbol,
            row.get("time") if hasattr(row, "get") else None,
        )
        session_memory = session_family_memory_snapshot(
            connection, strategy.symbol, strategy.family, regime, session, side
        )
        market["session"] = session
        market["session_memory"] = session_memory
        features = build_super_features(
            strategy, row, regime, side, signal_details, snapshot,
            bayes_loss, collective, market, micro,
        )
        prediction = online_model_probability(connection, strategy.symbol, features)
        model_probability = safe_float(prediction.get("probability"), 0.50)
        active = bool(prediction.get("active"))
        is_scalp = bool(
            family_is_scalp(strategy.family)
            and getattr(settings, "SUPERLEARNER_SCALP_EXPECTANCY_MODE", True)
        )

        flags = {
            "high_spread": safe_float(market.get("spread_atr")) >= safe_float(getattr(settings, "SUPERLEARNER_HIGH_SPREAD_ATR", 0.12), 0.12),
            "high_entropy": safe_float(market.get("entropy")) >= safe_float(getattr(settings, "SUPERLEARNER_HIGH_ENTROPY", 0.88), 0.88),
            "high_drift": safe_float(market.get("drift")) >= safe_float(getattr(settings, "SUPERLEARNER_HIGH_DRIFT", 0.70), 0.70),
            "random_hurst": bool(market.get("random_zone")),
            "dom_against": safe_float(market.get("microstructure_score")) * side <= -safe_float(getattr(settings, "SUPERLEARNER_DOM_VETO_SCORE", 0.45), 0.45),
            "sentiment_against": safe_float(market.get("sentiment_score")) * side <= -0.65,
            "session_cold": (
                safe_float(session_memory.get("observations"))
                >= safe_float(getattr(settings, "SESSION_MEMORY_MIN_OBSERVATIONS", 6.0), 6.0)
                and safe_float(session_memory.get("win_probability"), 0.50)
                < safe_float(getattr(settings, "SESSION_BAD_WIN_PROBABILITY", 0.40), 0.40)
            ),
        }
        xai_penalty, frequencies = recent_loss_pattern_penalty(connection, strategy.symbol, flags)

        # V6.6: high-R:R scalp opportunities are judged by expected R and by a
        # calibrated blend of independent evidence. The old fixed 0.58-0.68
        # probability floor rejected many mathematically positive-expectancy
        # scalp candidates before GPT ever saw them.
        probability_components: dict[str, dict[str, float]] = {}
        if is_scalp:
            weighted_sum = 0.0
            total_weight = 0.0

            def add_component(name: str, value: float, weight: float, observations: float, usable: bool = True) -> None:
                nonlocal weighted_sum, total_weight
                value = clamp(safe_float(value, 0.50), 0.05, 0.95)
                weight = max(0.0, safe_float(weight))
                probability_components[name] = {
                    "probability": value, "weight": weight if usable else 0.0,
                    "observations": max(0.0, safe_float(observations)),
                }
                if usable and weight > 0.0:
                    weighted_sum += value * weight
                    total_weight += weight

            add_component(
                "online_model", model_probability,
                safe_float(getattr(settings, "SUPERLEARNER_SCALP_MODEL_WEIGHT", 0.55), 0.55),
                safe_float(prediction.get("global_updates")), active,
            )
            adaptive_obs = safe_float(snapshot.get("observations"))
            add_component(
                "adaptive_bayes", 1.0 - bayes_loss,
                safe_float(getattr(settings, "SUPERLEARNER_SCALP_ADAPTIVE_WEIGHT", 0.20), 0.20),
                adaptive_obs, adaptive_obs >= 3.0,
            )
            collective_obs = safe_float(collective.get("observations"))
            add_component(
                "collective", safe_float(collective.get("win_probability"), 0.50),
                safe_float(getattr(settings, "SUPERLEARNER_SCALP_COLLECTIVE_WEIGHT", 0.15), 0.15),
                collective_obs, collective_obs >= 4.0,
            )
            session_obs_for_blend = safe_float(session_memory.get("observations"))
            add_component(
                "session_family", safe_float(session_memory.get("win_probability"), 0.50),
                safe_float(getattr(settings, "SUPERLEARNER_SCALP_SESSION_WEIGHT", 0.10), 0.10),
                session_obs_for_blend, session_obs_for_blend >= 4.0,
            )
            hunter_ctx_for_blend = signal_details.get("micro_hunter") if isinstance(signal_details.get("micro_hunter"), dict) else {}
            hunter_mem = hunter_ctx_for_blend.get("playbook_memory") if isinstance(hunter_ctx_for_blend.get("playbook_memory"), dict) else {}
            hunter_obs = safe_float(hunter_mem.get("observations"))
            if hunter_ctx_for_blend:
                # V8 institutional ensemble: the deterministic micro-alpha engine
                # contributes a bounded expert prior immediately, even before the
                # playbook has enough broker-DEMO outcomes for its learned memory
                # component. This removes the old single-online-model bottleneck.
                alpha_score = clamp(safe_float(hunter_ctx_for_blend.get("score"), 0.0), 0.0, 100.0)
                alpha_probability = clamp(0.25 + 0.005 * alpha_score, 0.35, 0.78)
                add_component(
                    "micro_alpha_score", alpha_probability,
                    safe_float(getattr(settings, "V8_ALPHA_SCORE_SUPER_WEIGHT", 0.35), 0.35),
                    1.0, alpha_score >= safe_float(getattr(settings, "V8_ALPHA_SCORE_BLEND_MIN", 55.0), 55.0),
                )
                hunter_win = safe_float(hunter_mem.get("win_rate"), 0.50)
                hunter_reward = safe_float(hunter_mem.get("reward_ewma"), 0.0)
                hunter_reward_prob = clamp(0.50 + hunter_reward / 1.50, 0.10, 0.90)
                hunter_probability = 0.60 * hunter_win + 0.40 * hunter_reward_prob
                add_component(
                    "micro_playbook", hunter_probability,
                    safe_float(getattr(settings, "V7_MICRO_PLAYBOOK_SUPER_WEIGHT", 0.15), 0.15),
                    hunter_obs, hunter_obs >= safe_float(getattr(settings, "V7_MICRO_PLAYBOOK_MIN_BLEND_OBS", 5), 5),
                )
            probability = (weighted_sum / total_weight) if total_weight > 0 else model_probability
            probability = clamp(probability, 0.05, 0.95)
        else:
            probability = model_probability
            probability_components["online_model"] = {
                "probability": model_probability, "weight": 1.0,
                "observations": safe_float(prediction.get("global_updates")),
            }

        sl_mult, tp_mult, risk_mult = adaptive_exit_multipliers(
            strategy, side, probability, active, market, bayes_loss, collective,
            exit_profile=exit_profile,
        )

        base_stop_atr = safe_float(strategy.params.get("stop_atr"), 1.0)
        base_take_atr = safe_float(strategy.params.get("take_atr"), 1.0)
        hunter_ctx = signal_details.get("micro_hunter") if isinstance(signal_details.get("micro_hunter"), dict) else {}
        if hunter_ctx:
            # Evaluate the same bounded geometry that execution will use. The
            # micro lane may tighten the carrier stop but never widen it.
            base_stop_atr = min(base_stop_atr, max(0.25, safe_float(hunter_ctx.get("stop_atr"), base_stop_atr)))
            base_take_atr = max(0.35, safe_float(hunter_ctx.get("take_atr"), base_take_atr))
        stop_atr = max(1e-9, base_stop_atr * sl_mult)
        take_atr = max(0.0, base_take_atr * tp_mult)
        reward_to_risk = take_atr / stop_atr
        break_even_probability = 1.0 / max(1.000001, 1.0 + reward_to_risk)

        if is_scalp:
            edge_margin = safe_float(getattr(settings, "SUPERLEARNER_SCALP_EDGE_MARGIN", 0.035), 0.035)
            hunter_score_for_threshold = safe_float(hunter_ctx.get("score"), 0.0) if hunter_ctx else 0.0
            if hunter_score_for_threshold >= 82.0:
                edge_margin = max(0.02, edge_margin - 0.012)
            elif hunter_score_for_threshold >= 72.0:
                edge_margin = max(0.025, edge_margin - 0.006)
            min_probability = safe_float(getattr(settings, "SUPERLEARNER_SCALP_MIN_PROBABILITY", 0.34), 0.34)
            max_threshold = safe_float(getattr(settings, "SUPERLEARNER_SCALP_MAX_PROBABILITY_THRESHOLD", 0.62), 0.62)
            threshold = max(min_probability, break_even_probability + edge_margin)
            if flags["high_entropy"] and flags["random_hurst"]:
                threshold += 0.03
            if flags["high_drift"]:
                threshold += 0.02
            if flags["high_spread"]:
                threshold += 0.015
            if bayes_loss >= 0.60 and safe_float(snapshot.get("observations")) >= 3:
                threshold += min(0.05, (bayes_loss - 0.60) * 0.30)
            if safe_float(collective.get("observations")) >= 8 and safe_float(collective.get("win_probability")) < 0.42:
                threshold += 0.02
            if flags["sentiment_against"]:
                threshold += 0.015
            if flags["session_cold"]:
                threshold += 0.025
            elif (
                safe_float(session_memory.get("observations"))
                >= safe_float(getattr(settings, "SESSION_MEMORY_MIN_OBSERVATIONS", 6.0), 6.0)
                and safe_float(session_memory.get("win_probability"), 0.50)
                >= safe_float(getattr(settings, "SESSION_GOOD_WIN_PROBABILITY", 0.58), 0.58)
            ):
                threshold -= 0.015
            threshold += min(0.05, max(0.0, xai_penalty) * 0.60)
            threshold = clamp(threshold, min_probability, max_threshold)
            min_expected_r = safe_float(getattr(settings, "SUPERLEARNER_SCALP_MIN_EXPECTED_R", 0.10), 0.10)
        else:
            threshold = safe_float(getattr(settings, "SUPERLEARNER_BASE_APPROVAL_PROBABILITY", 0.50), 0.50)
            if flags["high_entropy"] and flags["random_hurst"]:
                threshold += 0.04
            if flags["high_drift"]:
                threshold += 0.03
            if bayes_loss >= 0.60:
                threshold += min(0.08, (bayes_loss - 0.60) * 0.40)
            if safe_float(collective.get("observations")) >= 8 and safe_float(collective.get("win_probability")) < 0.42:
                threshold += 0.03
            if flags["sentiment_against"]:
                threshold += 0.025
            if flags["session_cold"]:
                threshold += 0.035
            threshold += xai_penalty
            threshold = clamp(threshold, 0.46, 0.68)
            edge_margin = safe_float(getattr(settings, "V7_PROBABILITY_EDGE_MARGIN", 0.06), 0.06)
            threshold = max(threshold, break_even_probability + edge_margin)
            threshold = clamp(threshold, 0.50, 0.78)
            min_expected_r = safe_float(getattr(settings, "V7_MIN_EXPECTED_R", 0.08), 0.08)

        expected_r = probability * reward_to_risk - (1.0 - probability)
        market["reward_to_risk"] = reward_to_risk
        market["break_even_probability"] = break_even_probability
        market["expected_r"] = expected_r
        market["model_probability_raw"] = model_probability
        market["effective_probability"] = probability
        market["probability_components"] = probability_components
        market["scalp_expectancy_mode"] = is_scalp

        session_obs = safe_float(session_memory.get("observations"))
        session_win = safe_float(session_memory.get("win_probability"), 0.50)
        if session_obs >= safe_float(getattr(settings, "SESSION_MEMORY_MIN_OBSERVATIONS", 6.0), 6.0):
            if session_win < safe_float(getattr(settings, "SESSION_BAD_WIN_PROBABILITY", 0.40), 0.40):
                risk_mult *= 0.75
            elif session_win >= safe_float(getattr(settings, "SESSION_GOOD_WIN_PROBABILITY", 0.58), 0.58):
                risk_mult *= 1.05
        if family_is_scalp(strategy.family) and bool(getattr(settings, "SCALP_PRIMARY_MODE", True)):
            risk_mult *= 1.03
        risk_mult = clamp(risk_mult, 0.35, 1.20)

        approved = True
        reasons: list[str] = []
        if flags["dom_against"] and bool(micro.get("available")):
            approved = False
            reasons.append("persistent DOM/microstructure pressure is strongly against signal")
        if flags["high_entropy"] and flags["high_drift"] and flags["random_hurst"]:
            approved = False
            reasons.append("market is simultaneously random, noisy and drifting")
        if bayes_loss >= safe_float(getattr(settings, "SUPERLEARNER_HARD_BAYES_VETO", 0.76), 0.76) and int(snapshot.get("observations") or 0) >= 5:
            approved = False
            reasons.append(f"Bayesian setup loss probability is {bayes_loss:.2f}")
        if active and probability < threshold:
            approved = False
            label = "calibrated scalp probability" if is_scalp else "online model probability"
            reasons.append(f"{label} {probability:.2f} below {threshold:.2f}")
        if active and expected_r < min_expected_r:
            approved = False
            reasons.append(f"expected edge only {expected_r:.2f}R")
        if not active:
            reasons.append(
                "online model is warming up; adaptive/Bayesian/market filters remain active "
                f"(global_n={int(prediction.get('global_updates') or 0)})"
            )
        if approved and not reasons:
            reasons.append("all proactive approval checks passed")
        elif approved:
            reasons.append("proactive approval passed")

        return SuperLearnerDecision(
            approved=approved, reason="; ".join(reasons), probability=probability,
            probability_active=active, threshold=threshold,
            bayes_loss_probability=bayes_loss, sl_multiplier=sl_mult,
            tp_multiplier=tp_mult, risk_multiplier=risk_mult, features=features,
            market=market, microstructure=micro, collective=collective,
            exit_profile=exit_profile, condition_flags=flags, xai_penalty=xai_penalty,
            xai_loss_frequencies=frequencies,
            latency_ms=(time.perf_counter() - started_at) * 1000.0,
        )


SUPER_LEARNER = SuperLearner()


def log_superlearner_decision(
    connection: sqlite3.Connection,
    symbol: str,
    strategy_id: int | None,
    regime: str,
    side: int,
    decision: SuperLearnerDecision,
) -> None:
    details = decision.as_dict()
    connection.execute(
        """
        INSERT INTO superlearner_decisions(
            timestamp, symbol, strategy_id, regime, side, decision, reason,
            probability, probability_active, threshold, bayes_loss_probability,
            drift_score, hurst, entropy, microstructure_score, sl_multiplier,
            tp_multiplier, risk_multiplier, latency_ms, features_json, details_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            utc_now(), symbol, strategy_id, regime, int(side),
            "APPROVE" if decision.approved else "REJECT", decision.reason,
            decision.probability, 1 if decision.probability_active else 0,
            decision.threshold, decision.bayes_loss_probability,
            safe_float(decision.market.get("drift")), safe_float(decision.market.get("hurst"), 0.50),
            safe_float(decision.market.get("entropy"), 0.50), safe_float(decision.market.get("microstructure_score")),
            decision.sl_multiplier, decision.tp_multiplier, decision.risk_multiplier,
            safe_float(decision.latency_ms), json_text(decision.features), json_text(details),
        ),
    )


def update_open_demo_excursions(connection: sqlite3.Connection) -> None:
    rows = connection.execute("SELECT * FROM demo_positions WHERE status='open'").fetchall()
    for row in rows:
        tick = mt5.symbol_info_tick(str(row["symbol"]))
        if tick is None:
            continue
        side = int(row["side"])
        mark = safe_float(tick.bid if side == 1 else tick.ask)
        entry = safe_float(row["entry_price"])
        stop_distance = abs(entry - safe_float(row["stop_loss"]))
        if stop_distance <= 0:
            continue
        move_r = (mark - entry) * side / stop_distance
        mfe = max(safe_float(row["mfe_r"]), max(0.0, move_r))
        mae = max(safe_float(row["mae_r"]), max(0.0, -move_r))
        connection.execute(
            "UPDATE demo_positions SET mfe_r=?, mae_r=? WHERE id=?",
            (mfe, mae, int(row["id"])),
        )


# ============================================================
# Strategy definitions and generator
# ============================================================


SCALP_FAMILIES = (
    "super_scalp", "scalp", "micro_momentum", "pullback_scalp",
    "breakout_scalp", "mean_revert_scalp",
)
FAMILIES = SCALP_FAMILIES + ("trend", "mean_reversion", "breakout")


def family_is_scalp(family: str) -> bool:
    return str(family) in SCALP_FAMILIES


PARAM_RULES: dict[str, dict[str, tuple[str, float, float, float]]] = {
    "super_scalp": {
        "fast": ("int", 2, 8, 1),
        "slow": ("int", 5, 21, 1),
        "rsi_low": ("int", 38, 49, 1),
        "rsi_high": ("int", 51, 62, 1),
        "stop_atr": ("float", 0.35, 0.90, 0.05),
        "take_atr": ("float", 0.40, 1.30, 0.05),
        "max_hold": ("int", 1, 5, 1),
        "dom_threshold": ("float", 0.02, 0.25, 0.01),
    },
    "scalp": {
        "fast": ("int", 3, 20, 1),
        "slow": ("int", 10, 60, 1),
        "rsi_low": ("int", 38, 49, 1),
        "rsi_high": ("int", 51, 62, 1),
        "stop_atr": ("float", 0.55, 1.40, 0.05),
        "take_atr": ("float", 0.65, 2.00, 0.05),
        "max_hold": ("int", 3, 20, 1),
        "dom_threshold": ("float", 0.00, 0.25, 0.01),
    },
    "micro_momentum": {
        "fast": ("int", 2, 10, 1),
        "slow": ("int", 6, 28, 1),
        "rsi_low": ("int", 36, 48, 1),
        "rsi_high": ("int", 52, 64, 1),
        "min_body_atr": ("float", 0.05, 0.55, 0.05),
        "min_volume_ratio": ("float", 0.70, 2.20, 0.05),
        "stop_atr": ("float", 0.35, 1.00, 0.05),
        "take_atr": ("float", 0.45, 1.60, 0.05),
        "max_hold": ("int", 1, 8, 1),
        "dom_threshold": ("float", 0.00, 0.25, 0.01),
    },
    "pullback_scalp": {
        "fast": ("int", 3, 14, 1),
        "slow": ("int", 12, 55, 1),
        "pullback_atr": ("float", 0.05, 0.65, 0.05),
        "rsi_low": ("int", 38, 49, 1),
        "rsi_high": ("int", 51, 62, 1),
        "stop_atr": ("float", 0.45, 1.15, 0.05),
        "take_atr": ("float", 0.55, 1.80, 0.05),
        "max_hold": ("int", 2, 12, 1),
        "dom_threshold": ("float", 0.00, 0.22, 0.01),
    },
    "breakout_scalp": {
        "lookback": ("int", 3, 30, 1),
        "rsi_low": ("int", 38, 49, 1),
        "rsi_high": ("int", 51, 64, 1),
        "min_break_atr": ("float", 0.00, 0.35, 0.05),
        "min_volume_ratio": ("float", 0.70, 2.20, 0.05),
        "stop_atr": ("float", 0.40, 1.20, 0.05),
        "take_atr": ("float", 0.55, 2.00, 0.05),
        "max_hold": ("int", 2, 12, 1),
        "dom_threshold": ("float", 0.00, 0.25, 0.01),
    },
    "mean_revert_scalp": {
        "lookback": ("int", 5, 35, 1),
        "rsi_buy": ("int", 18, 42, 1),
        "rsi_sell": ("int", 58, 82, 1),
        "z_entry": ("float", 0.55, 2.10, 0.05),
        "stop_atr": ("float", 0.40, 1.20, 0.05),
        "take_atr": ("float", 0.35, 1.50, 0.05),
        "max_hold": ("int", 2, 12, 1),
        "dom_threshold": ("float", 0.00, 0.20, 0.01),
    },
    "trend": {
        "fast": ("int", 8, 45, 1),
        "slow": ("int", 30, 140, 1),
        "rsi_low": ("int", 42, 52, 1),
        "rsi_high": ("int", 48, 58, 1),
        "stop_atr": ("float", 0.90, 2.50, 0.05),
        "take_atr": ("float", 1.20, 4.00, 0.05),
        "max_hold": ("int", 20, 180, 1),
        "dom_threshold": ("float", 0.00, 0.18, 0.01),
    },
    "mean_reversion": {
        "lookback": ("int", 8, 80, 1),
        "rsi_buy": ("int", 18, 40, 1),
        "rsi_sell": ("int", 60, 82, 1),
        "z_entry": ("float", 0.80, 2.50, 0.05),
        "stop_atr": ("float", 0.70, 2.20, 0.05),
        "take_atr": ("float", 0.50, 2.20, 0.05),
        "max_hold": ("int", 5, 60, 1),
        "dom_threshold": ("float", 0.00, 0.18, 0.01),
    },
    "breakout": {
        "lookback": ("int", 5, 100, 1),
        "rsi_low": ("int", 42, 52, 1),
        "rsi_high": ("int", 48, 58, 1),
        "stop_atr": ("float", 0.75, 2.50, 0.05),
        "take_atr": ("float", 1.00, 4.50, 0.05),
        "max_hold": ("int", 10, 140, 1),
        "dom_threshold": ("float", 0.00, 0.20, 0.01),
    },
}


@dataclass(frozen=True)
class StrategyDefinition:
    strategy_id: int
    symbol: str
    family: str
    params: dict[str, Any]


def quantize(value: float, step: float) -> float:
    if step <= 0:
        return value
    return round(round(value / step) * step, 8)


def normalize_params(family: str, params: dict[str, Any]) -> dict[str, Any]:
    if family not in PARAM_RULES:
        raise ValueError(f"Unknown strategy family: {family}")

    rules = PARAM_RULES[family]
    normalized: dict[str, Any] = {}
    for key, (kind, low, high, step) in rules.items():
        raw = params.get(key, low)
        value = clamp(safe_float(raw, low), low, high)
        value = quantize(value, step)
        normalized[key] = int(round(value)) if kind == "int" else float(value)

    if "fast" in normalized and "slow" in normalized:
        fast = int(normalized["fast"])
        slow = int(normalized["slow"])
        if fast >= slow:
            slow_max = int(rules["slow"][2])
            fast_min = int(rules["fast"][1])
            if fast + 2 <= slow_max:
                slow = fast + 2
            else:
                fast = max(fast_min, slow - 2)
        normalized["fast"] = fast
        normalized["slow"] = slow

    if family in {"super_scalp", "scalp", "micro_momentum", "pullback_scalp", "breakout_scalp", "breakout", "trend"}:
        if normalized.get("rsi_low", 0) >= normalized.get("rsi_high", 100):
            midpoint = int((normalized["rsi_low"] + normalized["rsi_high"]) / 2)
            normalized["rsi_low"] = max(1, midpoint - 2)
            normalized["rsi_high"] = min(99, midpoint + 2)

    if family in {"mean_reversion", "mean_revert_scalp"}:
        if normalized["rsi_buy"] >= normalized["rsi_sell"]:
            normalized["rsi_buy"] = 35
            normalized["rsi_sell"] = 65

    return normalized


def random_value(rule: tuple[str, float, float, float]) -> int | float:
    kind, low, high, step = rule
    units = int(round((high - low) / step))
    value = low + random.randint(0, max(0, units)) * step
    value = quantize(value, step)
    return int(round(value)) if kind == "int" else float(value)


def random_strategy(family: str) -> dict[str, Any]:
    params = {key: random_value(rule) for key, rule in PARAM_RULES[family].items()}
    return normalize_params(family, params)


REGIME_FAMILY_WEIGHTS: dict[str, dict[str, float]] = {
    # V6 keeps exploration across all families, but scalping is the primary
    # research objective in every regime. Trend regimes therefore favour
    # micro-momentum/pullback/breakout scalps rather than long-hold trend only.
    "extreme_trend": {
        "micro_momentum": 5.0, "breakout_scalp": 4.5, "pullback_scalp": 3.5,
        "scalp": 2.8, "super_scalp": 1.5, "trend": 1.4, "breakout": 1.1,
        "mean_revert_scalp": 0.4, "mean_reversion": 0.2,
    },
    "volatile_trend": {
        "micro_momentum": 4.8, "breakout_scalp": 4.0, "pullback_scalp": 3.8,
        "scalp": 3.0, "super_scalp": 1.8, "trend": 1.3, "breakout": 1.0,
        "mean_revert_scalp": 0.7, "mean_reversion": 0.3,
    },
    "trend": {
        "pullback_scalp": 4.5, "micro_momentum": 4.2, "breakout_scalp": 3.6,
        "scalp": 3.2, "super_scalp": 1.8, "trend": 1.2, "breakout": 0.9,
        "mean_revert_scalp": 0.8, "mean_reversion": 0.4,
    },
    "volatile_range": {
        "mean_revert_scalp": 4.5, "super_scalp": 4.0, "scalp": 3.8,
        "breakout_scalp": 2.8, "micro_momentum": 2.3, "pullback_scalp": 2.0,
        "mean_reversion": 1.0, "breakout": 0.5, "trend": 0.4,
    },
    "range": {
        "mean_revert_scalp": 5.0, "super_scalp": 4.2, "scalp": 4.0,
        "pullback_scalp": 2.2, "micro_momentum": 1.8, "breakout_scalp": 1.5,
        "mean_reversion": 1.0, "trend": 0.3, "breakout": 0.3,
    },
    "unknown": {
        "super_scalp": 2.5, "scalp": 2.5, "micro_momentum": 2.5,
        "pullback_scalp": 2.5, "breakout_scalp": 2.5, "mean_revert_scalp": 2.5,
        "trend": 0.8, "mean_reversion": 0.8, "breakout": 0.8,
    },
}


def guided_strategy(family: str, regime: str, volatility_ratio: float) -> dict[str, Any]:
    """Create a strategy biased by the currently observed market regime.

    This is still a hypothesis generator, not a profit claim. Historical, OOS,
    walk-forward and live-shadow gates decide whether it can progress.
    """
    params = random_strategy(family)
    volatile = volatility_ratio >= 0.0025
    trend_regime = regime in {"trend", "volatile_trend", "extreme_trend"}
    range_regime = regime in {"range", "volatile_range"}

    if trend_regime and family in {"trend", "breakout", "scalp", "micro_momentum", "pullback_scalp", "breakout_scalp"}:
        if "take_atr" in params:
            params["take_atr"] = safe_float(params["take_atr"]) * 1.15
        if "max_hold" in params:
            params["max_hold"] = int(round(safe_float(params["max_hold"]) * 1.20))
        if "dom_threshold" in params:
            params["dom_threshold"] = safe_float(params["dom_threshold"]) * 0.80

    if range_regime and family in {"mean_reversion", "mean_revert_scalp", "scalp", "super_scalp"}:
        if "take_atr" in params:
            params["take_atr"] = safe_float(params["take_atr"]) * 0.90
        if "max_hold" in params:
            params["max_hold"] = int(round(safe_float(params["max_hold"]) * 0.80))

    if volatile:
        if "stop_atr" in params:
            params["stop_atr"] = safe_float(params["stop_atr"]) * 1.15
        if "take_atr" in params:
            params["take_atr"] = safe_float(params["take_atr"]) * 1.10
    else:
        if "stop_atr" in params:
            params["stop_atr"] = safe_float(params["stop_atr"]) * 0.92

    return normalize_params(family, params)


def live_regime_seed_strategies(count_per_symbol: int) -> int:
    """Generate new hypotheses from the current MT5 market regime."""
    init_database()
    created = 0
    connect_mt5(show_account=False)
    try:
        with db_connect() as connection:
            for symbol in settings.SYMBOLS:
                try:
                    raw = fetch_raw_bars(symbol, settings.LIVE_BARS)
                    frame = add_features(raw, {20, 50})
                    regime = detect_regime(frame)
                    row = frame.iloc[-2]
                    close = max(abs(safe_float(row.get("close"))), 1e-12)
                    volatility_ratio = safe_float(row.get("atr_14")) / close
                    weights_map = REGIME_FAMILY_WEIGHTS.get(
                        regime, REGIME_FAMILY_WEIGHTS["unknown"]
                    )
                    families = list(weights_map)
                    weights: list[float] = []
                    collective_generation_meta: dict[str, Any] = {}
                    session = market_session_tag(
                        symbol, row.get("time") if hasattr(row, "get") else None
                    )
                    for family_name in families:
                        base_weight = safe_float(weights_map[family_name], 1.0)
                        side_cells = [
                            collective_memory_snapshot(
                                connection, symbol, family_name, regime, side
                            )
                            for side in (1, -1)
                        ]
                        total_obs = sum(safe_float(cell.get("observations")) for cell in side_cells)
                        if total_obs > 0:
                            win_prob = sum(
                                safe_float(cell.get("win_probability"), 0.50)
                                * max(1e-9, safe_float(cell.get("observations")))
                                for cell in side_cells
                            ) / max(1e-9, total_obs)
                            reward_ewma = sum(
                                safe_float(cell.get("reward_ewma"))
                                * max(1e-9, safe_float(cell.get("observations")))
                                for cell in side_cells
                            ) / max(1e-9, total_obs)
                            learning_factor = clamp(
                                1.0 + (win_prob - 0.50) * 1.20
                                + 0.20 * math.tanh(reward_ewma),
                                0.55, 1.55,
                            )
                        else:
                            win_prob = 0.50
                            reward_ewma = 0.0
                            learning_factor = 1.0
                        session_cells = [
                            session_family_memory_snapshot(
                                connection, symbol, family_name, regime, session, side
                            )
                            for side in (1, -1)
                        ]
                        session_obs = sum(
                            safe_float(cell.get("observations")) for cell in session_cells
                        )
                        if session_obs > 0:
                            session_win = sum(
                                safe_float(cell.get("win_probability"), 0.50)
                                * max(1e-9, safe_float(cell.get("observations")))
                                for cell in session_cells
                            ) / max(1e-9, session_obs)
                            session_reward = sum(
                                safe_float(cell.get("reward_ewma"))
                                * max(1e-9, safe_float(cell.get("observations")))
                                for cell in session_cells
                            ) / max(1e-9, session_obs)
                            session_factor = clamp(
                                1.0 + (session_win - 0.50) * 1.4
                                + 0.18 * math.tanh(session_reward),
                                0.55, 1.60,
                            )
                        else:
                            session_win = 0.50
                            session_reward = 0.0
                            session_factor = 1.0
                        scalp_factor = (
                            1.15 if family_is_scalp(family_name)
                            and bool(getattr(settings, "SCALP_PRIMARY_MODE", True))
                            else 1.0
                        )
                        weights.append(
                            base_weight * learning_factor * session_factor * scalp_factor
                        )
                        collective_generation_meta[family_name] = {
                            "observations": total_obs,
                            "win_probability": win_prob,
                            "reward_ewma": reward_ewma,
                            "generation_weight_factor": learning_factor,
                            "session": session,
                            "session_observations": session_obs,
                            "session_win_probability": session_win,
                            "session_reward_ewma": session_reward,
                            "session_weight_factor": session_factor,
                            "scalp_priority_factor": scalp_factor,
                        }
                    symbol_created = 0
                    attempts = 0
                    max_attempts = max(count_per_symbol * 5, count_per_symbol + 20)
                    while symbol_created < count_per_symbol and attempts < max_attempts:
                        attempts += 1
                        family = random.choices(families, weights=weights, k=1)[0]
                        params = guided_strategy(family, regime, volatility_ratio)
                        _, is_new = insert_strategy(
                            connection, symbol, family, params, generation=1
                        )
                        if is_new:
                            symbol_created += 1
                            created += 1
                    state_set(
                        connection,
                        f"live_regime:{symbol}",
                        json_text(
                            {
                                "timestamp": utc_now(),
                                "regime": regime,
                                "volatility_ratio": volatility_ratio,
                                "generated": symbol_created,
                                "collective_generation_weights": collective_generation_meta,
                            }
                        ),
                    )
                    connection.commit()
                    print(
                        f"{symbol}: live-guided strategies={symbol_created} | "
                        f"regime={regime} | ATR/price={volatility_ratio:.5f}"
                    )
                except Exception as error:
                    print(f"{symbol}: live-guided generation skipped: {error}")
            connection.commit()
    finally:
        mt5.shutdown()
    print(f"Live-guided generation complete. New unique strategies={created}")
    return created


def mutate_params(family: str, params: dict[str, Any]) -> dict[str, Any]:
    base = normalize_params(family, params)
    rules = PARAM_RULES[family]
    mutated = dict(base)
    keys = random.sample(list(rules), random.randint(1, min(4, len(rules))))
    for key in keys:
        kind, low, high, step = rules[key]
        span = high - low
        current = safe_float(mutated[key], low)
        if random.random() < 0.20:
            new_value = random_value(rules[key])
        else:
            sigma = max(step, span * 0.08)
            new_value = clamp(current + random.gauss(0, sigma), low, high)
            new_value = quantize(new_value, step)
            if kind == "int":
                new_value = int(round(new_value))
        mutated[key] = new_value
    return normalize_params(family, mutated)


def repair_existing_strategies() -> None:
    if not Path(settings.DATABASE_PATH).exists():
        return
    try:
        with db_connect() as connection:
            rows = connection.execute(
                "SELECT id, symbol, family, params_json FROM strategies"
            ).fetchall()
            repaired = 0
            duplicates = 0
            for row in rows:
                try:
                    old = json.loads(row["params_json"])
                    new = normalize_params(str(row["family"]), old)
                except Exception as error:
                    connection.execute(
                        "UPDATE strategies SET status='invalid_params' WHERE id=?",
                        (int(row["id"]),),
                    )
                    connection.execute(
                        """
                        INSERT INTO backtest_errors(
                            strategy_id, symbol, stage, error_type,
                            error_message, traceback_text, created_at
                        ) VALUES (?, ?, 'repair', ?, ?, '', ?)
                        """,
                        (
                            int(row["id"]),
                            row["symbol"],
                            type(error).__name__,
                            str(error),
                            utc_now(),
                        ),
                    )
                    continue

                if old == new:
                    continue
                digest = strategy_hash(row["symbol"], row["family"], new)
                conflict = connection.execute(
                    "SELECT id FROM strategies WHERE strategy_hash=? AND id<>?",
                    (digest, int(row["id"])),
                ).fetchone()
                if conflict:
                    connection.execute(
                        "UPDATE strategies SET status='duplicate_repaired' WHERE id=?",
                        (int(row["id"]),),
                    )
                    duplicates += 1
                else:
                    connection.execute(
                        """
                        UPDATE strategies
                        SET params_json=?, strategy_hash=?, status='generated'
                        WHERE id=?
                        """,
                        (json_text(new), digest, int(row["id"])),
                    )
                    repaired += 1
            if repaired or duplicates:
                print(
                    f"Strategy repair: repaired={repaired}, "
                    f"duplicates_marked={duplicates}"
                )
    except sqlite3.OperationalError:
        # During first schema creation, optional tables may not exist yet.
        return


def insert_strategy(
    connection: sqlite3.Connection,
    symbol: str,
    family: str,
    params: dict[str, Any],
    generation: int = 0,
    parent_id: int | None = None,
) -> tuple[int | None, bool]:
    clean = normalize_params(family, params)
    digest = strategy_hash(symbol, family, clean)
    cursor = connection.execute(
        """
        INSERT OR IGNORE INTO strategies(
            strategy_hash, symbol, family, params_json,
            generation, parent_id, status, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, 'generated', ?)
        """,
        (
            digest,
            symbol,
            family,
            json_text(clean),
            generation,
            parent_id,
            utc_now(),
        ),
    )
    if cursor.rowcount == 0:
        row = connection.execute(
            "SELECT id FROM strategies WHERE strategy_hash=?", (digest,)
        ).fetchone()
        return (int(row["id"]) if row else None, False)
    return int(cursor.lastrowid), True


def generate_strategies(count_per_symbol: int) -> int:
    init_database()
    created = 0
    with db_connect() as connection:
        for symbol in settings.SYMBOLS:
            symbol_created = 0
            symbol_attempts = 0
            max_attempts = max(count_per_symbol * 4, count_per_symbol + 20)
            while symbol_created < count_per_symbol and symbol_attempts < max_attempts:
                symbol_attempts += 1
                if bool(getattr(settings, "SCALP_PRIMARY_MODE", True)) and random.random() < safe_float(getattr(settings, "SCALP_GENERATION_SHARE", 0.82), 0.82):
                    family = random.choice(SCALP_FAMILIES)
                else:
                    family = random.choice(FAMILIES)
                _, is_new = insert_strategy(
                    connection, symbol, family, random_strategy(family)
                )
                if is_new:
                    symbol_created += 1
                    created += 1
            connection.commit()
            print(f"{symbol}: generated new unique strategies={symbol_created}")

        total = connection.execute(
            "SELECT COUNT(*) AS n FROM strategies"
        ).fetchone()["n"]
    print(f"Strategies in database: {total} (new unique: {created})")
    return created


def evolve_top_strategies(
    children_per_parent: int = 3,
    parents_per_symbol: int | None = None,
) -> int:
    init_database()
    parent_limit = parents_per_symbol or settings.EVOLVE_PARENTS_PER_SYMBOL
    created = 0
    with db_connect() as connection:
        for symbol in settings.SYMBOLS:
            parents = connection.execute(
                """
                SELECT s.id, s.symbol, s.family, s.params_json, s.status,
                       s.generation, MAX(b.score) AS best_score,
                       COALESCE(AVG(ss.reward_mean), 0) AS regime_reward,
                       COALESCE(cls.reward_mean, 0) AS shadow_reward,
                       COALESCE(cls.observations, 0) AS shadow_observations,
                       COALESCE(cm.collective_reward, 0) AS collective_reward,
                       COALESCE(cm.collective_observations, 0) AS collective_observations
                FROM strategies s
                JOIN backtests b ON b.strategy_id=s.id
                LEFT JOIN strategy_scores ss ON ss.strategy_id=s.id
                LEFT JOIN candidate_live_scores cls ON cls.strategy_id=s.id
                LEFT JOIN (
                    SELECT symbol, family,
                           AVG(reward_ewma) AS collective_reward,
                           SUM(effective_observations) AS collective_observations
                    FROM collective_market_memory
                    GROUP BY symbol, family
                ) cm ON cm.symbol=s.symbol AND cm.family=s.family
                WHERE s.symbol=?
                  AND s.status IN (
                      'shadow_approved', 'historical_validated',
                      'backtest_rejected', 'shadow_rejected'
                  )
                GROUP BY s.id
                ORDER BY
                    CASE WHEN s.family IN (
                        'super_scalp','scalp','micro_momentum','pullback_scalp',
                        'breakout_scalp','mean_revert_scalp'
                    ) THEN 0 ELSE 1 END,
                    CASE s.status
                        WHEN 'shadow_approved' THEN 0
                        WHEN 'historical_validated' THEN 1
                        WHEN 'backtest_rejected' THEN 2
                        ELSE 3
                    END,
                    (best_score + regime_reward * 4.0 + shadow_reward * 5.0
                     + MIN(shadow_observations, 30) * 0.03
                     + collective_reward * 2.0
                     + MIN(collective_observations, 100) * 0.01) DESC
                LIMIT ?
                """,
                (symbol, parent_limit),
            ).fetchall()
            for parent in parents:
                for _ in range(children_per_parent):
                    child = mutate_params(
                        parent["family"], json.loads(parent["params_json"])
                    )
                    _, is_new = insert_strategy(
                        connection,
                        parent["symbol"],
                        parent["family"],
                        child,
                        generation=int(parent["generation"]) + 1,
                        parent_id=int(parent["id"]),
                    )
                    if is_new:
                        created += 1
            connection.commit()
            print(f"{symbol}: evolution parents={len(parents)}")
    print(f"Evolution complete. New unique children={created}")
    return created


def load_strategy(row: sqlite3.Row) -> StrategyDefinition:
    return StrategyDefinition(
        strategy_id=int(row["id"]),
        symbol=str(row["symbol"]),
        family=str(row["family"]),
        params=normalize_params(str(row["family"]), json.loads(row["params_json"])),
    )




def _v8_native_family(playbook: str) -> str:
    return {
        "momentum_burst": "micro_momentum",
        "impulse_pullback_resume": "pullback_scalp",
        "squeeze_breakout": "breakout_scalp",
        "wick_rejection": "mean_revert_scalp",
        "exhaustion_snapback": "mean_revert_scalp",
        "trend_pullback": "pullback_scalp",
        "momentum_breakout": "breakout_scalp",
        "squeeze_release": "breakout_scalp",
        "liquidity_sweep_reclaim": "mean_revert_scalp",
        "failed_breakout_reversal": "mean_revert_scalp",
        "vwap_mean_reversion": "mean_revert_scalp",
        "range_edge_rotation": "mean_revert_scalp",
    }.get(str(playbook), "scalp")


def _v8_native_params(setup: micro_hunter.MicroSetup) -> dict[str, Any]:
    """Bounded specialist parameters for DEMO-only institutional alpha probes.

    The entry itself comes from the live playbook/state machine. These parameters
    exist so the established risk, SuperLearner, exit and audit stack can keep
    using a normal StrategyDefinition without pretending a generated strategy
    has historical approval it did not earn.
    """
    family = _v8_native_family(setup.playbook)
    base: dict[str, Any]
    if family == "micro_momentum":
        base = {"fast": 4, "slow": 11, "rsi_low": 42, "rsi_high": 58,
                "min_body_atr": 0.15, "min_volume_ratio": 0.85,
                "stop_atr": setup.stop_atr, "take_atr": setup.take_atr,
                "max_hold": setup.max_hold_bars, "dom_threshold": 0.04}
    elif family == "pullback_scalp":
        base = {"fast": 5, "slow": 18, "pullback_atr": 0.25,
                "rsi_low": 42, "rsi_high": 58, "stop_atr": setup.stop_atr,
                "take_atr": setup.take_atr, "max_hold": setup.max_hold_bars,
                "dom_threshold": 0.03}
    elif family == "breakout_scalp":
        base = {"lookback": 8, "rsi_low": 42, "rsi_high": 60,
                "min_break_atr": 0.05, "min_volume_ratio": 0.85,
                "stop_atr": setup.stop_atr, "take_atr": setup.take_atr,
                "max_hold": setup.max_hold_bars, "dom_threshold": 0.04}
    elif family == "mean_revert_scalp":
        base = {"lookback": 12, "rsi_buy": 34, "rsi_sell": 66, "z_entry": 0.90,
                "stop_atr": setup.stop_atr, "take_atr": setup.take_atr,
                "max_hold": setup.max_hold_bars, "dom_threshold": 0.03}
    else:
        base = {"fast": 6, "slow": 22, "rsi_low": 42, "rsi_high": 58,
                "stop_atr": setup.stop_atr, "take_atr": setup.take_atr,
                "max_hold": setup.max_hold_bars, "dom_threshold": 0.03}
    return normalize_params(family, base)


def v8_native_alpha_strategy(
    connection: sqlite3.Connection,
    setup: micro_hunter.MicroSetup,
) -> StrategyDefinition:
    """Return a persistent DEMO-only native playbook carrier.

    This removes V7's dependency on having a matching approved/trial strategy
    before a high-quality micro setup can even reach the downstream intelligence
    stack. It does *not* bypass Spartan, adaptive/Bayesian learning, SuperLearner,
    Edge Recovery, Luna, broker risk checks or the DEMO hard lock.
    """
    family = _v8_native_family(setup.playbook)
    params = _v8_native_params(setup)
    digest = f"v8-native-alpha:{setup.symbol}:{setup.playbook}"
    now = utc_now()
    connection.execute(
        """
        INSERT OR IGNORE INTO strategies(
            strategy_hash,symbol,family,params_json,generation,parent_id,status,created_at
        ) VALUES (?,?,?,?,0,NULL,'v8_native_demo',?)
        """,
        (digest, setup.symbol, family, json_text(params), now),
    )
    connection.execute(
        "UPDATE strategies SET family=?,params_json=?,status='v8_native_demo' WHERE strategy_hash=?",
        (family, json_text(params), digest),
    )
    row = connection.execute(
        "SELECT id,symbol,family,params_json FROM strategies WHERE strategy_hash=?",
        (digest,),
    ).fetchone()
    if row is None:
        raise RuntimeError("Could not create V8 native DEMO alpha carrier")
    return load_strategy(row)


def v8_native_daily_trade_count(connection: sqlite3.Connection, symbol: str | None = None) -> int:
    today = utc_date()
    if symbol:
        row = connection.execute(
            """SELECT COUNT(*) AS n FROM demo_positions
               WHERE substr(opened_at,1,10)=? AND symbol=? AND execution_tier='v8_native_alpha'""",
            (today, symbol),
        ).fetchone()
    else:
        row = connection.execute(
            """SELECT COUNT(*) AS n FROM demo_positions
               WHERE substr(opened_at,1,10)=? AND execution_tier='v8_native_alpha'""",
            (today,),
        ).fetchone()
    return int(row["n"] if row else 0)


def required_ema_lengths(definitions: Iterable[StrategyDefinition]) -> set[int]:
    lengths = {20, 50}
    for definition in definitions:
        for key in ("fast", "slow"):
            if key in definition.params:
                lengths.add(int(definition.params[key]))
    return lengths


# ============================================================
# MT5 connection and market data
# ============================================================


def connect_mt5(show_account: bool = True) -> Any:
    if not mt5.initialize():
        raise ConnectionError(f"MT5 initialize failed: {mt5.last_error()}")
    account = mt5.account_info()
    if account is None:
        mt5.shutdown()
        raise ConnectionError(f"account_info failed: {mt5.last_error()}")
    if show_account:
        print(
            f"Connected | server={account.server} | login={account.login} | "
            f"balance={account.balance:.2f} | equity={account.equity:.2f}"
        )
        if safe_float(account.equity) <= 0:
            print(
                "WARNING: connected account equity is 0.00. Research can run, "
                "but DEMO orders cannot be sized until a funded demo account is selected."
            )
    return account


def verify_demo_account() -> Any:
    account = mt5.account_info()
    if account is None:
        raise RuntimeError("No MT5 account information.")
    demo_code = getattr(mt5, "ACCOUNT_TRADE_MODE_DEMO", 0)
    if settings.DEMO_ONLY_HARD_LOCK and int(account.trade_mode) != int(demo_code):
        raise RuntimeError(
            "HARD BLOCK: current MT5 account is not DEMO. No order was sent."
        )
    if not bool(getattr(account, "trade_allowed", True)):
        raise RuntimeError("Trading is not allowed on the connected MT5 account.")
    if safe_float(account.equity) <= 0:
        raise RuntimeError(
            "DEMO account equity is 0.00. Select/fund the intended demo account "
            "before starting demo autopilot."
        )
    return account


def prepare_symbol(symbol: str) -> Any:
    info = mt5.symbol_info(symbol)
    if info is None:
        raise ValueError(f"Symbol not found: {symbol}")
    if not info.visible and not mt5.symbol_select(symbol, True):
        raise RuntimeError(f"Cannot select {symbol}: {mt5.last_error()}")
    refreshed = mt5.symbol_info(symbol)
    if refreshed is None:
        raise RuntimeError(f"Cannot refresh symbol info: {symbol}")
    return refreshed


def _copy_rates_chunk(
    symbol: str,
    start_pos: int,
    count: int,
) -> Any | None:
    """Read one MT5 history chunk with retries.

    Some terminals reject a very large first request with RES_E_INVALID_PARAMS.
    Small paged requests are also less likely to time out while MT5 is still
    synchronising history from the broker.
    """
    attempts = max(1, int(getattr(settings, "HISTORY_FETCH_RETRIES", 4)))
    delay = max(0.1, float(getattr(settings, "HISTORY_RETRY_SECONDS", 1.0)))
    last_error: Any = None

    for attempt in range(1, attempts + 1):
        rates = mt5.copy_rates_from_pos(
            symbol, settings.TIMEFRAME, int(start_pos), int(count)
        )
        if rates is not None and len(rates) > 0:
            return rates

        last_error = mt5.last_error()
        if attempt < attempts:
            # The first unsuccessful call can start terminal-side history sync.
            time.sleep(delay * attempt)

    return None


def fetch_raw_bars(symbol: str, count: int) -> pd.DataFrame:
    """Fetch MT5 bars in safe chunks and return all available history.

    The requested amount is a target, not a promise: MT5 can only return bars
    available in the terminal/broker history and inside Max bars in chart.
    """
    prepare_symbol(symbol)
    requested = min(
        max(250, int(count)),
        int(getattr(settings, "MAX_HISTORY_DOWNLOAD_BARS", int(count))),
    )

    terminal = mt5.terminal_info()
    terminal_maxbars = int(getattr(terminal, "maxbars", 0) or 0)
    target = min(requested, terminal_maxbars) if terminal_maxbars > 0 else requested
    chunk_size = min(
        target,
        max(250, int(getattr(settings, "HISTORY_CHUNK_BARS", 5_000))),
    )

    pieces: list[pd.DataFrame] = []
    start_pos = 0
    last_error: Any = None

    while start_pos < target:
        remaining = target - start_pos
        normal_size = min(chunk_size, remaining)
        # If a terminal rejects a chunk, retry the same position with smaller
        # requests. This specifically handles last_error=(-2, Invalid params).
        sizes: list[int] = []
        for size in (normal_size, min(2_000, remaining), min(500, remaining), min(250, remaining)):
            if size > 0 and size not in sizes:
                sizes.append(size)

        rates = None
        used_size = 0
        for size in sizes:
            rates = _copy_rates_chunk(symbol, start_pos, size)
            last_error = mt5.last_error()
            if rates is not None and len(rates) > 0:
                used_size = size
                break

        if rates is None or len(rates) == 0:
            # Reaching beyond broker/terminal history is normal after at least
            # one successful chunk; keep what was downloaded.
            if pieces:
                break
            raise RuntimeError(
                f"No usable M1 history for {symbol}. "
                f"requested={requested}, first_chunk={normal_size}, "
                f"last_error={last_error}. Open the {symbol} M1 chart in MT5, "
                "confirm the account is connected, set Tools > Options > Charts "
                "> Max bars in chart to 100000 or more, then run again."
            )

        part = pd.DataFrame(rates)
        if part.empty or "time" not in part.columns:
            if pieces:
                break
            raise RuntimeError(
                f"MT5 returned malformed history for {symbol}; "
                f"last_error={last_error}"
            )
        part["time"] = pd.to_datetime(part["time"], unit="s", utc=True)
        pieces.append(part)

        received = len(part)
        start_pos += received
        if start_pos == received or start_pos % 20_000 < received:
            print(f"  {symbol} history progress: {start_pos}/{target} bars")

        # A short page means the oldest available terminal history was reached.
        if received < used_size:
            break
        time.sleep(0.03)

    if not pieces:
        raise RuntimeError(
            f"Insufficient bars for {symbol}: received=0, last_error={last_error}"
        )

    frame = pd.concat(pieces, ignore_index=True)
    frame = frame.sort_values("time").drop_duplicates("time").reset_index(drop=True)
    if len(frame) < 250:
        raise RuntimeError(
            f"Insufficient bars for {symbol}: received={len(frame)}, "
            f"last_error={last_error}"
        )
    if len(frame) < requested:
        print(
            f"  {symbol}: MT5 supplied {len(frame)} of requested {requested} bars; "
            "using all available history."
        )
    return frame


def history_path(symbol: str) -> Path:
    folder = settings.DATA_DIR / symbol
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"bars_{settings.TIMEFRAME_NAME}.csv"


def merge_and_save_history(symbol: str, fresh: pd.DataFrame) -> Path:
    path = history_path(symbol)
    raw_columns = [
        "time", "open", "high", "low", "close",
        "tick_volume", "spread", "real_volume",
    ]
    fresh = fresh[[c for c in raw_columns if c in fresh.columns]].copy()
    if path.exists():
        try:
            old = pd.read_csv(path)
            old["time"] = pd.to_datetime(old["time"], utc=True)
            combined = pd.concat([old, fresh], ignore_index=True)
            combined = combined.drop_duplicates("time", keep="last")
            combined = combined.sort_values("time").reset_index(drop=True)
        except Exception:
            combined = fresh
    else:
        combined = fresh
    combined.to_csv(path, index=False)
    return path


def load_cached_history(symbol: str) -> pd.DataFrame | None:
    path = history_path(symbol)
    if not path.exists():
        return None
    frame = pd.read_csv(path)
    if len(frame) < 250:
        return None
    frame["time"] = pd.to_datetime(frame["time"], utc=True)
    return frame.sort_values("time").drop_duplicates("time").reset_index(drop=True)


def download_history(bars: int) -> None:
    init_database()
    connect_mt5()
    successes = 0
    failures: list[str] = []
    try:
        requested = min(max(250, bars), settings.MAX_HISTORY_DOWNLOAD_BARS)
        for symbol in settings.SYMBOLS:
            print(f"Downloading {symbol}: requested bars={requested} (chunked)")
            try:
                frame = fetch_raw_bars(symbol, requested)
                path = merge_and_save_history(symbol, frame)
                with db_connect() as connection:
                    state_set(
                        connection, f"last_full_history_attempt:{symbol}", utc_now()
                    )
                    connection.commit()
                successes += 1
                print(
                    f"Saved {symbol}: bars={len(frame)}, "
                    f"from={frame['time'].iloc[0]}, to={frame['time'].iloc[-1]} | {path}"
                )
            except Exception as error:
                failures.append(f"{symbol}: {error}")
                print(f"HISTORY SKIP {symbol}: {error}")
    finally:
        mt5.shutdown()

    if failures:
        print("\nHistory warnings:")
        for failure in failures:
            print(f"- {failure}")
    if successes == 0:
        raise RuntimeError(
            "MT5 returned no usable history for any configured symbol. "
            "Check the logged-in account, broker connection and M1 charts."
        )
    print(f"\nHistory download complete: success={successes}, failed={len(failures)}")


def diagnose_mt5() -> None:
    init_database()
    account = connect_mt5()
    try:
        terminal = mt5.terminal_info()
        print("\nMT5 DIAGNOSTIC")
        print(
            f"trade_mode={getattr(account, 'trade_mode', None)} | "
            f"trade_allowed={getattr(account, 'trade_allowed', None)} | "
            f"terminal_connected={getattr(terminal, 'connected', None)} | "
            f"maxbars={getattr(terminal, 'maxbars', None)}"
        )
        for symbol in settings.SYMBOLS:
            try:
                info = prepare_symbol(symbol)
                tick = mt5.symbol_info_tick(symbol)
                rates = _copy_rates_chunk(symbol, 0, 10)
                bars = 0 if rates is None else len(rates)
                print(
                    f"{symbol}: visible={getattr(info, 'visible', None)} | "
                    f"tick={'yes' if tick is not None else 'no'} | "
                    f"M1_bars_test={bars} | last_error={mt5.last_error()}"
                )
            except Exception as error:
                print(f"{symbol}: diagnostic error: {error}")
    finally:
        mt5.shutdown()


def verify_demo_connection() -> None:
    connect_mt5()
    try:
        account = verify_demo_account()
        print(
            f"DEMO HARD-LOCK CHECK PASSED | server={account.server} | "
            f"trade_mode={account.trade_mode} | equity={account.equity:.2f}"
        )
    finally:
        mt5.shutdown()


def order_book_imbalance(symbol: str) -> float | None:
    if not mt5.market_book_add(symbol):
        return None
    try:
        time.sleep(0.03)
        book = mt5.market_book_get(symbol)
        if not book:
            return None
        buy_types = {
            getattr(mt5, "BOOK_TYPE_BUY", 2),
            getattr(mt5, "BOOK_TYPE_BUY_MARKET", 4),
        }
        sell_types = {
            getattr(mt5, "BOOK_TYPE_SELL", 1),
            getattr(mt5, "BOOK_TYPE_SELL_MARKET", 3),
        }
        buy_volume = 0.0
        sell_volume = 0.0
        for item in book:
            volume = safe_float(
                getattr(item, "volume_dbl", getattr(item, "volume", 0.0))
            )
            if item.type in buy_types:
                buy_volume += volume
            elif item.type in sell_types:
                sell_volume += volume
        total = buy_volume + sell_volume
        return None if total <= 0 else (buy_volume - sell_volume) / total
    finally:
        mt5.market_book_release(symbol)


# ============================================================
# Features and market regime
# ============================================================


def ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=max(2, int(length)), adjust=False).mean()


def rsi(series: pd.Series, length: int = 14) -> pd.Series:
    delta = series.diff()
    gains = delta.clip(lower=0).ewm(alpha=1 / length, adjust=False).mean()
    losses = (-delta.clip(upper=0)).ewm(alpha=1 / length, adjust=False).mean()
    relative = gains / losses.replace(0, np.nan)
    value = 100.0 - (100.0 / (1.0 + relative))
    value = value.where(~((losses == 0) & (gains > 0)), 100.0)
    value = value.where(~((gains == 0) & (losses > 0)), 0.0)
    value = value.where(~((gains == 0) & (losses == 0)), 50.0)
    return value


def atr(frame: pd.DataFrame, length: int = 14) -> pd.Series:
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


def add_features(
    frame: pd.DataFrame,
    ema_lengths: Iterable[int] | None = None,
) -> pd.DataFrame:
    result = frame.copy()
    lengths = sorted({20, 50, *(int(x) for x in (ema_lengths or []))})
    ema_columns = {
        f"ema_{length}": ema(result["close"], length)
        for length in lengths
    }
    result = pd.concat([result, pd.DataFrame(ema_columns, index=result.index)], axis=1)
    result["rsi_5"] = rsi(result["close"], 5)
    result["rsi_7"] = rsi(result["close"], 7)
    result["rsi_14"] = rsi(result["close"], 14)
    result["atr_14"] = atr(result, 14)
    result["return_1"] = result["close"].pct_change()
    result["volatility_30"] = result["return_1"].rolling(30).std()
    result["atr_ratio"] = result["atr_14"] / result["close"].replace(0, np.nan)
    volume_base = result["tick_volume"].rolling(20).mean().replace(0, np.nan) if "tick_volume" in result.columns else pd.Series(np.nan, index=result.index)
    result["volume_ratio_20"] = result["tick_volume"] / volume_base if "tick_volume" in result.columns else 1.0
    result["bar_body_atr"] = (result["close"] - result["open"]).abs() / result["atr_14"].replace(0, np.nan)
    result["bar_range_atr"] = (result["high"] - result["low"]).abs() / result["atr_14"].replace(0, np.nan)
    result["momentum_3_atr"] = (result["close"] - result["close"].shift(3)) / result["atr_14"].replace(0, np.nan)
    # Spartan-Pro canonical indicators (EMA5/13/200 + ADX14) are additive;
    # existing strategy features and historical behavior remain available.
    result = spartan.ensure_features(result)
    return result.replace([np.inf, -np.inf], np.nan)


def detect_regime(frame: pd.DataFrame) -> str:
    clean = frame.dropna(subset=["ema_20", "ema_50", "atr_14", "atr_ratio"])
    if len(clean) < 100:
        return "unknown"
    row = clean.iloc[-2]
    atr_value = safe_float(row["atr_14"])
    if atr_value <= 0:
        return "unknown"
    trend_strength = abs(safe_float(row["ema_20"] - row["ema_50"])) / atr_value
    recent_atr = clean["atr_ratio"].tail(300)
    high_volatility = safe_float(row["atr_ratio"]) >= safe_float(
        recent_atr.quantile(0.70)
    )
    extreme_volatility = safe_float(row["atr_ratio"]) >= safe_float(
        recent_atr.quantile(0.90)
    )
    if extreme_volatility and trend_strength >= 0.80:
        return "extreme_trend"
    if trend_strength >= 0.80 and high_volatility:
        return "volatile_trend"
    if trend_strength >= 0.55:
        return "trend"
    if high_volatility:
        return "volatile_range"
    return "range"


# ============================================================
# Signals
# ============================================================


def row_signal(
    frame: pd.DataFrame,
    index: int,
    definition: StrategyDefinition,
    dom_imbalance: float | None = None,
) -> int:
    if index < 150 or index >= len(frame):
        return 0
    row = frame.iloc[index]
    prev = frame.iloc[index - 1]
    params = definition.params
    family = definition.family
    if not np.isfinite(safe_float(row.get("atr_14"), np.nan)):
        return 0

    dom_threshold = safe_float(params.get("dom_threshold", 0.0))
    dom_buy_ok = dom_imbalance is None or dom_imbalance >= dom_threshold
    dom_sell_ok = dom_imbalance is None or dom_imbalance <= -dom_threshold

    if family in {"super_scalp", "scalp"}:
        fast = int(params["fast"])
        slow = int(params["slow"])
        rsi_name = "rsi_5" if family == "super_scalp" else "rsi_7"
        required = [f"ema_{fast}", f"ema_{slow}", rsi_name]
        if any(name not in frame.columns for name in required):
            raise KeyError(f"Missing feature(s): {required}")
        buy = (
            prev[f"ema_{fast}"] <= prev[f"ema_{slow}"]
            and row[f"ema_{fast}"] > row[f"ema_{slow}"]
            and row[rsi_name] >= float(params["rsi_high"])
            and dom_buy_ok
        )
        sell = (
            prev[f"ema_{fast}"] >= prev[f"ema_{slow}"]
            and row[f"ema_{fast}"] < row[f"ema_{slow}"]
            and row[rsi_name] <= float(params["rsi_low"])
            and dom_sell_ok
        )
    elif family == "micro_momentum":
        fast = int(params["fast"])
        slow = int(params["slow"])
        required = [f"ema_{fast}", f"ema_{slow}", "rsi_5", "bar_body_atr", "volume_ratio_20", "momentum_3_atr"]
        if any(name not in frame.columns for name in required):
            raise KeyError(f"Missing feature(s): {required}")
        min_body = float(params["min_body_atr"])
        min_volume = float(params["min_volume_ratio"])
        buy = (
            row[f"ema_{fast}"] > row[f"ema_{slow}"]
            and safe_float(row["momentum_3_atr"]) > 0
            and safe_float(row["bar_body_atr"]) >= min_body
            and safe_float(row["volume_ratio_20"], 1.0) >= min_volume
            and row["rsi_5"] >= float(params["rsi_high"])
            and dom_buy_ok
        )
        sell = (
            row[f"ema_{fast}"] < row[f"ema_{slow}"]
            and safe_float(row["momentum_3_atr"]) < 0
            and safe_float(row["bar_body_atr"]) >= min_body
            and safe_float(row["volume_ratio_20"], 1.0) >= min_volume
            and row["rsi_5"] <= float(params["rsi_low"])
            and dom_sell_ok
        )
    elif family == "pullback_scalp":
        fast = int(params["fast"])
        slow = int(params["slow"])
        required = [f"ema_{fast}", f"ema_{slow}", "rsi_7", "atr_14"]
        if any(name not in frame.columns for name in required):
            raise KeyError(f"Missing feature(s): {required}")
        atrv = max(1e-12, safe_float(row["atr_14"]))
        pullback = float(params["pullback_atr"]) * atrv
        buy = (
            row[f"ema_{fast}"] > row[f"ema_{slow}"]
            and safe_float(prev["low"]) <= safe_float(prev[f"ema_{fast}"]) + pullback
            and safe_float(row["close"]) > safe_float(row[f"ema_{fast}"])
            and safe_float(row["rsi_7"]) >= float(params["rsi_high"])
            and dom_buy_ok
        )
        sell = (
            row[f"ema_{fast}"] < row[f"ema_{slow}"]
            and safe_float(prev["high"]) >= safe_float(prev[f"ema_{fast}"]) - pullback
            and safe_float(row["close"]) < safe_float(row[f"ema_{fast}"])
            and safe_float(row["rsi_7"]) <= float(params["rsi_low"])
            and dom_sell_ok
        )
    elif family == "breakout_scalp":
        lookback = int(params["lookback"])
        prior_high = frame["high"].iloc[index - lookback : index].max()
        prior_low = frame["low"].iloc[index - lookback : index].min()
        atrv = max(1e-12, safe_float(row["atr_14"]))
        break_buffer = float(params["min_break_atr"]) * atrv
        volume_ok = safe_float(row.get("volume_ratio_20"), 1.0) >= float(params["min_volume_ratio"])
        buy = (
            safe_float(row["close"]) > safe_float(prior_high) + break_buffer
            and safe_float(row["rsi_7"]) >= float(params["rsi_high"])
            and volume_ok and dom_buy_ok
        )
        sell = (
            safe_float(row["close"]) < safe_float(prior_low) - break_buffer
            and safe_float(row["rsi_7"]) <= float(params["rsi_low"])
            and volume_ok and dom_sell_ok
        )
    elif family == "mean_revert_scalp":
        lookback = int(params["lookback"])
        window = frame["close"].iloc[index - lookback + 1 : index + 1]
        mean_price = safe_float(window.mean())
        deviation = safe_float(window.std())
        if deviation <= 0:
            return 0
        z_entry = float(params["z_entry"])
        buy = (
            row["close"] < mean_price - z_entry * deviation
            and row["rsi_5"] <= float(params["rsi_buy"])
            and dom_buy_ok
        )
        sell = (
            row["close"] > mean_price + z_entry * deviation
            and row["rsi_5"] >= float(params["rsi_sell"])
            and dom_sell_ok
        )
    elif family == "trend":
        fast = int(params["fast"])
        slow = int(params["slow"])
        buy = (
            row[f"ema_{fast}"] > row[f"ema_{slow}"]
            and row["close"] > row[f"ema_{fast}"]
            and row["rsi_14"] >= float(params["rsi_high"])
            and dom_buy_ok
        )
        sell = (
            row[f"ema_{fast}"] < row[f"ema_{slow}"]
            and row["close"] < row[f"ema_{fast}"]
            and row["rsi_14"] <= float(params["rsi_low"])
            and dom_sell_ok
        )
    elif family == "mean_reversion":
        lookback = int(params["lookback"])
        window = frame["close"].iloc[index - lookback + 1 : index + 1]
        mean_price = safe_float(window.mean())
        deviation = safe_float(window.std())
        if deviation <= 0:
            return 0
        z_entry = float(params["z_entry"])
        buy = (
            row["close"] < mean_price - z_entry * deviation
            and row["rsi_14"] <= float(params["rsi_buy"])
            and dom_buy_ok
        )
        sell = (
            row["close"] > mean_price + z_entry * deviation
            and row["rsi_14"] >= float(params["rsi_sell"])
            and dom_sell_ok
        )
    elif family == "breakout":
        lookback = int(params["lookback"])
        prior_high = frame["high"].iloc[index - lookback : index].max()
        prior_low = frame["low"].iloc[index - lookback : index].min()
        buy = (
            row["close"] > prior_high
            and row["rsi_14"] >= float(params["rsi_high"])
            and dom_buy_ok
        )
        sell = (
            row["close"] < prior_low
            and row["rsi_14"] <= float(params["rsi_low"])
            and dom_sell_ok
        )
    else:
        return 0

    if bool(buy) and not bool(sell):
        return 1
    if bool(sell) and not bool(buy):
        return -1
    return 0


# ============================================================
# Backtester and validation
# ============================================================


@dataclass
class Trade:
    side: int
    entry: float
    exit: float
    pnl: float
    reason: str
    r_multiple: float
    bars_held: int


def approximate_spread_price(row: pd.Series, symbol_info: Any) -> float:
    return max(0.0, safe_float(row.get("spread", 0.0)) * safe_float(symbol_info.point))


def simulate_backtest(
    frame: pd.DataFrame,
    definition: StrategyDefinition,
    starting_balance: float,
    start_index: int = 150,
    end_index: int | None = None,
) -> dict[str, Any]:
    info = prepare_symbol(definition.symbol)
    params = definition.params
    balance = float(starting_balance)
    peak = balance
    max_drawdown = 0.0
    trades: list[Trade] = []
    stress_r_values: list[float] = []
    position: dict[str, Any] | None = None
    final_index = min(len(frame) - 1, end_index if end_index is not None else len(frame) - 1)

    for i in range(max(150, start_index), final_index):
        signal_row = frame.iloc[i]
        next_row = frame.iloc[i + 1]
        atr_value = safe_float(signal_row.get("atr_14"))
        if atr_value <= 0:
            continue

        if position is not None:
            position["bars_held"] += 1
            side = int(position["side"])
            stop = float(position["stop"])
            take = float(position["take"])
            exit_price: float | None = None
            reason = ""
            if side == 1:
                stop_hit = safe_float(next_row["low"]) <= stop
                take_hit = safe_float(next_row["high"]) >= take
            else:
                stop_hit = safe_float(next_row["high"]) >= stop
                take_hit = safe_float(next_row["low"]) <= take
            if stop_hit:
                exit_price, reason = stop, "stop"
            elif take_hit:
                exit_price, reason = take, "take"
            elif position["bars_held"] >= int(params["max_hold"]):
                exit_price, reason = safe_float(next_row["open"]), "time"

            if exit_price is not None:
                price_move = (exit_price - position["entry"]) * side
                risk_cash = balance * settings.RISK_PER_TRADE
                stop_distance = abs(position["entry"] - position["stop"])
                r_multiple = price_move / stop_distance if stop_distance > 0 else 0.0
                pnl = risk_cash * r_multiple
                balance += pnl
                trades.append(
                    Trade(
                        side, position["entry"], exit_price, pnl, reason,
                        r_multiple, int(position["bars_held"]),
                    )
                )
                stress_r_values.append(
                    r_multiple - safe_float(position.get("lab_cost_stress_r"), 0.0)
                )
                position = None
                peak = max(peak, balance)
                drawdown = (peak - balance) / peak if peak else 0.0
                max_drawdown = max(max_drawdown, drawdown)
                if max_drawdown >= safe_float(
                    getattr(settings, "BACKTEST_ABORT_DRAWDOWN_PCT", settings.MAX_DRAWDOWN_PCT),
                    settings.MAX_DRAWDOWN_PCT,
                ):
                    break

        if position is None:
            signal = row_signal(frame, i, definition, None)
            if signal == 0:
                continue
            spread = approximate_spread_price(next_row, info)
            if spread > atr_value * settings.MAX_SPREAD_ATR_FRACTION:
                continue
            if signal == 1:
                entry = safe_float(next_row["open"]) + spread
                stop = entry - float(params["stop_atr"]) * atr_value
                take = entry + float(params["take_atr"]) * atr_value
            else:
                entry = safe_float(next_row["open"]) - spread
                stop = entry + float(params["stop_atr"]) * atr_value
                take = entry - float(params["take_atr"]) * atr_value
            position = {
                "side": signal,
                "entry": entry,
                "stop": stop,
                "take": take,
                "bars_held": 0,
                "lab_cost_stress_r": scalp_lab.conservative_cost_stress_r(
                    spread, abs(entry - stop)
                ),
            }

    wins = sum(1 for trade in trades if trade.pnl > 0)
    losses = len(trades) - wins
    gross_profit = sum(max(0.0, trade.pnl) for trade in trades)
    gross_loss = abs(sum(min(0.0, trade.pnl) for trade in trades))
    net_profit = balance - starting_balance
    return_pct = net_profit / starting_balance if starting_balance else 0.0
    win_rate = wins / len(trades) if trades else 0.0
    profit_factor = (
        gross_profit / gross_loss
        if gross_loss > 0
        else (5.0 if gross_profit > 0 else 0.0)
    )
    r_values = [safe_float(trade.r_multiple) for trade in trades]
    hold_values = [max(1, int(trade.bars_held)) for trade in trades]
    expectancy_r = sum(r_values) / len(r_values) if r_values else 0.0
    stress_expectancy_r = sum(stress_r_values) / len(stress_r_values) if stress_r_values else expectancy_r
    stress_gross_win = sum(max(0.0, value) for value in stress_r_values)
    stress_gross_loss = abs(sum(min(0.0, value) for value in stress_r_values))
    stress_profit_factor = (
        stress_gross_win / stress_gross_loss
        if stress_gross_loss > 0
        else (5.0 if stress_gross_win > 0 else 0.0)
    )
    avg_cost_stress_r = (
        sum(max(0.0, raw - stressed) for raw, stressed in zip(r_values, stress_r_values)) / len(stress_r_values)
        if stress_r_values else 0.0
    )
    avg_hold_bars = sum(hold_values) / len(hold_values) if hold_values else 0.0
    median_hold_bars = float(np.median(hold_values)) if hold_values else 0.0
    tested_bars = max(1, final_index - max(150, start_index))
    trades_per_1000_bars = len(trades) * 1000.0 / tested_bars
    sample_penalty = min(1.0, len(trades) / 50.0)
    score = (
        return_pct * 100.0
        + min(profit_factor, 3.0) * 3.0
        + win_rate * 4.0
        - max_drawdown * 100.0 * 2.5
    ) * sample_penalty
    if family_is_scalp(definition.family):
        hold_target = (
            safe_float(getattr(settings, "SUPER_SCALP_MAX_AVG_HOLD_BARS", 7.0), 7.0)
            if definition.family == "super_scalp"
            else safe_float(getattr(settings, "SCALP_MAX_AVG_HOLD_BARS", 15.0), 15.0)
        )
        hold_quality = clamp((hold_target - avg_hold_bars) / max(1.0, hold_target), -1.0, 1.0)
        activity_quality = clamp(trades_per_1000_bars / 2.0, 0.0, 1.5)
        score += (
            safe_float(getattr(settings, "SCALP_FAMILY_SCORE_BONUS", 4.0), 4.0)
            + 4.0 * math.tanh(expectancy_r)
            + 2.0 * hold_quality
            + 1.5 * activity_quality
        ) * sample_penalty
    elif bool(getattr(settings, "SCALP_PRIMARY_MODE", True)):
        score -= 1.5
    return {
        "bars": max(0, final_index - start_index),
        "trades": len(trades),
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "net_profit": net_profit,
        "return_pct": return_pct,
        "max_drawdown_pct": max_drawdown,
        "profit_factor": profit_factor,
        "expectancy_r": expectancy_r,
        "stress_expectancy_r": stress_expectancy_r,
        "stress_profit_factor": stress_profit_factor,
        "avg_cost_stress_r": avg_cost_stress_r,
        "avg_hold_bars": avg_hold_bars,
        "median_hold_bars": median_hold_bars,
        "trades_per_1000_bars": trades_per_1000_bars,
        "score": score,
        "ending_balance": balance,
    }


def walk_forward_metrics(
    frame: pd.DataFrame,
    definition: StrategyDefinition,
    starting_balance: float,
) -> dict[str, Any]:
    """Anchored chronological walk-forward evaluation on unseen windows."""
    folds = max(2, int(getattr(settings, "WALK_FORWARD_FOLDS", 4)))
    first_index = 150
    last_index = len(frame) - 1
    usable = max(0, last_index - first_index)
    if usable < 600:
        return {
            "folds": [],
            "pass_count": 0,
            "pass_ratio": 0.0,
            "mean_score": -999.0,
            "total_trades": 0,
            "reason": "insufficient_walk_forward_bars",
        }

    initial_train = max(300, int(usable * 0.40))
    test_start = min(last_index - 1, first_index + initial_train)
    remaining = max(1, last_index - test_start)
    fold_size = max(50, remaining // folds)
    results: list[dict[str, Any]] = []

    for fold in range(folds):
        start = test_start + fold * fold_size
        end = last_index if fold == folds - 1 else min(last_index, start + fold_size)
        if end - start < 25:
            continue
        metrics = simulate_backtest(
            frame, definition, starting_balance, start_index=start, end_index=end
        )
        passed = (
            int(metrics["trades"])
            >= int(getattr(settings, "WALK_FORWARD_MIN_TRADES_PER_FOLD", 3))
            and safe_float(metrics["profit_factor"])
            >= safe_float(getattr(settings, "WALK_FORWARD_MIN_PROFIT_FACTOR", 0.85))
            and safe_float(metrics["max_drawdown_pct"])
            < settings.MAX_VALIDATED_DRAWDOWN_PCT
        )
        results.append(
            {
                "fold": fold + 1,
                "start_index": start,
                "end_index": end,
                "passed": passed,
                **metrics,
            }
        )

    pass_count = sum(1 for item in results if item["passed"])
    pass_ratio = pass_count / len(results) if results else 0.0
    mean_score = (
        sum(safe_float(item["score"]) for item in results) / len(results)
        if results
        else -999.0
    )
    return {
        "folds": results,
        "pass_count": pass_count,
        "pass_ratio": pass_ratio,
        "mean_score": mean_score,
        "total_trades": sum(int(item["trades"]) for item in results),
        "reason": "ok" if results else "no_walk_forward_folds",
    }


def validation_result(
    definition: StrategyDefinition,
    full: dict[str, Any],
    train: dict[str, Any],
    oos: dict[str, Any],
    walk_forward: dict[str, Any],
) -> tuple[bool, str, float]:
    reasons: list[str] = []
    scalp = family_is_scalp(definition.family)
    min_full_trades = (
        int(getattr(settings, "SCALP_MIN_BACKTEST_TRADES", 60))
        if scalp else settings.MIN_BACKTEST_TRADES
    )
    min_full_pf = (
        safe_float(getattr(settings, "SCALP_MIN_FULL_PROFIT_FACTOR", 1.03), 1.03)
        if scalp else settings.MIN_PROFIT_FACTOR
    )
    min_oos_trades = (
        int(getattr(settings, "SCALP_MIN_OOS_TRADES", 15))
        if scalp else settings.MIN_OOS_TRADES
    )
    min_oos_pf = (
        safe_float(getattr(settings, "SCALP_MIN_OOS_PROFIT_FACTOR", 0.98), 0.98)
        if scalp else settings.MIN_OOS_PROFIT_FACTOR
    )
    if full["trades"] < min_full_trades:
        reasons.append("too_few_full_trades")
    if full["profit_factor"] < min_full_pf:
        reasons.append("low_full_profit_factor")
    if full["max_drawdown_pct"] >= settings.MAX_VALIDATED_DRAWDOWN_PCT:
        reasons.append("drawdown_limit")
    if oos["trades"] < min_oos_trades:
        reasons.append("too_few_oos_trades")
    if oos["profit_factor"] < min_oos_pf:
        reasons.append("low_oos_profit_factor")
    if scalp:
        max_hold = (
            safe_float(getattr(settings, "SUPER_SCALP_MAX_AVG_HOLD_BARS", 7.0), 7.0)
            if definition.family == "super_scalp"
            else safe_float(getattr(settings, "SCALP_MAX_AVG_HOLD_BARS", 15.0), 15.0)
        )
        if safe_float(full.get("avg_hold_bars")) > max_hold:
            reasons.append("not_scalp_holding_time")
        if bool(getattr(settings, "LAB_COST_STRESS_GATE_ENABLED", False)):
            if safe_float(full.get("stress_expectancy_r")) <= 0.0:
                reasons.append("cost_stress_negative_expectancy")
            if safe_float(oos.get("stress_profit_factor")) < 1.0:
                reasons.append("cost_stress_oos_pf_below_1")
    if train["score"] > 0 and oos["score"] < -abs(train["score"]) * 0.75:
        reasons.append("oos_collapse")
    if safe_float(walk_forward.get("pass_ratio")) < safe_float(
        getattr(settings, "WALK_FORWARD_MIN_PASS_RATIO", 0.50)
    ):
        reasons.append("walk_forward_weak")
    stability = (
        oos["score"]
        - abs(train["score"] - oos["score"]) * 0.20
        + safe_float(walk_forward.get("mean_score")) * 0.25
    )
    return (
        not reasons,
        "historical_validated" if not reasons else ",".join(reasons),
        stability,
    )


def log_backtest_error(
    connection: sqlite3.Connection,
    strategy_id: int | None,
    symbol: str,
    stage: str,
    error: Exception,
) -> None:
    connection.execute(
        """
        INSERT INTO backtest_errors(
            strategy_id, symbol, stage, error_type,
            error_message, traceback_text, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            strategy_id,
            symbol,
            stage,
            type(error).__name__,
            str(error),
            traceback.format_exc(limit=12),
            utc_now(),
        ),
    )


def pending_rows(
    connection: sqlite3.Connection,
    symbol: str,
    limit_per_symbol: int | None,
) -> list[sqlite3.Row]:
    query = """
        SELECT s.*
        FROM strategies s
        WHERE s.symbol=?
          AND s.status NOT IN ('invalid_params', 'duplicate_repaired')
          AND (
              s.status='needs_revalidation'
              OR NOT EXISTS (
                  SELECT 1 FROM backtests b WHERE b.strategy_id=s.id
              )
          )
        ORDER BY
            CASE WHEN s.family IN (
                'super_scalp','scalp','micro_momentum','pullback_scalp',
                'breakout_scalp','mean_revert_scalp'
            ) THEN 0 ELSE 1 END,
            CASE WHEN s.status='needs_revalidation' THEN 0 ELSE 1 END,
            s.generation, s.id
    """
    params: list[Any] = [symbol]
    if limit_per_symbol is not None:
        query += " LIMIT ?"
        params.append(limit_per_symbol)
    return list(connection.execute(query, params).fetchall())


def backtest_pending(limit_per_symbol: int | None = None) -> dict[str, Any]:
    init_database()
    connect_mt5()
    report: dict[str, Any] = {"started_at": utc_now(), "symbols": {}}
    try:
        for symbol in settings.SYMBOLS:
            with db_connect() as connection:
                rows = pending_rows(connection, symbol, limit_per_symbol)
            if not rows:
                print(f"{symbol}: no pending strategies")
                report["symbols"][symbol] = []
                continue

            definitions: list[StrategyDefinition] = []
            valid_rows: list[sqlite3.Row] = []
            with db_connect() as connection:
                for row in rows:
                    try:
                        definitions.append(load_strategy(row))
                        valid_rows.append(row)
                    except Exception as error:
                        log_backtest_error(
                            connection, int(row["id"]), symbol, "load_strategy", error
                        )
                        connection.execute(
                            "UPDATE strategies SET status='invalid_params' WHERE id=?",
                            (int(row["id"]),),
                        )
                        connection.commit()

            print(f"\nRefreshing {symbol} history for {len(definitions)} strategies...")
            cached = load_cached_history(symbol)
            full_key = f"last_full_history_attempt:{symbol}"
            with db_connect() as state_connection:
                previous_full = state_get(state_connection, full_key)
            full_due = cached is None
            if previous_full:
                try:
                    previous_dt = datetime.fromisoformat(previous_full)
                    age_hours = (
                        datetime.now(timezone.utc) - previous_dt
                    ).total_seconds() / 3600.0
                    full_due = full_due or age_hours >= settings.HISTORY_FULL_REFRESH_HOURS
                except ValueError:
                    full_due = True
            else:
                full_due = True
            request_bars = (
                settings.HISTORY_BARS
                if full_due
                else settings.HISTORY_RECENT_REFRESH_BARS
            )
            try:
                fresh = fetch_raw_bars(symbol, request_bars)
                data_path = merge_and_save_history(symbol, fresh)
                combined = load_cached_history(symbol)
                raw = combined if combined is not None else fresh
                if full_due:
                    with db_connect() as state_connection:
                        state_set(state_connection, full_key, utc_now())
                        state_connection.commit()
            except Exception as error:
                if cached is None:
                    message = f"History unavailable; symbol skipped: {error}"
                    print(f"{symbol}: {message}")
                    report["symbols"][symbol] = {"error": message}
                    continue
                raw = cached
                data_path = history_path(symbol)
                print(
                    f"{symbol}: history refresh failed; using cached "
                    f"bars={len(raw)} | reason={error}"
                )
            frame = add_features(raw, required_ema_lengths(definitions))
            print(
                f"Saved market data: {data_path} | bars={len(frame)} | "
                f"EMAs={len(required_ema_lengths(definitions))}"
            )
            split_index = max(300, int(len(frame) * (1.0 - settings.OOS_FRACTION)))
            symbol_results: list[dict[str, Any]] = []

            for number, definition in enumerate(definitions, start=1):
                try:
                    prescreen_failed = False
                    prescreen_reason = ""
                    prescreen: dict[str, Any] | None = None
                    if family_is_scalp(definition.family):
                        recent_bars = max(
                            5_000,
                            int(getattr(settings, "SCALP_FAST_PRESCREEN_BARS", 40_000)),
                        )
                        recent_start = max(150, len(frame) - recent_bars - 1)
                        prescreen = simulate_backtest(
                            frame, definition, settings.STARTING_BALANCE,
                            recent_start, len(frame) - 1,
                        )
                        fast_reasons: list[str] = []
                        if int(prescreen["trades"]) < int(
                            getattr(settings, "SCALP_FAST_PRESCREEN_MIN_TRADES", 12)
                        ):
                            fast_reasons.append("few_trades")
                        if safe_float(prescreen["profit_factor"]) < safe_float(
                            getattr(settings, "SCALP_FAST_PRESCREEN_MIN_PROFIT_FACTOR", 0.82),
                            0.82,
                        ):
                            fast_reasons.append("low_pf")
                        if safe_float(prescreen.get("expectancy_r")) < safe_float(
                            getattr(settings, "SCALP_FAST_PRESCREEN_MIN_EXPECTANCY_R", -0.08),
                            -0.08,
                        ):
                            fast_reasons.append("low_expectancy")
                        if fast_reasons:
                            prescreen_failed = True
                            prescreen_reason = "fast_prescreen:" + ",".join(fast_reasons)

                    if prescreen_failed and prescreen is not None:
                        full = dict(prescreen)
                        train = dict(prescreen)
                        oos = dict(prescreen)
                        walk_forward = {
                            "folds": [], "pass_count": 0, "pass_ratio": 0.0,
                            "mean_score": safe_float(prescreen.get("score")),
                            "total_trades": int(prescreen.get("trades") or 0),
                            "reason": prescreen_reason,
                        }
                        validated = False
                        reason = prescreen_reason
                        stability = safe_float(prescreen.get("score")) - 25.0
                    else:
                        full = simulate_backtest(
                            frame, definition, settings.STARTING_BALANCE, 150, len(frame) - 1
                        )
                        train = simulate_backtest(
                            frame, definition, settings.STARTING_BALANCE, 150, split_index
                        )
                        oos = simulate_backtest(
                            frame,
                            definition,
                            settings.STARTING_BALANCE,
                            max(150, split_index),
                            len(frame) - 1,
                        )
                        walk_forward = walk_forward_metrics(
                            frame, definition, settings.STARTING_BALANCE
                        )
                        validated, reason, stability = validation_result(
                            definition, full, train, oos, walk_forward
                        )
                    full["train"] = train
                    full["oos"] = oos
                    full["walk_forward"] = walk_forward
                    full["stability_score"] = stability
                    full["validation_reason"] = reason
                    status = (
                        "historical_validated" if validated else "backtest_rejected"
                    )

                    with db_connect() as connection:
                        connection.execute(
                            """
                            INSERT INTO backtests(
                                strategy_id, symbol, timeframe, bars,
                                trades, wins, losses, win_rate,
                                net_profit, return_pct, max_drawdown_pct,
                                profit_factor, score, tested_at, metrics_json
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                definition.strategy_id,
                                symbol,
                                settings.TIMEFRAME_NAME,
                                len(frame),
                                full["trades"],
                                full["wins"],
                                full["losses"],
                                full["win_rate"],
                                full["net_profit"],
                                full["return_pct"],
                                full["max_drawdown_pct"],
                                full["profit_factor"],
                                full["score"],
                                utc_now(),
                                json_text(full),
                            ),
                        )
                        connection.execute(
                            """
                            INSERT INTO strategy_diagnostics(
                                strategy_id, symbol, train_trades,
                                train_profit_factor, train_score,
                                oos_trades, oos_profit_factor, oos_score,
                                stability_score, validation_reason, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(strategy_id) DO UPDATE SET
                                train_trades=excluded.train_trades,
                                train_profit_factor=excluded.train_profit_factor,
                                train_score=excluded.train_score,
                                oos_trades=excluded.oos_trades,
                                oos_profit_factor=excluded.oos_profit_factor,
                                oos_score=excluded.oos_score,
                                stability_score=excluded.stability_score,
                                validation_reason=excluded.validation_reason,
                                updated_at=excluded.updated_at
                            """,
                            (
                                definition.strategy_id,
                                symbol,
                                train["trades"],
                                train["profit_factor"],
                                train["score"],
                                oos["trades"],
                                oos["profit_factor"],
                                oos["score"],
                                stability,
                                reason,
                                utc_now(),
                            ),
                        )
                        connection.execute(
                            "UPDATE strategies SET status=? WHERE id=?",
                            (status, definition.strategy_id),
                        )
                        connection.commit()

                    result_row = {
                        "strategy_id": definition.strategy_id,
                        "family": definition.family,
                        "status": status,
                        **full,
                    }
                    symbol_results.append(result_row)
                    print(
                        f"{symbol} [{number}/{len(definitions)}] "
                        f"id={definition.strategy_id} {definition.family} "
                        f"trades={full['trades']} PF={full['profit_factor']:.2f} "
                        f"ER={safe_float(full.get('expectancy_r')):+.3f}R "
                        f"hold={safe_float(full.get('avg_hold_bars')):.1f}m "
                        f"OOS_PF={oos['profit_factor']:.2f} "
                        f"WF={walk_forward['pass_count']}/{len(walk_forward['folds'])} "
                        f"DD={full['max_drawdown_pct']:.2%} "
                        f"score={full['score']:.2f} -> {status}"
                    )
                except Exception as error:
                    with db_connect() as connection:
                        log_backtest_error(
                            connection,
                            definition.strategy_id,
                            symbol,
                            "backtest_strategy",
                            error,
                        )
                        connection.execute(
                            "UPDATE strategies SET status='backtest_error' WHERE id=?",
                            (definition.strategy_id,),
                        )
                        connection.commit()
                    print(
                        f"{symbol} [{number}/{len(definitions)}] "
                        f"id={definition.strategy_id} ERROR: {error} (continued)"
                    )
            report["symbols"][symbol] = symbol_results

        report["finished_at"] = utc_now()
        path = settings.REPORTS_DIR / f"backtest_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        print(f"\nBacktest report: {path}")
        return report
    finally:
        mt5.shutdown()


# ============================================================
# Selection, memory and reporting
# ============================================================


REGIME_FAMILY_BONUS: dict[str, dict[str, float]] = {
    "extreme_trend": {
        "micro_momentum": 4.0, "breakout_scalp": 3.5, "pullback_scalp": 2.5,
        "scalp": 1.8, "super_scalp": 0.8, "trend": 0.5, "breakout": 0.3,
    },
    "volatile_trend": {
        "micro_momentum": 3.8, "breakout_scalp": 3.2, "pullback_scalp": 3.0,
        "scalp": 2.0, "super_scalp": 1.0, "trend": 0.4,
    },
    "trend": {
        "pullback_scalp": 3.6, "micro_momentum": 3.2, "breakout_scalp": 2.8,
        "scalp": 2.2, "super_scalp": 1.1, "trend": 0.3,
    },
    "volatile_range": {
        "mean_revert_scalp": 4.0, "super_scalp": 3.5, "scalp": 3.0,
        "breakout_scalp": 2.2, "micro_momentum": 1.7, "mean_reversion": 0.3,
    },
    "range": {
        "mean_revert_scalp": 4.2, "super_scalp": 3.8, "scalp": 3.2,
        "pullback_scalp": 1.8, "micro_momentum": 1.2, "mean_reversion": 0.3,
    },
    "unknown": {family: (2.0 if family in SCALP_FAMILIES else 0.0) for family in FAMILIES},
}



def family_context_adjustment(
    connection: sqlite3.Connection,
    symbol: str,
    family: str,
    regime: str,
) -> tuple[float, dict[str, Any]]:
    session = market_session_tag(symbol)
    session_cells = [
        session_family_memory_snapshot(connection, symbol, family, regime, session, side)
        for side in (1, -1)
    ]
    collective_cells = [
        collective_memory_snapshot(connection, symbol, family, regime, side)
        for side in (1, -1)
    ]

    def blend(cells: list[dict[str, Any]]) -> tuple[float, float, float]:
        obs = sum(safe_float(cell.get("observations")) for cell in cells)
        if obs <= 0:
            return 0.0, 0.50, 0.0
        win = sum(
            safe_float(cell.get("win_probability"), 0.50)
            * max(1e-9, safe_float(cell.get("observations")))
            for cell in cells
        ) / max(1e-9, obs)
        reward = sum(
            safe_float(cell.get("reward_ewma"))
            * max(1e-9, safe_float(cell.get("observations")))
            for cell in cells
        ) / max(1e-9, obs)
        return obs, win, reward

    session_obs, session_win, session_reward = blend(session_cells)
    collective_obs, collective_win, collective_reward = blend(collective_cells)
    adjustment = 0.0
    if family_is_scalp(family) and bool(getattr(settings, "SCALP_PRIMARY_MODE", True)):
        adjustment += safe_float(getattr(settings, "SCALP_FAMILY_SCORE_BONUS", 4.0), 4.0)
    adjustment += (
        (session_win - 0.50)
        * safe_float(getattr(settings, "SCALP_SESSION_SCORE_WEIGHT", 5.0), 5.0)
        + math.tanh(session_reward) * 1.5
    )
    adjustment += (
        (collective_win - 0.50)
        * safe_float(getattr(settings, "SCALP_COLLECTIVE_SCORE_WEIGHT", 3.0), 3.0)
        + math.tanh(collective_reward)
    )
    return adjustment, {
        "session": session,
        "session_observations": session_obs,
        "session_win_probability": session_win,
        "session_reward_ewma": session_reward,
        "collective_observations": collective_obs,
        "collective_win_probability": collective_win,
        "collective_reward_ewma": collective_reward,
    }


def candidate_strategies(
    connection: sqlite3.Connection,
    symbol: str,
    regime: str,
) -> list[tuple[StrategyDefinition, float]]:
    rows = connection.execute(
        """
        SELECT s.*,
               MAX(b.score) AS backtest_score,
               COALESCE(d.stability_score, 0) AS stability_score,
               COALESCE(ss.reward_mean, 0) AS live_reward,
               COALESCE(ss.observations, 0) AS live_observations
        FROM strategies s
        JOIN backtests b ON b.strategy_id=s.id
        LEFT JOIN strategy_diagnostics d ON d.strategy_id=s.id
        LEFT JOIN strategy_scores ss
          ON ss.strategy_id=s.id AND ss.symbol=s.symbol AND ss.regime=?
        WHERE s.symbol=? AND s.status=?
        GROUP BY s.id
        ORDER BY backtest_score DESC
        LIMIT ?
        """,
        (
            regime, symbol, settings.EXECUTION_STRATEGY_STATUS,
            max(settings.ENSEMBLE_CANDIDATES * 5, 120),
        ),
    ).fetchall()
    bonus_map = REGIME_FAMILY_BONUS.get(regime, {})
    candidates: list[tuple[StrategyDefinition, float]] = []
    for row in rows:
        definition = load_strategy(row)
        observations_raw = int(row["live_observations"] or 0)
        observations = min(observations_raw, 30)
        live_reward = safe_float(row["live_reward"])
        if (
            observations_raw >= int(getattr(settings, "SCALP_SHADOW_EARLY_QUARANTINE_TRADES", 8))
            and live_reward <= safe_float(getattr(settings, "SCALP_SHADOW_EARLY_QUARANTINE_MEAN_R", -0.25), -0.25)
        ):
            continue
        evidence_scale = min(1.0, observations_raw / 15.0)
        score = (
            safe_float(row["backtest_score"])
            + safe_float(row["stability_score"]) * 0.35
            + live_reward * safe_float(getattr(settings, "SCALP_SHADOW_SCORE_WEIGHT", 30.0), 30.0) * evidence_scale
            + observations * 0.04
            + bonus_map.get(definition.family, 0.0)
        )
        adaptive_adjustment, _adaptive_meta = adaptive_candidate_adjustment(
            connection, definition.strategy_id, symbol, regime
        )
        context_adjustment, _context_meta = family_context_adjustment(
            connection, symbol, definition.family, regime
        )
        score += adaptive_adjustment + context_adjustment
        candidates.append((definition, score))
    ranked = sorted(candidates, key=lambda item: item[1], reverse=True)
    limit = max(1, int(settings.ENSEMBLE_CANDIDATES))
    if not bool(getattr(settings, "SCALP_PRIMARY_MODE", True)):
        return ranked[:limit]
    scalp_ranked = [item for item in ranked if family_is_scalp(item[0].family)]
    general_ranked = [item for item in ranked if not family_is_scalp(item[0].family)]
    scalp_slots = min(
        len(scalp_ranked),
        max(1, int(round(limit * safe_float(getattr(settings, "SCALP_ENSEMBLE_TARGET_SHARE", 0.75), 0.75)))),
    )
    selected = scalp_ranked[:scalp_slots] + general_ranked[: max(0, limit - scalp_slots)]
    return sorted(selected, key=lambda item: item[1], reverse=True)



def demo_trial_candidate_strategies(
    connection: sqlite3.Connection,
    symbol: str,
    regime: str,
) -> list[tuple[StrategyDefinition, float]]:
    """Return strong historical passers for tiny-risk DEMO live trials.

    Strict ``shadow_approved`` strategies always take priority.  This fallback
    exists only because the merged V5.1 snapshot contains legacy strategies
    that had already passed full/OOS validation before anchored walk-forward
    was introduced.  Generated/rejected strategies are intentionally excluded.
    """
    if not bool(getattr(settings, "ENABLE_DEMO_TRIAL_BRIDGE", False)):
        return []
    rows = connection.execute(
        """
        SELECT s.*,
               MAX(b.score) AS backtest_score,
               MAX(b.profit_factor) AS full_profit_factor,
               MIN(b.max_drawdown_pct) AS full_drawdown,
               d.oos_trades, d.oos_profit_factor, d.stability_score,
               d.validation_reason,
               COALESCE(ss.reward_mean, 0) AS live_reward,
               COALESCE(ss.observations, 0) AS live_observations
        FROM strategies s
        JOIN backtests b ON b.strategy_id=s.id
        JOIN strategy_diagnostics d ON d.strategy_id=s.id
        LEFT JOIN strategy_scores ss
          ON ss.strategy_id=s.id AND ss.symbol=s.symbol AND ss.regime=?
        WHERE s.symbol=?
          AND (
              s.status='historical_validated'
              OR (
                  s.status='needs_revalidation'
                  AND LOWER(COALESCE(d.validation_reason, '')) LIKE 'validated%'
              )
          )
        GROUP BY s.id
        HAVING MAX(b.profit_factor)>=?
           AND d.oos_profit_factor>=?
           AND d.oos_trades>=?
           AND d.stability_score>=?
           AND MAX(b.score)>=?
        ORDER BY
            CASE WHEN s.family IN (
                'super_scalp','scalp','micro_momentum','pullback_scalp',
                'breakout_scalp','mean_revert_scalp'
            ) THEN 0 ELSE 1 END,
            CASE s.status WHEN 'historical_validated' THEN 0 ELSE 1 END,
            (MAX(b.score)
             + d.stability_score * 0.35
             + COALESCE(ss.reward_mean, 0) * 5.0
             + MIN(COALESCE(ss.observations, 0), 30) * 0.04) DESC
        LIMIT ?
        """,
        (
            regime,
            symbol,
            settings.DEMO_TRIAL_MIN_FULL_PROFIT_FACTOR,
            settings.DEMO_TRIAL_MIN_OOS_PROFIT_FACTOR,
            settings.DEMO_TRIAL_MIN_OOS_TRADES,
            settings.DEMO_TRIAL_MIN_STABILITY_SCORE,
            settings.DEMO_TRIAL_MIN_BACKTEST_SCORE,
            max(settings.DEMO_TRIAL_CANDIDATES * 5, 100),
        ),
    ).fetchall()
    bonus_map = REGIME_FAMILY_BONUS.get(regime, {})
    candidates: list[tuple[StrategyDefinition, float]] = []
    for row in rows:
        definition = load_strategy(row)
        observations_raw = int(row["live_observations"] or 0)
        observations = min(observations_raw, 30)
        live_reward = safe_float(row["live_reward"])
        if (
            observations_raw >= int(getattr(settings, "SCALP_SHADOW_EARLY_QUARANTINE_TRADES", 8))
            and live_reward <= safe_float(getattr(settings, "SCALP_SHADOW_EARLY_QUARANTINE_MEAN_R", -0.25), -0.25)
        ):
            continue
        evidence_scale = min(1.0, observations_raw / 15.0)
        score = (
            safe_float(row["backtest_score"])
            + safe_float(row["stability_score"]) * 0.35
            + live_reward * safe_float(getattr(settings, "SCALP_SHADOW_SCORE_WEIGHT", 30.0), 30.0) * evidence_scale
            + observations * 0.04
            + bonus_map.get(definition.family, 0.0)
        )
        adaptive_adjustment, _adaptive_meta = adaptive_candidate_adjustment(
            connection, definition.strategy_id, symbol, regime
        )
        context_adjustment, _context_meta = family_context_adjustment(
            connection, symbol, definition.family, regime
        )
        score += adaptive_adjustment + context_adjustment
        candidates.append((definition, score))
    ranked = sorted(candidates, key=lambda item: item[1], reverse=True)
    limit = max(1, int(settings.DEMO_TRIAL_CANDIDATES))
    if not bool(getattr(settings, "SCALP_PRIMARY_MODE", True)):
        return ranked[:limit]
    scalp_ranked = [item for item in ranked if family_is_scalp(item[0].family)]
    general_ranked = [item for item in ranked if not family_is_scalp(item[0].family)]
    scalp_slots = min(
        len(scalp_ranked),
        max(
            1,
            int(round(limit * safe_float(
                getattr(settings, "SCALP_ENSEMBLE_TARGET_SHARE", 0.75), 0.75
            ))),
        ),
    )
    selected = scalp_ranked[:scalp_slots] + general_ranked[: max(0, limit - scalp_slots)]
    return sorted(selected, key=lambda item: item[1], reverse=True)


def execution_candidate_set(
    connection: sqlite3.Connection,
    symbol: str,
    regime: str,
) -> tuple[list[tuple[StrategyDefinition, float]], str]:
    approved = candidate_strategies(connection, symbol, regime)
    trial = demo_trial_candidate_strategies(connection, symbol, regime)
    if bool(getattr(settings, "SCALP_EXECUTION_PRIORITY", True)):
        approved_scalp = [item for item in approved if family_is_scalp(item[0].family)]
        trial_scalp = [item for item in trial if family_is_scalp(item[0].family)]
        if approved_scalp:
            return approved_scalp, "shadow_approved"
        if trial_scalp:
            return trial_scalp, "demo_trial"
    if approved:
        return approved, "shadow_approved"
    if trial:
        return trial, "demo_trial"
    return [], "none"


def trial_signal(
    frame: pd.DataFrame,
    index: int,
    candidates: list[tuple[StrategyDefinition, float]],
    dom: float | None,
    connection: sqlite3.Connection | None = None,
    regime: str | None = None,
) -> tuple[int, StrategyDefinition | None, dict[str, Any]]:
    """Use ensemble consensus when available, otherwise one top trial signal.

    A trial remains signal-driven; this never forces a trade merely to increase
    activity.  The single-strategy fallback is allowed only on DEMO and only
    for the strong historical candidate set defined above.
    """
    signal, strategy, details = ensemble_signal(
        frame, index, candidates, dom, connection=connection, regime=regime
    )
    if signal != 0 and strategy is not None:
        details["trial_signal_mode"] = "ensemble"
        return signal, strategy, details
    errors = list(details.get("errors") or [])
    for definition, score in candidates:
        try:
            candidate_signal = row_signal(frame, index, definition, dom)
        except Exception as error:
            errors.append(f"id={definition.strategy_id}:{error}")
            continue
        if candidate_signal in (-1, 1):
            details.update(
                {
                    "trial_signal_mode": "top_single",
                    "selected_score": score,
                    "selected_strategy_id": definition.strategy_id,
                    "errors": errors[:10],
                }
            )
            return candidate_signal, definition, details
    details["trial_signal_mode"] = "no_signal"
    details["errors"] = errors[:10]
    return 0, None, details


def ensemble_signal(
    frame: pd.DataFrame,
    index: int,
    candidates: list[tuple[StrategyDefinition, float]],
    dom: float | None,
    connection: sqlite3.Connection | None = None,
    regime: str | None = None,
) -> tuple[int, StrategyDefinition | None, dict[str, Any]]:
    buy_weight = 0.0
    sell_weight = 0.0
    buy_defs: list[tuple[StrategyDefinition, float]] = []
    sell_defs: list[tuple[StrategyDefinition, float]] = []
    errors: list[str] = []
    for definition, raw_weight in candidates:
        weight = max(0.10, raw_weight + 10.0)
        try:
            signal = row_signal(frame, index, definition, dom)
        except Exception as error:
            errors.append(f"id={definition.strategy_id}:{error}")
            continue
        dynamic_meta: dict[str, Any] | None = None
        if signal in (-1, 1) and connection is not None and regime:
            multiplier, dynamic_meta = dynamic_ensemble_multiplier(
                connection, definition, regime, signal
            )
            weight *= multiplier
        if signal == 1:
            buy_weight += weight
            buy_defs.append((definition, weight))
        elif signal == -1:
            sell_weight += weight
            sell_defs.append((definition, weight))

    details = {
        "buy_weight": buy_weight,
        "sell_weight": sell_weight,
        "buy_votes": len(buy_defs),
        "sell_votes": len(sell_defs),
        "candidate_count": len(candidates),
        "dom_imbalance": dom,
        "dynamic_ensemble": connection is not None and bool(regime),
        "errors": errors[:10],
    }
    ratio = settings.ENSEMBLE_CONSENSUS_RATIO
    if (
        len(buy_defs) >= settings.ENSEMBLE_MIN_AGREE
        and buy_weight > max(0.0, sell_weight) * ratio
        and buy_weight > 0
    ):
        return 1, max(buy_defs, key=lambda x: x[1])[0], details
    if (
        len(sell_defs) >= settings.ENSEMBLE_MIN_AGREE
        and sell_weight > max(0.0, buy_weight) * ratio
        and sell_weight > 0
    ):
        return -1, max(sell_defs, key=lambda x: x[1])[0], details
    return 0, None, details


def update_reward_memory(
    connection: sqlite3.Connection,
    strategy_id: int,
    symbol: str,
    regime: str,
    reward: float,
    source: str,
    details: dict[str, Any],
) -> None:
    previous = connection.execute(
        """
        SELECT observations, reward_mean FROM strategy_scores
        WHERE strategy_id=? AND symbol=? AND regime=?
        """,
        (strategy_id, symbol, regime),
    ).fetchone()
    if previous:
        n = int(previous["observations"])
        mean = safe_float(previous["reward_mean"])
        new_n = n + 1
        new_mean = mean + (reward - mean) / new_n
        connection.execute(
            """
            UPDATE strategy_scores
            SET observations=?, reward_mean=?, updated_at=?
            WHERE strategy_id=? AND symbol=? AND regime=?
            """,
            (new_n, new_mean, utc_now(), strategy_id, symbol, regime),
        )
    else:
        connection.execute(
            """
            INSERT INTO strategy_scores(
                strategy_id, symbol, regime, observations, reward_mean, updated_at
            ) VALUES (?, ?, ?, 1, ?, ?)
            """,
            (strategy_id, symbol, regime, reward, utc_now()),
        )
    connection.execute(
        """
        INSERT INTO rl_reward_events(
            timestamp, strategy_id, symbol, regime, reward, source, details_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (utc_now(), strategy_id, symbol, regime, reward, source, json_text(details)),
    )


_LAST_DECISION_LOG_TS: dict[tuple[str, str, str, int | None, int], float] = {}


def log_decision(
    connection: sqlite3.Connection,
    mode: str,
    symbol: str,
    regime: str | None,
    strategy_id: int | None,
    signal: int,
    reason: str,
    details: dict[str, Any],
) -> None:
    """Persist a decision while suppressing high-frequency duplicate noise.

    The executor historically wrote the same no-signal/spread/veto row every
    couple of seconds even though the strategy signal is based on a closed M1
    candle. V6.6 keeps important state changes while de-duplicating identical
    rows inside a short in-process window.
    """
    dedup_seconds = max(0.0, safe_float(getattr(settings, "DECISION_LOG_DEDUP_SECONDS", 0.0)))
    noisy = (
        reason in {"No approved/trial entry", "No ensemble entry"}
        or reason.startswith("Spread filter rejected entry")
        or reason.startswith("SuperLearner veto:")
        or reason.startswith("Spartan-Pro veto:")
    )
    if dedup_seconds > 0 and noisy:
        key = (str(mode), str(symbol), str(reason), int(strategy_id) if strategy_id is not None else None, int(signal))
        now_monotonic = time.monotonic()
        previous = _LAST_DECISION_LOG_TS.get(key, -1e12)
        if now_monotonic - previous < dedup_seconds:
            return
        _LAST_DECISION_LOG_TS[key] = now_monotonic

    connection.execute(
        """
        INSERT INTO decision_logs(
            timestamp, mode, symbol, regime, strategy_id,
            signal, reason, details_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            utc_now(), mode, symbol, regime, strategy_id,
            signal, reason, json_text(details),
        ),
    )


def build_status_data(connection: sqlite3.Connection) -> dict[str, Any]:
    data: dict[str, Any] = {"generated_at": utc_now(), "symbols": {}}
    for symbol in settings.SYMBOLS:
        counts = connection.execute(
            """
            SELECT status, COUNT(*) AS n FROM strategies
            WHERE symbol=? GROUP BY status
            """,
            (symbol,),
        ).fetchall()
        best = connection.execute(
            """
            SELECT s.id, s.family, MAX(b.score) AS score,
                   MAX(b.profit_factor) AS profit_factor,
                   MIN(b.max_drawdown_pct) AS drawdown,
                   d.oos_profit_factor, d.oos_trades, d.stability_score
            FROM strategies s
            JOIN backtests b ON b.strategy_id=s.id
            LEFT JOIN strategy_diagnostics d ON d.strategy_id=s.id
            WHERE s.symbol=? AND s.status='shadow_approved'
            GROUP BY s.id ORDER BY score DESC LIMIT 1
            """,
            (symbol,),
        ).fetchone()
        scalp_counts = connection.execute(
            f"""
            SELECT status, COUNT(*) AS n FROM strategies
            WHERE symbol=? AND family IN ({','.join('?' for _ in SCALP_FAMILIES)})
            GROUP BY status
            """,
            (symbol, *SCALP_FAMILIES),
        ).fetchall()
        data["symbols"][symbol] = {
            "status_counts": {str(row["status"]): int(row["n"]) for row in counts},
            "scalp_status_counts": {str(row["status"]): int(row["n"]) for row in scalp_counts},
            "best_executable": dict(best) if best else None,
        }
    data["backtest_errors"] = int(
        connection.execute("SELECT COUNT(*) AS n FROM backtest_errors").fetchone()["n"]
    )
    data["paper"] = dict(
        connection.execute(
            """
            SELECT
                SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) AS open_n,
                SUM(CASE WHEN status='closed' THEN 1 ELSE 0 END) AS closed_n,
                COALESCE(SUM(CASE WHEN status='closed' THEN pnl ELSE 0 END), 0) AS total_pnl
            FROM paper_positions
            """
        ).fetchone()
    )
    data["executions"] = dict(
        connection.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN status='sent' THEN 1 ELSE 0 END) AS sent,
                   SUM(CASE WHEN status='rejected' THEN 1 ELSE 0 END) AS rejected
            FROM execution_logs
            """
        ).fetchone()
    )
    data["demo_positions"] = dict(
        connection.execute(
            """
            SELECT
                SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) AS open_n,
                SUM(CASE WHEN status='closed' THEN 1 ELSE 0 END) AS closed_n,
                COALESCE(SUM(CASE WHEN status='closed' THEN pnl ELSE 0 END), 0) AS total_pnl,
                COALESCE(AVG(CASE WHEN status='closed' THEN reward_r END), 0) AS average_reward_r
            FROM demo_positions
            """
        ).fetchone()
    )
    data["shadow_positions"] = dict(
        connection.execute(
            """
            SELECT
                SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) AS open_n,
                SUM(CASE WHEN status='closed' THEN 1 ELSE 0 END) AS closed_n,
                COALESCE(AVG(CASE WHEN status='closed' THEN reward_r END), 0) AS average_reward_r
            FROM shadow_positions
            """
        ).fetchone()
    )
    data["shadow_scores"] = dict(
        connection.execute(
            """
            SELECT COUNT(*) AS strategies_tested,
                   COALESCE(SUM(observations), 0) AS observations,
                   SUM(CASE WHEN promoted_at IS NOT NULL THEN 1 ELSE 0 END) AS promoted
            FROM candidate_live_scores
            """
        ).fetchone()
    )
    data["adaptive_learning"] = dict(
        connection.execute(
            """
            SELECT COUNT(*) AS setups,
                   COALESCE(SUM(observations), 0) AS observations,
                   COALESCE(SUM(wins), 0) AS wins,
                   COALESCE(SUM(losses), 0) AS losses,
                   COALESCE(AVG(confidence), 0.5) AS average_stored_confidence,
                   COALESCE(SUM(CASE WHEN blocked_until>? THEN 1 ELSE 0 END), 0) AS cooling_down
            FROM adaptive_trade_memory
            """,
            (utc_now(),),
        ).fetchone()
    )
    data["collective_memory"] = dict(connection.execute(
        """
        SELECT COUNT(*) AS cells,
               COALESCE(SUM(effective_observations), 0) AS effective_observations,
               COALESCE(SUM(win_mass), 0) AS win_mass,
               COALESCE(SUM(loss_mass), 0) AS loss_mass
        FROM collective_market_memory
        """
    ).fetchone())
    data["session_memory"] = dict(connection.execute(
        """
        SELECT COUNT(*) AS cells,
               COALESCE(SUM(effective_observations), 0) AS effective_observations,
               COALESCE(SUM(win_mass), 0) AS win_mass,
               COALESCE(SUM(loss_mass), 0) AS loss_mass
        FROM session_family_memory
        """
    ).fetchone())
    scalp_placeholders = ','.join('?' for _ in SCALP_FAMILIES)
    data["scalp_demo"] = dict(connection.execute(
        f"""
        SELECT COUNT(*) AS closed_n,
               COALESCE(SUM(dp.pnl), 0) AS total_pnl,
               COALESCE(AVG(dp.reward_r), 0) AS average_reward_r,
               COALESCE(AVG(CASE WHEN dp.pnl > 0 THEN 1.0 ELSE 0.0 END), 0) AS win_rate
        FROM demo_positions dp
        JOIN strategies s ON s.id=dp.strategy_id
        WHERE dp.status='closed' AND s.family IN ({scalp_placeholders})
        """,
        SCALP_FAMILIES,
    ).fetchone())
    model_rows = connection.execute(
        "SELECT model_key, updates, last_probability, last_logloss FROM superlearner_model_state ORDER BY model_key"
    ).fetchall()
    data["superlearner_models"] = [dict(row) for row in model_rows]
    data["superlearner_decisions"] = dict(connection.execute(
        """
        SELECT COUNT(*) AS total,
               COALESCE(SUM(CASE WHEN decision='APPROVE' THEN 1 ELSE 0 END),0) AS approved,
               COALESCE(SUM(CASE WHEN decision='REJECT' THEN 1 ELSE 0 END),0) AS rejected,
               COALESCE(AVG(probability),0.5) AS average_probability,
               COALESCE(AVG(latency_ms),0) AS average_latency_ms
        FROM superlearner_decisions
        """
    ).fetchone())
    latency_rows = connection.execute(
        "SELECT latency_ms FROM superlearner_decisions ORDER BY id DESC LIMIT 500"
    ).fetchall()
    latencies = [safe_float(row["latency_ms"]) for row in latency_rows]
    data["superlearner_decisions"]["p95_latency_ms"] = (
        float(np.percentile(latencies, 95)) if latencies else 0.0
    )
    return data


def status_report(write_file: bool = True) -> dict[str, Any]:
    init_database()
    with db_connect() as connection:
        data = build_status_data(connection)
    print("\nSTRATEGY DATABASE")
    for symbol, item in data["symbols"].items():
        counts = item["status_counts"]
        scalp_counts = item.get("scalp_status_counts") or {}
        print(f"{symbol}: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
        print(
            "  scalp pipeline: "
            + (", ".join(f"{k}={v}" for k, v in sorted(scalp_counts.items())) or "none yet")
        )
        best = item["best_executable"]
        if best:
            print(
                f"  best shadow-approved id={best['id']} family={best['family']} "
                f"score={best['score']:.2f} PF={best['profit_factor']:.2f} "
                f"OOS_PF={safe_float(best['oos_profit_factor']):.2f} "
                f"DD={safe_float(best['drawdown']):.2%}"
            )
    print(f"Backtest errors: {data['backtest_errors']}")
    print(
        f"Paper: open={data['paper']['open_n'] or 0}, "
        f"closed={data['paper']['closed_n'] or 0}, "
        f"PnL={safe_float(data['paper']['total_pnl']):.2f}"
    )
    print(
        f"Demo execution logs: total={data['executions']['total'] or 0}, "
        f"sent={data['executions']['sent'] or 0}, "
        f"rejected={data['executions']['rejected'] or 0}"
    )
    print(
        f"Demo positions: open={data['demo_positions']['open_n'] or 0}, "
        f"closed={data['demo_positions']['closed_n'] or 0}, "
        f"PnL={safe_float(data['demo_positions']['total_pnl']):.2f}, "
        f"avg reward={safe_float(data['demo_positions']['average_reward_r']):.2f}R"
    )
    print(
        f"Live shadow lab: open={data['shadow_positions']['open_n'] or 0}, "
        f"closed={data['shadow_positions']['closed_n'] or 0}, "
        f"avg reward={safe_float(data['shadow_positions']['average_reward_r']):.2f}R, "
        f"strategies tested={data['shadow_scores']['strategies_tested'] or 0}, "
        f"observations={data['shadow_scores']['observations'] or 0}, "
        f"approved={data['shadow_scores']['promoted'] or 0}"
    )
    adaptive = data["adaptive_learning"]
    print(
        f"Adaptive execution learning: setups={adaptive['setups'] or 0}, "
        f"observations={adaptive['observations'] or 0}, "
        f"wins={adaptive['wins'] or 0}, losses={adaptive['losses'] or 0}, "
        f"cooling_down={adaptive['cooling_down'] or 0}, "
        f"stored_conf={safe_float(adaptive['average_stored_confidence'], 0.50):.2f}"
    )
    collective = data["collective_memory"]
    session_memory = data["session_memory"]
    scalp_demo = data["scalp_demo"]
    decisions = data["superlearner_decisions"]
    print(
        f"Scalp live outcomes: closed={scalp_demo['closed_n'] or 0}, "
        f"PnL={safe_float(scalp_demo['total_pnl']):.2f}, "
        f"win_rate={safe_float(scalp_demo['win_rate']):.1%}, "
        f"avg_reward={safe_float(scalp_demo['average_reward_r']):+.3f}R"
    )
    print(
        f"SuperLearner V6 Scalp Intelligence: collective_cells={collective['cells'] or 0}, "
        f"collective_experience={safe_float(collective['effective_observations']):.1f}/"
        f"{int(getattr(settings, 'COLLECTIVE_EXPERIENCE_TARGET', 10000))}, "
        f"session_cells={session_memory['cells'] or 0}, "
        f"session_experience={safe_float(session_memory['effective_observations']):.1f}, "
        f"decisions={decisions['total'] or 0}, approves={decisions['approved'] or 0}, "
        f"vetoes={decisions['rejected'] or 0}, models={len(data['superlearner_models'])}, "
        f"p95_latency={safe_float(decisions.get('p95_latency_ms')):.1f}ms"
    )
    for model in data["superlearner_models"]:
        print(
            f"  model {model['model_key']}: updates={model['updates']} "
            f"last_p={safe_float(model['last_probability'], 0.50):.2f} "
            f"logloss={safe_float(model['last_logloss']):.3f}"
        )
    if write_file:
        path = settings.REPORTS_DIR / f"status_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        print(f"Status report saved: {path}")
    return data


# ============================================================
# Risk, paper and demo execution
# ============================================================


def position_risk_cash(
    symbol: str,
    side: int,
    volume: float,
    entry: float,
    stop: float,
) -> float:
    order_type = mt5.ORDER_TYPE_BUY if side == 1 else mt5.ORDER_TYPE_SELL
    potential = mt5.order_calc_profit(order_type, symbol, volume, entry, stop)
    return abs(safe_float(potential)) if potential is not None else 0.0


def volume_plan(
    symbol: str,
    side: int,
    entry: float,
    stop: float,
    equity: float,
    allow_minimum_bridge: bool = False,
    risk_fraction: float | None = None,
    hard_ceiling_fraction: float | None = None,
) -> dict[str, Any]:
    info = prepare_symbol(symbol)
    target_risk_pct = max(
        0.0,
        safe_float(
            settings.RISK_PER_TRADE if risk_fraction is None else risk_fraction
        ),
    )
    ceiling_pct = max(
        target_risk_pct,
        safe_float(
            getattr(settings, "MAX_DEMO_MIN_LOT_RISK_PCT", target_risk_pct)
            if hard_ceiling_fraction is None
            else hard_ceiling_fraction
        ),
    )
    target_risk_cash = max(0.0, equity * target_risk_pct)
    hard_ceiling_cash = max(target_risk_cash, equity * ceiling_pct)
    order_type = mt5.ORDER_TYPE_BUY if side == 1 else mt5.ORDER_TYPE_SELL
    one_lot = mt5.order_calc_profit(order_type, symbol, 1.0, entry, stop)
    loss_per_lot = abs(safe_float(one_lot)) if one_lot is not None else 0.0
    step = safe_float(info.volume_step)
    minimum = safe_float(info.volume_min)
    maximum = safe_float(info.volume_max)
    plan: dict[str, Any] = {
        "volume": 0.0,
        "actual_risk_cash": 0.0,
        "actual_risk_pct": 0.0,
        "target_risk_cash": target_risk_cash,
        "target_risk_pct": target_risk_pct,
        "hard_ceiling_cash": hard_ceiling_cash,
        "hard_ceiling_pct": ceiling_pct,
        "minimum_volume": minimum,
        "volume_step": step,
        "loss_per_lot": loss_per_lot,
        "used_minimum_bridge": False,
        "reason": "",
    }
    if equity <= 0 or target_risk_cash <= 0:
        plan["reason"] = "Account equity/risk budget is zero"
        return plan
    if step <= 0 or minimum <= 0 or maximum <= 0 or loss_per_lot <= 0:
        plan["reason"] = "Broker sizing/profit calculation unavailable"
        return plan

    raw_volume = target_risk_cash / loss_per_lot
    volume = math.floor(raw_volume / step + 1e-12) * step
    volume = min(volume, maximum)

    if volume < minimum:
        min_risk = position_risk_cash(symbol, side, minimum, entry, stop)
        plan["minimum_lot_risk_cash"] = min_risk
        plan["minimum_lot_risk_pct"] = min_risk / equity if equity > 0 else 0.0
        plan["required_equity_for_target"] = (
            min_risk / target_risk_pct if target_risk_pct > 0 else 0.0
        )
        bridge_enabled = bool(
            allow_minimum_bridge
            and getattr(settings, "ALLOW_DEMO_MINIMUM_LOT", False)
        )
        if bridge_enabled and min_risk > 0 and min_risk <= hard_ceiling_cash:
            volume = minimum
            plan["used_minimum_bridge"] = True
            plan["reason"] = "Broker minimum lot accepted inside demo hard ceiling"
        else:
            plan["reason"] = (
                "Minimum broker lot exceeds demo hard risk ceiling"
                if bridge_enabled
                else "Minimum broker lot exceeds configured target risk"
            )
            return plan

    decimals = max(0, len(f"{step:.10f}".rstrip("0").split(".")[-1]))
    volume = round(volume, decimals)
    actual_risk = position_risk_cash(symbol, side, volume, entry, stop)
    if actual_risk <= 0 or actual_risk > hard_ceiling_cash + 1e-9:
        plan["reason"] = "Calculated order risk exceeds hard ceiling"
        return plan
    plan["volume"] = volume
    plan["actual_risk_cash"] = actual_risk
    plan["actual_risk_pct"] = actual_risk / equity if equity > 0 else 0.0
    if not plan["reason"]:
        plan["reason"] = "Target-risk sizing accepted"
    return plan

def normalized_volume(
    symbol: str,
    side: int,
    entry: float,
    stop: float,
    equity: float,
) -> float:
    # Paper/backward-compatible sizing keeps the strict target-risk rule.
    return safe_float(
        volume_plan(symbol, side, entry, stop, equity, False).get("volume")
    )


def recent_portfolio_loss_streak(connection: sqlite3.Connection, limit: int = 8) -> dict[str, Any]:
    """Portfolio streak is advisory only in V9.2.1; it no longer hard-freezes all symbols."""
    rows = connection.execute(
        """
        SELECT reward_r, closed_at FROM demo_positions
        WHERE status='closed' AND reward_r IS NOT NULL
        ORDER BY COALESCE(closed_at, opened_at) DESC, id DESC LIMIT ?
        """,
        (max(1, min(20, int(limit))),),
    ).fetchall()
    streak = 0
    last_closed = None
    for row in rows:
        if last_closed is None:
            last_closed = parse_utc_timestamp(row["closed_at"])
        # A true losing trade increments the streak; exact break-even does not.
        if safe_float(row["reward_r"]) < 0:
            streak += 1
        else:
            break
    age_seconds = None
    if last_closed is not None:
        age_seconds = max(0.0, (datetime.now(timezone.utc) - last_closed).total_seconds())
    return {"loss_streak": streak, "last_closed_age_seconds": age_seconds}


def v921_loss_epoch(connection: sqlite3.Connection) -> str:
    """Persistent epoch: old V9/V9.1/V9.2 losses remain learning data, not V9.2.1 freeze debt."""
    key = "v9_2_1_symbol_loss_epoch"
    value = state_get(connection, key)
    if not value:
        value = utc_now()
        state_set(connection, key, value)
    return str(value)


def recent_symbol_loss_streak(
    connection: sqlite3.Connection,
    symbol: str,
    limit: int = 20,
) -> dict[str, Any]:
    epoch = v921_loss_epoch(connection)
    rows = connection.execute(
        """
        SELECT reward_r, closed_at, opened_at FROM demo_positions
        WHERE status='closed' AND reward_r IS NOT NULL
          AND symbol=?
          AND COALESCE(closed_at, opened_at) >= ?
          AND execution_tier LIKE 'v9%'
        ORDER BY COALESCE(closed_at, opened_at) DESC, id DESC LIMIT ?
        """,
        (symbol, epoch, max(1, min(50, int(limit)))),
    ).fetchall()
    streak = 0
    last_closed = None
    for row in rows:
        if last_closed is None:
            last_closed = parse_utc_timestamp(row["closed_at"] or row["opened_at"])
        if safe_float(row["reward_r"]) < 0:
            streak += 1
        else:
            break
    age_seconds = None
    if last_closed is not None:
        age_seconds = max(0.0, (datetime.now(timezone.utc) - last_closed).total_seconds())
    return {
        "symbol": symbol,
        "epoch": epoch,
        "loss_streak": streak,
        "last_closed_age_seconds": age_seconds,
    }


def symbol_loss_guard(
    connection: sqlite3.Connection,
    symbol: str,
) -> tuple[bool, str, dict[str, Any]]:
    state = recent_symbol_loss_streak(connection, symbol)
    freeze_after = max(2, int(getattr(settings, "PORTFOLIO_FREEZE_AFTER_LOSSES", 5)))
    freeze_seconds = max(60, int(getattr(settings, "PORTFOLIO_FREEZE_SECONDS", 900)))
    last_age = state.get("last_closed_age_seconds")
    details = {
        **state,
        "freeze_after": freeze_after,
        "freeze_seconds": freeze_seconds,
        "scope": "symbol_v9_2_1_epoch",
    }
    if (
        bool(getattr(settings, "PORTFOLIO_HARD_FREEZE_ENABLED", False))
        and int(state.get("loss_streak") or 0) >= freeze_after
        and last_age is not None
        and safe_float(last_age) < freeze_seconds
    ):
        details["freeze_remaining_seconds"] = max(0.0, freeze_seconds - safe_float(last_age))
        return False, f"{symbol} loss-streak freeze active", details
    return True, "OK", details


def portfolio_soft_risk_multiplier(guard: dict[str, Any]) -> tuple[float, str]:
    daily = safe_float(guard.get("daily_loss_pct"))
    drawdown = safe_float(guard.get("drawdown_pct"))
    if daily >= 0.040 or drawdown >= 0.120:
        multiplier, state = 0.35, "deep_defensive"
    elif daily >= 0.025 or drawdown >= 0.080:
        multiplier, state = 0.50, "defensive"
    elif daily >= 0.015 or drawdown >= 0.050:
        multiplier, state = 0.70, "cautious"
    elif daily >= 0.008 or drawdown >= 0.030:
        multiplier, state = 0.85, "watch"
    else:
        multiplier, state = 1.00, "normal"

    # Portfolio streak remains a soft de-risking signal, never a full multi-symbol stop.
    streak = int(guard.get("portfolio_loss_streak") or 0)
    if streak >= 3:
        multiplier *= safe_float(
            getattr(settings, "LOSS_STREAK_RISK_MULTIPLIER", 0.65), 0.65
        )
        state += f"+loss_streak_{streak}"
    return clamp(multiplier, 0.20, 1.0), state


def risk_guard(connection: sqlite3.Connection, account: Any) -> tuple[bool, str, dict[str, Any]]:
    """Portfolio guard: hard-stop only account-level daily loss / peak DD in V9.2.1."""
    today = utc_date()
    equity = safe_float(account.equity)
    day_key = f"risk_day:{today}"
    start_equity = safe_float(state_get(connection, day_key), equity)
    if state_get(connection, day_key) is None:
        state_set(connection, day_key, equity)
        start_equity = equity
    peak_key = "risk_peak_equity"
    peak = max(safe_float(state_get(connection, peak_key), equity), equity)
    state_set(connection, peak_key, peak)
    daily_loss = (start_equity - equity) / start_equity if start_equity > 0 else 0.0
    drawdown = (peak - equity) / peak if peak > 0 else 0.0
    streak_state = recent_portfolio_loss_streak(connection)
    details = {
        "equity": equity,
        "daily_start_equity": start_equity,
        "peak_equity": peak,
        "daily_loss_pct": daily_loss,
        "drawdown_pct": drawdown,
        "portfolio_loss_streak": int(streak_state.get("loss_streak") or 0),
        "last_closed_age_seconds": streak_state.get("last_closed_age_seconds"),
        "loss_streak_scope": "portfolio_advisory_only",
    }
    if daily_loss >= settings.MAX_DAILY_LOSS_PCT:
        return False, "Daily loss limit reached", details
    if drawdown >= settings.MAX_DRAWDOWN_PCT:
        return False, "Peak drawdown limit reached", details
    return True, "OK", details


def spartan_daily_trade_count(connection: sqlite3.Connection, symbol: str | None = None) -> int:
    today = utc_date()
    if symbol:
        row = connection.execute(
            "SELECT COUNT(*) AS n FROM demo_positions WHERE substr(opened_at,1,10)=? AND symbol=?",
            (today, symbol),
        ).fetchone()
    else:
        row = connection.execute(
            "SELECT COUNT(*) AS n FROM demo_positions WHERE substr(opened_at,1,10)=?",
            (today,),
        ).fetchone()
    return int(row["n"] if row else 0)



def _v92_normalize_close_volume(info: Any, value: float) -> float:
    step = max(1e-12, safe_float(getattr(info, "volume_step", 0.01), 0.01))
    minimum = max(step, safe_float(getattr(info, "volume_min", step), step))
    maximum = max(minimum, safe_float(getattr(info, "volume_max", value), value))
    if value < minimum - 1e-12:
        return 0.0
    units = math.floor((value + 1e-12) / step)
    return round(clamp(units * step, minimum, maximum), 8)


def _v92_close_position_volume(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    position: Any,
    volume: float,
    *,
    reason: str,
    profit_r: float,
) -> Any | None:
    symbol = str(row["symbol"])
    info = mt5.symbol_info(symbol)
    tick = mt5.symbol_info_tick(symbol)
    if info is None or tick is None:
        return None
    current_volume = safe_float(getattr(position, "volume", 0.0))
    close_volume = _v92_normalize_close_volume(info, min(current_volume, safe_float(volume)))
    if close_volume <= 0:
        return None
    side = int(row["side"] or 0)
    price = safe_float(tick.bid if side == 1 else tick.ask)
    accepted = {
        int(getattr(mt5, "TRADE_RETCODE_DONE", 10009)),
        int(getattr(mt5, "TRADE_RETCODE_PLACED", 10008)),
        int(getattr(mt5, "TRADE_RETCODE_DONE_PARTIAL", 10010)),
    }
    for fill_mode in filling_modes():
        request = {
            "action": getattr(mt5, "TRADE_ACTION_DEAL", 1),
            "symbol": symbol,
            "position": int(getattr(position, "ticket", row["position_ticket"] or 0)),
            "volume": close_volume,
            "type": getattr(mt5, "ORDER_TYPE_SELL", 1) if side == 1 else getattr(mt5, "ORDER_TYPE_BUY", 0),
            "price": price,
            "deviation": settings.DEFAULT_DEVIATION_POINTS,
            "magic": settings.MAGIC_NUMBER,
            "comment": f"V92-{reason}"[:31],
            "type_time": getattr(mt5, "ORDER_TIME_GTC", 0),
            "type_filling": fill_mode,
        }
        check = mt5.order_check(request)
        if check is None or int(getattr(check, "retcode", -1)) not in (0, *accepted):
            continue
        result = mt5.order_send(request)
        if result is not None and int(getattr(result, "retcode", -1)) in accepted:
            record_execution(
                connection, symbol, int(row["strategy_id"] or 0), request, result,
                "sent", f"v9_2_{reason}: ok",
            )
            log_decision(
                connection, "demo", symbol, str(row["regime"] or "unknown"),
                int(row["strategy_id"] or 0), side,
                f"V9.2 {reason}",
                {"position_ticket": int(getattr(position, "ticket", 0)), "volume": close_volume,
                 "profit_r": profit_r, "price": price},
            )
            return result
    return None


def spartan_manage_demo_positions(connection: sqlite3.Connection) -> None:
    """DEMO-only break-even/trailing manager using original stored R distance."""
    if not bool(getattr(settings, "SPARTAN_TRAILING_ENABLED", True)) and not bool(getattr(settings, "V11_ENABLED", False)):
        return
    account = mt5.account_info()
    demo_mode = int(getattr(mt5, "ACCOUNT_TRADE_MODE_DEMO", 0))
    if account is None or int(getattr(account, "trade_mode", -1)) != demo_mode:
        return
    open_rows = connection.execute(
        "SELECT * FROM demo_positions WHERE status='open' ORDER BY id"
    ).fetchall()
    if not open_rows:
        return
    positions = {int(getattr(p, "ticket", 0)): p for p in mt5_machine_positions()}
    accepted = {
        int(getattr(mt5, "TRADE_RETCODE_DONE", 10009)),
        int(getattr(mt5, "TRADE_RETCODE_PLACED", 10008)),
        int(getattr(mt5, "TRADE_RETCODE_DONE_PARTIAL", 10010)),
    }
    for row in open_rows:
        ticket = int(row["position_ticket"] or 0)
        position = positions.get(ticket)
        if position is None:
            continue
        entry_context_v11 = json.loads(str(row["context_json"] or "{}"))
        if entry_context_v11.get("v11"):
            scalp_v11.manage_position(sys.modules[__name__], connection, row, position, entry_context_v11)
            continue  # one versioned exit policy; no legacy tightening/partials
        if not bool(getattr(settings, "SPARTAN_TRAILING_ENABLED", True)):
            continue
        side = int(row["side"] or 0)
        if side not in (-1, 1):
            continue
        entry = safe_float(row["entry_price"])
        original_stop = safe_float(row["stop_loss"])
        risk_distance = abs(entry - original_stop)
        if risk_distance <= 0:
            continue
        tick = mt5.symbol_info_tick(str(row["symbol"]))
        info = mt5.symbol_info(str(row["symbol"]))
        if tick is None or info is None:
            continue
        current = safe_float(tick.bid if side == 1 else tick.ask)
        profit_r = ((current - entry) * side) / risk_distance
        current_sl = safe_float(getattr(position, "sl", 0.0))
        tp = safe_float(getattr(position, "tp", row["take_profit"]))
        candidate: float | None = None
        be_at = safe_float(getattr(settings, "SPARTAN_BREAK_EVEN_AT_R", 0.75), 0.75)
        be_lock = safe_float(getattr(settings, "SPARTAN_BREAK_EVEN_LOCK_R", 0.05), 0.05)
        trail_at = safe_float(getattr(settings, "SPARTAN_TRAILING_START_R", 1.0), 1.0)
        trail_distance_r = safe_float(getattr(settings, "SPARTAN_TRAILING_DISTANCE_R", 0.70), 0.70)
        # V7 micro-scalp positions have playbook-specific profit protection.
        # This only tightens management after entry; it cannot widen original risk.
        try:
            entry_context = json.loads(str(row["context_json"] or "{}")) if "context_json" in row.keys() else {}
            hunter_ctx = entry_context.get("micro_hunter", {}) if isinstance(entry_context, dict) else {}
            exit_profile = hunter_ctx.get("exit_profile", {}) if isinstance(hunter_ctx, dict) else {}
            if isinstance(exit_profile, dict) and exit_profile:
                be_at = min(be_at, max(0.30, safe_float(exit_profile.get("be_at_r"), be_at)))
                be_lock = max(0.0, min(be_lock, safe_float(exit_profile.get("be_lock_r"), be_lock)))
                trail_at = min(trail_at, max(be_at, safe_float(exit_profile.get("trail_at_r"), trail_at)))
                trail_distance_r = min(trail_distance_r, max(0.25, safe_float(exit_profile.get("trail_distance_r"), trail_distance_r)))
        except Exception:
            pass

        # V9.2 adaptive exit plan: dynamic ATR/MFE/MAE targets, real partial
        # scale-out when lot size permits, and adverse-flow early cut.
        v92_ctx: dict[str, Any] = {}
        v92_exit_plan: dict[str, Any] = {}
        if bool(getattr(settings, "V92_ADAPTIVE_SCALPING_ENABLED", False)):
            try:
                entry_context = json.loads(str(row["context_json"] or "{}")) if "context_json" in row.keys() else {}
                v92_ctx = entry_context.get("v9_2", {}) if isinstance(entry_context, dict) else {}
                v92_exit_plan = v92_ctx.get("exit_plan", {}) if isinstance(v92_ctx, dict) else {}
                if isinstance(v92_exit_plan, dict) and v92_exit_plan:
                    be_at = min(be_at, max(0.25, safe_float(v92_exit_plan.get("be_at_r"), be_at)))
                    trail_at = min(trail_at, max(be_at, safe_float(v92_exit_plan.get("trail_at_r"), trail_at)))
                    trail_distance_r = min(trail_distance_r, max(0.25, safe_float(v92_exit_plan.get("trail_distance_r"), trail_distance_r)))
            except Exception:
                v92_ctx, v92_exit_plan = {}, {}

        if v92_exit_plan and str(row["execution_tier"] or "").startswith("v9"):
            # A losing position is never averaged down. If both DOM and tick flow
            # turn strongly against it after a minimum hold, close early and let
            # the next fresh opposite signal re-enter independently.
            if bool(getattr(settings, "V92_ADVERSE_FLOW_CUT_ENABLED", True)) and profit_r <= safe_float(getattr(settings, "V92_ADVERSE_FLOW_CUT_AT_R", -0.48), -0.48):
                try:
                    opened = datetime.fromisoformat(str(row["opened_at"]))
                    if opened.tzinfo is None:
                        opened = opened.replace(tzinfo=timezone.utc)
                    age_s = (datetime.now(timezone.utc) - opened).total_seconds()
                except Exception:
                    age_s = 999.0
                if age_s >= int(getattr(settings, "V92_ADVERSE_FLOW_MIN_SECONDS_OPEN", 25)):
                    dom_now = order_book_microstructure(str(row["symbol"]))
                    tick_flow_now = spartan.tick_flow_snapshot(str(row["symbol"]), seconds=15)
                    dom_score = safe_float(dom_now.get("score"), 0.0)
                    tick_delta = safe_float(tick_flow_now.get("delta_ratio"), 0.0)
                    aligned = side * (0.60 * dom_score + 0.40 * tick_delta)
                    if aligned <= safe_float(getattr(settings, "V92_ADVERSE_FLOW_COMBINED_THRESHOLD", -0.58), -0.58):
                        result = _v92_close_position_volume(
                            connection, row, position, safe_float(getattr(position, "volume", 0.0)),
                            reason="adverse_flow_cut", profit_r=profit_r,
                        )
                        if result is not None:
                            print(f"V9.2 CUT {row['symbol']} ticket={ticket} profit={profit_r:.2f}R adverse_flow={aligned:.2f}")
                            continue

            # Partial scale-out. Tiny 0.01-lot positions cannot be split; in that
            # case crossing a partial target tightens the SL instead.
            partial_lock_candidate: float | None = None
            partial_sent = False
            if bool(getattr(settings, "V92_PARTIAL_EXIT_ENABLED", True)):
                try:
                    entry_context = json.loads(str(row["context_json"] or "{}"))
                except Exception:
                    entry_context = {}
                v92_store = entry_context.setdefault("v9_2", {}) if isinstance(entry_context, dict) else {}
                done = list(v92_store.get("partial_done") or []) if isinstance(v92_store, dict) else []
                partials = v92_exit_plan.get("partials") if isinstance(v92_exit_plan.get("partials"), list) else []
                for stage in partials:
                    stage_name = str(stage.get("stage") or "p")
                    stage_r = safe_float(stage.get("at_r"), 99.0)
                    if stage_name in done or profit_r < max(safe_float(getattr(settings, "V92_PARTIAL_MIN_PROFIT_R", 0.30), 0.30), stage_r):
                        continue
                    info2 = mt5.symbol_info(str(row["symbol"]))
                    current_volume = safe_float(getattr(position, "volume", 0.0))
                    original_volume = safe_float(row["volume"], current_volume)
                    fraction = clamp(safe_float(stage.get("fraction"), 0.0), 0.0, 0.90)
                    desired = original_volume * fraction
                    close_volume = _v92_normalize_close_volume(info2, desired) if info2 is not None else 0.0
                    min_volume = safe_float(getattr(info2, "volume_min", 0.01), 0.01) if info2 is not None else 0.01
                    if close_volume > 0 and current_volume - close_volume >= min_volume - 1e-12:
                        result = _v92_close_position_volume(
                            connection, row, position, close_volume, reason=f"partial_{stage_name}", profit_r=profit_r,
                        )
                        if result is not None:
                            done.append(stage_name)
                            v92_store["partial_done"] = done
                            connection.execute(
                                "UPDATE demo_positions SET context_json=? WHERE id=?",
                                (json_text(entry_context), int(row["id"])),
                            )
                            print(f"V9.2 PARTIAL {row['symbol']} {stage_name} profit={profit_r:.2f}R volume={close_volume}")
                            partial_sent = True
                            break
                    # Minimum-lot fallback: mark stage and convert it to a profit lock.
                    done.append(stage_name)
                    v92_store["partial_done"] = done
                    connection.execute(
                        "UPDATE demo_positions SET context_json=? WHERE id=?",
                        (json_text(entry_context), int(row["id"])),
                    )
                    lock_r = max(0.03, safe_float(stage.get("lock_r"), 0.05))
                    partial_lock_candidate = entry + side * lock_r * risk_distance
                    break
            if partial_sent:
                continue
            if partial_lock_candidate is not None:
                candidate = partial_lock_candidate

        # V9.1 precision exit optimizer: protect weaker/recently-poor setups
        # earlier while A/A+ runners retain more room. This only tightens SL;
        # it can never widen the original stop or add exposure.
        if bool(getattr(settings, "V9_PRECISION_EXIT_ENABLED", False)):
            try:
                entry_context = json.loads(str(row["context_json"] or "{}")) if "context_json" in row.keys() else {}
                v9_ctx = entry_context.get("v9_execution", {}) if isinstance(entry_context, dict) else {}
                hunter_ctx = entry_context.get("micro_hunter", {}) if isinstance(entry_context, dict) else {}
                mem = hunter_ctx.get("playbook_memory", {}) if isinstance(hunter_ctx, dict) else {}
                grade = str(v9_ctx.get("grade") or "B").upper()
                mem_n = int(mem.get("observations") or 0) if isinstance(mem, dict) else 0
                mem_ewma = safe_float(mem.get("reward_ewma"), 0.0) if isinstance(mem, dict) else 0.0
                mem_win = safe_float(mem.get("win_rate"), 0.50) if isinstance(mem, dict) else 0.50

                if grade == "A+":
                    be_at = min(be_at, safe_float(getattr(settings, "V9_A_PLUS_BE_AT_R", 0.65), 0.65))
                elif grade == "A":
                    be_at = min(be_at, safe_float(getattr(settings, "V9_A_BE_AT_R", 0.55), 0.55))
                else:
                    be_at = min(be_at, safe_float(getattr(settings, "V9_B_BE_AT_R", 0.45), 0.45))
                    trail_at = min(trail_at, safe_float(getattr(settings, "V9_B_TRAIL_START_R", 0.80), 0.80))
                    trail_distance_r = min(trail_distance_r, safe_float(getattr(settings, "V9_B_TRAIL_DISTANCE_R", 0.55), 0.55))

                # Once there is enough real broker evidence that this playbook is
                # struggling, bank open profit sooner rather than trying to win it
                # back by increasing size.
                if mem_n >= int(getattr(settings, "V9_PRECISION_MEMORY_MIN_OBSERVATIONS", 4)) and (
                    mem_ewma < safe_float(getattr(settings, "V9_PRECISION_WEAK_EWMA_R", -0.08), -0.08)
                    or mem_win < safe_float(getattr(settings, "V9_PRECISION_WEAK_WIN_RATE", 0.35), 0.35)
                ):
                    be_at = min(be_at, safe_float(getattr(settings, "V9_WEAK_MEMORY_BE_AT_R", 0.35), 0.35))
                    trail_at = min(trail_at, safe_float(getattr(settings, "V9_WEAK_MEMORY_TRAIL_START_R", 0.65), 0.65))
                    trail_distance_r = min(trail_distance_r, safe_float(getattr(settings, "V9_WEAK_MEMORY_TRAIL_DISTANCE_R", 0.45), 0.45))
            except Exception:
                pass
        if profit_r >= be_at:
            be_candidate = entry + side * be_lock * risk_distance
            if candidate is None:
                candidate = be_candidate
            elif side == 1:
                candidate = max(candidate, be_candidate)
            else:
                candidate = min(candidate, be_candidate)
        if profit_r >= trail_at:
            trailing = current - side * trail_distance_r * risk_distance
            if candidate is None:
                candidate = trailing
            elif side == 1:
                candidate = max(candidate, trailing)
            else:
                candidate = min(candidate, trailing)
        if candidate is None:
            continue
        point = max(1e-12, safe_float(getattr(info, "point", 0.0), 1e-5))
        digits = int(getattr(info, "digits", 5))
        min_distance = max(0, int(getattr(info, "trade_stops_level", 0))) * point
        if side == 1:
            candidate = min(candidate, safe_float(tick.bid) - min_distance)
            improves = current_sl <= 0 or candidate > current_sl + point
        else:
            candidate = max(candidate, safe_float(tick.ask) + min_distance)
            improves = current_sl <= 0 or candidate < current_sl - point
        candidate = round(candidate, digits)
        if not improves or candidate <= 0:
            continue
        request = {
            "action": getattr(mt5, "TRADE_ACTION_SLTP", 6),
            "symbol": str(row["symbol"]),
            "position": ticket,
            "sl": candidate,
            "tp": tp,
            "magic": settings.MAGIC_NUMBER,
        }
        result = mt5.order_send(request)
        if result is not None and int(getattr(result, "retcode", -1)) in accepted:
            log_decision(
                connection, "demo", str(row["symbol"]), str(row["regime"] or "unknown"),
                int(row["strategy_id"] or 0), side,
                "Spartan trailing stop modified",
                {"position_ticket": ticket, "profit_r": profit_r, "old_sl": current_sl, "new_sl": candidate, "tp": tp},
            )
            print(f"TRAIL {row['symbol']} ticket={ticket} profit={profit_r:.2f}R SL {current_sl} -> {candidate}")


def mt5_machine_positions() -> list[Any]:
    positions = mt5.positions_get()
    if not positions:
        return []
    return [p for p in positions if int(getattr(p, "magic", -1)) == settings.MAGIC_NUMBER]


def latest_machine_position_ticket(symbol: str) -> int:
    positions = [p for p in mt5_machine_positions() if str(p.symbol) == symbol]
    if not positions:
        return 0
    latest = max(
        positions,
        key=lambda p: (
            int(getattr(p, "time_msc", 0)),
            int(getattr(p, "time", 0)),
            int(getattr(p, "ticket", 0)),
        ),
    )
    return int(getattr(latest, "ticket", 0))


def register_demo_position(
    connection: sqlite3.Connection,
    position_ticket: int,
    strategy: StrategyDefinition,
    regime: str,
    side: int,
    volume: float,
    entry: float,
    stop: float,
    take: float,
    risk_cash: float,
    result: Any,
    execution_tier: str,
    context: dict[str, Any],
    learning_confidence: float,
    risk_multiplier: float,
    super_probability: float = 0.50,
    super_decision: dict[str, Any] | None = None,
    sl_multiplier: float = 1.0,
    tp_multiplier: float = 1.0,
) -> None:
    if position_ticket <= 0:
        position_ticket = int(getattr(result, "order", 0) or getattr(result, "deal", 0) or 0)
    connection.execute(
        """
        INSERT INTO demo_positions(
            position_ticket, strategy_id, symbol, regime, side, volume,
            entry_price, stop_loss, take_profit, risk_cash, opened_at,
            status, result_json, execution_tier, setup_key, context_json,
            learning_confidence, risk_multiplier, mfe_r, mae_r, super_probability,
            super_decision_json, sl_multiplier, tp_multiplier
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            position_ticket, strategy.strategy_id, strategy.symbol, regime, side,
            volume, entry, stop, take, risk_cash, utc_now(), json_text(as_dict(result)),
            execution_tier,
            adaptive_setup_key(strategy.strategy_id, strategy.symbol, regime, side),
            json_text(context), safe_float(learning_confidence, 0.50),
            safe_float(risk_multiplier, 1.0), 0.0, 0.0,
            safe_float(super_probability, 0.50), json_text(super_decision or {}),
            safe_float(sl_multiplier, 1.0), safe_float(tp_multiplier, 1.0),
        ),
    )


def reconcile_demo_positions(connection: sqlite3.Connection) -> None:
    rows = connection.execute(
        "SELECT * FROM demo_positions WHERE status='open' ORDER BY id"
    ).fetchall()
    if not rows:
        return
    current_tickets = {int(getattr(p, "ticket", 0)) for p in mt5_machine_positions()}
    out_codes = {
        int(getattr(mt5, "DEAL_ENTRY_OUT", 1)),
        int(getattr(mt5, "DEAL_ENTRY_OUT_BY", 3)),
        int(getattr(mt5, "DEAL_ENTRY_INOUT", 2)),
    }
    for row in rows:
        ticket = int(row["position_ticket"] or 0)
        if ticket > 0 and ticket in current_tickets:
            continue
        try:
            deals = mt5.history_deals_get(position=ticket) if ticket > 0 else None
        except Exception:
            deals = None
        if not deals:
            continue
        closing = [d for d in deals if int(getattr(d, "entry", -1)) in out_codes]
        if not closing and len(deals) < 2:
            continue
        pnl = sum(
            safe_float(getattr(d, "profit", 0.0))
            + safe_float(getattr(d, "commission", 0.0))
            + safe_float(getattr(d, "swap", 0.0))
            + safe_float(getattr(d, "fee", 0.0))
            for d in deals
        )
        risk_cash = safe_float(row["risk_cash"])
        reward_r = pnl / risk_cash if risk_cash > 0 else 0.0
        final_deal = max(
            deals,
            key=lambda d: (
                int(getattr(d, "time_msc", 0)),
                int(getattr(d, "time", 0)),
            ),
        )
        close_timestamp = int(getattr(final_deal, "time", 0))
        closed_at = (
            datetime.fromtimestamp(close_timestamp, tz=timezone.utc).isoformat()
            if close_timestamp > 0
            else utc_now()
        )
        mfe_r = max(safe_float(row["mfe_r"]), max(0.0, reward_r)) if "mfe_r" in row.keys() else max(0.0, reward_r)
        mae_r = max(safe_float(row["mae_r"]), max(0.0, -reward_r)) if "mae_r" in row.keys() else max(0.0, -reward_r)
        entry_context_v11 = json.loads(str(row["context_json"] or "{}"))
        actual_close_reason = (scalp_v11.close_reason(mt5, final_deal, entry_context_v11)
                               if entry_context_v11.get("v11") else "broker_sl_tp_or_manual")
        connection.execute(
            """
            UPDATE demo_positions
            SET status='closed', closed_at=?, pnl=?, reward_r=?, mfe_r=?, mae_r=?,
                close_reason=?
            WHERE id=?
            """,
            (closed_at, pnl, reward_r, mfe_r, mae_r, actual_close_reason, int(row["id"])),
        )
        if entry_context_v11.get("v11"):
            scalp_v11.broker_outcome(connection, row, entry_context_v11, reward_r, pnl,
                int(getattr(final_deal, "time_msc", 0) or close_timestamp*1000), actual_close_reason)
        reward_details = {
            "demo_position_id": int(row["id"]),
            "position_ticket": ticket,
            "pnl": pnl,
            "execution_tier": row["execution_tier"] if "execution_tier" in row.keys() else None,
            "setup_key": row["setup_key"] if "setup_key" in row.keys() else None,
            "entry_context": (
                json.loads(str(row["context_json"]))
                if "context_json" in row.keys() and row["context_json"]
                else {}
            ),
            "mfe_r": mfe_r,
            "mae_r": mae_r,
            "deals": [as_dict(d) for d in deals],
        }
        update_reward_memory(
            connection,
            int(row["strategy_id"]),
            str(row["symbol"]),
            str(row["regime"]),
            reward_r,
            "demo_trade",
            reward_details,
        )
        adaptive = update_adaptive_learning(
            connection,
            int(row["strategy_id"]),
            str(row["symbol"]),
            str(row["regime"]),
            int(row["side"]),
            reward_r,
            "demo_trade",
            reward_details,
            closed_at=closed_at,
        )
        strategy_row = connection.execute(
            "SELECT family FROM strategies WHERE id=?", (int(row["strategy_id"]),)
        ).fetchone()
        if strategy_row:
            family_name = str(strategy_row["family"])
            update_collective_memory(
                connection, str(row["symbol"]), family_name,
                str(row["regime"]), int(row["side"]), reward_r, 1.0,
            )
            update_session_family_memory(
                connection, str(row["symbol"]), family_name, str(row["regime"]),
                market_session_tag(str(row["symbol"]), row["opened_at"]),
                int(row["side"]), reward_r, 1.0,
            )
        try:
            entry_context = reward_details.get("entry_context") or {}
            super_data = entry_context.get("superlearner", {}) if isinstance(entry_context, dict) else {}
            features = super_data.get("features", {}) if isinstance(super_data, dict) else {}
            if features:
                update_online_model(connection, str(row["symbol"]), features, reward_r, 1.0)
        except Exception as model_error:
            print(f"SUPERLEARNER UPDATE WARNING: {model_error}")

        # V6.5 GPT decision-memory: CONFIRM outcomes calibrate the reviewer.
        # This is outcome memory only; it cannot change hard risk/session/broker rules.
        try:
            entry_context = reward_details.get("entry_context") or {}
            llm_context = entry_context.get("spartan_llm", {}) if isinstance(entry_context, dict) else {}
            fingerprint = str(llm_context.get("fingerprint") or "") if isinstance(llm_context, dict) else ""
            if fingerprint:
                gpt_memory.record_confirm_outcome(
                    connection,
                    fingerprint=fingerprint,
                    symbol=str(row["symbol"]),
                    regime=str(row["regime"]),
                    side=int(row["side"]),
                    reward_r=reward_r,
                    closed_at=closed_at,
                )
        except Exception as gpt_memory_error:
            print(f"GPT DECISION MEMORY WARNING: {type(gpt_memory_error).__name__}: {gpt_memory_error}")

        # V7 micro-scalp learning: the playbook brain learns from actual broker-DEMO
        # MFE/MAE and final R. Shadow research never enters this memory.
        try:
            micro_hunter.record_demo_outcome(
                connection, symbol=str(row["symbol"]), side=int(row["side"]),
                reward_r=reward_r, mfe_r=mfe_r, mae_r=mae_r,
                context=reward_details.get("entry_context") or {},
            )
        except Exception as micro_learning_error:
            print(f"V7 MICRO HUNTER LEARNING WARNING: {type(micro_learning_error).__name__}: {micro_learning_error}")

        # V9.2 contextual broker learning: symbol + playbook + side + regime +
        # session learns immediately from real DEMO R/MFE/MAE.
        if bool(getattr(settings, "V92_ADAPTIVE_SCALPING_ENABLED", False)):
            try:
                v92.record_broker_outcome(
                    connection, symbol=str(row["symbol"]), side=int(row["side"]),
                    regime=str(row["regime"]), reward_r=reward_r,
                    mfe_r=mfe_r, mae_r=mae_r,
                    entry_context=reward_details.get("entry_context") or {},
                )
            except Exception as v92_learning_error:
                print(f"V9.2 CONTEXT LEARNING WARNING: {type(v92_learning_error).__name__}: {v92_learning_error}")

        # Level-3 GPT post-trade analyst: asynchronous and advisory only.
        # Its hypotheses are journaled for research and are NEVER read back into
        # live risk/strategy settings automatically.
        try:
            entry_context = reward_details.get("entry_context") or {}
            post_trade_payload = {
                "demo_position_id": int(row["id"]),
                "position_ticket": ticket,
                "symbol": str(row["symbol"]),
                "strategy_id": int(row["strategy_id"]),
                "strategy_family": family_name if strategy_row else "unknown",
                "regime": str(row["regime"]),
                "side": "buy" if int(row["side"]) == 1 else "sell",
                "execution_tier": str(row["execution_tier"] if "execution_tier" in row.keys() else "unknown"),
                "opened_at": str(row["opened_at"]),
                "closed_at": closed_at,
                "entry_price": safe_float(row["entry_price"]),
                "stop_loss": safe_float(row["stop_loss"]),
                "take_profit": safe_float(row["take_profit"]),
                "volume": safe_float(row["volume"]),
                "risk_cash": risk_cash,
                "pnl": pnl,
                "reward_r": reward_r,
                "mfe_r": mfe_r,
                "mae_r": mae_r,
                "close_reason": "broker_sl_tp_or_manual",
                "entry_context": entry_context,
                "adaptive_learning_after": {
                    "confidence": safe_float(adaptive.get("confidence"), 0.50),
                    "consecutive_losses": int(adaptive.get("consecutive_losses") or 0),
                    "blocked_until": adaptive.get("blocked_until"),
                },
                "deals": [as_dict(d) for d in deals[-8:]],
                "post_trade_policy": {
                    "advisory_only": True,
                    "automatic_live_parameter_changes": False,
                },
            }
            spartan_posttrade_enqueue(post_trade_payload)
        except Exception as post_error:
            print(f"GPT POST-TRADE QUEUE WARNING: {type(post_error).__name__}: {post_error}")

        print(
            f"DEMO CLOSED {row['symbol']} | strategy={row['strategy_id']} | "
            f"pnl={pnl:.2f} | reward={reward_r:.2f}R | "
            f"learn_conf={safe_float(adaptive.get('confidence'), 0.50):.2f} "
            f"loss_streak={int(adaptive.get('consecutive_losses') or 0)}"
        )


def cooldown_ready(connection: sqlite3.Connection, symbol: str) -> bool:
    value = state_get(connection, f"last_order:{symbol}")
    if not value:
        return True
    try:
        previous = datetime.fromisoformat(value)
        return (datetime.now(timezone.utc) - previous).total_seconds() >= settings.COOLDOWN_SECONDS
    except ValueError:
        return True


def record_execution(
    connection: sqlite3.Connection,
    symbol: str,
    strategy_id: int | None,
    request: dict[str, Any] | None,
    result: Any,
    status: str,
    message: str,
) -> None:
    connection.execute(
        """
        INSERT INTO execution_logs(
            timestamp, symbol, strategy_id, request_json,
            result_json, status, message
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            utc_now(),
            symbol,
            strategy_id,
            json_text(request) if request else None,
            json_text(as_dict(result)) if result is not None else None,
            status,
            message,
        ),
    )


def filling_modes() -> list[int]:
    modes = [
        getattr(mt5, "ORDER_FILLING_RETURN", 2),
        getattr(mt5, "ORDER_FILLING_IOC", 1),
        getattr(mt5, "ORDER_FILLING_FOK", 0),
    ]
    return list(dict.fromkeys(int(x) for x in modes))


def send_demo_order(
    connection: sqlite3.Connection,
    symbol: str,
    strategy: StrategyDefinition,
    signal: int,
    entry: float,
    stop: float,
    take: float,
    volume: float,
    execution_tier: str = "shadow_approved",
) -> Any:
    if not settings.ENABLE_DEMO_ORDER_EXECUTION:
        raise RuntimeError("Demo order execution is OFF in settings.py")
    verify_demo_account()
    accepted = {
        int(getattr(mt5, "TRADE_RETCODE_DONE", 10009)),
        int(getattr(mt5, "TRADE_RETCODE_PLACED", 10008)),
        int(getattr(mt5, "TRADE_RETCODE_DONE_PARTIAL", 10010)),
    }
    last_result: Any = None
    last_request: dict[str, Any] | None = None
    tier_tag = "A" if execution_tier == "shadow_approved" else "T"
    for fill_mode in filling_modes():
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": volume,
            "type": mt5.ORDER_TYPE_BUY if signal == 1 else mt5.ORDER_TYPE_SELL,
            "price": entry,
            "sl": stop,
            "tp": take,
            "deviation": settings.DEFAULT_DEVIATION_POINTS,
            "magic": settings.MAGIC_NUMBER,
            "comment": f"TM5{tier_tag}-{strategy.strategy_id}-{strategy.family[:4]}"[:31],
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": fill_mode,
        }
        last_request = request
        check = mt5.order_check(request)
        if check is None:
            continue
        check_retcode = int(getattr(check, "retcode", -1))
        if check_retcode not in (0, *accepted):
            last_result = check
            continue
        result = mt5.order_send(request)
        last_result = result
        if result is not None and int(getattr(result, "retcode", -1)) in accepted:
            record_execution(
                connection,
                symbol,
                strategy.strategy_id,
                request,
                result,
                "sent",
                f"{execution_tier}: {getattr(result, 'comment', 'Order accepted')}",
            )
            state_set(connection, f"last_order:{symbol}", utc_now())
            return result

    message = (
        str(getattr(last_result, "comment", "order_check/order_send failed"))
        if last_result is not None
        else f"No result; last_error={mt5.last_error()}"
    )
    record_execution(
        connection,
        symbol,
        strategy.strategy_id,
        last_request,
        last_result,
        "rejected",
        f"{execution_tier}: {message}",
    )
    raise RuntimeError(f"Order rejected: {message}")


def v9_execute_hunter_candidate(
    connection: sqlite3.Connection,
    *,
    symbol: str,
    strategy: StrategyDefinition,
    signal: int,
    regime: str,
    details: dict[str, Any],
    frame: pd.DataFrame,
    row: pd.Series,
    tick: Any,
    atr_value: float,
    spread: float,
    micro: dict[str, Any],
    dom: float | None,
    account: Any,
    machine_positions: list[Any],
    hunter_setup: micro_hunter.MicroSetup,
    growth_state: dict[str, Any],
    guard: dict[str, Any],
) -> bool:
    """V9 DEMO-only execution-first path for a real Micro Hunter trigger.

    The live alpha/playbook owns the entry decision. Spartan, adaptive memory,
    SuperLearner, Edge Recovery and Luna remain ensemble evidence and modify
    confidence/risk. They are not serial vetoes. Hard execution blockers remain:
    DEMO account lock, global drawdown/risk guard (upstream), position/cooldown
    limits (upstream), cost-aware spread gate (upstream), severe negative Quant
    expectancy, news lock, daily V9 trade cap, fresh-price drift, broker-valid
    sizing, order_check and order_send.
    """
    if not bool(getattr(settings, "V9_EXECUTION_FIRST_ENABLED", False)):
        return False

    v9_exec.ensure_tables(connection)
    slot_ok, slot = v9_exec.trade_slot_available(connection, symbol)
    if not slot_ok:
        log_decision(
            connection, "demo", symbol, regime, strategy.strategy_id, signal,
            "V9 execution-first daily trade cap reached", {**details, "v9_slot": slot},
        )
        scalp_diag.record(symbol, "v9_cap", f"{slot['per_symbol']}/{slot['symbol_cap']} symbol; {slot['total']}/{slot['total_cap']} total")
        return True

    playbook = str((details.get("micro_hunter") or {}).get("playbook") or hunter_setup.playbook)
    micro_score = safe_float((details.get("micro_hunter") or {}).get("score"), safe_float(hunter_setup.score))

    # Deterministic Spartan snapshot is retained as context, not a serial veto.
    spartan_snapshot: dict[str, Any] = {"enabled": False, "approved": True}
    if bool(getattr(settings, "ENABLE_SPARTAN_PRO", True)):
        try:
            spartan_snapshot = spartan.build_gate_snapshot(
                symbol, frame, tick, signal, micro,
                daily_trade_count=spartan_daily_trade_count(connection),
                fetch_htf=True,
                playbook=playbook,
                micro_score=micro_score,
                execution_tier="v8_native_alpha",
            )
        except Exception as sp_error:
            spartan_snapshot = {
                "enabled": True, "approved": False,
                "reason": f"snapshot_error:{type(sp_error).__name__}",
                "candidate": {"action": "buy" if signal == 1 else "sell", "confidence": 0.0},
                "condition_scores": {}, "news": {}, "session": {},
            }
    details["spartan_pro"] = spartan_snapshot

    # News lock remains authoritative. Session/ADX/confluence are advisory in V9.
    news = spartan_snapshot.get("news") if isinstance(spartan_snapshot.get("news"), dict) else {}
    if bool(news.get("locked")):
        pre = {
            "pre_score": micro_score, "micro_score": micro_score,
            "expected_r": 0.0, "probability": 0.5,
            "break_even_probability": 0.5, "probability_edge": 0.0,
            "spartan_approved": bool(spartan_snapshot.get("approved", False)),
            "adaptive_approved": True, "edge_allowed": True,
            "hard_hold": True, "reason": "authoritative news lock",
        }
        decision = v9_exec.final_decision(
            pre=pre, super_decision=type("S", (), {"approved": False})(),
            spartan_snapshot=spartan_snapshot, adaptive_allowed=True,
            edge_allowed=True, luna_review=None,
        )
        v9_exec.record_decision(
            connection, symbol=symbol, strategy_id=int(strategy.strategy_id), side=int(signal),
            playbook=playbook, decision=decision, context={"spartan": spartan_snapshot},
        )
        log_decision(connection, "demo", symbol, regime, strategy.strategy_id, signal, "V9 hard hold: news lock", details)
        return True

    # Learning memory is advisory: loss memory reduces size but does not by itself
    # delete a fresh micro trigger.
    adaptive_allowed, adaptive_reason, adaptive_gate = adaptive_learning_gate(
        connection, strategy, regime, signal, "v8_native_alpha", details
    )
    details["adaptive_learning"] = adaptive_gate
    details["adaptive_learning_advisory_reason"] = adaptive_reason

    # SuperLearner always evaluates and is logged, but V9 uses its probability /
    # Expected-R as evidence rather than a mandatory yes/no permission layer.
    super_decision = SUPER_LEARNER.predict(
        connection, strategy, frame, row, regime, signal, details,
        adaptive_gate, spread, atr_value, micro,
    )
    log_superlearner_decision(
        connection, symbol, strategy.strategy_id, regime, signal, super_decision
    )
    details["superlearner"] = super_decision.as_dict()

    # Edge Recovery remains valuable evidence. A quarantine now de-risks the V9
    # entry instead of blocking all broker evidence forever.
    edge_allowed, edge_risk_mult, edge_state = scalp_lab.edge_recovery_guard(
        connection, symbol=symbol, family=str(getattr(strategy, "family", "unknown")),
        strategy_id=int(strategy.strategy_id), execution_tier="v8_native_alpha",
    )
    details["edge_recovery"] = edge_state

    # Consume broker playbook memory BEFORE the entry decision. V9.1 previously
    # fetched this memory only after GO/HOLD, which meant some live decisions
    # displayed mem=legacy even though the broker memory existed.
    if bool(getattr(hunter_setup, "v10_synthetic", False)):
        try:
            early_hunter_memory = micro_hunter.memory_snapshot(
                connection, symbol, playbook, int(signal)
            )
        except Exception:
            early_hunter_memory = {"observations": 0, "win_rate": 0.50, "reward_ewma": 0.0, "reward_mean": 0.0}
        early_hunter_risk_mult = 1.0
        details.setdefault("micro_hunter", {})["v10_superhuman"] = True
    else:
        early_hunter_risk_mult, early_hunter_memory = micro_hunter.execution_risk_multiplier(
            connection, hunter_setup
        )
    details.setdefault("micro_hunter", {})["playbook_memory"] = early_hunter_memory

    pre = v9_exec.pre_luna_score(
        micro_score=micro_score,
        super_decision=super_decision,
        spartan_snapshot=spartan_snapshot,
        adaptive_allowed=adaptive_allowed,
        adaptive_gate=adaptive_gate,
        edge_allowed=edge_allowed,
        playbook_memory=early_hunter_memory,
    )
    details["v9_pre_luna"] = pre

    v92_overlay: dict[str, Any] = {}
    if bool(getattr(settings, "V92_ADAPTIVE_SCALPING_ENABLED", False)):
        v92_overlay = v92.entry_overlay(
            connection, symbol=symbol, playbook=playbook, side=int(signal),
            regime=str(regime), pre=pre, spartan_snapshot=spartan_snapshot, micro=micro,
            session_hint=market_session_tag(symbol, row.get("time") if hasattr(row, "get") else None),
        )
        # Rejected-trade outcomes can nudge the score floor by only +/-1.5.
        rejected_bias = v92.rejected_threshold_bias(connection, symbol)
        if rejected_bias.get("score_floor_delta"):
            v92_overlay["execute_floor"] = clamp(
                safe_float(v92_overlay.get("execute_floor"), safe_float(getattr(settings, "V9_EXECUTE_SCORE_MIN", 62.0))),
                safe_float(getattr(settings, "V92_MIN_ADAPTIVE_EXECUTE_FLOOR", 59.0)),
                safe_float(getattr(settings, "V92_MAX_ADAPTIVE_EXECUTE_FLOOR", 69.0)),
            ) + safe_float(rejected_bias.get("score_floor_delta"))
        v92_overlay["rejected_trade_bias"] = rejected_bias
        pre = v92.apply_overlay(pre, v92_overlay)
        details["v9_2"] = {"overlay": v92_overlay}
        details["v9_pre_luna"] = pre

    if bool(pre.get("hard_hold")):
        decision = v9_exec.final_decision(
            pre=pre, super_decision=super_decision,
            spartan_snapshot=spartan_snapshot, adaptive_allowed=adaptive_allowed,
            edge_allowed=edge_allowed, luna_review=None,
        )
        v9_exec.record_decision(
            connection, symbol=symbol, strategy_id=int(strategy.strategy_id), side=int(signal),
            playbook=playbook, decision=decision, context=details,
        )
        log_decision(
            connection, "demo", symbol, regime, strategy.strategy_id, signal,
            f"V9 hard Quant hold: {decision.get('reason')}", details,
        )
        scalp_diag.record(symbol, "v9_hard_hold", str(decision.get("reason") or "negative quant"))
        print(
            f"{symbol}: V9 HARD HOLD | {playbook} score={micro_score:.1f} "
            f"ER={safe_float(pre.get('expected_r')):+.2f}R edge={safe_float(pre.get('probability_edge')):+.2f}"
        )
        if bool(getattr(settings, "V92_REJECTED_TRADE_LEARNING_ENABLED", False)):
            try:
                base_stop = safe_float((details.get("micro_hunter") or {}).get("stop_atr"), safe_float(strategy.params.get("stop_atr", 0.8)))
                base_take = safe_float((details.get("micro_hunter") or {}).get("take_atr"), safe_float(strategy.params.get("take_atr", 1.2)))
                plan = v92.dynamic_exit_plan(
                    base_stop_atr=base_stop, base_take_atr=base_take, atr_value=atr_value,
                    frame=frame, grade="B", playbook=playbook,
                    memory=(v92_overlay.get("context_memory") or {}), overlay=v92_overlay,
                )
                virtual_entry = safe_float(tick.ask if signal == 1 else tick.bid)
                rd = safe_float(plan.get("stop_atr"), base_stop) * atr_value
                td = safe_float(plan.get("take_atr"), base_take) * atr_value
                v92.record_virtual_trial(
                    connection, trial_type="rejected", symbol=symbol, playbook=playbook,
                    side=int(signal), regime=str(regime), session=str(v92_overlay.get("session") or "unknown"),
                    score=safe_float(pre.get("pre_score")), entry=virtual_entry,
                    stop=virtual_entry-rd if signal==1 else virtual_entry+rd,
                    take=virtual_entry+td if signal==1 else virtual_entry-td,
                    reason=str(decision.get("reason") or "hard_hold"),
                    context={"pre": pre, "overlay": v92_overlay},
                )
            except Exception:
                pass
        return True

    # Luna is now an economical advisory second brain. Only high-quality setups
    # request it; a HOLD reduces risk but is not a kill switch.
    llm_review: dict[str, Any] = {
        "decision": "not_requested", "approved": False, "confidence": 0.0,
        "decision_source": "not_requested", "reasoning": "V9 local ensemble sufficient",
    }
    llm_cache: dict[str, Any] = {"api_called": False}
    if v9_exec.should_call_luna(pre) and spartan.symbol_kind(symbol) in {"gold", "oil", "crypto"}:
        review_snapshot = dict(spartan_snapshot)
        review_snapshot["review_mode"] = "v9_advisory"
        review_snapshot["capital_growth"] = growth_state
        review_snapshot["strategy_context"] = {
            "strategy_id": int(strategy.strategy_id),
            "family": str(getattr(strategy, "family", "unknown")),
            "regime": str(regime),
            "execution_tier": "v9_execution_first",
        }
        review_snapshot["ml"] = {
            "approved": bool(super_decision.approved),
            "probability": safe_float(super_decision.probability, 0.50),
            "model_probability_raw": safe_float((super_decision.market or {}).get("model_probability_raw"), 0.50),
            "threshold": safe_float(super_decision.threshold, 0.50),
            "probability_active": bool(super_decision.probability_active),
            "bayes_loss_probability": safe_float(super_decision.bayes_loss_probability),
            "expected_r": safe_float((super_decision.market or {}).get("expected_r")),
            "reward_to_risk": safe_float((super_decision.market or {}).get("reward_to_risk")),
            "break_even_probability": safe_float((super_decision.market or {}).get("break_even_probability")),
            "reason": str(super_decision.reason),
        }
        review_snapshot["learning_context"] = {
            "approved": bool(adaptive_allowed),
            "reason": str(adaptive_reason),
            "confidence": safe_float((adaptive_gate.get("snapshot") or {}).get("confidence"), 0.50),
            "risk_multiplier_advisory": safe_float(adaptive_gate.get("risk_multiplier"), 1.0),
        }
        review_snapshot["micro_hunter"] = details.get("micro_hunter", {})
        review_snapshot["portfolio_context"] = {
            "daily_loss_pct": safe_float(guard.get("daily_loss_pct")),
            "drawdown_pct": safe_float(guard.get("drawdown_pct")),
            "open_machine_positions": int(len(machine_positions)),
            "symbol_open_positions": int(sum(1 for p in machine_positions if str(p.symbol) == symbol)),
            "daily_trade_count": int(spartan_daily_trade_count(connection)),
        }
        review_snapshot["opportunity_score"] = safe_float(pre.get("pre_score"))
        review_snapshot["v8_local_alpha_score"] = safe_float(pre.get("pre_score"))
        review_snapshot["gpt_calibration_context"] = gpt_memory.decision_memory_snapshot(
            connection, symbol, str(regime), int(signal)
        )
        try:
            bar_time = row.get("time") if hasattr(row, "get") else "unknown"
            llm_review, llm_cache = gpt_memory.review_with_cache(
                connection, review_snapshot,
                strategy_id=int(strategy.strategy_id),
                family=str(getattr(strategy, "family", "unknown")),
                regime=str(regime), side=int(signal), bar_time=bar_time,
                reviewer=spartan_llm_review,
            )
        except Exception as luna_error:
            llm_review = {
                "decision": "hold", "approved": False, "confidence": 0.0,
                "decision_source": "runtime_error",
                "reasoning": f"Luna advisory unavailable: {type(luna_error).__name__}",
            }
            llm_cache = {"api_called": False, "error": str(luna_error)[:200]}
    details["spartan_llm"] = llm_review
    details["spartan_gpt_memory"] = llm_cache

    decision = v9_exec.final_decision(
        pre=pre, super_decision=super_decision,
        spartan_snapshot=spartan_snapshot, adaptive_allowed=adaptive_allowed,
        edge_allowed=edge_allowed, luna_review=llm_review,
    )
    details["v9_execution"] = decision
    v9_exec.record_decision(
        connection, symbol=symbol, strategy_id=int(strategy.strategy_id), side=int(signal),
        playbook=playbook, decision=decision, context={
            "adaptive_reason": adaptive_reason,
            "edge_state": edge_state,
            "luna": llm_review,
            "micro_hunter": details.get("micro_hunter", {}),
            "precision_memory": decision.get("precision_memory", {}),
            "recovery_mode": bool(decision.get("recovery_mode")),
        },
    )

    if not bool(decision.get("execute")):
        log_decision(
            connection, "demo", symbol, regime, strategy.strategy_id, signal,
            f"V9 ensemble hold: {decision.get('reason')}", details,
        )
        scalp_diag.record(symbol, "v9_ensemble_hold", str(decision.get("reason") or "score"))
        print(
            f"{symbol}: V9 ENSEMBLE HOLD | {playbook} "
            f"score={safe_float(decision.get('final_score')):.1f} "
            f"ER={safe_float(decision.get('expected_r')):+.2f}R"
        )
        if bool(getattr(settings, "V92_REJECTED_TRADE_LEARNING_ENABLED", False)):
            try:
                base_stop = safe_float((details.get("micro_hunter") or {}).get("stop_atr"), safe_float(strategy.params.get("stop_atr", 0.8)))
                base_take = safe_float((details.get("micro_hunter") or {}).get("take_atr"), safe_float(strategy.params.get("take_atr", 1.2)))
                plan = v92.dynamic_exit_plan(
                    base_stop_atr=base_stop, base_take_atr=base_take, atr_value=atr_value,
                    frame=frame, grade=str(decision.get("grade") or "B"), playbook=playbook,
                    memory=(v92_overlay.get("context_memory") or {}), overlay=v92_overlay,
                )
                virtual_entry = safe_float(tick.ask if signal == 1 else tick.bid)
                rd = safe_float(plan.get("stop_atr"), base_stop) * atr_value
                td = safe_float(plan.get("take_atr"), base_take) * atr_value
                v92.record_virtual_trial(
                    connection, trial_type="rejected", symbol=symbol, playbook=playbook,
                    side=int(signal), regime=str(regime), session=str(v92_overlay.get("session") or "unknown"),
                    score=safe_float(decision.get("final_score")), entry=virtual_entry,
                    stop=virtual_entry-rd if signal==1 else virtual_entry+rd,
                    take=virtual_entry+td if signal==1 else virtual_entry-td,
                    reason=str(decision.get("reason") or "ensemble_hold"),
                    context={"decision": decision, "overlay": v92_overlay},
                )
            except Exception:
                pass
        return True

    # Risk owns HOW MUCH. Advisory model disagreements reduce size instead of
    # serially deleting the trade.
    portfolio_mult, portfolio_state = portfolio_soft_risk_multiplier(guard)
    risk_multiplier = safe_float(decision.get("risk_multiplier"), 0.35) * portfolio_mult
    risk_multiplier *= clamp(safe_float(edge_risk_mult, 1.0), 0.45, 1.0)

    hunter_risk_mult, hunter_memory = early_hunter_risk_mult, early_hunter_memory
    risk_multiplier *= clamp(safe_float(hunter_risk_mult, 1.0), 0.45, 1.0)
    details.setdefault("micro_hunter", {})["playbook_memory"] = hunter_memory

    correlation_mult, correlation_state = scalp_lab.portfolio_correlation_risk(
        symbol, signal, frame, machine_positions, fetch_raw_bars
    )
    risk_multiplier *= clamp(safe_float(correlation_mult, 1.0), 0.45, 1.0)
    details["correlation_risk"] = correlation_state

    execution_mult, execution_state = scalp_lab.execution_quality_risk_multiplier(
        connection, symbol
    )
    risk_multiplier *= clamp(safe_float(execution_mult, 1.0), 0.45, 1.0)
    details["execution_quality_risk"] = execution_state

    growth_mult = 1.0
    if bool(getattr(settings, "ENABLE_CAPITAL_GROWTH_CONTROLLER", True)):
        growth_mult = clamp(safe_float(growth_state.get("risk_multiplier"), 1.0), 0.0, 1.0)
        risk_multiplier *= growth_mult
    risk_multiplier = clamp(
        risk_multiplier,
        safe_float(getattr(settings, "V9_MIN_RISK_MULTIPLIER", 0.20), 0.20),
        1.0,
    )
    details["portfolio_risk_state"] = {
        "state": portfolio_state, "multiplier": portfolio_mult,
        "daily_loss_pct": safe_float(guard.get("daily_loss_pct")),
        "drawdown_pct": safe_float(guard.get("drawdown_pct")),
    }

    # Fresh-price anti-chase lock remains hard.
    fresh_tick = mt5.symbol_info_tick(symbol)
    if fresh_tick is None:
        scalp_diag.record(symbol, "tick_missing", "V9 fresh MT5 tick unavailable")
        return True
    stale_entry = safe_float(tick.ask if signal == 1 else tick.bid)
    entry = safe_float(fresh_tick.ask if signal == 1 else fresh_tick.bid)
    max_drift = safe_float(getattr(settings, "V9_MAX_FRESH_PRICE_DRIFT_ATR", 0.35), 0.35) * atr_value
    if abs(entry - stale_entry) > max_drift:
        log_decision(
            connection, "demo", symbol, regime, strategy.strategy_id, signal,
            "V9 fresh-price drift hard hold",
            {**details, "old_entry": stale_entry, "fresh_entry": entry, "atr": atr_value, "max_drift": max_drift},
        )
        scalp_diag.record(symbol, "price_drift", f"drift={abs(entry-stale_entry):.6g}")
        return True

    hunter_ctx = details.get("micro_hunter") if isinstance(details.get("micro_hunter"), dict) else {}
    base_stop_atr = clamp(safe_float(hunter_ctx.get("stop_atr"), strategy.params.get("stop_atr", 0.8)), 0.35, 1.50)
    base_take_atr = clamp(safe_float(hunter_ctx.get("take_atr"), strategy.params.get("take_atr", 1.2)), 0.50, 2.80)
    exit_plan: dict[str, Any] = {}
    if bool(getattr(settings, "V92_DYNAMIC_EXIT_ENABLED", False)):
        exit_plan = v92.dynamic_exit_plan(
            base_stop_atr=base_stop_atr, base_take_atr=base_take_atr, atr_value=atr_value,
            frame=frame, grade=str(decision.get("grade") or "B"), playbook=playbook,
            memory=(v92_overlay.get("context_memory") or {}), overlay=v92_overlay,
        )
        stop_atr = clamp(safe_float(exit_plan.get("stop_atr"), base_stop_atr), 0.35, 1.50)
        take_atr = clamp(safe_float(exit_plan.get("take_atr"), base_take_atr), 0.45, 3.20)
    else:
        stop_atr, take_atr = base_stop_atr, base_take_atr
    stop_distance = stop_atr * atr_value
    take_distance = take_atr * atr_value
    stop = entry - stop_distance if signal == 1 else entry + stop_distance
    take = entry + take_distance if signal == 1 else entry - take_distance

    base_risk = safe_float(getattr(settings, "V9_BASE_RISK_PER_TRADE", 0.0010), 0.0010)
    ceiling = safe_float(getattr(settings, "V9_MAX_MIN_LOT_RISK_PCT", 0.0035), 0.0035)
    risk_fraction = min(ceiling, base_risk * risk_multiplier)
    sizing = volume_plan(
        symbol, signal, entry, stop, safe_float(account.equity),
        allow_minimum_bridge=True,
        risk_fraction=risk_fraction,
        hard_ceiling_fraction=ceiling,
    )
    volume = safe_float(sizing.get("volume"))
    if volume <= 0:
        reason = str(sizing.get("reason") or "V9 broker sizing rejected")
        log_decision(
            connection, "demo", symbol, regime, strategy.strategy_id, signal,
            reason, {**details, "entry": entry, "stop": stop, "take": take, "sizing": sizing},
        )
        scalp_diag.record(symbol, "v9_sizing", reason)
        print(f"{symbol}: V9 SIZING HOLD | {reason}")
        return True

    entry_context = learning_entry_context(
        row, atr_value, spread, dom, details, "v9_execution_first", risk_multiplier
    )
    entry_context["micro_hunter"] = details.get("micro_hunter", {})
    entry_context["learning_snapshot"] = adaptive_gate.get("snapshot", {})
    entry_context["target_risk_fraction"] = risk_fraction
    entry_context["actual_risk_pct"] = safe_float(sizing.get("actual_risk_pct"))
    entry_context["superlearner"] = super_decision.as_dict()
    entry_context["superlearner"]["features"] = super_decision.features
    entry_context["spartan_pro"] = spartan_snapshot
    entry_context["spartan_llm"] = llm_review
    entry_context["v9_execution"] = decision
    entry_context["v9_2"] = {
        "playbook": playbook,
        "session": str(v92_overlay.get("session") or "unknown"),
        "regime": str(regime),
        "overlay": v92_overlay,
        "exit_plan": exit_plan,
    }
    entry_context["portfolio_risk_state"] = details.get("portfolio_risk_state", {})

    order_started = time.perf_counter()
    try:
        result = send_demo_order(
            connection, symbol, strategy, signal, entry, stop, take, volume,
            execution_tier="v9_execution_first",
        )
    except Exception as order_error:
        scalp_diag.record(symbol, "v9_broker_reject", str(order_error))
        print(f"{symbol}: V9 BROKER REJECT | {order_error}")
        return True

    execution_latency_ms = (time.perf_counter() - order_started) * 1000.0
    if bool(getattr(settings, "LAB_EXECUTION_QUALITY_ENABLED", True)):
        scalp_lab.record_execution_quality(
            connection, symbol=symbol, strategy_id=int(strategy.strategy_id), side=int(signal),
            requested_entry=entry, filled_entry=getattr(result, "price", None),
            spread_price=spread, atr_value=atr_value, latency_ms=execution_latency_ms,
            retcode=getattr(result, "retcode", None), execution_tier="v9_execution_first",
            details={"volume": volume, "stop": stop, "take": take, "v9": decision},
        )
    time.sleep(0.20)
    position_ticket = latest_machine_position_ticket(symbol)
    register_demo_position(
        connection, position_ticket, strategy, regime, signal, volume,
        entry, stop, take, safe_float(sizing.get("actual_risk_cash")), result,
        "v9_execution_first", entry_context,
        safe_float((adaptive_gate.get("snapshot") or {}).get("confidence"), 0.50),
        risk_multiplier,
        super_probability=super_decision.probability,
        super_decision=super_decision.as_dict(),
        sl_multiplier=1.0, tp_multiplier=1.0,
    )
    if not bool(getattr(hunter_setup, "v10_synthetic", False)):
        micro_hunter.mark_executed(connection, hunter_setup)
    machine_positions[:] = mt5_machine_positions()
    log_decision(
        connection, "demo", symbol, regime, strategy.strategy_id, signal,
        "V9 demo order sent",
        {**details, "entry": entry, "stop": stop, "take": take, "volume": volume, "sizing": sizing, "result": as_dict(result)},
    )
    scalp_diag.record(symbol, "v9_order_sent", f"{decision.get('grade')} score={decision.get('final_score')}")
    print(
        f"V9 DEMO ORDER {symbol} {'BUY' if signal == 1 else 'SELL'} | {playbook} | "
        f"grade={decision.get('grade')} score={safe_float(decision.get('final_score')):.1f} "
        f"risk_x={risk_multiplier:.2f} vol={volume} | "
        f"ER={safe_float(decision.get('expected_r')):+.2f}R "
        f"Luna={decision.get('luna_decision')} | retcode={getattr(result, 'retcode', None)}"
    )
    return True

def choose_live_opportunity(
    connection: sqlite3.Connection,
    symbol: str,
    frame: pd.DataFrame,
    dom: float | None,
    hunter_setup: micro_hunter.MicroSetup | None = None,
) -> tuple[int, StrategyDefinition | None, str, dict[str, Any]]:
    regime = detect_regime(frame)
    candidates, execution_tier = execution_candidate_set(connection, symbol, regime)

    # V8 institutional alpha: a high-quality live micro setup no longer dies
    # merely because the research library has zero matching approved scalp
    # carriers. First preference is still an evidence-qualified approved/trial
    # scalp carrier. If none matches, a persistent DEMO-only native alpha
    # carrier is used so the setup can reach *all* downstream hard gates.
    if hunter_setup is not None:
        carrier = None
        carrier_score = 0.0
        # V8.4 execution-first architecture: live micro playbooks learn on their own
        # persistent DEMO-only carrier instead of inheriting stale approval/trial
        # strategy memory. Research candidates remain useful as background evidence,
        # but they no longer choke the live alpha path.
        native_primary = bool(getattr(settings, "V8_NATIVE_ALPHA_PRIMARY_FOR_HUNTER", True))
        if native_primary and bool(getattr(settings, "V8_NATIVE_ALPHA_ENABLED", True)):
            carrier = v8_native_alpha_strategy(connection, hunter_setup)
            carrier_score = float(hunter_setup.score)
            execution_tier = "v8_native_alpha"
        elif candidates:
            carrier, carrier_score = micro_hunter.select_carrier(candidates, hunter_setup)
        if carrier is None and bool(getattr(settings, "V8_NATIVE_ALPHA_ENABLED", True)):
            carrier = v8_native_alpha_strategy(connection, hunter_setup)
            carrier_score = float(hunter_setup.score)
            execution_tier = "v8_native_alpha"
        if carrier is not None:
            memory = micro_hunter.memory_snapshot(
                connection, symbol, hunter_setup.playbook, hunter_setup.side
            )
            details = {
                "execution_tier": execution_tier,
                "candidate_count": len(candidates),
                "trial_signal_mode": "v8_institutional_alpha" if execution_tier == "v8_native_alpha" else "v7_micro_hunter",
                "selected_score": carrier_score,
                "selected_strategy_id": int(carrier.strategy_id),
                "buy_votes": 1 if int(hunter_setup.side) == 1 else 0,
                "sell_votes": 1 if int(hunter_setup.side) == -1 else 0,
                "buy_weight": float(hunter_setup.score) if int(hunter_setup.side) == 1 else 0.0,
                "sell_weight": float(hunter_setup.score) if int(hunter_setup.side) == -1 else 0.0,
                "micro_hunter": {
                    **hunter_setup.as_dict(),
                    "playbook_memory": memory,
                    "exit_profile": hunter_setup.context.get("exit_profile", {}),
                    "native_alpha": execution_tier == "v8_native_alpha",
                },
            }
            if not bool(getattr(hunter_setup, "v10_synthetic", False)):
                micro_hunter.bind_execution(
                    connection, hunter_setup, int(carrier.strategy_id), execution_tier
                )
            else:
                details["trial_signal_mode"] = "v10_superhuman_expert"
                details["micro_hunter"]["v10_superhuman"] = True
            return int(hunter_setup.side), carrier, regime, details

    if not candidates:
        return 0, None, regime, {
            "reason": "no_approved_or_trial_candidates",
            "execution_tier": "none",
        }

    needed = required_ema_lengths(definition for definition, _ in candidates)
    missing = [length for length in needed if f"ema_{length}" not in frame.columns]
    if missing:
        frame = add_features(frame[[
            c for c in frame.columns if not c.startswith("ema_")
        ]], needed)

    if execution_tier == "shadow_approved":
        signal, strategy, details = ensemble_signal(
            frame, len(frame) - 2, candidates, dom, connection=connection, regime=regime
        )
    else:
        signal, strategy, details = trial_signal(
            frame, len(frame) - 2, candidates, dom, connection=connection, regime=regime
        )
    details["execution_tier"] = execution_tier
    details["candidate_count"] = len(candidates)
    return signal, strategy, regime, details

def paper_cycle() -> None:
    connect_mt5(show_account=False)
    try:
        account = mt5.account_info()
        if account is None:
            raise RuntimeError("Cannot read account")
        with db_connect() as connection:
            for symbol in settings.SYMBOLS:
                raw = fetch_raw_bars(symbol, settings.LIVE_BARS)
                frame = add_features(raw, {20, 50})
                dom = order_book_imbalance(symbol)
                signal, strategy, regime, details = choose_live_opportunity(
                    connection, symbol, frame, dom
                )
                if signal == 0 or strategy is None:
                    log_decision(
                        connection, "paper", symbol, regime, None, 0,
                        "No ensemble entry", details,
                    )
                    continue
                tick = mt5.symbol_info_tick(symbol)
                if tick is None:
                    continue
                row = frame.iloc[-2]
                atr_value = safe_float(row["atr_14"])
                if bool(getattr(settings, "ENABLE_SPARTAN_PRO", True)):
                    paper_micro = order_book_microstructure(symbol)
                    paper_spartan = spartan.build_gate_snapshot(
                        symbol, frame, tick, signal, paper_micro, daily_trade_count=0, fetch_htf=True
                    )
                    details = {**details, "spartan_pro": paper_spartan}
                    if not bool(paper_spartan.get("approved", True)):
                        log_decision(
                            connection, "paper", symbol, regime, strategy.strategy_id, signal,
                            f"Spartan-Pro paper veto: {paper_spartan.get('reason')}", details,
                        )
                        continue
                entry = safe_float(tick.ask if signal == 1 else tick.bid)
                stop_distance = float(strategy.params["stop_atr"]) * atr_value
                take_distance = float(strategy.params["take_atr"]) * atr_value
                stop = entry - stop_distance if signal == 1 else entry + stop_distance
                take = entry + take_distance if signal == 1 else entry - take_distance
                volume = normalized_volume(
                    symbol, signal, entry, stop, safe_float(account.equity)
                )
                log_decision(
                    connection,
                    "paper",
                    symbol,
                    regime,
                    strategy.strategy_id,
                    signal,
                    "Paper signal only; no MT5 order sent",
                    {**details, "entry": entry, "stop": stop, "take": take, "volume": volume},
                )
                print(
                    f"PAPER {symbol} {'BUY' if signal == 1 else 'SELL'} | "
                    f"strategy={strategy.strategy_id} {strategy.family} | "
                    f"regime={regime} volume={volume} DOM={dom}"
                )
            connection.commit()
    finally:
        mt5.shutdown()


def rebuild_execution_memories_v66(connection: sqlite3.Connection) -> None:
    """One-time repair: rebuild execution memories from trustworthy sources.

    V6.5's broad research shadow pool included backtest-rejected strategies.
    Those outcomes are valuable for research but should not share the same
    collective/session memory used by broker DEMO approval.
    """
    if not bool(getattr(settings, "V66_REBUILD_EXECUTION_MEMORY_ONCE", True)):
        return
    state_key = "v66_execution_memory_rebuilt"
    if str(state_get(connection, state_key) or "") == "1":
        return

    connection.execute("DELETE FROM collective_market_memory")
    connection.execute("DELETE FROM session_family_memory")
    demo_count = 0
    shadow_count = 0

    demo_rows = connection.execute(
        """
        SELECT d.symbol, d.regime, d.side, d.reward_r, d.opened_at, s.family
        FROM demo_positions d
        LEFT JOIN strategies s ON s.id=d.strategy_id
        WHERE d.status='closed' AND d.reward_r IS NOT NULL
        ORDER BY d.id
        """
    ).fetchall()
    for item in demo_rows:
        family = str(item["family"] or "unknown")
        if family == "unknown":
            continue
        update_collective_memory(
            connection, str(item["symbol"]), family, str(item["regime"] or "unknown"),
            int(item["side"] or 0), safe_float(item["reward_r"]), 1.0,
        )
        update_session_family_memory(
            connection, str(item["symbol"]), family, str(item["regime"] or "unknown"),
            market_session_tag(str(item["symbol"]), item["opened_at"]),
            int(item["side"] or 0), safe_float(item["reward_r"]), 1.0,
        )
        demo_count += 1

    shadow_rows = connection.execute(
        """
        SELECT p.strategy_id, p.symbol, p.family, p.regime, p.side, p.reward_r,
               p.opened_at, s.status AS strategy_status
        FROM shadow_positions p
        LEFT JOIN strategies s ON s.id=p.strategy_id
        WHERE p.status='closed' AND p.reward_r IS NOT NULL
        ORDER BY p.id
        """
    ).fetchall()
    setting_by_status = {
        "backtest_rejected": "SHADOW_MEMORY_WEIGHT_BACKTEST_REJECTED",
        "shadow_rejected": "SHADOW_MEMORY_WEIGHT_SHADOW_REJECTED",
        "historical_validated": "SHADOW_MEMORY_WEIGHT_HISTORICAL_VALIDATED",
        "shadow_approved": "SHADOW_MEMORY_WEIGHT_SHADOW_APPROVED",
    }
    for item in shadow_rows:
        setting_name = setting_by_status.get(str(item["strategy_status"] or "unknown"))
        weight = clamp(safe_float(getattr(settings, setting_name, 0.0), 0.0), 0.0, 1.0) if setting_name else 0.0
        if weight <= 0.0:
            continue
        update_collective_memory(
            connection, str(item["symbol"]), str(item["family"]), str(item["regime"] or "unknown"),
            int(item["side"] or 0), safe_float(item["reward_r"]), weight,
        )
        update_session_family_memory(
            connection, str(item["symbol"]), str(item["family"]), str(item["regime"] or "unknown"),
            market_session_tag(str(item["symbol"]), item["opened_at"]),
            int(item["side"] or 0), safe_float(item["reward_r"]), weight,
        )
        shadow_count += 1

    state_set(connection, state_key, "1")
    connection.commit()
    print(
        f"V6.6 EXECUTION MEMORY REBUILT | demo={demo_count} | "
        f"eligible_shadow={shadow_count} | rejected research excluded"
    )


def demo_cycle() -> None:
    if bool(getattr(settings, "V11_ENABLED", False)):
        scalp_v11.cycle(sys.modules[__name__])
        return
    account = connect_mt5(show_account=False)
    try:
        verify_demo_account()
        with db_connect() as connection:
            rebuild_execution_memories_v66(connection)
            update_open_demo_excursions(connection)
            reconcile_demo_positions(connection)
            if bool(getattr(settings, "V92_ADAPTIVE_SCALPING_ENABLED", False)):
                v92.ensure_tables(connection)
                boot_rows = v92.bootstrap_existing_v9(connection)
                if boot_rows:
                    print(f"V9.2 CONTEXT MEMORY BOOTSTRAP | broker_v9_rows={boot_rows}")
                for learning_symbol in settings.SYMBOLS:
                    learning_tick = mt5.symbol_info_tick(learning_symbol)
                    if learning_tick is not None:
                        v92.reconcile_virtual_trials(
                            connection, symbol=learning_symbol,
                            bid=safe_float(learning_tick.bid), ask=safe_float(learning_tick.ask),
                        )

            # V6.5: GPT-vetoed candidates remain virtual only, but their real-market
            # counterfactual outcomes are tracked so the system learns whether GPT
            # saved a loss or missed a winner. No broker order is created here.
            if bool(getattr(settings, "SPARTAN_GPT_VETO_SHADOW_LEARNING_ENABLED", True)):
                for learning_symbol in settings.SYMBOLS:
                    if spartan.symbol_kind(learning_symbol) not in {"gold", "oil"}:
                        continue
                    learning_tick = mt5.symbol_info_tick(learning_symbol)
                    if learning_tick is None:
                        continue
                    veto_outcomes = gpt_memory.reconcile_veto_shadows(
                        connection,
                        symbol=learning_symbol,
                        bid=safe_float(learning_tick.bid),
                        ask=safe_float(learning_tick.ask),
                    )
                    for outcome in veto_outcomes:
                        reward_r = safe_float(outcome.get("reward_r"))
                        features = outcome.get("superlearner_features") or {}
                        model_weight = safe_float(
                            getattr(settings, "SPARTAN_GPT_VETO_SHADOW_MODEL_WEIGHT", 0.25), 0.25
                        )
                        if isinstance(features, dict) and features and model_weight > 0:
                            try:
                                update_online_model(
                                    connection, learning_symbol, features, reward_r, model_weight
                                )
                            except Exception as learning_error:
                                print(f"GPT VETO SHADOW MODEL WARNING: {learning_error}")
                        family = str(outcome.get("family") or "unknown")
                        collective_weight = safe_float(
                            getattr(settings, "SPARTAN_GPT_VETO_SHADOW_COLLECTIVE_WEIGHT", 0.20), 0.20
                        )
                        if family and family != "unknown" and collective_weight > 0:
                            try:
                                update_collective_memory(
                                    connection, learning_symbol, family,
                                    str(outcome.get("regime") or "unknown"),
                                    int(outcome.get("side") or 0), reward_r, collective_weight,
                                )
                                context = outcome.get("context") or {}
                                opened_session = str(context.get("market_session") or "unknown")
                                update_session_family_memory(
                                    connection, learning_symbol, family,
                                    str(outcome.get("regime") or "unknown"), opened_session,
                                    int(outcome.get("side") or 0), reward_r, collective_weight,
                                )
                            except Exception as collective_error:
                                print(f"GPT VETO SHADOW COLLECTIVE WARNING: {collective_error}")
                        print(
                            f"GPT VETO SHADOW CLOSED {learning_symbol} | "
                            f"strategy={int(outcome.get('strategy_id') or 0)} "
                            f"result={reward_r:.2f}R label={outcome.get('outcome_label')} | "
                            "learned at reduced shadow weight"
                        )

            spartan_manage_demo_positions(connection)
            account = mt5.account_info() or account
            allowed, guard_reason, guard = risk_guard(connection, account)
            if not allowed:
                print(
                    f"RISK BLOCK (BROKER ENTRIES ONLY): {guard_reason} | {guard} | "
                    "generator/backtester/shadow learning continue in their own engines"
                )
                for symbol in settings.SYMBOLS:
                    log_decision(
                        connection, "demo", symbol, None, None, 0,
                        guard_reason, guard,
                    )
                connection.commit()
                return

            growth_state = capital_growth.update_state(connection, safe_float(account.equity))
            if bool(getattr(settings, "ENABLE_CAPITAL_GROWTH_CONTROLLER", True)) and not bool(growth_state.get("allow_new_entries", True)):
                print(
                    f"CAPITAL GROWTH LOCK: {growth_state.get('phase')} | "
                    f"today={safe_float(growth_state.get('growth_pct'))*100:+.2f}% "
                    f"target={safe_float(growth_state.get('target_pct'))*100:.1f}% "
                    f"stretch={safe_float(growth_state.get('stretch_pct'))*100:.1f}% | "
                    "open positions continue to be managed; new entries are paused"
                )
                for symbol in settings.SYMBOLS:
                    log_decision(
                        connection, "demo", symbol, None, None, 0,
                        "Capital growth stretch lock", growth_state,
                    )
                connection.commit()
                return

            machine_positions = mt5_machine_positions()
            for symbol in settings.SYMBOLS:
                scalp_diag.maybe_report()
                try:
                    symbol_allowed, symbol_guard_reason, symbol_guard = symbol_loss_guard(connection, symbol)
                    if not symbol_allowed:
                        print(
                            f"SYMBOL RISK BLOCK (OTHER SYMBOLS CONTINUE): {symbol_guard_reason} | {symbol_guard}"
                        )
                        log_decision(
                            connection, "demo", symbol, None, None, 0,
                            symbol_guard_reason, symbol_guard,
                        )
                        continue
                    if len(machine_positions) >= settings.MAX_OPEN_POSITIONS:
                        log_decision(
                            connection, "demo", symbol, None, None, 0,
                            "Maximum open positions reached", {"open": len(machine_positions)},
                        )
                        continue
                    symbol_position_count = sum(
                        1 for p in machine_positions if str(p.symbol) == symbol
                    )
                    max_per_symbol = max(
                        1, int(getattr(settings, "MAX_POSITIONS_PER_SYMBOL", 1))
                    )
                    if symbol_position_count >= max_per_symbol:
                        log_decision(
                            connection, "demo", symbol, None, None, 0,
                            "Maximum positions for symbol reached",
                            {
                                "symbol_open": symbol_position_count,
                                "symbol_limit": max_per_symbol,
                            },
                        )
                        continue
                    if not cooldown_ready(connection, symbol):
                        continue

                    raw = fetch_raw_bars(symbol, settings.LIVE_BARS)
                    base_frame = add_features(raw, {20, 50})
                    micro = order_book_microstructure(symbol)
                    dom = micro.get("imbalance") if micro.get("available") else None
                    regime_hint = detect_regime(base_frame)
                    hunter_setup: micro_hunter.MicroSetup | None = None
                    same_closed_bar = False
                    if bool(getattr(settings, "ENTRY_EVALUATE_NEW_CLOSED_BAR_ONLY", True)):
                        closed_bar_time = pd.Timestamp(raw.iloc[-2]["time"]).isoformat()
                        entry_bar_key = f"demo_entry_last_closed_bar:{symbol}"
                        same_closed_bar = state_get(connection, entry_bar_key) == closed_bar_time
                        if same_closed_bar:
                            # V7 high-attention mode: normal strategy/ML evaluation still
                            # runs once per closed M1 bar, but an already ARMED 2-3 candle
                            # micro setup may trigger from live ticks during the next minute.
                            live_tick = mt5.symbol_info_tick(symbol)
                            if live_tick is not None:
                                hunter_setup = micro_hunter.intrabar_trigger(
                                    connection, symbol, base_frame, live_tick
                                )
                            if hunter_setup is None:
                                hunter_setup = micro_hunter.spread_wait_resume(
                                    connection, symbol, base_frame, live_tick
                                )
                            if hunter_setup is None:
                                continue
                        else:
                            state_set(connection, entry_bar_key, closed_bar_time)
                            hunter_setup = micro_hunter.observe_closed_bar(
                                connection, symbol, base_frame, regime_hint, dom
                            )
                    else:
                        hunter_setup = micro_hunter.observe_closed_bar(
                            connection, symbol, base_frame, regime_hint, dom
                        )
                    # V10.1 PARALLEL ROUTER:
                    # On every NEW closed M1 bar, the legacy Micro Hunter and the V10
                    # 8-expert ensemble are evaluated in parallel. V10 is no longer
                    # fallback-only, so the legacy hunter cannot monopolise all entries.
                    v10_expert_setup = None
                    if bool(getattr(settings, "V10_SUPERHUMAN_SCALPER_ENABLED", False)) and not same_closed_bar:
                        try:
                            v10_expert_setup = v10.best_setup(
                                connection, symbol=symbol, frame=base_frame, dom=dom
                            )
                        except Exception as v10_error:
                            print(f"V10 EXPERT WARNING {symbol}: {type(v10_error).__name__}: {v10_error}")
                            v10_expert_setup = None

                        chosen_setup, chosen_source, router_reason = v10.choose_parallel_setup(
                            hunter_setup, v10_expert_setup
                        )
                        try:
                            v10.record_router_decision(
                                connection,
                                symbol=symbol,
                                hunter_setup=hunter_setup,
                                expert_setup=v10_expert_setup,
                                chosen_setup=chosen_setup,
                                chosen_source=chosen_source,
                                reason=router_reason,
                            )
                        except Exception as router_log_error:
                            print(
                                f"V10 ROUTER LOG WARNING {symbol}: "
                                f"{type(router_log_error).__name__}: {router_log_error}"
                            )
                        if v10_expert_setup is not None:
                            print(
                                f"{symbol}: V10 EXPERT | {v10_expert_setup.playbook} "
                                f"{'BUY' if v10_expert_setup.side == 1 else 'SELL'} "
                                f"score={v10_expert_setup.score:.1f} | "
                                f"router={chosen_source}:{router_reason}"
                            )
                        hunter_setup = chosen_setup

                    signal, strategy, regime, details = choose_live_opportunity(
                        connection, symbol, base_frame, dom, hunter_setup=hunter_setup
                    )
                    if hunter_setup is not None:
                        hunter_version = "V10" if bool(getattr(hunter_setup, "v10_synthetic", False)) else ("V9" if bool(getattr(settings, "V9_EXECUTION_FIRST_ENABLED", False)) else "V8")
                        print(
                            f"{symbol}: {hunter_version} MICRO TRIGGER | {hunter_setup.playbook} "
                            f"{'BUY' if hunter_setup.side == 1 else 'SELL'} "
                            f"score={hunter_setup.score:.1f} phase={hunter_setup.phase}"
                        )
                        scalp_diag.record(symbol, "micro_trigger", f"{hunter_setup.playbook} {hunter_setup.score:.1f}")
                    execution_tier = str(details.get("execution_tier") or "none")
                    if signal == 0 or strategy is None:
                        log_decision(
                            connection, "demo", symbol, regime, None, 0,
                            "No approved/trial entry", details,
                        )
                        scalp_diag.record(symbol, "no_signal", "No approved/trial entry")
                        continue

                    # Trial entries are deliberately narrower than fully approved
                    # ensemble execution: at most one per symbol and three total.
                    if execution_tier == "demo_trial":
                        trial_total_limit = max(
                            1, int(settings.DEMO_TRIAL_MAX_OPEN_POSITIONS)
                        )
                        trial_symbol_limit = max(
                            1, int(settings.DEMO_TRIAL_MAX_POSITIONS_PER_SYMBOL)
                        )
                        if len(machine_positions) >= trial_total_limit:
                            log_decision(
                                connection, "demo", symbol, regime,
                                strategy.strategy_id, signal,
                                "DEMO trial total-position limit reached",
                                {**details, "open": len(machine_positions)},
                            )
                            continue
                        if symbol_position_count >= trial_symbol_limit:
                            log_decision(
                                connection, "demo", symbol, regime,
                                strategy.strategy_id, signal,
                                "DEMO trial symbol-position limit reached",
                                {**details, "symbol_open": symbol_position_count},
                            )
                            continue

                    if execution_tier == "v8_native_alpha" and not (bool(getattr(settings, "V9_EXECUTION_FIRST_ENABLED", False)) and hunter_setup is not None):
                        native_total = v8_native_daily_trade_count(connection)
                        native_symbol = v8_native_daily_trade_count(connection, symbol)
                        native_total_cap = max(1, int(getattr(settings, "V8_NATIVE_ALPHA_MAX_TRADES_PER_DAY", 6)))
                        native_symbol_cap = max(1, int(getattr(settings, "V8_NATIVE_ALPHA_MAX_TRADES_PER_SYMBOL_PER_DAY", 3)))
                        if native_total >= native_total_cap or native_symbol >= native_symbol_cap:
                            log_decision(
                                connection, "demo", symbol, regime, strategy.strategy_id, signal,
                                "V8 native alpha daily probe cap reached",
                                {**details, "native_total": native_total, "native_total_cap": native_total_cap,
                                 "native_symbol": native_symbol, "native_symbol_cap": native_symbol_cap},
                            )
                            scalp_diag.record(symbol, "alpha_cap", f"{native_symbol}/{native_symbol_cap} symbol; {native_total}/{native_total_cap} total")
                            continue

                    if execution_tier == "v8_native_alpha":
                        candidates = [(strategy, safe_float(details.get("selected_score"), 0.0))]
                        confirmed_tier = execution_tier
                    else:
                        candidates, confirmed_tier = execution_candidate_set(
                            connection, symbol, regime
                        )
                    if confirmed_tier != execution_tier or not candidates:
                        continue
                    frame = add_features(
                        raw, required_ema_lengths(d for d, _ in candidates)
                    )
                    hunter_mode = str(details.get("trial_signal_mode") or "")
                    # V10.1.1 HANDOFF REPAIR:
                    # A V10 expert selected by the parallel router is already a real
                    # live setup. Treat it exactly like the existing hunter trigger
                    # for downstream signal preservation; do NOT send it back through
                    # legacy trial_signal(), which can erase the selected V10 side.
                    hunter_active = hunter_mode in {
                        "v7_micro_hunter",
                        "v8_institutional_alpha",
                        "v10_superhuman_expert",
                    }
                    if hunter_active:
                        # Preserve the selected live alpha trigger. All hard cost/risk,
                        # learning, SuperLearner, Edge Recovery and selective Luna
                        # logic still run after this point.
                        signal_details = {
                            "v8_institutional_alpha_trigger": hunter_mode == "v8_institutional_alpha",
                            "v7_micro_hunter_trigger": hunter_mode in {"v7_micro_hunter", "v8_institutional_alpha"},
                            "v10_superhuman_trigger": hunter_mode == "v10_superhuman_expert",
                        }
                    elif execution_tier == "shadow_approved":
                        signal, strategy, signal_details = ensemble_signal(
                            frame, len(frame) - 2, candidates, dom, connection=connection, regime=regime
                        )
                    else:
                        signal, strategy, signal_details = trial_signal(
                            frame, len(frame) - 2, candidates, dom, connection=connection, regime=regime
                        )
                    details = {
                        **details, **signal_details, "execution_tier": execution_tier,
                        "capital_growth": growth_state,
                    }
                    if signal == 0 or strategy is None:
                        scalp_diag.record(symbol, "no_signal", "candidate set produced no live signal")
                        continue
                    scalp_diag.record(symbol, "signal", f"strategy={strategy.strategy_id} {execution_tier}")
                    tick = mt5.symbol_info_tick(symbol)
                    if tick is None:
                        continue
                    row = frame.iloc[-2]
                    atr_value = safe_float(row["atr_14"])
                    spread = safe_float(tick.ask - tick.bid)
                    spread_limit_fraction = safe_float(settings.MAX_SPREAD_ATR_FRACTION, 0.18)
                    if bool(getattr(settings, "V9_EXECUTION_FIRST_ENABLED", False)) and hunter_active and hunter_setup is not None:
                        spread_limit_fraction = v9_exec.spread_limit_atr(
                            safe_float((details.get("micro_hunter") or {}).get("take_atr"), safe_float(hunter_setup.take_atr))
                        )
                    if atr_value <= 0 or spread > atr_value * spread_limit_fraction:
                        if (
                            hunter_active and hunter_setup is not None and atr_value > 0
                            and bool(getattr(settings, "V8_SPREAD_WAIT_ENABLED", True))
                            and not bool(getattr(hunter_setup, "v10_synthetic", False))
                        ):
                            # Legacy hunter setups have persistent ARMED/SPREAD-WAIT
                            # state. V10 expert setups are synthetic closed-bar setups,
                            # so they are re-evaluated fresh instead of being written
                            # into the Micro Hunter wait-state table.
                            micro_hunter.mark_wait_spread(connection, hunter_setup, spread, atr_value)
                            log_decision(
                                connection, "demo", symbol, regime, strategy.strategy_id, signal,
                                "V8 spread wait armed",
                                {**details, "spread": spread, "atr": atr_value,
                                 "spread_atr": spread / max(1e-12, atr_value),
                                 "spread_limit_atr": spread_limit_fraction,
                                 "wait_ttl_s": int(getattr(settings, "V8_SPREAD_WAIT_TTL_SECONDS", 35))},
                            )
                            scalp_diag.record(symbol, "spread_wait", f"spread={spread:.6g} atr={atr_value:.6g}")
                            print(
                                f"{symbol}: {'V9 COST WAIT' if bool(getattr(settings, 'V9_EXECUTION_FIRST_ENABLED', False)) else 'V8 SPREAD WAIT'} | {hunter_setup.playbook} "
                                f"{'BUY' if signal == 1 else 'SELL'} | "
                                f"spread/ATR={spread/max(1e-12, atr_value):.3f}/{spread_limit_fraction:.3f} | "
                                f"wait up to {int(getattr(settings, 'V8_SPREAD_WAIT_TTL_SECONDS', 35))}s, no chase"
                            )
                        else:
                            log_decision(
                                connection,
                                "demo",
                                symbol,
                                regime,
                                strategy.strategy_id,
                                signal,
                                "Spread filter rejected entry",
                                {**details, "spread": spread, "atr": atr_value},
                            )
                            scalp_diag.record(symbol, "spread", f"spread={spread:.6g} atr={atr_value:.6g}")
                        continue

                    # V9 execution-first DEMO path: once a real Micro Hunter trigger
                    # clears the hard cost gate, advisory intelligence adjusts risk
                    # instead of serially vetoing the setup.
                    if (
                        bool(getattr(settings, "V9_EXECUTION_FIRST_ENABLED", False))
                        and hunter_active and hunter_setup is not None
                        and execution_tier == "v8_native_alpha"
                    ):
                        handled_v9 = v9_execute_hunter_candidate(
                            connection,
                            symbol=symbol, strategy=strategy, signal=signal, regime=regime,
                            details=details, frame=frame, row=row, tick=tick,
                            atr_value=atr_value, spread=spread, micro=micro, dom=dom,
                            account=account, machine_positions=machine_positions,
                            hunter_setup=hunter_setup, growth_state=growth_state, guard=guard,
                        )
                        if handled_v9:
                            continue

                    # Spartan-Pro is an additive hard gate for XAUUSD/USOIL.
                    # BTC/other symbols retain the existing pipeline unchanged.
                    spartan_snapshot: dict[str, Any] = {"enabled": False, "approved": True}
                    if bool(getattr(settings, "ENABLE_SPARTAN_PRO", True)):
                        spartan_snapshot = spartan.build_gate_snapshot(
                            symbol, frame, tick, signal, micro,
                            daily_trade_count=spartan_daily_trade_count(connection),
                            fetch_htf=True,
                            playbook=str((details.get("micro_hunter") or {}).get("playbook") or "") if isinstance(details.get("micro_hunter"), dict) else None,
                            micro_score=safe_float((details.get("micro_hunter") or {}).get("score")) if isinstance(details.get("micro_hunter"), dict) else None,
                            execution_tier=execution_tier,
                        )
                        details = {**details, "spartan_pro": spartan_snapshot}
                        if not bool(spartan_snapshot.get("approved", True)):
                            log_decision(
                                connection, "demo", symbol, regime, strategy.strategy_id, signal,
                                f"Spartan-Pro veto: {spartan_snapshot.get('reason')}", details,
                            )
                            print(
                                f"{symbol}: SPARTAN VETO | strategy={strategy.strategy_id} "
                                f"score={spartan_snapshot.get('candidate', {}).get('confluence_score', 0)}/8 "
                                f"conf={safe_float(spartan_snapshot.get('candidate', {}).get('confidence')):.2f} | "
                                f"{spartan_snapshot.get('reason')}"
                            )
                            scalp_diag.record(symbol, "spartan_veto", str(spartan_snapshot.get("reason") or "hard gate"))
                            continue
                    learning_allowed, learning_reason, learning_gate = adaptive_learning_gate(
                        connection, strategy, regime, signal, execution_tier, details
                    )
                    details = {**details, "adaptive_learning": learning_gate}
                    if not learning_allowed:
                        log_decision(
                            connection, "demo", symbol, regime,
                            strategy.strategy_id, signal, learning_reason, details,
                        )
                        print(
                            f"{symbol}: {learning_reason} | strategy={strategy.strategy_id} "
                            f"side={'BUY' if signal == 1 else 'SELL'}"
                        )
                        scalp_diag.record(symbol, "learning_veto", str(learning_reason))
                        continue

                    super_decision = SUPER_LEARNER.predict(
                        connection, strategy, frame, row, regime, signal, details,
                        learning_gate, spread, atr_value, micro,
                    )
                    log_superlearner_decision(
                        connection, symbol, strategy.strategy_id, regime, signal, super_decision
                    )
                    details = {**details, "superlearner": super_decision.as_dict()}
                    if isinstance(details.get("spartan_pro"), dict):
                        votes = details["spartan_pro"].setdefault("agents_vote", {})
                        threshold = safe_float(super_decision.threshold, 0.50)
                        probability = safe_float(super_decision.probability, 0.50)
                        if super_decision.probability_active:
                            votes["ml"] = ("buy" if signal == 1 else "sell") if probability >= threshold else "neutral"
                        details["spartan_pro"]["ml_probability"] = probability
                        details["spartan_pro"]["ml_threshold"] = threshold
                    # V6.7: a narrow *soft* SuperLearner rejection may ask Luna
                    # for one second opinion. Hard market/risk/adaptive gates are
                    # already upstream and are never overridable by GPT.
                    borderline_ok, borderline_meta = scalp_ai.superlearner_borderline_eligible(super_decision)
                    if not super_decision.approved and not borderline_ok:
                        log_decision(
                            connection, "demo", symbol, regime, strategy.strategy_id, signal,
                            f"SuperLearner veto: {super_decision.reason}", details,
                        )
                        scalp_diag.record(symbol, "super_veto", str(super_decision.reason))
                        print(
                            f"{symbol}: SUPERLEARNER VETO | strategy={strategy.strategy_id} "
                            f"Pwin={super_decision.probability:.2f} "
                            f"Ploss={super_decision.bayes_loss_probability:.2f} | "
                            f"{super_decision.reason}"
                        )
                        continue

                    # V6.9 edge recovery: recent broker-demo evidence can
                    # quarantine a repeatedly losing strategy or softly de-risk a
                    # weak family/symbol *before* a paid Luna call. Aggregate
                    # weakness never hard-blocks an improving symbol by itself.
                    edge_allowed, edge_risk_mult, edge_state = scalp_lab.edge_recovery_guard(
                        connection, symbol=symbol, family=str(getattr(strategy, "family", "unknown")),
                        strategy_id=int(strategy.strategy_id), execution_tier=execution_tier,
                    )
                    details["edge_recovery"] = edge_state
                    if not edge_allowed:
                        log_decision(
                            connection, "demo", symbol, regime, strategy.strategy_id, signal,
                            "V6.9 edge-recovery quarantine", details,
                        )
                        scalp_diag.record(symbol, "edge_quarantine", ",".join(edge_state.get("reasons") or []))
                        print(
                            f"{symbol}: EDGE QUARANTINE | strategy={strategy.strategy_id} "
                            f"mean={safe_float((edge_state.get('strategy') or {}).get('mean_r')):+.3f}R "
                            f"PF={safe_float((edge_state.get('strategy') or {}).get('profit_factor')):.2f}"
                        )
                        continue

                    gpt_borderline_rescue = False
                    gpt_disagreement_probe = False
                    budget_quant_probe = False
                    if borderline_ok:
                        slot_ok, rescue_used, rescue_cap = scalp_ai.rescue_trade_slot_available(connection)
                        details["gpt_borderline_meta"] = {
                            **borderline_meta, "daily_used": rescue_used, "daily_cap": rescue_cap
                        }
                        if not slot_ok:
                            log_decision(
                                connection, "demo", symbol, regime, strategy.strategy_id, signal,
                                "SuperLearner borderline veto; GPT rescue daily cap reached", details,
                            )
                            scalp_diag.record(symbol, "super_veto", "GPT rescue daily cap reached")
                            continue
                        print(
                            f"{symbol}: BORDERLINE -> LUNA | strategy={strategy.strategy_id} "
                            f"P={super_decision.probability:.2f}/{super_decision.threshold:.2f} "
                            f"ER={safe_float((super_decision.market or {}).get('expected_r')):+.2f}R"
                        )

                    # Luna is the final context brain for Gold, Oil and BTC. Python
                    # sends one compact but information-dense packet and expects one
                    # tiny structured answer. Fingerprint caching prevents paying for
                    # an unchanged material market state.
                    review_mode = "borderline_review" if borderline_ok else "final_review"
                    review_snapshot: dict[str, Any] | None = None
                    if isinstance(details.get("spartan_pro"), dict) and spartan.symbol_kind(symbol) in {"gold", "oil", "crypto"}:
                        review_snapshot = details["spartan_pro"]

                    if review_snapshot is None:
                        if borderline_ok:
                            log_decision(
                                connection, "demo", symbol, regime, strategy.strategy_id, signal,
                                "Borderline candidate held because Luna context is unavailable", details,
                            )
                            continue
                    else:
                        review_snapshot["review_mode"] = review_mode
                        review_snapshot["capital_growth"] = growth_state
                        review_snapshot["strategy_context"] = {
                            "strategy_id": int(strategy.strategy_id),
                            "family": str(getattr(strategy, "family", "unknown")),
                            "regime": str(regime),
                            "execution_tier": str(execution_tier),
                        }
                        review_snapshot["ml"] = {
                            "approved": bool(super_decision.approved),
                            "probability": safe_float(super_decision.probability, 0.50),
                            "model_probability_raw": safe_float((super_decision.market or {}).get("model_probability_raw"), 0.50),
                            "threshold": safe_float(super_decision.threshold, 0.50),
                            "probability_active": bool(super_decision.probability_active),
                            "bayes_loss_probability": safe_float(super_decision.bayes_loss_probability),
                            "drift_score": safe_float((super_decision.market or {}).get("drift")),
                            "expected_r": safe_float((super_decision.market or {}).get("expected_r")),
                            "reward_to_risk": safe_float((super_decision.market or {}).get("reward_to_risk")),
                            "break_even_probability": safe_float((super_decision.market or {}).get("break_even_probability")),
                            "scalp_expectancy_mode": bool((super_decision.market or {}).get("scalp_expectancy_mode")),
                            "reason": str(super_decision.reason),
                        }
                        review_snapshot["learning_context"] = {
                            "approved": bool(learning_allowed),
                            "reason": str(learning_reason),
                            "confidence": safe_float(learning_gate.get("snapshot", {}).get("confidence"), 0.50),
                            "risk_multiplier_advisory": safe_float(learning_gate.get("risk_multiplier"), 1.0),
                        }
                        if isinstance(details.get("micro_hunter"), dict):
                            review_snapshot["micro_hunter"] = details.get("micro_hunter", {})
                        review_snapshot["portfolio_context"] = {
                            "daily_loss_pct": safe_float(guard.get("daily_loss_pct")),
                            "drawdown_pct": safe_float(guard.get("drawdown_pct")),
                            "open_machine_positions": int(len(machine_positions)),
                            "symbol_open_positions": int(symbol_position_count),
                            "daily_trade_count": int(spartan_daily_trade_count(connection)),
                        }
                        review_snapshot["gpt_calibration_context"] = gpt_memory.decision_memory_snapshot(
                            connection, symbol, str(regime), int(signal)
                        )
                        # Advisory opportunity rank is included for Luna and reports,
                        # but does not bypass any deterministic gate.
                        scores = review_snapshot.get("condition_scores") if isinstance(review_snapshot.get("condition_scores"), dict) else {}
                        session_mem = (super_decision.market or {}).get("session_memory") if isinstance((super_decision.market or {}).get("session_memory"), dict) else {}
                        opportunity = scalp_lab.opportunity_score(
                            super_decision,
                            confluence_ratio=safe_float(scores.get("available_ratio")),
                            spread_atr=(spread / max(1e-9, atr_value)),
                            session_quality=safe_float(session_mem.get("win_probability"), 0.50),
                        )
                        if isinstance(details.get("micro_hunter"), dict):
                            micro_score = safe_float(details["micro_hunter"].get("score"), 0.0)
                            memory_r = safe_float((details["micro_hunter"].get("playbook_memory") or {}).get("reward_ewma"), 0.0)
                            memory_bonus = clamp(memory_r / 0.30, -1.0, 1.0) * 4.0
                            opportunity = round(clamp(0.75 * opportunity + 0.25 * micro_score + memory_bonus, 0.0, 100.0), 2)
                        review_snapshot["opportunity_score"] = opportunity
                        details["opportunity_score"] = opportunity
                        scalp_lab.persist_snapshot(
                            connection, "opportunity",
                            {"mode": review_mode, "score": opportunity, "probability": super_decision.probability,
                             "expected_r": safe_float((super_decision.market or {}).get("expected_r")),
                             "spread_atr": spread / max(1e-9, atr_value)},
                            symbol=symbol, strategy_id=int(strategy.strategy_id), score=opportunity,
                        )
                        spartan.persist_snapshot(review_snapshot)

                        # V8: one cheap local shortlist before any paid Luna call.
                        # This is deliberately after all hard gates and Edge Recovery,
                        # and it cannot approve a trade by itself. It only decides
                        # whether this candidate deserves an API review.
                        local_luna_ok, local_alpha_score, local_alpha_meta = institutional_alpha.local_pre_luna_gate(
                            connection, symbol=symbol, strategy_id=int(strategy.strategy_id), side=int(signal),
                            details=details, super_decision=super_decision,
                            spartan_snapshot=review_snapshot,
                        )
                        details["v8_pre_luna"] = {"score": local_alpha_score, **local_alpha_meta}
                        review_snapshot["v8_local_alpha_score"] = local_alpha_score
                        if not local_luna_ok:
                            log_decision(
                                connection, "demo", symbol, regime, strategy.strategy_id, signal,
                                f"V8 local pre-Luna hold: {local_alpha_meta.get('reason')}", details,
                            )
                            scalp_diag.record(symbol, "pre_luna_hold", str(local_alpha_meta.get("reason") or "local shortlist"))
                            print(
                                f"{symbol}: LOCAL HOLD -> NO LUNA | strategy={strategy.strategy_id} "
                                f"score={local_alpha_score:.1f}/{safe_float(local_alpha_meta.get('threshold')):.1f} "
                                f"ER={safe_float((super_decision.market or {}).get('expected_r')):+.2f}R"
                            )
                            continue

                        # V8.6: the local shortlist cap controls paid Luna usage,
                        # not whether the DEMO execution engine may keep evaluating
                        # a strong Quant setup. Once the local API quota is exhausted,
                        # route directly into the zero-token evidence policy.
                        api_quota_available = bool(local_alpha_meta.get("api_quota_available", True))
                        if not api_quota_available:
                            llm_review = {
                                "decision": "hold",
                                "approved": False,
                                "confidence": 0.0,
                                "decision_source": "local_shortlist_cap",
                                "reasoning": "Paid Luna shortlist quota exhausted; zero-token Quant lane evaluated.",
                            }
                            llm_cache = {"api_called": False, "review_id": None, "fingerprint": None}
                            details["spartan_llm"] = llm_review
                            details["spartan_gpt_memory"] = llm_cache
                            budget_probe_ok, budget_probe_mult, budget_probe_meta = institutional_alpha.budget_exhausted_quant_probe_policy(
                                connection, symbol=symbol, strategy_id=int(strategy.strategy_id), side=int(signal),
                                details=details, super_decision=super_decision,
                                local_alpha_meta={"score": local_alpha_score, **local_alpha_meta},
                                llm_review=llm_review, spartan_snapshot=review_snapshot,
                            )
                            if budget_probe_ok:
                                budget_quant_probe = True
                                details["v8_budget_quant_probe"] = {
                                    "enabled": True, "risk_multiplier": budget_probe_mult, **budget_probe_meta
                                }
                                scalp_diag.record(symbol, "budget_probe", "local_shortlist_cap")
                                print(
                                    f"{symbol}: PAID LUNA QUOTA FULL -> TINY LOCAL DEMO PROBE | "
                                    f"strategy={strategy.strategy_id} local={local_alpha_score:.1f} "
                                    f"micro={safe_float((details.get('micro_hunter') or {}).get('score')):.1f} "
                                    f"ER={safe_float((super_decision.market or {}).get('expected_r')):+.2f}R "
                                    f"risk_x={budget_probe_mult:.2f} | zero API tokens"
                                )
                                llm_source = "local_shortlist_cap"
                                llm_approved = True  # execution permission comes from budget probe, not Luna
                            else:
                                log_decision(
                                    connection, "demo", symbol, regime, strategy.strategy_id, signal,
                                    f"V8 zero-token Quant hold: {budget_probe_meta.get('reason')}",
                                    {**details, "v8_budget_probe": budget_probe_meta},
                                )
                                scalp_diag.record(symbol, "budget_probe_hold", str(budget_probe_meta.get("reason") or "quant policy"))
                                print(
                                    f"{symbol}: PAID LUNA QUOTA FULL -> LOCAL HOLD | "
                                    f"strategy={strategy.strategy_id} "
                                    f"{budget_probe_meta.get('reason')}"
                                )
                                continue
                        else:
                            bar_time = row.get("time") if hasattr(row, "get") else "unknown"
                            llm_review, llm_cache = gpt_memory.review_with_cache(
                                connection, review_snapshot,
                                strategy_id=int(strategy.strategy_id),
                                family=str(getattr(strategy, "family", "unknown")),
                                regime=str(regime), side=int(signal), bar_time=bar_time,
                                reviewer=spartan_llm_review,
                            )
                            details["spartan_llm"] = llm_review
                            details["spartan_gpt_memory"] = llm_cache
                            if bool(llm_cache.get("api_called")):
                                scalp_diag.record(symbol, "gpt_call", review_mode)

                            llm_source = str(llm_review.get("decision_source") or "")
                            llm_approved = bool(llm_review.get("approved", False))
                        if borderline_ok and not budget_quant_probe:
                            rescue_min_conf = safe_float(
                                getattr(settings, "SPARTAN_GPT_BORDERLINE_MIN_CONFIDENCE", 0.82), 0.82
                            )
                            source_ok = llm_source in {"openai", "decision_fingerprint_cache"}
                            llm_approved = bool(
                                llm_approved and source_ok
                                and safe_float(llm_review.get("confidence")) >= rescue_min_conf
                            )
                            if llm_approved:
                                gpt_borderline_rescue = True
                                details["gpt_borderline_rescue"] = True
                                scalp_diag.record(symbol, "rescue", f"Luna conf={safe_float(llm_review.get('confidence')):.2f}")

                        if not llm_approved and not budget_quant_probe:
                            # V8.6: API budget exhaustion stops API spending, not the
                            # entire DEMO learning engine. A stricter zero-token Quant
                            # lane can collect tiny broker evidence if the setup remains
                            # safely above break-even after every hard gate.
                            budget_probe_ok, budget_probe_mult, budget_probe_meta = institutional_alpha.budget_exhausted_quant_probe_policy(
                                connection, symbol=symbol, strategy_id=int(strategy.strategy_id), side=int(signal),
                                details=details, super_decision=super_decision,
                                local_alpha_meta={"score": local_alpha_score, **local_alpha_meta},
                                llm_review=llm_review, spartan_snapshot=review_snapshot,
                            )
                            if budget_probe_ok:
                                budget_quant_probe = True
                                details["v8_budget_quant_probe"] = {
                                    "enabled": True, "risk_multiplier": budget_probe_mult, **budget_probe_meta
                                }
                                scalp_diag.record(symbol, "budget_probe", str(budget_probe_meta.get("source") or "budget"))
                                print(
                                    f"{symbol}: LUNA UNAVAILABLE -> TINY LOCAL DEMO PROBE | strategy={strategy.strategy_id} "
                                    f"local={local_alpha_score:.1f} micro={safe_float((details.get('micro_hunter') or {}).get('score')):.1f} "
                                    f"ER={safe_float((super_decision.market or {}).get('expected_r')):+.2f}R "
                                    f"risk_x={budget_probe_mult:.2f} | zero API tokens"
                                )
                                probe_ok, probe_risk_mult, probe_meta = False, 1.0, budget_probe_meta
                            else:
                                # Real/cached Luna HOLDs can still use the existing
                                # V8.2 disagreement-learning lane.
                                probe_ok, probe_risk_mult, probe_meta = institutional_alpha.luna_disagreement_probe_policy(
                                    connection, symbol=symbol, strategy_id=int(strategy.strategy_id), side=int(signal),
                                    details=details, super_decision=super_decision,
                                    local_alpha_meta={"score": local_alpha_score, **local_alpha_meta},
                                    llm_review=llm_review, spartan_snapshot=review_snapshot,
                                )

                            # Ordinary HOLDs are audited counterfactually. A real DEMO
                            # disagreement probe does NOT also create a duplicate shadow
                            # sample, preventing double-counting the same market event.
                            if (
                                bool(getattr(settings, "SPARTAN_GPT_VETO_SHADOW_LEARNING_ENABLED", True))
                                and llm_cache.get("review_id")
                                and not probe_ok
                                and not budget_quant_probe
                            ):
                                if bool(getattr(settings, "SPARTAN_FORCE_FIXED_ATR_EXITS", False)) and spartan.symbol_kind(symbol) in {"gold", "oil"}:
                                    shadow_stop_atr = safe_float(getattr(settings, "SPARTAN_FIXED_SL_ATR", 1.0), 1.0)
                                    shadow_take_atr = safe_float(getattr(settings, "SPARTAN_FIXED_TP_ATR", 1.5), 1.5)
                                else:
                                    shadow_stop_atr = safe_float(strategy.params.get("stop_atr"), 1.0)
                                    shadow_take_atr = safe_float(strategy.params.get("take_atr"), 1.5)
                                shadow_entry = safe_float(tick.ask if signal == 1 else tick.bid)
                                shadow_stop_distance = shadow_stop_atr * atr_value * safe_float(super_decision.sl_multiplier, 1.0)
                                shadow_take_distance = shadow_take_atr * atr_value * safe_float(super_decision.tp_multiplier, 1.0)
                                shadow_stop = shadow_entry - shadow_stop_distance if signal == 1 else shadow_entry + shadow_stop_distance
                                shadow_take = shadow_entry + shadow_take_distance if signal == 1 else shadow_entry - shadow_take_distance
                                gpt_memory.open_veto_shadow(
                                    connection, review_id=int(llm_cache.get("review_id")),
                                    fingerprint=str(llm_cache.get("fingerprint") or ""),
                                    symbol=symbol, strategy_id=int(strategy.strategy_id),
                                    family=str(getattr(strategy, "family", "unknown")),
                                    regime=str(regime), side=int(signal), entry=shadow_entry,
                                    stop=shadow_stop, take=shadow_take, opened_bar_time=bar_time,
                                    max_hold_bars=max(1, int(strategy.params.get("max_hold", 15))),
                                    superlearner_features=dict(super_decision.features or {}),
                                    context={
                                        "market_session": market_session_tag(symbol, utc_now()),
                                        "execution_tier": execution_tier, "review_mode": review_mode,
                                        "llm_review": llm_review,
                                        "ml_probability": safe_float(super_decision.probability, 0.50),
                                        "confluence_score": int(review_snapshot.get("candidate", {}).get("confluence_score") or 0),
                                    },
                                )

                            if budget_quant_probe:
                                # Zero-token budget probe was already approved above.
                                pass
                            elif probe_ok:
                                gpt_disagreement_probe = True
                                details["v8_luna_disagreement_probe"] = {
                                    "enabled": True, "risk_multiplier": probe_risk_mult, **probe_meta
                                }
                                scalp_diag.record(symbol, "luna_probe", f"conf={safe_float(llm_review.get('confidence')):.2f}")
                                print(
                                    f"{symbol}: LUNA HOLD -> TINY DEMO PROBE | strategy={strategy.strategy_id} "
                                    f"local={local_alpha_score:.1f} micro={safe_float((details.get('micro_hunter') or {}).get('score')):.1f} "
                                    f"ER={safe_float((super_decision.market or {}).get('expected_r')):+.2f}R "
                                    f"risk_x={probe_risk_mult:.2f}"
                                )
                            else:
                                log_decision(
                                    connection, "demo", symbol, regime, strategy.strategy_id, signal,
                                    f"Spartan LLM final veto: {llm_review.get('reasoning')}", {**details, "v8_luna_probe": probe_meta},
                                )
                                scalp_diag.record(symbol, "gpt_veto", str(llm_review.get("reasoning") or llm_source))
                                cache_tag = "CACHE" if bool(llm_review.get("cache_hit")) else ("API" if llm_source == "openai" else llm_source.upper())
                                print(
                                    f"{symbol}: LUNA HOLD [{cache_tag}] | "
                                    f"conf={safe_float(llm_review.get('confidence')):.2f} | "
                                    f"{llm_review.get('reasoning')}"
                                )
                                continue

                        if not budget_quant_probe and not gpt_disagreement_probe:
                            cache_tag = "CACHE" if bool(llm_review.get("cache_hit")) else "API"
                            label = "LUNA RESCUE CONFIRM" if gpt_borderline_rescue else "LUNA CONFIRM"
                            print(
                                f"{symbol}: {label} [{cache_tag}] | strategy={strategy.strategy_id} "
                                f"side={'BUY' if signal == 1 else 'SELL'} "
                                f"conf={safe_float(llm_review.get('confidence')):.2f} "
                                f"opp={safe_float(details.get('opportunity_score')):.1f}/100"
                            )

                    portfolio_risk_mult, portfolio_risk_state = portfolio_soft_risk_multiplier(guard)
                    risk_multiplier = (
                        safe_float(learning_gate.get("risk_multiplier"), 1.0)
                        * super_decision.risk_multiplier
                        * portfolio_risk_mult
                        * safe_float(edge_risk_mult, 1.0)
                    )
                    details["edge_recovery_risk_multiplier"] = safe_float(edge_risk_mult, 1.0)
                    if hunter_setup is not None:
                        hunter_risk_mult, hunter_memory = micro_hunter.execution_risk_multiplier(
                            connection, hunter_setup
                        )
                        risk_multiplier = min(1.0, risk_multiplier * hunter_risk_mult)
                        details.setdefault("micro_hunter", {})["risk_multiplier_applied"] = hunter_risk_mult
                        details["micro_hunter"]["playbook_memory"] = hunter_memory
                    if execution_tier == "v8_native_alpha":
                        native_mult = safe_float(getattr(settings, "V8_NATIVE_ALPHA_RISK_MULTIPLIER", 0.30), 0.30)
                        risk_multiplier *= clamp(native_mult, 0.10, 0.50)
                        details["v8_native_alpha_risk_multiplier"] = native_mult
                    if gpt_borderline_rescue:
                        risk_multiplier *= safe_float(
                            getattr(settings, "SPARTAN_GPT_BORDERLINE_RISK_MULTIPLIER", 0.30), 0.30
                        )
                    if gpt_disagreement_probe:
                        disagreement_mult = safe_float(
                            getattr(settings, "V8_LUNA_DISAGREEMENT_RISK_MULTIPLIER", 0.10), 0.10
                        )
                        risk_multiplier *= clamp(disagreement_mult, 0.05, 0.20)
                        details["v8_luna_disagreement_probe_risk_multiplier"] = disagreement_mult
                    if budget_quant_probe:
                        budget_mult = safe_float(
                            getattr(settings, "V8_BUDGET_PROBE_RISK_MULTIPLIER", 0.07), 0.07
                        )
                        risk_multiplier *= clamp(budget_mult, 0.03, 0.10)
                        details["v8_budget_quant_probe_risk_multiplier"] = budget_mult
                    correlation_mult, correlation_state = scalp_lab.portfolio_correlation_risk(
                        symbol, signal, frame, machine_positions, fetch_raw_bars
                    )
                    risk_multiplier *= safe_float(correlation_mult, 1.0)
                    details["correlation_risk"] = correlation_state
                    execution_mult, execution_state = scalp_lab.execution_quality_risk_multiplier(
                        connection, symbol
                    )
                    risk_multiplier *= safe_float(execution_mult, 1.0)
                    details["execution_quality_risk"] = execution_state
                    growth_mult = 1.0
                    if bool(getattr(settings, "ENABLE_CAPITAL_GROWTH_CONTROLLER", True)):
                        growth_mult = safe_float(growth_state.get("risk_multiplier"), 1.0)
                        risk_multiplier *= max(0.0, min(1.0, growth_mult))
                    details["capital_growth_risk"] = {
                        "multiplier": growth_mult,
                        "phase": growth_state.get("phase"),
                        "growth_pct": safe_float(growth_state.get("growth_pct")),
                        "target_pct": safe_float(growth_state.get("target_pct")),
                        "stretch_pct": safe_float(growth_state.get("stretch_pct")),
                    }
                    details["portfolio_risk_state"] = {
                        "state": portfolio_risk_state,
                        "multiplier": portfolio_risk_mult,
                        "daily_loss_pct": safe_float(guard.get("daily_loss_pct")),
                        "drawdown_pct": safe_float(guard.get("drawdown_pct")),
                    }
                    fresh_tick = mt5.symbol_info_tick(symbol)
                    if fresh_tick is None:
                        scalp_diag.record(symbol, "tick_missing", "fresh MT5 tick unavailable")
                        continue
                    stale_entry = safe_float(tick.ask if signal == 1 else tick.bid)
                    entry = safe_float(fresh_tick.ask if signal == 1 else fresh_tick.bid)
                    if spartan.symbol_kind(symbol) in {"gold", "oil"}:
                        max_drift = safe_float(getattr(settings, "SPARTAN_MAX_ENTRY_DRIFT_ATR", 0.50), 0.50) * atr_value
                        if abs(entry - stale_entry) > max_drift:
                            log_decision(
                                connection, "demo", symbol, regime, strategy.strategy_id, signal,
                                "Spartan fresh-price drift veto",
                                {**details, "old_entry": stale_entry, "fresh_entry": entry, "atr": atr_value, "max_drift": max_drift},
                            )
                            scalp_diag.record(symbol, "price_drift", f"drift={abs(entry-stale_entry):.6g}")
                            continue
                    if bool(getattr(settings, "SPARTAN_FORCE_FIXED_ATR_EXITS", False)) and spartan.symbol_kind(symbol) in {"gold", "oil"}:
                        base_stop_atr = safe_float(getattr(settings, "SPARTAN_FIXED_SL_ATR", 1.0), 1.0)
                        base_take_atr = safe_float(getattr(settings, "SPARTAN_FIXED_TP_ATR", 1.5), 1.5)
                    else:
                        base_stop_atr = float(strategy.params["stop_atr"])
                        base_take_atr = float(strategy.params["take_atr"])
                    # V7 micro playbooks use bounded short-duration geometry. They
                    # may tighten/reshape exits, but cannot widen SL beyond the
                    # carrier strategy's existing ATR stop or bypass cash sizing.
                    hunter_ctx = details.get("micro_hunter") if isinstance(details.get("micro_hunter"), dict) else {}
                    if hunter_ctx:
                        hunter_stop = max(0.25, safe_float(hunter_ctx.get("stop_atr"), base_stop_atr))
                        hunter_take = max(0.35, safe_float(hunter_ctx.get("take_atr"), base_take_atr))
                        base_stop_atr = min(base_stop_atr, hunter_stop)
                        base_take_atr = hunter_take
                        details["micro_hunter"]["applied_stop_atr"] = base_stop_atr
                        details["micro_hunter"]["applied_take_atr"] = base_take_atr
                    stop_distance = base_stop_atr * atr_value * super_decision.sl_multiplier
                    take_distance = base_take_atr * atr_value * super_decision.tp_multiplier
                    stop = entry - stop_distance if signal == 1 else entry + stop_distance
                    take = entry + take_distance if signal == 1 else entry - take_distance
                    if execution_tier == "demo_trial":
                        base_risk_fraction = settings.DEMO_TRIAL_RISK_PER_TRADE
                        ceiling_fraction = settings.DEMO_TRIAL_MAX_MIN_LOT_RISK_PCT
                    else:
                        base_risk_fraction = settings.RISK_PER_TRADE
                        ceiling_fraction = settings.MAX_DEMO_MIN_LOT_RISK_PCT
                    risk_fraction = min(
                        safe_float(ceiling_fraction),
                        safe_float(base_risk_fraction) * max(0.0, risk_multiplier),
                    )
                    sizing = volume_plan(
                        symbol,
                        signal,
                        entry,
                        stop,
                        safe_float(account.equity),
                        allow_minimum_bridge=True,
                        risk_fraction=risk_fraction,
                        hard_ceiling_fraction=ceiling_fraction,
                    )
                    volume = safe_float(sizing.get("volume"))
                    if volume <= 0:
                        reason = str(sizing.get("reason") or "Order sizing rejected")
                        log_decision(
                            connection,
                            "demo",
                            symbol,
                            regime,
                            strategy.strategy_id,
                            signal,
                            reason,
                            {
                                **details,
                                "entry": entry,
                                "stop": stop,
                                "take": take,
                                "sizing": sizing,
                            },
                        )
                        min_risk = safe_float(sizing.get("minimum_lot_risk_cash"))
                        ceiling = safe_float(sizing.get("hard_ceiling_cash"))
                        required = safe_float(sizing.get("required_equity_for_target"))
                        print(
                            f"{symbol}: {execution_tier} | {reason} | "
                            f"min-lot risk=${min_risk:.2f} ceiling=${ceiling:.2f} "
                            f"target-equity=${required:.2f}"
                        )
                        continue
                    if bool(sizing.get("used_minimum_bridge")):
                        print(
                            f"{symbol}: {execution_tier} min-lot bridge | "
                            f"volume={volume} | "
                            f"risk=${safe_float(sizing.get('actual_risk_cash')):.2f} "
                            f"({safe_float(sizing.get('actual_risk_pct')):.2%})"
                        )
                    entry_context = learning_entry_context(
                        row, atr_value, spread, dom, details,
                        execution_tier, risk_multiplier,
                    )
                    if isinstance(details.get("micro_hunter"), dict):
                        entry_context["micro_hunter"] = details.get("micro_hunter", {})
                    entry_context["learning_snapshot"] = learning_gate.get("snapshot", {})
                    entry_context["target_risk_fraction"] = risk_fraction
                    entry_context["actual_risk_pct"] = safe_float(sizing.get("actual_risk_pct"))
                    entry_context["superlearner"] = super_decision.as_dict()
                    entry_context["superlearner"]["features"] = super_decision.features
                    entry_context["spartan_pro"] = details.get("spartan_pro", {})
                    entry_context["spartan_llm"] = details.get("spartan_llm", {})
                    if details.get("v8_luna_disagreement_probe"):
                        entry_context["v8_luna_disagreement_probe"] = details.get("v8_luna_disagreement_probe")
                    if details.get("v8_budget_quant_probe"):
                        entry_context["v8_budget_quant_probe"] = details.get("v8_budget_quant_probe")
                    entry_context["portfolio_risk_state"] = details.get("portfolio_risk_state", {})
                    order_started = time.perf_counter()
                    result = send_demo_order(
                        connection,
                        symbol,
                        strategy,
                        signal,
                        entry,
                        stop,
                        take,
                        volume,
                        execution_tier=execution_tier,
                    )
                    execution_latency_ms = (time.perf_counter() - order_started) * 1000.0
                    if bool(getattr(settings, "LAB_EXECUTION_QUALITY_ENABLED", True)):
                        scalp_lab.record_execution_quality(
                            connection, symbol=symbol, strategy_id=int(strategy.strategy_id), side=int(signal),
                            requested_entry=entry, filled_entry=getattr(result, "price", None),
                            spread_price=spread, atr_value=atr_value, latency_ms=execution_latency_ms,
                            retcode=getattr(result, "retcode", None), execution_tier=execution_tier,
                            details={"volume": volume, "stop": stop, "take": take, "opportunity_score": details.get("opportunity_score")},
                        )
                    time.sleep(0.20)
                    position_ticket = latest_machine_position_ticket(symbol)
                    register_demo_position(
                        connection,
                        position_ticket,
                        strategy,
                        regime,
                        signal,
                        volume,
                        entry,
                        stop,
                        take,
                        safe_float(sizing.get("actual_risk_cash")),
                        result,
                        execution_tier,
                        entry_context,
                        safe_float(learning_gate.get("snapshot", {}).get("confidence"), 0.50),
                        risk_multiplier,
                        super_probability=super_decision.probability,
                        super_decision=super_decision.as_dict(),
                        sl_multiplier=super_decision.sl_multiplier,
                        tp_multiplier=super_decision.tp_multiplier,
                    )
                    if hunter_setup is not None:
                        micro_hunter.mark_executed(connection, hunter_setup)
                    machine_positions = mt5_machine_positions()
                    log_decision(
                        connection,
                        "demo",
                        symbol,
                        regime,
                        strategy.strategy_id,
                        signal,
                        "Demo order sent",
                        {
                            **details,
                            "entry": entry,
                            "stop": stop,
                            "take": take,
                            "volume": volume,
                            "sizing": sizing,
                            "adaptive_learning": learning_gate,
                            "risk_multiplier": risk_multiplier,
                            "result": as_dict(result),
                        },
                    )
                    scalp_diag.record(symbol, "order_sent", f"strategy={strategy.strategy_id} {execution_tier}")
                    print(
                        f"DEMO ORDER [{execution_tier}] {symbol} "
                        f"{'BUY' if signal == 1 else 'SELL'} | "
                        f"strategy={strategy.strategy_id} {strategy.family} | "
                        f"volume={volume} regime={regime} DOM={dom} | "
                        f"learn_conf={safe_float(learning_gate.get('snapshot', {}).get('confidence'), 0.50):.2f} "
                        f"Pwin={super_decision.probability:.2f} "
                        f"SLx={super_decision.sl_multiplier:.2f} TPx={super_decision.tp_multiplier:.2f} "
                        f"risk_x={risk_multiplier:.2f} | "
                        f"retcode={getattr(result, 'retcode', None)}"
                    )
                    if bool(getattr(settings, "SPARTAN_TELEGRAM_ENABLED", False)):
                        spartan_telegram_alert(
                            f"DEMO {symbol} {'BUY' if signal == 1 else 'SELL'} | "
                            f"strategy={strategy.strategy_id} {strategy.family} | "
                            f"entry={entry} sl={stop} tp={take} volume={volume} | "
                            f"Pwin={super_decision.probability:.2f}"
                        )
                except Exception as error:
                    log_decision(
                        connection,
                        "demo",
                        symbol,
                        None,
                        None,
                        0,
                        f"Cycle error: {error}",
                        {"traceback": traceback.format_exc(limit=8)},
                    )
                    print(f"{symbol}: demo cycle error: {error}")
            connection.commit()
    finally:
        mt5.shutdown()

def shadow_candidate_strategies(
    connection: sqlite3.Connection,
    symbol: str,
) -> list[StrategyDefinition]:
    """Return strategies for current-market virtual validation.

    Historical passers are first priority. Near-pass rejected strategies may be
    observed to improve the evolutionary search, but cannot be promoted directly
    to executable status.
    """
    rows = connection.execute(
        """
        SELECT s.*, MAX(b.score) AS best_score,
               d.train_profit_factor, d.oos_profit_factor, d.oos_trades
        FROM strategies s
        JOIN backtests b ON b.strategy_id=s.id
        JOIN strategy_diagnostics d ON d.strategy_id=s.id
        WHERE s.symbol=? AND (
            s.status IN ('historical_validated', 'shadow_approved')
            OR (
                s.status='backtest_rejected'
                AND d.train_profit_factor>=?
                AND d.oos_profit_factor>=?
                AND d.oos_trades>=?
            )
        )
        GROUP BY s.id
        HAVING s.status IN ('historical_validated', 'shadow_approved')
               OR MAX(b.score)>=?
        ORDER BY
            CASE WHEN s.family IN (
                'super_scalp','scalp','micro_momentum','pullback_scalp',
                'breakout_scalp','mean_revert_scalp'
            ) THEN 0 ELSE 1 END,
            CASE s.status
                WHEN 'historical_validated' THEN 0
                WHEN 'shadow_approved' THEN 1
                ELSE 2
            END,
            COALESCE((
                SELECT observations FROM candidate_live_scores cls
                WHERE cls.strategy_id=s.id
            ), 0) ASC,
            best_score DESC
        LIMIT ?
        """,
        (
            symbol,
            settings.SHADOW_MIN_TRAIN_PROFIT_FACTOR,
            settings.SHADOW_MIN_OOS_PROFIT_FACTOR,
            settings.SHADOW_MIN_OOS_TRADES,
            settings.SHADOW_MIN_BACKTEST_SCORE,
            settings.SHADOW_CANDIDATES_PER_SYMBOL,
        ),
    ).fetchall()
    return [load_strategy(row) for row in rows]


def update_candidate_live_score(
    connection: sqlite3.Connection,
    strategy_id: int,
    symbol: str,
    reward_r: float,
) -> dict[str, Any]:
    row = connection.execute(
        "SELECT * FROM candidate_live_scores WHERE strategy_id=?",
        (strategy_id,),
    ).fetchone()
    if row:
        observations = int(row["observations"]) + 1
        wins = int(row["wins"]) + (1 if reward_r > 0 else 0)
        losses = int(row["losses"]) + (1 if reward_r <= 0 else 0)
        gross_win_r = safe_float(row["gross_win_r"]) + max(0.0, reward_r)
        gross_loss_r = safe_float(row["gross_loss_r"]) + abs(min(0.0, reward_r))
        cumulative_r = safe_float(row["cumulative_r"]) + reward_r
        peak_r = max(safe_float(row["peak_r"]), cumulative_r)
        max_drawdown_r = max(
            safe_float(row["max_drawdown_r"]), peak_r - cumulative_r
        )
        reward_mean = cumulative_r / observations if observations else 0.0
        connection.execute(
            """
            UPDATE candidate_live_scores
            SET observations=?, wins=?, losses=?, gross_win_r=?, gross_loss_r=?,
                reward_mean=?, cumulative_r=?, peak_r=?, max_drawdown_r=?,
                updated_at=?
            WHERE strategy_id=?
            """,
            (
                observations, wins, losses, gross_win_r, gross_loss_r,
                reward_mean, cumulative_r, peak_r, max_drawdown_r,
                utc_now(), strategy_id,
            ),
        )
    else:
        observations = 1
        wins = 1 if reward_r > 0 else 0
        losses = 1 if reward_r <= 0 else 0
        gross_win_r = max(0.0, reward_r)
        gross_loss_r = abs(min(0.0, reward_r))
        cumulative_r = reward_r
        peak_r = max(0.0, reward_r)
        max_drawdown_r = peak_r - cumulative_r
        reward_mean = reward_r
        connection.execute(
            """
            INSERT INTO candidate_live_scores(
                strategy_id, symbol, observations, wins, losses,
                gross_win_r, gross_loss_r, reward_mean, cumulative_r,
                peak_r, max_drawdown_r, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                strategy_id, symbol, observations, wins, losses,
                gross_win_r, gross_loss_r, reward_mean, cumulative_r,
                peak_r, max_drawdown_r, utc_now(),
            ),
        )
    profit_factor = (
        gross_win_r / gross_loss_r
        if gross_loss_r > 0
        else (5.0 if gross_win_r > 0 else 0.0)
    )
    return {
        "observations": observations,
        "wins": wins,
        "losses": losses,
        "reward_mean": reward_mean,
        "cumulative_r": cumulative_r,
        "profit_factor": profit_factor,
        "max_drawdown_r": max_drawdown_r,
    }


def maybe_promote_shadow_candidate(
    connection: sqlite3.Connection,
    strategy_id: int,
    live: dict[str, Any],
) -> bool:
    row = connection.execute(
        """
        SELECT s.status, s.symbol, s.family,
               COALESCE(d.oos_profit_factor,0) AS oos_profit_factor,
               COALESCE(d.stability_score,0) AS stability_score
        FROM strategies s
        LEFT JOIN strategy_diagnostics d ON d.strategy_id=s.id
        WHERE s.id=?
        """,
        (strategy_id,),
    ).fetchone()
    if not row:
        return False

    status = str(row["status"])
    observations = int(live["observations"])
    normal_live_ok = (
        observations >= settings.SHADOW_APPROVAL_TRADES
        and safe_float(live["reward_mean"]) >= settings.SHADOW_APPROVAL_MEAN_R
        and safe_float(live["profit_factor"]) >= settings.SHADOW_APPROVAL_PROFIT_FACTOR
        and safe_float(live["max_drawdown_r"]) <= settings.SHADOW_APPROVAL_MAX_DRAWDOWN_R
    )
    scalp_fast_track = bool(
        bool(getattr(settings, "SCALP_FAST_TRACK_SHADOW_ENABLED", True))
        and family_is_scalp(str(row["family"]))
        and observations >= int(getattr(settings, "SCALP_FAST_TRACK_TRADES", 20))
        and safe_float(live["reward_mean"]) >= safe_float(getattr(settings, "SCALP_FAST_TRACK_MEAN_R", 0.18), 0.18)
        and safe_float(live["profit_factor"]) >= safe_float(getattr(settings, "SCALP_FAST_TRACK_PROFIT_FACTOR", 1.40), 1.40)
        and safe_float(live["max_drawdown_r"]) <= safe_float(getattr(settings, "SCALP_FAST_TRACK_MAX_DRAWDOWN_R", 2.50), 2.50)
        and safe_float(row["oos_profit_factor"]) >= safe_float(getattr(settings, "SCALP_FAST_TRACK_MIN_OOS_PF", 1.10), 1.10)
        and safe_float(row["stability_score"]) >= safe_float(getattr(settings, "SCALP_FAST_TRACK_MIN_STABILITY", 0.70), 0.70)
    )
    live_ok = normal_live_ok or scalp_fast_track
    catastrophic_bad = (
        observations >= int(getattr(settings, "SCALP_SHADOW_EARLY_QUARANTINE_TRADES", 8))
        and safe_float(live["reward_mean"]) <= safe_float(getattr(settings, "SCALP_SHADOW_EARLY_QUARANTINE_MEAN_R", -0.25), -0.25)
    )
    live_bad = catastrophic_bad or (
        observations >= settings.SHADOW_REJECT_AFTER_TRADES
        and safe_float(live["reward_mean"]) <= settings.SHADOW_REJECT_MEAN_R
    )

    now = utc_now()
    if status == "historical_validated" and live_ok:
        approval_label = "Fast-track scalp shadow approved" if scalp_fast_track and not normal_live_ok else "Shadow approved"
        reason = (
            f"{approval_label} after historical/OOS/walk-forward pass: "
            f"trades={observations}, mean={live['reward_mean']:.3f}R, "
            f"PF={live['profit_factor']:.2f}, DD={live['max_drawdown_r']:.2f}R"
        )
        connection.execute(
            "UPDATE strategies SET status='shadow_approved' WHERE id=?",
            (strategy_id,),
        )
        connection.execute(
            "UPDATE strategy_diagnostics SET validation_reason=?, updated_at=? "
            "WHERE strategy_id=?",
            (reason, now, strategy_id),
        )
        connection.execute(
            "UPDATE candidate_live_scores SET promoted_at=?, updated_at=? "
            "WHERE strategy_id=?",
            (now, now, strategy_id),
        )
        print(
            f"SHADOW APPROVED {row['symbol']} | strategy={strategy_id} "
            f"{row['family']} | {reason}"
        )
        return True

    if status in {"historical_validated", "shadow_approved"} and live_bad:
        reason = (
            "Shadow rejected/demoted: "
            f"trades={observations}, mean={live['reward_mean']:.3f}R, "
            f"PF={live['profit_factor']:.2f}"
        )
        connection.execute(
            "UPDATE strategies SET status='shadow_rejected' WHERE id=?",
            (strategy_id,),
        )
        connection.execute(
            "UPDATE strategy_diagnostics SET validation_reason=?, updated_at=? "
            "WHERE strategy_id=?",
            (reason, now, strategy_id),
        )
        print(
            f"SHADOW REJECTED {row['symbol']} | strategy={strategy_id} "
            f"{row['family']} | {reason}"
        )

    # A historical rejection is learning evidence only. It is intentionally
    # never promoted directly to execution by current-market results.
    return False


def shadow_execution_memory_weight(
    connection: sqlite3.Connection,
    strategy_id: int,
) -> tuple[float, str]:
    """Return execution-memory weight for a research shadow strategy.

    Shadow positions are intentionally broad research. A backtest-rejected
    experiment must not teach the broker-execution collective/session memory
    that is later used to approve real DEMO candidates.
    """
    row = connection.execute(
        "SELECT status FROM strategies WHERE id=?", (int(strategy_id),)
    ).fetchone()
    status = str(row["status"] if row else "unknown")
    setting_by_status = {
        "backtest_rejected": "SHADOW_MEMORY_WEIGHT_BACKTEST_REJECTED",
        "shadow_rejected": "SHADOW_MEMORY_WEIGHT_SHADOW_REJECTED",
        "historical_validated": "SHADOW_MEMORY_WEIGHT_HISTORICAL_VALIDATED",
        "shadow_approved": "SHADOW_MEMORY_WEIGHT_SHADOW_APPROVED",
    }
    setting_name = setting_by_status.get(status)
    if setting_name is None:
        return 0.0, status
    return clamp(safe_float(getattr(settings, setting_name, 0.0), 0.0), 0.0, 1.0), status


def shadow_virtual_fill(
    entry: float,
    stop: float,
    take: float,
    side: int,
    trigger_price: float,
    reason: str,
) -> tuple[float, float]:
    """Return a deterministic virtual fill and R outcome for shadow research.

    Shadow positions are not broker orders.  When the polling loop notices that
    SL/TP was crossed, filling at the *current* quote can manufacture multi-R
    overshoots after a restart/gap.  For research integrity we therefore fill
    virtual SL/TP at their configured levels.  Time exits still use the current
    executable quote.  Real DEMO trades continue to use broker deal history.
    """
    entry_f = safe_float(entry)
    stop_f = safe_float(stop)
    take_f = safe_float(take)
    mark_f = safe_float(trigger_price)
    side_i = 1 if int(side) >= 0 else -1
    stop_distance = abs(entry_f - stop_f)
    if stop_distance <= 1e-12:
        return mark_f, 0.0
    if reason == "stop":
        return stop_f, -1.0
    if reason == "take":
        target_r = ((take_f - entry_f) * side_i) / stop_distance
        return take_f, max(0.0, safe_float(target_r))
    reward_r = ((mark_f - entry_f) * side_i) / stop_distance
    return mark_f, safe_float(reward_r)


def _shadow_live_aggregate(rows: list[sqlite3.Row]) -> dict[int, dict[str, Any]]:
    grouped: dict[int, dict[str, Any]] = {}
    for row in rows:
        sid = int(row["strategy_id"])
        reward = safe_float(row["reward_r"])
        state = grouped.setdefault(sid, {
            "symbol": str(row["symbol"]), "n": 0, "wins": 0, "losses": 0,
            "gross_win": 0.0, "gross_loss": 0.0, "cum": 0.0,
            "peak": 0.0, "max_dd": 0.0,
        })
        state["n"] += 1
        state["wins"] += 1 if reward > 0 else 0
        state["losses"] += 1 if reward <= 0 else 0
        state["gross_win"] += max(0.0, reward)
        state["gross_loss"] += abs(min(0.0, reward))
        state["cum"] += reward
        state["peak"] = max(state["peak"], state["cum"])
        state["max_dd"] = max(state["max_dd"], state["peak"] - state["cum"])
    return grouped


def repair_shadow_fill_integrity(connection: sqlite3.Connection) -> dict[str, int]:
    """One-time V6.9.1 repair for historical virtual SL/TP overshoot artifacts."""
    marker = state_get(connection, "shadow_fill_integrity_repair")
    if marker == "v6.9.1":
        return {"positions": 0, "events": 0}

    rows = connection.execute(
        """
        SELECT id, strategy_id, symbol, entry_price, stop_loss, take_profit,
               side, reward_r, close_reason
        FROM shadow_positions
        WHERE status='closed' AND reward_r IS NOT NULL
          AND close_reason IN ('stop','take')
        ORDER BY id
        """
    ).fetchall()
    repaired: dict[int, float] = {}
    for row in rows:
        fill, canonical = shadow_virtual_fill(
            safe_float(row["entry_price"]), safe_float(row["stop_loss"]),
            safe_float(row["take_profit"]), int(row["side"]),
            safe_float(row["exit_price"] if "exit_price" in row.keys() else 0.0),
            str(row["close_reason"]),
        )
        old_reward = safe_float(row["reward_r"])
        if abs(old_reward - canonical) <= 1e-9:
            continue
        connection.execute(
            "UPDATE shadow_positions SET exit_price=?, reward_r=? WHERE id=?",
            (fill, canonical, int(row["id"])),
        )
        repaired[int(row["id"])] = canonical

    # Keep the append-only reward ledger internally consistent with repaired
    # shadow positions.  Only shadow_live events are touched; demo_trade rows are
    # never modified.
    event_updates = 0
    if repaired:
        events = connection.execute(
            "SELECT id, details_json FROM rl_reward_events WHERE source='shadow_live'"
        ).fetchall()
        for event in events:
            try:
                details = json.loads(str(event["details_json"] or "{}"))
                shadow_id = int(details.get("shadow_position_id", 0) or 0)
            except Exception:
                continue
            if shadow_id in repaired:
                connection.execute(
                    "UPDATE rl_reward_events SET reward=? WHERE id=?",
                    (repaired[shadow_id], int(event["id"])),
                )
                event_updates += 1

        # strategy_scores is a mixed demo+shadow rolling mean. Rebuild it from
        # the corrected immutable reward ledger rather than applying fragile
        # deltas.
        connection.execute("DELETE FROM strategy_scores")
        connection.execute(
            """
            INSERT INTO strategy_scores(
                strategy_id, symbol, regime, observations, reward_mean, updated_at
            )
            SELECT strategy_id, symbol, regime, COUNT(*), AVG(reward), MAX(timestamp)
            FROM rl_reward_events
            GROUP BY strategy_id, symbol, regime
            """
        )

        # Rebuild candidate_live_scores from canonical shadow outcomes so old
        # overshoot artifacts cannot distort promotion/quarantine decisions.
        promoted = {
            int(r["strategy_id"]): r["promoted_at"]
            for r in connection.execute(
                "SELECT strategy_id, promoted_at FROM candidate_live_scores"
            ).fetchall()
        }
        shadow_rows = connection.execute(
            """
            SELECT strategy_id, symbol, reward_r
            FROM shadow_positions
            WHERE status='closed' AND reward_r IS NOT NULL
            ORDER BY strategy_id, id
            """
        ).fetchall()
        aggregates = _shadow_live_aggregate(shadow_rows)
        connection.execute("DELETE FROM candidate_live_scores")
        now = utc_now()
        for sid, st in aggregates.items():
            n = int(st["n"])
            mean = safe_float(st["cum"]) / n if n else 0.0
            connection.execute(
                """
                INSERT INTO candidate_live_scores(
                    strategy_id, symbol, observations, wins, losses,
                    gross_win_r, gross_loss_r, reward_mean, cumulative_r,
                    peak_r, max_drawdown_r, promoted_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sid, st["symbol"], n, st["wins"], st["losses"],
                    st["gross_win"], st["gross_loss"], mean, st["cum"],
                    st["peak"], st["max_dd"], promoted.get(sid), now,
                ),
            )

    state_set(connection, "shadow_fill_integrity_repair", "v6.9.1")
    state_set(connection, "shadow_fill_integrity_repaired_positions", len(repaired))
    state_set(connection, "shadow_fill_integrity_repaired_events", event_updates)
    if repaired:
        print(
            "SHADOW FILL INTEGRITY REPAIR | "
            f"positions={len(repaired)} reward_events={event_updates} | "
            "virtual SL/TP fills normalized; DEMO broker trades untouched"
        )
    return {"positions": len(repaired), "events": event_updates}


def close_shadow_positions(
    connection: sqlite3.Connection,
    symbol: str,
    tick: Any,
) -> None:
    rows = connection.execute(
        """
        SELECT * FROM shadow_positions
        WHERE symbol=? AND status='open'
        ORDER BY id
        """,
        (symbol,),
    ).fetchall()
    now_dt = datetime.now(timezone.utc)
    for row in rows:
        side = int(row["side"])
        exit_price = safe_float(tick.bid if side == 1 else tick.ask)
        stop = safe_float(row["stop_loss"])
        take = safe_float(row["take_profit"])
        entry = safe_float(row["entry_price"])
        reason: str | None = None
        if side == 1 and exit_price <= stop:
            reason = "stop"
        elif side == 1 and exit_price >= take:
            reason = "take"
        elif side == -1 and exit_price >= stop:
            reason = "stop"
        elif side == -1 and exit_price <= take:
            reason = "take"
        try:
            opened = datetime.fromisoformat(str(row["opened_at"]))
            held_minutes = (now_dt - opened).total_seconds() / 60.0
        except ValueError:
            held_minutes = 0.0
        if reason is None and held_minutes >= int(row["max_hold"]):
            reason = "time"
        if reason is None:
            continue
        exit_price, reward_r = shadow_virtual_fill(
            entry, stop, take, side, exit_price, reason
        )
        connection.execute(
            """
            UPDATE shadow_positions
            SET status='closed', closed_at=?, exit_price=?, reward_r=?, close_reason=?
            WHERE id=?
            """,
            (utc_now(), exit_price, reward_r, reason, int(row["id"])),
        )
        live = update_candidate_live_score(
            connection, int(row["strategy_id"]), symbol, reward_r
        )
        update_reward_memory(
            connection,
            int(row["strategy_id"]),
            symbol,
            str(row["regime"]),
            reward_r,
            "shadow_live",
            {
                "shadow_position_id": int(row["id"]),
                "close_reason": reason,
                "entry": entry,
                "exit": exit_price,
            },
        )
        memory_weight, strategy_status = shadow_execution_memory_weight(
            connection, int(row["strategy_id"])
        )
        if memory_weight > 0.0:
            update_collective_memory(
                connection, symbol, str(row["family"]), str(row["regime"]),
                int(row["side"]), reward_r, memory_weight,
            )
            update_session_family_memory(
                connection, symbol, str(row["family"]), str(row["regime"]),
                market_session_tag(symbol, row["opened_at"]),
                int(row["side"]), reward_r, memory_weight,
            )
        try:
            shadow_context = (
                json.loads(str(row["context_json"]))
                if "context_json" in row.keys() and row["context_json"] else {}
            )
            shadow_features = (
                shadow_context.get("superlearner", {}).get("features", {})
                if isinstance(shadow_context, dict) else {}
            )
            shadow_model_weight = safe_float(
                getattr(settings, "SUPERLEARNER_SHADOW_MODEL_WEIGHT", 0.0), 0.0
            )
            if shadow_features and shadow_model_weight > 0.0:
                update_online_model(
                    connection, symbol, shadow_features, reward_r, shadow_model_weight,
                )
        except Exception as model_error:
            print(f"SHADOW SUPERLEARNER UPDATE WARNING: {model_error}")
        print(
            f"SHADOW CLOSED {symbol} | strategy={row['strategy_id']} "
            f"{row['family']} | {reason} | {reward_r:.2f}R | "
            f"live_n={live['observations']} PF={live['profit_factor']:.2f} | "
            f"status={strategy_status} exec_memory_w={memory_weight:.2f}"
        )
        maybe_promote_shadow_candidate(
            connection, int(row["strategy_id"]), live
        )


def shadow_cycle() -> None:
    connect_mt5(show_account=False)
    try:
        with db_connect() as connection:
            for symbol in settings.SYMBOLS:
                try:
                    tick = mt5.symbol_info_tick(symbol)
                    if tick is None:
                        continue
                    close_shadow_positions(connection, symbol, tick)
                    raw = fetch_raw_bars(symbol, settings.LIVE_BARS)
                    closed_bar_time = pd.Timestamp(raw.iloc[-2]["time"]).isoformat()
                    key = f"shadow_last_bar:{symbol}"
                    if state_get(connection, key) == closed_bar_time:
                        continue
                    state_set(connection, key, closed_bar_time)
                    definitions = shadow_candidate_strategies(connection, symbol)
                    if not definitions:
                        print(f"{symbol}: no near-pass candidates for live shadow lab")
                        continue
                    open_rows = connection.execute(
                        """
                        SELECT strategy_id FROM shadow_positions
                        WHERE symbol=? AND status='open'
                        """,
                        (symbol,),
                    ).fetchall()
                    open_ids = {int(row["strategy_id"]) for row in open_rows}
                    slots = max(
                        0,
                        settings.SHADOW_MAX_OPEN_PER_SYMBOL - len(open_ids),
                    )
                    if slots <= 0:
                        continue
                    frame = add_features(raw, required_ema_lengths(definitions))
                    regime = detect_regime(frame)
                    micro = order_book_microstructure(symbol)
                    dom = micro.get("imbalance") if micro.get("available") else None
                    row = frame.iloc[-2]
                    atr_value = safe_float(row["atr_14"])
                    spread = safe_float(tick.ask - tick.bid)
                    if atr_value <= 0 or spread > atr_value * settings.MAX_SPREAD_ATR_FRACTION:
                        continue
                    opened = 0
                    for definition in definitions:
                        if definition.strategy_id in open_ids:
                            continue
                        try:
                            signal = row_signal(
                                frame, len(frame) - 2, definition, dom
                            )
                        except Exception as error:
                            log_decision(
                                connection,
                                "shadow",
                                symbol,
                                regime,
                                definition.strategy_id,
                                0,
                                f"Candidate signal error: {error}",
                                {},
                            )
                            continue
                        if signal == 0:
                            continue
                        entry = safe_float(tick.ask if signal == 1 else tick.bid)
                        stop_distance = safe_float(definition.params.get("stop_atr")) * atr_value
                        take_distance = safe_float(definition.params.get("take_atr")) * atr_value
                        if stop_distance <= 0 or take_distance <= 0:
                            continue
                        stop = entry - stop_distance if signal == 1 else entry + stop_distance
                        take = entry + take_distance if signal == 1 else entry - take_distance
                        shadow_snapshot = adaptive_learning_snapshot(
                            connection, definition.strategy_id, symbol, regime, signal
                        )
                        shadow_rolling = rolling_setup_performance(
                            connection, definition.strategy_id, symbol, regime, signal,
                            max(5, int(getattr(settings, "SUPERLEARNER_ROLLING_WINDOW", 20))),
                        )
                        shadow_gate = {
                            "snapshot": shadow_snapshot,
                            "bayes_loss_probability": bayesian_loss_probability(
                                shadow_snapshot, shadow_rolling
                            ),
                        }
                        shadow_signal_details = {
                            "buy_votes": 1 if signal == 1 else 0,
                            "sell_votes": 1 if signal == -1 else 0,
                            "buy_weight": 1.0 if signal == 1 else 0.0,
                            "sell_weight": 1.0 if signal == -1 else 0.0,
                            "shadow_observation": True,
                        }
                        shadow_super = SUPER_LEARNER.predict(
                            connection, definition, frame, row, regime, signal,
                            shadow_signal_details, shadow_gate, spread, atr_value, micro,
                        )
                        shadow_context = {
                            "superlearner": shadow_super.as_dict(),
                            "shadow_observation": True,
                            "entry": entry, "stop": stop, "take": take,
                        }
                        connection.execute(
                            """
                            INSERT INTO shadow_positions(
                                strategy_id, symbol, family, regime, side,
                                entry_price, stop_loss, take_profit,
                                opened_bar_time, opened_at, max_hold, context_json, status
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')
                            """,
                            (
                                definition.strategy_id,
                                symbol,
                                definition.family,
                                regime,
                                signal,
                                entry,
                                stop,
                                take,
                                closed_bar_time,
                                utc_now(),
                                int(definition.params.get("max_hold", 10)),
                                json_text(shadow_context),
                            ),
                        )
                        log_decision(
                            connection,
                            "shadow",
                            symbol,
                            regime,
                            definition.strategy_id,
                            signal,
                            "Live MT5 shadow position opened; no broker order sent",
                            {
                                "entry": entry,
                                "stop": stop,
                                "take": take,
                                "dom": dom,
                                "superlearner_observation": shadow_super.as_dict(),
                            },
                        )
                        print(
                            f"SHADOW OPEN {symbol} {'BUY' if signal == 1 else 'SELL'} | "
                            f"strategy={definition.strategy_id} {definition.family} | "
                            f"regime={regime}"
                        )
                        open_ids.add(definition.strategy_id)
                        opened += 1
                        if opened >= slots:
                            break
                except Exception as error:
                    print(f"{symbol}: shadow cycle error: {error}")
                    log_decision(
                        connection,
                        "shadow",
                        symbol,
                        None,
                        None,
                        0,
                        f"Shadow cycle error: {error}",
                        {"traceback": traceback.format_exc(limit=8)},
                    )
            connection.commit()
    finally:
        mt5.shutdown()


def run_loop(mode: str, hours: float, interval: float) -> None:
    init_database()
    finish_at = time.time() + max(0.01, hours) * 3600
    print(
        f"{mode.upper()} loop started for {hours:g} hour(s), "
        f"interval={interval}s. Ctrl+C stops safely."
    )
    try:
        while time.time() < finish_at:
            started = time.time()
            try:
                if mode == "paper":
                    paper_cycle()
                elif mode == "shadow":
                    shadow_cycle()
                else:
                    demo_cycle()
            except Exception as error:
                print(f"{mode} loop error: {error}")
            remaining = finish_at - time.time()
            if remaining <= 0:
                break
            sleep_for = max(0.05 if mode == "demo" and bool(getattr(settings, "V11_ENABLED", False)) else 1.0,
                            float(interval) - (time.time() - started))
            time.sleep(min(sleep_for, remaining))
    except KeyboardInterrupt:
        print(f"\n{mode.capitalize()} loop stopped by user")
    finally:
        if mode == "demo" and bool(getattr(settings, "V11_ENABLED", False)):
            mt5.shutdown()


def strategy_factory(
    hours: float,
    generate_count: int,
    backtest_limit: int,
    children: int,
    sleep_seconds: int,
) -> None:
    init_database()
    finish_at = time.time() + max(0.01, hours) * 3600
    batch = 0
    print(
        f"Strategy factory started for {hours:g} hour(s). "
        "It continuously generates, backtests, evolves and reports."
    )
    try:
        while time.time() < finish_at:
            batch += 1
            print(f"\n===== FACTORY BATCH {batch} =====")
            try:
                # Two hypothesis streams run in every batch: broad random
                # exploration and current-regime-guided exploration.
                generate_strategies(generate_count)
                live_regime_seed_strategies(
                    settings.LIVE_GUIDED_STRATEGIES_PER_SYMBOL
                )
                backtest_pending(backtest_limit)
                # Evolution ranks parents using historical score plus shadow/demo
                # reward memory, so live outcomes influence the next generation.
                evolve_top_strategies(children)
                backtest_pending(backtest_limit)
                status_report(write_file=True)
            except Exception as error:
                print(f"Factory batch error: {error}")
            remaining = finish_at - time.time()
            if remaining <= 0:
                break
            time.sleep(min(max(5, sleep_seconds), remaining))
    except KeyboardInterrupt:
        print("\nStrategy factory stopped by user")



# ============================================================
# Split 24/7 engines for visible multi-window operation
# ============================================================


def _run_until(hours: float) -> float:
    return time.time() + max(0.01, hours) * 3600


def strategy_generation_engine(
    hours: float,
    random_count: int,
    guided_count: int,
    children: int,
    interval: int,
) -> None:
    """Continuously create hypotheses without flooding the backtest queue."""
    init_database()
    finish_at = _run_until(hours)
    cycle = 0
    print("=" * 76)
    print("SCREEN 1/5 - SCALP-FIRST STRATEGY GENERATOR + COLLECTIVE/SESSION MEMORY")
    print("Random + current MT5 regime guidance + reward-led mutation")
    print("Queue throttle pauses creation while historical testing catches up.")
    print("=" * 76)
    try:
        while time.time() < finish_at:
            cycle += 1
            started = time.time()
            print(f"\n===== GENERATION CYCLE {cycle} | {utc_now()} =====")
            try:
                with db_connect() as connection:
                    pending_rows_count = connection.execute(
                        """
                        SELECT COUNT(*) AS n
                        FROM strategies s
                        WHERE s.status IN ('generated','needs_revalidation','backtest_error')
                          AND (
                              s.status='needs_revalidation'
                              OR NOT EXISTS (
                                  SELECT 1 FROM backtests b WHERE b.strategy_id=s.id
                              )
                          )
                        """
                    ).fetchone()["n"]
                    counts = connection.execute(
                        "SELECT status, COUNT(*) AS n FROM strategies GROUP BY status"
                    ).fetchall()
                    state_set(connection, "heartbeat:generator", utc_now())
                max_pending = max(
                    1, int(getattr(settings, "MAX_PENDING_STRATEGIES_TOTAL", 900))
                )
                if int(pending_rows_count) >= max_pending:
                    count_text = ", ".join(
                        f"{r['status']}={r['n']}" for r in counts
                    )
                    print(
                        f"QUEUE THROTTLE | pending={pending_rows_count}/{max_pending} | "
                        f"generation paused until backtester catches up | {count_text}"
                    )
                else:
                    remaining_budget = max_pending - int(pending_rows_count)
                    per_stream_budget = max(
                        1, remaining_budget // max(1, len(settings.SYMBOLS) * 3)
                    )
                    random_run = min(max(1, random_count), per_stream_budget)
                    guided_run = min(max(1, guided_count), per_stream_budget)
                    random_new = generate_strategies(random_run)
                    guided_new = live_regime_seed_strategies(guided_run)
                    # Evolution can add parents*children strategies, so skip it
                    # when the queue is already close to the hard cap.
                    evolved_new = 0
                    estimated_evolution = (
                        len(settings.SYMBOLS)
                        * max(1, settings.EVOLVE_PARENTS_PER_SYMBOL)
                        * max(1, children)
                    )
                    if remaining_budget > estimated_evolution:
                        evolved_new = evolve_top_strategies(max(1, children))
                    with db_connect() as connection:
                        counts = connection.execute(
                            "SELECT status, COUNT(*) AS n FROM strategies GROUP BY status"
                        ).fetchall()
                        pending_after = connection.execute(
                            """
                            SELECT COUNT(*) AS n FROM strategies s
                            WHERE s.status IN ('generated','needs_revalidation','backtest_error')
                              AND (s.status='needs_revalidation' OR NOT EXISTS (
                                  SELECT 1 FROM backtests b WHERE b.strategy_id=s.id
                              ))
                            """
                        ).fetchone()["n"]
                        state_set(connection, "heartbeat:generator", utc_now())
                    count_text = ", ".join(
                        f"{r['status']}={r['n']}" for r in counts
                    )
                    print(
                        f"GENERATOR SUMMARY | random={random_new} guided={guided_new} "
                        f"evolved={evolved_new} | pending={pending_after}/{max_pending} | "
                        f"{count_text}"
                    )
            except Exception as error:
                print(f"GENERATOR CYCLE ERROR: {error}")
                traceback.print_exc(limit=8)
            remaining = finish_at - time.time()
            if remaining <= 0:
                break
            elapsed = int(time.time() - started)
            time.sleep(min(max(5, interval - elapsed), remaining))
    except KeyboardInterrupt:
        print("\nStrategy generator stopped by user")

def historical_backtest_engine(hours: float, limit: int, interval: int) -> None:
    """Continuously consume generated/revalidation strategies through all tests."""
    init_database()
    finish_at = _run_until(hours)
    cycle = 0
    print("=" * 76)
    print("SCREEN 2/5 - HISTORICAL/OOS/WALK-FORWARD BACKTESTER")
    print("Consumes generated strategies and tests them on cached M1 history.")
    print("Passers enter historical_validated; failures remain non-executable.")
    print("=" * 76)
    try:
        while time.time() < finish_at:
            cycle += 1
            started = time.time()
            print(f"\n===== BACKTEST CYCLE {cycle} | {utc_now()} =====")
            try:
                report = backtest_pending(max(1, limit))
                with db_connect() as connection:
                    pending = connection.execute(
                        """
                        SELECT COUNT(*) AS n FROM strategies
                        WHERE status IN ('generated','needs_revalidation','backtest_error')
                        """
                    ).fetchone()["n"]
                    passed = connection.execute(
                        "SELECT COUNT(*) AS n FROM strategies WHERE status='historical_validated'"
                    ).fetchone()["n"]
                    approved = connection.execute(
                        "SELECT COUNT(*) AS n FROM strategies WHERE status='shadow_approved'"
                    ).fetchone()["n"]
                    state_set(connection, "heartbeat:backtester", utc_now())
                tested = sum(
                    len(items) for items in (report.get("symbols") or {}).values()
                ) if isinstance(report, dict) else 0
                print(
                    f"BACKTEST SUMMARY | tested_this_cycle={tested} pending={pending} "
                    f"historical_validated={passed} shadow_approved={approved}"
                )
            except Exception as error:
                print(f"BACKTEST CYCLE ERROR: {error}")
                traceback.print_exc(limit=8)
            remaining = finish_at - time.time()
            if remaining <= 0:
                break
            elapsed = int(time.time() - started)
            time.sleep(min(max(5, interval - elapsed), remaining))
    except KeyboardInterrupt:
        print("\nHistorical backtester stopped by user")


def approved_library_cycle() -> None:
    """Export/display approved strategies and the DEMO-only trial fallback."""
    init_database()
    connect_mt5(show_account=False)
    try:
        export_rows: list[dict[str, Any]] = []
        with db_connect() as connection:
            status_rows = connection.execute(
                "SELECT status, COUNT(*) AS n FROM strategies GROUP BY status ORDER BY status"
            ).fetchall()
            print("STRATEGY DATABASE | " + ", ".join(
                f"{row['status']}={row['n']}" for row in status_rows
            ))

            for symbol in settings.SYMBOLS:
                try:
                    raw = fetch_raw_bars(symbol, settings.LIVE_BARS)
                    frame = add_features(raw, {20, 50})
                    regime = detect_regime(frame)
                    approved = candidate_strategies(connection, symbol, regime)
                    trial = demo_trial_candidate_strategies(connection, symbol, regime)
                    active_candidates, active_tier = execution_candidate_set(
                        connection, symbol, regime
                    )
                    approved_scalps = sum(
                        1 for definition, _score in approved
                        if family_is_scalp(definition.family)
                    )
                    trial_scalps = sum(
                        1 for definition, _score in trial
                        if family_is_scalp(definition.family)
                    )
                    print(
                        f"\n{symbol} | current_regime={regime} | "
                        f"approved={len(approved)} (scalp={approved_scalps}) "
                        f"trial={len(trial)} (scalp={trial_scalps}) | "
                        f"executor_tier={active_tier} active={len(active_candidates)}"
                    )
                    if active_tier == "demo_trial":
                        print(
                            "  SCALP-FIRST DEMO trial bridge is active while shadow validation "
                            "continues; no trade is forced without a real signal."
                        )
                    elif active_tier == "none":
                        print("  No approved or eligible historical trial strategy yet.")

                    # Export the exact candidate tier consumed by the executor, rather
                    # than showing a trend-heavy approved pool that may not be active.
                    for tier, candidates in ((active_tier, active_candidates),):
                        for rank, (definition, selection_score) in enumerate(
                            candidates[:10], 1
                        ):
                            live = connection.execute(
                                """
                                SELECT observations, reward_mean FROM strategy_scores
                                WHERE strategy_id=? AND symbol=? AND regime=?
                                """,
                                (definition.strategy_id, symbol, regime),
                            ).fetchone()
                            strategy_status = connection.execute(
                                "SELECT status FROM strategies WHERE id=?",
                                (definition.strategy_id,),
                            ).fetchone()["status"]
                            adaptive_adjustment, adaptive_meta = adaptive_candidate_adjustment(
                                connection, definition.strategy_id, symbol, regime
                            )
                            _context_adjustment, context_meta = family_context_adjustment(
                                connection, symbol, definition.family, regime
                            )
                            collective_cells = [
                                collective_memory_snapshot(
                                    connection, symbol, definition.family, regime, side
                                )
                                for side in (1, -1)
                            ]
                            collective_obs = sum(
                                safe_float(cell.get("observations")) for cell in collective_cells
                            )
                            if collective_obs > 0:
                                collective_win = sum(
                                    safe_float(cell.get("win_probability"), 0.50)
                                    * max(1e-9, safe_float(cell.get("observations")))
                                    for cell in collective_cells
                                ) / max(1e-9, collective_obs)
                                collective_reward = sum(
                                    safe_float(cell.get("reward_ewma"))
                                    * max(1e-9, safe_float(cell.get("observations")))
                                    for cell in collective_cells
                                ) / max(1e-9, collective_obs)
                            else:
                                collective_win = 0.50
                                collective_reward = 0.0
                            row = {
                                "execution_tier": tier,
                                "strategy_status": strategy_status,
                                "rank_for_current_regime": rank,
                                "strategy_id": definition.strategy_id,
                                "symbol": symbol,
                                "family": definition.family,
                                "current_regime": regime,
                                "selection_score": round(selection_score, 6),
                                "live_observations": int(live["observations"]) if live else 0,
                                "live_reward_mean": safe_float(live["reward_mean"]) if live else 0.0,
                                "adaptive_observations": int(adaptive_meta.get("observations") or 0),
                                "adaptive_confidence": safe_float(adaptive_meta.get("confidence"), 0.50),
                                "adaptive_loss_streak": int(adaptive_meta.get("loss_streak") or 0),
                                "adaptive_score_adjustment": safe_float(adaptive_adjustment),
                                "market_session": str(context_meta.get("session") or "unknown"),
                                "session_observations": round(safe_float(context_meta.get("session_observations")), 3),
                                "session_win_probability": safe_float(context_meta.get("session_win_probability"), 0.50),
                                "session_reward_ewma": safe_float(context_meta.get("session_reward_ewma")),
                                "collective_observations": round(collective_obs, 3),
                                "collective_win_probability": collective_win,
                                "collective_reward_ewma": collective_reward,
                                "params_json": json_text(definition.params),
                                "exported_at": utc_now(),
                            }
                            export_rows.append(row)
                            print(
                                f"  [{tier}] #{rank:02d} id={definition.strategy_id} "
                                f"status={strategy_status:<20} "
                                f"family={definition.family:<15} "
                                f"score={selection_score:8.3f} "
                                f"live_n={row['live_observations']} "
                                f"live_mean={row['live_reward_mean']:+.3f}R "
                                f"learn_n={row['adaptive_observations']} "
                                f"learn_conf={row['adaptive_confidence']:.2f} "
                                f"loss_streak={row['adaptive_loss_streak']} "
                                f"session={row['market_session']} "
                                f"session_n={row['session_observations']:.1f} "
                                f"session_win={row['session_win_probability']:.2f} "
                                f"collective_n={row['collective_observations']:.1f} "
                                f"collective_win={row['collective_win_probability']:.2f}"
                            )
                except Exception as error:
                    print(f"{symbol}: library/selector error: {error}")

            state_set(connection, "heartbeat:library_selector", utc_now())

        library_path = settings.REPORTS_DIR / "approved_strategy_library_current.csv"
        columns = [
            "execution_tier", "strategy_status", "rank_for_current_regime",
            "strategy_id", "symbol", "family", "current_regime",
            "selection_score", "live_observations", "live_reward_mean",
            "adaptive_observations", "adaptive_confidence",
            "adaptive_loss_streak", "adaptive_score_adjustment",
            "market_session", "session_observations",
            "session_win_probability", "session_reward_ewma",
            "collective_observations", "collective_win_probability",
            "collective_reward_ewma", "params_json", "exported_at",
        ]
        pd.DataFrame(export_rows, columns=columns).to_csv(library_path, index=False)
        print(f"\nAPPROVED/TRIAL LIBRARY SAVED: {library_path}")
    finally:
        mt5.shutdown()

def approved_library_engine(hours: float, interval: int) -> None:
    init_database()
    finish_at = _run_until(hours)
    cycle = 0
    print("=" * 76)
    print("SCREEN 4/5 - APPROVED STRATEGY LIBRARY + MARKET SELECTOR")
    print("Shows strict approved strategies plus the tiny-risk DEMO trial fallback.")
    print("The same tiered ranking is consumed by the DEMO executor.")
    print("=" * 76)
    try:
        while time.time() < finish_at:
            cycle += 1
            started = time.time()
            print(f"\n===== LIBRARY/SELECTOR CYCLE {cycle} | {utc_now()} =====")
            try:
                approved_library_cycle()
            except Exception as error:
                print(f"LIBRARY/SELECTOR ERROR: {error}")
                traceback.print_exc(limit=8)
            remaining = finish_at - time.time()
            if remaining <= 0:
                break
            elapsed = int(time.time() - started)
            time.sleep(min(max(10, interval - elapsed), remaining))
    except KeyboardInterrupt:
        print("\nApproved library/selector stopped by user")


# ============================================================
# CLI
# ============================================================


def research(count: int, limit: int | None) -> None:
    generate_strategies(count)
    backtest_pending(limit)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Trading Machine FINAL V5.5 SuperLearner: balanced parallel research, OOS validation, "
            "ensemble selection, paper testing and DEMO-only MT5 execution."
        )
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")

    history = sub.add_parser("history")
    history.add_argument("--bars", type=int, default=settings.HISTORY_BARS)

    generate = sub.add_parser("generate")
    generate.add_argument("--count", type=int, default=settings.INITIAL_RANDOM_STRATEGIES_PER_SYMBOL)

    backtest = sub.add_parser("backtest")
    backtest.add_argument("--limit", type=int, default=None)

    research_parser = sub.add_parser("research")
    research_parser.add_argument("--count", type=int, default=settings.INITIAL_RANDOM_STRATEGIES_PER_SYMBOL)
    research_parser.add_argument("--limit", type=int, default=settings.BACKTEST_BATCH_PER_SYMBOL)

    evolve = sub.add_parser("evolve")
    evolve.add_argument("--children", type=int, default=settings.CHILDREN_PER_PARENT)
    evolve.add_argument("--parents", type=int, default=settings.EVOLVE_PARENTS_PER_SYMBOL)

    paper = sub.add_parser("paper")
    paper.add_argument("--hours", type=float, default=0.5)
    paper.add_argument("--interval", type=int, default=10)

    demo = sub.add_parser("demo")
    demo.add_argument("--hours", type=float, default=12.0)
    demo.add_argument("--interval", type=float, default=float(getattr(settings, "V11_POLL_SECONDS", 0.5)))

    shadow = sub.add_parser("shadow")
    shadow.add_argument("--hours", type=float, default=12.0)
    shadow.add_argument("--interval", type=int, default=settings.SHADOW_CYCLE_SECONDS)

    generator_loop = sub.add_parser("generator-loop")
    generator_loop.add_argument("--hours", type=float, default=12.0)
    generator_loop.add_argument("--random", type=int, default=settings.INITIAL_RANDOM_STRATEGIES_PER_SYMBOL)
    generator_loop.add_argument("--guided", type=int, default=settings.LIVE_GUIDED_STRATEGIES_PER_SYMBOL)
    generator_loop.add_argument("--children", type=int, default=settings.CHILDREN_PER_PARENT)
    generator_loop.add_argument("--interval", type=int, default=settings.FACTORY_SLEEP_SECONDS)

    backtest_loop = sub.add_parser("backtest-loop")
    backtest_loop.add_argument("--hours", type=float, default=12.0)
    backtest_loop.add_argument("--limit", type=int, default=settings.BACKTEST_BATCH_PER_SYMBOL)
    backtest_loop.add_argument("--interval", type=int, default=15)

    library = sub.add_parser("library")
    library.add_argument("--hours", type=float, default=12.0)
    library.add_argument("--interval", type=int, default=30)

    factory = sub.add_parser("factory")
    factory.add_argument("--hours", type=float, default=12.0)
    factory.add_argument("--generate", type=int, default=settings.INITIAL_RANDOM_STRATEGIES_PER_SYMBOL)
    factory.add_argument("--limit", type=int, default=settings.BACKTEST_BATCH_PER_SYMBOL)
    factory.add_argument("--children", type=int, default=settings.CHILDREN_PER_PARENT)
    factory.add_argument("--sleep", type=int, default=settings.FACTORY_SLEEP_SECONDS)

    sub.add_parser("status")
    sub.add_parser("diagnose")
    sub.add_parser("verify-demo")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "init":
        init_database()
    elif args.command == "history":
        download_history(args.bars)
    elif args.command == "generate":
        generate_strategies(max(1, args.count))
    elif args.command == "backtest":
        backtest_pending(args.limit)
    elif args.command == "research":
        research(max(1, args.count), max(1, args.limit) if args.limit is not None else None)
    elif args.command == "evolve":
        evolve_top_strategies(max(1, args.children), max(1, args.parents))
    elif args.command == "paper":
        run_loop("paper", max(0.01, args.hours), max(2, args.interval))
    elif args.command == "demo":
        run_loop("demo", max(0.01, args.hours), max(0.05, args.interval))
    elif args.command == "shadow":
        run_loop("shadow", max(0.01, args.hours), max(5, args.interval))
    elif args.command == "generator-loop":
        strategy_generation_engine(
            max(0.01, args.hours), max(1, args.random), max(1, args.guided),
            max(1, args.children), max(5, args.interval),
        )
    elif args.command == "backtest-loop":
        historical_backtest_engine(
            max(0.01, args.hours), max(1, args.limit), max(5, args.interval)
        )
    elif args.command == "library":
        approved_library_engine(
            max(0.01, args.hours), max(10, args.interval)
        )
    elif args.command == "factory":
        strategy_factory(
            max(0.01, args.hours),
            max(1, args.generate),
            max(1, args.limit),
            max(1, args.children),
            max(5, args.sleep),
        )
    elif args.command == "status":
        status_report(write_file=True)
    elif args.command == "diagnose":
        diagnose_mt5()
    elif args.command == "verify-demo":
        verify_demo_connection()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"\nERROR: {error}")
        sys.exit(1)
