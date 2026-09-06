# The Services Layer, Explained

This document explains every service in `app/`: what it is for, how its mechanism
actually works, where it sits in the request flow, and what is wrong with it.

It assumes no weather-domain background. Read section 1 and 2 first; the rest is
reference, one file at a time.

Last verified against code 2026-09-05. Where a fault has been fixed since this document
was first written, it is marked **[FIXED]** rather than deleted, so the document still
explains *why* the current design looks the way it does — most of the temporal-ranking
term, the per-timestamp disagreement buckets, and the corroboration rule exist directly
because of faults recorded here. Faults still open are marked as before; where one is
already tracked with a finding ID, this document points at `BUG.md`/`AUDIT.md` instead of
re-describing it, so there is one home per fact — `FIXES.md` is the current open/closed
status for every finding ID cited below.

---

## 1. What the system is trying to do

A user asks a question in plain language — *"will it rain in Indore tomorrow"*.

A naive weather bot would hand that sentence to a language model and print whatever
comes back. This system is built on the opposite principle, and it is the rule that
explains almost every design decision in the codebase:

> **Numbers come from deterministic pipelines. A language model may only explain
> data that has already been fetched, validated and fused. It never invents a value
> and never chooses a source.**

So the work happens in stages. Each stage takes evidence, narrows or reshapes it,
and hands it on. Nothing is allowed to enter later stages without a traceable origin.

Two data structures carry everything:

**CEO — Canonical Evidence Object** (`app/schemas/ceo.py`).
One measurement, from one source, for one variable, at one time. "Open-Meteo says
0.4mm of rain will fall in Indore between 14:00 and 15:00 UTC tomorrow" is one CEO.
A single query produces hundreds of them.

The important part is that a CEO records not just the number but *what kind of number
it is*: the variable, the statistic (an instantaneous reading? a total accumulated over
a period? a probability?), the unit, and — critically — the **accumulation window**.
1mm of rain per hour and 1mm of rain per day are not the same fact, and the schema
refuses to let them be confused. Every CEO also carries a `provenance.transformations`
list recording every step applied to it.

**WIO — Weather Intelligence Object** (`app/schemas/wio.py`).
The single fused answer object. Where the CEO list is hundreds of raw facts, the WIO
is the digested view: one rain panel, one temperature panel, one wind panel, an
agreement verdict, and the evidence list backing it.

The rule that matters here: **two sources reporting the same variable are never
averaged.** Averaging destroys the information that they disagreed. Instead the
highest-ranked value is presented, every source is kept in the evidence list, and any
conflict is *flagged* in `agreement`/`disagreements`. Official warnings are kept
structurally separate from numeric fusion — a government cyclone warning is never
blended into an arithmetic mean with a model forecast.

### The request flow

```
POST /query
  |
  1. guardrail            check_question_fast (deterministic reject) then
                          query_guardrail.run_guardrail (one LLM call -> a strict
                          GuardrailAction; deterministic fallback if the LLM is down)
  2. location_resolver    "Indore" -> 22.7196, 75.8577
  3. time_parser          "tomorrow" -> 2026-09-03 00:00 .. 23:59
  4. retrieval_planner    which variables, which sources  (deterministic, no LLM)
  5. retrieval            fetch all sources concurrently, cache, isolate failures
       -> adapters/*      HTTP call per source
       -> decoders/*      raw JSON -> CEOs
  6. temporal_align       drop CEOs outside the time window
  7. semantic_gate        drop CEOs whose semantics are invalid
  8. (ML bias correction) seam, not yet written
  9. wio_builder          rank + fuse + detect disagreement -> WIO
       -> ranker
       -> spatial_match
 10. agents/orchestrator  8 agents derive claims; reviewer gates on evidence
 11. rade/v2              risk-aware decision, only for decision questions
 12. _synthesize          template answer string
```

Stages 2-4 decide *what to ask for*. Stage 5 gets it. Stages 6-7 throw away what
does not belong. Stage 9 is where many facts become one answer. Stages 10-11 reason
over that answer. Nothing after stage 5 ever touches the network.

---

## 2. A real trace, and the bug it led to fixing (history — closed 2026-09-02/03)

This section originally reported a live defect. It is kept as history because it is the
best explanation of *why* several mechanisms described later in this document exist —
the fix motivated by this trace is the largest single correctness change this codebase
has had. The defect itself is closed; nothing below describes current behavior.

The original trace, against the live Open-Meteo API for *"will it rain in Indore
tomorrow"*:

```
window: 2026-09-03 00:00 IST -> 23:59 IST     horizon: short
raw CEOs fetched:            288
after time-window filter:     96
distinct ranking scores:       1   over 96 objects
```

The system answered **"Rain unlikely (0%)"** for a day whose own fetched evidence held
2.4mm of rain and a 75% peak probability. Three compounding defects, each fixed:

**(a) No aggregation.** `wio_builder` picked exactly one CEO per variable — the first
decoded, midnight, when it happened to be dry — and reported it as the whole day.
**Fixed:** `_rain_panel` now sums precipitation across the query window grouped by
`(source, accumulation_window_hours)`, takes probability as the window's peak, and
reports temperature as a min-max range.

**(b) Ranking could not tell the hours apart.** All 96 same-source, same-location,
same-issue-time objects scored identically, so "highest-ranked" degraded to insertion
order. **Fixed:** `ranker.py` added a temporal term (`rank_weight_temporal`,
`_temporal_score`) scoring proximity to the query window's centre, so hourly records
from one source are no longer indistinguishable.

