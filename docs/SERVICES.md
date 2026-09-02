# The Services Layer, Explained

This document explains every service in `app/`: what it is for, how its mechanism
actually works, where it sits in the request flow, and what is wrong with it.

It assumes no weather-domain background. Read section 1 and 2 first; the rest is
reference, one file at a time.

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
  1. guardrail            (does not exist yet)
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

## 2. A real trace, and the central bug

I ran the real pipeline against the live Open-Meteo API for *"will it rain in Indore
tomorrow"*. This is not a hypothetical:

```
window: 2026-09-03 00:00 IST -> 23:59 IST     horizon: short
raw CEOs fetched:            288
after time-window filter:     96
after semantic gate:          96
distinct ranking scores:       1   over 96 objects
```

The system's answer:

```
summary:     "Rain unlikely (0%)."
rain panel:  value_mm 0.0, probability 0.0, accumulation_hours 1
             valid_from 2026-09-02T19:00:00Z   <- 00:30 IST, the first hour of the day
temperature: 23.5 C
agreement:   full_agreement — "Sources agree on occurrence and magnitude"
```

The actual forecast contained in the very evidence it fetched:

```
24 hourly precipitation values, day total   2.4 mm
peak hourly probability                     75 %
temperature range                     22.5 - 29.2 C
```

**The system said "rain unlikely, 0%" about a day with 2.4mm of rain and a 75% peak
chance.** Nothing was hallucinated and nothing was mis-fetched. The evidence was
correct and complete. The fusion stage simply reported *the first hour of the day* —
midnight, when it was dry — and presented it as the answer for the whole day.

Three separate defects combine to produce this, and each is worth understanding
because the same pattern recurs elsewhere:

**(a) There is no aggregation.** `wio_builder` picks exactly one CEO per variable and
reports it. But "will it rain tomorrow" is a question about a 24-hour window, and the
evidence is 24 separate hourly facts. Rain must be **summed** over the window (2.4mm),
probability taken as the **maximum or a combination** (75%), temperature reported as a
**range** (22.5-29.2). Reporting one hour as if it were the day is a category error:
it answers a question nobody asked.

**(b) Ranking cannot tell the hours apart.** The score is
`0.4*authority + 0.25*freshness + 0.20*proximity + 0.15*quality`. For 96 objects from
one source, at one location, issued at one moment, **every one of those terms is
identical** — all 96 scored exactly 0.88. So "the highest-ranked evidence" is
meaningless here; the sort is stable, so the winner is simply whichever object was
decoded first, which is the earliest hour. The ranker has no notion of *when* the
evidence is valid relative to *when the user asked about*.

**(c) `full_agreement` is asserted from a single source.** The check is
`len(scored) >= 2` — two or more *evidence objects*. But those two can be the
temperature and the rainfall from the same API call. Here, 96 objects from one vendor
produced "Sources agree on occurrence and magnitude". There was no second source to
agree with. This is not cosmetic: RADE reads this field and raises its confidence from
0.55 to 0.8 on the strength of corroboration that never happened.

Everything else in this document is smaller than this.

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
- **[DEAD]** `source_retries` — defined, and read by nothing. There is no retry logic.
- **[DEAD]** `enable_llm` — zero readers.
- **[DEAD]** `database_path` — defined, but `context/store.py` hardcodes
  `Path("weathergpt.db")` and ignores it.
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

**Fault.** **[WRONG]** `IST` being a global constant is the root of the timezone
problem in `time_parser`. See 3.4.

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
  block violators by IP. Nothing in the code enforces this. Under load, this is how
  the EC2 instance gets banned.
- **[SLOW]** PIN resolution costs two network round trips: India Post gives a district
  and state but no coordinates, so the district name is then geocoded.
- **[SLOW]** Every provider call opens a fresh HTTPS connection.
- **[RISK]** The cache only evicts an expired entry when that exact key is looked up
  again. Keys never queried again are retained forever.
- **[TIDY]** `logger.info(..., extra={...})` is called, but no logging is configured
  anywhere in the app, so none of it is visible.

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
the historical reanalysis sources, a `nowcast` does not.

**Connects.** `main._weather_request` calls it third; output goes to
`build_retrieval_plan`, to `retrieval` (as the fetch range), and to
`temporal_align.filter_by_window`.

