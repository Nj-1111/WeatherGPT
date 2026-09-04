# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Ownership boundary — read this first

The developer working in this repo owns **only the ML / data / training / inference-serving portions**. This is a hard scope boundary, not a preference:

**In scope:** the data pipeline (Open-Meteo/ERA5/GFS/IMD/NASA POWER/CAP retrieval), the CEO→WIO fusion pipeline (`app/services/`, `app/schemas/ceo.py`, `app/schemas/wio.py`), the bias-correction model's *integration path* (`app/services/model_client.py` — **not yet written**, see `model.md` for the contract it must implement; training happens in a separate repo), the LLM/agent orchestration layer (`app/agents/`, `app/orchestrator/`), the RADE decision engine (`app/rade/`), and the FastAPI inference-serving surface (`app/main.py`) as an API provider — plus the MLOps around all of that (checkpointing/hosting is the separate model repo's concern now; this repo's MLOps scope is basic EC2 inference deployment of the orchestration API).

**Permanently out of scope — do not propose or generate code for these unless explicitly asked:** any Android/mobile client, any dedicated frontend/UI, general backend/product engineering (auth, user accounts, billing, non-inference API surface), voice/TTS/STT, Nginx/Vercel/HF-Space *website* deployment, and CI/CD or release engineering beyond basic version control.

## Session record — 2026-09-02/03 hardening pass

A five-phase audit and hardening pass ran over `app/`. `docs/SERVICES.md` is the
plain-language explanation of every service (mechanism, connections, faults) and is the
best starting point for anyone new to this code.

**Verified state at end of session:** `pytest -q` 107 passed · `ruff check` clean ·
`mypy app` clean · 6 of 8 sources live. (A later same-day session took this to 120 tests —
see the reviewer-gate record below.)

### What changed, and why

**Foundations.** Every tunable scalar moved into `app/config.py` (~45 settings, all
env-overridable); domain reference data — the source authority table, the variable
registry, RADE's utility tables — deliberately stayed with the code that owns it.
`TTLCache`, `LocationCache` and `EvidenceStore` are now LRU-bounded with expiry swept on
write; previously all three grew until the process was killed. Added `app/logging_config.py`
(the app had no logging configured at all) with request-ID propagation via `ContextVar`.
`context/store.py` now honours `settings.database_path`, closes its connections (`with
sqlite3.connect(...)` commits but does **not** close), compares expiry as instants rather
than strings, and no longer writes to disk at import time.

**Evidence pipeline.** One shared pooled `httpx` client replaces a per-call client in every
adapter and geocoder. Added retry with backoff (only for genuinely transient failures — a
404 or missing key is not retried) and a circuit breaker so a dead source stops costing a
timeout per request. `plan.variables` is now actually applied — it had zero readers, so the
planner chose variables and nothing filtered on them. Cache keys round to ~1km against a
27km source grid. Removed ~288 deep Pydantic clones per cache hit that existed to stamp a
field nothing read.

- **`decoders/imd_json.py`**: records without coordinates were silently stamped with
  Nagpur's, then ranked by distance to the user as if local. Now skipped and logged.
- **CAP is live** (`cap-sources.s3.amazonaws.com/in-imd-en/rss.xml`, keyless). The feed is
  an RSS *index*; decoding it directly returns zero warnings while appearing to succeed, so
  the adapter does a two-step fetch. The decoder now keeps the polygon, uses `<onset>`
  rather than the issue time, and maps the event to the right variable (every alert was
  filed as heavy rain).
- **MET Norway added** — the only source independent of Open-Meteo. Before this, "sources
  agree" compared one vendor against itself. Needs an identifying User-Agent; generic ones
  get 403.
- Word-boundary keyword matching: `"go"` matched *Goa* and *mango*, turning weather
  questions into travel decisions.

