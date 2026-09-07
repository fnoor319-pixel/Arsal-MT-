from __future__ import annotations

import os
import sqlite3
import subprocess
from pathlib import Path

import settings
from runtime_state import PID_PATH, READY_PATH, engine_pid_path
import spartan_decision_memory as memory
import capital_growth

ENGINES = ("generator", "backtester", "shadow", "library", "executor")


def read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="ascii", errors="ignore").strip().splitlines()[0])
    except Exception:
        return None


def running(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True, text=True, check=False)
        text = (r.stdout or "").strip()
        return bool(text) and "No tasks are running" not in text and f'"{pid}"' in text
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def main() -> int:
    print("=" * 78)
    print("V6.8 MACHINE HEALTH / SELF-HEALING STATUS")
    print("=" * 78)
    sup = read_pid(PID_PATH)
    print(f"Supervisor: pid={sup} running={running(sup)} ready_marker={READY_PATH.exists()}")
    all_workers = True
    for name in ENGINES:
        pid = read_pid(engine_pid_path(name))
        ok = running(pid)
        all_workers &= ok
        print(f"  {name:<10} pid={pid} running={ok}")
    print(f"Auto-restart supervisor coverage: {'OK' if all_workers else 'CHECK/START MACHINE'}")

    db = Path(settings.DATABASE_PATH)
    print(f"Database: {db} exists={db.exists()}")
    if db.exists():
        try:
            with sqlite3.connect(db, timeout=10) as con:
                con.row_factory = sqlite3.Row
                con.execute("SELECT 1").fetchone()
                memory.ensure_tables(con)
                budget = memory.api_budget_snapshot(con)
                last = con.execute("SELECT timestamp,reason,symbol FROM decision_logs ORDER BY id DESC LIMIT 1").fetchone()
                print("SQLite integrity/read: OK")
                if last:
                    print(f"Last decision: {last['timestamp']} {last['symbol']} | {last['reason']}")
                print(
                    f"Luna bot budget: used≈${budget['estimated_spent_usd']:.4f} / ${budget['budget_usd']:.2f} "
                    f"allowed={budget['allowed']}"
                )
                cap = capital_growth.snapshot(con)
                if cap.get("phase") != "waiting_for_equity":
                    print(
                        f"Capital growth: today={float(cap.get('growth_pct') or 0)*100:+.2f}% "
                        f"peak={float(cap.get('peak_growth_pct') or 0)*100:+.2f}% "
                        f"phase={cap.get('phase')}"
                    )
        except Exception as error:
            print(f"SQLite check: ERROR {type(error).__name__}: {error}")

    try:
        import MetaTrader5 as mt5
        ok = bool(mt5.initialize())
        print(f"MT5 initialize: {ok}")
        if ok:
            account = mt5.account_info()
            terminal = mt5.terminal_info()
            print(
                f"MT5 terminal connected={bool(getattr(terminal,'connected',False))} "
                f"trade_allowed={bool(getattr(terminal,'trade_allowed',False))} "
                f"account_mode={getattr(account,'trade_mode',None)} server={getattr(account,'server',None)}"
            )
            mt5.shutdown()
    except Exception as error:
        print(f"MT5 check unavailable: {type(error).__name__}: {error}")

    print(f"OpenAI key configured: {bool(str(getattr(settings,'OPENAI_API_KEY','')).strip())}")
    print(f"Luna model: {settings.GPT_MODEL}")
    print("NOTE: this health check does NOT spend an API call.")
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
