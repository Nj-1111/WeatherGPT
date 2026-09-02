# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Ownership boundary — read this first

The developer working in this repo owns **only the ML / data / training / inference-serving portions**. This is a hard scope boundary, not a preference:

**In scope:** the data pipeline (Open-Meteo/ERA5/GFS/IMD/NASA POWER/CAP retrieval), the CEO→WIO fusion pipeline (`app/services/`, `app/schemas/ceo.py`, `app/schemas/wio.py`), the ML models (`training/` — M1 semantic classifier, M2 bias-correction, M3 intent parser), the LLM/agent orchestration layer (`app/agents/`, `app/orchestrator/`), the RADE decision engine (`app/rade/`), and the FastAPI inference-serving surface (`app/main.py`) as an API provider — plus the MLOps around all of that (Kaggle training, checkpointing, Hugging Face model hosting, basic EC2 inference deployment).

**Permanently out of scope — do not propose or generate code for these unless explicitly asked:** any Android/mobile client, any dedicated frontend/UI, general backend/product engineering (auth, user accounts, billing, non-inference API surface), voice/TTS/STT, Nginx/Vercel/HF-Space *website* deployment, and CI/CD or release engineering beyond basic version control.

## Commands

**Environment setup** (no committed venv/lockfile):
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-api.txt      # API dev/test only — no torch, boots backend + runs pytest
pip install -r requirements.txt          # full: ML/training stack (torch, transformers, lightgbm, huggingface_hub, ...)
pip install -r requirements-full.txt     # adds GRIB2 decoding (cfgrib/eccodes/xarray) on top of requirements.txt
```
Three files, not four — `requirements-kaggle.txt` was deleted (unreferenced by any code/doc/Dockerfile, and the actual Kaggle workflow in `docs/KAGGLE_TRAINING_GUIDE.md` doesn't use it; Kaggle's base image already has everything `kaggle_kernel_m2/train_m2.py` needs except `huggingface_hub`, installed inline in the notebook). `requirements-full.txt` only adds the 3 packages `grib2_adapter.py`/`grib2_placeholder.py` actually import (`cfgrib`, `eccodes`, `xarray`) — it used to also list `netCDF4`/`h5py`/`pyproj`/`paho-mqtt`/`pillow` for NetCDF/HDF5/WIS2-MQTT/image features that were never built; removed as dead install weight, not real optionality.

`requirements.txt`'s `torch` pin is a flexible range (`>=2.3,<3`), not an exact version — an exact pin (`==2.3.1`) has no wheel for Python 3.12+ and hard-fails `pip install` entirely on newer systems (hit and fixed this session on Python 3.14). If `pip` itself is missing and you can't get sudo/`apt install python3-venv` in a sandboxed environment: `curl -sS https://bootstrap.pypa.io/get-pip.py | python3 - --user --break-system-packages`, then `pip install --user --break-system-packages <packages>`. The live API never imports `torch` at request time (see Architecture below), so `requirements-api.txt` is enough to run the app and test suite.

**Run the API:**
```bash
uvicorn app.main:app --host 0.0.0.0 --port 8001
```

**Tests:**
```bash
pytest -q                                # full suite (20 tests as of last verified run)
pytest tests/test_ceo.py::test_comparable_gate   # single test
```
`testpaths=["tests"]` and `pythonpath=["."]` are set in `pyproject.toml`, so `pytest` runs correctly from repo root without extra flags.

**Lint / type-check** (configured in `pyproject.toml`, not currently run in any CI):
```bash
ruff check .        # line-length 100, target py310
mypy app             # ignore_missing_imports = true
```

**Training** (see Architecture → Training below before running — several scripts intentionally refuse to run without real data):
```bash
python training/train_bias_correction.py --model mlp --epochs 20 --batch-size 256 --device auto
python training/train_semantic_classifier.py --epochs 5 --batch-size 32 --device auto
python training/train_intent_parser.py --epochs 3 --batch-size 32 --device auto
```
Actual GPU training runs happen on Kaggle (2×T4), via `kaggle_kernel_official/official_train.py` — not in a local/sandbox environment.

**Docker:**
```bash
docker compose up --build   # python:3.10-slim, exposes :8001, mounts ./training/models
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

### Training (`training/`)

Three scripts (`train_semantic_classifier.py`=M1, `train_bias_correction.py`=M2, `train_intent_parser.py`=M3), each config-driven from `training/configs/*.yaml` to varying degrees (M2's config is fully wired; M1/M3 have some YAML fields — `weight_decay`, `seed`, `intent.yaml`'s BIO label list — that are defined but not actually read by the script). **M1 and M3 intentionally raise `RuntimeError` and refuse to train** if their required real dataset isn't present (`training/datasets/field_names.csv`, `training/datasets/intent_samples.jsonl`) — they do not silently fall back to synthetic data for a real run; the synthetic generators only run under `--dry-run`. M2 requires an explicit `--allow-synthetic-development` flag to use synthetic data at all, and prints a "DEVELOPMENT ONLY" warning when it does.

`training/datasets/` and `training/models/` are empty (`.gitkeep` only) — no real dataset or validated weights are currently committed. Trained artifacts under `training/models/` also have **no consumer in `app/`** yet — `/health` explicitly reports `"models": {"runtime_loading": "rule-based fallback only; artifacts are not trusted until registry validation"}`.

`kaggle_kernel_official/official_train.py` is a substantially rewritten, self-contained single-file version of all three training scripts (different/deeper architectures, live HTTP dataset generation inline, Groq-based data augmentation for M3, forced CPU due to actual Kaggle P100/sm_60 incompatibility) — it is not a fork of the local scripts and ignores `training/configs/*.yaml` entirely, hardcoding its own hyperparameters. It's the thing that actually runs on Kaggle.

### Known state, don't assume otherwise

- Root-level `architecture.md`, `implementation.md`, `report.md`, `setup.md`, `INSTALL.md`, and ten dated `docs/*_2026-09-01.md`/planning docs described an earlier/aspirational system built by a previous developer (a different machine path, a different Kaggle account, a fully-live Groq multi-agent pipeline, nonexistent endpoints like `GET /plan`, self-reported metrics later found unverified, and — in `report.md` — a partially-visible API key fragment) that did not match current code. Deleted as stale in this session; `README.md` and `docs/ARCHITECTURE.md`/`docs/API.md`/`docs/VERIFICATION.md` remain the accurate source of truth.
- No ML metric currently in this repo is independently validated — `docs/VERIFICATION.md` says so explicitly for the Kaggle-hosted runs.
- `kaggle_kernel/` is a frozen, byte-identical snapshot of an earlier `app/` (pre-RADE-v2). It is not kept in sync with `app/` — treat changes to `app/` as not automatically applying there.