**Fusion — the largest correctness fix.** `build_wio` answered a whole-day question with a
single arbitrary hourly record. Live, *"will it rain in Indore tomorrow"* returned **"Rain
unlikely (0%)"** for a day carrying 2.4mm and a 75% peak probability: it reported the
midnight hour. Rain is now summed across the window, probability is the window peak from
the same source, temperature is a min-max range, wind the window max — grouped by
`(source, accumulation_window)` so 1h and 6h records are never added together.
`full_agreement` now requires two *distinct sources* at the same timestamp; it previously
counted evidence objects, so one vendor's rain and temperature read as corroboration and
inflated RADE confidence 0.55 → 0.8. Ensemble members group per timestamp instead of
pooling across hours, and collapse to one summary row (a decision response carried ~840
evidence entries; now ~121, with members still retrievable by ID).

`ranker.py` gained a temporal term — all 96 hourly records scored an identical 0.88, so
"best evidence" was whichever was decoded first. Disagreement detection buckets by
timestamp and accumulation window; it previously compared a dry morning against a wet
evening from one source, and would have compared IMD's 24h totals against 1h totals.

**Time and place.** Windows resolve in the location's own timezone (Open-Meteo geocoding
supplies it; `zoneinfo`, no new dependency), not hardcoded IST. Fixed `"may"` matching
inside *maybe* and a day-of-month regex that took the first digits anywhere in the
sentence. Past dates now select historical sources.

**API surface.** Added a strict deterministic guardrail (`services/guardrail.py`) ahead of
location resolution — junk, off-topic and injection-shaped input is rejected with **zero
upstream calls**. Added per-IP rate limiting (`services/rate_limit.py`, 30/min, 1000/day;
`/health` exempt). RADE ran **twice** per decision request on different inputs and could
contradict itself — now computed once. `_domain` returns `None` instead of silently
applying travel utilities to unmatched questions. The forecast agent cites the panel
evidence its claims describe rather than the first three of all CEOs.

### Measured latency

| Stage | Cold | Warm |
|---|---|---|
| retrieval | 1585ms (63%) | 3ms |
| location | 907ms (36%) | 0.1ms |
| fusion | 14ms | 11ms |
| everything else | <8ms | <2ms |
| **total** | **2515ms** | **19ms** |

The request is entirely I/O-bound. Location resolution runs before retrieval and cannot be
parallelised with it (retrieval needs the coordinates), but its cache has a 30-day TTL, so
it only costs on a location's first use.

### Session record — 2026-09-03, reviewer gate and LLM seam

`cloud.md` §2 is closed. The reviewer no longer takes a claim's value on trust.

**The gate.** Every claim declares how its value was derived in `Claim.extra["derivation"]`
— `sum` / `max` / `min` / `identity` / `none`, with the variable, unit and accumulation
window it operated on. `app/agents/verification.py:verify_claim` re-runs that derivation over
the CEOs the claim actually cites and compares. Citing real evidence is necessary but no
longer sufficient. Two traps the verifier has to handle, both real in the fused panels: the
rain panel cites a `precipitation_probability` record alongside the amounts it summed (so the
verifier filters by declared variable *and* window before summing), and the wind panel reports
km/h from records that may be m/s (so the same conversion is re-applied). Panels now record
their own `variable` in `wio_builder.py`, which they previously did not — the temperature panel
picks between `temperature_2m` and `temperature_max` and the claim could not otherwise say which.

Comparison is `math.isclose` against `reviewer_value_rel_tol` / `reviewer_value_abs_tol`,
because panels round and exact equality would false-fail.

**Failure is split.** A contradicted value, a unit mismatch or an unknown evidence ID is an
error → 503, as before. A claim shape no verifier recognises is a *warning* — a future claim
type must not 503 the API just because nobody has written a verifier for it.

**Free text.** `check_prose_grounding` extracts every quantity carrying a unit (mm, %, C,
km/h) and requires each to match something the pipeline produced, allowing rounding to 0–2
places. Bare numerals are deliberately not checked — "the next 24 hours" is prose, not a
weather claim, and checking it yields only false rejections.

