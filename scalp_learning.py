"""Additive V11 evidence store. Never rewrites historical learning tables."""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from scalp_policy import (POLICY_VERSION, ExitState, TradePlan, empirical_expectancy,
                          evaluate_quote, finite, net_virtual_reward, reward_summary)


def now_msc() -> int:
    return time.time_ns() // 1_000_000


def json_text(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), allow_nan=False, default=str)


def ensure_tables(connection: sqlite3.Connection) -> None:
    statements = [
        """CREATE TABLE IF NOT EXISTS v11_candidates(
            event_key TEXT PRIMARY KEY, created_msc INTEGER NOT NULL,
            bar_msc INTEGER NOT NULL, symbol TEXT NOT NULL, playbook TEXT NOT NULL,
            side INTEGER NOT NULL, regime TEXT NOT NULL, raw_score REAL NOT NULL,
            plan_json TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS v11_virtual_positions(
            id INTEGER PRIMARY KEY, event_key TEXT NOT NULL, symbol TEXT NOT NULL,
            playbook TEXT NOT NULL, side INTEGER NOT NULL, regime TEXT NOT NULL,
            variant TEXT NOT NULL, opened_msc INTEGER NOT NULL, closed_msc INTEGER,
            status TEXT NOT NULL, quality TEXT NOT NULL DEFAULT 'complete',
            plan_json TEXT NOT NULL, state_json TEXT NOT NULL,
            UNIQUE(event_key,variant))""",
        """CREATE INDEX IF NOT EXISTS idx_v11_virtual_open
            ON v11_virtual_positions(symbol,status)""",
        """CREATE TABLE IF NOT EXISTS v11_outcomes(
            id INTEGER PRIMARY KEY, event_key TEXT NOT NULL, version TEXT NOT NULL,
            variant TEXT NOT NULL, symbol TEXT NOT NULL, playbook TEXT NOT NULL,
            side INTEGER NOT NULL, regime TEXT NOT NULL, source TEXT NOT NULL,
            quality TEXT NOT NULL, opened_msc INTEGER NOT NULL, closed_msc INTEGER NOT NULL,
            reward_r REAL NOT NULL, pnl REAL, reason TEXT NOT NULL,
            commission_r REAL NOT NULL DEFAULT 0, slippage_r REAL NOT NULL DEFAULT 0,
            details_json TEXT NOT NULL DEFAULT '{}', UNIQUE(event_key,variant,source))""",
        """CREATE INDEX IF NOT EXISTS idx_v11_outcome_cell
            ON v11_outcomes(version,symbol,playbook,side,variant,closed_msc)""",
        """CREATE TABLE IF NOT EXISTS v11_policy_state(
            symbol TEXT NOT NULL, playbook TEXT NOT NULL, side INTEGER NOT NULL,
            active_variant TEXT NOT NULL DEFAULT 'balanced',
            last_evaluated_id INTEGER NOT NULL DEFAULT 0,
            updated_msc INTEGER NOT NULL, evidence_json TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY(symbol,playbook,side))""",
        """CREATE TABLE IF NOT EXISTS v11_decision_events(
            id INTEGER PRIMARY KEY, time_msc INTEGER NOT NULL, event_key TEXT,
            symbol TEXT NOT NULL, stage TEXT NOT NULL, reason TEXT NOT NULL,
            details_json TEXT NOT NULL DEFAULT '{}')""",
        """CREATE INDEX IF NOT EXISTS idx_v11_decision_time
            ON v11_decision_events(time_msc,symbol,stage)""",
        """CREATE TABLE IF NOT EXISTS v11_runtime_state(
            name TEXT PRIMARY KEY, updated_msc INTEGER NOT NULL, value_json TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS v11_probe_entries(
            event_key TEXT PRIMARY KEY, symbol TEXT NOT NULL, time_msc INTEGER NOT NULL,
            state TEXT NOT NULL, risk_cash REAL NOT NULL DEFAULT 0)""",
        """CREATE TABLE IF NOT EXISTS v11_order_intents(
            event_key TEXT PRIMARY KEY, symbol TEXT NOT NULL, side INTEGER NOT NULL,
            created_msc INTEGER NOT NULL, updated_msc INTEGER NOT NULL,
            state TEXT NOT NULL, broker_ticket INTEGER,
            request_json TEXT NOT NULL DEFAULT '{}', result_json TEXT NOT NULL DEFAULT '{}')""",
        """CREATE TABLE IF NOT EXISTS v11_policy_audits(
            id INTEGER PRIMARY KEY, time_msc INTEGER NOT NULL, symbol TEXT NOT NULL,
            playbook TEXT NOT NULL, side INTEGER NOT NULL, previous_variant TEXT NOT NULL,
            proposed_variant TEXT NOT NULL, promoted INTEGER NOT NULL, evidence_json TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS v11_luna_reviews(
            event_key TEXT PRIMARY KEY, symbol TEXT NOT NULL,
            created_msc INTEGER NOT NULL, updated_msc INTEGER NOT NULL,
            state TEXT NOT NULL, decision TEXT, approved INTEGER,
            confidence REAL, fingerprint TEXT, review_id INTEGER,
            snapshot_json TEXT NOT NULL DEFAULT '{}', response_json TEXT NOT NULL DEFAULT '{}',
            cache_json TEXT NOT NULL DEFAULT '{}', error_code TEXT)""",
        """CREATE INDEX IF NOT EXISTS idx_v11_luna_state
            ON v11_luna_reviews(state,updated_msc)""",
    ]
    # execute(), not executescript(): do not implicitly commit a caller's trade.
    for statement in statements:
        connection.execute(statement)


