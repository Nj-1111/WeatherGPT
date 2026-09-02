"""Central runtime configuration.  Secrets are read only from the environment.

Every tunable scalar in the system is defined here exactly once. Values that are
domain reference data rather than tuning knobs — the source authority table, the
variable registry, RADE's utility tables — stay with the code that owns them.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _flag(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    app_env: str = os.getenv("WEATHERGPT_ENV", "development")
    log_level: str = os.getenv("WEATHERGPT_LOG_LEVEL", "INFO").upper()
    log_json: bool = _flag("WEATHERGPT_LOG_JSON", "false")
    database_path: str = os.getenv("WEATHERGPT_DB_PATH", "weathergpt.db")
    request_max_bytes: int = int(os.getenv("WEATHERGPT_REQUEST_MAX_BYTES", "65536"))

    # Retrieval
    http_max_connections: int = int(os.getenv("WEATHERGPT_HTTP_MAX_CONNECTIONS", "50"))
    http_max_keepalive: int = int(os.getenv("WEATHERGPT_HTTP_MAX_KEEPALIVE", "20"))
    http_user_agent: str = os.getenv("WEATHERGPT_HTTP_USER_AGENT", "WeatherGPT/1.0 (weather intelligence backend)")
    source_timeout_seconds: float = float(os.getenv("WEATHERGPT_SOURCE_TIMEOUT_SECONDS", "20"))
    source_retries: int = int(os.getenv("WEATHERGPT_SOURCE_RETRIES", "2"))
    source_retry_backoff_seconds: float = float(os.getenv("WEATHERGPT_SOURCE_RETRY_BACKOFF_SECONDS", "0.4"))
    # A source that fails this many times in a row is skipped until the reset window
    # elapses, so a permanently dead source stops costing a full timeout per request.
    circuit_breaker_threshold: int = int(os.getenv("WEATHERGPT_CIRCUIT_BREAKER_THRESHOLD", "3"))
    circuit_breaker_reset_seconds: float = float(os.getenv("WEATHERGPT_CIRCUIT_BREAKER_RESET_SECONDS", "300"))
    # Source grids are ~0.25 deg (~27km); 2dp (~1km) lets a whole city share cache entries.
    cache_key_precision: int = int(os.getenv("WEATHERGPT_CACHE_KEY_PRECISION", "2"))

    # Caches — bounded because the process is long-lived
    forecast_cache_ttl_seconds: int = int(os.getenv("WEATHERGPT_FORECAST_CACHE_TTL_SECONDS", "900"))
    historical_cache_ttl_seconds: int = int(os.getenv("WEATHERGPT_HISTORICAL_CACHE_TTL_SECONDS", "86400"))
    warning_cache_ttl_seconds: int = int(os.getenv("WEATHERGPT_WARNING_CACHE_TTL_SECONDS", "300"))
    weather_cache_max_entries: int = int(os.getenv("WEATHERGPT_WEATHER_CACHE_MAX_ENTRIES", "2048"))
    location_cache_ttl_seconds: int = int(os.getenv("WEATHERGPT_LOCATION_CACHE_TTL_SECONDS", "2592000"))
    location_cache_max_entries: int = int(os.getenv("WEATHERGPT_LOCATION_CACHE_MAX_ENTRIES", "4096"))
    evidence_store_max_entries: int = int(os.getenv("WEATHERGPT_EVIDENCE_STORE_MAX_ENTRIES", "20000"))
    evidence_store_ttl_seconds: int = int(os.getenv("WEATHERGPT_EVIDENCE_STORE_TTL_SECONDS", "3600"))

    # Ranking weights — must sum to 1.0
    rank_weight_authority: float = float(os.getenv("WEATHERGPT_RANK_WEIGHT_AUTHORITY", "0.35"))
    rank_weight_freshness: float = float(os.getenv("WEATHERGPT_RANK_WEIGHT_FRESHNESS", "0.20"))
    rank_weight_spatial: float = float(os.getenv("WEATHERGPT_RANK_WEIGHT_SPATIAL", "0.15"))
    rank_weight_quality: float = float(os.getenv("WEATHERGPT_RANK_WEIGHT_QUALITY", "0.10"))
    # Without this term every hourly record from one source scores identically and
    # "best evidence" degrades to whichever was decoded first.
    rank_weight_temporal: float = float(os.getenv("WEATHERGPT_RANK_WEIGHT_TEMPORAL", "0.20"))
    rank_staleness_ceiling_hours: float = float(os.getenv("WEATHERGPT_RANK_STALENESS_CEILING_HOURS", "72"))
    rank_spatial_half_decay_km: float = float(os.getenv("WEATHERGPT_RANK_SPATIAL_HALF_DECAY_KM", "50"))
    rank_unknown_location_penalty: float = float(os.getenv("WEATHERGPT_RANK_UNKNOWN_LOCATION_PENALTY", "0.25"))
    rank_warning_authority_bonus: float = float(os.getenv("WEATHERGPT_RANK_WARNING_AUTHORITY_BONUS", "0.1"))
    rank_bad_quality_factor: float = float(os.getenv("WEATHERGPT_RANK_BAD_QUALITY_FACTOR", "0.2"))

    # Fusion
    disagreement_threshold_mm: float = float(os.getenv("WEATHERGPT_DISAGREEMENT_THRESHOLD_MM", "10"))
    disagreement_threshold_c: float = float(os.getenv("WEATHERGPT_DISAGREEMENT_THRESHOLD_C", "3"))
    rain_likely_probability: float = float(os.getenv("WEATHERGPT_RAIN_LIKELY_PROBABILITY", "0.6"))
    rain_possible_probability: float = float(os.getenv("WEATHERGPT_RAIN_POSSIBLE_PROBABILITY", "0.3"))
    measurable_rain_mm: float = float(os.getenv("WEATHERGPT_MEASURABLE_RAIN_MM", "0.5"))

    # Time parsing
    default_timezone: str = os.getenv("WEATHERGPT_DEFAULT_TIMEZONE", "Asia/Kolkata")

    # Location resolution
    geocoding_enabled: bool = _flag("WEATHERGPT_GEOCODING_ENABLED", "true")
    geocoding_timeout_seconds: float = float(os.getenv("WEATHERGPT_GEOCODING_TIMEOUT_SECONDS", "6"))
    # Score gap required to auto-pick the top candidate; below it the request is ambiguous.
    # Scores are log10(population)-based, so 1.0 means "roughly an order of magnitude bigger".
    geocoding_dominance_margin: float = float(os.getenv("WEATHERGPT_GEOCODING_DOMINANCE_MARGIN", "1.0"))
    # OSM policy requires a real identifying User-Agent and at most 1 request/second.
    nominatim_user_agent: str = os.getenv("WEATHERGPT_NOMINATIM_USER_AGENT", "WeatherGPT/1.0 (weather intelligence backend)")
    nominatim_min_interval_seconds: float = float(os.getenv("WEATHERGPT_NOMINATIM_MIN_INTERVAL_SECONDS", "1.0"))

    # Source credentials and endpoints
    cap_feed_url: str = os.getenv("CAP_FEED_URL", "https://cap-sources.s3.amazonaws.com/in-imd-en/rss.xml")
    cap_max_alerts: int = int(os.getenv("WEATHERGPT_CAP_MAX_ALERTS", "25"))
    groq_api_key: str = os.getenv("GROQ_API_KEY", "")
    imd_api_key: str = os.getenv("IMD_API_KEY", "")
    imd_api_base: str = os.getenv("IMD_API_BASE", "")
    # api.met.no rejects generic User-Agents; it must identify the deployment.
    met_norway_user_agent: str = os.getenv("WEATHERGPT_MET_NORWAY_USER_AGENT", "WeatherGPT/1.0 (weather intelligence backend)")

    # API protection
    rate_limit_per_minute: int = int(os.getenv("WEATHERGPT_RATE_LIMIT_PER_MINUTE", "30"))
    rate_limit_per_day: int = int(os.getenv("WEATHERGPT_RATE_LIMIT_PER_DAY", "1000"))
    rate_limit_enabled: bool = _flag("WEATHERGPT_RATE_LIMIT_ENABLED", "true")
    guardrail_enabled: bool = _flag("WEATHERGPT_GUARDRAIL_ENABLED", "true")
    guardrail_min_chars: int = int(os.getenv("WEATHERGPT_GUARDRAIL_MIN_CHARS", "3"))
    guardrail_max_chars: int = int(os.getenv("WEATHERGPT_GUARDRAIL_MAX_CHARS", "512"))
    guardrail_max_words: int = int(os.getenv("WEATHERGPT_GUARDRAIL_MAX_WORDS", "60"))

    cors_origins: tuple[str, ...] = tuple(
        item.strip() for item in os.getenv("WEATHERGPT_CORS_ORIGINS", "").split(",") if item.strip()
    )

    @property
    def rank_weights_total(self) -> float:
        return (self.rank_weight_authority + self.rank_weight_freshness + self.rank_weight_spatial
                + self.rank_weight_quality + self.rank_weight_temporal)


settings = Settings()
