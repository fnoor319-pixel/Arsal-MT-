"""V11 DEMO executor: continuous observation, bounded probes, one exit policy.

One process owns MT5. No LLM/network call runs on the execution thread. Historical
tables are retained; only version-matched, chronological outcomes price an edge.
"""
from __future__ import annotations

import atexit
import json
import os
import statistics
import time
from collections import Counter, deque
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pandas as pd
import settings
import scalp_learning as learning
import scalp_advisor
from scalp_execution import plain, quote_problem, quote_time, send_checked_deal
from scalp_policy import ExitAction, ExitState, TradePlan, clamp, evaluate_quote, finite, make_plan


TIER = "v9_v11_canonical"  # v9 prefix deliberately preserves existing streak guards


def cfg(name: str, default: Any) -> Any:
    return getattr(settings, name, default)


def source_playbook(setup: Any) -> str:
    source = "expert" if getattr(setup, "v10_synthetic", False) else "hunter"
    return f"{source}:{setup.playbook}"


def plan_for(setup: Any, tick: Any, info: Any, atr: float, variant: str,
             commission_r: float, slippage_r: float) -> TradePlan:
    point = finite(getattr(info, "point", 0), 0.00001)
    step = finite(getattr(info, "trade_tick_size", 0)) or point
    distance = max(int(getattr(info, "trade_stops_level", 0)),
                   int(getattr(info, "trade_freeze_level", 0))) * point + step
    return make_plan(symbol=setup.symbol, playbook=source_playbook(setup), side=int(setup.side),
                     entry=float(tick.ask if setup.side == 1 else tick.bid), atr=atr,
                     stop_atr=float(setup.stop_atr), take_atr=float(setup.take_atr),
                     max_hold_bars=int(setup.max_hold_bars), opened_msc=quote_time(tick),
                     price_step=step, min_stop_distance=distance,
                     spread=float(tick.ask-tick.bid), commission_r=commission_r,
                     slippage_r=slippage_r, variant=variant)


def economic_mode(evidence: dict[str, Any]) -> tuple[str, str]:
    """Raw signal scores are NOT calibrated probabilities or returns."""
    n = int(evidence.get("unique_events", 0))
    er = finite(evidence.get("expected_net_r"))
    mass = finite(evidence.get("effective_n"))
    broker_n = int(evidence.get("broker_n", 0))
    if n >= int(cfg("V11_NEGATIVE_EVIDENCE_MIN_EVENTS", 12)) and er <= float(cfg("V11_NEGATIVE_EVIDENCE_R", -0.15)):
        return "shadow_only", "negative_same_policy_net_evidence"
    if (n >= int(cfg("V11_ESTABLISHED_MIN_EVENTS", 24))
            and mass >= float(cfg("V11_ESTABLISHED_MIN_EFFECTIVE_N", 8.0))
            and broker_n >= int(cfg("V11_ESTABLISHED_MIN_BROKER_TRADES", 5))
            and er >= float(cfg("V11_MIN_EXPECTED_NET_R", 0.05))):
        return "evidence_entry", "positive_version_matched_net_evidence"
    return "demo_probe", "unproven_policy_bounded_demo_exploration"


def rank_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Economic ordering with deterministic fall-through to viable alternatives."""
    eligible = [c for c in candidates if c["mode"] != "shadow_only" and not c.get("consumed")]
    established = sorted(
        (c for c in eligible if c["mode"] == "evidence_entry"),
        key=lambda c: (-finite(c["evidence"]["expected_net_r"]), c["key"]),
    )
    # Balance cold-start coverage; do not compare Hunter's 90 with Expert's 90.
    probes = sorted(
        (c for c in eligible if c["mode"] != "evidence_entry"),
        key=lambda c: (finite(c["evidence"]["effective_n"]), c["key"]),
    )
    return established + probes


def choose_candidate(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    ranked = rank_candidates(candidates)
    return ranked[0] if ranked else None


class ExecutorLock:
    """Advisory OS lock held for this executor's lifetime, released on crashes."""
    def __init__(self, path: Path):
        self.handle = path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self.handle.seek(0, 2)
                if self.handle.tell() == 0:
                    self.handle.write(b"0")
                    self.handle.flush()
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, IOError):
            self.handle.close()
            raise RuntimeError("Another V11 executor owns this folder; no second executor allowed") from None

    def close(self) -> None:
        if not self.handle.closed:
            self.handle.close()


