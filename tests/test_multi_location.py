"""Multi-location/multi-time query decomposition: pairing-mode resolution and the
additive `comparisons` response field."""
import asyncio
from datetime import datetime, timedelta, timezone

import httpx

from app.main import _resolve_pairs, app
from app.schemas.ceo import CanonicalEvidenceObject, Geometry, Provenance
from app.schemas.location import ResolvedLocation
from app.schemas.query import GuardrailAction, GuardrailDecision


def test_no_locations_or_times_falls_back_to_a_single_none_pair():
    assert _resolve_pairs([], [], "locations_x_shared_time", 6) == [(None, None)]


def test_locations_x_shared_time_pairs_every_location_with_the_one_time():
    pairs = _resolve_pairs(["Delhi", "Mumbai"], ["this weekend"], "locations_x_shared_time", 6)
    assert pairs == [("Delhi", "this weekend"), ("Mumbai", "this weekend")]


def test_times_x_shared_location_pairs_the_one_location_with_every_time():
    pairs = _resolve_pairs(["Pune"], ["today", "tomorrow"], "times_x_shared_location", 6)
    assert pairs == [("Pune", "today"), ("Pune", "tomorrow")]


def test_full_cross_product_zips_equal_length_lists():
    pairs = _resolve_pairs(["Mumbai", "Delhi"], ["tomorrow", "Friday"], "full_cross_product", 6)
    assert pairs == [("Mumbai", "tomorrow"), ("Delhi", "Friday")]


def test_full_cross_product_falls_back_when_lengths_mismatch():
    """A malformed full_cross_product (unequal list lengths) must not crash zip(strict=True)
    — it falls back to the safe locations_x_shared_time behavior instead."""
    pairs = _resolve_pairs(["Mumbai", "Delhi", "Chennai"], ["tomorrow"], "full_cross_product", 6)
    assert pairs == [("Mumbai", "tomorrow"), ("Delhi", "tomorrow"), ("Chennai", "tomorrow")]


def test_max_pairs_caps_the_result():
    pairs = _resolve_pairs(["A", "B", "C", "D"], ["today"], "locations_x_shared_time", 2)
    assert len(pairs) == 2


def _fixture_evidence():
    now = datetime.now(timezone.utc) + timedelta(days=1)
    geometry = Geometry(type="Point", coordinates=[76.3, 9.9])
    provenance = Provenance(original_source="fixture", transformations=["fixture test"])
    return [CanonicalEvidenceObject(source="OPEN_METEO", evidence_class="forecast",
                                    variable="temperature_2m", value=29.0, unit="C", statistic="instant",
                                    geometry=geometry, valid_from=now, valid_to=now, provenance=provenance)]


async def _post(path, body):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        return await client.post(path, json=body)


def test_multi_location_query_populates_comparisons(monkeypatch):
    async def fake_retrieve(*args, **kwargs):
        return _fixture_evidence(), {"sources": {"fixture": {"status": "ok"}}, "partial": False}
    monkeypatch.setattr("app.main.retrieve", fake_retrieve)

    async def fake_run_guardrail(text):
        return GuardrailDecision(original_text=text, action=GuardrailAction.ACCEPT_WEATHER_FULL,
                                 locations=["Kochi", "Chennai"], time_phrases=["tomorrow"],
                                 pairing_mode="locations_x_shared_time", confidence=0.9)
    monkeypatch.setattr("app.main.run_guardrail", fake_run_guardrail)

    async def fake_resolve_location(phrase):
        return ResolvedLocation(raw=phrase, lat=13.08, lon=80.27, timezone="Asia/Kolkata", normalized_name=phrase)
    monkeypatch.setattr("app.services.location_resolver.resolve_location", fake_resolve_location)

    body = {"question": "compare Kochi and Chennai tomorrow", "location": {"latitude": 9.9, "longitude": 76.3}}
    response = asyncio.run(_post("/query", body))
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["comparisons"] is not None
    assert len(data["comparisons"]) == 1
    assert data["comparisons"][0]["query"]["resolved_location"]["normalized_name"] == "Chennai"


def test_single_location_query_leaves_comparisons_none(monkeypatch):
    async def fake_retrieve(*args, **kwargs):
        return _fixture_evidence(), {"sources": {"fixture": {"status": "ok"}}, "partial": False}
    monkeypatch.setattr("app.main.retrieve", fake_retrieve)

    async def fake_run_guardrail(text):
        return GuardrailDecision(original_text=text, action=GuardrailAction.ACCEPT_WEATHER_FULL,
                                 locations=["Kochi"], time_phrases=["tomorrow"], confidence=0.9)
    monkeypatch.setattr("app.main.run_guardrail", fake_run_guardrail)

    body = {"question": "weather in Kochi tomorrow", "location": {"latitude": 9.9, "longitude": 76.3}}
    response = asyncio.run(_post("/query", body))
    assert response.status_code == 200, response.text
    assert response.json()["comparisons"] is None
