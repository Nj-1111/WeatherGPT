"""Server-to-server API key auth: a small, known set of trusted backend callers (not public end users) present a static key; this only ever verifies it and derives a scope id, with no concept of login, expiry, or accounts."""
from __future__ import annotations

import hashlib
import hmac

from app.config import settings


def verify_api_key(header_value: str | None) -> str | None:
    """Returns the matched configured key, or None — compares every key with hmac.compare_digest so a mismatch can't be timed to learn which key came closer."""
    if not header_value or not header_value.startswith("Bearer "):
        return None
    candidate = header_value[len("Bearer "):]
    matched = None
    for key in settings.api_keys:
        if hmac.compare_digest(candidate, key):
            matched = key
    return matched


def key_scope(api_key: str) -> str:
    """Stable per-key id used to namespace stored user/session data — one-way, so the raw key never ends up sitting in storage."""
    return hashlib.sha256(api_key.encode()).hexdigest()[:16]
