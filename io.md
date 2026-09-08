# io.md — Input/Output Generalization Roadmap

**Living document.** Update this file in the same change that touches anything it
describes — a language fix, a new source, a schema change to `QueryRequestV1`/WIO. If you
fix or build one row of the capability map, move its status and add one line, in the same
commit. Keep entries factual and current, not a session diary — the "why" for a *closed*
item belongs in `CLAUDE.md`'s session records; this file tracks what's live and what's next.

---

## Capability map

| id | responsibility | depends on | status |
|---|---|---|---|
| `lang-match` | Detect the input's language; force every output surface into that same language | — | **partially done** — see note |
| `warning-agent-geofilter` | Warning claims/evidence stop carrying every nationwide CAP alert, not just the WIO's `official_warning` field | — | **done (2026-09-06)** |
| `location-message-fix` | `LocationAmbiguousError` stops dead-ending as a raw 409 | — | **done differently than scoped — see note** |
| `response-shape` | Trim/structure the response now that the warning-agent fix removes most of the current bloat; consider a `detail` request parameter | `warning-agent-geofilter` | not started |
| `poi-geocoding` | Resolve local landmarks (ghats, parks, named points of interest), not just administrative places | — | not started — needs a POI-capable provider identified and evaluated |
| `capability-selector` | Guardrail LLM call also picks which weather capabilities a query needs (closed enum), replacing keyword-only retrieval triggering while fetching stays fully deterministic | — | **done (2026-09-07)** — see note |
| `multi-location` | `QueryRequestV1` accepts more than one location; WIO/response shape for a side-by-side comparison | — | **done (2026-09-07)** — see note |
| `multi-time` | Same location(s), multiple time windows in one request ("today vs tomorrow") | `multi-location` | **done (2026-09-07)** — same change as `multi-location`, see note |
| `new-source-visibility` | Add `visibility` to `open_meteo_forecast.py`'s requested hourly fields | — | **done (2026-09-07)** — folded into `capability-selector` |
| `new-source-aqi` | New adapter against Open-Meteo's separate Air Quality API, same `REGISTRY` pattern as every existing source | — | not started, **[not live-verified]** |
| `new-source-sunrise-sunset` | Open-Meteo's `daily` parameter set | — | not started, **[not live-verified]** |
| `new-source-tides-moon-astro` | Moonrise/moonset, tide timetables, equinox/solstice, eclipse/panchang-level data | — | deferred, unscoped — no source identified yet |
| `personalization` | Profile-aware phrasing/detail level beyond the existing `profile` dict passthrough | `response-shape` | deferred, unscoped |
| `session-aware-guardrail` | Guardrail LLM call sees recent conversation turns, so a contextless follow-up ("can I go play in the evening" after "will it rain in Newtown") is read as a continuation instead of hard-rejected | — | **done (2026-09-08)** — see note |
| `output-guardrail` | Lenient post-hoc `check_output(response_text) -> OK \| FLAGGED` on the explanation LLM's response (toxicity keywords, leaked-instruction markers, obvious off-topic drift), wired in `main.py` right after `run_explanation_agent`; on `FLAGGED`, degrade through the same `status="partial"` fallback path an explanation failure already uses | — | not started |
| `gfs-full-wiring` | Install `cfgrib`/`eccodes`/`xarray` in the Docker image + system `libeccodes`; expand GFS's NOMADS request/decode from 2 variables (temp, precip) to 6 (+ wind speed/direction, humidity, pressure) | — | **done (2026-09-08)** — see note |
| `new-source-snowfall` | Open-Meteo forecast already has a `snowfall` hourly field alongside precipitation — same adapter, no new source | — | not started — untracked gap found 2026-09-08 |
| `new-source-pollen` | No source identified yet | — | not started — untracked gap found 2026-09-08 |
| `new-source-soil` | Soil moisture/temperature — Open-Meteo forecast has `soil_moisture_*`/`soil_temperature_*` hourly fields | — | not started — untracked gap found 2026-09-08 |
| `wind-gust-wiring` | `wind_gust` already exists in `CanonicalVariable`/`variable_registry.py` but no adapter populates it — Open-Meteo forecast has `wind_gusts_10m` alongside the `wind_speed_10m` it already fetches | — | not started — cheap win, schema-ready |

