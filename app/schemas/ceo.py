"""Canonical Evidence Object (CEO) — interoperability envelope; preserves original source, never silently averages incompatible values."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class EvidenceSource(str, Enum):
    IMD = "IMD"
    GFS = "GFS"
    WRF = "WRF"
    ERA5 = "ERA5"
    INSAT = "INSAT"
    RADAR = "RADAR"
    CAP = "CAP"
    OPEN_METEO = "OPEN_METEO"
    GEFS = "GEFS"
    NASA_POWER = "NASA_POWER"
    MET_NORWAY = "MET_NORWAY"
    OPEN_METEO_MARINE = "OPEN_METEO_MARINE"
    STORMGLASS = "STORMGLASS"
    OTHER = "OTHER"

class EvidenceClass(str, Enum):
    observation = "observation"
    forecast = "forecast"
    nowcast = "nowcast"
    warning = "warning"
    radar = "radar"
    satellite = "satellite"
    climate = "climate"
    advisory = "advisory"
    reanalysis = "reanalysis"
    climatology = "climatology"

class CanonicalVariable(str, Enum):
    precipitation_amount = "precipitation_amount"
    precipitation_probability = "precipitation_probability"
    precipitation_rate = "precipitation_rate"
    temperature_2m = "temperature_2m"
    temperature_max = "temperature_max"
    temperature_min = "temperature_min"
    wind_speed = "wind_speed"
    wind_gust = "wind_gust"
    wind_direction = "wind_direction"
    humidity = "humidity"
    pressure_msl = "pressure_msl"
    cloud_cover = "cloud_cover"
    thunderstorm_probability = "thunderstorm_probability"
    rainfall_distribution = "rainfall_distribution"
    heavy_rain_warning = "heavy_rain_warning"
    thunderstorm_warning = "thunderstorm_warning"
    cyclone_warning = "cyclone_warning"
    heat_warning = "heat_warning"
    flood_warning = "flood_warning"
    marine_warning = "marine_warning"
    visibility = "visibility"
    wave_height = "wave_height"
    wave_direction = "wave_direction"
    wave_period = "wave_period"
    ocean_current_velocity = "ocean_current_velocity"
    ocean_current_direction = "ocean_current_direction"
    sea_surface_temperature = "sea_surface_temperature"
    other = "other"

class Statistic(str, Enum):
    instant = "instant"
    accumulation = "accumulation"
    mean = "mean"
    max = "max"
    min = "min"
    probability = "probability"
    categorical = "categorical"

class GeometryType(str, Enum):
    Point = "Point"
    Polygon = "Polygon"
    GridCell = "GridCell"
    RasterCell = "RasterCell"
    Route = "Route"

class Geometry(BaseModel):
    type: GeometryType = Field(default=GeometryType.Point)
    coordinates: Any = Field(default=None, description="lon/lat, polygon ring, or reference id")
    reference: str | None = None  # e.g. district code, basin id

class Provenance(BaseModel):
    original_source: str
    original_field: str | None = None
    original_unit: str | None = None
    transformations: list[str] = Field(default_factory=list)
    raw_record_id: str | None = None

class CanonicalEvidenceObject(BaseModel):
    model_config = {"protected_namespaces": ()}
    evidence_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    source: EvidenceSource
    source_type: str | None = Field(default=None, description="e.g. api, gridded, reanalysis")
    source_record_id: str | None = None
    evidence_class: EvidenceClass
    variable: CanonicalVariable
    value: float | None = None
    raw_value: Any | None = None  # original categorical / string value
    unit: str | None = None
    statistic: Statistic = Field(default=Statistic.instant)
    geometry: Geometry = Field(default_factory=Geometry)
    spatial_resolution: str | None = None
    observed_at: datetime | None = None
    issued_at: datetime | None = None
    model_initialization_time: datetime | None = None
    forecast_reference_time: datetime | None = Field(default=None, description="alias for model_initialization_time")
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    forecast_lead_hours: float | None = None
    accumulation_window_hours: float | None = None
    vertical_level: str | None = Field(default="surface")
    model_name: str | None = None
    model_version: str | None = None
    ensemble_member: int | None = None
    probability: float | None = Field(default=None, ge=0, le=1)
    quality_flag: str | None = None
    quality: str | None = Field(default=None, description="alias for quality_flag")
    confidence: str | None = None
    warning_severity: str | None = None  # green/yellow/orange/red
    ingested_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    retrieval_timestamp: datetime | None = Field(default=None, description="when retrieved from source")
    provenance: Provenance
    parent_ids: list[str] = Field(default_factory=list, description="parent evidence IDs if derived")
    transformation: str | None = Field(default=None, description="transformation performed")
    transformation_timestamp: datetime | None = None
    algorithm_version: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)