**LLM seam wired, off by default.** `run_explanation_agent` calls Groq only when
`WEATHERGPT_LLM_ENABLED=true` **and** `GROQ_API_KEY` is set. It receives a fact sheet built
from the WIO panels — never the raw CEO list — and is instructed to introduce no number. The
explanation is produced *before* the reviewer so the reviewer can check it, but stays last in
the returned agent list, preserving the existing response contract. Any failure (timeout, dead
key, empty response) degrades to the deterministic template answer with `status="partial"`; an
ungrounded number suppresses the explanation rather than failing the request
(`WEATHERGPT_REVIEWER_PROSE_FAILURE_MODE=fail` makes it fatal). `groq_client.py` was moved onto
the shared pooled `httpx` client and reads its key from `settings`, matching every adapter.

**Verified:** `pytest -q` 120 passed (13 new in `tests/test_reviewer.py`) · `ruff check` clean
· `mypy app` clean. Live against real sources: a 72-record Indore window summed to 22.7mm
passes the reviewer clean; with an invalid `GROQ_API_KEY` the 401 lands as explanation
`partial` and the request still returns 200 on the template answer. **The Groq success path
has never run against a real key** — it is covered only by tests with a stubbed client.

**What the gate still cannot do.** It confirms a claim is arithmetically faithful to the
evidence it cites, not that it cites the *right* evidence. An agent citing a real but
irrelevant CEO and reporting its value honestly still passes. Relevance is guaranteed by
construction (claims are built from the fused panels), not by the reviewer.

## Session record — 2026-09-04, intelligent guardrail/dispatch + marine data + deploy

Closed §2.9 (`/health` unauthenticated 8-way fan-out — now cached, `WEATHERGPT_HEALTH_CACHE_TTL_SECONDS`,
default 30s, in `app/adapters/registry.py`). Then a spec-driven initiative
(`CAPABILITY_MAP.md`, per-module `SPEC-*.md` files, `tasks/`) replaced the dead
`is_weather_related` boolean with a real dispatch key.

**The guardrail is now a decision, not a flag.** `app/services/query_guardrail.py`
replaces `query_extractor.py` (deleted, along with `NormalizedQuery`/`QueryIntent`, which
extracted an intent nothing ever branched on). One LLM call classifies every query into
`GuardrailAction`: `ACCEPT_LOCATION_ONLY` (skips retrieval/fusion/agents/RADE entirely —
"what are the coordinates of X" now answers in ~1s instead of running the full pipeline),
`ACCEPT_WEATHER_FULL` (unchanged), `REJECT_OFF_TOPIC`, `CLARIFY`, `VERIFY` (asks "did you
mean X?", confirmed via `session_router.py`'s new pending-verification store), and
`UNSUPPORTED_TOPIC` (a disaster type with no data source — earthquake, tsunami, wildfire,
landslide, volcanic, drought — answered honestly instead of rejected as off-topic or
fabricated with irrelevant weather data). The LLM applies a fixed decision-tree prompt,
never its own judgment; every non-accept action's message is a fixed Python template,
never LLM prose. Deterministic fallback (LLM down) covers every action except
CLARIFY(garbled)/VERIFY, which genuinely need language understanding.

**Scope actually widened, not just gated differently.** Marine/fishing, mountain/trek,
weather-driven disaster (cyclone/flood/storm/heat — already backed by CAP, just blocked
by the old narrow topic list), and route/travel planning are now accepted
(`guardrail.py`'s `TOPIC_WORDS`, the guardrail's system prompt). Found and fixed along the
way: `extract_place_phrase` had no lead pattern for "to" — "cyclone coming **to** Chennai"
passed the widened guardrail but then 422'd, since the location was never extracted.

**Marine data — genuinely new, not just unlocked.** `app/adapters/open_meteo_marine.py`
(primary, keyless) — verified live against the real API before writing it, which caught
that `ocean_current_velocity` is natively km/h, not m/s as first planned.
`app/adapters/stormglass_adapter.py` (fallback, keyed, `STORMGLASS_API_KEY`) — built to
its documented shape but **not** live-verified (needs a paid key). Six new
`CanonicalVariable`s, a `WIOWeather.marine` panel, `retrieval_planner.py` wiring. Proves
`adapter-extensibility` for real: both registered in `app/adapters/registry.py` with zero
other pipeline changes.

**Geoapify added as primary geocoder** (`app/services/location_resolver/providers/
geoapify.py`), existing Open-Meteo→Nominatim→India Post chain kept as fallback —
live-verified (correct fields, and ambiguity detection still works against its result
shape unmodified).

**`CAP_FEED_URL` switched to NDMA's Sachet feed** (`sachet.ndma.gov.in`, India's
multi-hazard alert aggregator, broader than the old IMD-only feed) — surfaced a real bug:
`cap_adapter.py`'s `_alert_links` filtered on `.xml`-suffixed links (IMD's shape); NDMA's
links are `FetchXMLFile?identifier=...` and were silently all dropped, which would have
made the new feed return zero warnings with no error. Fixed to accept any `http(s)` link,
live-verified (25 alert documents, 36 CEOs decoded correctly).

