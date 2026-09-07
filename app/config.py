"""Central runtime configuration — secrets read only from the environment. Every tunable scalar is defined here exactly once; domain reference data (source authority table, variable registry, RADE's utility tables) stays with the code that owns it."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _flag(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class LLMEndpoint:
    """One OpenAI-compatible chat endpoint — no provider is named anywhere in code; it's just a base URL and model string from the environment."""
    model: str
    base_url: str
    api_key: str

    @property
    def host(self) -> str:
        """Reported in logs and traces so an operator can see which endpoint answered."""
        without_scheme = self.base_url.split("://")[-1]
        return without_scheme.split("/")[0]


def _llm_chain(tier: str) -> tuple[LLMEndpoint, ...]:
    """Primary + ordered fallbacks for one tier from {TIER}_LLM_MODEL/_BASE_URL/_KEY then _FALLBACK_{n}_..., stopping at the first gap; an unconfigured tier yields an empty chain (reported unavailable, not an error)."""
    endpoints: list[LLMEndpoint] = []
    model, base_url = os.getenv(f"{tier}_LLM_MODEL", ""), os.getenv(f"{tier}_LLM_BASE_URL", "")
    if model and base_url:
        endpoints.append(LLMEndpoint(model, base_url, os.getenv(f"{tier}_LLM_KEY", "")))
    index = 1
    while True:
        prefix = f"{tier}_LLM_FALLBACK_{index}"
        model, base_url = os.getenv(f"{prefix}_MODEL", ""), os.getenv(f"{prefix}_BASE_URL", "")
        if not (model and base_url):
            break
        endpoints.append(LLMEndpoint(model, base_url, os.getenv(f"{prefix}_KEY", "")))
        index += 1
    return tuple(endpoints)


