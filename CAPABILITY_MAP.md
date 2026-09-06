# Capability Map: Intelligent Query Guardrail & Dispatch

Approved 2026-09-04. Build order below; every module is now built or explicitly deferred.

| Module id | Responsibility | Depends on | Status |
|---|---|---|---|
| `guardrail-template` | Structured LLM decision step: ACCEPT_LOCATION_ONLY / ACCEPT_WEATHER_FULL / REJECT_OFF_TOPIC / CLARIFY / VERIFY, strictly template-driven (fixed decision-tree prompt, not the LLM's own judgment) | — | **Built** — `app/services/query_guardrail.py`, `tests/test_query_guardrail.py` |
| `intent-dispatch` | Wires the guardrail's decision into `main.py`'s actual control flow: location-only fast path (skips retrieval/fusion/agents/RADE entirely), clarify/verify short-circuit (return immediately, no location resolution attempted), full-pipeline path unchanged. Also retires the now-superseded `query_extractor.py`/`NormalizedQuery`. | `guardrail-template` | **Built** |
| `output-templates` | Strict per-branch response schema (reject / clarify / verify / location-only / full-weather), plus a config-driven output tone directive for the explanation LLM | `intent-dispatch` | **Built (basic)** |
| `guardrail-disaster-broadening` | Widen accepted topics (marine/fishing, mountain/trek, weather-driven disaster, route/travel); new `UNSUPPORTED_TOPIC` action for hazard types with no data source (earthquake, tsunami, wildfire, landslide, volcanic, drought) | `guardrail-template` | **Built** |
| `marine-schema-foundation` | New `CanonicalVariable`/`EvidenceSource` enum values, `variable_registry.py` entries, `WIOWeather.marine` panel, `wio_builder.py` panel logic, `retrieval_planner.py` marine keyword/source wiring | — | **Built** |
| `marine-adapter-open-meteo` | Primary marine adapter — Open-Meteo Marine Weather API (keyless) | `marine-schema-foundation` | **Built, live-verified** |
| `marine-adapter-stormglass` | Fallback marine adapter — StormGlass.io (keyed, optional) | `marine-adapter-open-meteo` | **Built, not live-verified** (needs a paid key) |
| `geoapify-provider` | New `GeocodingProvider` implementation (Geoapify as primary geocoder), existing chain (Open-Meteo → Nominatim → India Post) kept as fallback | — (independent) | **Built** |
| `adapter-extensibility` | Verification pass confirming `app/adapters/registry.py`'s `REGISTRY` pattern already supports adding weather-API providers without new architecture | — (independent) | **Verified** — proven by the two new marine adapters registering with zero pipeline changes |
| `rag-app-info` | Vector-DB RAG for "what is this app" queries | — | **Deferred** — future scope, not specced |

**Build order:** `guardrail-template` → `intent-dispatch` → `output-templates` →
`guardrail-disaster-broadening` → `marine-schema-foundation` →
`marine-adapter-open-meteo` → `marine-adapter-stormglass`; `geoapify-provider` ran
independently in parallel. Only `rag-app-info` remains deferred.

All code in this repo must follow the coding rules in `CLAUDE.md` — checked on every module.

The per-module `SPEC-*.md` files were merged into this table and deleted 2026-09-05; each
module's status column is the surviving record. `git show HEAD~1 -- SPEC-<id>.md` recovers one.

## Decisions locked in (do not re-litigate without the user)

1. ~~Disaster/food/medical/marine-safety topics: rejected for now~~ — **superseded**: weather-driven disaster (cyclone/flood/storm/heat) and marine data are now built. Food/medical safety remain out of scope (no data source, not attempted). Non-weather disaster types (earthquake, tsunami, wildfire, landslide, volcanic, drought) are permanently honest-unsupported (`UNSUPPORTED_TOPIC`), not a placeholder for future work — there is no plan to acquire seismic/wildfire data.
2. Clarify/verify wording: **fixed deterministic templates**, selected by the LLM's classified action/reason but never composed by the LLM.
3. Geocoding: **Geoapify as primary**, existing provider chain (Open-Meteo → Nominatim → India Post) as fallback — added as a new `GeocodingProvider`, not a replacement of the resolver package.