**Groq added as the small LLM tier** (`SMALL_LLM_MODEL=llama-3.3-70b-versatile`,
`SMALL_LLM_BASE_URL=https://api.groq.com/openai/v1`) — **currently 404s on every call**,
falling back to Gemini (which still works, proving the chain's resilience design). Model
name is the suspect; not yet fixed. See Next steps.

`TUNING_GUIDE.md` (which file to edit for a given customization), `AWS.md` (EC2 deploy —
Docker + a `weathergpt.service` systemd unit so it survives reboot), `coding_rules.md`
(general code-quality rules the user asked to be followed strictly going forward, on top
of the conventions already described in this file) added this session.

**Verified:** `pytest -q` 256+ passed (1 pre-existing unrelated failure — small LLM tier
unconfigured *in the test environment specifically*, not a real bug); `ruff`/`mypy` clean
throughout. Several live smoke tests against real external APIs (Geoapify, Open-Meteo
Marine, NDMA CAP feed) — this session verified against reality more than any prior one.

### Next steps

**Groq 404 fixed, same day.** `llama-3.3-70b-versatile` still exists in Groq's catalogue
but is gated to Enterprise pricing — a plain API key 404s on it rather than a clearer
403. Switched `SMALL_LLM_MODEL` to `openai/gpt-oss-120b` (standard pay-as-you-go tier).
Live-verified: `fallback_used=False`, `attempts=1`, ~1.0-1.2s latency — noticeably faster
than the Gemini fallback (2-6s) that had been silently absorbing every call until now.

1. **`IMD_API_KEY`** — still blocked on you. Register at `api.imd.gov.in/public/login.php`
   (IP whitelisting; needs the EC2 elastic IP to exist — see `AWS.md`). CAP (NDMA's Sachet
   feed, live and keyless) already covers official warnings including cyclone/flood/storm/
   heat, so this is no longer the only route to Indian warnings, just the richer one
   (forecasts, observations, rainfall on top).
2. **`app/services/model_client.py`** still does not exist — see `model.md` for the
   contract. Call it between the semantic gate and `build_wio` in `main.py`.
3. **`STORMGLASS_API_KEY`** — the fallback marine adapter is built but not live-verified
   (needs a paid signup key, unlike Open-Meteo Marine's keyless primary). If you get one,
   smoke-test it the same way Geoapify/Open-Meteo Marine were verified this session.
4. **`big_llm()`** (`app/llm/client.py`) is fully wired (config, chain, fallback) but
   **nothing calls it** — the "deterministic complexity trigger" that was supposed to wake
   it was never built. Either build that trigger or stop carrying `BIG_LLM_*` config as if
   it does something.
5. Security audit items **beyond §2.1/§2.9 remain open** — §2.2 (query-extractor prompt
   injection, assessed low actual impact), §2.3 (Nominatim's per-process throttle is a
   shared cross-tenant bottleneck), §2.5 (prose-grounding regex only catches 4 unit
   spellings), §2.6 (request-size cap is bypassable without `Content-Length`), §2.7 (no
   per-user cap on stored fact count/size), §2.8 (synchronous SQLite calls block the event
   loop), §2.10 (CAP adapter has no host-allowlist/XXE-hardening for feed-supplied URLs),
   §2.11 (raw exception text leaks into `retrieval_status`). None fixed this session —
   full detail in `docs/security-audit-2026-09-03.md`'s punch list.
6. Rate limiting keys on `request.client.host`. Behind a proxy every caller shares one
   bucket; `X-Forwarded-For` is deliberately not trusted because it is spoofable.
7. `GFS` needs `requirements-full.txt` (eccodes needs system libraries) — the Docker image
   built this session (`AWS.md`) still only installs `requirements-api.txt`; GFS stays
   unavailable in the container too until that's added.
8. `rag-app-info` and broader (non-weather-driven) disaster types remain deliberately
   deferred — see `CAPABILITY_MAP.md`.

### Resume next session

```bash
cd ~/weathergpt && source .venv/bin/activate
pip install -r requirements-api.txt
pytest -q                                    # expect 256+ passed, 1 pre-existing unrelated failure
ruff check app tests && mypy app             # both clean
set -a && source .env && set +a && uvicorn app.main:app --host 0.0.0.0 --port 8001
```

Read `docs/SERVICES.md` first, then `cloud.md` for what remains, `CAPABILITY_MAP.md` for
this session's module-by-module build record, and `TUNING_GUIDE.md` for "which file do I
edit to change X."

## Commands

**Environment setup** (no committed venv/lockfile):
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-api.txt      # everything needed to run the app + test suite
pip install -r requirements-full.txt     # adds GRIB2 decoding (cfgrib/eccodes/xarray) on top of requirements-api.txt
```
Two files. ML training moved to a separate repo (see `model.md`), so this repo carries no torch/ML dependency — `requirements-api.txt` alone runs the app and the test suite, and now lists only what is actually imported (fastapi, uvicorn, pydantic, httpx, pytest; pyyaml, shapely, pint, tenacity, orjson, python-dotenv and python-multipart were removed as unused). `requirements-full.txt` adds only the 3 packages the GRIB2 path imports (`cfgrib`, `eccodes`, `xarray`).

If `pip` itself is missing and you can't get sudo/`apt install python3-venv` in a sandboxed environment: `curl -sS https://bootstrap.pypa.io/get-pip.py | python3 - --user --break-system-packages`, then `pip install --user --break-system-packages <packages>`.

**Run the API:**
```bash
uvicorn app.main:app --host 0.0.0.0 --port 8001
```

**Tests:**
```bash
pytest -q                                # full suite (256+ tests as of last verified run)
pytest tests/test_ceo.py::test_comparable_gate   # single test
```
`testpaths=["tests"]` and `pythonpath=["."]` are set in `pyproject.toml`, so `pytest` runs correctly from repo root without extra flags.

**Lint / type-check** (configured in `pyproject.toml`, not currently run in any CI):
```bash
ruff check .        # line-length 100, target py310
mypy app             # ignore_missing_imports = true
```

**Training**: no longer happens in this repo — see `model.md` for the full handoff (dataset methodology, real validated results, and the integration contract `app/services/model_client.py` must implement once written).

**Docker:**
```bash
docker compose up --build   # python:3.10-slim, exposes :8001
```
Only `requirements-api.txt` is installed in the image — GFS/GRIB2 stays unavailable
inside the container too (see Next steps). `AWS.md` has the full EC2 deploy path
(Docker + a `weathergpt.service` systemd unit so it survives reboot).

## Architecture

WeatherGPT is an evidence-grounded weather intelligence backend, not a "call an LLM with a weather prompt" system. The governing rule throughout the codebase: **numbers come from deterministic pipelines, never from an LLM's imagination.** LLMs (when wired in) only ever explain or reason over data that's already been fetched, validated, and fused — they never select data sources, never invent values, and never bypass evidence citation.

### Request flow (`POST /query`, `app/main.py:_weather_request`)

```
query_guardrail.run_guardrail  (one LLM call → GuardrailAction; fixed decision-tree prompt,
                                 never the LLM's own judgment — see Session record 2026-09-04)
  ├─ REJECT_OFF_TOPIC / CLARIFY / VERIFY / UNSUPPORTED_TOPIC → fixed template message, return now
  ├─ ACCEPT_LOCATION_ONLY → location_resolver only → return now (no retrieval/fusion/agents/RADE)
  └─ ACCEPT_WEATHER_FULL ↓
location_resolver → time_parser → retrieval_planner (deterministic — LLM never picks sources)
  → retrieval.py (concurrent per-source fetch+cache, isolated failures)
    → adapters/*.py .fetch() → decoders/*.py → CanonicalEvidenceObject (CEO)
  → temporal_align.filter_by_window → semantic_gate.validated_evidence
  → wio_builder.build_wio  (ranker.rank + spatial_match + detect_disagreements → WeatherIntelligenceObject)
  → agents/orchestrator.run_all_agents  (forecast/warning/historical/observation/context/decision/reviewer/explanation)
  → rade/v2.decide  (only when a decision context is present)
  → _synthesize  (template-built answer string)
```

**CEO (`app/schemas/ceo.py`)** — the interoperability envelope every raw source record becomes. Carries `variable`/`statistic`/`unit`/`accumulation_window_hours` as closed enums (never mixed — e.g. a 1h and 24h precipitation accumulation are not comparable), plus a mandatory `provenance.transformations[]` trail. Different sources reporting the same variable are **never averaged** — each stays a separate CEO; conflict is only ever *flagged* (`ranker.detect_disagreements`, >10mm spread threshold), never silently resolved.

**WIO (`app/schemas/wio.py`)** — the single fused object everything downstream reads from. `weather.rain`/`wind`/`temperature` hold the highest-ranked value per variable (ranked by `0.4·source_authority + 0.25·freshness + 0.20·spatial_proximity + 0.15·quality`, table in `app/services/ranker.py:AUTHORITY`), but `evidence[]` still lists every surviving CEO for audit, and `agreement.status`/`disagreements[]` surface any conflict explicitly. Official warnings are structurally separate from numeric fusion — never blended in.

**Agents (`app/agents/orchestrator.py`)** — seven deterministic Python functions that each derive a claim from the already-built WIO, plus `run_explanation_agent`, the one seam where a language model runs. `reviewer_agent` is a hard gate on two counts: a claim citing an `evidence_id` not present in the retrieved evidence flips the request to a 503, **and** the value attached to that citation is recomputed from the cited evidence (`app/agents/verification.py`) and must match. LLM prose is checked instead for quantities the pipeline never produced. `run_explanation_agent` is inert unless `LLM_ENABLED=true` and the small LLM tier (`SMALL_LLM_MODEL`/`_BASE_URL`/`_KEY` in `.env`) is configured; it writes prose only, never selects a source, never originates a number, and degrades to the deterministic template answer on any failure.

**RADE (`app/rade/v2.py`, function `decide`)** — the risk-aware decision engine for questions like "should I spray." Builds 2 (or, with ensemble member data, 5-bin) scenarios from `wio.weather.rain`, scores each action as `expected_utility − risk_lambda·downside_risk`, picks the argmax. Returns `defer_decision` rather than guessing when evidence is insufficient — never fabricates a probability or amount. This is the *only* RADE implementation in `app/` — an older parallel v1 (`enumerator.py`/`utility.py`/`policy.py`) existed and was silently computed-but-discarded on every request; it's been removed from `app/` (still present, unmodified, in the separate frozen `kaggle_kernel/app/` snapshot, whose own `main.py` genuinely depends on it — don't delete that copy).

### Bias-correction model — integration path only, training lives elsewhere (`model.md`, `app/services/model_client.py`)

ML model training (GFS-forecast-vs-ERA5-reanalysis bias correction, formerly `training/` + `kaggle_kernel_m3/` in this repo) was moved out entirely to a separate repo. `model.md` at the repo root is the full handoff document: dataset construction methodology, feature engineering, real validated baseline results (LightGBM vs. ridge vs. no-correction on a real 24,960-row dataset), the MLP architecture that was attempted but never got a real GPU run, and — most importantly — the exact HTTP API contract this repo expects from that model once it exists.

**`app/services/model_client.py` does not exist yet** — this section describes the intended design, not current code. When written it should be a thin HTTP adapter calling the external model API, and be invoked between the semantic gate and `build_wio` in `app/main.py` so corrected values flow into fusion with a `provenance.transformations[]` entry. `CanonicalEvidenceObject` already carries unused `parent_ids`/`transformation`/`transformation_timestamp`/`algorithm_version` fields for exactly this. Until then every response uses raw, uncorrected forecast evidence, which `/health` reports honestly as `models.bias_correction`.

### Location resolution (`app/services/location_resolver/`)

A package, not a module — the old single-file resolver with its 8-city gazetteer and 7-PIN dict is gone. Resolution order is coordinates → cache → PIN → normalized place name → provider chain → rank → ambiguity. Providers (`providers/`) are tried in order: Open-Meteo Geocoding (primary, keyless, structured admin1/admin2/population), then Nominatim/OSM (covers Indian districts, states, small towns and historical aliases that Open-Meteo lacks), with India Post for PIN codes. All keyless.

India is preferred by **scoring, not filtering** (`ranking.py`): `log10(population) + 2.0 if India + 1.0 if capital + 0.5 exact-name`. Filtering by `countryCode=IN` was tried and rejected — it resolves "Springfield" to a Tamil Nadu hamlet. A winner is auto-picked only when it beats the runner-up by `settings.geocoding_dominance_margin`; otherwise `LocationAmbiguousError` → 409, never a coin-flip. `seed.py` is an offline last resort (exact match only) that still raises rather than fabricate coordinates for an unknown place.

`resolve_location`/`extract_location` are **async** — they perform network geocoding.

## Known state, don't assume otherwise

- Root-level `architecture.md`, `implementation.md`, `report.md`, `setup.md`, `INSTALL.md`, and ten dated `docs/*_2026-09-01.md`/planning docs described an earlier/aspirational system built by a previous developer (a different machine path, a different Kaggle account, a fully-live Groq multi-agent pipeline, nonexistent endpoints like `GET /plan`, self-reported metrics later found unverified, and — in `report.md` — a partially-visible API key fragment) that did not match current code. Deleted as stale in this session; `README.md` and `docs/ARCHITECTURE.md`/`docs/API.md`/`docs/PROOF_OF_WORK.md` remain the accurate source of truth.
- No ML metric currently in this repo is independently validated — the real validated baseline numbers (LightGBM/ridge vs. no-correction) that used to be summarized in `docs/PROOF_OF_WORK.md` now live in `model.md`, since ML training itself moved to a separate repo.
- The old RADE v1 snapshot (`kaggle_kernel/`) and the M1/M3 training kernels (`kaggle_kernel_checker/`, `kaggle_kernel_official/`) were deleted along with M1/M3 themselves — do not reference or try to resurrect them. The bias-correction model (formerly `kaggle_kernel_m3/`, informally "M3") is unrelated to the deleted M3 intent-parser above — don't confuse them if the name resurfaces in old commits.
- `cloud.md` at the repo root is the live outstanding-work register. It was rewritten at the end of the 2026-09 hardening pass to record what remains, not what was fixed. Read it before proposing work.
- `docs/SERVICES.md` explains every service in plain language — mechanism, connections, and known faults. Start there.
- All ML training code (`training/`, `kaggle_kernel_m3/`) and the Kaggle training guide were removed from this repo as of this change — training now happens in a separate repo. See `model.md` for the full handoff.
- `CAPABILITY_MAP.md` + per-module `SPEC-*.md` files + `tasks/{plan,todo}.md` track the 2026-09-04 guardrail/marine/geoapify initiative module by module — read `CAPABILITY_MAP.md` first for what's built vs. deferred before starting related work.
- `TUNING_GUIDE.md` maps "I want to change X" to the exact file — check there before searching.
- `AWS.md` + `weathergpt.service` are the EC2 deployment path (Docker + systemd, survives reboot).

## Code style

Match the existing codebase: no unnecessary comments (only ones explaining non-obvious *why*, never restating *what* the code does), no emojis, no debug prints. Don't create new files without a clear reason. `coding_rules.md` at the repo root is a stricter, more detailed rule set the user asked to be followed on every change going forward — it doesn't contradict the above, it's more exhaustive (import hygiene, dead-code removal, performance/data-structure choices, cache-artifact cleanup after each task).
