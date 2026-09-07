"""Weather Intelligence Object — compact LLM-safe summary."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


class WIOQuery(BaseModel):
    raw_text: str
    resolved_location: dict[str, Any] = Field(default_factory=dict)
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    intent: str | None = None  # e.g. precipitation, pesticide_spraying, marine
    lang: str = "en"
    # Mirrors GuardrailDecision.apparent_context — a short free-text phrase describing who's
    # asking, for the explanation prompt to read; None whenever the guardrail didn't run or
    # didn't infer anything.
    apparent_context: str | None = None

class WIOWeather(BaseModel):
    summary: str = ""
    rain: dict[str, Any] | None = None
    wind: dict[str, Any] | None = None
    temperature: dict[str, Any] | None = None
    humidity: dict[str, Any] | None = None
    pressure: dict[str, Any] | None = None
    cloud_cover: dict[str, Any] | None = None
    visibility: dict[str, Any] | None = None
    heat_stress: dict[str, Any] | None = None
    marine: dict[str, Any] | None = None
    other: dict[str, Any] = Field(default_factory=dict)

class WIOWarning(BaseModel):
    active: bool = False
    authority: str | None = None
    severity: str | None = None
    event: str | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    areas: list[str] = Field(default_factory=list)
    provenance: dict[str, Any] | None = None

class WIOAgreement(BaseModel):
    status: str = "unknown"  # full_agreement | partial_agreement | conflict | insufficient_evidence
    notes: str = ""

class EvidenceSummary(BaseModel):
    evidence_id: str
    source: str
    evidence_class: str
    variable: str
    value: float | None = None
    unit: str | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    provenance: dict[str, Any] | None = None

class WeatherIntelligenceObject(BaseModel):
    query: WIOQuery
    weather: WIOWeather = Field(default_factory=WIOWeather)
    official_warning: WIOWarning = Field(default_factory=WIOWarning)
    agreement: WIOAgreement = Field(default_factory=WIOAgreement)
    evidence: list[EvidenceSummary] = Field(default_factory=list)
    disagreements: list[str] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    wio_version: str = "1.0"

