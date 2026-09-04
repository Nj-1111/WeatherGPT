# Tasks: Intelligent Query Guardrail & Dispatch

See [tasks/plan.md](plan.md) for ordering rationale.

## guardrail-template — done

- [x] Task: Add `GuardrailAction`/`ClarifyReason`/`GuardrailDecision` to `app/schemas/query.py`
  - Acceptance: schema validates all five actions and both clarify reasons
  - Verify: `pytest tests/test_query_guardrail.py`
  - Files: `app/schemas/query.py`
- [x] Task: Build `app/services/query_guardrail.py` (prompt, LLM call, deterministic fallback, message rendering)
  - Acceptance: every action reachable via stubbed LLM; fallback never produces CLARIFY(garbled_input)/VERIFY; malformed/fenced JSON and unknown action degrade safely
  - Verify: `pytest tests/test_query_guardrail.py` (17 tests, all passing)
  - Files: `app/services/query_guardrail.py`
- [x] Task: Confirm zero regression to the existing pipeline
  - Acceptance: full suite unaffected (this module isn't imported anywhere yet)
  - Verify: `pytest -q` (232 passed + 1 pre-existing unrelated failure), `ruff check app tests`, `mypy app`
  - Files: none (verification only)

## intent-dispatch — done

- [x] Task: Extend `session_router.py` to stash a pending `verify_candidate` per `session_id`
  - Verify: `pytest tests/test_session_router.py` (11 tests, all passing)
  - Files: `app/services/session_router.py`
- [x] Task: Rewrite `app/main.py`'s `_understand_query`/`_weather_request` to call `run_guardrail` and branch on `GuardrailAction`
  - Verify: manual `uvicorn` smoke test (location-only, full-weather, off-topic, clarify all confirmed working) + `pytest -q`
  - Files: `app/main.py`
- [x] Task: Retire `app/services/query_extractor.py` and `NormalizedQuery`/`QueryIntent`, migrate their tests
  - Verify: `grep -rn "NormalizedQuery\|QueryIntent\|extract_and_normalize" app/ tests/` returns nothing
  - Files: deleted `app/services/query_extractor.py`, `tests/test_robust_pipeline.py`; edited `app/schemas/query.py`, `tests/test_session_router.py`, added `tests/test_query_guardrail.py` coverage for the two cases uniquely in the deleted file
- [x] Task: Full verification
  - Verify: `pytest -q` (226 passed + 1 pre-existing unrelated failure), `ruff check app tests`, `mypy app` — all clean
  - Files: none

## output-templates (basic) — done

- [x] Task: `LocationOnlyResponse` schema, wired identically into all 5 endpoints
  - Verify: live smoke test, `/query`/`/forecast`/`/decision` return byte-identical shape
  - Files: `app/schemas/api.py`, `app/main.py`
- [x] Task: config-driven tone directive interpolated into the explanation prompt
  - Verify: `tests/test_reviewer.py::test_explanation_prompt_carries_the_configured_tone_directive`
  - Files: `app/config.py`, `app/agents/orchestrator.py`, `.env.example`
- [x] Task: full verification
  - Verify: `pytest -q` (227 passed + 1 pre-existing unrelated failure), ruff/mypy clean
  - Files: none

## geoapify-provider — done

- [x] Task: `GeoapifyGeocoder` implementing the existing `LocationProvider` protocol
  - Verify: `pytest tests/test_location.py` (8 new tests), live smoke test against the real API
  - Files: `app/services/location_resolver/providers/geoapify.py`
- [x] Task: wire into `_GEOCODERS` as primary, add config + `.env.example` entry
  - Verify: chain-order test; unconfigured case falls through unchanged
  - Files: `app/services/location_resolver/__init__.py`, `app/config.py`, `.env.example`
- [x] Task: full verification
  - Verify: `pytest -q` (235 passed + 1 pre-existing unrelated failure), ruff/mypy clean,
    live query against the real Geoapify API confirms correct field mapping and that
    ambiguity detection still works against its result shape
  - Files: none

## topic-scope-expansion (4 modules) — done

- [x] Task: `guardrail-disaster-broadening` — new `UNSUPPORTED_TOPIC` action, widened `TOPIC_WORDS`, fixed the "to X" location-extraction gap it exposed
  - Verify: `pytest tests/test_query_guardrail.py tests/test_bugfixes.py`, live smoke test (earthquake honest-unsupported, cyclone/route now full pipeline)
  - Files: `app/schemas/query.py`, `app/services/{guardrail,query_guardrail}.py`, `app/services/location_resolver/normalize.py`, `app/main.py`
- [x] Task: `marine-schema-foundation` — 6 new canonical variables, `WIOWeather.marine` panel, retrieval planner wiring
  - Verify: `pytest tests/test_fusion.py tests/test_retrieval.py`
  - Files: `app/schemas/{ceo,wio}.py`, `app/services/{variable_registry,wio_builder}.py`, `app/orchestrator/retrieval_planner.py`
- [x] Task: `marine-adapter-open-meteo` — primary marine adapter, keyless
  - Verify: `pytest tests/test_adapters.py`, live-verified against the real API (confirmed field names/units, including the km/h current-velocity correction)
  - Files: `app/adapters/open_meteo_marine.py`, `app/adapters/registry.py`, `app/constants.py`
- [x] Task: `marine-adapter-stormglass` — fallback marine adapter, keyed
  - Verify: `pytest tests/test_adapters.py`; degrades cleanly without a key (confirmed live); NOT live-verified with a real key (needs paid signup)
  - Files: `app/adapters/stormglass_adapter.py`, `app/config.py`, `.env.example`
- [x] Task: full verification
  - Verify: `pytest -q` (256 passed + 1 pre-existing unrelated failure), ruff/mypy clean throughout
  - Files: none

## Not started

Nothing — only `rag-app-info` remains deferred (see CAPABILITY_MAP.md).
All future work follows `coding_rules.md`.
