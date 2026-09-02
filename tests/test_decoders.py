from app.decoders.cap_decoder import decode_cap_xml
from app.decoders.imd_json import decode_city_forecast, decode_warning


def test_imd_city():
    rec = {"city":"Nagpur","lat":21.14,"lon":79.08,"forecast_date":"2026-09-01T00:00:00+05:30","rainfall":12,"temp_max":32,"issued_at":"2026-08-31T06:00:00+05:30"}
    ceos = decode_city_forecast(rec)
    assert any(c.variable=="precipitation_amount" for c in ceos)
    assert any(c.accumulation_window_hours==24 for c in ceos)

def test_imd_warning():
    rec = {"district":"Nagpur","hazard":"heavy rainfall","colour":"orange","valid_from":"2026-09-01T00:00:00+05:30","valid_to":"2026-09-01T12:00:00+05:30","issue_time":"2026-08-31T06:00:00+05:30"}
    ceos = decode_warning(rec)
    assert ceos[0].warning_severity=="orange"
    assert ceos[0].evidence_class=="warning"

def test_cap():
    xml = b"""<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<alert xmlns=\"urn:oasis:names:tc:emergency:cap:1.2\">\n<identifier>TEST-1</identifier><sender>imd@test</sender><sent>2026-08-31T06:00:00+05:30</sent><status>Actual</status><msgType>Alert</msgType>\n<info><event>Heavy Rainfall</event><severity>Severe</severity><effective>2026-09-01T00:00:00+05:30</effective><expires>2026-09-01T12:00:00+05:30</expires><area><areaDesc>Nagpur</areaDesc></area></info></alert>"""
    ceos = decode_cap_xml(xml)
    assert len(ceos)==1
    assert ceos[0].warning_severity=="orange"


def test_imd_record_without_coordinates_is_skipped_not_placed_in_nagpur():
    """A missing position used to inherit Nagpur's, so the record ranked as local
    evidence for every user in India."""
    assert decode_city_forecast({"city": "Somewhere", "rainfall": 12}) == []


def test_cap_event_selects_the_matching_warning_variable():
    from app.decoders.cap_decoder import decode_cap_xml
    def alert(event):
        return (f'<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2"><identifier>x</identifier>'
                f'<sender>s</sender><sent>2026-09-01T00:00:00Z</sent><status>Actual</status>'
                f'<msgType>Alert</msgType><info><event>{event}</event><severity>Severe</severity>'
                f'<area><areaDesc>A</areaDesc></area></info></alert>').encode()
    assert decode_cap_xml(alert("Cyclone Warning"))[0].variable == "cyclone_warning"
    assert decode_cap_xml(alert("Thunderstorm"))[0].variable == "thunderstorm_warning"
    assert decode_cap_xml(alert("Heavy Rainfall"))[0].variable == "heavy_rain_warning"
    assert decode_cap_xml(alert("Flood"))[0].variable == "flood_warning"


def test_cap_polygon_is_preserved_and_bounds_the_warning():
    from app.decoders.cap_decoder import decode_cap_xml
    from app.services.spatial_match import covers_query
    xml = (b'<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2"><identifier>x</identifier>'
           b'<sender>s</sender><sent>2026-09-01T00:00:00Z</sent><status>Actual</status><msgType>Alert</msgType>'
           b'<info><event>Heavy Rainfall</event><severity>Severe</severity><onset>2026-09-01T06:00:00Z</onset>'
           b'<area><areaDesc>Box</areaDesc><polygon>20.0,70.0 20.0,80.0 30.0,80.0 30.0,70.0 20.0,70.0</polygon>'
           b'</area></info></alert>')
    ceo = decode_cap_xml(xml)[0]
    assert len(ceo.geometry.coordinates) == 5
    assert covers_query(ceo, 25.0, 75.0) is True
    assert covers_query(ceo, 13.0, 80.3) is False
    assert ceo.valid_from.hour == 6, "onset must win over the issue time"