@dataclass(frozen=True)
class Settings:
    app_env: str = os.getenv("WEATHERGPT_ENV", "development")
    log_level: str = os.getenv("WEATHERGPT_LOG_LEVEL", "INFO").upper()
    log_json: bool = _flag("WEATHERGPT_LOG_JSON", "false")
    database_path: str = os.getenv("WEATHERGPT_DB_PATH", "weathergpt.db")
    request_max_bytes: int = int(os.getenv("WEATHERGPT_REQUEST_MAX_BYTES", "65536"))
    # Bounds POST /context growth — unlimited distinct facts per user_id previously grew user_context without limit; see app/context/store.py:upsert_fact.
    context_max_facts_per_user: int = int(os.getenv("WEATHERGPT_CONTEXT_MAX_FACTS_PER_USER", "256"))
    context_value_max_chars: int = int(os.getenv("WEATHERGPT_CONTEXT_VALUE_MAX_CHARS", "4096"))

    # Retrieval
    http_max_connections: int = int(os.getenv("WEATHERGPT_HTTP_MAX_CONNECTIONS", "50"))
    http_max_keepalive: int = int(os.getenv("WEATHERGPT_HTTP_MAX_KEEPALIVE", "20"))
    http_user_agent: str = os.getenv("WEATHERGPT_HTTP_USER_AGENT", "WeatherGPT/1.0 (weather intelligence backend)")
    source_timeout_seconds: float = float(os.getenv("WEATHERGPT_SOURCE_TIMEOUT_SECONDS", "8"))
    source_retries: int = int(os.getenv("WEATHERGPT_SOURCE_RETRIES", "1"))
    source_retry_backoff_seconds: float = float(os.getenv("WEATHERGPT_SOURCE_RETRY_BACKOFF_SECONDS", "0.4"))
    # A source failing this many times in a row is skipped until the reset window elapses, so a dead source stops costing a full timeout per request.
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
    # /health is exempt from auth/rate limiting (infra must probe it); this short cache is what stops repeat calls re-triggering all 8 adapters.
    health_cache_ttl_seconds: int = int(os.getenv("WEATHERGPT_HEALTH_CACHE_TTL_SECONDS", "30"))

    # Ranking weights — must sum to 1.0
    rank_weight_authority: float = float(os.getenv("WEATHERGPT_RANK_WEIGHT_AUTHORITY", "0.35"))
    rank_weight_freshness: float = float(os.getenv("WEATHERGPT_RANK_WEIGHT_FRESHNESS", "0.20"))
    rank_weight_spatial: float = float(os.getenv("WEATHERGPT_RANK_WEIGHT_SPATIAL", "0.15"))
    rank_weight_quality: float = float(os.getenv("WEATHERGPT_RANK_WEIGHT_QUALITY", "0.10"))
    # Without this term every hourly record from one source scores identically, so "best evidence" degrades to whichever was decoded first.
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

    # RADE marine domain thresholds only (utility model is app/rade/v2.py's POLICIES): small-craft-advisory bands, ~18-33kn/~4-5ft = "caution", gale-force 34kn+/~2.5m+ = "avoid".
    rade_marine_wave_caution_m: float = float(os.getenv("WEATHERGPT_RADE_MARINE_WAVE_CAUTION_M", "1.25"))
    rade_marine_wave_avoid_m: float = float(os.getenv("WEATHERGPT_RADE_MARINE_WAVE_AVOID_M", "2.5"))
    rade_marine_current_caution_kmh: float = float(os.getenv("WEATHERGPT_RADE_MARINE_CURRENT_CAUTION_KMH", "3.7"))
    rade_marine_wind_caution_kmh: float = float(os.getenv("WEATHERGPT_RADE_MARINE_WIND_CAUTION_KMH", "28"))
    rade_marine_wind_avoid_kmh: float = float(os.getenv("WEATHERGPT_RADE_MARINE_WIND_AVOID_KMH", "39"))
    # Domain-general "how close was this call" threshold: a top_margin below this asks a
    # declared CLARIFYING_FIELDS question instead of just answering. Not domain-specific —
    # every RADE domain shares this one score-gap threshold.
    rade_borderline_score_margin: float = float(os.getenv("WEATHERGPT_RADE_BORDERLINE_SCORE_MARGIN", "5.0"))

    # Reviewer anti-hallucination gate recomputes every claimed value from its cited evidence; panels round, so an exact compare would false-fail.
    reviewer_value_rel_tol: float = float(os.getenv("WEATHERGPT_REVIEWER_VALUE_REL_TOL", "0.01"))
    reviewer_value_abs_tol: float = float(os.getenv("WEATHERGPT_REVIEWER_VALUE_ABS_TOL", "0.05"))
    # An ungrounded number in LLM prose suppresses the explanation (answers from the template); "fail" instead rejects the whole request with a 503.
    reviewer_prose_failure_mode: str = os.getenv("WEATHERGPT_REVIEWER_PROSE_FAILURE_MODE", "suppress")

    # LLM is a transport only — never selects sources, resolves a coordinate, or originates a number. Two tiers, each an ordered chain; all failing returns a typed unavailable result.
    llm_enabled: bool = _flag("LLM_ENABLED", "true")
    llm_timeout_seconds: float = float(os.getenv("LLM_TIMEOUT_SECONDS", "8"))
    # Per-endpoint timeout x chain length is the worst case and sits on the request path; this caps the whole chain so a long fallback list can't stall a user.
    llm_total_timeout_seconds: float = float(os.getenv("LLM_TOTAL_TIMEOUT_SECONDS", "20"))
    llm_max_words: int = int(os.getenv("WEATHERGPT_LLM_MAX_WORDS", "120"))
    small_llm_chain: tuple[LLMEndpoint, ...] = field(default_factory=lambda: _llm_chain("SMALL"))
    big_llm_chain: tuple[LLMEndpoint, ...] = field(default_factory=lambda: _llm_chain("BIG"))
    # Confidence floor below which a RADE decision (deferred = 0) needs real reasoning, not a panel restate; sits between RADE's 0.55 partial/0.8 full-agreement so only partial wakes the big tier.
    big_llm_complexity_confidence_threshold: float = float(
        os.getenv("WEATHERGPT_BIG_LLM_COMPLEXITY_CONFIDENCE_THRESHOLD", "0.6"))

    # Storage backend switch exists so promotion is a config change, not a rewrite; only "memory"/"sqlite" exist today and anything else fails loudly at import.
    session_backend: str = os.getenv("SESSION_BACKEND", "memory").strip().lower()
    db_backend: str = os.getenv("DB_BACKEND", "sqlite").strip().lower()
    session_ttl_seconds: int = int(os.getenv("SESSION_TTL_SECONDS", "1800"))
    session_max_entries: int = int(os.getenv("SESSION_MAX_ENTRIES", "4096"))
    conversation_max_turns: int = int(os.getenv("CONVERSATION_MAX_TURNS", "20"))

    # Time parsing
    default_timezone: str = os.getenv("WEATHERGPT_DEFAULT_TIMEZONE", "Asia/Kolkata")

    # Location resolution
    geocoding_enabled: bool = _flag("WEATHERGPT_GEOCODING_ENABLED", "true")
    geocoding_timeout_seconds: float = float(os.getenv("WEATHERGPT_GEOCODING_TIMEOUT_SECONDS", "6"))
    # Score gap required to auto-pick the top candidate (below it, ambiguous); scores are log10(population)-based, so 1.0 ~= "an order of magnitude bigger".
    geocoding_dominance_margin: float = float(os.getenv("WEATHERGPT_GEOCODING_DOMINANCE_MARGIN", "1.0"))
    # OSM policy requires a real identifying User-Agent and at most 1 request/second.
    nominatim_user_agent: str = os.getenv("WEATHERGPT_NOMINATIM_USER_AGENT", "WeatherGPT/1.0 (weather intelligence backend)")
    nominatim_min_interval_seconds: float = float(os.getenv("WEATHERGPT_NOMINATIM_MIN_INTERVAL_SECONDS", "1.0"))
    # Primary geocoder when set; unaffected keyless chain (Open-Meteo, Nominatim) still works when empty — GeoapifyGeocoder.search() just returns [].
    geoapify_api_key: str = os.getenv("GEOAPIFY_API_KEY", "")
    # Fallback marine source when OPEN_METEO_MARINE yields nothing; unconfigured means only the primary runs (same degrade-gracefully convention as IMD).
    stormglass_api_key: str = os.getenv("STORMGLASS_API_KEY", "")

    # Source credentials and endpoints
    cap_feed_url: str = os.getenv("CAP_FEED_URL", "https://cap-sources.s3.amazonaws.com/in-imd-en/rss.xml")
    cap_max_alerts: int = int(os.getenv("WEATHERGPT_CAP_MAX_ALERTS", "25"))
    imd_api_key: str = os.getenv("IMD_API_KEY", "")
    imd_api_base: str = os.getenv("IMD_API_BASE", "")
    # api.met.no rejects generic User-Agents; it must identify the deployment.
    met_norway_user_agent: str = os.getenv("WEATHERGPT_MET_NORWAY_USER_AGENT", "WeatherGPT/1.0 (weather intelligence backend)")

    # API protection: server-to-server auth for a small set of trusted backend callers (static key, not a login/JWT system); empty/unset disables the gate like every opt-in flag below.
    api_keys: tuple[str, ...] = tuple(
        item.strip() for item in os.getenv("WEATHERGPT_API_KEYS", "").split(",") if item.strip()
    )
    rate_limit_per_minute: int = int(os.getenv("WEATHERGPT_RATE_LIMIT_PER_MINUTE", "30"))
    rate_limit_per_day: int = int(os.getenv("WEATHERGPT_RATE_LIMIT_PER_DAY", "1000"))
    rate_limit_enabled: bool = _flag("WEATHERGPT_RATE_LIMIT_ENABLED", "true")
    guardrail_enabled: bool = _flag("WEATHERGPT_GUARDRAIL_ENABLED", "true")
    guardrail_min_chars: int = int(os.getenv("WEATHERGPT_GUARDRAIL_MIN_CHARS", "3"))
    guardrail_max_chars: int = int(os.getenv("WEATHERGPT_GUARDRAIL_MAX_CHARS", "512"))
    guardrail_max_words: int = int(os.getenv("WEATHERGPT_GUARDRAIL_MAX_WORDS", "60"))
    # The guardrail's LLM call is deterministic (fixed decision tree, temperature 0), so a repeated question can't legitimately reach a different action — safe to cache.
    guardrail_cache_ttl_seconds: int = int(os.getenv("WEATHERGPT_GUARDRAIL_CACHE_TTL_SECONDS", "3600"))
    guardrail_cache_max_entries: int = int(os.getenv("WEATHERGPT_GUARDRAIL_CACHE_MAX_ENTRIES", "2048"))

    # LLM-first location/time/intent/topic extraction ahead of geocoding, mandatory by product decision; below the confidence floor a read is treated as off-topic.
    query_understanding_enabled: bool = _flag("WEATHERGPT_QUERY_UNDERSTANDING_ENABLED", "true")
    query_understanding_confidence_threshold: float = float(
        os.getenv("WEATHERGPT_QUERY_UNDERSTANDING_CONFIDENCE_THRESHOLD", "0.7"))

    # Short-circuits geocoding+time parsing for a follow-up naming no new location; its own short TTL (not session_ttl_seconds) because a stale-location silent answer is worse than a cache miss.
    follow_up_context_enabled: bool = _flag("WEATHERGPT_FOLLOW_UP_CONTEXT_ENABLED", "true")
    follow_up_context_ttl_seconds: int = int(os.getenv("WEATHERGPT_FOLLOW_UP_CONTEXT_TTL_SECONDS", "300"))
    follow_up_context_max_entries: int = int(os.getenv("WEATHERGPT_FOLLOW_UP_CONTEXT_MAX_ENTRIES", "4096"))

    # A VERIFY decision ("did you mean Bangalore?") stores its candidate so the next turn can confirm instead of re-guessing; short TTL since a stale pending verify answering later is worse than asking again.
    verify_pending_ttl_seconds: int = int(os.getenv("WEATHERGPT_VERIFY_PENDING_TTL_SECONDS", "120"))
    verify_pending_max_entries: int = int(os.getenv("WEATHERGPT_VERIFY_PENDING_MAX_ENTRIES", "4096"))

    # Cap on (location, time-window) pairs a single query can fan out into — bounds API call
    # volume/latency from one request and keeps Nominatim's 1 req/s throttle from serializing
    # an unbounded number of geocoding calls. A starting guess, not derived from real cost
    # data; revisit once real multi-location usage exists.
    max_location_time_pairs: int = int(os.getenv("WEATHERGPT_MAX_LOCATION_TIME_PAIRS", "6"))

    # A domain's clarifying follow-up (e.g. marine's "alone or with a crew?") is conversational pace, not instant yes/no — gets follow-up-context's TTL, not VERIFY's tighter one.
    pending_followup_ttl_seconds: int = int(os.getenv("WEATHERGPT_PENDING_FOLLOWUP_TTL_SECONDS", "300"))

    cors_origins: tuple[str, ...] = tuple(
        item.strip() for item in os.getenv("WEATHERGPT_CORS_ORIGINS", "").split(",") if item.strip()
    )

    @property
    def rank_weights_total(self) -> float:
        return (self.rank_weight_authority + self.rank_weight_freshness + self.rank_weight_spatial
                + self.rank_weight_quality + self.rank_weight_temporal)


settings = Settings()