**(c) `full_agreement` was asserted from one vendor.** The check was `len(scored) >= 2`
— two *evidence objects*, which the same API call's temperature and rainfall both
satisfy with no second source involved. RADE reads this field and raised confidence
0.55 -> 0.8 on corroboration that never happened. **Fixed:** `corroborated()` now
requires two *distinct sources* reporting the same variable at the same timestamp
(`group_comparable` buckets by `(variable, window, valid_from)`, excluding ensemble
members since 2026-09-05 for the same reason — see `AUDIT.md` A1).

Full account: `CLAUDE.md`'s "Fusion — the largest correctness fix" session record.

---

## 3. File by file

Each entry: **what it is for** / **how the mechanism works** / **how it connects** /
**what is wrong**. Faults are marked
**[WRONG]** produces incorrect output ·
**[SLOW]** wastes time or memory ·
**[DEAD]** unreachable or unread ·
**[RISK]** a production hazard ·
**[TIDY]** style only.

---

### 3.1 `config.py` — runtime configuration

**Purpose.** One place where every environment-tunable value is defined, and the only
place secrets are read from (the environment, never a file in the repo).

**Mechanism.** A frozen dataclass whose field defaults call `os.getenv` at import time.
`settings = Settings()` is a module-level singleton imported everywhere.

**Connects.** Read by `retrieval`, `main`, and the location resolver.

**Faults.**
- **[FIXED]** `source_retries` is read by `retrieval._fetch_with_retry`; retry with backoff
  exists, and only genuinely transient failures are retried. Default 1 since 2026-09-05.
- **[FIXED]** `database_path` is honoured by `context/store.py`.
- **[DEAD]** `rank_weights_total`, `conversation_max_turns`,
  `query_understanding_confidence_threshold` — zero readers (`AUDIT.md` A7).
- **[RISK]** Defaults are evaluated at import, so a config change needs a restart.
  Acceptable, but worth knowing.
- The values that *should* be here are scattered through the services instead:
  ranking weights, the 50km distance decay, the 72h staleness ceiling, the 10mm
  disagreement threshold, cache sizes, adapter timeouts. That is the "no hardcoded
  values" problem, and it is mostly a problem of *location* — the numbers are fine,
  they are just defined in eleven different files.

---

### 3.2 `constants.py`

**Purpose.** Shared constants. Currently one: `IST`, the India Standard Time zone.

**Fault.** **[FIXED]** `IST` was a global constant applied to every request regardless
of location, the root of the timezone problem described in the original 3.4. Windows now
resolve in the location's own timezone (`zoneinfo`, supplied by the geocoding provider);
`IST` remains only as the final fallback when a timezone cannot be resolved at all.

---

### 3.3 `location_resolver/` — turning words into coordinates

A package of six modules. Everything downstream needs a latitude and longitude; this
is what produces them, and it is the most carefully built part of the codebase.

**Mechanism — the resolution order:**

```
coordinates -> cache -> PIN code -> normalized name -> providers -> rank -> ambiguity
```

- `detect.py` — pure regex. Is this input already coordinates? Is it a six-digit
  Indian PIN code? No network, no I/O.
- `normalize.py` — deterministic cleanup. Collapses whitespace, strips punctuation,
  applies a small alias table (Bombay->Mumbai, Calcutta->Kolkata). `extract_place_phrase`
  pulls "Indore" out of "will it rain in Indore tomorrow" by matching lead-in patterns
  ("weather in", "rain in") and stripping trailing time words.
- `providers/` — three keyless services, tried in order. Open-Meteo Geocoding first
  (structured, gives population and admin hierarchy); Nominatim/OSM second (covers
  Indian districts, small towns and historical names Open-Meteo lacks); India Post for
  PIN codes.
- `ranking.py` — the interesting part. Multiple places share a name. Scoring is
  `log10(population) + 2.0 if India + 1.0 if capital + 0.5 exact-name-match`.

**Why scoring and not filtering:** the obvious approach is to filter results to
India. That was tried and rejected, because it resolves "Springfield" to an obscure
Tamil Nadu hamlet. A *bias* keeps Indian places winning whenever they are plausible
without breaking global queries.

**The ambiguity rule:** a winner is only accepted if it beats the runner-up by
`geocoding_dominance_margin` (default 1.0, i.e. roughly an order of magnitude more
population). Otherwise the request fails with **409 and a candidate list** rather than
silently picking one. Refusing to guess is correct behaviour and it should stay.

- `seed.py` — an offline last resort: 8 cities, 7 PIN codes, exact match only. If
  every provider is unreachable and the place is not one of those, it still raises
  rather than fabricate coordinates. Correct as designed.
- `cache.py` — a separate TTL cache from the weather one, because location facts are
  stable for weeks while forecasts expire in minutes.

**Faults.**
- **[RISK]** Nominatim's usage policy is a hard maximum of 1 request/second and they
  block violators by IP. `NominatimGeocoder` throttles in-process
  (`nominatim_min_interval_seconds`), but the throttle is class-level state — with
  multiple uvicorn workers the aggregate can still exceed the policy (`BUG.md` B12).
- **[SLOW]** PIN resolution still costs two network round trips: India Post gives a
  district and state but no coordinates, so the district name is then geocoded.
- **[FIXED]** Every provider call opened a fresh HTTPS connection. All four providers
  (`geoapify.py`, `nominatim.py`, `open_meteo.py`, `india_post.py`) now share one pooled
  `httpx` client via `app/adapters/http.py:get_client()`.
- **[FIXED]** The cache only evicted an expired entry when that exact key was looked up
  again, so keys never re-queried were retained forever. `LocationCache` now extends the
  bounded `TTLCache`, which sweeps expired entries on every write and evicts LRU past
  `location_cache_max_entries`.
