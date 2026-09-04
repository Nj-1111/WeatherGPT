# Spec: topic-scope-expansion (4 modules)

Covers `guardrail-disaster-broadening`, `marine-schema-foundation`,
`marine-adapter-open-meteo`, `marine-adapter-stormglass` from
[CAPABILITY_MAP.md](CAPABILITY_MAP.md). Status: **all built and verified**, live where
possible.

## Objective

`topic-scope-expansion` was deferred as "no data source exists yet." Investigation found
that was only half true — cyclone/flood/storm/heat-wave warnings and the RADE
"travel"/"marine" decision domains already existed end-to-end; the input guardrail's
narrow topic list was the only real blocker. Marine **currents** (as opposed to the
existing marine *decision* logic) had no adapter at all. This round: broaden the
guardrail intelligently (including hazard types this system has no data for, handled
honestly rather than either rejected or fabricated), and add marine data via a primary +
fallback adapter pair.

## 1. `guardrail-disaster-broadening`

New `GuardrailAction.UNSUPPORTED_TOPIC` (`app/schemas/query.py`) — distinct from
`REJECT_OFF_TOPIC`: a real disaster question (earthquake, tsunami, wildfire, landslide,
volcanic activity, drought) this system has no data for gets an honest "I don't have
data for X" instead of either a misleading rejection or fabricated generic weather data.
`query_guardrail.py`'s prompt and deterministic fallback both classify this before the
normal topic gate. `guardrail.py`'s `TOPIC_WORDS` widened (marine/mountain/route/disaster
vocabulary) so *acceptance* grew, not just a new rejection path.

**Bug found and fixed along the way:** `extract_place_phrase` (`normalize.py`) had no
lead pattern for "to" — "cyclone coming **to** Chennai" and "route **to** Pune" were
guardrail-accepted but then 422'd with `LOCATION_REQUIRED` since the location was never
extracted. Fixed by adding "to" to the generic catch-all lead pattern.

**Verified live:** earthquake → `400 TOPIC_NOT_SUPPORTED`; cyclone and route questions →
`200`, full pipeline (previously would have been rejected or 422'd).

## 2. `marine-schema-foundation`

Six new `CanonicalVariable` values (`app/schemas/ceo.py`), matching
`variable_registry.py` entries, `WIOWeather.marine` panel (`app/schemas/wio.py`), and
`_marine_panel` in `wio_builder.py` — hazard-relevant fields (wave height, current
velocity) report the window peak; direction/period/temperature fields report the most
recent reading (maxing a direction is meaningless). `retrieval_planner.py` gained a
`marine` variable family, triggered by wave/current/swell/tide keywords or the existing
RADE `"marine"` decision trigger (fishing/boat/sail).

## 3. `marine-adapter-open-meteo` (primary, keyless)

**Verified the real API live before writing the adapter** — confirmed exact field names
and, importantly, that `ocean_current_velocity` is natively **km/h**, not m/s as
originally planned; `variable_registry.py` and the panel key name were corrected to
match. `app/adapters/open_meteo_marine.py` mirrors the existing Open-Meteo adapter family
exactly (own inline `normalize()`, matching `open_meteo_historical.py`'s pattern rather
than the shared decoder the plain forecast adapter uses). New `MARINE_HEALTH_PROBE_LAT/
LON` (Bay of Bengal, off Chennai) — the existing health probe coordinate is inland
Nagpur, useless for a marine API.

**Verified live end-to-end**: a real wave-height question returns a fully-populated
marine panel with real values through the whole pipeline (fetch → normalize → semantic
gate → fusion → panel).

## 4. `marine-adapter-stormglass` (fallback, keyed)

`STORMGLASS_API_KEY` — same missing-key convention as `ImdAdapter` (`fetch()` raises,
`retrieval.py`'s existing per-source isolation handles it, confirmed live: reports
`"unavailable"` cleanly in both `/health` and per-request retrieval status with no
plumbing changes needed). **Not live-verified against a real key** — StormGlass requires
paid signup, unlike Open-Meteo's keyless API — built to its documented response shape
(per-field readings keyed by underlying model, "sg" preferred as StormGlass's own
consensus). Flagging this honestly: if you get a key, smoke-test it the same way Geoapify
was (add to `.env` yourself, never pasted into chat).

## Files changed

`app/schemas/{query,ceo,wio}.py`, `app/services/{guardrail,query_guardrail,
variable_registry,wio_builder}.py`, `app/services/location_resolver/normalize.py`,
`app/orchestrator/retrieval_planner.py`, `app/adapters/{registry,open_meteo_marine,
stormglass_adapter}.py`, `app/constants.py`, `app/config.py`, `app/main.py`,
`.env.example`, and matching test files for every module above.

## Success Criteria

- [x] `pytest -q`: 256 passed + the same 1 pre-existing unrelated failure throughout
- [x] `ruff check app tests` / `mypy app` clean at every step
- [x] Live: earthquake → honest unsupported-topic message; cyclone/route questions →
      full pipeline (previously blocked)
- [x] Live: real wave-height query returns a fully populated marine panel
- [x] Live: StormGlass without a key degrades cleanly, no crash, no new plumbing

## Open Questions

StormGlass's exact response shape is unverified — flagged, not hidden. `topic-scope`
still excludes non-weather-driven hazards from real data grounding by design (there is no
seismic/wildfire data source); `UNSUPPORTED_TOPIC` is the permanent, honest answer for
those, not a placeholder for future work.
