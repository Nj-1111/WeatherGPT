# Spec: guardrail-template

Module of [CAPABILITY_MAP.md](CAPABILITY_MAP.md). Status: **built**.

## Objective

Today, `app/services/query_extractor.py` extracts a `QueryIntent` enum but never branches
on it — the only thing that gates a request is a bare `is_weather_related` boolean
(`app/main.py:_understand_query`). This means every accepted request runs the full
retrieval/fusion/agents/RADE pipeline regardless of what was actually asked, and there is
no way to ask for clarification or verify an ambiguous location — only accept-full or
reject-outright.

This module replaces that boolean with a real, strictly template-driven decision:
`GuardrailAction` ∈ {ACCEPT_LOCATION_ONLY, ACCEPT_WEATHER_FULL, REJECT_OFF_TOPIC, CLARIFY,
VERIFY}, produced by one LLM call that applies a fixed decision-tree checklist (never its
own judgment), plus a deterministic fallback for when the LLM is unavailable. User-facing
text for every non-ACCEPT action comes from a fixed Python template, never LLM prose —
so wording is centrally editable and adds no latency.

This module intentionally does **not** wire the decision into `main.py`'s control flow —
that is `intent-dispatch`, the next module. Building and proving the classifier in
isolation first means nothing already working can regress while it's built.

## Tech Stack

Same as the rest of the repo: Python 3.10+, Pydantic v2, `app.llm.client.small_llm` (the
existing provider-agnostic LLM gateway) for the LLM call, stdlib `json`/`re` for parsing.
No new dependency.

## Project Structure

```
app/schemas/query.py            → GuardrailAction, ClarifyReason, GuardrailDecision (added
                                   alongside the existing NormalizedQuery/QueryIntent,
                                   which stay in place until intent-dispatch retires them)
app/services/query_guardrail.py → the module itself: prompt, LLM call, deterministic
                                   fallback, message rendering
tests/test_query_guardrail.py   → unit tests, small_llm stubbed (mirrors
                                   tests/test_robust_pipeline.py's approach for the
                                   sibling query_extractor.py)
```

## Code Style

Matches `app/services/query_extractor.py`'s existing shape exactly (same funnel pattern:
`_parse` → validate → construct, `_deterministic_fallback` → keyword-based safe default,
public `run_guardrail`/`render_guardrail_message` as the module's two entry points). No
new abstractions introduced beyond what the schema requires.

## Testing Strategy

`pytest`, colocated in `tests/`, one file per module (existing repo convention). Every
`GuardrailAction` covered via the LLM path (stubbed `small_llm`) and, where reachable
deterministically, via the fallback path. Malformed/fenced JSON and an unrecognized
`action` value both assert a safe fallback rather than an exception. Message rendering
tested per action independent of the classification tests.

## Boundaries

- **Always:** run `pytest`/`ruff`/`mypy` before considering a task done; never let the
  deterministic fallback return CLARIFY(garbled_input) or VERIFY — those need real
  language understanding, not a keyword match (see the module's own docstring).
- **Ask first:** any change to the decision-tree rule order, or to which actions the
  deterministic fallback can produce.
- **Never:** let the LLM compose the user-facing message text itself — it may only select
  an action/reason; wording is always the fixed Python template.

## Success Criteria

- [x] `GuardrailDecision`/`GuardrailAction`/`ClarifyReason` added to `app/schemas/query.py`
- [x] `run_guardrail(question) -> GuardrailDecision` classifies via LLM, degrades to
      deterministic fallback on unavailability/malformed output
- [x] `render_guardrail_message(decision) -> str | None` — fixed template per action/reason
- [x] Full existing test suite (232 passing + 1 pre-existing unrelated failure) unaffected
      — this module is additive only
- [x] `ruff check` / `mypy app` clean

## Open Questions

None outstanding for this module — the three design questions (topic scope, clarify/verify
wording, geocoding provider order) were resolved before this was built; see
CAPABILITY_MAP.md's "Decisions locked in."
