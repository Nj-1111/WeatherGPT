import asyncio

import pytest

from app.services.location_resolver import (
    LocationAmbiguousError,
    LocationNotFoundError,
    resolve_location,
)
from app.services.location_resolver.cache import location_cache
from app.services.location_resolver.providers.base import LocationCandidate


@pytest.fixture(autouse=True)
def _isolate_cache():
    """Every test starts with an empty location cache so ordering never matters."""
    location_cache._entries.clear()
    location_cache.hits = 0
    location_cache.misses = 0
    yield
    location_cache._entries.clear()


def _candidate(name, lat, lon, country_code="IN", population=None, **kw):
    return LocationCandidate(name=name, lat=lat, lon=lon, country_code=country_code,
                             population=population, provider="stub", **kw)


class _StubGeocoder:
    """Stands in for a live provider. Records calls so cache behaviour is testable."""
    name = "stub"

    def __init__(self, results=None, error=None):
        self.results = results or []
        self.error = error
        self.calls = 0

    async def search(self, query):
        self.calls += 1
        if self.error:
            raise self.error
        return list(self.results)


@pytest.fixture
def stub_chain(monkeypatch):
    """Replace the real provider chain; returns a setter for the stubs under test."""
    def _install(*providers):
        monkeypatch.setattr("app.services.location_resolver._GEOCODERS", tuple(providers))
        return providers
    return _install


# --- coordinates (no network at all) ---------------------------------------

def test_coordinates_resolve_without_providers(stub_chain):
    never = _StubGeocoder(error=AssertionError("must not be called for coordinates"))
    stub_chain(never)
    loc = asyncio.run(resolve_location("21.14,79.08"))
    assert (loc.lat, loc.lon) == (21.14, 79.08)
    assert loc.resolution_method == "coordinates"
    assert never.calls == 0


def test_coordinate_boundaries_accepted(stub_chain):
    stub_chain(_StubGeocoder())
    assert asyncio.run(resolve_location("90,180")).lat == 90.0
    assert asyncio.run(resolve_location("-90,-180")).lon == -180.0


def test_out_of_range_coordinates_rejected(stub_chain):
    stub_chain(_StubGeocoder())
    with pytest.raises(LocationNotFoundError):
        asyncio.run(resolve_location("91.0, 200.0"))


# --- geocoded place names --------------------------------------------------

def test_indian_city_resolves_via_provider(stub_chain):
    """The original failure case: Indore was unresolvable with the old gazetteer."""
    stub_chain(_StubGeocoder([
        _candidate("Indore", 22.71792, 75.8333, "IN", population=1994397,
                   admin1="Madhya Pradesh", admin2="Indore district"),
        _candidate("Indore", 38.35288, -81.14678, "US"),
    ]))
    loc = asyncio.run(resolve_location("Indore"))
    assert (round(loc.lat, 3), round(loc.lon, 3)) == (22.718, 75.833)
    assert loc.state == "Madhya Pradesh"
    assert loc.resolution_method == "geocoded"


def test_india_is_preferred_over_larger_foreign_match(stub_chain):
    """India bias must beat raw population for same-named places."""
    stub_chain(_StubGeocoder([
        _candidate("Patna", 55.36406, -4.50594, "GB", population=2190),
        _candidate("Patna", 25.59408, 85.13563, "IN", population=1684297, admin1="Bihar"),
    ]))
    loc = asyncio.run(resolve_location("Patna"))
    assert loc.state == "Bihar"


def test_india_wins_hard_tier_even_when_foreign_population_dwarfs_it(stub_chain):
    """The hard tier, not a soft bonus: a +2.0 additive bonus can be outweighed by a big
    enough foreign population gap (log10(10_000_000)=7.0 vs log10(90_000)+2=6.95 — the
    foreign candidate used to win by 0.05). The tier makes this impossible regardless of
    the population gap — the exact shape of the live "Kochi" -> Japan failure this closes.
    """
    stub_chain(_StubGeocoder([
        _candidate("Kochi", 33.5597, 133.5311, "JP", population=10_000_000),
        _candidate("Kochi", 9.9312, 76.2673, "IN", population=90_000, admin1="Kerala"),
    ]))
    loc = asyncio.run(resolve_location("Kochi"))
    assert loc.state == "Kerala"


def test_single_implausible_candidate_is_not_auto_dominant(stub_chain):
    """A lone candidate with no population and no exact-name match (the shape of a fuzzy
    free-text match on a typo or nonsense query) must not be silently confident — it
    should read as ambiguous rather than a guessed answer."""
    stub_chain(_StubGeocoder([
        _candidate("Some Unrelated Village", 4.5, 11.5, "CM", population=None),
    ]))
    with pytest.raises(LocationAmbiguousError):
        asyncio.run(resolve_location("Zzqxplace"))


def test_global_location_still_resolves(stub_chain):
    stub_chain(_StubGeocoder([
        _candidate("London", 51.50853, -0.12574, "GB", population=8961989,
                   feature_code="PPLC", country="United Kingdom"),
        _candidate("London", 42.98339, -81.23304, "CA", population=422324),
    ]))
    loc = asyncio.run(resolve_location("London"))
    assert round(loc.lat, 2) == 51.51
    assert loc.country == "United Kingdom"


