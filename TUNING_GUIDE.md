# Tuning Guide — where to edit for a given change

Practical map from "I want to change X" to the exact file. Not an architecture doc —
see `docs/SERVICES.md`/`CLAUDE.md` for that. This is for hands-on tinkering.

## Guardrail — what gets accepted, rejected, or asked to clarify

| Want to change... | Edit |
|---|---|
| The LLM's decision-tree rules (what counts as in-scope, what triggers CLARIFY/VERIFY/UNSUPPORTED_TOPIC) | `app/services/query_guardrail.py` — `_SYSTEM_PROMPT` |
| The exact wording of a clarify/verify/unsupported/reject message | same file — `_CLARIFY_MESSAGES` dict and `render_guardrail_message()` |
| Which keywords the *deterministic fallback* (LLM down) treats as location-only, or as an unsupported disaster type | same file — `_LOCATION_ONLY_WORDS`, `_UNSUPPORTED_DISASTER_WORDS` |
| Which keywords the cheap pre-LLM gate accepts as on-topic at all | `app/services/guardrail.py` — `TOPIC_WORDS` |
| Add a new `GuardrailAction` (a new kind of decision branch) | `app/schemas/query.py` (enum + `GuardrailDecision` fields) → wire the new branch into `app/main.py`'s `_GUARDRAIL_ERROR_CODES` / `_weather_request` |
| Confirmation-reply words for VERIFY ("yes"/"haan"/...) | `app/main.py` — `_AFFIRMATION_WORDS` |

## Output — the actual answer text and response shape

| Want to change... | Edit |
|---|---|
| The deterministic template sentence structure (`"Rain likely (...)"` etc.) | `app/services/wio_builder.py` — `_rain_panel`/`_wind_panel`/`_temperature_panel`/`_marine_panel` build the *numbers*; `app/main.py`'s `_synthesize()` assembles the final sentence |
| The explanation LLM's tone/style or grounding rules | `app/agents/orchestrator.py` — `_EXPLANATION_SYSTEM` (one domain-general prompt, no per-domain tone config) |
| The explanation LLM's max length | `WEATHERGPT_LLM_MAX_WORDS` in `.env` |
| The JSON response shape for a given branch | `app/schemas/api.py` (`LocationOnlyResponse`, etc.) + the matching handler in `app/main.py` |
| Which fields appear in the fused weather panel | `app/schemas/wio.py` (`WIOWeather`) + the corresponding `_*_panel` function in `wio_builder.py` |

## Decisions (RADE) — spray/irrigate/harvest/marine/travel advice

| Want to change... | Edit |
|---|---|
| The utility scores for each action (how favorable dry/wet/windy conditions are) | `app/rade/v2.py` — `POLICIES` dict |
| Which keywords select a decision domain | same file — `_domain()` |
| Risk tolerance mapping (low/medium/high → risk_lambda) | same file — `decide()`, the `risk_lambda` dict literal |
| Which optional profile fields a domain can ask a clarifying follow-up about when a decision is borderline | same file — `CLARIFYING_FIELDS` dict; the borderline threshold itself is `WEATHERGPT_RADE_BORDERLINE_SCORE_MARGIN` |

## Retrieval — which sources/variables get fetched for a given question

| Want to change... | Edit |
|---|---|
| Keyword → variable/source mapping (e.g. what triggers fetching marine data) | `app/orchestrator/retrieval_planner.py` — the `_*_WORDS` tuples and `build_retrieval_plan()` |
| What the guardrail LLM can request beyond keyword matches (the `capabilities` field — e.g. "fog on the road" → `visibility` with no keyword hit at all) | `app/orchestrator/retrieval_planner.py` — `DATA_CAPABILITIES`/`GUIDANCE_FLAGS`; guardrail-side selection rules live in `app/prompts/guardrail/behavior_rules.md` |
| Add a brand-new canonical variable | `app/schemas/ceo.py` (`CanonicalVariable` enum) + `app/services/variable_registry.py` (unit/statistic/evidence_class entry) — the semantic gate rejects anything not registered here |
| Add a brand-new data source/adapter | New file in `app/adapters/` implementing `WeatherSourceAdapter` (see `app/adapters/open_meteo_marine.py` as the simplest recent example), register it in `app/adapters/registry.py`'s `REGISTRY` dict — **no other file needs to change** to make it fetchable (verified this session: `retrieval.py` dispatches generically via `REGISTRY.get(source)`) |
| Location resolution priority (which geocoder is tried first) | `app/services/location_resolver/__init__.py` — `_GEOCODERS` tuple |
| India-bias scoring in ambiguous location matches | `app/services/location_resolver/ranking.py` |
| How many (location, time-window) pairs one multi-location/multi-time query can fan out into | `WEATHERGPT_MAX_LOCATION_TIME_PAIRS` in `.env` (default 6) — extra pairs beyond the cap are truncated, not rejected |

## Config knobs (no code change, just `.env`)

Every tunable scalar lives in `app/config.py` as a dataclass field reading an env var —
that file is the authoritative list. Notable ones:

- `WEATHERGPT_API_KEYS` — server-to-server auth gate (empty = off)
- `SMALL_LLM_MODEL` / `_BASE_URL` / `_KEY` (+ `_FALLBACK_1_*`) — the guardrail/explanation LLM chain
- `BIG_LLM_MODEL` / `_BASE_URL` / `_KEY` — woken by `run_explanation_agent`'s deterministic
  complexity trigger (`app/agents/orchestrator.py:_requires_big_llm`) on a low-confidence/
  deferred RADE decision or a source disagreement; empty falls back to the small tier
  (see CLAUDE.md). `WEATHERGPT_BIG_LLM_COMPLEXITY_CONFIDENCE_THRESHOLD` tunes the
  confidence floor that counts as "low" (default 0.6)
- `GEOAPIFY_API_KEY` — primary geocoder (empty = falls back to keyless Open-Meteo/Nominatim chain)
- `STORMGLASS_API_KEY` — fallback marine source (empty = Open-Meteo Marine only)
- `CAP_FEED_URL` — which CAP alert index feed to poll
- `WEATHERGPT_RATE_LIMIT_PER_MINUTE` / `_PER_DAY` — per-IP throttling
- `WEATHERGPT_HEALTH_CACHE_TTL_SECONDS` — how long `/health` caches its 8-source fan-out

## Tests — where coverage for each area lives

`tests/test_query_guardrail.py` (guardrail), `tests/test_fusion.py` (panel/fusion logic),
`tests/test_retrieval.py` (retrieval planning), `tests/test_adapters.py` (adapters),
`tests/test_location.py` (geocoding), `tests/test_reviewer.py` (anti-hallucination gate),
`tests/test_multi_location.py` (multi-location/time pairing and fan-out).
Run `pytest -q` after any change above — 380+ tests, ~10s.
