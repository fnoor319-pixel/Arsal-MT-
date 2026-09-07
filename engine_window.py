from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

from runtime_state import engine_pid_path

ROOT = Path(__file__).resolve().parent
LOGS = ROOT / "logs"
LOGS.mkdir(parents=True, exist_ok=True)

ENGINES = {
    "generator": {
        "title": "TM 1 OF 5 - SCALP-FIRST STRATEGY GENERATOR",
        "label": "Scalp-First Strategy Generator",
    },
    "backtester": {
        "title": "TM 2 OF 5 - HISTORICAL OOS WALK-FORWARD BACKTESTER",
        "label": "Historical Backtester",
    },
    "shadow": {
        "title": "TM 3 OF 5 - LIVE SHADOW VALIDATOR - NO ORDERS",
        "label": "Live Shadow Validator",
    },
    "library": {
        "title": "TM 4 OF 5 - SCALP LIBRARY + SESSION SELECTOR",
        "label": "Scalp Library + Session Selector",
    },
    "executor": {
        "title": "TM 5 OF 5 - V11 CANONICAL LEARNING SCALPER - MT5 DEMO ONLY",
        "label": "V11 Canonical Learning + Optional Async Luna + MT5 DEMO",
    },
}


class Tee:
    def __init__(self, *streams: object) -> None:
        self.streams = streams

    def write(self, text: str) -> int:
        for stream in self.streams:
            if stream is None:
                continue
            try:
                stream.write(text)  # type: ignore[attr-defined]
                stream.flush()  # type: ignore[attr-defined]
            except Exception:
                pass
        return len(text)

    def flush(self) -> None:
        for stream in self.streams:
            if stream is None:
                continue
            try:
                stream.flush()  # type: ignore[attr-defined]
            except Exception:
                pass


def pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            check=False,
        )
        text = (result.stdout or "").strip()
        return bool(text) and "No tasks are running" not in text and f'"{pid}"' in text
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def set_title(title: str) -> None:
    if sys.platform == "win32":
        try:
            ctypes.windll.kernel32.SetConsoleTitleW(title)
        except Exception:
            pass


def run_engine(name: str) -> None:
    # Import inside the retry boundary so a startup/import error stays visible
    # and is retried instead of closing the console.
    import trading_machine as tm

    if name == "generator":
        tm.strategy_generation_engine(8760.0, 16, 24, 3, 20)
    elif name == "backtester":
        tm.historical_backtest_engine(8760.0, 240, 5)
    elif name == "shadow":
        tm.run_loop("shadow", 8760.0, 5)
    elif name == "library":
        tm.approved_library_engine(8760.0, 15)
    elif name == "executor":
        tm.run_loop("demo", 8760.0, float(getattr(tm.settings, "V11_POLL_SECONDS", 0.5)))
    else:
        raise ValueError(f"Unknown engine: {name}")


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in ENGINES:
        print("Usage: engine_window.py generator|backtester|shadow|library|executor")
        return 2

    name = sys.argv[1]
    info = ENGINES[name]
    set_title(str(info["title"]))
    pid_path = engine_pid_path(name)

    try:
        existing = int(pid_path.read_text(encoding="ascii").strip()) if pid_path.exists() else 0
    except (OSError, ValueError):
        existing = 0
    if existing and existing != os.getpid() and pid_is_running(existing):
        print(f"{info['label']} is already running (PID {existing}).")
        return 3

    pid_path.write_text(str(os.getpid()), encoding="ascii")
    log_path = LOGS / f"engine_{name}_{datetime.now():%Y%m%d}.log"
    log_handle = log_path.open("a", encoding="utf-8", buffering=1)
    original_out = sys.stdout
    original_err = sys.stderr
    sys.stdout = Tee(original_out, log_handle)  # type: ignore[assignment]
    sys.stderr = Tee(original_err, log_handle)  # type: ignore[assignment]

    print("=" * 78)
    print(f"{info['label']} visible engine wrapper started | PID={os.getpid()}")
    print(f"Log: {log_path}")
    print("A startup/runtime error will be shown here and retried automatically.")
    print("=" * 78)

    attempt = 0
    try:
        while True:
            attempt += 1
            try:
                print(f"\n{datetime.now():%Y-%m-%d %H:%M:%S} | engine start attempt={attempt}")
                run_engine(name)
                print("Engine loop returned normally; restarting in 5 seconds.")
                time.sleep(5)
            except KeyboardInterrupt:
                print("\nEngine stopped by user.")
                return 0
            except BaseException as error:
                print(f"\nENGINE ERROR [{name}]: {type(error).__name__}: {error}")
                traceback.print_exc()
                print("Retrying in 20 seconds. This window will remain open.")
                time.sleep(20)
    finally:
        try:
            if pid_path.exists() and pid_path.read_text(encoding="ascii").strip() == str(os.getpid()):
                pid_path.unlink()
        except OSError:
            pass
        log_handle.close()


if __name__ == "__main__":
    raise SystemExit(main())
