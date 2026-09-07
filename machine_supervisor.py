from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import IO

import settings
from runtime_state import (
    LOCK_PATH,
    PID_PATH,
    READY_PATH,
    STATE_DIR,
    atomic_write_text,
    engine_pid_path,
)

ROOT = Path(__file__).resolve().parent
LOGS = ROOT / "logs"
ENGINE_WRAPPER = ROOT / "engine_window.py"
START_TOKEN = os.environ.get("TM_START_TOKEN", "manual-start")
LOGS.mkdir(parents=True, exist_ok=True)
STATE_DIR.mkdir(parents=True, exist_ok=True)

if sys.stdout is None:
    sys.stdout = open(LOGS / "supervisor_hidden.log", "a", encoding="utf-8", buffering=1)
if sys.stderr is None:
    sys.stderr = sys.stdout


@dataclass
class Worker:
    name: str
    engine_arg: str
    window_title: str
    pid: int | None = None
    restarts: int = 0

    @property
    def pid_path(self) -> Path:
        return engine_pid_path(self.engine_arg)


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


def read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="ascii", errors="ignore").strip())
    except (OSError, ValueError):
        return None


def acquire_single_instance_lock() -> IO[bytes]:
    """Lock one byte and publish the actual interpreter PID separately."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    handle = open(LOCK_PATH, "a+b")
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            handle.close()
            raise RuntimeError(
                "Supervisor already running. Run 09_STOP_ALL_WINDOWS.bat first."
            ) from error
    atomic_write_text(PID_PATH, f"{os.getpid()}\n")
    return handle


def write_ready(workers: list[Worker]) -> None:
    lines = [
        f"token={START_TOKEN}",
        f"supervisor_pid={os.getpid()}",
        f"project_root={ROOT}",
    ]
    lines.extend(f"{worker.engine_arg}_pid={worker.pid or 0}" for worker in workers)
    atomic_write_text(READY_PATH, "\n".join(lines) + "\n", encoding="utf-8")


def remove_stale_worker_pid(worker: Worker) -> None:
    old_pid = read_pid(worker.pid_path)
    if old_pid and pid_is_running(old_pid):
        subprocess.run(
            ["taskkill", "/PID", str(old_pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        time.sleep(1)
    try:
        worker.pid_path.unlink(missing_ok=True)
    except OSError:
        pass


def start_worker(worker: Worker) -> None:
    if not ENGINE_WRAPPER.exists():
        raise FileNotFoundError(f"Missing {ENGINE_WRAPPER.name}")
    remove_stale_worker_pid(worker)

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    if os.name == "nt":
        process = subprocess.Popen(
            [sys.executable, "-u", str(ENGINE_WRAPPER), worker.engine_arg],
            cwd=str(ROOT),
            env=env,
            creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
            close_fds=True,
        )
    else:
        process = subprocess.Popen(
            [sys.executable, "-u", str(ENGINE_WRAPPER), worker.engine_arg],
            cwd=str(ROOT),
            env=env,
        )

    for _ in range(60):
        pid = read_pid(worker.pid_path)
        if pid and pid_is_running(pid):
            worker.pid = pid
            print(
                f"{datetime.now().isoformat(timespec='seconds')} OPENED "
                f"{worker.name} pid={pid} title={worker.window_title}",
                flush=True,
            )
            return
        if process.poll() is not None:
            raise RuntimeError(
                f"{worker.name} process exited before its PID marker was created "
                f"(exit={process.returncode}). Check "
                f"logs/engine_{worker.engine_arg}_YYYYMMDD.log."
            )
        time.sleep(0.25)
    raise RuntimeError(
        f"{worker.name} console was created but its PID marker was not written. "
        f"Check logs/engine_{worker.engine_arg}_YYYYMMDD.log."
    )


def stop_worker(worker: Worker) -> None:
    pid = worker.pid or read_pid(worker.pid_path)
    if pid and pid_is_running(pid):
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    try:
        worker.pid_path.unlink(missing_ok=True)
    except OSError:
        pass
    worker.pid = None


def database_snapshot() -> str:
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(settings.DATABASE_PATH, timeout=2)
        rows = connection.execute(
            "SELECT status, COUNT(*) FROM strategies GROUP BY status"
        ).fetchall()
        positions = connection.execute(
            "SELECT COUNT(*) FROM demo_positions WHERE status='open'"
        ).fetchone()[0]
        counts = ", ".join(f"{status}={count}" for status, count in rows)
        return f"strategies[{counts}] demo_open={positions}"
    except Exception as error:
        return f"database_status_unavailable={error}"
    finally:
        if connection is not None:
            connection.close()


def cleanup_supervisor_markers() -> None:
    for path in (READY_PATH, PID_PATH):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def main() -> int:
    lock = acquire_single_instance_lock()
    try:
        READY_PATH.unlink(missing_ok=True)
    except OSError:
        pass

    workers = [
        Worker("live_strategy_generator", "generator", "TM 1 of 5 - SCALP-FIRST Strategy Generator"),
        Worker("historical_backtester", "backtester", "TM 2 of 5 - Historical OOS Walk-Forward Backtester"),
        Worker("live_shadow_validator", "shadow", "TM 3 of 5 - Live Shadow Validator"),
        Worker("approved_library_selector", "library", "TM 4 of 5 - Scalp Library + Session Selector"),
        Worker("demo_executor", "executor", "TM 5 of 5 - V11 CANONICAL LEARNING - MT5 DEMO Executor"),
    ]
    print("=" * 78, flush=True)
    print("V11 CANONICAL DEMO SCALPER SUPERVISOR - FIVE VISIBLE ENGINE WINDOWS", flush=True)
    print("Closed/crashed worker windows are restarted automatically.", flush=True)
    print(f"Startup token={START_TOKEN}", flush=True)
    print("=" * 78, flush=True)

    stopping = False

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, request_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, request_stop)

    try:
        for worker in workers:
            start_worker(worker)
        write_ready(workers)
        print(
            f"{datetime.now().isoformat(timespec='seconds')} "
            f"READY token={START_TOKEN} five_windows=1",
            flush=True,
        )

        last_status = 0.0
        while not stopping:
            now = time.time()
            changed = False
            for worker in workers:
                live_pid = read_pid(worker.pid_path)
                if not live_pid or not pid_is_running(live_pid):
                    worker.restarts += 1
                    print(
                        f"{datetime.now().isoformat(timespec='seconds')} RESTART "
                        f"{worker.name} old_pid={worker.pid} attempt={worker.restarts}",
                        flush=True,
                    )
                    time.sleep(max(1, settings.WORKER_RESTART_SECONDS))
                    start_worker(worker)
                    changed = True
                else:
                    worker.pid = live_pid
            if changed:
                write_ready(workers)
            if now - last_status >= settings.SUPERVISOR_STATUS_SECONDS:
                process_text = " | ".join(
                    f"{w.name}:pid={w.pid or '-'} restart={w.restarts}" for w in workers
                )
                print(
                    f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | "
                    f"{process_text} | {database_snapshot()}",
                    flush=True,
                )
                last_status = now
            time.sleep(2)
    finally:
        print("Stopping all five workers...", flush=True)
        cleanup_supervisor_markers()
        for worker in workers:
            stop_worker(worker)
        lock.close()
        try:
            LOCK_PATH.unlink(missing_ok=True)
        except OSError:
            pass
        print("All workers stopped.", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        cleanup_supervisor_markers()
        print(f"SUPERVISOR ERROR: {error}", flush=True)
        raise SystemExit(1)
