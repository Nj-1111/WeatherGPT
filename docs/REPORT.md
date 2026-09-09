# REPORT.md — status, defect history, and roadmap

Consolidates the former `AUDIT.md`, `BUG.md`, `FIXES.md`, and `io.md` into one document.
This is the compact index — **check "Currently open" first**; the narrative sections after
it are detail and history, not something you need to read to know what's outstanding.
Session-by-session "why" for closed items lives in `CLAUDE.md`'s session records, not here.

---

## Currently open

### Security

| # | Issue | Status |
|---|---|---|
| §2.3 | Nominatim throttle is a shared cross-tenant lock | open — scoped, declined (private VPC, no proxy in front; revisit if that changes) |
| §2.4 | Unrecognized claim `derivation.op` degrades to a warning, not an error | open |
| §2.8 | Synchronous SQLite calls block the event loop | open |

(§2.1, §2.2, §2.5, §2.6, §2.7, §2.9, §2.10, §2.11 are closed — see "Fixed, historical" below
if you need the detail.)

### Correctness

| Issue | Sev |
|---|---|
| ~~Pure Devanagari (or any script) rejected outright when the LLM is down~~ | **closed** — the deterministic fallback no longer treats a keyword miss as evidence of being off-topic; see "Language-proofing the guardrail" below |
| LLM guardrail is inconsistent on Indic queries even when the LLM *is* reachable (prompt-steering issue, not a fallback issue) — needs a live-harness re-run to distinguish improvement from noise | P1 |
| `CLARIFY(no_location)` unreachable in practice — "will it rain tomorrow" hits a raw 422 instead | P2 |
| General class: same intent, different phrasing → different guardrail action (one specific reported case fixed; the class is still open) | P2 |
| Reviewer's 503 diagnostic echoes both the fabricated and re-derived value to the client (not a leak of the number as response data — logged/error-detail only) | P2 |
| `REVIEW_FAILED` is the only 503 today; nothing stops a future one from being indistinguishable at the status-code level | P3 |
| `pytest` still installs into the runtime Docker image (the `.env`/`.dockerignore` half of this is closed; the dependency-split half is not) | P3 |
| An unpaced burst can still exhaust the small-LLM tier chain and cascade to the deterministic fallback — mitigated (3 endpoints in the chain instead of 2) but not eliminated; the 3rd (free-tier OpenRouter) endpoint itself showed transient 502s live | P2 |

### From the 2026-09-05 teardown

| Issue |
|---|
| `query_understanding_confidence_threshold` has zero readers; its comment claims it gates something it doesn't |
| Rain panel reports the window's peak probability but cites `probabilities[0]` — value and citation disagree |
| Reviewer 503s pooled with all 5xx in metrics; `wio_latency_ms_mean` divides by the wrong count |
| `retrieve()`'s `asyncio.gather` has no total deadline — one slow source still stalls the whole request |
| Explanation LLM call (~0.7-1.6s) runs on every request, uncached |

### Blocked on credentials / infra