def test_small_town_resolves_when_only_candidate(stub_chain):
    stub_chain(_StubGeocoder([
        _candidate("Rajgir", 25.02828, 85.42079, "IN", population=41587,
                   admin1="Bihar", admin2="Nalanda"),
    ]))
    loc = asyncio.run(resolve_location("Rajgir"))
    assert loc.district == "Nalanda"


# --- normalization ---------------------------------------------------------

@pytest.mark.parametrize("query", ["Patna", "  patna  ", "PATNA", "near Patna", "weather in Patna"])
def test_normalization_variants_reach_same_place(stub_chain, query):
    stub_chain(_StubGeocoder([
        _candidate("Patna", 25.59408, 85.13563, "IN", population=1684297, admin1="Bihar"),
    ]))
    loc = asyncio.run(resolve_location(query))
    assert round(loc.lat, 3) == 25.594


def test_historical_alias_is_normalized(stub_chain):
    """'Bombay' geocodes to Bombay, New York without alias normalization."""
    stub = _StubGeocoder([
        _candidate("Mumbai", 19.07, 72.87, "IN", population=12691836, admin1="Maharashtra"),
    ])
    stub_chain(stub)
    loc = asyncio.run(resolve_location("Bombay"))
    assert loc.state == "Maharashtra"


@pytest.mark.parametrize("typo, canonical", [
    ("bnglr", "Bengaluru"), ("blr", "Bengaluru"), ("mum", "Mumbai"),
    ("chenai", "Chennai"), ("hyd", "Hyderabad"), ("del", "Delhi"),
    ("dilli", "Delhi"), ("cbe", "Coimbatore"), ("vizag", "Visakhapatnam"),
])
def test_expanded_india_aliases_normalize_to_canonical_name(typo, canonical):
    """chenai -> Chennai is the exact live-verified typo that used to resolve to France."""
    from app.services.input_pipeline.normalize import normalize_query
    assert normalize_query(typo) == canonical


@pytest.mark.parametrize("question", [
    "weather near me", "what's the weather here", "my location weather",
    "current weather at my location",
])
def test_extract_place_phrase_rejects_self_referential_phrases(question):
    """"near me"/"here" are not place names — a free-text geocoder used to fuzzy-match
    "near me" to an unrelated foreign village (verified live: Cameroon)."""
    from app.services.input_pipeline.normalize import extract_place_phrase
    assert extract_place_phrase(question) is None


def test_resolve_location_rejects_near_me_before_hitting_a_geocoder(stub_chain):
    """Backstop for when a caller (the guardrail's LLM extraction) hands 'near me'
    straight to the resolver, bypassing extract_place_phrase entirely."""
    stub = _StubGeocoder([_candidate("Some Unrelated Village", 4.5, 11.5, "CM")])
    stub_chain(stub)
    with pytest.raises(LocationNotFoundError):
        asyncio.run(resolve_location("near me"))
    assert stub.calls == 0


# --- ambiguity -------------------------------------------------------------

def test_ambiguous_place_raises_with_candidates(stub_chain):
    """Springfield: comparable populations, no dominant winner -> ambiguous, not a guess."""
    stub_chain(_StubGeocoder([
        _candidate("Springfield", 37.21533, -93.29824, "US", population=170188, admin1="Missouri"),
        _candidate("Springfield", 39.80172, -89.64371, "US", population=114394, admin1="Illinois"),
    ]))
    with pytest.raises(LocationAmbiguousError) as exc:
        asyncio.run(resolve_location("Springfield"))
    assert len(exc.value.candidates) == 2
    assert {c["state"] for c in exc.value.candidates} == {"Missouri", "Illinois"}


def test_dominant_candidate_is_auto_selected(stub_chain):
    """A clearly dominant match must NOT raise ambiguity."""
    stub_chain(_StubGeocoder([
        _candidate("Delhi", 28.65195, 77.23149, "IN", population=10927986, admin1="Delhi"),
        _candidate("Delhi", 42.85033, -80.5, "CA", population=None),
    ]))
    assert asyncio.run(resolve_location("Delhi")).state == "Delhi"


def test_second_provider_can_resolve_first_providers_ambiguity(stub_chain):
    """Nominatim rescues district/state names Open-Meteo answers ambiguously."""
    vague = _StubGeocoder([
        _candidate("Nalanda", 7.6606, 80.6424, "LK"),
        _candidate("Nalanda", -6.57333, 20.30692, "CD"),
    ])
    precise = _StubGeocoder([
        _candidate("Nalanda", 25.1364, 85.4436, "IN", population=2877653, admin1="Bihar"),
    ])
    stub_chain(vague, precise)
    loc = asyncio.run(resolve_location("Nalanda"))
    assert loc.state == "Bihar"
    assert precise.calls == 1


# --- provider failure isolation -------------------------------------------

