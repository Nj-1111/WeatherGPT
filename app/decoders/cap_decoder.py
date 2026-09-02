"""CAP XML -> CEOs. Preserves alert lifecycle (update/cancel) and geography."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from app.schemas.ceo import CanonicalEvidenceObject, Geometry, Provenance

NS = {"cap": "urn:oasis:names:tc:emergency:cap:1.2"}

_SEVERITY_COLOURS = {"minor": "green", "moderate": "yellow", "severe": "orange", "extreme": "red"}

# CAP carries a free-text event; the canonical variable must reflect what the warning is
# about. Everything used to be filed as heavy_rain_warning, so a cyclone alert was
# labelled heavy rain. Order matters: the first match wins.
_EVENT_VARIABLES = (
    (("cyclone", "hurricane", "typhoon"), "cyclone_warning"),
    (("thunder", "squall", "lightning"), "thunderstorm_warning"),
    (("flood", "inundation"), "flood_warning"),
    (("heat", "heatwave", "warm wave"), "heat_warning"),
    (("marine", "wind", "gale", "sea"), "marine_warning"),
    (("rain", "precipitation", "shower"), "heavy_rain_warning"),
)


def _text(element, tag: str) -> str | None:
    node = element.find(f"cap:{tag}", NS)
    if node is None:
        node = element.find(tag)
    return node.text.strip() if node is not None and node.text else None


def _time(raw: str | None, default: datetime | None = None) -> datetime | None:
    if not raw:
        return default
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return default
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _variable_for(event: str, headline: str | None) -> str:
    haystack = f"{event} {headline or ''}".casefold()
    for keywords, variable in _EVENT_VARIABLES:
        if any(keyword in haystack for keyword in keywords):
            return variable
    return "heavy_rain_warning"


def _polygon(area) -> list[list[float]] | None:
    """CAP polygons are 'lat,lon lat,lon ...'; CEO geometry stores [lon, lat] pairs."""
    raw = _text(area, "polygon")
    if not raw:
        return None
    points: list[list[float]] = []
    for pair in raw.split():
        try:
            lat_text, lon_text = pair.split(",")
            points.append([float(lon_text), float(lat_text)])
        except ValueError:
            continue
    return points or None


def decode_cap_xml(xml_bytes: bytes) -> list[CanonicalEvidenceObject]:
    out: list[CanonicalEvidenceObject] = []
    root = ET.fromstring(xml_bytes)
    identifier = _text(root, "identifier") or "cap-unknown"
    sender = _text(root, "sender") or "CAP"
    msg_type = _text(root, "msgType") or "Alert"
    status = _text(root, "status") or "Actual"
    is_cancel = msg_type.casefold() == "cancel" or status.casefold() == "cancel"
    issued = _time(_text(root, "sent"), datetime.now(timezone.utc))

    for info in root.findall("cap:info", NS) + root.findall("info"):
        event = _text(info, "event") or "weather"
        headline = _text(info, "headline")
        severity = (_text(info, "severity") or "Moderate").casefold()
        colour = "cancelled" if is_cancel else _SEVERITY_COLOURS.get(severity, "yellow")
        # IMD publishes <onset>; <effective> is often absent. Falling back to the issue
        # time made every warning look like it started the moment it was published.
        valid_from = _time(_text(info, "onset")) or _time(_text(info, "effective")) or issued
        valid_to = _time(_text(info, "expires"))

        areas: list[str] = []
        coordinates: list[list[float]] | None = None
        for area in info.findall("cap:area", NS) + info.findall("area"):
            areas.append(_text(area, "areaDesc") or "unknown")
            coordinates = coordinates or _polygon(area)

        geometry = Geometry(type="Polygon", coordinates=coordinates,
                            reference=", ".join(areas) if areas else "unknown")
        out.append(CanonicalEvidenceObject(
            source="CAP", source_record_id=identifier, evidence_class="warning",
            variable=_variable_for(event, headline), value=None, raw_value=headline or event,
            unit=None, statistic="categorical", geometry=geometry,
            issued_at=issued, valid_from=valid_from, valid_to=valid_to, warning_severity=colour,
            provenance=Provenance(original_source=sender, original_field="CAP info",
                                  transformations=["parsed CAP XML", f"msgType={msg_type} status={status}"],
                                  raw_record_id=identifier),
            extra={"cap_severity": severity, "cap_status": status, "cap_msgType": msg_type,
                   "cap_event": event, "areas": areas},
        ))
    return out
