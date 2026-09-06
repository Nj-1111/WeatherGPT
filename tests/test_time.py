
from datetime import datetime, timedelta, timezone

from app.services.time_parser import parse_time_window

IST = timezone(timedelta(hours=5, minutes=30))
def test_tomorrow_afternoon():
    now = datetime(2026,9,1,10,0, tzinfo=IST)
    vf, vt, hor, conf = parse_time_window("tomorrow afternoon", now)
    assert vt.hour == 18
    assert hor == "short"
def test_next_week():
    now = datetime(2026,9,1,10,0, tzinfo=IST)
    vf, vt, hor, conf = parse_time_window("next week", now)
    assert hor in ("medium","climate")
def test_explicit_date():
    now = datetime(2026,9,1,10,0, tzinfo=IST)
    vf, vt, hor, conf = parse_time_window("2024-01-15", now)
    assert vf.year == 2024


def test_month_name_inside_another_word_is_not_a_date():
    """'may' is a substring of 'maybe', which parsed the question as a date in May."""
    now = datetime(2026, 9, 3, 10, 0, tzinfo=IST)
    vf, _, _, _ = parse_time_window("will it rain tomorrow, maybe?", now)
    assert (vf.month, vf.day) == (9, 4)


def test_day_is_read_next_to_the_month_not_from_anywhere_in_the_sentence():
    now = datetime(2026, 9, 3, 10, 0, tzinfo=IST)
    vf, _, _, _ = parse_time_window("rain at 2 pm on Aug 5", now)
    assert (vf.month, vf.day) == (8, 5)


def test_window_follows_the_location_timezone():
    now = datetime(2026, 9, 3, 10, 0, tzinfo=IST)
    indore, _, _, _ = parse_time_window("will it rain tomorrow", now, "Asia/Kolkata")
    chicago, _, _, _ = parse_time_window("will it rain tomorrow", now, "America/Chicago")
    assert indore.day != chicago.day, "tomorrow is not the same calendar day everywhere"


def test_unknown_timezone_falls_back_instead_of_failing():
    now = datetime(2026, 9, 3, 10, 0, tzinfo=IST)
    assert parse_time_window("tomorrow", now, "Not/AZone")[0].day == 4


def test_past_date_selects_the_historical_horizon():
    now = datetime(2026, 9, 3, 10, 0, tzinfo=IST)
    assert parse_time_window("2024-01-15", now)[2] == "climate"


def test_right_now_is_a_nowcast_window_not_the_whole_day():
    """'is it raining right now' answered with a 24h sum reports hours already past."""
    now = datetime(2026, 9, 5, 13, 48, tzinfo=IST)
    valid_from, valid_to, horizon, _ = parse_time_window("is it raining right now", now=now)
    assert horizon == "nowcast"
    assert (valid_to - valid_from) == timedelta(hours=1)
    assert valid_from.hour == 13 and valid_from.minute == 0, "must cover the current hour"


def test_next_n_hours_uses_that_span():
    now = datetime(2026, 9, 5, 13, 48, tzinfo=IST)
    valid_from, valid_to, horizon, _ = parse_time_window("how much rain in the next 6 hours", now=now)
    assert horizon == "nowcast"
    assert (valid_to - valid_from) == timedelta(hours=6)


def test_tomorrow_is_still_a_full_day():
    now = datetime(2026, 9, 5, 13, 48, tzinfo=IST)
    valid_from, valid_to, horizon, _ = parse_time_window("will it rain tomorrow", now=now)
    assert horizon == "short"
    assert valid_from.hour == 0 and valid_to.hour == 23