**Faults.**
- **[WRONG]** **Every request is parsed in IST**, regardless of where the user asked
  about. "Tomorrow" for Springfield, Illinois is computed as an Indian calendar day.
  For any non-Indian location the window is offset by hours and can select the wrong
  day entirely.
- **[WRONG]** The month-name loop tests `if mon in text_l` for each of
  `jan feb mar ... dec`. `"may"` is a substring of **"maybe"**, so *"will it rain
  tomorrow, maybe?"* is parsed as a date in May.
- **[WRONG]** Within that branch, `re.search(r"(\d{1,2})")` takes the **first digits
  anywhere in the sentence** as the day of month. "2 pm on Aug 5" yields August 2nd.
- **[RISK]** Two bare `except:` clauses. A bare except also swallows `KeyboardInterrupt`
  and `SystemExit`, which makes a process hard to shut down cleanly.
- **[DEAD]** `elif "next 3 days" ... : pass` in the first chain — the second chain
  handles it; the branch does nothing but prevent later branches from being reached.
- **[DEAD]** `QueryRequestV1` has a `timezone` field. Nothing reads it.
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
- **[DEAD]** **`plan.variables` and `plan.evidence_classes` have zero readers in the
  entire codebase.** The planner carefully decides which variables are needed, and
  then nothing filters on them. Every adapter returns everything it has. This is why
  the Indore trace carried 288 objects when the question needed two variables — roughly
  four times more data than necessary through ranking, gating, fusion and serialization.
- **[WRONG]** Keyword matching is plain substring, not word-boundary:
  - `"go"` matches **Goa**, **mango**, **going**, **agro** — a weather question about
    Goa is classified as a travel decision, which silently changes which sources are
    fetched and makes RADE produce a recommendation nobody asked for.
  - `"sea"` matches **season**.
  - `"fish"`, `"crop"` and others have the same shape of problem.
- **[TIDY]** Hindi keywords are present but there is no language routing yet.

---

### 3.6 `retrieval.py` — fetching, concurrently and safely

**Purpose.** Fetch every planned source at once, cache results, and make sure one dead
source can never fail the request.

**Mechanism.** `_one()` per source: build source-specific arguments, hash them into a
cache key, return the cached CEOs on a hit, otherwise call the adapter under
`asyncio.wait_for`, normalize raw JSON to CEOs via the decoder, cache, return.
`retrieve()` runs all of these under `asyncio.gather`.

**The isolation contract is the good part.** `_one` catches every exception per source
and returns it as data, not as a raised error. A failed source becomes an entry in
`retrieval_status` with `partial: True`, and the request continues with what survived.
This is exactly right and should be preserved.

**Faults.**
- **[DEAD]** `_one()` takes a `plan` argument and never reads it.
- **[SLOW]** **No connection pooling.** Every adapter opens its own
  `httpx.AsyncClient` per call — a fresh TLS handshake per source per request. On one
  box this is the single largest avoidable latency.
- **[RISK]** **No retry.** One transient 502 drops that source from the answer entirely.
- **[SLOW]** **No circuit breaker.** CAP and IMD are unconfigured and fail every time,
  yet are re-dialled on every request, each paying a full timeout. The whole `gather`
  is bounded by the slowest source.
- **[SLOW]** Timeouts are hardcoded per adapter (8/10/20/30s) *and* wrapped at 20s
  here, so the 30s ERA5 timeout is unreachable and the effective value is a surprise.
- **[SLOW]** The cache key rounds coordinates to 4 decimal places — about 11 metres.
  The underlying source grid is 0.25 degrees, about 27km. Two users in the same city
  share no cache entry despite being served identical data.
- **[SLOW]** On a cache hit, every CEO is `model_copy(deep=True)`-ed — hundreds of deep
  Pydantic clones per request, purely to stamp a cache-metadata field.
- **[SLOW]** One TTL for everything. Historical reanalysis data, which cannot change,
  expires after 15 minutes like a live forecast.
- **[TIDY]** `__import__("datetime").datetime.now(...)` inline instead of an import.

---

### 3.7 `adapters/` and `decoders/` — one per source

