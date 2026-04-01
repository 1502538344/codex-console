from __future__ import annotations

import re
from typing import Iterable, Optional
from urllib.request import getproxies


_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")
_PROXY_KEY_PRIORITY = ("https", "http", "all", "socks", "socks5")


def _normalize_proxy_url(
    value: Optional[str], scheme_hint: str = "http"
) -> Optional[str]:
    raw = str(value or "").strip()
    if not raw:
        return None

    lowered = raw.lower()
    if lowered in {"direct", "direct://", "none"}:
        return None

    if _SCHEME_RE.match(raw):
        if lowered.startswith("socks://"):
            return "socks5://" + raw[len("socks://") :]
        return raw

    if scheme_hint in {"socks", "socks5"}:
        return f"socks5://{raw}"
    return f"http://{raw}"


def get_system_proxy_url(priority: Optional[Iterable[str]] = None) -> Optional[str]:
    try:
        proxies = getproxies()
    except Exception:
        return None

    if not isinstance(proxies, dict):
        return None

    for key in tuple(priority) if priority else _PROXY_KEY_PRIORITY:
        normalized = _normalize_proxy_url(proxies.get(key), scheme_hint=key)
        if normalized:
            return normalized

    return None
