"""CAP adapter — official warnings, lifecycle aware.

The configured feed is an RSS index whose items each link to a separate CAP alert
document, so fetching is two-step. Pointing this at the index and decoding it directly
yields zero warnings while appearing to succeed.
"""
from __future__ import annotations

import asyncio
import logging
import time
import xml.etree.ElementTree as ET
from typing import Any

from app.adapters.base import WeatherSourceAdapter
from app.adapters.http import get_client
from app.config import settings
from app.decoders.cap_decoder import decode_cap_xml
from app.schemas.ceo import CanonicalEvidenceObject

logger = logging.getLogger(__name__)


class CapAdapter(WeatherSourceAdapter):
    source_name = "CAP"

    async def fetch(self, xml_bytes: bytes | None = None, **kwargs) -> list[bytes]:
        if xml_bytes is not None:
            return [xml_bytes]
        if not settings.cap_feed_url:
            raise RuntimeError("CAP_FEED_URL not configured")
        client = get_client()
        response = await client.get(settings.cap_feed_url)
        response.raise_for_status()
        links = self._alert_links(response.content)
        if not links:
            # The feed was a single alert document rather than an index.
            return [response.content]
        documents = await asyncio.gather(
            *[client.get(link) for link in links[: settings.cap_max_alerts]], return_exceptions=True
        )
        alerts: list[bytes] = []
        for link, document in zip(links, documents, strict=False):
            if isinstance(document, BaseException):
                logger.warning("cap.alert_fetch_failed", extra={"link": link, "error": type(document).__name__})
                continue
            if document.status_code == 200:
                alerts.append(document.content)
        return alerts

    @staticmethod
    def _alert_links(payload: bytes) -> list[str]:
        try:
            root = ET.fromstring(payload)
        except ET.ParseError:
            return []
        return [link.text.strip() for link in root.findall(".//item/link")
                if link.text and link.text.strip().endswith(".xml")]

    def normalize(self, raw: Any, **kwargs) -> list[CanonicalEvidenceObject]:
        if not raw:
            return []
        documents: list[bytes] = [bytes(raw)] if isinstance(raw, (bytes, bytearray)) else list(raw)
        out: list[CanonicalEvidenceObject] = []
        for document in documents:
            try:
                out.extend(decode_cap_xml(document))
            except ET.ParseError as exc:
                logger.warning("cap.alert_unparseable", extra={"error": str(exc)})
        return out

    async def health_check(self) -> dict[str, Any]:
        started = time.time()
        if not settings.cap_feed_url:
            return {"available": False, "latency_ms": 0, "reason": "CAP_FEED_URL not configured (ready)"}
        try:
            response = await get_client().get(settings.cap_feed_url, timeout=settings.source_timeout_seconds)
            response.raise_for_status()
            alerts = len(self._alert_links(response.content))
            return {"available": True, "latency_ms": int((time.time() - started) * 1000),
                    "reason": f"feed reachable; {alerts} alerts listed"}
        except Exception as exc:
            return {"available": False, "latency_ms": int((time.time() - started) * 1000), "reason": str(exc)}
