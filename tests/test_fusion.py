"""Window aggregation, source-aware agreement, and comparability rules."""
from datetime import datetime, timedelta, timezone

from app.schemas.ceo import CanonicalEvidenceObject, Geometry, Provenance
from app.services.ranker import corroborated, detect_disagreements, rank
from app.services.wio_builder import build_wio

START = datetime(2026, 9, 4, 0, 0, tzinfo=timezone.utc)
END = START + timedelta(hours=23, minutes=59)
LOCATION = {"lat": 22.72, "lon": 75.86}


def _ceo(variable, value, hour=0, source="OPEN_METEO", window=None, member=None, unit="mm",
         statistic="accumulation", probability=None):
    valid = START + timedelta(hours=hour)
    return CanonicalEvidenceObject(
        source=source, evidence_class="forecast", variable=variable, value=value, unit=unit,
        statistic=statistic, geometry=Geometry(type="GridCell", coordinates=[75.86, 22.72]),
        issued_at=START, valid_from=valid, valid_to=valid, accumulation_window_hours=window,
        ensemble_member=member, probability=probability,
        provenance=Provenance(original_source=source))


def _hourly_rain(values, source="OPEN_METEO"):
    return [_ceo("precipitation_amount", value, hour, source, window=1) for hour, value in enumerate(values)]


def test_rain_is_summed_over_the_window_not_sampled_from_one_hour():
    ceos = _hourly_rain([0.0, 0.0, 1.2, 0.8, 0.4])
    wio = build_wio("will it rain tomorrow", LOCATION, START, END, "short", ceos)
    assert wio.weather.rain["value_mm"] == 2.4
    assert wio.weather.rain["hours_counted"] == 5
    assert wio.weather.rain["peak_hourly_mm"] == 1.2
    assert len(wio.weather.rain["evidence_ids"]) == 5


def test_probability_reported_is_the_window_peak_and_pairs_with_the_total():
    ceos = _hourly_rain([0.0, 2.4])
    ceos += [_ceo("precipitation_probability", 0.1, 0, unit="probability", statistic="probability", probability=0.1),
             _ceo("precipitation_probability", 0.75, 1, unit="probability", statistic="probability", probability=0.75)]
    wio = build_wio("will it rain tomorrow", LOCATION, START, END, "short", ceos)
    assert wio.weather.rain["probability"] == 0.75
    assert wio.weather.rain["value_mm"] == 2.4
    assert "75%" in wio.weather.summary and "2.4 mm" in wio.weather.summary


def test_accumulation_windows_are_never_summed_together():
    """A 1h and a 6h record cover overlapping time; adding them double-counts."""
    ceos = _hourly_rain([1.0, 1.0]) + [_ceo("precipitation_amount", 12.0, 0, window=6)]
    wio = build_wio("rain tomorrow", LOCATION, START, END, "short", ceos)
    assert wio.weather.rain["value_mm"] == 2.0
    assert wio.weather.rain["accumulation_hours"] == 1


def test_temperature_is_a_range_over_the_window():
    ceos = [_ceo("temperature_2m", value, hour, unit="C", statistic="instant")
            for hour, value in enumerate([22.5, 29.2, 25.0])]
    wio = build_wio("temperature tomorrow", LOCATION, START, END, "short", ceos)
    assert (wio.weather.temperature["min"], wio.weather.temperature["max"]) == (22.5, 29.2)


def test_marine_panel_reports_peak_wave_height_and_latest_direction():
    ceos = [_ceo("wave_height", value, hour, unit="m", statistic="instant")
            for hour, value in enumerate([1.2, 2.1, 1.8])]
    ceos += [_ceo("wave_direction", direction, hour, unit="deg", statistic="instant")
            for hour, direction in enumerate([180.0, 190.0, 200.0])]
    wio = build_wio("wave height tomorrow", LOCATION, START, END, "short", ceos)
    assert wio.weather.marine["wave_height_m"] == 2.1
    assert wio.weather.marine["wave_direction_deg"] == 200.0  # latest reading, not peak
    assert wio.weather.marine["source"] == "OPEN_METEO"
    assert len(wio.weather.marine["evidence_ids"]) == 2