class Runtime:
    def __init__(self, tm: Any, *, acquire_lock: bool = True):
        self.tm = tm
        self.broker = tm.mt5
        self.path = str(Path(settings.DATABASE_PATH).resolve())
        self.lock = ExecutorLock(Path(self.path).parent / "executor_v11.lock") if acquire_lock else None
        self.frames: dict[str, dict[str, Any]] = {}
        self.candidates: dict[str, list[dict[str, Any]]] = {}
        self.flow: dict[str, deque[float]] = {}
        self.mids: dict[str, float] = {}
        self.costs: dict[str, tuple[float, float, float]] = {}
        self.intent_checked: dict[str, float] = {}
        self.throttled: dict[tuple[str, str, str], float] = {}
        self.loop_ms: deque[float] = deque(maxlen=240)
        self.last_maintenance = self.last_status = 0.0
        self.initialized = False
        atexit.register(self.close)

    def close(self) -> None:
        if self.lock:
            self.lock.close()

    def event(self, con: Any, symbol: str, stage: str, reason: str,
              details: dict[str, Any] | None = None, key: str | None = None) -> None:
        stamp = time.monotonic()
        token = (symbol, stage, reason)
        if stamp-self.throttled.get(token, -1e9) < 30:
            return
        self.throttled[token] = stamp
        learning.record_event(con, symbol, stage, reason, details, key)
        self.tm.scalp_diag.record(symbol, f"v11_{stage}", reason)

    def cost_reserve(self, con: Any, symbol: str, stop_atr: float = .65) -> tuple[float, float]:
        cached = self.costs.get(symbol)
        if cached and time.monotonic()-cached[0] < 300:
            commission, slip_atr = cached[1], cached[2]
            slip = max(float(cfg("V11_MIN_SLIPPAGE_RESERVE_R", 0.03)),
                       slip_atr/max(.35, abs(finite(stop_atr, .65)))
                       * float(cfg("V11_SLIPPAGE_ROUND_TRIP_MULTIPLIER", 2.0)))
            return commission, slip
        fees: list[float] = []
        # Legacy broker costs are useful execution estimates, NOT policy labels.
        rows = con.execute("""SELECT e.details_json,p.risk_cash FROM rl_reward_events e
            JOIN demo_positions p ON p.id=json_extract(e.details_json,'$.demo_position_id')
            WHERE e.symbol=? AND e.source='demo_trade' ORDER BY e.id DESC LIMIT 80""", (symbol,)).fetchall()
        for row in rows:
            risk = finite(row["risk_cash"])
            try:
                deals = json.loads(row["details_json"]).get("deals", [])
                paid = sum(max(0.0, -sum(finite(d.get(k)) for k in ("commission", "fee", "swap"))) for d in deals)
                if risk > 0:
                    fees.append(paid/risk)
            except (ValueError, TypeError, AttributeError):
                continue
        fallback = float(cfg("V11_UNKNOWN_COMMISSION_RESERVE_R", 0.03))
        commission = max(fallback, statistics.median(fees) if fees else fallback)
        slip_rows = con.execute("SELECT slippage_atr FROM execution_quality WHERE symbol=? ORDER BY id DESC LIMIT 100", (symbol,)).fetchall()
        slips = sorted(abs(finite(r[0])) for r in slip_rows)
        slip_atr = slips[int((len(slips)-1)*0.9)] if slips else 0.0
        # execution_quality stores entry slippage in ATR units. Convert its P90
        # to R using this setup's real stop distance, with a round-trip reserve.
        slip = max(float(cfg("V11_MIN_SLIPPAGE_RESERVE_R", 0.03)),
                   slip_atr/max(.35, abs(finite(stop_atr, .65)))
                   * float(cfg("V11_SLIPPAGE_ROUND_TRIP_MULTIPLIER", 2.0)))
        self.costs[symbol] = (time.monotonic(), commission, slip_atr)
        return commission, slip

    def frame(self, symbol: str) -> Any:
        now = time.monotonic()
        cached = self.frames.get(symbol)
        if cached and now-cached["fetched"] < float(cfg("V11_BAR_REFRESH_SECONDS", 2.0)):
            return cached["frame"]
        # One bounded, read-only request; no multi-second retry loop in the hot path.
        rates = self.broker.copy_rates_from_pos(symbol, settings.TIMEFRAME, 0, int(cfg("V11_FEATURE_BARS", 360)))
        if rates is None or len(rates) < 220:
            raise RuntimeError("M1_history_unavailable_or_too_short")
        raw = pd.DataFrame(rates)
        raw["time"] = pd.to_datetime(raw["time"], unit="s", utc=True)
        raw = raw.sort_values("time").drop_duplicates("time").reset_index(drop=True)
        closed = int(pd.Timestamp(raw.iloc[-2]["time"]).timestamp()*1000)
        if cached and closed == cached["bar"]:
            featured = cached["frame"]  # experts only consume closed bars
        else:
            featured = self.tm.add_features(raw, {5, 13, 20, 50, 200})
        self.frames[symbol] = dict(frame=featured, fetched=now, bar=closed)
        return featured

    def replay(self, con: Any, symbol: str, tick: Any) -> None:
        cursor = learning.get_state(con, f"ticks:{symbol}", {})
        end = quote_time(tick)
        current_signature = f"{finite(tick.bid):.12g}|{finite(tick.ask):.12g}"
        if not isinstance(cursor, dict) or "time_msc" not in cursor:
            # The live snapshot is a trustworthy anchor, but no path precedes it.
            learning.set_state(con, f"ticks:{symbol}",
                               dict(time_msc=end, boundary=[current_signature]))
            learning.set_state(con, f"tick_health:{symbol}",
                               dict(status="anchored", cursor_msc=end, target_msc=end, lag_ms=0, rows=0))
            self.mids[symbol] = (finite(tick.bid)+finite(tick.ask))/2
            return
        start = int(cursor.get("time_msc", end))
        if start > end:
            self.event(con, symbol, "data", "tick_clock_regression",
                       {"cursor_msc": start, "target_msc": end, "delta_ms": end-start})
            learning.set_state(con, f"tick_health:{symbol}",
                               dict(status="clock_regression", cursor_msc=start, target_msc=end, lag_ms=end-start, rows=0))
            return
        max_gap = float(cfg("V11_MAX_TICK_CATCHUP_SECONDS", 120)) * 1000
        if end-start > max_gap:
            learning.invalidate_open_trials(con, symbol, "tick_catchup_limit_exceeded")
            self.flow.pop(symbol, None)
            self.mids[symbol] = (finite(tick.bid)+finite(tick.ask))/2
            learning.set_state(con, f"ticks:{symbol}",
                               dict(time_msc=end, boundary=[current_signature]))
            learning.set_state(con, f"tick_health:{symbol}",
                               dict(status="catchup_limit_reset", cursor_msc=end, target_msc=end,
                                    lag_ms=0, rows=0, discarded_gap_ms=end-start))
            return
        # Retain duplicate multiplicities at the boundary, including equal-ms ticks.
        prior_counts = Counter(cursor.get("boundary", []))
        try:
            data = self.broker.copy_ticks_range(symbol,
                datetime.fromtimestamp(start/1000, timezone.utc),
                datetime.fromtimestamp(end/1000, timezone.utc),
                int(getattr(self.broker, "COPY_TICKS_ALL", -1)))
        except Exception:
            data = None
        if data is None or len(data) == 0:
            if end > start:
                self.event(con, symbol, "data_gap", "tick_history_retry_pending",
                           {"cursor_msc": start, "target_msc": end, "lag_ms": end-start})
            learning.set_state(con, f"tick_health:{symbol}",
                               dict(status="retry_pending", cursor_msc=start, target_msc=end,
                                    lag_ms=end-start, rows=0))
            return
        names = set(data.dtype.names or ()) if hasattr(data, "dtype") else set(data[0].keys())
        rows = []
        seen = Counter()
        boundary: list[str] = []
        saw_inrange = False
        last = start
        structural_error = ""
        for value in data:
            stamp = int(value["time_msc"]) if "time_msc" in names else int(value["time"])*1000
            bid, ask = finite(value["bid"]), finite(value["ask"])
            if stamp < start:
                continue
            if stamp > end:
                continue
            if stamp < last or bid <= 0 or ask < bid:
                structural_error = "incomplete_or_unordered_tick_history"
                break
            signature = f"{bid:.12g}|{ask:.12g}"
            if not saw_inrange or stamp > last:
                last = stamp
                boundary = [signature]
            elif stamp == last:
                boundary.append(signature)
            saw_inrange = True
            if stamp == start:
                seen[signature] += 1
                if seen[signature] <= prior_counts[signature]:
                    continue
            mid = (bid+ask)/2
            previous = self.mids.get(symbol, mid)
            flow = self.flow.setdefault(symbol, deque(maxlen=32))
            if mid != previous:
                flow.append(1.0 if mid > previous else -1.0)
            self.mids[symbol] = mid
            rows.append(dict(time_msc=stamp, bid=bid, ask=ask,
                             quote_flow=sum(flow)/len(flow) if flow else None,
                             flow_samples=len(flow), allow_management=False))
        if any(seen[signature] < count for signature, count in prior_counts.items()):
            structural_error = "incomplete_or_unordered_tick_history"
        if structural_error:
            learning.invalidate_open_trials(con, symbol, structural_error)
            self.flow.pop(symbol, None)
            self.mids[symbol] = (finite(tick.bid)+finite(tick.ask))/2
            learning.set_state(con, f"ticks:{symbol}",
                               dict(time_msc=end, boundary=[current_signature]))
            learning.set_state(con, f"tick_health:{symbol}",
                               dict(status="invalid_path_reset", cursor_msc=end, target_msc=end,
                                    lag_ms=0, rows=len(rows)))
            return

        if not saw_inrange:
            boundary = list(cursor.get("boundary", []))
        caught_up = last >= end
        if rows:
            # SL/TP checks use every ordered quote. Client amendments only at
            # this real poll, not unrealistically on every historical quote.
            rows[-1]["allow_management"] = caught_up
            learning.replay_quotes(con, symbol, rows)
        # Never jump the durable cursor to a snapshot that tick history has not
        # returned. A temporarily lagging terminal can catch up next poll.
        learning.set_state(con, f"ticks:{symbol}", dict(time_msc=last, boundary=boundary))
        status = "caught_up" if caught_up else "catchup_pending"
        learning.set_state(con, f"tick_health:{symbol}",
                           dict(status=status, cursor_msc=last, target_msc=end,
                                lag_ms=end-last, rows=len(rows)))
        if not caught_up:
            self.event(con, symbol, "data_gap", "tick_history_catchup_pending",
                       {"cursor_msc": last, "target_msc": end, "lag_ms": end-last,
                        "replayed_rows": len(rows)})

    def observe(self, con: Any, symbol: str, tick: Any, info: Any, *, replayed: bool = False) -> None:
        if not replayed:
            self.replay(con, symbol, tick)
        frame = self.frame(symbol)
        bar = int(pd.Timestamp(frame.iloc[-2]["time"]).timestamp()*1000)
        if abs(quote_time(tick)-(bar+60000)) > float(cfg("V11_MAX_BAR_AGE_SECONDS", 120))*1000:
            self.candidates[symbol] = []
            self.event(con, symbol, "data", "stale_M1_history")
            return
        regime = self.tm.detect_regime(frame)
        atr = finite(frame.iloc[-2].get("atr_14"))
        if atr <= 0:
            return
        setups = []
        new_bar = int(learning.get_state(con, f"scan:{symbol}", 0)) != bar
        if new_bar:
            # Observe all triggered experts, not only the raw-score winner.
            raw_experts, context = self.tm.v10._score_components(frame, None)
            for item in raw_experts:
                if finite(item.get("score")) < float(cfg("V10_EXPERT_TRIGGER_MIN_SCORE", 72)):
                    continue
                setups.append(self.tm.v10.V10Setup(symbol=symbol, playbook=str(item["playbook"]),
                    side=int(item["side"]), score=finite(item["score"]),
                    stop_atr=finite(item.get("stop_atr"), .65), take_atr=finite(item.get("take_atr"), 1.15),
                    max_hold_bars=int(item.get("max_hold_bars", 5)),
                    context={**context, "source": "v11_expert_candidates", "top_reason": item.get("why", {})}))
            hunter = self.tm.micro_hunter.observe_closed_bar(con, symbol, frame, regime, None)
            if hunter is not None:
                setups.append(hunter)
            learning.set_state(con, f"scan:{symbol}", bar)
            self.candidates[symbol] = []
            self.event(con, symbol, "scan", "new_closed_bar_observed", {"triggered_candidates": len(setups), "bar_msc": bar})
        else:
            hunter = self.tm.micro_hunter.intrabar_trigger(con, symbol, frame, tick)
            if hunter is not None:
                setups.append(hunter)
        existing = self.candidates.setdefault(symbol, [])
        for setup in setups:
            pb = source_playbook(setup)
            candidate_bar = bar
            if getattr(setup, "bar_time", None):
                candidate_bar = int(pd.Timestamp(setup.bar_time).timestamp()*1000)
            key = learning.event_key(symbol, pb, int(setup.side), candidate_bar)
            if any(c["key"] == key for c in existing):
                continue
            variant = learning.active_variant(con, symbol, pb, int(setup.side))
            fees, slip = self.cost_reserve(con, symbol, float(setup.stop_atr))
            plan = plan_for(setup, tick, info, atr, variant, fees, slip)
            learning.observe_pair(con, key=key, plan=plan, regime=regime, raw_score=float(setup.score), bar_msc=candidate_bar)
            evidence = learning.evidence_for_plan(con, plan, regime)
            mode, reason = economic_mode(evidence)
            existing.append(dict(key=key, setup=setup, plan=plan, evidence=evidence, mode=mode,
                                 regime=regime, bar_msc=candidate_bar, created_msc=learning.now_msc()))
            learning.record_event(con, symbol, "candidate", reason,
                                  {"raw_score_not_probability": float(setup.score), "evidence": evidence,
                                   "playbook": pb, "mode": mode}, key)
            learning.evaluate_variant(con, symbol, pb, int(setup.side),
                minimum_pairs=int(cfg("V11_POLICY_MIN_PAIRS", 60)), minimum_days=int(cfg("V11_POLICY_MIN_DAYS", 3)))
        expiry = learning.now_msc()-float(cfg("V11_CANDIDATE_TTL_SECONDS", 20))*1000
        self.candidates[symbol] = [c for c in existing if c["created_msc"] >= expiry]

    def bind_intents(self, con: Any, all_positions: list[Any]) -> None:
        pending = con.execute("SELECT * FROM v11_order_intents WHERE state IN ('reserved','sent','unknown') ORDER BY created_msc").fetchall()
        if not pending:
            return
        for intent in pending:
            check_time = time.monotonic()
            if check_time-self.intent_checked.get(intent["event_key"], -1e9) < 2:
                continue
            self.intent_checked[intent["event_key"]] = check_time
            payload = json.loads(intent["request_json"])
            request, context = payload["request"], payload["context"]
            comment = request["comment"]
            found = [p for p in all_positions if str(getattr(p, "comment", "")) == comment
                     and str(getattr(p, "symbol", "")) == intent["symbol"]
                     and int(getattr(p, "magic", -1)) == settings.MAGIC_NUMBER]
            position = found[0] if len(found) == 1 else None
            if position is None:
                # A fast SL/TP may close the trade before positions_get sees it.
                start = datetime.fromtimestamp((int(intent["created_msc"])-60000)/1000, timezone.utc)
                try:
                    history = self.broker.history_deals_get(start, datetime.now(timezone.utc))
                except Exception:
                    history = None
                ack = json.loads(intent["result_json"] or "{}").get("result", {})
                order_id = int(ack.get("order", 0) or 0)
                entries = [d for d in (history or ()) if int(getattr(d, "magic", -1)) == settings.MAGIC_NUMBER
                           and str(getattr(d, "symbol", "")) == intent["symbol"]
                           and int(getattr(d, "entry", -1)) == int(getattr(self.broker, "DEAL_ENTRY_IN", 0))
                           and (str(getattr(d, "comment", "")) == comment or (order_id > 0 and int(getattr(d, "order", 0)) == order_id))]
                if entries:
                    ids = {int(getattr(d, "position_id", 0)) for d in entries}
                    if len(ids) == 1 and next(iter(ids)) > 0:
                        ticket = next(iter(ids))
                        actual = [p for p in all_positions if int(getattr(p, "identifier", getattr(p, "ticket", 0))) == ticket]
                        vol = sum(finite(getattr(d, "volume", 0)) for d in entries)
                        price = sum(finite(getattr(d, "volume", 0))*finite(getattr(d, "price", 0)) for d in entries)/max(vol, 1e-12)
                        position = actual[0] if actual else SimpleNamespace(ticket=ticket, volume=vol, price_open=price,
                            time_msc=min(int(getattr(d, "time_msc", 0)) for d in entries), sl=request["sl"], tp=request["tp"])
                elif history is not None and order_id > 0:
                    try:
                        orders = self.broker.history_orders_get(ticket=order_id)
                    except Exception:
                        orders = None
                    terminal_states = {int(getattr(self.broker, "ORDER_STATE_CANCELED", 2)),
                                       int(getattr(self.broker, "ORDER_STATE_REJECTED", 5)),
                                       int(getattr(self.broker, "ORDER_STATE_EXPIRED", 6))}
                    # Empty history alone proves nothing. A specific terminal
                    # order with no executed volume can safely release the slot.
                    if orders and len(orders) == 1 and int(orders[0].ticket) == order_id:
                        order = orders[0]
                        if (int(order.state) in terminal_states
                                and abs(finite(order.volume_initial)-finite(order.volume_current)) < 1e-9):
                            con.execute("UPDATE v11_order_intents SET state='rejected',updated_msc=? WHERE event_key=?",
                                        (learning.now_msc(), intent["event_key"]))
                            if payload.get("probe"):
                                learning.update_probe(con, intent["event_key"], intent["symbol"], "rejected")
                            self.event(con, intent["symbol"], "order_reconciled", "confirmed_unfilled_terminal_order", {"order": order_id})
                            continue
            if position is None:
                self.event(con, intent["symbol"], "order_pending", "awaiting_definitive_broker_reconciliation",
                           {"event_key": intent["event_key"], "state": intent["state"]})
                continue
            ticket = int(position.ticket)
            row = con.execute("SELECT id FROM demo_positions WHERE position_ticket=?", (ticket,)).fetchone()
            if row is None:
                planned = TradePlan.from_dict(context["v11"]["plan"])
                filled = float(position.price_open)
                filled_risk = abs(filled-planned.stop)
                valid = planned.side*(filled-planned.stop) > 0 and planned.side*(planned.take-filled) > 0
                if not valid:
                    self.event(con, intent["symbol"], "order_pending", "fill_outside_planned_bracket_manual_check_required", {"ticket": ticket})
                    continue
                actual_plan = replace(planned, entry=filled, risk_distance=filled_risk,
                                      opened_msc=int(getattr(position, "time_msc", 0) or planned.opened_msc))
                risk = self.tm.position_risk_cash(planned.symbol, planned.side, float(position.volume), filled, planned.stop)
                if risk <= 0:
                    self.event(con, intent["symbol"], "order_pending", "filled_risk_calculation_unavailable", {"ticket": ticket})
                    continue
                context["v11"]["plan"] = actual_plan.to_dict()
                context["v11"]["state"] = ExitState.initial(actual_plan).to_dict()
                context["v11"]["actual_fill_price"] = filled
                context["actual_risk_pct"] = risk/max(finite(payload.get("equity")), 1e-12)
                strategy = self.tm.load_strategy(con.execute("SELECT id,symbol,family,params_json FROM strategies WHERE id=?", (payload["strategy_id"],)).fetchone())
                self.tm.register_demo_position(con, ticket, strategy, payload["regime"], planned.side,
                    float(position.volume), filled, planned.stop, planned.take, risk,
                    SimpleNamespace(order=ticket), TIER, context, 0.5, payload["risk_multiplier"],
                    super_probability=finite(context["v11"]["evidence"].get("net_win_probability"), .5))
                opened = datetime.fromtimestamp(actual_plan.opened_msc/1000, timezone.utc).isoformat()
                con.execute("UPDATE demo_positions SET opened_at=? WHERE position_ticket=? AND status='open'", (opened, ticket))
            con.execute("UPDATE v11_order_intents SET state='bound',broker_ticket=?,updated_msc=? WHERE event_key=?",
                        (ticket, learning.now_msc(), intent["event_key"]))
            self.tm.state_set(con, f"last_order:{intent['symbol']}", self.tm.utc_now())
            if payload.get("probe"):
                learning.update_probe(con, intent["event_key"], intent["symbol"], "sent", finite(payload.get("risk_cash")))
            self.event(con, intent["symbol"], "order_bound", "actual_fill_registered", {"ticket": ticket}, intent["event_key"])

    def manage_position(self, con: Any, row: Any, position: Any, context: dict[str, Any]) -> None:
        plan = TradePlan.from_dict(context["v11"]["plan"])
        state = ExitState(**context["v11"].get("state", ExitState.initial(plan).to_dict()))
        tick = self.broker.symbol_info_tick(plan.symbol)
        if quote_problem(tick, now_msc=learning.now_msc(), max_age_seconds=float(cfg("SPARTAN_MAX_TICK_AGE_SECONDS", 10))):
            self.event(con, plan.symbol, "exit_wait", "fresh_quote_required_for_client_exit")
            return  # server-side SL/TP remains installed
        server_sl = finite(getattr(position, "sl", 0))
        if float(position.volume) > finite(row["volume"]) + 1e-9:
            # A broker may acknowledge a partial entry before completing it.
            # Incorporate later entry fills into the original cash-risk basis;
            # do NOT shrink that basis after an exit/partial close.
            filled = float(position.price_open)
            if plan.side*(filled-plan.stop) > 0 and plan.side*(plan.take-filled) > 0:
                plan = replace(plan, entry=filled, risk_distance=abs(filled-plan.stop))
                risk = self.tm.position_risk_cash(plan.symbol, plan.side, float(position.volume), filled, plan.stop)
                equity_at_entry = finite(context["v11"].get("equity_at_entry"))
                if risk > 0:
                    con.execute("UPDATE demo_positions SET volume=?,entry_price=?,risk_cash=? WHERE id=?",
                        (float(position.volume), filled, risk, row["id"]))
                    context["v11"]["plan"] = plan.to_dict()
                    if equity_at_entry > 0:
                        context["actual_risk_pct"] = risk/equity_at_entry
        if server_sl > 0:
            state.active_stop = server_sl  # broker acknowledgement, not a wish
        flow = self.flow.get(plan.symbol, ())
        action = evaluate_quote(plan, state, bid=float(tick.bid), ask=float(tick.ask), time_msc=quote_time(tick),
            now_msc=learning.now_msc(), quote_flow=sum(flow)/len(flow) if flow else None, flow_samples=len(flow))
        sizing = context["v11"].get("sizing", {})
        if server_sl <= 0:
            action = ExitAction("close", "missing_broker_protective_stop")
        elif plan.side*(server_sl-plan.stop) < -plan.price_step*.5:
            action = ExitAction("close", "broker_stop_widened_beyond_plan")
        elif finite(context.get("actual_risk_pct")) > finite(sizing.get("hard_ceiling_pct"), 1.0) + 1e-9:
            action = ExitAction("close", "fill_slippage_exceeded_risk_ceiling")
        context["v11"]["state"] = state.to_dict()
        con.execute("UPDATE demo_positions SET context_json=?,mfe_r=?,mae_r=? WHERE id=?",
                    (learning.json_text(context), state.mfe_r, state.mae_r, row["id"]))
        if action.action == "hold":
            return
        ticket = int(position.ticket)
        if action.action == "amend_stop":
            request = dict(action=self.broker.TRADE_ACTION_SLTP, symbol=plan.symbol, position=ticket,
                           sl=action.new_stop, tp=float(getattr(position, "tp", plan.take) or plan.take), magic=settings.MAGIC_NUMBER)
            con.commit()
            try:
                result = self.broker.order_send(request)
            except Exception:
                result = None
            if result is not None and int(getattr(result, "retcode", -1)) == int(getattr(self.broker, "TRADE_RETCODE_DONE", 10009)):
                state.active_stop = action.new_stop
                context["v11"]["state"] = state.to_dict()
                con.execute("UPDATE demo_positions SET context_json=? WHERE id=?", (learning.json_text(context), row["id"]))
            else:
                self.event(con, plan.symbol, "stop_amend", "amendment_not_acknowledged", {"ticket": ticket})
            return
        pending = learning.get_state(con, f"close:{ticket}", {})
        if pending.get("state") in {"reserved", "unknown", "sent"}:
            orders = self.broker.orders_get(symbol=plan.symbol)
            if (pending.get("state") in {"reserved", "unknown"} or orders is None or len(orders)
                    or float(position.volume) >= finite(pending.get("volume")) - 1e-9):
                self.event(con, plan.symbol, "exit_wait", "previous_close_awaiting_broker_confirmation", {"ticket": ticket})
                return
        elif learning.now_msc()-int(pending.get("time_msc", 0)) < 2000:
            return
        request = dict(action=self.broker.TRADE_ACTION_DEAL, symbol=plan.symbol, position=ticket,
                       volume=float(position.volume), type=self.broker.ORDER_TYPE_SELL if plan.side == 1 else self.broker.ORDER_TYPE_BUY,
                       price=float(tick.bid if plan.side == 1 else tick.ask), deviation=settings.DEFAULT_DEVIATION_POINTS,
                       magic=settings.MAGIC_NUMBER, comment=f"V11:{action.reason}"[:31], type_time=self.broker.ORDER_TIME_GTC)
        context["v11"]["requested_close_reason"] = action.reason
        con.execute("UPDATE demo_positions SET context_json=? WHERE id=?", (learning.json_text(context), row["id"]))
        entry = dict(state="reserved", volume=float(position.volume), reason=action.reason, time_msc=learning.now_msc())
        learning.set_state(con, f"close:{ticket}", entry)
        con.commit()  # intent survives a crash during the broker RPC
        result = send_checked_deal(self.broker, request, self.tm.filling_modes())
        learning.set_state(con, f"close:{ticket}", {**entry, "state": result["state"], "ack": result})
        self.event(con, plan.symbol, "exit", f"{action.reason}:{result['state']}", {"ticket": ticket})
        con.commit()

    def entry(self, con: Any, symbol: str, candidate: dict[str, Any], account: Any,
              all_positions: list[Any], guard: dict[str, Any], growth: dict[str, Any]) -> str:
        """Try one candidate and tell the caller whether fall-through is safe."""
        key, setup = candidate["key"], candidate["setup"]
        if con.execute("SELECT 1 FROM v11_order_intents WHERE event_key=?", (key,)).fetchone():
            candidate["consumed"] = True
            return "next_candidate"
        if con.execute("SELECT 1 FROM v11_order_intents WHERE symbol=? AND state IN ('reserved','sent','unknown')", (symbol,)).fetchone():
            self.event(con, symbol, "hold", "unresolved_previous_order")
            return "stop_symbol"
        if not bool(settings.ENABLE_DEMO_ORDER_EXECUTION):
            self.event(con, symbol, "hold", "demo_order_execution_disabled")
            return "stop_symbol"
        pending_orders = self.broker.orders_get(symbol=symbol)
        if pending_orders is None or len(pending_orders):
            self.event(con, symbol, "hold", "pending_orders_or_order_list_unavailable")
            return "stop_symbol"
        account = self.tm.verify_demo_account()
        if int(account.trade_mode) != int(getattr(self.broker, "ACCOUNT_TRADE_MODE_DEMO", 0)):
            raise RuntimeError("V11 always requires a DEMO account")
        tick = self.broker.symbol_info_tick(symbol)
        problem = quote_problem(tick, now_msc=learning.now_msc(), max_age_seconds=float(cfg("SPARTAN_MAX_TICK_AGE_SECONDS", 10)))
        if problem:
            self.event(con, symbol, "hold", problem)
            return "stop_symbol"
        info = self.broker.symbol_info(symbol)
        if info is None:
            self.event(con, symbol, "hold", "symbol_info_unavailable")
            return "stop_symbol"
        frame = self.frames[symbol]["frame"]
        row = frame.iloc[-2]
        atr = finite(row.get("atr_14"))
        prior = candidate["plan"]
        current_entry = float(tick.ask if setup.side == 1 else tick.bid)
        drift_atr = abs(current_entry-prior.entry)/max(atr, 1e-12)
        if drift_atr > float(cfg("V9_MAX_FRESH_PRICE_DRIFT_ATR", .35)):
            self.event(con, symbol, "hold", "fresh_price_drift",
                       {"drift_atr": drift_atr, "limit_atr": float(cfg("V9_MAX_FRESH_PRICE_DRIFT_ATR", .35)),
                        "candidate_entry": prior.entry, "current_entry": current_entry})
            return "next_candidate"
        fees, slip = self.cost_reserve(con, symbol, float(setup.stop_atr))
        plan = plan_for(setup, tick, info, atr, prior.variant, fees, slip)
        spread = float(tick.ask-tick.bid)
        spread_limit_atr = self.tm.v9_exec.spread_limit_atr(float(setup.take_atr))
        spread_atr = spread/max(atr, 1e-12)
        if spread_atr > spread_limit_atr:
            self.event(con, symbol, "hold", "spread_cost_limit",
                       {"spread_atr": spread_atr, "limit_atr": spread_limit_atr,
                        "spread": spread, "atr": atr, "target_r": plan.target_r})
            return "next_candidate"
        stop_gap = (float(tick.bid)-plan.stop) if setup.side == 1 else (plan.stop-float(tick.ask))
        take_gap = (plan.take-float(tick.bid)) if setup.side == 1 else (float(tick.ask)-plan.take)
        if min(stop_gap, take_gap) < plan.min_stop_distance:
            self.event(con, symbol, "hold", "broker_stop_distance_invalid",
                       {"stop_gap": stop_gap, "take_gap": take_gap,
                        "broker_min_distance": plan.min_stop_distance})
            return "next_candidate"
        total_cost_r = plan.commission_r+plan.slippage_r+plan.spread_r
        cost_fraction = total_cost_r/max(plan.target_r, 1e-12)
        if cost_fraction > float(cfg("V11_MAX_COST_TO_TARGET", .35)):
            self.event(con, symbol, "hold", "estimated_cost_exceeds_target_fraction",
                       {"cost_r": total_cost_r, "commission_r": plan.commission_r,
                        "slippage_r": plan.slippage_r, "spread_r": plan.spread_r,
                        "target_r": plan.target_r, "cost_to_target": cost_fraction,
                        "limit": float(cfg("V11_MAX_COST_TO_TARGET", .35))})
            return "next_candidate"
        news = self.tm.spartan.news_status(symbol)
        if news.get("locked") or (bool(cfg("SPARTAN_NEWS_HARD_GATE", False)) and not news.get("available")):
            self.event(con, symbol, "hold", "news_lock_or_required_feed_unavailable", news)
            return "stop_symbol"
        evidence = learning.evidence_for_plan(con, plan, candidate["regime"])
        mode, reason = economic_mode(evidence)
        if mode == "shadow_only":
            self.event(con, symbol, "hold", reason, evidence)
            candidate["consumed"] = True
            return "next_candidate"
        slot_ok, counts = self.tm.v9_exec.trade_slot_available(con, symbol)
        if not slot_ok:
            self.event(con, symbol, "hold", "daily_trade_capacity", counts)
            return "stop_symbol"
        probe = mode == "demo_probe"
        if probe:
            available, capacity = learning.probe_capacity(con, symbol, timestamp_msc=learning.now_msc(),
                per_symbol=int(cfg("V11_PROBES_PER_SYMBOL_DAY", 8)), total_cap=int(cfg("V11_PROBES_TOTAL_DAY", 24)))
            if not available:
                self.event(con, symbol, "hold", "bounded_probe_capacity_reached", capacity)
                return "next_candidate"

        strategy = self.tm.v8_native_alpha_strategy(con, setup)
        flow = self.flow.get(symbol, ())
        flow_value = sum(flow)/len(flow) if flow else None
        micro_metrics = {
            "available": flow_value is not None,
            "score": finite(flow_value), "imbalance": finite(flow_value),
            "persistence": finite(flow_value), "change": 0.0,
        }
        adaptive = self.tm.adaptive_learning_snapshot(
            con, int(strategy.strategy_id), symbol, candidate["regime"], int(setup.side)
        )
        rolling = self.tm.rolling_setup_performance(
            con, int(strategy.strategy_id), symbol, candidate["regime"], int(setup.side),
            max(5, int(cfg("SUPERLEARNER_ROLLING_WINDOW", 20))),
        )
        bayes_loss = self.tm.bayesian_loss_probability(adaptive, rolling)
        signal_details = {
            "buy_votes": int(setup.side == 1), "sell_votes": int(setup.side == -1),
            "buy_weight": 1.0 if setup.side == 1 else .5,
            "sell_weight": 1.0 if setup.side == -1 else .5,
            "micro_hunter": setup.as_dict(),
        }
        super_decision = self.tm.SUPER_LEARNER.predict(
            con, strategy, frame, row, candidate["regime"], int(setup.side),
            signal_details, {"snapshot": adaptive, "bayes_loss_probability": bayes_loss},
            spread, atr, micro_metrics,
        )
        features = super_decision.features
        legacy_model = self.tm.online_model_probability(con, symbol, features)
        model_probability = finite(legacy_model.get("probability"), .5)
        learner_probability = finite(super_decision.probability, model_probability)
        learner_active = bool(legacy_model.get("active", False))
        learner_veto = float(cfg("V11_SUPERLEARNER_VETO_PROBABILITY", .35))
        if learner_active and model_probability <= learner_veto:
            candidate["consumed"] = True
            self.event(con, symbol, "superlearner", "strong_broker_net_conflict",
                       {**legacy_model, "effective_probability": learner_probability,
                        "veto_probability": learner_veto,
                        "execution_effect": "candidate_veto", "target": "positive_net_broker_outcome"}, key)
            return "next_candidate"
        learner_mult = clamp(finite(super_decision.risk_multiplier, 1.0), .50, 1.0)
        if learner_active and model_probability < float(cfg("V11_SUPERLEARNER_DERISK_BELOW", .47)):
            learner_mult = min(learner_mult,
                               clamp(float(cfg("V11_SUPERLEARNER_DERISK_MULTIPLIER", .70)), 0.1, 1.0))

        portfolio_mult, portfolio_mode = self.tm.portfolio_soft_risk_multiplier(guard)
        execution_mult, execution_state = self.tm.scalp_lab.execution_quality_risk_multiplier(con, symbol)
        risk_mult = float(cfg("V11_PROBE_RISK_MULTIPLIER", .25) if probe else cfg("V11_EVIDENCE_RISK_MULTIPLIER", .50))
        risk_mult *= (clamp(portfolio_mult, 0, 1)*clamp(execution_mult, 0, 1)
                      * clamp(finite(growth.get("risk_multiplier"), 1), 0, 1)*learner_mult)
        ceiling = float(cfg("V9_MAX_MIN_LOT_RISK_PCT", .0035))
        # Probes have their OWN actual-risk cap; broker minimum lot is never
        # silently described as 0.025% when it really risks 0.35%.
        if probe:
            ceiling = min(ceiling, float(cfg("V11_PROBE_HARD_RISK_PCT", .001)))
        target_risk = min(ceiling, float(cfg("V9_BASE_RISK_PER_TRADE", .001))*risk_mult)
        sizing = self.tm.volume_plan(symbol, int(setup.side), plan.entry, plan.stop, float(account.equity),
            allow_minimum_bridge=True, risk_fraction=target_risk, hard_ceiling_fraction=ceiling)
        if finite(sizing.get("volume")) <= 0:
            self.event(con, symbol, "sizing", str(sizing.get("reason")), sizing)
            return "next_candidate"
        context = {"micro_hunter": {**setup.as_dict(), "v10_superhuman": bool(getattr(setup, "v10_synthetic", False))},
            "superlearner": {"features": features, "probability": learner_probability,
                             "model_probability": model_probability, "model": legacy_model,
                             "decision": super_decision.as_dict(),
                             "role": "broker_net_outcome_veto_and_derisk_not_TP_probability",
                             "risk_multiplier": learner_mult},
            "target_risk_fraction": target_risk, "actual_risk_pct": sizing["actual_risk_pct"],
            "portfolio_risk_state": {"state": portfolio_mode, "execution": execution_state},
            "v11": {"event_key": key, "plan": plan.to_dict(), "evidence": evidence, "mode": mode,
                    "equity_at_entry": float(account.equity),
                    "state": ExitState.initial(plan).to_dict(), "news_feed": news.get("feed_status"),
                    "raw_score_not_probability": float(setup.score), "sizing": sizing}}

        luna_snapshot = {
            "symbol": symbol,
            "review_mode": "v11_async_pretrade_final",
            "candidate": {
                "action": "buy" if setup.side == 1 else "sell",
                "confidence": learner_probability if learner_active else evidence["net_win_probability"],
                "confluence_score": int(round(clamp(float(setup.score)/100.0, 0, 1)*8)),
            },
            "strategy_context": {"strategy_id": int(strategy.strategy_id), "family": str(strategy.family),
                                 "regime": candidate["regime"], "execution_tier": TIER},
            "market": {"bid": float(tick.bid), "ask": float(tick.ask), "spread_atr": spread_atr},
            "technical": {"rsi7": finite(row.get("rsi_7"), 50), "adx14": finite(row.get("adx_14")),
                          "atr14": atr, "close": finite(row.get("close"))},
            "derived_context": {
                "regime": candidate["regime"], "spread_atr_ratio": spread_atr,
                "ema5_minus_ema13_atr": (finite(row.get("ema_5"))-finite(row.get("ema_13")))/max(atr, 1e-12),
                "price_vs_ema200_pct": (finite(row.get("close"))-finite(row.get("ema_200")))
                                        / max(abs(finite(row.get("ema_200"))), 1e-12)*100,
            },
            "tick_flow": {"available": flow_value is not None, "delta_ratio": flow_value,
                          "samples": len(flow)},
            "news": news,
            "micro_hunter": context["micro_hunter"],
            "ml": {"probability": learner_probability, "model_probability_raw": model_probability,
                   "probability_active": learner_active, "threshold": finite(super_decision.threshold, learner_veto),
                   "expected_r": evidence["expected_net_r"], "reward_to_risk": plan.target_r},
            "learning_context": {"confidence": evidence["net_win_probability"],
                                 "risk_multiplier_advisory": learner_mult},
            "portfolio_context": {**guard, "open_machine_positions": len(all_positions)},
            "capital_growth": growth,
            "opportunity_score": float(setup.score),
            "v11_plan": {**plan.to_dict(), "target_r": plan.target_r, "cost_to_target": cost_fraction},
        }
        luna_payload = dict(
            database=self.path, key=key, symbol=symbol, strategy_id=int(strategy.strategy_id),
            family=str(strategy.family), regime=candidate["regime"], side=int(setup.side),
            bar_time=str(row["time"]), snapshot=luna_snapshot,
        )
        luna = scalp_advisor.pretrade_gate(con, luna_payload)
        if luna["state"] == "pending":
            self.event(con, symbol, "luna", "pretrade_review_pending",
                       {"event_key": key, "age_ms": luna.get("age_ms", 0)}, key)
            return "stop_symbol"
        if luna["state"] == "veto":
            candidate["consumed"] = True
            self.event(con, symbol, "luna", "candidate_veto",
                       {"event_key": key, "review": luna.get("review", {}),
                        "execution_effect": "candidate_veto_try_next"}, key)
            return "next_candidate"
        if luna["state"] == "confirm":
            context["spartan_llm"] = {
                **(luna.get("review") or {}), "fingerprint": luna.get("fingerprint"),
                "review_id": luna.get("review_id"), "execution_effect": "confirmed_candidate",
            }
        else:
            context["spartan_llm"] = {
                "state": "local_fallback", "reason": luna.get("reason"),
                "execution_effect": "deterministic_stack_authorized",
            }
            self.event(con, symbol, "luna", str(luna.get("reason") or "local_fallback"),
                       {"event_key": key, "capacity": luna.get("capacity", {}),
                        "execution_effect": "none_local_fallback"}, key)

        request = dict(action=self.broker.TRADE_ACTION_DEAL, symbol=symbol, volume=float(sizing["volume"]),
            type=self.broker.ORDER_TYPE_BUY if setup.side == 1 else self.broker.ORDER_TYPE_SELL,
            price=plan.entry, sl=plan.stop, tp=plan.take, deviation=settings.DEFAULT_DEVIATION_POINTS,
            magic=settings.MAGIC_NUMBER, comment=f"TM11-{key[:20]}", type_time=self.broker.ORDER_TIME_GTC)
        payload = dict(request=request, context=context, strategy_id=int(strategy.strategy_id),
                       regime=candidate["regime"], equity=float(account.equity), risk_multiplier=risk_mult,
                       risk_cash=finite(sizing["actual_risk_cash"]), probe=probe)
        stamp = learning.now_msc()
        con.execute("INSERT INTO v11_order_intents VALUES(?,?,?,?,?,'reserved',NULL,?,'{}')",
                    (key, symbol, int(setup.side), stamp, stamp, learning.json_text(payload)))
        if probe:
            learning.update_probe(con, key, symbol, "reserved", finite(sizing["actual_risk_cash"]))
        con.commit()
        started = time.perf_counter()
        result = send_checked_deal(self.broker, request, self.tm.filling_modes())
        latency = (time.perf_counter()-started)*1000
        con.execute("UPDATE v11_order_intents SET state=?,updated_msc=?,result_json=? WHERE event_key=?",
                    (result["state"], learning.now_msc(), learning.json_text(result), key))
        if probe:
            learning.update_probe(con, key, symbol, result["state"], finite(sizing["actual_risk_cash"]))
        if result["state"] in {"sent", "unknown"}:
            self.tm.state_set(con, f"last_order:{symbol}", self.tm.utc_now())
        self.tm.scalp_lab.record_execution_quality(con, symbol=symbol, strategy_id=int(strategy.strategy_id),
            side=int(setup.side), requested_entry=plan.entry, filled_entry=(result.get("result") or {}).get("price"),
            spread_price=spread, atr_value=atr, latency_ms=latency, retcode=result.get("retcode"),
            execution_tier=TIER, details={"event_key": key, "state": result["state"], "sizing": sizing})
        learning.record_event(con, symbol, "order", result["state"],
            {"event_key": key, "mode": mode, "expected_net_r": evidence["expected_net_r"],
             "actual_risk_pct": sizing["actual_risk_pct"], "target_risk_pct": target_risk, "broker_latency_ms": latency}, key)
        self.tm.record_execution(con, symbol, int(strategy.strategy_id), request, result.get("result"), result["state"], f"V11 {reason}")
        con.commit()
        print(f"V11 DEMO {symbol} {setup.playbook} {result['state']} | {mode} | netER={evidence['expected_net_r']:+.3f}R | actual risk={sizing['actual_risk_pct']*100:.3f}% | broker={latency:.0f}ms")
        if result["state"] == "sent" and not getattr(setup, "v10_synthetic", False):
            self.tm.micro_hunter.mark_executed(con, setup)
        return "order_attempted"

    def cycle(self) -> None:
        started = time.perf_counter()
        terminal = self.broker.terminal_info()
        if terminal is None or not bool(getattr(terminal, "connected", True)):
            self.tm.connect_mt5(show_account=False)
        account = self.tm.verify_demo_account()
        if int(account.trade_mode) != int(getattr(self.broker, "ACCOUNT_TRADE_MODE_DEMO", 0)):
            raise RuntimeError("V11 DEMO hard lock: real/contest accounts are not permitted")
        with self.tm.db_connect() as con:
            if not self.initialized:
                learning.ensure_tables(con)
                self.tm.v9_exec.ensure_tables(con)
                self.tm.rebuild_execution_memories_v66(con)
                self.initialized = True
                con.commit()
            positions = self.broker.positions_get()
            if positions is None:
                self.event(con, "ALL", "hold", "positions_get_unavailable_no_entry")
                return
            all_positions = list(positions)
            self.bind_intents(con, all_positions)
            # Catch up quote flow before evaluating discretionary exits. Both
            # real and virtual policies see the same known path at this poll.
            market = {}
            for symbol in settings.SYMBOLS:
                try:
                    info = self.tm.prepare_symbol(symbol)
                    tick = self.broker.symbol_info_tick(symbol)
                    problem = quote_problem(tick, now_msc=learning.now_msc(), max_age_seconds=float(cfg("SPARTAN_MAX_TICK_AGE_SECONDS", 10)))
                    if problem:
                        self.candidates[symbol] = []
                        self.event(con, symbol, "data", problem)
                        continue
                    self.replay(con, symbol, tick)
                    market[symbol] = (tick, info)
                except Exception as error:
                    self.candidates[symbol] = []
                    self.event(con, symbol, "data", f"tick_reader_{type(error).__name__}", {"message": str(error)[:180]})
            # Exit management is before feature scans, portfolio locks or AI work.
            self.tm.spartan_manage_demo_positions(con)
            if time.monotonic()-self.last_maintenance >= 2:
                self.tm.reconcile_demo_positions(con)
                self.last_maintenance = time.monotonic()
            for symbol in settings.SYMBOLS:
                try:
                    if symbol not in market:
                        continue
                    tick, info = market[symbol]
                    self.observe(con, symbol, tick, info, replayed=True)
                except Exception as error:
                    self.candidates[symbol] = []
                    self.event(con, symbol, "data", f"observer_{type(error).__name__}", {"message": str(error)[:180]})
            # Observation above continues during EVERY entry-only pause.
            account = self.tm.verify_demo_account()
            allowed, reason, guard = self.tm.risk_guard(con, account)
            growth = self.tm.capital_growth.update_state(con, finite(account.equity))
            learning.set_state(con, "risk", {**guard, "allowed": allowed, "reason": reason})
            if not allowed:
                self.event(con, "ALL", "risk", reason, guard)
            elif bool(cfg("ENABLE_CAPITAL_GROWTH_CONTROLLER", True)) and not growth.get("allow_new_entries", True):
                self.event(con, "ALL", "risk", "capital_growth_entry_lock", growth)
            else:
                for symbol in settings.SYMBOLS:
                    symbol_ok, symbol_reason, symbol_guard = self.tm.symbol_loss_guard(con, symbol)
                    learning.set_state(con, f"symbol_guard:{symbol}", {**symbol_guard, "allowed": symbol_ok})
                    if not symbol_ok:
                        self.event(con, symbol, "risk", symbol_reason, symbol_guard)
                        continue
                    fresh_positions = self.broker.positions_get()
                    if fresh_positions is None:
                        self.event(con, symbol, "hold", "positions_get_unavailable_no_entry")
                        continue
                    all_positions = list(fresh_positions)
                    owned = [p for p in all_positions if int(getattr(p, "magic", -1)) == settings.MAGIC_NUMBER]
                    if len(owned) >= settings.MAX_OPEN_POSITIONS:
                        self.event(con, symbol, "hold", "maximum_open_positions")
                        continue
                    # Never merge into an unrelated/manual/netting position.
                    if any(str(p.symbol) == symbol for p in all_positions):
                        self.event(con, symbol, "hold", "symbol_position_already_open")
                        continue
                    if not self.tm.cooldown_ready(con, symbol):
                        self.event(con, symbol, "hold", "order_cooldown")
                        continue
                    candidates = [c for c in self.candidates.get(symbol, []) if not c.get("consumed") and not con.execute(
                        "SELECT 1 FROM v11_order_intents WHERE event_key=?", (c["key"],)).fetchone()]
                    maximum = max(1, int(cfg("V11_MAX_CANDIDATES_PER_POLL", 8)))
                    for chosen in rank_candidates(candidates)[:maximum]:
                        try:
                            disposition = self.entry(con, symbol, chosen, account, all_positions, guard, growth)
                            if disposition != "next_candidate":
                                break
                        except Exception as error:
                            self.event(con, symbol, "error", f"entry_{type(error).__name__}", {"message": str(error)[:180]})
                            break
            self.loop_ms.append((time.perf_counter()-started)*1000)
            if time.monotonic()-self.last_status >= 30:
                ordered = sorted(self.loop_ms)
                stats = dict(poll_target_seconds=float(cfg("V11_POLL_SECONDS", .5)),
                             processing_median_ms=statistics.median(ordered),
                             processing_p95_ms=ordered[int((len(ordered)-1)*.95)],
                             open_virtual=int(con.execute("SELECT COUNT(*) FROM v11_virtual_positions WHERE status='open'").fetchone()[0]),
                             mode="DEMO_ONLY", version="11.1.0", broker_latency_is_separate=True)
                learning.set_state(con, "heartbeat", stats)
                print(f"V11 observer active | virtual={stats['open_virtual']} | loop p95={stats['processing_p95_ms']:.0f}ms | poll target={stats['poll_target_seconds']}s | DEMO only")
                self.last_status = time.monotonic()
            con.commit()


