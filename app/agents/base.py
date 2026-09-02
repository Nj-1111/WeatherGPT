"""AgentResult — structured output for all agents."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


class Claim(BaseModel):
    claim: str
    value: Any
    unit: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    confidence: float = 0.8
    extra: dict[str, Any] = Field(default_factory=dict)

class AgentResult(BaseModel):
    agent_name: str
    claims: list[Claim] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    confidence: float = 0.8
    uncertainty: str | None = None
    warnings: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    execution_time_ms: int = 0
    model: str | None = None
    status: str = "success"  # success, partial, failed
    errors: list[str] = Field(default_factory=list)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