def test_marine_panel_is_none_without_marine_evidence():
    ceos = [_ceo("temperature_2m", 25.0, 0, unit="C", statistic="instant")]
    wio = build_wio("weather tomorrow", LOCATION, START, END, "short", ceos)
    assert wio.weather.marine is None


def test_one_source_reporting_two_variables_is_not_agreement():
    ceos = [_ceo("precipitation_amount", 1.0, 0, window=1),
            _ceo("temperature_2m", 25.0, 0, unit="C", statistic="instant")]
    assert corroborated(ceos) is False
    wio = build_wio("weather tomorrow", LOCATION, START, END, "short", ceos)
    assert wio.agreement.status == "single_source"


def test_two_distinct_sources_at_the_same_time_is_agreement():
    ceos = _hourly_rain([1.0]) + _hourly_rain([1.2], source="MET_NORWAY")
    assert corroborated(ceos) is True
    wio = build_wio("weather tomorrow", LOCATION, START, END, "short", ceos)
    assert wio.agreement.status == "full_agreement"


def test_one_source_varying_across_the_day_is_not_a_disagreement():
    """A dry morning and a wet evening from one source used to read as sources conflicting."""
    scored = rank(_hourly_rain([0.0, 30.0]), 22.72, 75.86)
    assert detect_disagreements(scored) == []


def test_sources_conflicting_at_the_same_hour_is_a_disagreement():
    ceos = _hourly_rain([0.5]) + _hourly_rain([40.0], source="MET_NORWAY")
    assert detect_disagreements(rank(ceos, 22.72, 75.86))


def test_different_accumulation_windows_are_not_compared():
    ceos = _hourly_rain([0.5]) + [_ceo("precipitation_amount", 40.0, 0, source="MET_NORWAY", window=24)]
    assert detect_disagreements(rank(ceos, 22.72, 75.86)) == []


def test_ensemble_members_are_summarised_and_grouped_by_timestamp():
    ceos = _hourly_rain([1.0])
    ceos += [_ceo("precipitation_amount", 5.0, 0, source="GEFS", window=1, member=i) for i in range(30)]
    ceos += [_ceo("precipitation_amount", 0.0, 1, source="GEFS", window=1, member=i) for i in range(30)]
    wio = build_wio("should I spray tomorrow", LOCATION, START, END, "short", ceos)
    assert wio.weather.rain["member_count"] == 30, "one timestamp's spread, not every hour pooled"
    member_rows = [e for e in wio.evidence if e.variable == "rainfall_distribution"]
    assert len(member_rows) == 1 and member_rows[0].value == 60
    assert not any(e.source == "GEFS" and e.variable == "precipitation_amount" for e in wio.evidence)


def test_ranking_separates_records_that_differ_only_by_time():
    scored = rank(_hourly_rain([0.0] * 6), 22.72, 75.86, window=(START, END))
    assert len({round(score, 9) for score, _ in scored}) > 1


def test_rade_defers_on_an_unmatched_domain():
    """An unmatched question used to receive travel-policy utilities silently."""
    from app.rade.v2 import decide
    ceos = _hourly_rain([1.0])
    wio = build_wio("what is the humidity", LOCATION, START, END, "short", ceos)
    result = decide(wio, {}, "what is the humidity")
    assert result.recommended_action == "defer_decision"


def test_rade_domain_ignores_stored_user_context():
    """A stored fact containing 'crop' used to flip the domain of an unrelated question."""
    from app.rade.v2 import decide
    ceos = _hourly_rain([1.0])
    wio = build_wio("should I travel tomorrow", LOCATION, START, END, "short", ceos)
    assert "harvest" not in decide(wio, {"note": "crop farmer"}, "should I travel tomorrow").rationale


def test_forecast_agent_cites_the_evidence_behind_its_claims():
    import asyncio

    from app.agents.orchestrator import run_forecast_agent
    ceos = _hourly_rain([1.0, 2.0])
    wio = build_wio("rain tomorrow", LOCATION, START, END, "short", ceos)
    result = asyncio.run(run_forecast_agent(wio))
    rain_claim = next(c for c in result.claims if c.claim == "precipitation_amount")
    assert rain_claim.value == wio.weather.rain["value_mm"]
    assert set(rain_claim.evidence_ids) == set(wio.weather.rain["evidence_ids"])
