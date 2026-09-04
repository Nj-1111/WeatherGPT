# Plan: Intelligent Query Guardrail & Dispatch

See [CAPABILITY_MAP.md](../CAPABILITY_MAP.md) for the full module list and locked-in
decisions. This tracks implementation order and status across modules.

## Order and rationale

1. **`guardrail-template`** (built) — the classifier must exist and be proven correct in
   isolation before anything wires into it; it touches no live request path, so it carries
   zero regression risk to build first.
2. **`intent-dispatch`** (next) — the highest-value piece: makes `ACCEPT_LOCATION_ONLY`
   actually skip retrieval/fusion/agents/RADE (the latency win that motivated this whole
   initiative), and makes `REJECT_OFF_TOPIC`/`CLARIFY`/`VERIFY` return immediately instead
   of falling through to the old boolean gate. Retires `query_extractor.py`/
   `NormalizedQuery` once nothing references them. Touches `app/main.py` and
   `app/services/session_router.py` — the live request path — so it's sequenced right
   after the classifier it depends on is already verified, not bundled with it.
3. **`output-templates`** — only meaningful once `intent-dispatch` exists (there's nothing
   to template differently per branch until the branches themselves exist).
4. **`geoapify-provider`** / **`adapter-extensibility`** — independent of the above, can be
   picked up any time; not sequenced relative to 1–3.
5. **`topic-scope-expansion`** / **`rag-app-info`** — deferred, no spec until their
   blocking questions (data sources) are resolved.

## Verification checkpoint after each module

`pytest -q`, `ruff check app tests`, `mypy app` — all clean before moving to the next
module. `intent-dispatch` additionally needs a manual smoke test against a running
`uvicorn` instance (a location-only question, a full-weather question, an off-topic
question, a garbled question) since it changes the live `/query` response shape.

## Risks

- `intent-dispatch`'s `VERIFY` action needs a place to stash `verify_candidate` against
  `session_id` so a user's next message ("yes") can confirm it — this needs a small
  extension to `session_router.py`'s existing follow-up-cache pattern, not a new store.
  Flagged now so it isn't a surprise mid-module.
- Retiring `query_extractor.py` touches `tests/test_robust_pipeline.py` and
  `tests/test_session_router.py`, both of which construct/assert the old
  `NormalizedQuery` shape directly — these need rewriting to the new schema as part of
  `intent-dispatch`, not left half-migrated.