_RUNTIME: Runtime | None = None


def runtime(tm: Any) -> Runtime:
    global _RUNTIME
    path = str(Path(settings.DATABASE_PATH).resolve())
    if _RUNTIME is None or _RUNTIME.path != path:
        if _RUNTIME is not None:
            _RUNTIME.close()
        _RUNTIME = Runtime(tm)
    return _RUNTIME


def cycle(tm: Any) -> None:
    runtime(tm).cycle()


def manage_position(tm: Any, con: Any, row: Any, position: Any, context: dict[str, Any]) -> None:
    runtime(tm).manage_position(con, row, position, context)


def broker_outcome(con: Any, row: Any, context: dict[str, Any], reward_r: float,
                   pnl: float, closed_msc: int, reason: str) -> None:
    data = context.get("v11") or {}
    if not data:
        return
    plan = TradePlan.from_dict(data["plan"])
    learning.record_outcome(con, key=data["event_key"], plan=plan, regime=str(row["regime"]),
        source="broker", reward_r=reward_r, pnl=pnl, closed_msc=closed_msc, reason=reason,
        details={"position_ticket": int(row["position_ticket"]), "fees_included": True,
                 "fill_price": plan.entry, "state": data.get("state", {})})


def close_reason(broker: Any, final_deal: Any, context: dict[str, Any]) -> str:
    code = int(getattr(final_deal, "reason", -1))
    if code == int(getattr(broker, "DEAL_REASON_SL", 4)):
        plan = context.get("v11", {}).get("plan", {})
        price = finite(getattr(final_deal, "price", 0))
        return "broker_profit_stop" if (price-finite(plan.get("entry")))*int(plan.get("side", 0)) > 0 else "broker_stop_loss"
    if code == int(getattr(broker, "DEAL_REASON_TP", 5)):
        return "broker_take_profit"
    if code == int(getattr(broker, "DEAL_REASON_SO", 6)):
        return "broker_stop_out"
    if code in {0, 1, 2}:
        return "manual_close"
    return str(context.get("v11", {}).get("requested_close_reason") or f"broker_reason_{code}")
