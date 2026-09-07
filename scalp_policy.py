"""Versioned, deterministic scalp exits shared by DEMO and tick replay.

There is no broker, network, model, or database access in this module. Quotes
are executable bid/ask prices, never candle highs invented as tick paths.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, replace
from typing import Any


POLICY_VERSION = "11.0.0"


def finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (ValueError, TypeError, OverflowError):
        return default


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, finite(value)))


def price_grid(price: float, tick_size: float, direction: int = 0) -> float:
    step = max(1e-12, finite(tick_size, 1e-5))
    scaled = price / step
    units = math.floor(scaled + 1e-9) if direction < 0 else math.ceil(scaled - 1e-9) if direction > 0 else round(scaled)
    return float(f"{units * step:.12g}")


@dataclass(frozen=True)
class TradePlan:
    plan_id: str
    version: str
    variant: str
    symbol: str
    playbook: str
    side: int
    entry: float
    stop: float
    take: float
    risk_distance: float
    opened_msc: int
    max_hold_seconds: float
    price_step: float
    min_stop_distance: float
    commission_r: float
    slippage_r: float
    spread_r: float
    be_at_r: float
    be_lock_r: float
    trail_at_r: float
    trail_distance_r: float
    adverse_cut_r: float
    adverse_min_seconds: float
    adverse_flow_threshold: float

    @property
    def target_r(self) -> float:
        return abs(self.take - self.entry) / self.risk_distance

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "TradePlan":
        plan = cls(**{key: value[key] for key in cls.__dataclass_fields__})
        validate_plan(plan)
        return plan


def validate_plan(plan: TradePlan) -> None:
    numeric = [plan.entry, plan.stop, plan.take, plan.risk_distance,
               plan.max_hold_seconds, plan.price_step, plan.min_stop_distance,
               plan.commission_r, plan.slippage_r, plan.spread_r,
               plan.be_at_r, plan.be_lock_r, plan.trail_at_r,
               plan.trail_distance_r, plan.adverse_cut_r,
               plan.adverse_min_seconds, plan.adverse_flow_threshold]
    if not all(math.isfinite(float(x)) for x in numeric):
        raise ValueError("Non-finite trade plan")
    if plan.side not in (-1, 1) or min(plan.entry, plan.stop, plan.take, plan.risk_distance, plan.price_step) <= 0:
        raise ValueError("Invalid trade plan prices/side")
    if (plan.entry - plan.stop) * plan.side <= 0 or (plan.take - plan.entry) * plan.side <= 0:
        raise ValueError("Stop/target is on the wrong side")
    if plan.max_hold_seconds <= 0 or plan.opened_msc <= 0:
        raise ValueError("Missing trade deadline")
    if not math.isclose(abs(plan.entry - plan.stop), plan.risk_distance, rel_tol=1e-7, abs_tol=1e-9):
        raise ValueError("Plan risk must match the initial stop")
    if min(plan.commission_r, plan.slippage_r, plan.spread_r, plan.min_stop_distance) < 0:
        raise ValueError("Negative trading cost")


def make_plan(*, symbol: str, playbook: str, side: int, entry: float,
              atr: float, stop_atr: float, take_atr: float,
              max_hold_bars: int, opened_msc: int, price_step: float,
              min_stop_distance: float = 0.0, spread: float = 0.0,
              commission_r: float = 0.0, slippage_r: float = 0.02,
              variant: str = "balanced") -> TradePlan:
    if side not in (-1, 1) or not math.isfinite(atr) or atr <= 0 or not math.isfinite(entry) or entry <= 0:
        raise ValueError("A plan needs a valid side, ATR, and entry")
    if variant not in {"balanced", "runner"}:
        raise ValueError("Unknown exit policy")
    rd = clamp(stop_atr, 0.35, 1.50) * atr
    td = clamp(take_atr, 0.50, 2.80) * atr
    stop = price_grid(entry - side * rd, price_step, -side)
    take = price_grid(entry + side * td, price_step, side)
    rd = abs(entry - stop)
    cost = max(0.0, finite(commission_r)) + max(0.0, finite(slippage_r))
    rr = abs(take - entry) / max(rd, 1e-12)
    # Whole-position policy: no untradeable 0.005-lot partials, no early
    # 0.05R profit grab, and no widening/averaging of losing positions.
    be_at = max(cost + 0.15, min(0.85 if variant == "balanced" else 1.05, rr * 0.72))
    trail_at = max(be_at + 0.15, min(1.15 if variant == "balanced" else 1.35, rr * 0.88))
    values = dict(version=POLICY_VERSION, variant=variant, symbol=str(symbol),
                  playbook=str(playbook), side=side, entry=float(entry),
                  stop=stop, take=take, risk_distance=rd,
                  opened_msc=int(opened_msc), max_hold_seconds=max(1, int(max_hold_bars)) * 60.0,
                  price_step=max(1e-12, finite(price_step)),
                  min_stop_distance=max(0.0, finite(min_stop_distance)),
                  commission_r=max(0.0, finite(commission_r)),
                  slippage_r=max(0.0, finite(slippage_r)), spread_r=max(0.0, finite(spread)) / rd,
                  be_at_r=be_at, be_lock_r=cost + 0.03,
                  trail_at_r=trail_at, trail_distance_r=0.60 if variant == "balanced" else 0.80,
                  adverse_cut_r=-0.65, adverse_min_seconds=25.0,
                  adverse_flow_threshold=0.75)
    digest = hashlib.sha256(json.dumps(values, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:24]
    plan = TradePlan(plan_id=digest, **values)
    validate_plan(plan)
    return plan


def reprice_plan(plan: TradePlan, entry: float, opened_msc: int, spread: float) -> TradePlan:
    """Finalize on the fresh quote, preserving policy and price distances."""
    stop = price_grid(entry - plan.side * plan.risk_distance, plan.price_step, -plan.side)
    take = price_grid(entry + plan.side * abs(plan.take - plan.entry), plan.price_step, plan.side)
    values = plan.to_dict()
    values.update(entry=float(entry), stop=stop, take=take,
                  risk_distance=abs(entry - stop), opened_msc=int(opened_msc),
                  spread_r=max(0.0, spread) / abs(entry - stop))
    values.pop("plan_id")
    values["plan_id"] = hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()[:24]
    result = TradePlan(**values)
    validate_plan(result)
    return result


@dataclass
class ExitState:
    active_stop: float
    mfe_r: float = 0.0
    mae_r: float = 0.0
    last_msc: int = 0
    last_bid: float = 0.0
    last_ask: float = 0.0

    @classmethod
    def initial(cls, plan: TradePlan) -> "ExitState":
        return cls(active_stop=plan.stop, last_msc=plan.opened_msc)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ExitAction:
    action: str = "hold"
    reason: str = ""
    price: float = 0.0
    new_stop: float = 0.0
    gross_r: float = 0.0


def evaluate_quote(plan: TradePlan, state: ExitState, *, bid: float, ask: float,
                   time_msc: int, quote_flow: float | None = None,
                   flow_samples: int = 0, now_msc: int | None = None,
                   allow_management: bool = True) -> ExitAction:
    """Evaluate an observed quote without pretending an SL amendment filled.

    The caller applies ``new_stop`` only after broker acknowledgement (or after
    the same amendment in replay). For equal timestamps, different quotes are
    still processed in feed order. Timeout uses wall time for open broker trades.
    """
    if not all(math.isfinite(x) and x > 0 for x in (bid, ask)) or ask < bid:
        return ExitAction(reason="invalid_quote")
    if time_msc < state.last_msc or time_msc < plan.opened_msc:
        return ExitAction(reason="out_of_order_quote")
    mark = bid if plan.side == 1 else ask
    gross_r = plan.side * (mark - plan.entry) / plan.risk_distance
    state.mfe_r = max(state.mfe_r, gross_r, 0.0)
    state.mae_r = max(state.mae_r, -gross_r, 0.0)
    state.last_msc = int(time_msc)
    state.last_bid, state.last_ask = bid, ask
    if plan.side * (mark - state.active_stop) <= 1e-10:
        reason = "stop_loss" if plan.side * (state.active_stop - plan.entry) < 0 else "profit_stop"
        return ExitAction("close", reason, mark, gross_r=gross_r)
    if plan.side * (mark - plan.take) >= -1e-10:
        return ExitAction("close", "take_profit", mark, gross_r=gross_r)
    # Broker-side SL/TP is tick-sensitive. Client-side amendments only occur
    # on actual polls, not on every historical tick replayed between polls.
    if not allow_management:
        return ExitAction(gross_r=gross_r)
    age = (max(time_msc, int(now_msc or time_msc)) - plan.opened_msc) / 1000.0
    if age >= plan.max_hold_seconds:
        return ExitAction("close", "time_exit", mark, gross_r=gross_r)
    if (age >= plan.adverse_min_seconds and gross_r <= plan.adverse_cut_r
            and quote_flow is not None and math.isfinite(quote_flow)
            and flow_samples >= 8 and plan.side * quote_flow <= -plan.adverse_flow_threshold):
        return ExitAction("close", "adverse_quote_flow", mark, gross_r=gross_r)
    candidate: float | None = None
    if gross_r >= plan.be_at_r:
        candidate = plan.entry + plan.side * plan.be_lock_r * plan.risk_distance
    if gross_r >= plan.trail_at_r:
        trailing = mark - plan.side * plan.trail_distance_r * plan.risk_distance
        if candidate is None or plan.side * (trailing - candidate) > 0:
            candidate = trailing
    if candidate is None:
        return ExitAction(gross_r=gross_r)
    distance = max(plan.min_stop_distance, plan.price_step)
    candidate = min(candidate, bid - distance) if plan.side == 1 else max(candidate, ask + distance)
    candidate = price_grid(candidate, plan.price_step, -plan.side)
    if candidate <= 0 or plan.side * (candidate - state.active_stop) < plan.price_step * 0.5:
        return ExitAction(gross_r=gross_r)
    return ExitAction("amend_stop", "profit_protection", mark, candidate, gross_r)


def net_virtual_reward(plan: TradePlan, action: ExitAction) -> float:
    """Spread is already in executable prices; subtract only fees/slippage."""
    if action.action != "close":
        raise ValueError("Only a closed position has a reward")
    return action.gross_r - plan.commission_r - plan.slippage_r


def reward_summary(rewards: list[float]) -> dict[str, Any]:
    clean = [float(x) for x in rewards if math.isfinite(float(x))]
    n = len(clean)
    winners = [x for x in clean if x > 0]
    losers = [x for x in clean if x < 0]
    total = sum(clean)
    mean = total / n if n else 0.0
    variance = sum((x - mean) ** 2 for x in clean) / (n - 1) if n > 1 else 0.0
    cumulative = peak = drawdown = 0.0
    for value in clean:
        cumulative += value
        peak = max(peak, cumulative)
        drawdown = max(drawdown, peak - cumulative)
    avg_win = sum(winners) / len(winners) if winners else 0.0
    avg_loss = -sum(losers) / len(losers) if losers else 0.0
    return dict(n=n, wins=len(winners), losses=len(losers), flat=n-len(winners)-len(losers),
                win_rate=len(winners)/n if n else None, sum_r=total, mean_r=mean,
                avg_win_r=avg_win, avg_loss_r=avg_loss,
                profit_factor=sum(winners)/-sum(losers) if losers else None,
                break_even_win_rate=avg_loss/(avg_win+avg_loss) if avg_win and avg_loss else None,
                standard_error=math.sqrt(variance/n) if n > 1 else None,
                max_drawdown_r=drawdown)


def empirical_expectancy(samples: list[dict[str, Any]], *, prior_n: float = 4.0) -> dict[str, Any]:
    """Estimate net return directly; a tiny winner is never assigned full TP.

    Only prospective same-policy shadow outcomes and actual same-policy broker
    outcomes qualify. A shadow candidate paired with a broker trade contributes
    once, at the broker weight. Legacy results remain separate diagnostics.
    """
    by_event: dict[str, dict[str, Any]] = {}
    for sample in samples:
        if sample.get("source") not in {"broker", "shadow"} or sample.get("quality", "complete") != "complete":
            continue
        if not math.isfinite(finite(sample.get("reward_r"), float("nan"))):
            continue
        key = str(sample.get("event_key") or sample.get("id"))
        old = by_event.get(key)
        if old is None or sample.get("source") == "broker":
            by_event[key] = sample
    rows = list(by_event.values())
    weights = [1.0 if row["source"] == "broker" else 0.25 for row in rows]
    mass = sum(weights)
    total = sum(w * finite(row["reward_r"]) for w, row in zip(weights, rows))
    mean = total / (mass + max(0.0, prior_n)) if mass else 0.0
    win_mass = sum(w for w, row in zip(weights, rows) if finite(row["reward_r"]) > 0)
    probability = (win_mass + prior_n/2.0)/(mass+prior_n) if mass+prior_n else 0.5
    actual = reward_summary([finite(row["reward_r"]) for row in rows])
    return dict(expected_net_r=mean, net_win_probability=probability,
                broker_n=sum(row["source"] == "broker" for row in rows),
                shadow_n=sum(row["source"] == "shadow" for row in rows),
                effective_n=mass, unique_events=len(rows),
                source="realized_net_outcomes" if rows else "unproven",
                observed=actual)
