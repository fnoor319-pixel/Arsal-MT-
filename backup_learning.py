from __future__ import annotations

import hashlib
import shutil
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SOURCE_DB = ROOT / "trading_machine.db"
SOURCE_DATA = ROOT / "data"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    if not SOURCE_DB.exists():
        print(f"ERROR: Missing database: {SOURCE_DB}")
        return 1
    documents = Path.home() / "Documents"
    destination_root = documents / "TradingMachineBackups"
    destination_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    zip_base = destination_root / f"TM_FINAL_LEARNING_{stamp}"

    with tempfile.TemporaryDirectory(prefix="tm_learning_backup_") as temporary:
        stage = Path(temporary) / "TM_LEARNING"
        stage.mkdir(parents=True)
        backup_db = stage / "trading_machine.db"
        source_connection = sqlite3.connect(
            f"file:{SOURCE_DB.as_posix()}?mode=ro", uri=True, timeout=30
        )
        destination_connection = sqlite3.connect(backup_db)
        try:
            source_connection.backup(destination_connection)
        finally:
            destination_connection.close()
            source_connection.close()

        if SOURCE_DATA.exists():
            shutil.copytree(SOURCE_DATA, stage / "data")

        connection = sqlite3.connect(backup_db)
        try:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            statuses = connection.execute(
                "SELECT status, COUNT(*) FROM strategies GROUP BY status ORDER BY status"
            ).fetchall()
        finally:
            connection.close()

        manifest = [
            f"Created: {datetime.now().isoformat(timespec='seconds')}",
            f"Source: {ROOT}",
            f"Database integrity: {integrity}",
            f"Database SHA-256: {sha256(backup_db)}",
            "Strategy statuses: " + ", ".join(
                f"{status}={count}" for status, count in statuses
            ),
        ]
        (stage / "BACKUP_MANIFEST.txt").write_text(
            "\n".join(manifest) + "\n", encoding="utf-8"
        )
        archive = Path(shutil.make_archive(str(zip_base), "zip", stage.parent, stage.name))

    print(f"Learning backup created: {archive}")
    print("It contains trading_machine.db, data folder and a manifest.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
