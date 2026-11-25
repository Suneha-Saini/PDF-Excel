"""Find the public https address that reaches this backend (for webhook testing and for showing the URL to register).

Order: PUBLIC_BASE_URL in .env if set, otherwise ask a locally running ngrok for its tunnels (its local API on
127.0.0.1:4040). Cloudflare/other tunnels: set PUBLIC_BASE_URL. Returns None when nothing is found."""
from typing import Optional

import httpx

from app.config import settings

NGROK_LOCAL_API = "http://127.0.0.1:4040/api/tunnels"


def detect_public_url(backend_port: int = 8000) -> Optional[str]:
    configured = (getattr(settings, "PUBLIC_BASE_URL", None) or "").strip()
    if configured:
        return configured.rstrip("/")
    try:
        data = httpx.get(NGROK_LOCAL_API, timeout=1.0).json()
        for t in data.get("tunnels", []):
            url = str(t.get("public_url") or "")
            addr = str((t.get("config") or {}).get("addr") or "")
            # only a tunnel that points at THIS backend (a tunnel to the frontend can never receive a webhook)
            if url.startswith("https://") and addr.rstrip("/").endswith(f":{backend_port}"):
                return url.rstrip("/")
    except Exception:  # noqa: BLE001 - ngrok not running / not reachable is the normal "no tunnel" case
        pass
    return None
