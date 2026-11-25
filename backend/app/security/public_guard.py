"""Keep the app private when a tunnel (ngrok, cloudflared) exposes it to the internet.

The app has no login: it is a single-user local tool, so every endpoint is open to whoever can reach it. Webhooks need a
public address, though, so when a request arrives from outside (through a tunnel) ONLY the signature-verified webhook
paths may answer; everything else gets a plain 404, as if it did not exist.

A request counts as "public" when its Host is not a local address, or when it carries proxy headers that only a tunnel or
reverse proxy adds (a local browser talking straight to the backend never sends them).
"""
import ipaddress
from typing import Mapping, Tuple

#: Paths that may be reached from the internet. Each is authenticated by an HMAC signature, not by a login.
PUBLIC_PREFIXES: Tuple[str, ...] = ("/api/webhooks/",)

_TUNNEL_HEADERS = ("x-forwarded-for", "x-forwarded-host", "cf-connecting-ip", "x-real-ip", "forwarded")


def _is_local_host(host: str) -> bool:
    h = host.strip().lower()
    if h.startswith("["):                       # [::1]:8000
        h = h[1:].split("]")[0]
    elif h.count(":") == 1:                     # name:port or ipv4:port
        h = h.split(":")[0]
    if h in ("localhost", "host.docker.internal", ""):
        return True
    try:
        ip = ipaddress.ip_address(h)
        return ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_unspecified
    except ValueError:
        return "." not in h                     # a bare machine name ("DELL") is on the local network


def is_public_request(headers: Mapping[str, str]) -> bool:
    """True when the request came in from outside rather than from this machine or its own network."""
    lower = {k.lower(): v for k, v in headers.items()}
    if any(lower.get(h) for h in _TUNNEL_HEADERS):
        return True
    return not _is_local_host(lower.get("host", ""))


def allowed_when_public(path: str) -> bool:
    return any(path.startswith(p) for p in PUBLIC_PREFIXES)
