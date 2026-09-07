from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REQUIREMENTS = ROOT / "requirements.txt"

# Only packages required by the CURRENT live/demo runtime are mandatory here.
# The existing SuperLearner implementation is NumPy-based; scikit-learn/SciPy
# are not imported by the production trading path and are intentionally optional.
REQUIRED = {
    "MetaTrader5": "MetaTrader5",
    "pandas": "pandas",
    "numpy": "numpy",
    "openai": "openai",
    "pydantic": "pydantic",
    "dotenv": "python-dotenv",
}


def import_failures() -> list[tuple[str, str]]:
    failures: list[tuple[str, str]] = []
    for module, package in REQUIRED.items():
        try:
            importlib.import_module(module)
        except Exception as exc:
            failures.append((package, f"{type(exc).__name__}: {exc}"))
    return failures


def main() -> int:
    failures = import_failures()
    if not failures:
        print("DEPENDENCY CHECK OK | all required runtime packages import successfully")
        return 0

    print("DEPENDENCY AUTO-REPAIR | missing/broken runtime packages detected:")
    for package, error in failures:
        print(f"  - {package}: {error[:220]}")

    if not REQUIREMENTS.exists():
        print(f"ERROR: requirements.txt missing: {REQUIREMENTS}")
        return 2

    print("Installing/repairing ONLY required runtime packages...")
    cmd = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "-r",
        str(REQUIREMENTS),
    ]
    try:
        completed = subprocess.run(cmd, cwd=str(ROOT), check=False)
    except Exception as exc:
        print(f"ERROR: dependency repair could not start: {type(exc).__name__}: {exc}")
        return 3
    if completed.returncode != 0:
        print(f"ERROR: pip dependency repair failed with exit code {completed.returncode}")
        return 4

    importlib.invalidate_caches()
    remaining = import_failures()
    if remaining:
        print("ERROR: packages still missing/broken after repair:")
        for package, error in remaining:
            print(f"  - {package}: {error[:220]}")
        return 5

    print("DEPENDENCY AUTO-REPAIR OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