**Purpose.** An adapter knows how to *talk* to one source (`fetch`), how to convert its
reply into CEOs (`normalize`, usually delegating to a decoder), and how to report its
own health. The uniform interface is what lets `retrieval` treat all sources alike.

**Registry.** `adapters/registry.py` maps source name to instance:
OPEN_METEO, ERA5, GEFS, CAP, NASA_POWER, IMD, GFS.

**The state of the sources today** matters more than the code:

| Source | Authority | State |
|---|---|---|
| CAP | 1.00 | **Unconfigured** — no feed URL. Official warnings never enter the system. |
| IMD | 0.95 | **Unconfigured** — no API key. India's own met authority is absent. |
| GEFS | 0.72 | Live (Open-Meteo ensemble) |
| OPEN_METEO | 0.70 | Live |
| GFS | 0.70 | Off — needs the GRIB2 libraries |
| NASA_POWER | 0.65 | Live |
| ERA5 | 0.50 | Live (Open-Meteo historical) |

**Four of the five live sources are Open-Meteo endpoints.** This is the single most
important fact about the system's current quality: "sources agree" is being computed
over what is effectively one vendor. Configuring the CAP feed is the highest-value
single change available, because CAP carries the highest authority and drives RADE's
risk escalation, which is currently dead code in practice.

**Faults.**
- **[WRONG]** `decoders/imd_json.py:50,65,91` —
  `coordinates=[record.get("lon", 79.08), record.get("lat", 21.14)]`. Any IMD record
  missing coordinates is silently stamped with **Nagpur's**. It then passes the
  semantic gate, scores 0.95 authority, and is ranked by distance to the user using
  coordinates that are fiction. A user in Chennai can be shown an IMD observation
  presented as local, with a clean provenance trail hiding it. This is the most
  dangerous line in the repo.
- **[WRONG]** `nasa_power.fetch` defaults to `start="20240101", end="20240102"`.
  Unreachable today because `retrieval` always passes dates, but any future caller
  that omits them gets two days of January 2024 returned as current evidence.
- **[RISK]** Health probes hardcode Nagpur and 2024-01-01, so `/health` reports a
  source "available" based on one probe location.
- **[RISK]** Bare `except:` in `open_meteo_historical` and `nasa_power`.
- **[TIDY]** `print()` instead of logging in `grib2_placeholder.py`.

---

### 3.8 `temporal_align.py` — dropping evidence outside the window

**Purpose.** Keep only CEOs whose validity period overlaps the question's window, and
compute how stale a piece of evidence is for ranking.

**Mechanism.** `overlaps()` is an interval-intersection test with a fallback: if a CEO
has no validity period, fall back to when it was issued or observed. `to_utc()`
normalizes everything to UTC and treats a naive datetime as UTC.

**Connects.** Called by `main` immediately after retrieval — this is what cut 288
objects to 96 in the trace. `staleness_hours` is called by `ranker`.

**Faults.**
- **[RISK]** A CEO with *no* time information at all returns `True` — it is kept
  unconditionally. Combined with the geometry default below, evidence with neither a
  known place nor a known time flows through the pipeline scoring well.
- **[TIDY]** `staleness_hours` returns the magic number `999` for unknown.
- **[TIDY]** `to_utc(None)` returns `None` while annotated as returning `datetime`.

---

### 3.9 `spatial_match.py` — how far is this evidence from the user

**Purpose.** Great-circle distance between a CEO's location and the query point, for
the proximity term in ranking.

**Mechanism.** Standard haversine formula. `distance_to_query` extracts the point from
the CEO geometry.

**Faults.**
- **[WRONG]** `if geometry is None or coordinates is None: return 0.0`. Zero distance
  means *exactly at the query point*, which yields the **maximum** proximity score.
  Evidence whose location is unknown is therefore ranked as though it were perfectly
  local. The comment says this is for polygon/district evidence "treated as covering",
  which is defensible for a district warning — but the same branch catches genuinely
  unlocated data. Combined with the IMD Nagpur default, unlocated evidence is scored
  optimistically twice over.
- **[TIDY]** `except Exception: return 9999` swallows the reason.

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
- **[DEAD]** `REGISTRY = load_registry()` runs at import and loads an optional
  `variable_registry.yaml` override — and **nothing reads `REGISTRY`**.
  `validate_semantics` reads `DEFAULT_REGISTRY` directly. The documented extensibility
  mechanism does nothing at all. (The YAML file also does not exist, so the `yaml`
  dependency is currently carried for a code path with no effect.)
