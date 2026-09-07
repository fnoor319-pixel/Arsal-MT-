from __future__ import annotations

from getpass import getpass
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENV_PATH = ROOT / ".env"
DEFAULT_MODEL = "gpt-5.6-luna"


def update_env(key: str, model: str) -> None:
    existing = []
    if ENV_PATH.exists():
        existing = ENV_PATH.read_text(encoding="utf-8", errors="ignore").splitlines()
    kept = [line for line in existing if not line.strip().startswith(("OPENAI_API_KEY=", "OPENAI_MODEL=", "OPENAI_REASONING_EFFORT="))]
    kept.extend([f"OPENAI_API_KEY={key}", f"OPENAI_MODEL={model}", "OPENAI_REASONING_EFFORT=low"] )
    ENV_PATH.write_text("\n".join(kept).rstrip() + "\n", encoding="utf-8")


def main() -> int:
    print("Spartan-Pro GPT configuration")
    print("API key screen par show nahi hogi aur source code mein hardcode nahi hogi.")
    key = getpass("OPENAI_API_KEY paste karein: ").strip()
    if not key:
        print("ERROR: API key empty hai.")
        return 2
    model = input(f"Model [{DEFAULT_MODEL}]: ").strip() or DEFAULT_MODEL
    update_env(key, model)
    print(f"Saved to local .env (do not share this file) | model={model}")
    print("Ab 18_TEST_GPT_CONNECTION.bat chalayein.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