def test_first_provider_failure_falls_through_to_second(stub_chain):
    import httpx
    broken = _StubGeocoder(error=httpx.ConnectError("dns failure"))
    working = _StubGeocoder([
        _candidate("Indore", 22.71792, 75.8333, "IN", population=1994397, admin1="Madhya Pradesh"),
    ])
    stub_chain(broken, working)
    assert asyncio.run(resolve_location("Indore")).state == "Madhya Pradesh"
    assert broken.calls == 1 and working.calls == 1


def test_provider_timeout_is_isolated(stub_chain):
    import httpx
    stub_chain(_StubGeocoder(error=httpx.ReadTimeout("timed out")),
               _StubGeocoder(error=httpx.ReadTimeout("timed out")))
    with pytest.raises(LocationNotFoundError):
        asyncio.run(resolve_location("Utterly Unknown Placename"))


def test_all_providers_down_falls_back_to_offline_seed(stub_chain):
    """Seed data keeps the core demo cities working with zero network."""
    import httpx
    stub_chain(_StubGeocoder(error=httpx.ConnectError("offline")))
    loc = asyncio.run(resolve_location("nagpur"))
    assert loc.source == "gazetteer_seed"
    assert round(loc.lat, 3) == 21.146


def test_unknown_place_with_all_providers_down_still_raises(stub_chain):
    """Never fabricate coordinates, even offline."""
    import httpx
    stub_chain(_StubGeocoder(error=httpx.ConnectError("offline")))
    with pytest.raises(LocationNotFoundError):
        asyncio.run(resolve_location("AtlantisXYZ"))


def test_malformed_provider_response_does_not_crash(stub_chain):
    """A provider returning junk rows must degrade, not raise."""
    class _Malformed:
        name = "malformed"

        async def search(self, query):
            from app.services.location_resolver.providers.open_meteo import OpenMeteoGeocoder
            geocoder = OpenMeteoGeocoder()
            return [c for c in (geocoder._to_candidate(x) for x in
                                [{"no": "coords"}, "not-a-dict", None]) if c]

    stub_chain(_Malformed())
    with pytest.raises(LocationNotFoundError):
        asyncio.run(resolve_location("Somewhere Unmapped"))


# --- caching ---------------------------------------------------------------

def test_second_equivalent_request_hits_cache(stub_chain):
    stub = _StubGeocoder([
        _candidate("Patna", 25.59408, 85.13563, "IN", population=1684297, admin1="Bihar"),
    ])
    stub_chain(stub)
    asyncio.run(resolve_location("Patna"))
    asyncio.run(resolve_location("  PATNA  "))
    assert stub.calls == 1, "normalized-equivalent query should not re-hit the provider"
    assert location_cache.hits == 1


# --- Geoapify provider -------------------------------------------------------

def test_geoapify_is_tried_first_in_the_chain():
    from app.services.location_resolver import _GEOCODERS
    assert _GEOCODERS[0].name == "geoapify"


def test_geoapify_returns_empty_without_api_key(monkeypatch):
    import dataclasses

    from app.config import settings
    from app.services.location_resolver.providers.geoapify import GeoapifyGeocoder
    monkeypatch.setattr("app.services.location_resolver.providers.geoapify.settings",
                        dataclasses.replace(settings, geoapify_api_key=""))
    result = asyncio.run(GeoapifyGeocoder().search("Bangalore"))
    assert result == []


def test_geoapify_to_candidate_parses_full_result():
    from app.services.location_resolver.providers.geoapify import GeoapifyGeocoder
    candidate = GeoapifyGeocoder()._to_candidate({
        "lat": 12.9716, "lon": 77.5946, "city": "Bengaluru", "country": "India",
        "country_code": "in", "state": "Karnataka", "county": "Bengaluru Urban",
        "postcode": "560001", "result_type": "city",
        "timezone": {"name": "Asia/Kolkata"},
    })
    assert candidate is not None
    assert candidate.name == "Bengaluru"
    assert (candidate.lat, candidate.lon) == (12.9716, 77.5946)
    assert candidate.country_code == "IN"
    assert candidate.admin1 == "Karnataka"
    assert candidate.admin2 == "Bengaluru Urban"
    assert candidate.feature_code == "PPLA"
    assert candidate.timezone == "Asia/Kolkata"
    assert candidate.provider == "geoapify"


def test_geoapify_to_candidate_falls_back_to_formatted_name():
    from app.services.location_resolver.providers.geoapify import GeoapifyGeocoder
    candidate = GeoapifyGeocoder()._to_candidate({
        "lat": 12.9716, "lon": 77.5946, "formatted": "Bengaluru, Karnataka, India",
    })
    assert candidate is not None
    assert candidate.name == "Bengaluru"
    assert candidate.feature_code == "PPL"  # no result_type -> not a recognized capital type


@pytest.mark.parametrize("item", [{"lat": 12.9}, {"lat": "x", "lon": "y", "city": "A"}, "not-a-dict", None])
def test_geoapify_to_candidate_skips_malformed(item):
    from app.services.location_resolver.providers.geoapify import GeoapifyGeocoder
    assert GeoapifyGeocoder()._to_candidate(item) is None