- **[SLOW]** `validate_semantics` rescans all ~45 registry entries building a list, for
  every CEO. That is 96 x 45 wasted comparisons on the Indore query, where a
  precomputed index by canonical name would be one dictionary lookup.
- **[TIDY]** Rejection reasons are collected and then flattened by `main` into a single
  vague sentence, "Some incompatible evidence was rejected." Nothing logs which, or why.

---

### 3.11 `ranker.py` — which evidence wins

**Purpose.** Order evidence by trustworthiness, and flag disagreement between sources.

**Mechanism.**
`score = 0.4*authority + 0.25*freshness + 0.20*spatial + 0.15*quality`

- *authority* — a fixed table: CAP 1.0 (official warnings), IMD 0.95 (national met
  authority), down to ERA5 0.5. This encodes "an official warning outranks a model
  forecast", which is the correct instinct.
- *freshness* — staleness capped at 72h, linearly inverted.
- *spatial* — `1/(1 + km/50)`, so 50km is the half-decay distance.
- *quality* — 1.0 unless the CEO carries a bad quality flag.
- Warnings get a +0.1 authority boost.

`detect_disagreements` flags when precipitation values spread more than 10mm.

**Connects.** `wio_builder` calls `rank` before fusing; the ordering decides the
contents of every WIO panel.

**Faults.**
- **[WRONG]** **The score cannot distinguish two CEOs from the same source.** Same
  authority, same location, same issue time, same quality — all identical, as the
  Indore trace proved (96 objects, 1 distinct score). The ranker is only meaningful
  *between* sources, but it is being used to choose *within* a source, where it
  degrades to insertion order. There is no term for how close the evidence is to the
  time the user actually asked about.
- **[WRONG]** `detect_disagreements` compares precipitation across the **whole time
  window**, not between matching timestamps. A dry morning and a wet evening from a
  single source read as sources disagreeing — spurious `partial_agreement`.
- **[WRONG]** It also compares values with **different accumulation windows** directly.
  An IMD 24-hour total against an Open-Meteo 1-hour total will differ by far more than
  10mm as a matter of arithmetic, not of disagreement. Once IMD is configured this
  will fire constantly. This is precisely the confusion the CEO schema was designed to
  prevent, reintroduced at the comparison step.
- **[TIDY]** Only precipitation is checked; temperature and wind disagreement is never
  detected.
- **[TIDY]** The weights, the authority table, 50km, 72h and 10mm are all inline
  literals.
- **[TIDY]** `def rank(..., now: datetime = None)` should be `datetime | None`.

---

### 3.12 `wio_builder.py` — many facts become one answer

**Purpose.** The fusion stage. Turn a ranked CEO list into the single WIO the rest of
the system reads.

**Mechanism.** Rank everything. Walk the ranked list and keep the first CEO seen per
variable (`best_by_var`). Build the rain, temperature and wind panels from those.
Collect warning CEOs separately and surface the highest severity. Run disagreement
detection. Summarize every surviving CEO into the evidence list. Compose a summary
sentence.

**Connects.** Called by `main` at stage 9. Its output is read by every agent, by RADE,
and by `_synthesize`. **Everything after this point sees only the WIO** — so a mistake
here is invisible to all downstream validation, including the reviewer agent.

**Faults.** This file holds the most damage in the codebase.
- **[WRONG]** **No window aggregation** — the central bug from section 2. One hour is
  reported as the day.
- **[WRONG]** **`full_agreement` from `len(scored) >= 2`**, where both objects can come
  from the same source. Claims corroboration that does not exist and raises RADE's
  confidence from 0.55 to 0.8 on it.
- **[WRONG]** **Rain amount and rain probability are selected independently.** The
  amount comes from `best_by_var`; the probability comes from a separate loop that
  `break`s on the first probability CEO in rank order. Nothing ties them to the same
  hour. "60% chance of 5mm" can pair Tuesday's probability with Monday's amount.
- **[WRONG]** **`member_values` pools ensemble members across all timestamps.** An
  ensemble is many simulations of *the same moment*; pooling across hours smears
  RADE's probability distribution over time and makes its 5-bin scenarios meaningless.