- **[FIXED]** No logging was configured anywhere in the app, so `logger.info(...,
  extra={...})` calls were invisible. `app/logging_config.py` now configures it, with
  request-ID propagation via `ContextVar`.

---

### 3.4 `time_parser.py` — turning "tomorrow" into a time window

**Purpose.** Convert a phrase into a concrete `(valid_from, valid_to)` pair, plus a
`horizon` label (nowcast/short/medium/climate) and a confidence.

**Mechanism.** Regex and a long if/elif chain, in two passes. First establish a base
date ("tomorrow" -> now+1d, "day after tomorrow" -> now+2d, an explicit `2026-09-03`,
"next Monday", "this weekend"). Then apply a time-of-day window on top ("morning" ->
06:00-11:59, "evening" -> 18:00-21:00), defaulting to the whole day. Finally derive
the horizon from how far ahead the window starts.

The horizon matters because it feeds the retrieval planner: a `climate` horizon adds
the historical reanalysis sources; a `nowcast` does not, and (since 2026-09-05) is also
what "right now" and "in the next N hours" resolve to — see the fix below.

**Connects.** `main._weather_request` calls it third; output goes to
`build_retrieval_plan`, to `retrieval` (as the fetch range), and to
`temporal_align.filter_by_window`.

**Faults.**
- **[FIXED]** Every request was parsed in IST regardless of where the user asked about.
  Windows now resolve in `tz` — the location's own timezone, passed in from
  `req.timezone or location.timezone` — via `zoneinfo`.
- **[FIXED]** The month-name loop tested `if mon in text_l` for a bare prefix, so
  `"may"` matched inside **"maybe"**. `MONTH_PATTERN` now spells out each month
  explicitly with its own optional suffix (`may` has none to extend into; `mar(?:ch)?`
  etc.), so the match is exact.
- **[FIXED]** The day-of-month regex took the first digits anywhere in the sentence, so
  "2 pm on Aug 5" parsed as August 2nd. `_DAY_MONTH`/`_MONTH_DAY` now require the day to
  sit immediately next to the month name (`\b(\d{1,2})(?:st|nd|rd|th)?\s+(MONTH)\b`).
- **[FIXED]** Two bare `except:` clauses — zero remain in this file.
- **[FIXED]** A dead `elif "next 3 days": pass` duplicated a branch the later chain
  already handled — the duplicate is gone; `"next 3 days"`/`"next three days"` is
  matched once.
- **[FIXED]** `QueryRequestV1.timezone` had zero readers — `main._weather_request` now
  passes it as `tz=req.timezone or location.timezone` to every `parse_time_window` call.
- **[ADDED 2026-09-05]** "right now"/"currently"/"abhi" and "next N hours" previously
  fell through to the whole-day default like every other phrasing — `"is it raining
  right now"` was answered with a 24-hour sum including hours already past. Both now
  return a `nowcast` window floored to the top of the current hour
  (`_NOW_PHRASE`/`_NEXT_HOURS`), so the record covering the asked-about moment is inside
  the window instead of averaged away by hours before and after it.
- **[TIDY]** Confidence values (0.9, 0.8, 0.7, 0.6) are magic numbers inline.

---

### 3.5 `retrieval_planner.py` — deciding what to fetch

**Purpose.** Decide, **without any LLM**, which variables and which sources a question
needs. This is a deliberate architectural boundary: letting a language model choose
data sources is how a system starts inventing its own evidence.

**Mechanism.** Keyword matching over the lowercased question. Detect a decision context
first (spray / irrigate / harvest / marine / travel), because that drives everything
else — any decision question needs rain, wind, warnings and ensemble uncertainty
whether or not the user mentioned them. Then map keywords to variables, then to
sources, accumulating a `reasons` list so the plan can be audited.

**Connects.** Called by `main` after the time parser. Its `sources` list is what
`retrieval` fans out over; its `decision_context` decides whether RADE runs at all.

**Faults.**
- **[FIXED]** `plan.variables`/`plan.evidence_classes` had zero readers — every adapter
  returned everything it had regardless of what was planned. `retrieval._wanted()` now
  filters retrieved items to `plan.variables` (warnings pass through unconditionally,
  governed by `need_warnings` instead).
- **[FIXED]** Keyword matching was plain substring, so `"go"` matched **Goa**, **mango**,
  **going**; `"sea"` matched **season**. `has_word()` now matches on word boundaries
  (`(?<!\w)word(?!\w)`) everywhere in this file.
- **[FIXED, 2026-09-05]** `CAP` used to be fetched only when a warning keyword or a
  decision context was present, so `"weather in Kolkata"` returned no mention of a live
  official warning for that exact district while `"warnings in Kolkata"` did — live
  Kolkata rain testing hit this directly. `CAP` is now in every plan's base source list
  alongside `OPEN_METEO`/`MET_NORWAY`; `need_warnings` now only controls whether the
  warning-*specific* `IMD` source is added on top.
- **[TIDY]** Hindi keywords are present but there is no language routing yet.

---

### 3.6 `retrieval.py` — fetching, concurrently and safely

**Purpose.** Fetch every planned source at once, cache results, and make sure one dead
source can never fail the request.

**Mechanism.** `_one()` per source: build source-specific arguments, filter by
`plan.variables` (`_wanted`), hash them into a cache key, return the cached CEOs on a
hit, otherwise call the adapter with retry+backoff under `CircuitBreaker`/
`asyncio.wait_for`, normalize raw JSON to CEOs via the decoder, cache, return.
`retrieve()` runs all of these under `asyncio.gather`.

