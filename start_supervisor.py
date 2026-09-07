from __future__ import annotations

import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

from runtime_state import (
    LOCK_PATH,
    PID_PATH,
    READY_PATH,
    STATE_DIR,
    engine_pid_path,
)

ROOT = Path(__file__).resolve().parent
LOGS = ROOT / "logs"
LOG = LOGS / "supervisor_hidden.log"
SUPERVISOR = ROOT / "machine_supervisor.py"
LEGACY_MARKERS = (
    ROOT / "machine_supervisor.ready",
    ROOT / "machine_supervisor.pid",
    ROOT / "machine_supervisor.lock",
    ROOT / "engine_generator.pid",
    ROOT / "engine_backtester.pid",
    ROOT / "engine_shadow.pid",
    ROOT / "engine_library.pid",
    ROOT / "engine_executor.pid",
)
ENGINE_NAMES = ("generator", "backtester", "shadow", "library", "executor")


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
        return int(path.read_text(encoding="ascii", errors="ignore").strip().splitlines()[0])
    except (OSError, ValueError, IndexError):
        return None


def read_key_values(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {}
    values: dict[str, str] = {}
    for line in lines:
        key, separator, value = line.partition("=")
        if separator:
            values[key.strip()] = value.strip()
    return values


def ready_state(token: str) -> tuple[int, list[int]] | None:
    values = read_key_values(READY_PATH)
    if values.get("token") != token:
        return None
    try:
        supervisor_pid = int(values["supervisor_pid"])
        worker_pids = [int(values[f"{name}_pid"]) for name in ENGINE_NAMES]
    except (KeyError, ValueError):
        return None
    if not pid_is_running(supervisor_pid):
        return None
    if not all(pid_is_running(pid) for pid in worker_pids):
        return None
    return supervisor_pid, worker_pids


def live_worker_pids() -> list[int]:
    result: list[int] = []
    for name in ENGINE_NAMES:
        pid = read_pid(engine_pid_path(name))
        if not pid or not pid_is_running(pid):
            return []
        result.append(pid)
    return result


def tail_log(lines: int = 55) -> str:
    try:
        content = LOG.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(content[-lines:])
    except OSError:
        return "Supervisor log is not available."


def remove_stale_markers() -> bool:
    paths = [READY_PATH, PID_PATH, LOCK_PATH]
    paths.extend(engine_pid_path(name) for name in ENGINE_NAMES)
    paths.extend(LEGACY_MARKERS)
    for path in paths:
        if path.exists():
            try:
                path.unlink()
            except OSError as error:
                print(f"ERROR: Could not remove stale marker {path}: {error}")
                return False
    return True


def main() -> int:
    if os.name != "nt":
        print("This launcher is intended for Windows.")
        return 2
    if not SUPERVISOR.exists():
        print(f"ERROR: Missing {SUPERVISOR.name}")
        return 3

    LOGS.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    existing_pid = read_pid(PID_PATH)
    if existing_pid and pid_is_running(existing_pid):
        workers = live_worker_pids()
        print(f"Trading Machine is already running (supervisor PID {existing_pid}).")
        if len(workers) == 5:
            print("All five engine windows are already active.")
        print("Use 09_STOP_ALL_WINDOWS.bat before starting another copy.")
        return 0

    legacy_pid = read_pid(ROOT / "machine_supervisor.pid")
    if legacy_pid and pid_is_running(legacy_pid):
        print(f"An older Trading Machine supervisor is still running (PID {legacy_pid}).")
        print("Run 09_STOP_ALL_WINDOWS.bat first.")
        return 4

    if not remove_stale_markers():
        print("Run 09_STOP_ALL_WINDOWS.bat, then start again.")
        return 4

    token = secrets.token_hex(16)
    env = os.environ.copy()
    env["TM_START_TOKEN"] = token
    env["TM_PROJECT_ROOT"] = str(ROOT)

    with LOG.open("a", encoding="utf-8", buffering=1) as log_handle:
        log_handle.write("\n" + "=" * 78 + "\n")
        log_handle.write(f"START REQUESTED BY FINAL V5.5 SUPERLEARNER BOOTSTRAP token={token}\n")
        log_handle.flush()
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
        process = subprocess.Popen(
            [sys.executable, "-u", str(SUPERVISOR)],
            cwd=str(ROOT),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            creationflags=flags,
            close_fds=True,
        )

    # Important: on Windows a virtual-environment python launcher PID can differ
    # from os.getpid() inside the actual interpreter. Therefore success is tied
    # to the unique token + actual supervisor/worker PIDs, never process.pid equality.
    started_at = time.monotonic()
    deadline = started_at + 60.0
    while time.monotonic() < deadline:
        state = ready_state(token)
        if state is not None:
            supervisor_pid, worker_pids = state
            print(f"Supervisor started successfully (actual PID {supervisor_pid}).")
            print("Five visible engine windows opened successfully.")
            print("Worker PIDs: " + ", ".join(str(pid) for pid in worker_pids))
            return 0

        actual_pid = read_pid(PID_PATH)
        actual_running = bool(actual_pid and pid_is_running(actual_pid))
        if (
            process.poll() is not None
            and not actual_running
            and time.monotonic() - started_at >= 8.0
        ):
            # A Windows venv redirector may exit before the actual interpreter
            # publishes its PID. Give the real supervisor a generous startup
            # window before treating launcher exit as a genuine failure.
            print(f"ERROR: Supervisor launcher exited with code {process.returncode}.")
            print("\nLast supervisor log lines:\n")
            print(tail_log())
            return 5
        time.sleep(0.25)

    actual_pid = read_pid(PID_PATH)
    workers = live_worker_pids()
    if actual_pid and pid_is_running(actual_pid) and len(workers) == 5:
        print(f"Supervisor is running (actual PID {actual_pid}).")
        print("Five engine PID markers are live; startup accepted despite delayed READY file.")
        return 0

    log_text = tail_log()
    ready_log_token = f"READY token={token} five_windows=1"
    if actual_pid and pid_is_running(actual_pid) and ready_log_token in log_text:
        print(f"Supervisor is running (actual PID {actual_pid}).")
        print("READY was confirmed from the current startup log; system remains running.")
        return 0

    print("ERROR: Five-engine startup could not be verified.")
    print("The launcher will NOT kill a live supervisor merely because a marker is delayed.")
    print("\nLast supervisor log lines:\n")
    print(log_text)
    return 6


if __name__ == "__main__":
    raise SystemExit(main())
