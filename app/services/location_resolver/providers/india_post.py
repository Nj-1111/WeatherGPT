"""Indian PIN code resolution — India Post public API (free, no key); Open-Meteo's geocoder returns nothing for Indian PINs (verified: 560001, 110001 both empty). Returns District/State but no coordinates, so it only resolves a PIN to a place name — the caller geocodes that through the normal provider chain."""
from __future__ import annotations

from dataclasses import dataclass

from app.adapters.http import get_client
from app.config import settings

LOOKUP_URL = "https://api.postalpincode.in/pincode/"


@dataclass(frozen=True)
class PincodePlace:
    district: str
    state: str
    name: str | None = None

    def as_query(self) -> str:
        """District + state is the most reliably geocodable form of a PIN's location."""
        return f"{self.district}, {self.state}"


class IndiaPostProvider:
    name = "india_post"

    async def lookup(self, pincode: str) -> PincodePlace | None:
        response = await get_client().get(f"{LOOKUP_URL}{pincode}",
                                          timeout=settings.geocoding_timeout_seconds)
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, list):
            payload = payload[0] if payload else None
        if not isinstance(payload, dict) or payload.get("Status") != "Success":
            return None
        offices = payload.get("PostOffice")
        if not isinstance(offices, list) or not offices:
            return None
        first = offices[0]
        if not isinstance(first, dict):
            return None
        district, state = first.get("District"), first.get("State")
        if not district or not state:
            return None
        return PincodePlace(district=str(district), state=str(state), name=first.get("Name"))