**Build order:** `poi-geocoding` → new-source rows (`new-source-aqi`/`new-source-sunrise-sunset`,
each needing one live smoke test before writing the decoder — verify against the real API
first, same pattern every adapter in this repo has followed) → `wind-gust-wiring` (cheapest
of the new rows, schema already exists) → `new-source-snowfall`/`new-source-soil` (same
adapter, live smoke test first) → `new-source-pollen`/`new-source-tides-moon-astro` (both
need a source found before they can be scoped) → `response-shape` → `personalization` last.

---

## Done: notes

**`lang-match`** — detection piggybacks on the existing guardrail LLM call (no second
round-trip). `query_guardrail.py`'s prompt asks for a `detected_lang` ISO 639-1 code
alongside its decision; `GuardrailDecision.detected_lang` carries it, defaulting to `"en"`
when the LLM omits it or the deterministic fallback runs. `QueryRequestV1.language` is
`str | None` so a caller override is distinguishable from "not set." `app/main.py` resolves
one `effective_lang` (override, else detection, else `"en"`) and threads it into
`wio.query.lang`, the single value `run_all_agents`/`run_explanation_agent` read.
**What's NOT covered**: the LLM explanation agent's own `{lang}` prompt slot, and (as of
`session-aware-guardrail`, 2026-09-08) the guardrail's `reject_off_topic`/`clarify`
templates for Hindi only (`query_guardrail.py`'s `_TRANSLATIONS` static table — no LLM
call, since the wording never changes). Every other detected language still falls back to
English for those two templates, and `verify`/`unsupported_topic` messages, plus
`wio_builder.py`'s summary sentences, `rade/v2.py`'s rationale strings, and
`main.py:_synthesize`'s scaffolding, all stay English-only by design (deliberately never
LLM-composed). Closing the rest needs either more entries in `_TRANSLATIONS` or a
translated-template design per surface, not an extension of this mechanism.

**`warning-agent-geofilter`** — one filter, `wio_builder.filter_covered_warnings(ceos,
resolved_location)`, applied once in `main.py` right after `validated_evidence` and before
`evidence_store.add_many`/`build_wio`. Drops warning-class CEOs that don't cover the query
location (reusing `wio_builder.covers`/`place_names`, now public); every other
`evidence_class` passes through untouched. Since both `wio.evidence` and
`run_warning_agent`'s claims read the same upstream `evidence` list, this single change
fixes both — `run_warning_agent`/`run_all_agents` needed zero code changes. Tests:
`tests/test_ceo.py::test_filter_covered_warnings_*`. **Not yet done**: a live re-run of the
original nationwide-CAP-alert scenario to record actual before/after evidence counts.

