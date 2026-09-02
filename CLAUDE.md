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
`mypy app` clean · 6 of 8 sources live.

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

### Next steps

1. **`IMD_API_KEY`** — the only blocked item needing you. Register at
   `api.imd.gov.in/public/login.php` (IP whitelisting; needs the EC2 elastic IP to exist).
   Set `IMD_API_KEY` and `IMD_API_BASE`.
2. **Close the reviewer gap before wiring the LLM.** `run_reviewer_agent` checks only that
   cited evidence IDs *exist*, never that the claimed value matches the CEO. Sufficient
   while agents are deterministic; not sufficient once an LLM writes claims.
3. **`app/services/model_client.py`** still does not exist — see `model.md` for the
   contract. Call it between the semantic gate and `build_wio` in `main.py`.
4. Rate limiting keys on `request.client.host`. Behind a proxy every caller shares one
   bucket; `X-Forwarded-For` is deliberately not trusted because it is spoofable.
5. `GFS` needs `requirements-full.txt` (eccodes needs system libraries — use Docker).

### Resume next session

```bash
cd ~/weathergpt && source .venv/bin/activate
pip install -r requirements-api.txt
pytest -q                                    # expect 107 passed
ruff check app tests && mypy app             # both clean
uvicorn app.main:app --host 0.0.0.0 --port 8001
```

Read `docs/SERVICES.md` first, then `cloud.md` for what remains.

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
pytest -q                                # full suite (107 tests as of last verified run)
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

## Architecture

WeatherGPT is an evidence-grounded weather intelligence backend, not a "call an LLM with a weather prompt" system. The governing rule throughout the codebase: **numbers come from deterministic pipelines, never from an LLM's imagination.** LLMs (when wired in) only ever explain or reason over data that's already been fetched, validated, and fused — they never select data sources, never invent values, and never bypass evidence citation.

### Request flow (`POST /query`, `app/main.py:_weather_request`)

```
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

**Agents (`app/agents/orchestrator.py`)** — currently deterministic Python functions that each derive a claim from the already-built WIO, *not* LLM calls. `reviewer_agent` is a hard gate: any claim citing an `evidence_id` not actually present in the retrieved evidence flips the whole request to a 503. `run_explanation_agent` is the one agent meant to eventually call the LLM — as of the last audit it returns an empty claims list (Groq is not yet wired into the live path; `app/orchestrator/groq_client.py` exists but has no caller from `app/main.py`).

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

## Code style

Match the existing codebase: no unnecessary comments (only ones explaining non-obvious *why*, never restating *what* the code does), no emojis, no debug prints. Don't create new files without a clear reason.