**The isolation contract is the good part.** `_one` catches every exception per source
and returns it as data, not as a raised error. A failed source becomes an entry in
`retrieval_status` with `partial: True`, and the request continues with what survived.
This is exactly right and should be preserved.

**Faults.**
- **[FIXED]** `_one()` took a `plan` argument and never read it — it now filters
  returned items to `plan.variables` via `_wanted()`.
- **[FIXED]** No connection pooling — every adapter opened its own `httpx.AsyncClient`
  per call. One pooled client (`app/adapters/http.py`) is now shared by every adapter
  and every location provider.
- **[FIXED]** No retry — one transient 502 dropped that source entirely.
  `_fetch_with_retry` now retries with backoff, but only for genuinely transient
  failures (`_is_retryable`: timeouts, 5xx, 429 — not a 404 or a missing credential).
- **[FIXED]** No circuit breaker — a permanently unconfigured source paid a full
  timeout on every single request. `CircuitBreaker` now opens after
  `circuit_breaker_threshold` consecutive failures and half-opens after
  `circuit_breaker_reset_seconds`.
- **[SLOW]** One `source_timeout_seconds` (8s since 2026-09-05) serves both the retrieval
  wrapper and the shared httpx client, so a per-adapter value above it is unreachable.
  Worst case per source is now 2 attempts x 8s + backoff = 16.4s, down from 61.2s; the
  `gather` itself still has no total deadline (`AUDIT.md` A5).
- **[FIXED]** The cache key rounds coordinates to `cache_key_precision` (2dp, ~1km)
  against a ~27km source grid, so a whole city shares one entry.
- **[FIXED]** On a cache hit, every CEO was `model_copy(deep=True)`-ed purely to stamp a
  cache-metadata field — roughly 288 deep Pydantic clones on the original Indore trace.
  `retrieval_timestamp` is now stamped once on the source item at fetch time
  (`item.retrieval_timestamp = item.retrieval_timestamp or retrieved`), not re-cloned
  per cache hit.
- **[FIXED]** One TTL served everything, so historical reanalysis data (which cannot
  change) expired after 15 minutes like a live forecast. `_CACHE_TTL_BY_SOURCE` now
  gives `ERA5`/`NASA_POWER` `historical_cache_ttl_seconds` (default 86400s) and
  `CAP`/`IMD` `warning_cache_ttl_seconds` (default 300s).
- **[FIXED]** `__import__("datetime").datetime.now(...)` inline instead of an import —
  this file now imports `datetime`/`timezone` normally at the top.

---

### 3.7 `adapters/` and `decoders/` — one per source

**Purpose.** An adapter knows how to *talk* to one source (`fetch`), how to convert its
reply into CEOs (`normalize`, usually delegating to a decoder), and how to report its
own health. The uniform interface is what lets `retrieval` treat all sources alike.

**Registry.** `adapters/registry.py` maps source name to instance — 10 registered:
OPEN_METEO, ERA5, GEFS, CAP, NASA_POWER, IMD, GFS, MET_NORWAY, OPEN_METEO_MARINE,
STORMGLASS.

**The state of the sources today** matters more than the code:

| Source | Authority | State |
|---|---|---|
| CAP | 1.00 | Live and keyless on NDMA's Sachet feed. Retrieved on **every** weather query since 2026-09-05 — gating it on warning keywords hid live alerts. |
| IMD | 0.95 | **Unconfigured** — no API key. India's own met authority is absent. |
| MET_NORWAY | 0.75 | Live. Added specifically as an independent vendor — see below. |
| GEFS | 0.70 | Live. **Not an independent vendor** — it is `ensemble-api.open-meteo.com`, the same provider as `OPEN_METEO`. Its members are excluded from corroboration and disagreement (`ranker.group_comparable`, `AUDIT.md` A1). |
| OPEN_METEO | 0.70 | Live |
| GFS | 0.70 | Off — needs the GRIB2 libraries |
| OPEN_METEO_MARINE | — (marine panel) | Live, keyless. |
| NASA_POWER | 0.65 | Live |
| ERA5 | 0.50 | Live (Open-Meteo historical) |
| STORMGLASS | — (marine fallback) | Keyed, built but never live-verified (needs a paid key). |

**[FIXED]** This section originally read "four of the five live sources are Open-Meteo
endpoints... 'sources agree' is being computed over what is effectively one vendor."
`MET_NORWAY` was added specifically to close that gap — it is the only forecast source
independent of Open-Meteo — and `CAP`/`IMD` are official-authority sources, not model
forecasts, so corroboration is no longer computed over one vendor talking to itself.
GEFS remains an Open-Meteo endpoint and is excluded from corroboration accordingly.

**Faults.**
- **[FIXED]** `decoders/imd_json.py` stamped any record missing coordinates with
  Nagpur's (`coordinates=[record.get("lon", 79.08), record.get("lat", 21.14)]`), so it
  passed the semantic gate, scored full IMD authority, and was ranked by distance using
  fictional coordinates. Records without coordinates are now skipped and logged instead.
- **[FIXED]** `nasa_power.fetch` defaulted `start`/`end` to `"20240101"`/`"20240102"`.
  Both are now required parameters with no default.
- **[TIDY]** Health probes still use a fixed probe location and date
  (`HEALTH_PROBE_LAT`/`_LON`/`_DATE` in `constants.py`), so `/health` reports a source
  "available" based on one probe point rather than the caller's location — now at least
  centralised in one named place instead of scattered per-adapter literals.