**`location-message-fix`** — built as a bigger fix than originally scoped: rather than
just correcting the 409's wording, `LocationAmbiguousError` now offers a one-turn
conversational disambiguation. `app/main.py`'s `_resolve_location` stores the candidate
list in session state (`session_router.store_pending_disambiguation`/
`consume_pending_disambiguation`) and returns a numbered message ("Did you mean: 1) X,
state; 2) Y, state?"). The next turn is matched against the stored candidates by
`app/services/disambiguation.py` — a small LLM call (prompt files under
`app/prompts/disambiguation/`) that only ever selects an index into already-geocoded data
or returns null, with a deterministic digit/ordinal/state-or-country-substring fallback
when the LLM is unavailable (matching on the shared place *name* is deliberately excluded
from that fallback — every candidate in a disambiguation list shares essentially the same
name, so name-matching just matches all of them at once). A match reuses
`resolve_confirmed_location` — the same function `VERIFY`'s own confirm flow already uses
— to re-enter the normal geocoding chain with a fully-qualified string.
`query_guardrail.py`'s own prompt was restructured into the same
`app/prompts/<agent>/{system_role,behavior_rules,output_format}.md` convention (zero
behavior change), so every LLM call site in this app now shares one
tunable-without-a-code-change prompt shape. Live-verified end to end (Kalyani, Digha).
**Rejected on evidence during scoping, not built**: a `ranking.py` scoring fix (using
Nominatim's own `importance` score) would have closed the concrete Howrah/Baruipur/Kalyani
sub-locality failures without needing conversational disambiguation at all, but the user
chose this route instead and asked that `ranking.py` not be touched — so those
sub-localities still cost one clarification round-trip rather than resolving in a single
shot. Also rejected: adopting komoot/photon as a geocoder — it indexes the same
OpenStreetMap data Nominatim already uses and performed worse in a live test; self-hosting
needs new Java+OpenSearch infrastructure beyond this repo's MLOps scope. Revisit only if
Nominatim's 1 req/s throttle becomes an actual production bottleneck (`FIXES.md` §2.3).

**`capability-selector`** — `GuardrailDecision.capabilities` (closed enum in
`retrieval_planner.py`: `DATA_CAPABILITIES` — temperature/precipitation/wind/marine/
extreme_events/humidity/pressure/cloud_cover/visibility/heat_stress — plus the
data-carrying-nothing `GUIDANCE_FLAGS` entry `travel_safety_guidance`) is OR'd alongside
`build_retrieval_plan`'s existing keyword triggers, never replacing them, so the
deterministic path is unaffected when the LLM is down. `humidity`/`pressure`/`cloud_cover`/
`visibility` are wiring-only (already-decoded Open-Meteo fields that were fetched and
silently dropped); `heat_stress` is a pure derived computation
(`wio_builder._heat_stress_panel`, NWS heat-index/wind-chill formulas) from panels already
built. Every new field on `GuardrailDecision` degrades independently — an invalid
capability name is dropped, not fatal; only a `GUIDANCE_FLAGS`-only selection (carries no
data on its own) triggers the bounded one-shot retry `run_guardrail` already had a slot for.
Live-verified across 2 batch runs: capabilities fired correctly on phrasings matching
*none* of the keyword lists ("is it humid", "fog on the road", "will it feel very hot"),
confirming genuine generalization past keyword matching.

**`multi-location`/`multi-time`** — built together as one schema pass, since both extend
the same `GuardrailDecision` object. `locations`/`time_phrases` replaced the old singular
`location`/`time` fields (kept as read-only `@computed_field` properties returning
`locations[0]`/`time_phrases[0]`, so existing call sites needed no atomic migration); a
guardrail-declared `pairing_mode` (`locations_x_shared_time` / `times_x_shared_location` /
`full_cross_product`) says how to pair them. `app/main.py` fans every pair beyond the
primary one out via `asyncio.gather` (`_resolve_pairs` + `_build_comparison_wio`) into an
additive `comparisons: list[WeatherIntelligenceObject] | None` response field — the
existing `wio` field is untouched, so no current caller's response shape breaks. Location
dedup-by-coordinate and per-pair RADE/agents are deliberately deferred (cost optimizations,
not correctness-blocking); the pending-followup clarifying-question mechanism only fires
for a single resolved pair (no defined rule yet for which of several borderline pairs would
get asked). Capped at `settings.max_location_time_pairs` (default 6) by silent truncation,
not a rejection — closes `BUG.md`'s B8.

**`session-aware-guardrail`** — closes `BUG.md`'s new B21 (distinct from B6/B7 — a
genuinely location-less first turn and a rule-6 phrasing inconsistency, respectively; this
is a *second* turn with no topic signal at all, which no stateless-classifier prompt fix
could close): the guardrail had no conversation memory at all, so a contextless follow-up
("can I go play in the evening" after "will it rain in Newtown") was judged on its own text
and hard-rejected.
`app/storage/sqlite.py`'s `SqliteConversationLog` (protocol-conformant, tested, but never
called by anything) is now wired live via `app/services/input_pipeline/
conversation_history.py`'s `record_turn`/`recent_turns` (both `asyncio.to_thread`-wrapped,
since the SQLite calls are synchronous — `FIXES.md` §2.8 stays open for the store in
general, but this hot path doesn't block the event loop). `app/main.py`'s
`_resolve_guardrail_decision` fetches the last `settings.guardrail_history_turns` (default
3) turns before calling `run_guardrail`, and records every branch's outcome (including the
pending-disambiguation/verify/followup resumption paths) so later turns have full context.
`run_guardrail(question, history=...)` renders history into the **user** message, never the
system prompt, so the decision-tree rules stay stable and cacheable independent of history;
`app/prompts/guardrail/behavior_rules.md` gained rule 0 (continuation) plus a `reasoning`
diagnostic output field (logged only, never schema-persisted or user-visible). The decision
cache key changed from question-text-alone to `(text, history)`, since the same text can now
mean different things depending on context — a stateless repeat query keeps its old cache
benefit (empty history hashes the same as before), a context-dependent follow-up does not
collide across different conversations. Net latency effect is close to zero: no second
LLM/network call, just more context in the existing one.

