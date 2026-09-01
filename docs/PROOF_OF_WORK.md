# Proof of work — 2026-09-02

This document exists to answer one question honestly: does any of this actually work, or is it claims-on-top-of-claims? Every number and status below is either a command you can re-run, a file you can open, or a commit you can diff. Nothing here is asserted without a way to check it.

---

## 1. Agent pipeline correctness — a real, live end-to-end trace

**Claim being tested:** does a natural-language query actually flow through location resolution → multi-source retrieval → evidence fusion → the RADE decision engine → a consistent final answer, against real (not mocked) weather data?

**How to verify it yourself:**
```bash
uvicorn app.main:app --host 127.0.0.1 --port 8001 &
curl -s -X POST http://127.0.0.1:8001/query -H 'content-type: application/json' \
  -d '{"question":"Will it rain in Nagpur tomorrow afternoon and should I spray?","location":{"raw":"Nagpur"}}'
```

**Result** — full response saved at [`docs/proof/agent_trace.json`](proof/agent_trace.json), health snapshot at [`docs/proof/health_snapshot.json`](proof/health_snapshot.json):

| Check | Result |
|---|---|
| Real evidence retrieved | **384** CanonicalEvidenceObjects, from live `OPEN_METEO` and `GEFS` (Open-Meteo ensemble) APIs |
| Top-level `decision.recommended_action` | `reschedule` |
| `agents[]` decision-agent claim | `reschedule` — **matches** the top-level decision (see §3, this used to be two possibly-disagreeing engines) |
| `reviewer` agent status | `success`, zero errors — every claim's cited evidence actually exists |
| `wio.agreement.status` | `full_agreement` |
| Rain data source used | `GEFS`, with real ensemble **member-value scenarios** (`member_values` present) — this is notable: see below |

**This trace also caught and fixed two real, previously-undiscovered bugs** (not induced by anything changed this session — both pre-existed since the original `672748d` commit):

1. **`app/decoders/open_meteo.py`** hardcoded a `times[:48]` cap on decoded forecast hours. Depending on what time of day (UTC) a request is made, "tomorrow afternoon" in IST can fall past hour 48, silently dropping every piece of evidence and turning a normal query into a hard `503 REVIEW_FAILED`. Reproduced first (evidence count went from real data → `0` after the time-window filter), then fixed by removing the artificial cap — `forecast_days` (already computed from the query window in `app/services/retrieval.py`) is what should bound the data, not a second, conflicting hardcoded limit.
2. **`app/adapters/open_meteo_ensemble.py`** built ensemble evidence objects with `variable="precipitation"` — not a valid value in the `CanonicalVariable` enum (only `"precipitation_amount"` is). Every single GEFS precipitation row threw a Pydantic validation error, so the ensemble/member-value source has apparently **never worked** since it was introduced. Fixed by mapping the raw Open-Meteo field name to the canonical enum value.

Before these fixes, the exact query above returned `503 REVIEW_FAILED`. After, it succeeds — and for the first time actually exercises RADE's richer 5-bin ensemble-scenario path (`generate_scenarios` in `app/rade/v2.py`) instead of always falling back to the simpler 2-scenario path, since real member-value data is now reaching it.

`pytest -q`: **20 passed** both before and after these fixes (test suite doesn't happen to cover this specific timezone/ensemble interaction — a gap worth noting, not hiding).

---

## 2. ML pipeline validity — what's independently verified today

**Full training results are pending** — the actual Kaggle GPU run hasn't happened yet (see `docs/KAGGLE_TRAINING_GUIDE.md`; `docs/M2_TRAINING_REPORT.md` will be populated once you run it and share results back). What's verified **right now**, locally, in this session:

- **Real dataset, not synthetic**: `training/build_matched_pairs.py` fetched **24,960 real rows** from live Open-Meteo APIs (GFS forecast vs. ERA5 reanalysis), across 26 India-only locations and 4 seasons (winter/pre-monsoon/monsoon/post-monsoon) in 2024. Zero fetch failures (104/104 point×season combinations succeeded).
- **Split integrity verified**: the chronological tail-15% validation split falls entirely within `2024-10-09` to `2024-10-14` — zero date overlap with the `2024-01-05`–`2024-10-08` training range. Checked directly against the CSV, not assumed.
- **Data loading verified against the real file**: `X.shape=(24960, 6)`, `y.shape=(24960, 2)` — confirmed by actually loading `training/datasets/matched_pairs.csv` through the same logic `training/train_bias_correction.py` uses.
- **A real, working local training run** (CPU, 6 epochs, smoke-test scale, `kaggle_kernel_m2/train_m2.py`): train loss went `2.97 → 2.19`, val loss `2.01 → 1.87`, genuinely learning — not flat/broken. Already beat the naive "no correction" baseline by **15.4%** (temperature) and **12.0%** (precipitation) after just 6 epochs on CPU. A full 30-epoch GPU run should do meaningfully better.
- **Resumability verified**: killed the training process after epoch 3, restarted it, confirmed it resumed at epoch 4 (not epoch 1) with `metrics_history.jsonl` correctly appended, not overwritten.
- **HF upload failure handling verified**: ran with a deliberately invalid token — got a clean `401` caught and logged, training completed normally, nothing crashed.

**Not yet verified**: the actual multi-GPU Kaggle run, and whether the model continues improving past epoch 6 rather than overfitting — that's exactly what the real run in `docs/KAGGLE_TRAINING_GUIDE.md` will settle.

---

## 3. Session work summary

Commits this session (`git log`):
- `8344e2f` — checkpoint: RADE consolidated onto one engine (`app/rade/v2.py`), removed the older `enumerator.py`/`utility.py`/`policy.py` that ran silently in parallel and could disagree with the live decision (verified via a direct test: before the fix, the same query could produce two different recommended actions depending on which engine's output you looked at — `agents[]` vs. the top-level `decision` field); removed the orphaned, never-mounted `app/api/v1/main.py` router and unused `QueryRequest`/`QueryResponse` schemas; added `CLAUDE.md`/`PROJECT_SCOPE.md` for project memory.
- Uncommitted, pending your review: `app/schemas/wio_v2.py` deletion (unreferenced parallel schema), the dead `detect_disagreement` import removal from `app/main.py`, the M2 real-dataset work (§2), and the two bug fixes from §1.

`pytest -q`: **20/20 passing**, confirmed after every change in this list, not just at the end.

What this session did *not* do: wire the LLM (`groq_client.py`) into the live explanation path (still returns empty claims — see `CLAUDE.md`'s "known state" section), or run the actual Kaggle GPU training (that's on you, per `docs/KAGGLE_TRAINING_GUIDE.md`).
