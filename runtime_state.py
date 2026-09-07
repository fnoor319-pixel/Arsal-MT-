from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def get_state_dir() -> Path:
    configured = os.environ.get("TM_STATE_DIR", "").strip()
    if configured:
        path = Path(configured)
    else:
        local = os.environ.get("LOCALAPPDATA", "").strip()
        path = Path(local) / "TMFINAL" / "run" if local else ROOT / ".tmfinal_run"
    path.mkdir(parents=True, exist_ok=True)
    return path


STATE_DIR = get_state_dir()
LOCK_PATH = STATE_DIR / "machine_supervisor.lock"
PID_PATH = STATE_DIR / "machine_supervisor.pid"
READY_PATH = STATE_DIR / "machine_supervisor.ready"


def engine_pid_path(engine: str) -> Path:
    return STATE_DIR / f"engine_{engine}.pid"


def atomic_write_text(path: Path, text: str, encoding: str = "ascii") -> None:
    """Write a marker atomically so another process never reads a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding=encoding, newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
