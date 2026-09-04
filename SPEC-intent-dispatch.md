# Spec: intent-dispatch

Module of [CAPABILITY_MAP.md](CAPABILITY_MAP.md). Status: **built**. Depends on
`guardrail-template` ([SPEC-guardrail-template.md](SPEC-guardrail-template.md)).

## Objective

`guardrail-template` produces a `GuardrailDecision` but nothing consumed it — every
request still ran the full retrieval/fusion/agents/RADE pipeline regardless of what was
asked. This module makes the decision the real dispatch key `app/main.py` was missing:

- `ACCEPT_LOCATION_ONLY` → resolve the location and return immediately. No time parsing,
  no retrieval, no fusion, no agents, no RADE.
- `ACCEPT_WEATHER_FULL` → the existing full pipeline, byte-for-byte unchanged.
- `REJECT_OFF_TOPIC` / `CLARIFY` / `VERIFY` → the fixed template message, immediately, no
  location resolution attempted at all.
- A confirmed `VERIFY` (the next turn in the same session affirms the guessed candidate)
  resolves without re-guessing.

Also retires `app/services/query_extractor.py` and `NormalizedQuery`/`QueryIntent` —
`guardrail-template` deliberately left them in place so nothing could regress while it
was built in isolation; this module is where they're actually replaced.

## Tech Stack

Same as `guardrail-template` — no new dependency. Reuses `app/storage/memory.py`'s
`InMemorySessionStore` for the new pending-verification state (mirrors how
`session_router.py` already caches follow-up location context).

## Project Structure

```
app/main.py                     → _resolve_guardrail_decision, _weather_request rewritten
                                   to branch on GuardrailAction; the three POST handlers
                                   and two GET convenience routes updated for the new
                                   ResolvedLocation | tuple return shape
app/services/session_router.py  → store_pending_verification / consume_pending_verification
                                   (new), evaluate_follow_up's param type updated
app/services/query_guardrail.py → resolve_confirmed_location (new)
app/schemas/query.py             → NormalizedQuery/QueryIntent removed (dead once
                                   query_extractor.py had no callers)
app/services/query_extractor.py → deleted
tests/test_robust_pipeline.py    → deleted (fully superseded by tests/test_query_guardrail.py,
                                   which gained the two cases it uniquely covered:
                                   location/date separation, real-gateway-failure fallback)
tests/test_session_router.py     → migrated to GuardrailDecision, gained pending-verification tests
```

## Code Style

`_weather_request` returns `ResolvedLocation | tuple[...]` rather than inventing a new
result wrapper type — callers check `isinstance(result, ResolvedLocation)` once and
branch, matching the existing pattern where callers already unpack the tuple explicitly.
No exceptions-as-control-flow for the success path; `WeatherGPTError` (the existing
mechanism) is reused as-is for the three short-circuit rejection cases.

## Testing Strategy

Unit tests for the new session_router functions (`tests/test_session_router.py`); the
full existing suite as the regression check, since this module touches the live request
path. A manual `uvicorn` smoke test covering all four reachable branches (location-only,
full-weather, off-topic, clarify) — see Success Criteria.

## Boundaries

- **Always:** run `pytest`/`ruff`/`mypy` before considering this done; follow
  `coding_rules.md` for every line touched.
- **Ask first:** changing the `_GUARDRAIL_ERROR_CODES` mapping or the affirmation-word
  list — both are product-facing decisions, not implementation details.
- **Never:** let `ACCEPT_LOCATION_ONLY` reach retrieval/fusion/agents/RADE — that defeats
  the entire point of this module.

## Success Criteria

- [x] `pytest -q`: 226 passed, only the one pre-existing unrelated failure
      (`test_reviewer.py::test_fabricated_value_returns_503_end_to_end`, small LLM tier
      unconfigured in the test environment)
- [x] `ruff check app tests` / `mypy app` clean
- [x] Live smoke test: `"what are the coordinates of Bangalore?"` → `~1s` (geocoding only,
      no retrieval), `"Bengaluru is at 12.97194, 77.59369."`
- [x] Live smoke test: `"will it rain in Bangalore tomorrow?"` → unchanged full-pipeline
      answer (72 evidence records)
- [x] Live smoke test: `"who is the prime minister of India?"` → `400 QUESTION_REJECTED`
- [x] Live smoke test: `"what are the coordinates"` (no place) → `400 CLARIFICATION_NEEDED`
- [x] `query_extractor.py`/`NormalizedQuery`/`QueryIntent` fully removed, zero references left

## Open Questions

None outstanding. `VERIFY`'s confirmation flow is unit-tested but not live-smoke-tested
here since it requires a configured LLM (the deterministic fallback never produces
VERIFY by design) — covered by `tests/test_query_guardrail.py` and
`tests/test_session_router.py`'s pending-verification tests instead.
