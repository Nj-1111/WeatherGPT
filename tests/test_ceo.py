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
