# WeatherGPT — ML/Inference Ownership Scope

## Who I am on this project

I am taking over the **ML and intelligence-pipeline portion** of WeatherGPT, an
existing repository originally built by a teammate for a SIH (Smart India
Hackathon) submission. I did not build the initial version — I am inheriting
it, auditing it, and now owning its development going forward.

**I am explicitly NOT responsible for, and do not want help with:**
- The Android app / any mobile client
- Any dedicated frontend or UI
- General backend/product engineering (auth systems, user accounts, billing,
  non-inference API surface, etc.)
- DevOps concerns unrelated to serving a model (Nginx gateways, Vercel/HF
  Space deployment of a *website*, voice UI, multilingual TTS/STT, etc.)

**What I *am* responsible for, end to end:**
1. The **data pipeline** — pulling weather data from multiple sources/APIs
2. The **ML model's integration path** — model training itself now happens
   in a separate repo (see `model.md` at the repo root for the full
   handoff: dataset methodology, results, architecture). This repo owns
   *consuming* the trained model via a documented HTTP API contract
   (`app/services/model_client.py`), not training/evaluating/publishing it.
3. The **LLM orchestration / agent layer** — how multiple LLMs and the
   deterministic pipeline work together to go from a user query to a
   trustworthy weather answer
4. ~~Training infrastructure~~ — moved to the separate model repo, no
   longer this repo's concern.
5. ~~Model hosting~~ — the separate model repo's concern; this repo only
   calls it over HTTP.