def event_key(symbol: str, playbook: str, side: int, bar_msc: int) -> str:
    return hashlib.sha256(f"{POLICY_VERSION}|{symbol}|{playbook}|{side}|{bar_msc}".encode()).hexdigest()[:24]


def set_state(connection: sqlite3.Connection, name: str, value: Any) -> None:
    connection.execute("INSERT INTO v11_runtime_state VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET updated_msc=excluded.updated_msc,value_json=excluded.value_json",
                       (name, now_msc(), json_text(value)))


def get_state(connection: sqlite3.Connection, name: str, default: Any = None) -> Any:
    row = connection.execute("SELECT value_json FROM v11_runtime_state WHERE name=?", (name,)).fetchone()
    try:
        return json.loads(row[0]) if row else default
    except (ValueError, TypeError):
        return default


def record_event(connection: sqlite3.Connection, symbol: str, stage: str, reason: str,
                 details: dict[str, Any] | None = None, key: str | None = None) -> None:
    connection.execute("INSERT INTO v11_decision_events(time_msc,event_key,symbol,stage,reason,details_json) VALUES(?,?,?,?,?,?)",
                       (now_msc(), key, symbol, stage, reason, json_text(details or {})))


def record_outcome(connection: sqlite3.Connection, *, key: str, plan: TradePlan,
                   regime: str, source: str, reward_r: float, closed_msc: int,
                   reason: str, quality: str = "complete", pnl: float | None = None,
                   details: dict[str, Any] | None = None) -> bool:
    if source not in {"broker", "shadow"} or not math.isfinite(float(reward_r)):
        raise ValueError("Invalid outcome source or reward")
    result = connection.execute("""INSERT OR IGNORE INTO v11_outcomes(
        event_key,version,variant,symbol,playbook,side,regime,source,quality,
        opened_msc,closed_msc,reward_r,pnl,reason,commission_r,slippage_r,details_json)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (key, plan.version, plan.variant, plan.symbol, plan.playbook, plan.side, regime,
         source, quality, plan.opened_msc, int(closed_msc), float(reward_r), pnl, reason,
         plan.commission_r, plan.slippage_r, json_text(details or {})))
    return result.rowcount > 0


def observe_pair(connection: sqlite3.Connection, *, key: str, plan: TradePlan,
                 regime: str, raw_score: float, bar_msc: int) -> bool:
    """All qualifying archetypes are observed, including while entries pause."""
    result = connection.execute("""INSERT OR IGNORE INTO v11_candidates
        VALUES(?,?,?,?,?,?,?,?,?)""", (key, plan.opened_msc, int(bar_msc), plan.symbol,
        plan.playbook, plan.side, regime, finite(raw_score), json_text(plan.to_dict())))
    if result.rowcount == 0:
        return False
    active = connection.execute("""SELECT 1 FROM v11_virtual_positions
        WHERE symbol=? AND playbook=? AND side=? AND status='open' LIMIT 1""",
        (plan.symbol, plan.playbook, plan.side)).fetchone()
    if active:
        return False  # avoid overlapping clones of the same market event
    for variant in ("balanced", "runner"):
        other = plan
        if variant != plan.variant:
            # Policy alternatives share the exact entry, stop, target and costs.
            rr = plan.target_r
            be_at = max(plan.commission_r + plan.slippage_r + 0.15,
                        min(0.85 if variant == "balanced" else 1.05, rr * 0.72))
            other = replace(plan, variant=variant, plan_id=f"{plan.plan_id}:{variant}",
                            be_at_r=be_at,
                            trail_at_r=max(be_at+0.15, min(1.15 if variant == "balanced" else 1.35, rr*0.88)),
                            trail_distance_r=0.60 if variant == "balanced" else 0.80)
        connection.execute("""INSERT OR IGNORE INTO v11_virtual_positions(
            event_key,symbol,playbook,side,regime,variant,opened_msc,status,plan_json,state_json)
            VALUES(?,?,?,?,?,?,?,'open',?,?)""", (key, plan.symbol, plan.playbook,
            plan.side, regime, variant, plan.opened_msc, json_text(other.to_dict()),
            json_text(ExitState.initial(other).to_dict())))
    return True


def invalidate_open_trials(connection: sqlite3.Connection, symbol: str, reason: str) -> int:
    result = connection.execute("""UPDATE v11_virtual_positions SET status='incomplete',
        quality=?,closed_msc=? WHERE symbol=? AND status='open'""", (reason, now_msc(), symbol))
    if result.rowcount:
        record_event(connection, symbol, "data_gap", reason, {"excluded_trials": result.rowcount})
    return result.rowcount


def replay_quotes(connection: sqlite3.Connection, symbol: str,
                  quotes: list[dict[str, Any]]) -> int:
    """Apply the ordered observed path; ambiguous/missing paths never train."""
    rows = connection.execute("SELECT * FROM v11_virtual_positions WHERE symbol=? AND status='open' ORDER BY id",
                              (symbol,)).fetchall()
    closed = 0
    for row in rows:
        plan = TradePlan.from_dict(json.loads(row["plan_json"]))
        state = ExitState(**json.loads(row["state_json"]))
        action = None
        for quote in quotes:
            if int(quote["time_msc"]) < plan.opened_msc:
                continue
            action = evaluate_quote(plan, state, bid=float(quote["bid"]), ask=float(quote["ask"]),
                                    time_msc=int(quote["time_msc"]), quote_flow=quote.get("quote_flow"),
                                    flow_samples=int(quote.get("flow_samples", 0)),
                                    allow_management=bool(quote.get("allow_management", True)))
            if action.action == "amend_stop":
                state.active_stop = action.new_stop
            elif action.action == "close":
                record_outcome(connection, key=row["event_key"], plan=plan,
                               regime=row["regime"], source="shadow",
                               reward_r=net_virtual_reward(plan, action),
                               closed_msc=int(quote["time_msc"]), reason=action.reason,
                               details={"mfe_r": state.mfe_r, "mae_r": state.mae_r})
                connection.execute("UPDATE v11_virtual_positions SET status='closed',closed_msc=?,state_json=? WHERE id=?",
                                   (int(quote["time_msc"]), json_text(state.to_dict()), row["id"]))
                closed += 1
                break
        if action is None or action.action != "close":
            connection.execute("UPDATE v11_virtual_positions SET state_json=? WHERE id=?",
                               (json_text(state.to_dict()), row["id"]))
    return closed


def evidence_for_plan(connection: sqlite3.Connection, plan: TradePlan, regime: str,
                      window: int = 200) -> dict[str, Any]:
    rows = connection.execute("""SELECT id,event_key,source,quality,reward_r,regime,
        commission_r,slippage_r,closed_msc FROM v11_outcomes
        WHERE version=? AND symbol=? AND playbook=? AND side=? AND variant=?
          AND quality='complete' ORDER BY closed_msc DESC,id DESC LIMIT ?""",
        (POLICY_VERSION, plan.symbol, plan.playbook, plan.side, plan.variant, max(20, int(window)))).fetchall()
    samples = [dict(row) for row in reversed(rows)]
    local = [row for row in samples if row["regime"] == regime]
    scope = "symbol_playbook_side"
    if len(local) >= 24:
        samples, scope = local, "symbol_playbook_side_regime"
    summary = empirical_expectancy(samples)
    # Training rewards are already net. Only a *higher* current cost estimate
    # warrants an extra reserve; charging historical fees twice is incorrect.
    old_cost = sum(finite(x["commission_r"]) + finite(x["slippage_r"]) for x in samples) / len(samples) if samples else 0.0
    cost_increase = max(0.0, plan.commission_r + plan.slippage_r - old_cost) if samples else 0.0
    summary["expected_net_r"] -= cost_increase
    summary.update(scope=scope, current_cost_increase_r=cost_increase,
                   policy_version=plan.version, variant=plan.variant)
    return summary


def active_variant(connection: sqlite3.Connection, symbol: str, playbook: str, side: int) -> str:
    row = connection.execute("SELECT active_variant FROM v11_policy_state WHERE symbol=? AND playbook=? AND side=?",
                             (symbol, playbook, int(side))).fetchone()
    return str(row[0]) if row and row[0] in {"balanced", "runner"} else "balanced"


def evaluate_variant(connection: sqlite3.Connection, symbol: str, playbook: str, side: int,
                     *, minimum_pairs: int = 60, minimum_days: int = 3,
                     margin_r: float = 0.05) -> dict[str, Any]:
    """Prospective paired tick paths + later holdout; no MFE/MAE proxy promotion."""
    rows = connection.execute("""SELECT b.event_key,b.id AS bid,r.id AS rid,
        b.closed_msc,b.reward_r AS balanced,r.reward_r AS runner
        FROM v11_outcomes b JOIN v11_outcomes r ON b.event_key=r.event_key
        WHERE b.version=? AND r.version=? AND b.symbol=? AND b.playbook=? AND b.side=?
          AND b.source='shadow' AND r.source='shadow'
          AND b.variant='balanced' AND r.variant='runner'
          AND b.quality='complete' AND r.quality='complete'
        ORDER BY b.closed_msc DESC,b.id DESC LIMIT 240""",
        (POLICY_VERSION, POLICY_VERSION, symbol, playbook, int(side))).fetchall()
    paired = [dict(row) for row in reversed(rows)]
    current = active_variant(connection, symbol, playbook, side)
    proposed = "runner" if current == "balanced" else "balanced"
    days = {datetime.fromtimestamp(row["closed_msc"]/1000.0, timezone.utc).date().isoformat() for row in paired}
    evidence: dict[str, Any] = {"pairs": len(paired), "days": len(days), "promoted": False,
                                "active_variant": current, "proposed_variant": proposed,
                                "reason": "collecting_prospective_tick_evidence"}
    if len(paired) < minimum_pairs or len(days) < minimum_days:
        return evidence
    cut = len(paired) * 2 // 3
    train, holdout = paired[:cut], paired[cut:]
    train_diff = reward_summary([row[proposed]-row[current] for row in train])
    holdout_diff = reward_summary([row[proposed]-row[current] for row in holdout])
    holdout_new = reward_summary([row[proposed] for row in holdout])
    lower_margin = holdout_diff["mean_r"] - 1.64 * (holdout_diff["standard_error"] or 0.0)
    supported = bool(train_diff["mean_r"] >= margin_r and lower_margin >= margin_r
                     and holdout_new["mean_r"] > 0.05
                     and (holdout_new["profit_factor"] is None or holdout_new["profit_factor"] >= 1.10)
                     and holdout_new["max_drawdown_r"] <= 6.0)
    evidence.update(train=train_diff, holdout_difference=holdout_diff,
                    holdout_proposed=holdout_new, lower_margin_r=lower_margin,
                    reason="holdout_pass" if supported else "holdout_did_not_support_change")
    latest_id = max(max(row["bid"], row["rid"]) for row in paired)
    old = connection.execute("SELECT last_evaluated_id FROM v11_policy_state WHERE symbol=? AND playbook=? AND side=?",
                             (symbol, playbook, int(side))).fetchone()
    if old and latest_id <= int(old[0]):
        return evidence
    if old and sum(min(row["bid"], row["rid"]) > int(old[0]) for row in paired) < 20:
        evidence["reason"] = "waiting_for_20_new_pairs_before_retesting"
        return evidence
    # Never repeat a policy mutation using exactly the same evidence.
    changed = proposed if supported else current
    evidence["promoted"] = supported
    evidence["active_variant"] = changed
    connection.execute("""INSERT INTO v11_policy_state VALUES(?,?,?,?,?,?,?)
        ON CONFLICT(symbol,playbook,side) DO UPDATE SET active_variant=excluded.active_variant,
        last_evaluated_id=excluded.last_evaluated_id,updated_msc=excluded.updated_msc,evidence_json=excluded.evidence_json""",
        (symbol, playbook, int(side), changed, latest_id, now_msc(), json_text(evidence)))
    connection.execute("""INSERT INTO v11_policy_audits(time_msc,symbol,playbook,side,
        previous_variant,proposed_variant,promoted,evidence_json) VALUES(?,?,?,?,?,?,?,?)""",
        (now_msc(), symbol, playbook, int(side), current, proposed, int(supported), json_text(evidence)))
    return evidence


def probe_capacity(connection: sqlite3.Connection, symbol: str, *, timestamp_msc: int,
                   per_symbol: int, total_cap: int) -> tuple[bool, dict[str, int]]:
    stamp = datetime.fromtimestamp(timestamp_msc/1000.0, timezone.utc)
    start = int(stamp.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()*1000)
    rows = connection.execute("""SELECT symbol,COUNT(*) FROM v11_probe_entries
        WHERE time_msc>=? AND state IN ('reserved','sent','unknown') GROUP BY symbol""", (start,)).fetchall()
    total = sum(int(row[1]) for row in rows)
    local = sum(int(row[1]) for row in rows if row[0] == symbol)
    return (total < total_cap and local < per_symbol), dict(total=total, symbol=local,
                                                           total_cap=total_cap, symbol_cap=per_symbol)


def update_probe(connection: sqlite3.Connection, key: str, symbol: str, state: str,
                 risk_cash: float = 0.0) -> None:
    connection.execute("""INSERT INTO v11_probe_entries VALUES(?,?,?,?,?)
        ON CONFLICT(event_key) DO UPDATE SET state=excluded.state,risk_cash=excluded.risk_cash""",
        (key, symbol, now_msc(), state, finite(risk_cash)))