- **[SLOW]** The rain panel's `evidence_ids` includes **every** probability CEO in the
  window (25 in the trace). `wio.evidence` includes **every** surviving CEO (96).
  Both are serialized into every response.
- **[TIDY]** Comments restate the code (`# rain`, `# agreement`, `# group by variable`),
  against the project style rule.
- **[TIDY]** Summary thresholds (0.6, 0.3) inline.

---

### 3.13 `cache.py` and `evidence_store.py` — process memory

**Purpose.** `TTLCache` avoids refetching a source within its TTL and always keeps
freshness metadata alongside the value, so a cached answer can still be described
honestly. `EvidenceStore` indexes CEOs by ID so `GET /evidence/{id}` can show the user
exactly what backed an answer — the audit trail that makes the citations real.

**Faults.**
- **[RISK]** **Neither has any bound or eviction.** `EvidenceStore` grows by roughly
  one entry per CEO per request — 96 on a single Indore query — and **never removes
  anything**. `TTLCache` counts a stale entry as a miss but never deletes it. On a
  long-running EC2 process both grow until the process is killed. This is the clearest
  production stopper in the repo.
- **[RISK]** Both are per-process. With multiple uvicorn workers, `GET /evidence/{id}`
  returns 404 whenever the follow-up request lands on a different worker. Run one
  worker, or move to Redis.

---

### 3.14 `agents/orchestrator.py` — the claim layer

**Purpose.** Eight agents each derive a structured claim from the already-built WIO and
evidence, and a **reviewer** gates the result.

Despite the naming, **these are plain Python functions, not LLM calls.** No agent calls
a model. `model_for(role)` populates a cosmetic `model` field on every result, which
creates a false impression of multi-model routing in the response.

**The reviewer is the important one.** It walks every claim from every agent and checks
that each cited `evidence_id` actually exists in the retrieved evidence. If any claim
cites something absent, `main` turns the whole request into a **503**. This is the
anti-hallucination gate, and it is the mechanism that will matter most once an LLM is
wired in.

**Faults.**
- **[WRONG]** Agents slice **unranked** evidence arbitrarily — `ceos[:3]`, `hist[:2]`,
  `obs[:2]`. Their claims can therefore cite different CEOs than the WIO panels shown
  to the user, from the same request.
- **[WRONG]** `run_forecast_agent` sets `evidence_ids` to the first three of *all*
  CEOs, unrelated to the claims it just made. The grounding gate then passes on
  citations that do not support the claim.
- **[WRONG]** **RADE runs twice per decision request** — once inside
  `run_decision_agent` (using `wio.query.raw_text`) and again at the endpoint (using
  `req.decision_type or req.question`). Duplicate computation on *different inputs*,
  so the agent's stated recommendation can disagree with the one actually returned.
- **[RISK]** **The reviewer checks only that evidence IDs exist, never that the claimed
  value matches the CEO.** Sufficient while agents are deterministic; **not** sufficient
  the moment an LLM lands. This must be closed before the LLM seam is enabled.
- **[TIDY]** `AgentResult.status` is a bare string; a typo silently becomes a 503.
- **[TIDY]** `datetime.utcnow()` (deprecated, naive) — the source of all 19 test
  warnings. `import time, asyncio` on one line; `time.time()` for durations where
  `monotonic` is correct.

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
- **[WRONG]** `_domain()` falls through to `"travel"`. An unmatched decision question
  silently receives travel-policy utilities instead of deferring — the one place the
  file abandons its own "defer rather than guess" principle.
- **[WRONG]** `_domain` matches keywords against `f"{context} {user_context}"` — the
  user's whole context dictionary stringified. A stored user fact containing "crop"
  flips the decision domain for an unrelated question.
- **[RISK]** The warning-driven risk escalation is dead in practice, because CAP is
  unconfigured and `official_warning.active` is permanently `False`.
- **[TIDY]** Utility tables and scenario bin edges are inline literals. Defensible as
  policy, but they are exactly the kind of value that should be tunable data.

---

### 3.16 `main.py` — the API surface

**Purpose.** FastAPI app: endpoints, middleware, error handling, and `_weather_request`,
the function that runs the whole pipeline.

