"""Central runtime configuration.  Secrets are read only from the environment.

Every tunable scalar in the system is defined here exactly once. Values that are
domain reference data rather than tuning knobs — the source authority table, the
variable registry, RADE's utility tables — stay with the code that owns them.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _flag(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class LLMEndpoint:
    """One OpenAI-compatible chat endpoint. No provider is named anywhere in code —
    a provider is only ever a base URL and a model string supplied by the environment."""
    model: str
    base_url: str
    api_key: str

    @property
    def host(self) -> str:
        """Reported in logs and traces so an operator can see which endpoint answered."""
        without_scheme = self.base_url.split("://")[-1]
        return without_scheme.split("/")[0]


def _llm_chain(tier: str) -> tuple[LLMEndpoint, ...]:
    """Primary endpoint plus ordered fallbacks for one tier, read from the environment.

    `{TIER}_LLM_MODEL/_BASE_URL/_KEY`, then `{TIER}_LLM_FALLBACK_{n}_MODEL/_BASE_URL/_KEY`
    for n=1.. — scanning stops at the first gap so the order in the environment is the
    order they are tried. An unconfigured tier yields an empty chain, which the client
    reports as unavailable rather than treating as an error.
    """
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
    # /health is exempt from auth and rate limiting (infra must be able to probe it), so a
    # short cache is what actually stops repeat calls from re-triggering all 8 adapters.
    health_cache_ttl_seconds: int = int(os.getenv("WEATHERGPT_HEALTH_CACHE_TTL_SECONDS", "30"))

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

    # Reviewer — the anti-hallucination gate recomputes every claimed value from the
    # evidence the claim cites. Panels round, so an exact compare would false-fail.
    reviewer_value_rel_tol: float = float(os.getenv("WEATHERGPT_REVIEWER_VALUE_REL_TOL", "0.01"))
    reviewer_value_abs_tol: float = float(os.getenv("WEATHERGPT_REVIEWER_VALUE_ABS_TOL", "0.05"))
    # An ungrounded number in LLM prose suppresses the explanation and answers from the
    # deterministic template. "fail" instead rejects the whole request with a 503.
    reviewer_prose_failure_mode: str = os.getenv("WEATHERGPT_REVIEWER_PROSE_FAILURE_MODE", "suppress")

    # LLM — a transport, nothing more. It never selects sources, never resolves a
    # coordinate, never originates a number. Two tiers, each an ordered chain tried in
    # order; when every endpoint fails the caller gets a typed unavailable result.
    llm_enabled: bool = _flag("LLM_ENABLED", "true")
    llm_timeout_seconds: float = float(os.getenv("LLM_TIMEOUT_SECONDS", "8"))
    # Per-endpoint timeout times chain length is the worst case, and that sits on the
    # request path. This caps the whole chain so a long fallback list cannot stall a user.
    llm_total_timeout_seconds: float = float(os.getenv("LLM_TOTAL_TIMEOUT_SECONDS", "20"))
    llm_max_words: int = int(os.getenv("WEATHERGPT_LLM_MAX_WORDS", "120"))
    # A prompt-level instruction, not a verified gate — there's no mechanical check for
    # tone the way the reviewer mechanically checks numeric grounding. Editable without a
    # code change so the wording can be tuned freely later.
    explanation_tone_directive: str = os.getenv(
        "WEATHERGPT_EXPLANATION_TONE", "clear, neutral, and helpful — not overly casual, not overly formal")
    small_llm_chain: tuple[LLMEndpoint, ...] = field(default_factory=lambda: _llm_chain("SMALL"))
    big_llm_chain: tuple[LLMEndpoint, ...] = field(default_factory=lambda: _llm_chain("BIG"))
    # Confidence floor below which a RADE decision (including a deferred one, which always
    # scores 0) is treated as needing real reasoning rather than restating one panel value.
    # Sits strictly between RADE's two real confidence values (0.55 partial-agreement,
    # 0.8 full-agreement — app/rade/v2.py) so only the former wakes the big tier.
    big_llm_complexity_confidence_threshold: float = float(
        os.getenv("WEATHERGPT_BIG_LLM_COMPLEXITY_CONFIDENCE_THRESHOLD", "0.6"))

    # Storage backends. The switch exists so promotion is a config change, not a rewrite;
    # today only the in-process value is implemented and anything else fails loudly at
    # import rather than silently falling back to memory.
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
    # Score gap required to auto-pick the top candidate; below it the request is ambiguous.
    # Scores are log10(population)-based, so 1.0 means "roughly an order of magnitude bigger".
    geocoding_dominance_margin: float = float(os.getenv("WEATHERGPT_GEOCODING_DOMINANCE_MARGIN", "1.0"))
    # OSM policy requires a real identifying User-Agent and at most 1 request/second.
    nominatim_user_agent: str = os.getenv("WEATHERGPT_NOMINATIM_USER_AGENT", "WeatherGPT/1.0 (weather intelligence backend)")
    nominatim_min_interval_seconds: float = float(os.getenv("WEATHERGPT_NOMINATIM_MIN_INTERVAL_SECONDS", "1.0"))
    # Primary geocoder when set; the existing keyless chain (Open-Meteo, Nominatim) is
    # unaffected when this is empty — GeoapifyGeocoder.search() just returns [].
    geoapify_api_key: str = os.getenv("GEOAPIFY_API_KEY", "")
    # Fallback marine source when OPEN_METEO_MARINE yields nothing; unconfigured means
    # only the primary marine source runs (same degrade-gracefully convention as IMD).
    stormglass_api_key: str = os.getenv("STORMGLASS_API_KEY", "")

    # Source credentials and endpoints
    cap_feed_url: str = os.getenv("CAP_FEED_URL", "https://cap-sources.s3.amazonaws.com/in-imd-en/rss.xml")
    cap_max_alerts: int = int(os.getenv("WEATHERGPT_CAP_MAX_ALERTS", "25"))
    imd_api_key: str = os.getenv("IMD_API_KEY", "")
    imd_api_base: str = os.getenv("IMD_API_BASE", "")
    # api.met.no rejects generic User-Agents; it must identify the deployment.
    met_norway_user_agent: str = os.getenv("WEATHERGPT_MET_NORWAY_USER_AGENT", "WeatherGPT/1.0 (weather intelligence backend)")

    # API protection
    # Server-to-server auth: a small, known set of trusted backend callers, not public
    # accounts — so a static key is config, not a login/JWT subsystem. Empty (unset)
    # disables the gate, matching every other opt-in feature flag below.
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

    # Query understanding — LLM-first location/time/intent/topic extraction, ahead of
    # geocoding. Mandatory on the request path by product decision; the confidence floor
    # is where a low-confidence LLM read is treated the same as off-topic.
    query_understanding_enabled: bool = _flag("WEATHERGPT_QUERY_UNDERSTANDING_ENABLED", "true")
    query_understanding_confidence_threshold: float = float(
        os.getenv("WEATHERGPT_QUERY_UNDERSTANDING_CONFIDENCE_THRESHOLD", "0.7"))

    # Follow-up context — short-circuits geocoding + time parsing for a conversational
    # follow-up that names no new location. A dedicated (short, fixed) TTL, independent of
    # session_ttl_seconds: a stale location silently answering a new question is worse than
    # a cache miss, so this must not inherit whatever TTL general session state settles on.
    follow_up_context_enabled: bool = _flag("WEATHERGPT_FOLLOW_UP_CONTEXT_ENABLED", "true")
    follow_up_context_ttl_seconds: int = int(os.getenv("WEATHERGPT_FOLLOW_UP_CONTEXT_TTL_SECONDS", "300"))
    follow_up_context_max_entries: int = int(os.getenv("WEATHERGPT_FOLLOW_UP_CONTEXT_MAX_ENTRIES", "4096"))

    # A VERIFY decision ("did you mean Bangalore?") stores its candidate here so the next
    # turn in the same session can confirm it instead of re-guessing. Short TTL: a stale
    # pending verification silently answering an unrelated later question is worse than
    # asking again.
    verify_pending_ttl_seconds: int = int(os.getenv("WEATHERGPT_VERIFY_PENDING_TTL_SECONDS", "120"))
    verify_pending_max_entries: int = int(os.getenv("WEATHERGPT_VERIFY_PENDING_MAX_ENTRIES", "4096"))

    cors_origins: tuple[str, ...] = tuple(
        item.strip() for item in os.getenv("WEATHERGPT_CORS_ORIGINS", "").split(",") if item.strip()
    )

    @property
    def rank_weights_total(self) -> float:
        return (self.rank_weight_authority + self.rank_weight_freshness + self.rank_weight_spatial
                + self.rank_weight_quality + self.rank_weight_temporal)


settings = Settings()