Same change also extended `conversational-system-prompt` work: `_EXPLANATION_SYSTEM`
(`app/agents/orchestrator.py`) moved from an inline string to `app/prompts/explanation/`
(matching every other LLM call site's `{system_role,behavior_rules,output_format}.md`
convention), gained an official-source-escalation sentence, and a `ContextRetriever` stub
(`app/services/input_pipeline/context_retriever.py`, returns `[]` today) is now wired into
`run_explanation_agent` → `_fact_sheet`'s new `context_docs` param, so RAG is a fill-in-the-
blank later, not a re-architecture.

**Live-verified 2026-09-08** against a real running server with real LLM keys, paced to
avoid rate limits: the Newtown/park-child scenario, a Hinglish umbrella follow-up, and the
existing marine 2-turn flow all resolved correctly end to end (real answers, not
rejections); the mountain-pass scenario correctly carried "Manali" forward into the same
genuine geocoding ambiguity turn 1 hit (2 real places named Manali) rather than either
rejecting or silently guessing — the right behavior, not a bug. Full detail in `BUG.md` B21.
**Also surfaced, not yet fixed**: an unpaced burst (~20 requests/minute) exhausted the Groq
primary tier's rate limit, cascading enough fallback traffic onto Gemini to exhaust its
quota too, degrading several turns to the deterministic fallback until the burst subsided —
see B21's note for detail; not filed as its own numbered bug yet.

**Deferred, not built this round**: `output-guardrail` (see roadmap row above).

**`gfs-full-wiring`** — the adapter/decoder (`app/adapters/grib2_adapter.py`,
`app/decoders/grib2_placeholder.py`) were already complete, not stubs — confirmed by direct
read before touching anything (Chesterton's Fence check), purely blocked on
`cfgrib`/`eccodes`/`xarray` not being in the Docker image. Two things fixed together:
1. **Installable**: `Dockerfile` now installs `libeccodes0`/`libeccodes-data` (apt) +
   `requirements-full.txt` (pip) instead of `requirements-api.txt` alone — live-verified by
   actually building the image and running `import cfgrib; import eccodes` inside a
   container (pip's `eccodes` package installs cleanly without the system library, then
   raises `RuntimeError: Cannot find the ecCodes library` on import — confirmed live, so the
   apt packages are genuinely required, not a defensive guess).
2. **More variables**: `_build_request()` now also requests `UGRD`/`VGRD` at 10m (wind),
   `RH` at 2m (humidity), `PRMSL` at mean sea level (pressure) alongside the existing
   `TMP`/`APCP`; `decode_grib2_file()` decodes all of them, combining u/v into
   `wind_speed`/`wind_direction` (meteorological "blowing from" convention, matching every
   other source in this repo) and converting PRMSL Pa→hPa. Live-verified end to end inside
   the built image: a real fetch against Nagpur's coordinates decoded 6 CEOs (temperature
   31.9°C, precipitation, wind speed 3.1 m/s, wind direction 314°, humidity 56%, pressure
   1007 hPa) — all physically plausible values, not just "didn't crash."

Side effect: building the image live hit "no space left on device" copying this repo's own
`.venv/` — there was no `.dockerignore` at all. Added one (`.venv/`, `.git/`,
`__pycache__/`, `.env`, `tests/`, `*.db`), which also closes half of `BUG.md` B20 (the
`.env`-in-image half); the `pytest`-in-runtime-image half of B20 stays open (a
`requirements-dev.txt` split, not a `.dockerignore` fix).

---

## What already exists, so it is not re-proposed here

- **Wind speed** — live, every forecast source.
- **Wave height, ocean current velocity/direction** — live, `OPEN_METEO_MARINE`
  (`app/services/wio_builder.py`, `_MARINE_PEAK_FIELDS`/`_MARINE_LATEST_FIELDS`).
- **The registry pattern for adding a new source** — `app/adapters/registry.py`'s
  `REGISTRY` dict plus the shared pooled `httpx` client supports a new adapter with zero
  pipeline changes (proven by `OPEN_METEO_MARINE`, `MET_NORWAY`, `STORMGLASS`, `GEOAPIFY`).
  Every `new-source-*` row reuses this, not a new mechanism.
