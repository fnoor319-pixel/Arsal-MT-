from __future__ import annotations

import sys
from pydantic import BaseModel

import settings


class HealthReply(BaseModel):
    ok: bool
    message: str


def local_gpt_readiness() -> tuple[bool, str]:
    """Zero-token startup readiness check.

    Repeated bot restarts used to make a paid API health request even when no
    trade ever reached Luna. V8 validates configuration/imports locally at
    startup; the real trading call remains fail-closed if the service is not
    reachable. Use 18_TEST_GPT_CONNECTION.bat only when an actual network test
    is intentionally wanted.
    """
    key = str(getattr(settings, "OPENAI_API_KEY", "") or "").strip()
    model = str(getattr(settings, "SPARTAN_LLM_MODEL", getattr(settings, "GPT_MODEL", "")) or "").strip()
    if not key:
        return False, "OPENAI_API_KEY missing. Run 17_CONFIGURE_GPT.bat first."
    if not model:
        return False, "GPT/Luna model missing."
    try:
        from openai import OpenAI  # noqa: F401
    except Exception as error:
        return False, f"OpenAI Python package unavailable: {type(error).__name__}: {error}"
    return True, f"LUNA STARTUP READY (LOCAL, 0 API TOKENS) | model={model}"


def test_gpt_connection() -> tuple[bool, str]:
    key = str(getattr(settings, "OPENAI_API_KEY", "") or "").strip()
    model = str(getattr(settings, "SPARTAN_LLM_MODEL", getattr(settings, "GPT_MODEL", "")) or "").strip()
    if not key:
        return False, "OPENAI_API_KEY missing. Run 17_CONFIGURE_GPT.bat first."
    if not model:
        return False, "GPT_MODEL missing."
    try:
        from openai import OpenAI
        client = OpenAI(api_key=key, timeout=15.0)
        response = client.responses.parse(
            model=model,
            input=[
                {"role": "system", "content": "Connectivity check only. No trading advice."},
                {"role": "user", "content": "Return ok=true and a 1-3 word message."},
            ],
            text_format=HealthReply,
            store=False,
            max_output_tokens=24,
            reasoning={"effort": "none"},
        )
        parsed = response.output_parsed
        if parsed is None or not parsed.ok:
            return False, "OpenAI responded but structured health response was not OK."
        return True, f"GPT CONNECTION OK | model={model} | {parsed.message}"
    except Exception as error:
        return False, f"GPT CONNECTION FAILED | {type(error).__name__}: {str(error)[:400]}"


def main() -> int:
    startup_local = any(arg in {"--startup", "--local", "--startup-local"} for arg in sys.argv[1:])
    ok, message = local_gpt_readiness() if startup_local else test_gpt_connection()
    print(message)
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
