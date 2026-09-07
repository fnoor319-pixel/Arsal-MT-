from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from typing import Any

import settings


def telegram_alert(text: str) -> dict[str, Any]:
    if not bool(getattr(settings, "SPARTAN_TELEGRAM_ENABLED", False)):
        return {"sent": False, "reason": "disabled"}
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return {"sent": False, "reason": "missing_credentials"}
    try:
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": text[:3500]}).encode("utf-8")
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=data,
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
        return {"sent": bool(payload.get("ok")), "reason": "ok" if payload.get("ok") else "api_rejected"}
    except Exception as error:
        return {"sent": False, "reason": f"{type(error).__name__}: {error}"[:240]}
