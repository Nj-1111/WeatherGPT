from datetime import datetime, timedelta, timezone

from app.schemas.ceo import CanonicalEvidenceObject, Geometry, Provenance
from app.services.wio_builder import build_wio

IST = timezone(timedelta(hours=5, minutes=30))

def test_ceo_roundtrip():
    ceo = CanonicalEvidenceObject(
        source="IMD", evidence_class="forecast", variable="precipitation_amount",
        value=12.0, unit="mm", statistic="accumulation", geometry=Geometry(type="Point", coordinates=[79.08,21.14]),
        valid_from=datetime(2026,9,1,6,0,tzinfo=timezone.utc), valid_to=datetime(2026,9,1,12,0,tzinfo=timezone.utc),
        accumulation_window_hours=6, provenance=Provenance(original_source="IMD", transformations=["test"])
    )
    assert ceo.value == 12.0
    assert ceo.provenance.original_source == "IMD"

def test_wio_preserves_warning():
    from app.schemas.ceo import CanonicalEvidenceObject, Geometry, Provenance
    now = datetime.now(timezone.utc)
    ceos = [
        CanonicalEvidenceObject(source="OPEN_METEO", evidence_class="forecast", variable="precipitation_amount", value=25, unit="mm", statistic="accumulation", geometry=Geometry(type="GridCell", coordinates=[79.08,21.14]), valid_from=now, valid_to=now, accumulation_window_hours=6, provenance=Provenance(original_source="OPEN_METEO", transformations=[])),
        CanonicalEvidenceObject(source="IMD", evidence_class="warning", variable="heavy_rain_warning", value=None, raw_value="heavy rainfall", statistic="categorical", geometry=Geometry(type="Polygon", reference="Nagpur"), valid_from=now, valid_to=now, warning_severity="orange", provenance=Provenance(original_source="IMD", transformations=[])),
    ]
    vf = vt = now
    wio = build_wio("Will it rain?", {"lat":21.14,"lon":79.08, "raw":"Nagpur"}, vf, vt, "short", ceos, lang="en")
    assert wio.official_warning.active is True
    assert wio.official_warning.severity == "orange"
    assert any(e.evidence_class=="warning" for e in wio.evidence)


def _polygonless_warning(area: str):
    """Every alert in NDMA's national CAP feed ships without a polygon (31 of 31 checked
    live), carrying only this free-text area description."""
    now = datetime.now(timezone.utc)
    return CanonicalEvidenceObject(
        source="CAP", evidence_class="warning", variable="flood_warning", value=None,
        raw_value="river above danger level", statistic="categorical",
        geometry=Geometry(type="Polygon", reference=area), extra={"areas": [area]},
        valid_from=now, valid_to=now, warning_severity="orange",
        provenance=Provenance(original_source="CAP", transformations=[]))


NAGPUR = {"lat": 21.14, "lon": 79.08, "raw": "Nagpur", "district": "Nagpur",
          "state": "Maharashtra", "normalized_name": "Nagpur"}


def test_polygonless_warning_for_another_state_is_not_this_users_warning():
    """The whole national feed is polygon-less, so an untestable warning used to default
    to covering everyone: an Assam river alert showed as Nagpur's active warning and
    maxed RADE's risk aversion there."""
    now = datetime.now(timezone.utc)
    ceos = [_polygonless_warning("Brahmaputra, Dhubri, Dhubri, Assam")]
    wio = build_wio("should I spray?", NAGPUR, now, now, "short", ceos, lang="en")
    assert wio.official_warning.active is False


def test_polygonless_warning_naming_the_users_district_still_applies():
    now = datetime.now(timezone.utc)
    ceos = [_polygonless_warning("some parts of Nagpur, Wardha")]
    wio = build_wio("should I spray?", NAGPUR, now, now, "short", ceos, lang="en")
    assert wio.official_warning.active is True


def test_polygonless_warning_naming_only_the_state_still_applies():
    """A state-level alert genuinely does cover every district in it."""
    now = datetime.now(timezone.utc)
    ceos = [_polygonless_warning("isolated places over Maharashtra")]
    wio = build_wio("should I spray?", NAGPUR, now, now, "short", ceos, lang="en")
    assert wio.official_warning.active is True


def test_state_name_does_not_override_an_explicit_district_list():
    """IMD publishes district-scoped alerts as "<district>, <district> districts of
    <state>" (verified live). The trailing state names where those districts are; it
    does not put every other district in the state under warning."""
    now = datetime.now(timezone.utc)
    ceos = [_polygonless_warning("Dhule, Jalgaon, Nashik districts of Maharashtra")]
    wio = build_wio("should I spray?", NAGPUR, now, now, "short", ceos, lang="en")
    assert wio.official_warning.active is False


def test_district_list_that_does_name_the_user_still_applies():
    now = datetime.now(timezone.utc)
    ceos = [_polygonless_warning("Nagpur, Wardha districts of Maharashtra")]
    wio = build_wio("should I spray?", NAGPUR, now, now, "short", ceos, lang="en")
    assert wio.official_warning.active is True


def test_unusable_area_text_does_not_broadcast_nationwide():
    """Real publishers emit junk like "tslg"; it must match nobody rather than everybody."""
    now = datetime.now(timezone.utc)
    ceos = [_polygonless_warning("tslg")]
    wio = build_wio("should I spray?", NAGPUR, now, now, "short", ceos, lang="en")
    assert wio.official_warning.active is False


def test_polygon_still_wins_over_area_text_when_present():
    """A real polygon is authoritative: it excludes even when the text names the user."""
    now = datetime.now(timezone.utc)
    far_away = [[[88.0, 22.0], [88.5, 22.0], [88.5, 22.5], [88.0, 22.5], [88.0, 22.0]]]
    ceo = CanonicalEvidenceObject(
        source="CAP", evidence_class="warning", variable="flood_warning", value=None,
        raw_value="river warning", statistic="categorical",
        geometry=Geometry(type="Polygon", coordinates=far_away, reference="Nagpur"),
        extra={"areas": ["Nagpur"]}, valid_from=now, valid_to=now, warning_severity="orange",
        provenance=Provenance(original_source="CAP", transformations=[]))
    wio = build_wio("should I spray?", NAGPUR, now, now, "short", [ceo], lang="en")
    assert wio.official_warning.active is False
