# Spec: output-templates (basic)

Module of [CAPABILITY_MAP.md](CAPABILITY_MAP.md). Status: **built, basic version** — by
design, meant to be customized further (exact tone wording, possibly richer per-branch
schemas) rather than treated as final. Depends on `intent-dispatch`.

## Objective

Two gaps left after `intent-dispatch`:

1. `ACCEPT_LOCATION_ONLY` returned five slightly different ad hoc dicts, one per
   endpoint, instead of one declared shape.
2. No output-side tone control existed at all — the explanation LLM's prose had grounding
   constraints (`_EXPLANATION_SYSTEM`) but no style/tone instruction.

`REJECT_OFF_TOPIC`/`CLARIFY`/`VERIFY` needed no work — they already share one consistent
shape via the existing `WeatherGPTError` envelope.

## What was built

**`LocationOnlyResponse`** (`app/schemas/api.py`): `answer: str`, `location:
ResolvedLocation`, `request_id: str`. Returned identically from all 5 endpoints
(`/query`, `/wio/query`, `/decision`, `/warnings/active`, `/forecast`) via one helper,
`_location_only_response` (`app/main.py`) — verified live: the same request phrased as a
coordinates-only question returns byte-identical `answer`/`location` shape from `/query`,
`/forecast`, and `/decision`.

**Tone directive**: `Settings.explanation_tone_directive` (`app/config.py`,
`WEATHERGPT_EXPLANATION_TONE` env var, default `"clear, neutral, and helpful — not overly
casual, not overly formal"`), interpolated into `_EXPLANATION_SYSTEM`
(`app/agents/orchestrator.py`) as `"Tone: {tone_directive}."`. Editable via `.env` with no
code change, same pattern as the input guardrail's own `_CLARIFY_MESSAGES`.

**Important limitation, by design, not a gap to close later without a real decision:**
this is a *prompt-level instruction*, not a *verified gate*. The input guardrail's
`action` field is strictly branched on in code — there's no way to bypass it. Tone has no
equivalent: nothing mechanically checks the explanation LLM's prose actually matches the
configured tone the way the reviewer mechanically re-derives numeric claims. If stronger
enforcement is ever wanted, that's new scope (e.g. a second LLM call to classify the
output's tone, which trades latency for enforcement) — not something the current
5-line prompt addition provides.

## Files changed

`app/schemas/api.py`, `app/main.py`, `app/config.py`, `app/agents/orchestrator.py`,
`.env.example`, `tests/test_reviewer.py` (new test asserting the tone directive reaches
the rendered prompt).

## Success Criteria

- [x] `pytest -q`: 227 passed + the same 1 pre-existing unrelated failure
- [x] `ruff check app tests` / `mypy app` clean
- [x] Live smoke test: `/query`, `/forecast`, `/decision` all return byte-identical
      `LocationOnlyResponse` shape for the same coordinates-only question
- [x] `explanation_tone_directive` provably reaches the LLM's system prompt
      (`test_explanation_prompt_carries_the_configured_tone_directive`)

## Open Questions

The exact tone wording is yours to set — the current default is a neutral placeholder.
Whether stronger (verified, not just instructed) tone enforcement is ever needed is an
open product decision, not scoped here.