- **[FIXED]** Bare `except:` in `open_meteo_historical` and `nasa_power` — zero bare
  excepts remain anywhere in `adapters/` or `decoders/`.
- **[TIDY]** `print()` instead of logging in `grib2_placeholder.py` — low priority; GFS
  is unavailable in the current deployment regardless (needs `requirements-full.txt`).

---

### 3.8 `temporal_align.py` — dropping evidence outside the window

**Purpose.** Keep only CEOs whose validity period overlaps the question's window, and
compute how stale a piece of evidence is for ranking.

**Mechanism.** `overlaps()` is an interval-intersection test with a fallback: if a CEO
has no validity period, fall back to when it was issued or observed. `to_utc()`
normalizes everything to UTC and treats a naive datetime as UTC.

**Connects.** Called by `main` immediately after retrieval — this is what cut 288
objects to 96 in the trace. `staleness_hours` is called by `ranker`.

**Faults.** Verified still current 2026-09-05.
- **[RISK]** A CEO with *no* time information at all (`overlaps()`: no `valid_from`/
  `valid_to`, no `issued_at`/`observed_at` either) returns `True` — kept unconditionally.
  Combined with the geometry default in 3.9 (now fixed, see below), evidence with
  neither a known place nor a known time no longer scores optimistically — the time
  gap remains open on its own.
- **[TIDY]** `staleness_hours` returns the magic number `999` for unknown.
- **[TIDY]** `to_utc(None)` returns `None` while annotated as returning `datetime`.

---

### 3.9 `spatial_match.py` — how far is this evidence from the user

**Purpose.** Great-circle distance between a CEO's location and the query point, for
the proximity term in ranking.

**Mechanism.** Standard haversine formula. `distance_to_query` extracts the point from
the CEO geometry.

**Faults.**
- **[FIXED]** Missing geometry used to return `0.0` — exactly-at-the-query-point,
  the maximum proximity score — conflating "unknown location" with "confirmed local".
  `distance_to_query` now returns a dedicated `UNKNOWN_DISTANCE_KM` (`inf`) for missing
  or unparseable geometry, and `ranker.score_evidence` applies
  `rank_unknown_location_penalty` (default 0.25) instead of the maximum score. `0.0` is
  now reached only for a genuine polygon-containment match — the query point actually
  inside a warning polygon — which is correctly local.
- **[FIXED]** `except Exception: return 9999` swallowed the reason. Now catches the
  three specific expected failure types (`TypeError, ValueError, IndexError`) and
  returns the same `UNKNOWN_DISTANCE_KM` sentinel as the missing-geometry case, so
  "distance unknown" has one representation, not two magic numbers.

---

### 3.10 `variable_registry.py` and `semantic_gate.py` — the comparability rules

**Purpose.** This is the "never average incompatible things" rule, made executable.
The registry maps native field names from every source (`apcp`, `tp`, `t2m`, `2t`,
`prate`, ...) onto canonical variables, and records which statistics, units,
accumulation windows and evidence classes are legitimate for each.

The gate then checks every CEO against those rules and rejects violations —
a precipitation accumulation with no window, a probability in the wrong unit,
a warning variable arriving as a forecast.

**Why it exists.** Without it, a decoder bug or a future LLM-written adapter could
introduce a CEO claiming "precipitation_amount, 50, instant" and everything downstream
would treat it as comparable to a real 1-hour accumulation. The gate is a structural
defence, not a validation nicety.

**Connects.** `main` calls `validated_evidence` right after the time filter, stage 7.

**Faults.**
- **[FIXED]** A `REGISTRY = load_registry()` loaded an optional `variable_registry.yaml`
  override that nothing read (`validate_semantics` read `DEFAULT_REGISTRY` directly) and
  the YAML file never existed. Both the dead loader and the `pyyaml` dependency it
  existed for have been removed.
- **[FIXED]** `validate_semantics` rescanned all ~45 registry entries per CEO.
  `_BY_CANONICAL` now precomputes a dict keyed by canonical variable name, so lookup is
  one dictionary access, not a rescan.
- **[TIDY]** Rejection reasons are still flattened by `main` into one vague sentence,
  "Some incompatible evidence was rejected." Nothing logs which, or why.

---

### 3.11 `ranker.py` — which evidence wins

**Purpose.** Order evidence by trustworthiness, and flag disagreement between sources.

**Mechanism.**
`score = 0.35*authority + 0.20*freshness + 0.15*spatial + 0.10*quality + 0.20*temporal`
(weights configurable, `rank_weight_*` in `config.py`)

- *authority* — a fixed table: CAP 1.0 (official warnings), IMD 0.95 (national met
  authority), down to ERA5 0.5. This encodes "an official warning outranks a model
  forecast", which is the correct instinct.
- *freshness* — staleness capped at `rank_staleness_ceiling_hours` (72h), linearly
  inverted.
- *spatial* — `1/(1 + km/rank_spatial_half_decay_km)` (50km default half-decay);
  unknown location gets `rank_unknown_location_penalty` instead of the max score (3.9).
- *quality* — 1.0 unless the CEO carries a bad quality flag.
- *temporal* (added) — proximity of the evidence's valid time to the centre of the
  query window, so hourly records from one source are no longer indistinguishable.
- Warnings get a `rank_warning_authority_bonus` (+0.1) boost.

`detect_disagreements` buckets by `(variable, accumulation_window_hours, valid_from)`
and flags a spread over `disagreement_threshold_mm`/`_c` **within one bucket** — i.e.
between distinct sources at the same timestamp and window, never across the window or
across window lengths.

**Connects.** `wio_builder` calls `rank` before fusing; the ordering decides the
contents of every WIO panel.

