from __future__ import annotations

import json
import threading
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import settings

_LOCK = threading.Lock()
_STARTED = time.monotonic()
_LAST_REPORT = _STARTED
_COUNTS: dict[str, Counter[str]] = defaultdict(Counter)
_LAST_REASON: dict[str, str] = {}
_TOTAL: Counter[str] = Counter()


def record(symbol: str, gate: str, reason: str = "", detail: dict[str, Any] | None = None) -> None:
    del detail  # reserved for future richer diagnostics; keep hot-path cheap.
    sym = str(symbol or "UNKNOWN")
    key = str(gate or "unknown")
    with _LOCK:
        _COUNTS[sym][key] += 1
        _TOTAL[key] += 1
        if reason:
            _LAST_REASON[sym] = str(reason)[:180]


def _snapshot_locked(window_seconds: float) -> dict[str, Any]:
    symbols: dict[str, Any] = {}
    for symbol in sorted(_COUNTS):
        counter = _COUNTS[symbol]
        symbols[symbol] = {
            "counts": dict(counter),
            "top_gate": counter.most_common(1)[0][0] if counter else None,
            "last_reason": _LAST_REASON.get(symbol, ""),
        }
    return {
        "window_seconds": round(window_seconds, 1),
        "symbols": symbols,
        "total": dict(_TOTAL),
    }


def maybe_report(force: bool = False) -> None:
    global _LAST_REPORT
    interval = max(15, int(getattr(settings, "SCALP_FLOW_SUMMARY_SECONDS", 60)))
    now = time.monotonic()
    with _LOCK:
        if not force and now - _LAST_REPORT < interval:
            return
        window = max(0.1, now - _LAST_REPORT)
        snap = _snapshot_locked(window)
        _LAST_REPORT = now
        # Window counters reset; cumulative DB logs remain authoritative.
        _COUNTS.clear()
        _LAST_REASON.clear()
        _TOTAL.clear()

    parts: list[str] = []
    for symbol, data in snap["symbols"].items():
        counts = data["counts"]
        key_order = ("micro_trigger", "signal", "spartan_veto", "super_veto", "gpt_call", "gpt_veto", "rescue", "order_sent", "no_signal", "spread")
        compact = ",".join(f"{k}={counts.get(k, 0)}" for k in key_order if counts.get(k, 0))
        if not compact:
            compact = f"idle={sum(counts.values())}"
        parts.append(f"{symbol}[{compact};last={data.get('last_reason') or '-'}]")
    if parts:
        print(f"SCALP FLOW {int(snap['window_seconds'])}s | " + " | ".join(parts))

    try:
        reports = Path(getattr(settings, "REPORTS_DIR", Path(__file__).resolve().parent / "reports"))
        reports.mkdir(parents=True, exist_ok=True)
        target = reports / "scalp_flow_live.json"
        temp = target.with_suffix(".json.tmp")
        temp.write_text(json.dumps(snap, indent=2, default=str), encoding="utf-8")
        temp.replace(target)
    except Exception:
        pass
