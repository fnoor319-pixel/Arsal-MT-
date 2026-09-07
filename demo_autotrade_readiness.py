from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import settings


def _bool(name: str) -> bool:
    return bool(getattr(settings, name, False))


def _candidate_counts(symbol: str) -> tuple[int, int]:
    db = Path(getattr(settings, "DATABASE_PATH", Path(settings.PROJECT_DIR) / "trading_machine.db"))
    with sqlite3.connect(db) as con:
        shadow = con.execute(
            "SELECT COUNT(*) FROM strategies WHERE symbol=? AND status='shadow_approved'",
            (symbol,),
        ).fetchone()[0]
        hist = con.execute(
            "SELECT COUNT(*) FROM strategies WHERE symbol=? AND status='historical_validated'",
            (symbol,),
        ).fetchone()[0]
    return int(shadow), int(hist)


def _mt5_demo_status() -> tuple[bool, str]:
    try:
        import MetaTrader5 as mt5
    except Exception as exc:
        return False, f"MetaTrader5 import failed: {exc}"
    if not mt5.initialize():
        return False, f"MT5 initialize failed: {mt5.last_error()}"
    try:
        account = mt5.account_info()
        terminal = mt5.terminal_info()
        if account is None:
            return False, f"MT5 account_info failed: {mt5.last_error()}"
        demo_code = int(getattr(mt5, "ACCOUNT_TRADE_MODE_DEMO", 0))
        is_demo = int(getattr(account, "trade_mode", -1)) == demo_code
        if not is_demo:
            return False, "Current MT5 account is NOT DEMO; hard lock will block every order."
        if terminal is not None and not bool(getattr(terminal, "trade_allowed", True)):
            return False, "MT5 terminal AutoTrading/trade permission is disabled."
        return True, (
            f"MT5 DEMO OK | login={getattr(account, 'login', 'n/a')} | "
            f"server={getattr(account, 'server', 'n/a')} | equity={float(getattr(account, 'equity', 0.0)):.2f}"
        )
    finally:
        mt5.shutdown()


def main() -> int:
    print("=== SPARTAN PRO DEMO AUTOTRADE READINESS ===")
    failures: list[str] = []

    checks = {
        "DEMO order execution": _bool("ENABLE_DEMO_ORDER_EXECUTION"),
        "DEMO-only hard lock": _bool("DEMO_ONLY_HARD_LOCK"),
        "V11 async GPT or complete local fallback": _bool("V11_ENABLED") or _bool("SPARTAN_LLM_REVIEW_ENABLED"),
        "GPT legacy fail-closed / V11 logged fallback": _bool("V11_ENABLED") or _bool("SPARTAN_LLM_FAIL_CLOSED"),
        "DEMO trial bridge": _bool("ENABLE_DEMO_TRIAL_BRIDGE"),
    }
    for label, ok in checks.items():
        print(f"{'PASS' if ok else 'FAIL'} | {label}")
        if not ok:
            failures.append(label)

    for symbol in settings.SYMBOLS:
        shadow, hist = _candidate_counts(symbol)
        executable = _bool("V11_ENABLED") or shadow > 0 or (_bool("ENABLE_DEMO_TRIAL_BRIDGE") and hist > 0)
        source = "V11 bounded local playbook probes" if _bool("V11_ENABLED") else ("shadow_approved" if shadow > 0 else ("historical_validated DEMO trial" if hist > 0 else "none"))
        print(
            f"{'PASS' if executable else 'FAIL'} | {symbol} candidate pool | "
            f"shadow_approved={shadow} historical_validated={hist} source={source}"
        )
        if not executable:
            failures.append(f"{symbol} executable candidate pool")

    mt5_ok, mt5_message = _mt5_demo_status()
    print(f"{'PASS' if mt5_ok else 'FAIL'} | {mt5_message}")
    if not mt5_ok:
        failures.append("MT5 DEMO readiness")

    print()
    if failures:
        print("NOT READY: " + ", ".join(failures))
        return 2
    print("READY: start may place MT5 DEMO orders after signal, evidence/probe, news, cost, risk, SuperLearner and broker checks.")
    print("Luna: configured reviews are async final candidate decisions; unavailable/quota/error uses logged local fallback.")
    print("NOTE: READY does not mean an immediate trade is forced; no valid setup = HOLD/no order.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