**Faults.**
- **[FIXED]** The score could not distinguish two CEOs from the same source — same
  authority, location, issue time, quality, all identical, as the original Indore trace
  proved (96 objects, 1 distinct score). Added the temporal term above.
- **[FIXED]** `detect_disagreements` compared precipitation across the whole time
  window rather than between matching timestamps, so a dry morning and a wet evening
  from one source read as sources disagreeing. `group_comparable` now buckets by exact
  `valid_from`, so only genuinely simultaneous records are compared.
- **[FIXED]** It also compared values with different accumulation windows directly, so
  an IMD 24h total against an Open-Meteo 1h total would differ by construction, not
  disagreement. The accumulation window is now part of the bucket key.
- **[PARTIALLY FIXED]** Originally only precipitation was checked. `_THRESHOLDS` now
  also covers `temperature_2m` (`disagreement_threshold_c`); wind still is not.
- **[FIXED]** The weights, authority table, 50km, 72h and 10mm/3C were inline literals
  — all now in `config.py` as named, env-overridable settings.
- **[FIXED]** `def rank(..., now: datetime = None)` — already `datetime | None`.

---

### 3.12 `wio_builder.py` — many facts become one answer

**Purpose.** The fusion stage. Turn a ranked CEO list into the single WIO the rest of
the system reads.