**Mechanism.** `RequestIDMiddleware` enforces a size limit and stamps a request ID and
process time onto every response. Three exception handlers convert internal errors into
one consistent JSON error envelope. `_weather_request` executes stages 2-10 in order and
raises 503 if the reviewer failed. `_synthesize` builds the answer string from templates.

**Faults.**
- **[RISK]** **No rate limiting and no auth.** An open port is an open proxy to the
  upstream weather APIs under our IP. Nominatim and NOMADS both enforce fair use and
  will block.
- **[RISK]** **No input guardrail.** Any question at all reaches location resolution and
  triggers real upstream network calls. There is nothing to reject junk, off-topic,
  abusive or injection-shaped input before it costs money and quota.
- **[RISK]** The catch-all handler **discards the exception**. Every 500 is
  unattributable, and there is no logging configured to have caught it anyway.
- **[RISK]** `int(request.headers["content-length"])` on a client-supplied header with
  no guard — a malformed value raises inside middleware, where the exception handlers
  cannot format it.
- **[WRONG]** `next(r for r in agents if r.agent_name == "reviewer")` raises
  `StopIteration` if the reviewer is ever absent, which surfaces as an opaque 500.
- **[SLOW]** `_synthesize` appends **every** evidence ID to the answer sentence —
  hundreds of UUIDs embedded in prose meant for a human.
- **[DEAD]** `_weather_request` takes `request_id` and never uses it. Two callers pass
  the literal strings `"warnings"` and `"forecast"` for it.
- **[TIDY]** `/health` reports `models.runtime_loading`, describing a local model that
  no longer exists, and uses an inline `__import__("os")`.
- **[TIDY]** `_error` increments the error metric for ordinary 404s and 409s.

---

## 4. Cross-cutting

**Speed.** The request is dominated by network I/O, not computation, so the wins are:
connection pooling, a circuit breaker for dead sources, a coarser cache key, and
filtering to the planned variables. Rewriting anything in a faster language would save
milliseconds against 20-second network timeouts — it is not worth a second toolchain.

**Observability.** No logging is configured anywhere outside the location resolver, and
that output goes nowhere. In production there would be nothing to diagnose an incident
with.

**Memory.** Three unbounded dictionaries (`evidence_store`, `weather_cache`,
`location_cache`) grow for the life of the process.

**Determinism.** The pipeline is fully deterministic today, which is a genuine asset:
the same question with the same evidence gives the same answer, and every number is
traceable to a source record. Every fix below should preserve that.

---

## 5. Decisions I need from you

**1. Aggregation semantics — the important one.**
The system must stop reporting one hour as a whole day. The question is what it should
report instead for *"will it rain tomorrow"*:
- **(a)** Rain = **total across the window** (2.4mm), probability = **peak hour** (75%),
  temperature = **min-max range** (22.5-29.2C). Most informative and matches how a
  forecast is normally spoken. My recommendation.
- **(b)** Same totals, but also keep a **worst-hour** panel, so a decision engine can
  see that 0.5mm falls in one specific hour rather than drizzling all day. More useful
  for spray/harvest decisions, slightly more complex.
- **(c)** Keep single-hour selection but pick the hour nearest the middle of the window.
  Smallest change, still wrong for accumulations.

**2. What should "sources agree" mean** when only one vendor is reachable? Currently it
claims full agreement. Options: report `single_source` honestly (my recommendation), or
report agreement only when two *distinct sources* corroborate the same variable at the
same timestamp, and `single_source` otherwise.

**3. How strict should the guardrail be?** You asked for very strict. I read that as:
reject anything that is not a weather question, cap length and complexity, reject
prompt-injection shapes and abuse, and return a structured 4xx **before** any network
call. The trade-off is false rejections of unusual but legitimate questions. Should an
ambiguous input be rejected, or allowed through with reduced source fan-out?

**4. Rate limit numbers.** You said hardcode them. Suggest 30 requests/minute per IP
and 500/day per IP, tunable by environment variable with those as the defaults. Say if
you want different numbers.

**5. CAP feed.** Configuring it is the highest-value single change to answer quality —
it turns official warnings on, and makes RADE's risk escalation real. Do you have a
feed URL, or should I find the appropriate Indian CAP endpoint?
