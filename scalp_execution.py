"""Conservative MT5 acknowledgements; never blindly retry an unknown send."""
from __future__ import annotations

from typing import Any


def plain(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "_asdict"):
        return {k: plain(v) for k, v in value._asdict().items()}
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [plain(v) for v in value]
    if hasattr(value, "__dict__"):
        return {k: plain(v) for k, v in vars(value).items() if not k.startswith("_")}
    return str(value)


def send_checked_deal(broker: Any, request: dict[str, Any], modes: list[int]) -> dict[str, Any]:
    """Only INVALID_FILL is a definite, safe reason to try another fill mode.

    None, exceptions, timeout and connection errors after order_send are
    ambiguous. The caller must persist an intent BEFORE calling this function,
    and reconcile broker history BEFORE another entry on that symbol.
    """
    invalid_fill = int(getattr(broker, "TRADE_RETCODE_INVALID_FILL", 10030))
    accepted = {10008, 10009, 10010}
    unknown = {10012, 10031}
    attempted = False
    last: dict[str, Any] = {}
    for mode in dict.fromkeys(modes):
        current = {**request, "type_filling": int(mode)}
        try:
            check = broker.order_check(current)
        except Exception as error:
            return dict(state="rejected", reason=f"check_{type(error).__name__}", sent=False)
        if check is None:
            return dict(state="rejected", reason="order_check_unavailable", sent=False)
        code = int(getattr(check, "retcode", -1))
        last = dict(retcode=code, result=plain(check), request=current, sent=attempted)
        if code == invalid_fill:
            continue
        if code not in {0, *accepted}:
            return dict(last, state="rejected", reason="order_check_rejected")
        try:
            attempted = True
            result = broker.order_send(current)
        except Exception as error:
            return dict(state="unknown", reason=f"send_{type(error).__name__}", request=current, sent=True)
        if result is None:
            return dict(state="unknown", reason="no_send_acknowledgement", request=current, sent=True)
        code = int(getattr(result, "retcode", -1))
        last = dict(retcode=code, result=plain(result), request=current, sent=True)
        if code in accepted:
            return dict(last, state="sent", reason="broker_acknowledged")
        if code == invalid_fill:
            continue
        if code in unknown or code < 0:
            return dict(last, state="unknown", reason="ambiguous_broker_acknowledgement")
        return dict(last, state="rejected", reason="broker_rejected")
    return dict(last, state="rejected", reason="no_supported_fill_mode", sent=attempted)


def quote_time(tick: Any) -> int:
    return int(getattr(tick, "time_msc", 0) or int(getattr(tick, "time", 0)) * 1000)


def quote_problem(tick: Any, *, now_msc: int, max_age_seconds: float) -> str:
    from scalp_policy import finite
    if tick is None:
        return "tick_unavailable"
    bid, ask = finite(getattr(tick, "bid", 0)), finite(getattr(tick, "ask", 0))
    if bid <= 0 or ask < bid:
        return "invalid_bid_ask"
    stamp = quote_time(tick)
    if stamp <= 0 or now_msc - stamp > max_age_seconds * 1000:
        return "stale_tick"
    if stamp - now_msc > 2000:
        return "future_tick_clock_mismatch"
    return ""