- `IMD_API_KEY` — needs the EC2 elastic IP to exist first (registration is IP-whitelisted at `api.imd.gov.in/public/login.php`). CAP (NDMA's Sachet feed, live and keyless) already covers cyclone/flood/storm/heat warnings, so this is the richer route (forecasts, observations, rainfall), not the only route to Indian warnings.
- `STORMGLASS_API_KEY` — marine fallback built to its documented shape, never live-verified (needs a paid key). Primary marine source (`OPEN_METEO_MARINE`, keyless) works today.
- `app/services/model_client.py` — bias-correction integration doesn't exist yet. Contract is below, in "Bias-correction model integration contract".

### Roadmap — not started

1. `poi-geocoding` — landmark/POI resolution; needs a provider evaluated (Photon rejected — indexes the same OSM data Nominatim already uses and performed worse in a live test)
2. `new-source-aqi` / `new-source-sunrise-sunset` (each needs a live smoke test before writing the decoder) → `new-source-tides-moon-astro` (no source picked yet)
3. `new-source-snowfall` / `new-source-soil` — Open-Meteo forecast already has the hourly fields (`snowfall`, `soil_moisture_*`/`soil_temperature_*`); same adapter pattern, needs a live smoke test first
4. `new-source-pollen` — no source identified yet
5. `wind_gust` wiring — cheapest win: `wind_gust` already exists in `CanonicalVariable`/`variable_registry.py`, but no adapter populates it, though Open-Meteo forecast already fetches the sibling field `wind_gusts_10m`
6. `response-shape` — trim the response payload
7. `personalization` — deferred, unscoped
8. `output-guardrail` — a lenient post-hoc `check_output(response_text) -> OK | FLAGGED` on the explanation LLM's response, wired right after `run_explanation_agent`; degrades through the same `status="partial"` path an explanation failure already uses

### Accepted, not bugs (deliberate trades — don't "fix" these without a real reason)

- Sub-locality queries (Kalyani/Howrah-style) cost one clarification round-trip by design — `ranking.py`'s scoring was deliberately left untouched in favor of conversational disambiguation.
- Agreement thresholds are absolute (10mm / 3°C) — "both sources say no rain" reads identically to "both say 40mm"; two sources whose day totals differ 2.7x (1.4mm vs 3.8mm, live-verified) still read as `full_agreement`.
- `historical`/`observation` agents slice `[:2]` off class-filtered evidence rather than ranked output — arbitrary, but each claim cites its own CEO so citations stay self-consistent.
- Caches and the evidence store are per-process — run one uvicorn worker until they move to Redis.
- Rate limiting keys on `request.client.host`; `X-Forwarded-For` is deliberately not trusted (spoofable). Correct for the current single-tenant private-VPC deployment; revisit behind a real reverse proxy.
- Nominatim's 1 req/s throttle is a single process-wide lock, not per-caller pacing — same accepted tradeoff as §2.3 above.

---

## Language-proofing the guardrail (2026-09-09)

The deterministic fallback (used only when the classifying LLM is unreachable or returns
unparseable output) used to treat a miss against a hardcoded English/Hindi-transliteration
keyword list as proof a question was off-topic — so any language outside that list
(Urdu, Malayalam, Bengali in Latin script, anything) got a false `REJECT_OFF_TOPIC` the
moment the system degraded, even for a genuine weather question. Live-reproduced:
`"kolkata te ajke brishti hobe ki"` (Bengali) was rejected on one run and succeeded on
another, purely depending on whether that request happened to hit the LLM path or the
fallback.

**Fix:** a keyword *hit* is still a safe positive signal (fast-path accept/route, unchanged
behavior). A keyword *miss* is never again used as evidence of being off-topic — the
fallback has no reliable way to judge relevance without the LLM, so it now returns
`CLARIFY(service_degraded)` (a new `ClarifyReason` value) instead of confidently guessing
wrong. Worst case during an outage is one extra clarifying round-trip, never a wrongful
rejection. `TOPIC_WORDS` itself was kept (not deleted) as a positive-only accelerator —
removing working code that can only ever help was unnecessary; the actual defect was
letting its absence mean something it can't prove.

The fixed-template message language selection (previously English/Hindi only, via a static
`_TRANSLATIONS` table) now also falls back to a stdlib Unicode-script-range guess
(Devanagari/Bengali/Tamil/Telugu/Kannada/Malayalam/Gujarati/Gurmukhi/Odia/Arabic script)
when `detected_lang` isn't one of the maintained translations — picking between the same
small set of human-reviewed templates, never fabricating a new one. Latin-script languages
without a maintained translation (English, Hinglish, Spanish, romanized anything) fall
through to English, an acceptable degrade for a two-line fixed template with no LLM
available to ask.

**Not touched, confirmed already correct:** the primary LLM classification path
(`run_guardrail`) already detects language and topic relevance genuinely, for any language,
every request — no keyword list involved. This was never broken; only the degraded-outage
path was.

---

## Bias-correction model integration contract

ML model training (GFS-forecast-vs-ERA5-reanalysis bias correction) happens in a separate
repo, not here. This repo's job is a thin HTTP client — `app/services/model_client.py`,
**which does not exist yet** — invoked between the semantic gate and `build_wio` in
`app/main.py` so corrected values flow into fusion with a `provenance.transformations[]`
entry. `CanonicalEvidenceObject` already carries the unused `parent_ids`/`transformation`/
`transformation_timestamp`/`algorithm_version` fields for exactly this.

**`POST /v1/correct`** — batched (one call per `/query`, covering a whole forecast horizon
for one location, not one call per hour):

```json
// Request
{"instances": [{"gfs_t2m_k": 301.2, "gfs_apcp_mm": 0.4, "elevation_m": 0.0,
                "lead_hours": 18, "lat": 21.14, "lon": 79.08,
                "valid_from": "2026-09-03T06:00:00Z"}]}

// Response
{"model_version": "m3-<version>",
 "corrections": [{"temp_bias_c": -1.3, "precip_bias_mm": 0.2}]}
```

`corrections` must be the same length and order as `instances`. This repo applies:
`corrected_temp_c = (gfs_t2m_k − 273.15) + temp_bias_c`,
`corrected_precip_mm = max(0.0, gfs_apcp_mm + precip_bias_mm)`.

**`GET /health`** — plain 200, used by this repo's circuit breaker to decide whether to
keep sending traffic.

**Auth:** this repo sends `Authorization: Bearer <MODEL_API_KEY>` when `MODEL_API_KEY` is
configured — the model service should implement bearer-token auth to match.

**Failure behavior this repo assumes:** any non-200, timeout, malformed JSON, or missing
`corrections` array is treated as "correction unavailable" — the raw uncorrected forecast is
used instead, nothing breaks downstream. This repo will never surface a 5xx from that API
directly to an end user.

**Latency expectation:** an outer timeout (default 8s, configurable) plus circuit-breaking
after repeated failures. Well under 1s per batched call at a typical forecast-horizon batch
size (~72 hourly points for a 3-day forecast) is the target.

**Known gap:** `elevation_m` has no live lookup in this repo today — real requests will send
`elevation_m=0.0` unless this repo adds a lookup later, or the model API does its own
lat/lon→elevation lookup server-side.

Real validated baseline metrics (LightGBM vs. ridge vs. no-correction, on a real 24,960-row
2024 India dataset) live in the separate training repo now, not here — this repo carries no
unvalidated ML claims.

---

## Fixed, historical (condensed)

Full narrative for anything below lives in `CLAUDE.md`'s session records if you need the
live-verification detail; this is the compressed defect list, kept for institutional memory.

**Security (§2.1-§2.11), closed:** IDOR via unauthenticated `user_id`/`session_id`;
guardrail LLM output fields missing `max_length`; prose-grounding regex missing
mph/inches/°F; request-size cap trusting `Content-Length` (replaced by a raw ASGI
byte-stream check); no per-user cap on stored context facts; unauthenticated `/health`
8-way fan-out (now cached); CAP feed SSRF/XXE surface (host allowlist + `defusedxml`); raw
exception text leaking into `retrieval_status`.

**Fusion correctness:** CAP warnings broadcast nationwide (missing-polygon alerts read as
"covers everyone," corrupting every RADE recommendation under maxed-out risk aversion) —
fixed via area-name text matching, whole-word, requiring the user's own district when an
alert lists districts. `wio.weather.summary` empty for non-precipitation-only questions
(temperature/wind/marine queries reported "no compatible evidence" despite a full panel) —
fixed via a fallback summary built once at the source all consumers read. Ensemble members
bucketed with deterministic rows in the ranker, so one vendor corroborated itself and its
own spread read as a between-source disagreement — fixed by excluding members from the
comparability bucket. `single_source` and `full_agreement` scored identically in RADE
(corroboration bought nothing) — split to 0.65/0.8.

**Location resolution:** was not India-first by hard rule, only a soft population bonus —
"fishing near Kochi" resolved to Kōchi, Japan. Fixed: Indian candidates now outrank
non-Indian ones whenever any exist, regardless of population; a single-candidate provider
response is no longer unconditionally "dominant" without a plausibility check;
"near me"/"here"/"my location" are short-circuited before ever reaching a geocoder as a
literal place name.

**Latency:** guardrail LLM call was uncached, costing ~1.1s on every repeated query — now
memoized (TTL cache, keyed on text+history). Retrieval timeout/retry constants were
20s×3 attempts with no total budget (61.2s worst case on one slow source) — now 8s×1 retry
(16.4s worst case). Cold request time: 3815ms unchanged (I/O-bound floor); warm dropped
529-983ms from 1591ms.

**Multi-turn / session:** guardrail classified every query in isolation with zero
conversation memory — a contextless follow-up ("can I go play in the evening" after "will
it rain in Newtown") hard-rejected as off-topic. Fixed by wiring the previously-dead
`SqliteConversationLog` into the guardrail's user-message history (never the system prompt,
so the decision tree stays stable/cacheable independent of history), plus a continuation
rule in the prompt. Multi-location questions ("compare Delhi and Mumbai") silently answered
for one location with no indication the other was dropped — fixed via plural
`locations`/`time_phrases` fields and an additive `comparisons` response field, existing
callers' response shape unchanged.

**Dead code removed (not just flagged):** `app.storage.session_store` and its factory
(confirmed zero importers repo-wide); `app/services/location_resolver/seed.py` (an 8-city
gazetteer/7-PIN last-resort fallback, whose removal also surfaced 6 tests that were
accidentally depending on it to absorb live-network flakiness under full-suite load — fixed
by mocking location resolution properly in those tests); three zero-reader config settings
(`rank_weights_total`, `conversation_max_turns`, plus `session_ttl_seconds`/
`session_max_entries` once their only caller was gone); a stale `HF_TOKEN`/`HF_REPO_ID`
block in `.env`/`.env.example` left over from the training repo split.

**Docker:** no `.dockerignore` existed at all — a real image build hit "no space left on
device" copying this repo's own `.venv/`, and `.env` was being baked into the image layer
regardless of `.gitignore` (which never applies to a Docker build context). Fixed with a
`.dockerignore` excluding `.venv/`, `.git/`, `__pycache__/`, `.env`, `tests/`, `*.db`.
**If an image was built and pushed/shared before this fix**, rotate any keys that were in
`.env` at build time — they're baked into that image's layers regardless of this fix.

**GFS/GRIB2:** fully wired 2026-09-08. The adapter/decoder were already complete, purely
blocked on the Docker image missing `cfgrib`/`eccodes`/`xarray` and the system
`libeccodes0`/`libeccodes-data` packages. Now installs both; expanded from 2 decoded
variables (temperature, precipitation) to 6 (+ wind speed/direction, humidity, pressure).
Live-verified inside an actually-built image, not just import-checked.

---

## What already exists, so it doesn't get re-proposed

- Wind speed — live, every forecast source.
- Wave height, ocean current velocity/direction — live, `OPEN_METEO_MARINE`.
- The adapter registry pattern (`app/adapters/registry.py`) — a new source needs zero
  pipeline changes, proven by `OPEN_METEO_MARINE`, `MET_NORWAY`, `STORMGLASS`, `GEOAPIFY`.