**Mechanism.** Rank everything (`ranker.rank`). Build the rain panel by grouping the
best source's precipitation records by `accumulation_window_hours` and summing the
finest window available over the query range; probability is that source's window peak
(falling back to the next-best source only if the amount's source reported none).
Temperature and wind panels take min-max range and window-maximum respectively.
Collect warning CEOs separately, resolved by polygon-containment first and area-name
matching second, surfacing the highest severity. Run disagreement detection.
Summarize every surviving CEO into the evidence list — ensemble members collapse to one
summary row, individually retrievable by ID. Compose a summary sentence, falling back
to a temperature/wind/marine-based one when no rain evidence was fetched at all.

**Connects.** Called by `main` at stage 9. Its output is read by every agent, by RADE,
and by `_synthesize`. **Everything after this point sees only the WIO** — so a mistake
here is invisible to all downstream validation, including the reviewer agent.

**Faults.** This file held the most damage in the codebase; most of it is fixed.
- **[FIXED]** No window aggregation — one hour reported as the day (the original §2
  bug). `_rain_panel` now sums over the window, grouped by `(source,
  accumulation_window_hours)` so a 1h and 6h record are never added together.
- **[FIXED]** `full_agreement` was asserted from `len(scored) >= 2`, where both objects
  could come from the same source (or, since ensembles were added, one vendor's own
  members). `corroborated()` now requires two *distinct* sources at the same timestamp;
  ensemble members are excluded from the comparison entirely (`AUDIT.md` A1).
- **[IMPROVED, not fully closed]** Rain amount and probability used to be selected by
  two fully independent loops with no shared source or time. `_rain_panel` now prefers
  the probability from the *same* source as the amount (falling back to a different
  source only when that source reported no probability at all), and both are drawn
  from the same query window — but the probability is the window's *peak*, not tied to
  the specific hour the summed amount concentrates in. A "60% chance, 5mm total" can
  still describe different hours within the same window.
- **[FIXED]** `member_values` pooled ensemble members across all timestamps, smearing
  RADE's probability distribution over time. It now selects the single timestamp with
  the wettest member spread (`wettest = max(members.values(), key=sum)`) — one moment's
  distribution, not hours pooled together.
- **[NOW TRACKED AS A8]** The rain panel's probability citation is meant to be the peak
  probability record but cites `probabilities[:1]` (the first, not the peak) —
  `GET /evidence/{id}` on that citation can return a different probability than the
  one stated. See `AUDIT.md` A8; not yet fixed.
- **[IMPROVED]** `wio.evidence` still includes every surviving non-ensemble CEO, but
  ensemble members now collapse to one summary row instead of each being serialized —
  a decision response that carried ~840 evidence entries now carries ~121.
- **[TIDY]** Summary thresholds (`rain_likely_probability`, `rain_possible_probability`)
  are now named config settings, not inline literals.

---

### 3.13 `cache.py` and `evidence_store.py` — process memory

**Purpose.** `TTLCache` avoids refetching a source within its TTL and always keeps
freshness metadata alongside the value, so a cached answer can still be described
honestly. `EvidenceStore` indexes CEOs by ID so `GET /evidence/{id}` can show the user
exactly what backed an answer — the audit trail that makes the citations real.

**Faults.**
- **[FIXED]** Neither had any bound or eviction — `EvidenceStore` grew roughly one entry
  per CEO per request forever, and `TTLCache` counted a stale entry as a miss but never
  deleted it. Both are now LRU-bounded (`OrderedDict`, `max_entries`) with expired
  entries swept on every write, not left to accumulate. `weather_cache`, `location_cache`
  and `evidence_store` all follow this pattern now.
- **[RISK, unchanged]** Both are still per-process. With multiple uvicorn workers,
  `GET /evidence/{id}` returns 404 whenever the follow-up request lands on a different
  worker. Run one worker until these move to a shared backend (`app/storage/base.py`
  exists to make that a config change) — recorded as an accepted trade, not scheduled.

---

### 3.14 `agents/orchestrator.py` — the claim layer

**Purpose.** Eight agents each derive a structured claim from the already-built WIO and
evidence, and a **reviewer** gates the result.

Seven of the eight are plain Python functions, not LLM calls. The eighth —
`run_explanation_agent` — is the one seam where a language model runs, and it writes prose
only: it never selects a source and never originates a number. `model_for(role)` populates
a `model` field on every result, which for the seven deterministic agents is decorative.

**The reviewer is the important one.** For every claim from every agent it checks two
things:

1. **Citation** — each cited `evidence_id` exists in the retrieved evidence.
2. **Value** — the number attached to that citation is **recomputed** from the cited
   evidence and must match. Each claim declares how it was derived in
   `Claim.extra["derivation"]` (`sum` / `max` / `min` / `identity` / `none`, with the
   variable, unit and accumulation window it operated on), and
   `agents/verification.py:verify_claim` re-runs that operation over the CEOs the claim
   actually cites. Citing real evidence is necessary but no longer sufficient: a claim can
   cite a genuine record and still be rejected because the value hanging off it is not what
   that record says.

Free text is checked differently. `check_prose_grounding` extracts every quantity carrying
a physical unit (mm, %, C, km/h) from the explanation and requires each to match something
the deterministic pipeline actually produced — a panel value, an evidence value, the peak
probability — allowing rounding to 0, 1 or 2 places. Bare numerals ("the next 24 hours")
are not checked: they are prose, not weather claims, and treating them as claims produces
nothing but false rejections.

**Failure behaviour is deliberately split.** A value that contradicts its evidence, a unit
mismatch, or an unknown evidence ID is an **error** — `main` turns the request into a 503.
A claim shape no verifier recognises is a **warning** — recorded, but not a reason to
reject a response that may be perfectly correct. A future claim type must not start 503ing
the API just because nobody has written a verifier for it yet.

**The LLM seam — rewritten since this section was first drafted.** There is no
Groq-specific `groq_client.py` any more. `app/llm/client.py` is a provider-agnostic,
two-tier gateway (`small_llm`/`big_llm`): each tier is an ordered chain of
OpenAI-compatible endpoints (`{TIER}_LLM_MODEL`/`_BASE_URL`/`_KEY` plus numbered
fallbacks), tried in order on failure, and no provider name appears in code — a
provider is only ever a base URL and a model string from the environment. Inert unless
`LLM_ENABLED=true` and a tier's chain is non-empty.

`run_explanation_agent` sends a **fact sheet** built from the WIO panels (never the raw
question text since 2026-09-05 — see `_fact_sheet`'s `Intent:` line, `AUDIT.md` A6) to
the small tier by default. It escalates to the big tier only via
`_requires_big_llm`'s deterministic trigger — fused sources disagree, or a RADE decision
ran and landed below `big_llm_complexity_confidence_threshold` — never by asking the
small model whether it feels out of its depth. The returned prose goes through the
grounding check above. Ordering matters: the explanation is produced *before* the
reviewer runs so the reviewer can check it, but is appended last in the returned agent
list.

Any LLM failure — timeout, dead key, empty response — degrades to the deterministic
template answer with `status="partial"`. A third-party model being down must never 503 this
API. An ungrounded number likewise **suppresses** the explanation rather than failing the
request (`WEATHERGPT_REVIEWER_PROSE_FAILURE_MODE=fail` makes it fatal instead): the
guarantee is that no fabricated number reaches the user, not that Groq's worst output can
take the service down.

**Faults.**
- **[RISK, unchanged — an acknowledged limit, not an oversight]** The `identity` and
  `sum`/`max`/`min` verifiers confirm a claim is arithmetically faithful to the evidence
  it cites. They cannot confirm it cites the *right* evidence — an agent that cites a
  real but irrelevant CEO and reports its value honestly still passes. `CLAUDE.md`
  records this as "relevance is guaranteed by construction, not by the reviewer."
- **[BY DESIGN, not a fault]** `historical` and `observation` agents still slice `[:2]`
  off class-filtered evidence rather than reading ranked output. Each claim cites its
  own CEO, so citations stay self-consistent; documented in `BUG.md`'s "Not bugs" as an
  accepted, arbitrary-but-safe selection.
- **[TIDY]** `AgentResult.status` is a bare string; a typo silently becomes a 503.

---

### 3.15 `rade/v2.py` — the risk-aware decision engine

**Purpose.** For questions like "should I spray today", produce a recommendation that
accounts for *asymmetric consequences*. Spraying before rain wastes the chemical and
the day; delaying costs little. A plain "60% chance of rain" does not express that;
this does.

**Mechanism.** Build scenarios from the WIO rain panel — 5 bins if ensemble members are
present, otherwise a simple dry/wet pair from the probability. Score each candidate
action as `expected_utility - risk_lambda * downside_risk` against a per-domain utility
table, and pick the argmax. `risk_lambda` comes from the user's risk tolerance, and is
forced to maximum aversion when an orange or red official warning is active.

**The good part:** with insufficient evidence it returns `defer_decision` rather than
guessing, and never fabricates a probability. That instinct is correct and rare.

**Faults.**
- **[FIXED]** `_domain()` used to fall through to `"travel"` on an unmatched decision
  question, silently applying travel-policy utilities instead of deferring. It now
  returns `None` on no match, and `decide()` returns `defer_decision` when `domain is
  None` — verified by direct read of both functions 2026-09-05.
- **[FIXED]** `_domain` matched keywords against the user's whole context dictionary
  stringified, so a stored fact containing "crop" flipped the domain of an unrelated
  question. It now takes only `context: str` (the decision-context text) — the
  docstring records this was deliberate.
- **[FIXED]** The warning-driven risk escalation was dead in practice while CAP was
  unconfigured. **Live-verified working 2026-09-05**: a real Kolkata thunderstorm
  warning produced `risk_lambda = max(risk_lambda, 1.0)` and a `delay` recommendation
  with the rationale "Risk aversion increased because an official high-severity warning
  is active."
- **[BY DESIGN, not a fault]** Utility tables and scenario bin edges are inline
  literals. `config.py`'s own stated principle: domain reference data (authority table,
  variable registry, RADE's utility tables) deliberately stays with the code that owns
  it, distinct from tunable scalars.

---

### 3.16 `main.py` — the API surface

**Purpose.** FastAPI app: endpoints, middleware, error handling, and `_weather_request`,
the function that runs the whole pipeline.

**Mechanism.** `RequestIDMiddleware` enforces a size limit and stamps a request ID and
process time onto every response. Three exception handlers convert internal errors into
one consistent JSON error envelope. `_weather_request` executes stages 2-10 in order and
raises 503 if the reviewer failed. `_synthesize` builds the answer string from templates.

**Faults.**
- **[FIXED]** No rate limiting and no auth — an open port was an open proxy to the
  upstream APIs under our IP. `RateLimiter` (per-IP, 30/min · 1000/day, `/health`
  exempt) and an optional `WEATHERGPT_API_KEYS` gate now sit in `RequestIDMiddleware`.
- **[FIXED]** No input guardrail — any question reached location resolution and
  triggered real upstream calls. `check_question_fast` (deterministic, zero upstream
  calls) plus `query_guardrail.run_guardrail` (one LLM call to a strict
  `GuardrailAction`, cached since 2026-09-05, `AUDIT.md` A4) now run first.
- **[FIXED]** The catch-all handler discarded the exception. `unhandled_error` now logs
  it with `logger.exception` (full traceback) before returning the generic 500.
- **[FIXED]** `int(request.headers["content-length"])` on an unguarded client header
  could raise inside middleware. Now guarded with `declared.isdigit()` before the cast.
  (A related but distinct gap remains open: the check trusts `Content-Length` and is
  bypassable via chunked encoding or an omitted header — `BUG.md` B11.)
- **[FIXED]** `next(r for r in agents if r.agent_name == "reviewer")` raised
  `StopIteration` if the reviewer was ever absent. Now `next((...), None)` with an
  explicit `if reviewer is None or reviewer.status != "success"` check.
- **[FIXED]** `_synthesize` appended every evidence ID to the answer sentence. Now
  `wio.evidence[:3]`.
- **[FIXED]** `_weather_request` took an unused `request_id` parameter — the current
  signature is `_weather_request(req: QueryRequestV1, request: Request)`, no such
  parameter exists.
- **[FIXED]** `/health` reported `models.runtime_loading`, describing a model that no
  longer exists. It now reports `models.bias_correction` honestly as "not wired;
  responses use raw uncorrected forecast evidence."
- **[FIXED]** `_error` incremented the error metric for ordinary 404s/409s. Now only
  `if error.status_code >= 500`.

---

## 4. Cross-cutting

**Speed.** The request is still dominated by network I/O, not computation — connection
pooling, a circuit breaker for dead sources, a coarser cache key, and filtering to
planned variables are all in place. Rewriting anything in a faster language would save
milliseconds against network timeouts measured in seconds; not worth a second
toolchain. Current cold/warm latency: `CLAUDE.md`'s measured-latency table;
`AUDIT.md` §D for the 2026-09-05 session's changes to it.

**Observability. [FIXED]** No logging was configured anywhere outside the location
resolver, and that output went nowhere. `app/logging_config.py` now configures logging
app-wide with request-ID propagation via `ContextVar`.

**Memory. [FIXED]** Three unbounded dictionaries (`evidence_store`, `weather_cache`,
`location_cache`) grew for the life of the process. All three are now LRU-bounded with
expiry swept on write (3.13).

**Determinism.** The deterministic pipeline stages are still fully deterministic — same
question, same evidence, same answer, every number traceable to a source record. The
LLM explanation layer (small/big tier) is not: temperature 0.35, and `datetime.now()`
inside ranking, mean the same question can legitimately get different prose (never
different numbers) on two runs. Not a defect — the guarantee was always about numbers,
not wording — but worth stating precisely rather than leaving "fully deterministic" as
an unqualified claim.

---

## 5. Decisions made

The five decisions this document originally asked for were all made and implemented:

1. **Aggregation semantics** — option (a) was chosen: rain = total across the window,
   probability = window peak, temperature = min-max range. Implemented in
   `wio_builder._rain_panel`/`_temperature_panel` (3.12).
2. **"Sources agree" meaning** — the stricter option was chosen: `full_agreement`
   requires two *distinct* sources corroborating the same variable at the same
   timestamp; one source alone reports `single_source` (3.11-3.12, `AUDIT.md` A1/A3).
3. **Guardrail strictness** — built as a two-stage gate: a deterministic
   `check_question_fast` ahead of any network call, then an LLM-classified
   `GuardrailAction` (accept-location-only / accept-weather-full / reject / clarify /
   verify / unsupported-topic) with a deterministic fallback when the LLM is
   unavailable. See `app/services/guardrail.py` and `query_guardrail.py`.
4. **Rate limit numbers** — implemented as `rate_limit_per_minute`/`rate_limit_per_day`
   in `config.py`, defaulting to 30/min as suggested and **1000/day**, not the 500/day
   originally suggested here — raised during implementation, not a discrepancy to chase.
5. **CAP feed** — configured, and switched again since (`CAP_FEED_URL` now points at
   NDMA's Sachet feed, broader than the original IMD-only feed). Since 2026-09-05 it is
   fetched on every weather query rather than only on a warning keyword (3.5).
