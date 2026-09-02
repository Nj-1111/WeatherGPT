"""WeatherSourceAdapter — common interface for all weather sources."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from app.schemas.ceo import CanonicalEvidenceObject


class WeatherSourceAdapter(ABC):
    source_name: str
    supported_evidence_classes: list[str]
    supported_variables: list[str]

    @abstractmethod
    async def fetch(self, **kwargs) -> Any:
        """Fetch raw data from source. Must raise on unavailable/credential missing."""
        raise NotImplementedError

    @abstractmethod
    def normalize(self, raw: Any, **kwargs) -> list[CanonicalEvidenceObject]:
        """Convert raw source data into CEOs, preserving provenance."""
        raise NotImplementedError

    @abstractmethod
    async def health_check(self) -> dict[str, Any]:
        """Return {available: bool, latency_ms, reason, last_success} without LLM."""
        raise NotImplementedError