6. **Inference serving** — the FastAPI backend that calls out to Groq and
   the external model API
   and LLMs, exposed as a clean, hostable inference endpoint (e.g. via
   Docker, a HF Inference Endpoint, Modal, RunPod, or similar "easy to spin
   up" instance) — this is the *only* deployment surface I care about. No
   product-facing app sits on top of it that I'm responsible for.
7. **Code hosting** — I only push source code to GitHub; I am not building
   CI/CD pipelines, release processes, or anything beyond version control
   hygiene for this codebase.

---

## What the system architecturally is (as inherited, verified by code audit)

WeatherGPT is a **evidence-grounded weather intelligence backend**, not a
plain "call an LLM with a weather prompt" system. The core design principle
throughout the codebase is: **numbers come from deterministic pipelines,
never from an LLM's imagination.** LLMs are only ever allowed to explain or
reason over data that has already been fetched, validated, and fused — they
never select data sources, never invent values, and never bypass evidence
citations.

### The pipeline, in order

1. **Query intake** — a natural language question (e.g. "Will it rain in
   Nagpur tomorrow afternoon and should I spray?") comes in via a FastAPI
   endpoint.

2. **Location + time resolution** — deterministic parsing (gazetteer /
   pincode / lat-lon regex, and keyword-based time-window extraction).
   Ambiguous or unknown locations return structured errors — the system
   never silently defaults to a random city.

3. **Retrieval planning** — a deterministic planner decides *which* data
   sources and variables are needed based on the question (e.g. mentioning
   "spray" pulls in precipitation + warnings + ensemble data). **An LLM is
   never allowed to choose data sources** — this is a hard architectural
   rule, enforced in code, not just documented.

4. **Multi-source retrieval (the "multiple weather APIs" part)** —
   concurrent fetches from several providers, each isolated so one dead
   source doesn't break the whole request:
   - **Open-Meteo** (forecast, historical/ERA5 reanalysis, ensemble/GEFS)
   - **NASA POWER**
   - **IMD** (India Meteorological Department — official but needs a real
     API key)
   - **CAP** (Common Alerting Protocol — official warning feeds)
   - **GFS/GRIB2** (currently a stub/unreachable — real grid data, not yet
     wired to a live source)

5. **Normalization into Canonical Evidence Objects (CEOs)** — every raw API
   response, regardless of source, is decoded into a strictly-typed object
   with: the value, its unit, its variable type (precipitation amount vs.
   rate vs. probability — never mixed), its time window, its spatial
   resolution, and a **provenance trail** (exactly what transformations
   were applied to get from raw API response to this number). Nothing enters
   the system as an untyped blob.

6. **Semantic validation** — CEOs are checked for internal consistency
   (correct units for their statistic type, valid accumulation windows,
   etc.) before being trusted.

7. **Temporal + spatial alignment** — evidence outside the query's
   requested time window or too far from the queried location is filtered
   out.

8. **Ranking + fusion** — when multiple sources report the *same* variable
   (e.g. Open-Meteo says 25mm rain, another source says 40mm), they are
   **never averaged**. Instead, each is scored by a weighted formula
   (source authority × freshness × spatial proximity × data quality), the
   highest-ranked value is surfaced as the "answer," but **every competing
   value remains fully visible and independently auditable** in the
   response. If sources disagree beyond a threshold, this is explicitly
   flagged, not hidden.

9. **Weather Intelligence Object (WIO) construction** — the fused, ranked,
   provenance-preserving result. This is the *single object* that
   everything downstream (agents, decision engine, LLM explanation) is
   allowed to read from. Raw evidence never leaks past this point except
   for audit/citation purposes.

10. **Multi-agent layer** — a set of specialized functions (forecast agent,
    warning agent, historical agent, observation agent, context agent,
    decision agent, reviewer agent, explanation agent) each derive a
    specific "claim" about the WIO. Currently most of these are
    deterministic Python, not LLM calls — **this is one of the areas I'm
    meant to actually build out**: wiring real LLMs (via Groq, running
    multiple models — e.g. qwen3.8-27b, qwen3.6-27b, gpt-oss-20b,
    gpt-oss-120b — routed per agent role) into the agents that should
    reason in natural language, while keeping the reviewer agent as a hard
    gate that rejects any generated output referencing evidence that
    doesn't actually exist in the WIO.

11. **RADE — Risk-Aware Decision Engine** — for decision-oriented questions
    ("should I spray," "should I irrigate," "should I travel," etc.), an
    expected-utility calculation runs: scenarios are generated from the
    WIO's rain probability, each candidate action is scored via
    `expected_utility − risk_aversion × downside_risk`, and the
    highest-scoring action is recommended, with full transparency into the
    math and evidence used. If evidence is insufficient, it explicitly
    defers rather than guessing.

12. **Response synthesis** — currently template-based prose assembly from
    the structured objects above. Part of my job is to properly wire an
    LLM explanation step here that generates natural, farmer-facing (or
    general-purpose) prose *strictly grounded* in the WIO, with a
    deterministic template fallback if the LLM call fails or no API key is
    configured.

### The ML model (separate repo now — see `model.md`)

M1 (semantic classifier) and M3 (intent parser) were already deleted with
no validated dataset ever existing for them (see `CLAUDE.md`). The one
remaining model — bias correction between a GFS-class forecast and ERA5
reanalysis, informally still called "M3" from its later, unrelated training
script — no longer lives in this repo at all. Its dataset construction,
feature engineering, architecture attempts, and real validated baseline
results (LightGBM/ridge vs. no-correction, on a real 24,960-row dataset)
were all moved to `model.md` at the repo root as a handoff document, and
model training now continues in a separate repo. This repo's only remaining
responsibility for it is `app/services/model_client.py` — the HTTP client
consuming whatever that repo eventually publishes as an API.

---

## What "the ML part of my project" concretely means for scope

When I say I only care about the ML part, I mean the **union** of:

1. **Data engineering** — building reliable, versioned datasets from
   Open-Meteo / ERA5 / GFS / IMD / NASA POWER / CAP for training M1/M2/M3,
   with proper train/val/test splits (chronological, not random, for
   anything time-series-like).
2. **Model training** — on Kaggle (2× T4 GPU), with proper hyperparameter
   configs, tracked loss curves (logged + charted as images), and
   fully resumable checkpointing (so a Kaggle session timing out doesn't
   lose progress) — auto-uploading checkpoints/weights to a Hugging Face
   repo as training progresses.
3. **The LLM orchestration/agent layer** — wiring the multi-model Groq
   pipeline into the agent functions that are currently deterministic
   stand-ins, with strict grounding to the WIO and reviewer-enforced
   evidence citation.
4. **The RADE decision engine** — consolidating the two currently-parallel
   implementations (an older v1 that's silently discarded, and the live v2)
   into one correct, well-tested engine.
5. **The CEO → WIO fusion pipeline** — the deterministic evidence
   normalization/ranking/disagreement-detection logic that everything above
   depends on. I own keeping this correct and extending it (e.g. wiring in
   the currently-unused stricter `are_comparable` semantic check).
6. **Inference serving** — packaging all of the above (deterministic
   pipeline + trained ML models loaded from HF + LLM agent calls) behind a
   single FastAPI inference endpoint that can be spun up easily (Docker /
   HF Inference Endpoint / similar), for someone else's app (Android or
   otherwise) to call. I am the API provider, not the API consumer's app
   builder.

**Explicitly out of scope, permanently:** Android client code, any
dedicated frontend, voice/TTS/STT, non-inference backend features (user
accounts, notifications, etc.), and CI/CD or release engineering beyond
basic GitHub version control of the code I write.

---

## Known inherited issues to fix (from repo audit), relevant to my scope

- Root-level docs (`architecture.md`, `implementation.md`, `report.md`) were
  stale and described features that don't exist (Redis, Android, voice UI) —
  deleted in a later cleanup pass rather than treated as spec.
- Groq/LLM integration exists as dead code (`groq_client.py` is never
  called from the live request path) — needs to be wired in per the
  boundary described above.
- Two parallel RADE implementations exist (v1 discarded-but-computed inside
  the agent layer, v2 the actual client-facing decision) — v1 should be
  removed once the agent layer is updated to call v2.
- ML training (`training/`, `kaggle_kernel_m3/`) has since moved to a
  separate repo entirely — see `model.md` for the dataset methodology, real
  validated results so far, and open issues (including that inherited
  training runs had methodology problems: wrong validation target for M2,
  non-held-out splits for M1/M3 — worth redoing properly in the new repo,
  not inheriting the old numbers).

---

## MLOps + Inference Hosting Scope (AWS EC2)

This is part of my ownership too — not "DevOps for the app," but the
minimum operational layer needed to serve the orchestration API reliably.
Scope is deliberately kept to **basic/free-tier adjacent EC2 instance
types** — this is a SIH project, not a production SaaS, so
cost-consciousness matters. Training-side MLOps (experiment tracking,
checkpointing, Hugging Face publishing) is no longer this repo's concern —
it belongs to the separate model repo now (see `model.md`).

### Inference-side MLOps (serving on AWS EC2)

**Goal:** a single FastAPI process, containerized, that runs the
deterministic CEO→WIO pipeline, calls out to Groq for the LLM agent layer
and to an external model API for bias correction (both plain HTTPS calls —
no local model weights loaded at all), and exposes the existing REST
endpoints (`/query`, `/wio/query`, `/decision`, etc.) — hosted on a basic
EC2 instance, not a fleet, not Kubernetes. No local ML runtime means this
is lighter than originally scoped.

1. **Instance choice** — a **t3.micro or t3.small** (or the `t2.micro`
   free-tier instance if within eligibility) is enough since the process is
   pure I/O (weather APIs + Groq + the external model API), no local model,
   no GPU. Move to `t3.medium`/`t3.large` only if request latency under
   real load demands more vCPU/RAM headroom.
2. **Containerization** — the repo already has a `Dockerfile` /
   `docker-compose.yml`; the EC2 box just needs Docker installed and the
   image built/pulled and run — no need for a custom AMI or golden image
   unless you want faster cold starts later.
3. **Secrets** — `GROQ_API_KEY`, `IMD_API_KEY`, `MODEL_API_URL`,
   `MODEL_API_KEY`, etc. live in a `.env` file on the instance (not
   committed) or in AWS Systems Manager Parameter Store if you want them
   out of the filesystem entirely — basic is fine for now given the SIH
   scope.
4. **Process management** — run the container with `docker-compose up -d`
   plus `restart: unless-stopped` in the compose file, so the API survives
   an instance reboot without manual intervention. A process manager beyond
   Docker's own restart policy (e.g. systemd wrapping docker-compose) is a
   reasonable but optional add-on, not a hard requirement at this scale.
5. **Reverse proxy / TLS (optional, minimal)** — if you want a real HTTPS
   endpoint instead of a bare `http://<ec2-ip>:8001`, a lightweight Caddy
   or Nginx container in front handling Let's Encrypt is enough — this is
   the only "infra" beyond the orchestration box itself that's in scope,
   and only if you actually need HTTPS (e.g. for the Android app's HTTP
   client to accept the endpoint).
6. **Basic monitoring** — the existing `/health` and `/metrics` endpoints
   are enough to start; a simple uptime check (even a cron `curl` + email,
   or AWS CloudWatch's basic EC2/instance metrics) covers "is it alive"
   without needing a full observability stack.
7. **Cost control** — since this is likely intermittently used (demo/judging
   periods), consider stopping the instance when not in active use rather
   than running 24/7, or using an EC2 auto-stop schedule — basic-tier
   instances are cheap but not free indefinitely.

**Explicitly out of scope here too:** load balancers, auto-scaling groups,
multi-region deployment, managed Kubernetes (EKS), or any infra beyond
"one reliable box serving one container." If load ever demands it, that's
a future problem, not a SIH-stage one.
