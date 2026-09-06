# WeatherGPT

WeatherGPT is a modular FastAPI weather-intelligence backend. It normalizes source data into Canonical Evidence Objects (CEOs), applies deterministic semantic/temporal/spatial checks, builds a provenance-bearing WIO, and can run a risk-aware decision policy.

## Implemented

- CEO schema with source, valid window, statistic, accumulation window, provenance, retrieval time, and derived-evidence lineage.
- Deterministic location and time resolution. Unknown or ambiguous locations return structured errors; they are never mapped to a default city.
- Open-Meteo forecast and Open-Meteo ERA5 reanalysis adapters; NASA POWER historical adapter; CAP decoder and configured CAP feed adapter.
- Explicitly isolated IMD and GRIB2 integrations: they report unavailable until a configured, supported endpoint/decoder exists.
- Concurrent retrieval with per-source failure reporting and freshness-aware in-memory cache.
- Semantic compatibility gate: precipitation amount, rate, and probability cannot be mixed; accumulation windows must match.
- Warning evidence remains categorical and separate from numeric fusion.
- Structured deterministic agents, evidence-ID reviewer, WIO response, evidence lookup, SQLite user-context and feedback storage, and RADE v2.
- Versioned and compatibility API endpoints: `/health`, `/wio/query`, `/query`, `/decision`, `/rade/advise`, `/context`, `/feedback`, `/forecast`, `/warnings/active`, `/evidence/{id}`, `/metrics`.

## Experimental or unavailable

- Member-level ensemble output is accepted only when Open-Meteo returns actual member fields. A forecast mean is not presented as an ensemble.
- IMD requires a real compatible `IMD_API_KEY` and endpoint. CAP requires `CAP_FEED_URL`.
- GRIB2 is unavailable without `eccodes` and `cfgrib`; it is not used as a fallback.
- The checked-in ML artifacts and historical Kaggle claims are **not validated production metrics**. Training scripts must use a real, versioned dataset for reportable results.

## Run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-api.txt   # API only, no torch — see requirements.txt for the full ML/training set
uvicorn app.main:app --host 0.0.0.0 --port 8001
pytest -q
```

Example:

```bash
curl -X POST http://localhost:8001/wio/query \
  -H 'content-type: application/json' \
  -d '{"question":"Will it rain in Nagpur tomorrow afternoon?"}'
```

`GROQ_API_KEY`, `IMD_API_KEY`, and `CAP_FEED_URL` are optional configuration. Secrets are read only from environment variables and must not be committed.

## Runtime architecture

`POST /wio/query` resolves a supplied or unambiguous in-text location, deterministically normalizes time, creates a `RetrievalPlan`, retrieves independent sources concurrently, converts source records to CEOs, applies the temporal and semantic gates, persists evidence in the process index, builds a WIO, runs structured agents, and rejects the result when the reviewer finds an invalid evidence ID.

`POST /decision` runs the same path then invokes RADE v2. RADE uses member values only when present; otherwise it uses the source precipitation probability and amount as two explicit scenarios. If those are absent it returns `defer_decision`.

Weather data, decision mathematics, and response language remain separate. The current response synthesis is deterministic; Groq is an optional client utility and is not needed for liveness or readiness.

User context and feedback are user-ID scoped SQLite records. Context is retrieved only for the requesting `user_id`; no endpoint enumerates another user’s data.

## API

All request bodies are Pydantic-validated. Errors use `{error:{code,message,details,request_id}}`.

| Endpoint | Purpose |
| --- | --- |
| `GET /health` | Liveness, readiness, source configuration, cache and model-loading status. |
| `POST /wio/query` | Validated evidence retrieval and WIO only. |
| `POST /query` | WIO plus deterministic evidence-grounded synthesis. |
| `POST /decision`, `POST /rade/advise` | WIO plus RADE v2 result. |
| `GET /evidence/{id}` | A CEO generated in this running process. |
| `GET /forecast?location=Nagpur` | Convenience WIO forecast view. |
| `GET /warnings/active?location=Nagpur` | Active normalized warnings for a location query. |
| `POST /context`, `POST /feedback` | User-scoped SQLite context and outcome records. |
| `GET /metrics` | Process metrics and cache hit rate. |

The equivalent `/api/v1/` query, health, decision, context, and feedback routes are available where listed by OpenAPI. Location must be supplied explicitly or occur unambiguously in the question.

See [services](docs/SERVICES.md) for a plain-language walkthrough of every module,
[architecture](docs/architecture.md) for a diagram of the request pipeline,
[CLAUDE.md](CLAUDE.md) for coding rules and session history, [FIXES.md](FIXES.md) for the
current "what's left" index, and [BUG.md](BUG.md)/[AUDIT.md](AUDIT.md) for the detailed
defect register and 2026-09-05 teardown behind it.
