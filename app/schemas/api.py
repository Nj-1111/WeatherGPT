"""Versioned public contracts.  Internal services do not accept untyped dictionaries."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from app.schemas.location import ResolvedLocation


class LocationInput(BaseModel):
    raw: str | None = Field(default=None, max_length=256)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)

    @field_validator("raw")
    @classmethod
    def strip_raw(cls, value: str | None) -> str | None:
        return value.strip() if value else value

    def has_coordinates(self) -> bool:
        return self.latitude is not None and self.longitude is not None


class QueryRequestV1(BaseModel):
    question: str = Field(min_length=1, max_length=4096)
    location: LocationInput | None = None
    user_id: str | None = Field(default=None, max_length=128)
    # Conversational follow-up short-circuit key (services/session_router.py). Distinct
    # from user_id: one user may run several concurrent conversations, and a follow-up
    # must only ever reuse context from its own conversation.
    session_id: str | None = Field(default=None, max_length=128)
    # None means "not set" — distinct from an explicit "en" override — so the pipeline can
    # tell "caller wants English" apart from "caller didn't say," and fall back to the
    # guardrail's detected language only in the latter case (see services/i18n.py).
    language: str | None = Field(default=None, max_length=16)
    profile: dict[str, Any] = Field(default_factory=dict)
    timezone: str | None = Field(default=None, max_length=64)


class DecisionRequest(QueryRequestV1):
    decision_type: Literal["spray", "irrigate", "harvest", "travel", "marine"] | None = None


class ContextFactInput(BaseModel):
    fact: str = Field(min_length=1, max_length=64)
    value: Any
    confidence: float = Field(default=0.9, ge=0, le=1)
    source: Literal["user", "system", "import"] = "user"
    confirmed: bool = True
    expiry: datetime | None = None


class ContextRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=128)
    fact: ContextFactInput


class FeedbackRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=128)
    decision_id: str | None = None
    actual_outcome: dict[str, Any]
    user_feedback: str | None = Field(default=None, max_length=2048)


class LocationOnlyResponse(BaseModel):
    """The ACCEPT_LOCATION_ONLY branch's one response shape, identical across every
    endpoint — a coordinates-only answer doesn't change meaning depending on which route
    happened to receive it."""
    answer: str
    location: ResolvedLocation
    request_id: str
