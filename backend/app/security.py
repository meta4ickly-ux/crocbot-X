"""Security helpers: secret redaction, SSRF protection, rate limiting.

Rules (blueprint §Security, research report §5):
- Secrets never appear in logs, JSONL run logs, or responses.
- fetch_url: default-deny allow-list + block private/loopback/link-local
  / metadata IPs (SSRF). No fetch at all when the allow-list is empty.
- Rate limiting: in-memory per-IP token bucket; 429 when exceeded.
"""
from __future__ import annotations

import ipaddress
import re
import socket
import time
from collections import defaultdict
from urllib.parse import urlparse

_SECRET_PATTERNS = [
    re.compile(r"(?i)(xai[_-]?api[_-]?key|api[_-]?key|bearer)\s*[:=]\s*['\"]?([A-Za-z0-9_\-]{8,})"),
    re.compile(r"(?i)(sk-[A-Za-z0-9_\-]{8,})"),
]

REDACTED = "[REDACTED]"


def redact(text: str) -> str:
    """Strip anything that looks like a credential from log text."""
    if not text:
        return text
    out = text
    for pat in _SECRET_PATTERNS:
        out = pat.sub(REDACTED, out)
    return out


def is_ip_blocked(ip: str) -> bool:
    """True for private, loopback, link-local, multicast, reserved IPs."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


def check_fetch_url(url: str, allowlist: list[str]) -> str:
    """Validate a fetch URL. Returns the URL, or raises ValueError.

    Default-deny: an empty allow-list refuses every fetch. Hostnames are
    resolved and every resolved address is checked (SSRF).
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"fetch refused: scheme {parsed.scheme!r} not allowed")
    host = (parsed.hostname or "").lower()
    if not host:
        raise ValueError("fetch refused: no hostname")
    if not allowlist:
        raise ValueError("fetch refused: CROCBOT_FETCH_ALLOWLIST is empty (default-deny)")
    if host not in [h.lower() for h in allowlist] and not any(
        host.endswith("." + h.lower()) for h in allowlist
    ):
        raise ValueError(f"fetch refused: host {host!r} is not on the allow-list")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise ValueError(f"fetch refused: cannot resolve {host!r}: {exc}") from exc
    for info in infos:
        if is_ip_blocked(info[4][0]):
            raise ValueError(f"fetch refused: {host!r} resolves to blocked IP {info[4][0]}")
    return url


class RateLimiter:
    """Per-key token bucket, in-memory (Redis on the VPS — see BACKEND.md)."""

    def __init__(self, per_minute: int = 60):
        self.per_minute = per_minute
        self._hits: dict[str, list[float]] = defaultdict(list)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        window = [t for t in self._hits[key] if now - t < 60]
        if len(window) >= self.per_minute:
            self._hits[key] = window
            return False
        window.append(now)
        self._hits[key] = window
        return True
