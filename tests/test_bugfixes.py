"""Regression tests for the audited bugs. Each names the failure it prevents returning."""
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.adapters.registry import REGISTRY, health_all
from app.main import app
from app.schemas.ceo import CanonicalEvidenceObject, Geometry, Provenance
from app.services.cache import TTLCache
from app.services.rate_limit import RateLimiter
from app.services.time_parser import parse_time_window
from app.services.units import as_kmh
from app.services.variable_registry import validate_semantics

NOW = datetime(2026, 9, 3, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata"))


@pytest.mark.parametrize("question", [
    "maybe 5 people will spray, will it rain",
    "mayhem 3 days ago",
    "the mayor visits, will it rain",
])
def test_a1_a_word_starting_with_a_month_is_not_a_date(question):
    """'maybe 5' parsed as May 5th of next year, at confidence 0.8."""
    valid_from, _, _, _ = parse_time_window(question, now=NOW)
    assert valid_from.month != 5 or valid_from.date() == NOW.date()


@pytest.mark.parametrize("question,month,day", [
    ("will it rain on 5 aug", 8, 5),
    ("will it rain on august 5", 8, 5),
    ("rain on 15 september", 9, 15),
    ("rain on 3rd oct", 10, 3),
])
def test_a1_real_dates_still_parse_including_full_month_names(question, month, day):
    valid_from, _, _, confidence = parse_time_window(question, now=NOW)
    assert (valid_from.month, valid_from.day) == (month, day)
    assert confidence == 0.8


@pytest.mark.parametrize("variable,statistic,unit,evidence_class", [
    ("thunderstorm_probability", "categorical", None, "nowcast"),
    ("thunderstorm_probability", "probability", "%", "forecast"),
    ("wind_direction", "instant", "deg", "forecast"),
    ("rainfall_distribution", "instant", "members", "forecast"),
    ("other", "instant", None, "forecast"),
])
def test_a3_registered_variables_survive_the_semantic_gate(variable, statistic, unit, evidence_class):
    """These were rejected as 'unknown canonical variable', binning every IMD nowcast."""
    ok, reason = validate_semantics(variable, statistic, unit, evidence_class, None)
    assert ok, reason


def test_a3_the_gate_still_rejects_a_genuinely_unknown_variable():
    ok, reason = validate_semantics("nonsense", "instant", None, "forecast", None)
    assert not ok and "unknown canonical variable" in reason


def _fresh_health_cache(monkeypatch):
    """health_all() now caches its result; each test that wants a real probe needs its
    own empty cache instead of possibly reusing another test's cached outcome."""
    monkeypatch.setattr("app.adapters.registry._health_cache", TTLCache(max_entries=1))


def test_b1_health_probes_every_adapter_concurrently(monkeypatch):
    _fresh_health_cache(monkeypatch)
    result = asyncio.run(health_all())
    assert set(result) == set(REGISTRY)


def test_b1_one_raising_adapter_cannot_break_the_others(monkeypatch):
    """gather(return_exceptions=True) must isolate exactly as the old loop did."""
    _fresh_health_cache(monkeypatch)
    class Exploding:
        async def health_check(self):
            raise RuntimeError("boom")
    monkeypatch.setitem(REGISTRY, "OPEN_METEO", Exploding())
    result = asyncio.run(health_all())
    assert result["OPEN_METEO"] == {"available": False, "reason": "boom"}
    assert len(result) == len(REGISTRY)


def test_health_result_is_cached_within_ttl(monkeypatch):
    """§2.9: repeat /health calls must not re-trigger the 8-way upstream fan-out."""
    _fresh_health_cache(monkeypatch)
    calls = 0

    class Counting:
        async def health_check(self):
            nonlocal calls
            calls += 1
            return {"available": True}
    monkeypatch.setitem(REGISTRY, "OPEN_METEO", Counting())
    asyncio.run(health_all())
    asyncio.run(health_all())
    assert calls == 1


def test_b2_grib2_adapter_uses_the_shared_pool():
    source = __import__("pathlib").Path("app/adapters/grib2_adapter.py").read_text()
    assert "httpx.AsyncClient(" not in source
    assert "get_client()" in source


def _wind(value: float, unit: str) -> CanonicalEvidenceObject:
    return CanonicalEvidenceObject(
        source="MET_NORWAY", evidence_class="forecast", variable="wind_speed", value=value,
        unit=unit, statistic="instant", geometry=Geometry(type="Point", coordinates=[75.8, 22.7]),
        provenance=Provenance(original_source="test"))


def test_c1_one_conversion_shared_by_fusion_and_verification():
    """Two copies could drift, and the reviewer would then reject correct wind claims."""
    from app.agents import verification
    from app.services import wio_builder
    assert wio_builder.as_kmh is verification.as_kmh is as_kmh
    assert as_kmh(_wind(5.0, "m/s")) == 18.0
    assert as_kmh(_wind(18.0, "km/h")) == 18.0


def test_c3_dead_adapter_metadata_is_gone():
    for adapter in REGISTRY.values():
        assert not hasattr(adapter, "supported_variables")
        assert not hasattr(adapter, "supported_evidence_classes")
        assert adapter.source_name


def test_c3_dead_resolved_location_fields_are_gone():
    from app.schemas.location import ResolvedLocation
    fields = set(ResolvedLocation.model_fields)
    assert not fields & {"block", "candidates", "ambiguity_status"}


def test_c4_gazetteer_lives_with_its_only_consumer():
    from app.services.location_resolver import seed
    assert seed.seed_place("nagpur")["lat"] == pytest.approx(21.1458)
    assert not hasattr(__import__("app.schemas.location", fromlist=["x"]), "GAZETTEER")


async def _post(body):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        return await client.post("/wio/query", json=body)


@pytest.mark.parametrize("location,missing", [
    ({"latitude": 21.1}, "longitude"),
    ({"longitude": 79.0}, "latitude"),
])
def test_d1_a_half_coordinate_names_the_missing_half(location, missing):
    response = asyncio.run(_post({"question": "will it rain tomorrow", "location": location}))
    body = response.json()["error"]
    assert response.status_code == 422 and body["code"] == "LOCATION_INCOMPLETE"
    assert missing in body["message"] and body["details"]["missing"] == missing


def test_d1_a_coordinate_alongside_a_place_name_still_resolves_by_name(monkeypatch):
    """Conservative by design: this used to work and must keep working."""
    async def fake_resolve(raw):
        from app.schemas.location import ResolvedLocation
        return ResolvedLocation(raw=raw, lat=21.14, lon=79.08, timezone="Asia/Kolkata")

    async def fake_retrieve(*args, **kwargs):
        return [], {"sources": {}, "partial": False}
    monkeypatch.setattr("app.main.resolve_location", fake_resolve)
    monkeypatch.setattr("app.main.retrieve", fake_retrieve)
    response = asyncio.run(_post({"question": "will it rain tomorrow",
                                  "location": {"raw": "Nagpur", "latitude": 21.1}}))
    assert response.status_code == 200


def test_d2_the_daily_window_rolls_even_while_a_client_is_being_refused():
    limiter = RateLimiter(per_minute=1, per_day=100)
    day_start = 86400.0 * 20000
    assert limiter.check("c", day_start) is None
    assert limiter.check("c", day_start + 1) is not None       # refused
    _, _, day_before, _ = limiter._clients["c"]
    limiter.check("c", day_start + 86400)                       # next day
    _, _, day_after, count_after = limiter._clients["c"]
    assert day_after == day_before + 1 and count_after == 1


def test_d2_refusal_does_not_consume_more_budget():
    limiter = RateLimiter(per_minute=2, per_day=100)
    now = 86400.0 * 20000
    assert limiter.check("c", now) is None
    assert limiter.check("c", now) is None
    for _ in range(5):
        assert limiter.check("c", now) is not None
    _, minute_count, _, day_count = limiter._clients["c"]
    assert minute_count == 2 and day_count == 2


# --- A4: absolute dates and clock times leaked into the geocoded place name -------------

@pytest.mark.parametrize("question,place", [
    ("rainfall in Rajkot on 2026-08-01", "Rajkot"),
    ("will it rain in Rajkot on 5 aug", "Rajkot"),
    ("forecast in Pune on august 5", "Pune"),
    ("weather in Rajkot at 5pm", "Rajkot"),
    ("weather in Rajkot at 17:00", "Rajkot"),
    ("temperature in Delhi at 5 p.m.", "Delhi"),
    ("rain in Indore at 6", "Indore"),
    ("weather in Nagpur on monday", "Nagpur"),
])
def test_a4_dates_and_times_are_stripped_from_the_place_name(question, place):
    """These reached the geocoder as 'Rajkot on 2026-08-01' and 404'd after 1.26s."""
    from app.services.location_resolver.normalize import extract_place_phrase
    assert extract_place_phrase(question) == place


@pytest.mark.parametrize("question,expected", [
    ("weather in March", "March"),                     # a real town in Cambridgeshire
    ("weather in Rajkot tomorrow", "Rajkot"),
    ("rain in Rajkot in 6 hours", "Rajkot"),
    ("weather in Springfield next week", "Springfield"),
    ("will it rain in Nashik this weekend", "Nashik"),
    ("weather in New Delhi", "New Delhi"),
])
def test_a4_place_names_that_already_worked_are_untouched(question, expected):
    from app.services.location_resolver.normalize import extract_place_phrase
    assert extract_place_phrase(question) == expected


@pytest.mark.parametrize("question,place", [
    ("is a cyclone coming to Chennai this week", "Chennai"),
    ("safest route to Pune", "Pune"),
    ("will the flood reach to Patna", "Patna"),
])
def test_extract_place_phrase_recognizes_to_as_a_lead_preposition(question, place):
    """Disaster/route phrasing ("coming to X", "route to Y") was silently unextractable —
    the guardrail broadening accepted these topics but location resolution then 422'd."""
    from app.services.location_resolver.normalize import extract_place_phrase
    assert extract_place_phrase(question) == place


@pytest.mark.parametrize("question,month,day", [
    ("rainfall in Rajkot on 2026-08-01", 8, 1),
    ("will it rain in Rajkot on 5 aug", 8, 5),
    ("forecast in Pune on august 5", 8, 5),
])
def test_a4_the_date_still_reaches_the_time_parser(question, month, day):
    """The two stages read the same question independently: stripping the date from the
    place name must not remove it from the window the time parser resolves."""
    valid_from, _, _, confidence = parse_time_window(question, now=NOW)
    assert (valid_from.month, valid_from.day) == (month, day)
    assert confidence >= 0.8


def test_a4_a_dated_question_resolves_end_to_end_without_a_geocoder_failure(monkeypatch):
    """Before the fix this was 404 LOCATION_NOT_FOUND after two wasted upstream calls."""
    seen = {}

    async def fake_resolve(raw):
        from app.schemas.location import ResolvedLocation
        seen["raw"] = raw
        return ResolvedLocation(raw=raw, lat=22.30, lon=70.80, timezone="Asia/Kolkata",
                                normalized_name=raw)

    async def fake_retrieve(*args, **kwargs):
        return [], {"sources": {}, "partial": False}

    monkeypatch.setattr("app.services.location_resolver.resolve_location", fake_resolve)
    monkeypatch.setattr("app.main.retrieve", fake_retrieve)
    response = asyncio.run(_post({"question": "rainfall in Rajkot on 2026-08-01"}))
    assert response.status_code == 200, response.text
    assert seen["raw"] == "Rajkot"
    assert response.json()["wio"]["query"]["valid_from"].startswith("2026-08-01")


def test_a4_the_month_pattern_has_one_owner():
    """normalize.py discards dates, time_parser.py parses them — one definition of what a
    month looks like, or the two drift and this bug returns in a new shape."""
    from app.services.location_resolver import normalize
    from app.services.time_parser import MONTH_PATTERN
    assert normalize.MONTH_PATTERN is MONTH_PATTERN
