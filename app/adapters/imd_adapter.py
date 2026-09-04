"""IMD Adapter — ready stub, fails clearly if IMD_API_KEY missing."""
from __future__ import annotations

import time
from typing import Any

from app.adapters.base import WeatherSourceAdapter
from app.adapters.http import get_client
from app.config import settings
from app.decoders.imd_json import decode


class ImdAdapter(WeatherSourceAdapter):
    source_name = "IMD"

    async def fetch(self, **kwargs) -> Any:
        key = settings.imd_api_key
        base = settings.imd_api_base or "https://api.data.gov.in"
        if not key:
            raise RuntimeError("IMD_API_KEY missing — IMD source unavailable (ready, not mocked)")
        # Example: city forecast endpoint — real IMD portal requires specific resource
        # This adapter is ready but will only be used when credentials are set
        params = {"api-key": key, "format": "json", "limit": 10, **kwargs}
        response = await get_client().get(f"{base}/resource/3b01bcb8-0b14-4abf-b6f2-c1bfd384ba69", params=params)
        response.raise_for_status()
        return response.json()

    def normalize(self, raw: Any, **kwargs) -> list:
        # raw is IMD JSON records list
        product = kwargs.get("product", "forecast")
        if isinstance(raw, dict) and "records" in raw:
            records = raw["records"]
        elif isinstance(raw, list):
            records = raw
        else:
            records = [raw]
        out=[]
        for rec in records:
            out.extend(decode(rec, product=product))
        return out

    async def health_check(self) -> dict[str, Any]:
        if not settings.imd_api_key:
            return {"available": False, "latency_ms": 0, "reason": "IMD_API_KEY not configured (ready)"}
        start=time.time()
        try:
            await self.fetch(limit=1)
            return {"available": True, "latency_ms": int((time.time()-start)*1000), "reason": "ok"}
        except Exception as e:
            return {"available": False, "latency_ms": int((time.time()-start)*1000), "reason": str(e)}
