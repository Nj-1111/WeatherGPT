"""One HTTP connection pool for the whole process.

Every adapter used to open its own AsyncClient per call, so a request paid a fresh TLS
handshake per source. Sharing one pooled client keeps connections warm across requests,
which is the largest avoidable latency on a single-instance deployment.
"""
from __future__ import annotations

import httpx

from app.config import settings

_client: httpx.AsyncClient | None = None


def get_client() -> httpx.AsyncClient:
    """Created lazily so the pool binds to the running event loop, not to import time."""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.source_timeout_seconds),
            limits=httpx.Limits(max_connections=settings.http_max_connections,
                                max_keepalive_connections=settings.http_max_keepalive),
            follow_redirects=True,
            headers={"User-Agent": settings.http_user_agent},
        )
    return _client


async def close_client() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None
