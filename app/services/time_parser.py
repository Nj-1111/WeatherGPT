from __future__ import annotations

import re
from datetime import datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.config import settings
from app.constants import IST

_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
# Each month spelled out explicitly rather than as a prefix plus \w*. The prefix form let
# the month group swallow any suffix, so "maybe 5" matched as May 5th — confidently, and
# with a horizon far enough out to reroute retrieval to the historical sources.
MONTH_PATTERN = "|".join((
    "jan(?:uary)?", "feb(?:ruary)?", "mar(?:ch)?", "apr(?:il)?", "may", "jun(?:e)?",
    "jul(?:y)?", "aug(?:ust)?", "sep(?:t(?:ember)?)?", "oct(?:ober)?", "nov(?:ember)?",
    "dec(?:ember)?",
))
_ISO_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
# The day must sit next to the month name; searching the whole sentence for digits made
# "2 pm on Aug 5" resolve to August 2nd.
_DAY_MONTH = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({MONTH_PATTERN})\b")
_MONTH_DAY = re.compile(rf"\b({MONTH_PATTERN})\s+(\d{{1,2}})(?:st|nd|rd|th)?\b")
_IN_HOURS = re.compile(r"\bin (\d+) hours?\b")
_NEXT_HOURS = re.compile(r"\bnext (\d+)\s*hours?\b")
_NOW_PHRASE = re.compile(r"\b(?:right now|now|currently|at the moment|at present|abhi)\b")


def resolve_timezone(name: str | None) -> tzinfo:
    """Fall back rather than fail: an unknown zone must not break a weather question."""
    for candidate in (name, settings.default_timezone):
        if not candidate:
            continue
        try:
            return ZoneInfo(candidate)
        except (ZoneInfoNotFoundError, ValueError):
            continue
    return IST


def parse_time_window(text: str, now: datetime | None = None, tz: tzinfo | str | None = None):
    """Deterministic time normalization -> (valid_from, valid_to, horizon, confidence).

    The window is resolved in the location's own timezone: "tomorrow" in Springfield is
    not the same day as "tomorrow" in Indore.
    """
    if isinstance(tz, str) or tz is None:
        tz = resolve_timezone(tz)
    if now is None:
        now = datetime.now(tz)
    else:
        now = now.astimezone(tz) if now.tzinfo else now.replace(tzinfo=tz)

    text_l = (text or "").lower()
    base = now
    confidence = 0.9

    iso = _ISO_DATE.search(text_l)
    day_month = _DAY_MONTH.search(text_l) or _MONTH_DAY.search(text_l)
    if iso:
        try:
            base = datetime(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)), tzinfo=tz)
            confidence = 1.0
        except ValueError:
            confidence = 0.5
    elif day_month:
        groups = day_month.groups()
        day, month = (groups[0], groups[1]) if groups[0].isdigit() else (groups[1], groups[0])
        try:
            base = datetime(now.year, _MONTHS.index(month[:3]) + 1, int(day), tzinfo=tz)
            if base < now:
                base = base.replace(year=now.year + 1)
            confidence = 0.8
        except ValueError:
            confidence = 0.5
    elif "day after tomorrow" in text_l or "parso" in text_l or "परसों" in text_l:
        base = now + timedelta(days=2)
    elif "tomorrow" in text_l or "kal" in text_l or "कल" in text_l:
        base = now + timedelta(days=1)
    elif "next week" in text_l:
        base = now + timedelta(days=7)
        confidence = 0.7
    elif "coming monday" in text_l or "next monday" in text_l:
        base = now + timedelta(days=(0 - now.weekday()) % 7 or 7)
    elif "weekend" in text_l:
        days_ahead = (5 - now.weekday()) % 7
        base = now + timedelta(days=days_ahead if days_ahead != 0 else 7)
    elif in_hours := _IN_HOURS.search(text_l):
        base = now + timedelta(hours=int(in_hours.group(1)))
        return base, base + timedelta(hours=1), "nowcast", 0.95

    # A nowcast question answered with the whole calendar day reports hours that have
    # already passed and hours after the event ends. Floored to the top of the hour so
    # the record covering the current hour is inside the window, not just before it.
    if base == now:
        hour_start = now.replace(minute=0, second=0, microsecond=0)
        if next_hours := _NEXT_HOURS.search(text_l):
            span = min(int(next_hours.group(1)), 48)
            return hour_start, hour_start + timedelta(hours=span), "nowcast", 0.95
        if _NOW_PHRASE.search(text_l):
            return hour_start, hour_start + timedelta(hours=1), "nowcast", 0.95

    if "morning" in text_l:
        valid_from = base.replace(hour=6, minute=0, second=0, microsecond=0)
        valid_to = base.replace(hour=11, minute=59, second=59, microsecond=0)
    elif "afternoon" in text_l:
        valid_from = base.replace(hour=12, minute=0, second=0, microsecond=0)
        valid_to = base.replace(hour=18, minute=0, second=0, microsecond=0)
    elif "evening" in text_l:
        valid_from = base.replace(hour=18, minute=0, second=0, microsecond=0)
        valid_to = base.replace(hour=21, minute=0, second=0, microsecond=0)
    elif "night" in text_l or "tonight" in text_l:
        valid_from = base.replace(hour=21, minute=0, second=0, microsecond=0)
        valid_to = (base + timedelta(days=1)).replace(hour=5, minute=59, second=59, microsecond=0)
    elif "next 3 days" in text_l or "next three days" in text_l:
        valid_from = base.replace(hour=0, minute=0, second=0, microsecond=0)
        valid_to = (base + timedelta(days=2)).replace(hour=23, minute=59, second=59, microsecond=0)
        confidence = 0.8
    elif "next week" in text_l:
        valid_from = base.replace(hour=0, minute=0, second=0, microsecond=0)
        valid_to = (base + timedelta(days=6)).replace(hour=23, minute=59, second=59, microsecond=0)
        confidence = 0.7
    else:
        valid_from = base.replace(hour=0, minute=0, second=0, microsecond=0)
        valid_to = base.replace(hour=23, minute=59, second=59, microsecond=0)
        if base == now and "tomorrow" not in text_l and "today" not in text_l:
            confidence = 0.6

    delta_days = (valid_from.date() - now.date()).days
    if delta_days < 0:
        horizon = "climate"
    elif delta_days == 0 and (valid_to - now) <= timedelta(hours=6):
        horizon = "nowcast"
    elif delta_days <= 3:
        horizon = "short"
    elif delta_days <= 10:
        horizon = "medium"
    else:
        horizon = "climate"

    return valid_from, valid_to, horizon, confidence
