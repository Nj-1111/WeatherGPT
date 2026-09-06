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
| `multi-location` | `QueryRequestV1` accepts more than one location; WIO/response shape for a side-by-side comparison | — | not started — schema-level change |
| `multi-time` | Same location(s), multiple time windows in one request ("today vs tomorrow") | `multi-location` | not started |
| `new-source-visibility` | Add `visibility` to `open_meteo_forecast.py`'s requested hourly fields | — | not started — config-level, cheapest new-source item |
| `new-source-aqi` | New adapter against Open-Meteo's separate Air Quality API, same `REGISTRY` pattern as every existing source | — | not started, **[not live-verified]** |
| `new-source-sunrise-sunset` | Open-Meteo's `daily` parameter set | — | not started, **[not live-verified]** |
| `new-source-tides-moon-astro` | Moonrise/moonset, tide timetables, equinox/solstice, eclipse/panchang-level data | — | deferred, unscoped — no source identified yet |
| `personalization` | Profile-aware phrasing/detail level beyond the existing `profile` dict passthrough | `response-shape` | deferred, unscoped |

**Build order:** `poi-geocoding` → `multi-location` → `multi-time` → new-source rows
(cheapest first: `new-source-visibility`, then `new-source-aqi`/`new-source-sunrise-sunset`,
each needing one live smoke test before writing the decoder — verify against the real API
first, same pattern every adapter in this repo has followed) → `new-source-tides-moon-astro`
(needs a source found before it can be scoped) → `response-shape` → `personalization` last.

---

## Done: notes

**`lang-match`** — detection piggybacks on the existing guardrail LLM call (no second
round-trip). `query_guardrail.py`'s prompt asks for a `detected_lang` ISO 639-1 code
alongside its decision; `GuardrailDecision.detected_lang` carries it, defaulting to `"en"`
when the LLM omits it or the deterministic fallback runs. `QueryRequestV1.language` is
`str | None` so a caller override is distinguishable from "not set." `app/main.py` resolves
one `effective_lang` (override, else detection, else `"en"`) and threads it into
`wio.query.lang`, the single value `run_all_agents`/`run_explanation_agent` read.
**What's NOT covered**: only the LLM explanation agent's own `{lang}` prompt slot is
translated. The four fixed guardrail messages, `wio_builder.py`'s summary sentences,
`rade/v2.py`'s rationale strings, and `main.py:_synthesize`'s scaffolding all stay
English-only by design (they're deliberately never LLM-composed) — a Bengali question that
hits `REJECT_OFF_TOPIC`/`CLARIFY` still gets an English message. Closing that needs a
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

---

## What already exists, so it is not re-proposed here

- **Wind speed** — live, every forecast source.
- **Wave height, ocean current velocity/direction** — live, `OPEN_METEO_MARINE`
  (`app/services/wio_builder.py`, `_MARINE_PEAK_FIELDS`/`_MARINE_LATEST_FIELDS`).
- **The registry pattern for adding a new source** — `app/adapters/registry.py`'s
  `REGISTRY` dict plus the shared pooled `httpx` client supports a new adapter with zero
  pipeline changes (proven by `OPEN_METEO_MARINE`, `MET_NORWAY`, `STORMGLASS`, `GEOAPIFY`).
  Every `new-source-*` row reuses this, not a new mechanism.
