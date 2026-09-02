# WeatherGPT — Bias-Correction Model: Scope, Status, and Integration Contract

## Purpose

Corrects systematic bias between a GFS-class NWP forecast (in the `weathergpt`
orchestration repo, sourced via Open-Meteo's GFS-backed forecast API) and
ERA5 reanalysis, for `temperature_2m` and `precipitation_amount`. The model
predicts the *bias* (forecast minus truth), not the absolute value — the
orchestration repo adds the predicted bias back onto the raw forecast.

## Relationship to the `weathergpt` repo

This model used to be trained and partially scaffolded for local hosting
inside `weathergpt` (`training/`, `kaggle_kernel_m3/`). As of this change,
all training code and the local dataset were removed from `weathergpt` and
this file was written to hand off everything needed to continue the work in
a separate repo. `weathergpt` now contains only a thin HTTP client
(`app/services/model_client.py`) that calls this model as an external API —
it has zero knowledge of architecture, features, or weights. See
"Integration contract" below for exactly what that client sends and expects.

## Problem framing

- Input: a raw GFS-class forecast value at a given lead time and location.
- Target: signed bias = ERA5 reanalysis value − raw forecast value.
- **Ground-truth caveat — carry this forward, don't lose it**: "ground
  truth" here is ERA5 *reanalysis*, not real station observations. No
  validation against actual measured weather has been done at any point.

## Dataset construction (reproduce or extend this methodology)

Built by `training/build_matched_pairs.py` (now deleted from `weathergpt`,
methodology preserved here):

- Source: Open-Meteo `historical-forecast-api` (`models=gfs_seamless`) paired
  against Open-Meteo `archive-api` (ERA5), same lat/lon/time window, hourly.
- 26 fixed India lat/lon points — elevation-diverse general spread plus
  explicit agricultural-belt coverage (wheat/rice/cotton/soy/horticulture),
  since the app's real use case is spray/irrigate/harvest decisions:
  Nagpur 21.14,79.08 · Mumbai 19.07,72.87 · Delhi 28.61,77.20 ·
  Kolkata 22.57,88.36 · Chennai 13.08,80.27 · Bengaluru 12.97,77.59 ·
  Pune 18.52,73.85 · Jaipur 26.91,75.78 · Ahmedabad 23.02,72.57 ·
  Prayagraj 25.43,81.84 · Hyderabad 17.38,78.48 · Dharwad region 15.31,75.12 ·
  Coimbatore 11.01,76.96 · Chandigarh 30.73,76.77 · Srinagar 34.08,74.79 ·
  Bhubaneswar 20.29,85.82 · Guwahati 26.14,91.73 · Raipur 21.25,81.62 ·
  Udaipur 24.58,73.71 · Belagavi region 15.91,75.56 ·
  Ludhiana (wheat/rice) 30.90,75.85 · Amritsar (wheat/rice) 31.63,74.87 ·
  Karnal (wheat) 29.68,76.99 · Indore (soybean/cotton) 22.72,75.86 ·
  Nashik (horticulture) 19.99,73.79 · Guntur (cotton/chili) 16.30,80.44.
- 4 season windows, 10 days each, all in 2024: winter (Jan 5–14),
  pre-monsoon (Apr 5–14), monsoon (Jul 5–14), post-monsoon (Oct 5–14).
- Elevation fetched *live* from the GFS API response per point — never
  hardcoded (a wrong hardcoded elevation would silently corrupt a feature).
- Result: **24,960 real hourly rows**, zero fetch failures (104/104
  point×season combinations succeeded), sorted chronologically by
  `valid_from` (important: whatever split logic you use, sorting matters —
  the original split took the tail 15% of *rows* as validation and relied
  on this sort to land that tail entirely within the post-monsoon window,
  giving a genuine out-of-season holdout rather than an arbitrary slice).
- CSV columns: `valid_from, lat, lon, elevation_m, lead_hours, gfs_t2m_k,
  gfs_apcp_mm, obs_t2m_c, obs_apcp_mm` (`obs_*` = ERA5 values — not real
  station observations, see caveat above).

## Feature engineering used so far

Raw (6): `gfs_t2m_k` (Kelvin), `gfs_apcp_mm`, `elevation_m`, `lead_hours`,
`lat`, `lon`. Engineered/cyclical (+6, 12 total): `lon_sin`/`lon_cos`
(deg→rad), `lead_sin`/`lead_cos` (`sin/cos(2π·lead_hours/72)` — 72h GFS
reforecast cycle), `doy_sin`/`doy_cos` (`sin/cos(2π·day_of_year/365.25)`).
Targets: `y_temp = obs_t2m_c − (gfs_t2m_k − 273.15)` (°C),
`y_precip = obs_apcp_mm − gfs_apcp_mm` (mm). Features standardized via
`sklearn.StandardScaler` fit on the training split only.

## Split strategy used so far

Group-aware: 4 locations held out **entirely** (never seen in training —
the "spatial holdout," the number that reflects real deployment to new
locations). Remaining locations split chronologically, last 15% of rows =
temporal validation. Seed 42.

## Real validated results so far (`training/baseline_models.py`, actual run — not estimated)

Same 24,960-row dataset, identical splits across every model below:

| Model | val RMSE temp (°C) | **spatial-holdout** RMSE temp (°C) | val RMSE precip (mm) | **spatial-holdout** RMSE precip (mm) |
|---|---|---|---|---|
| No correction (raw GFS) | 2.208 | 2.296 | 0.753 | 0.870 |
| Constant (train-mean bias) | 1.983 | 1.786 | 0.757 | 0.872 |
| Ridge (linear) | 1.932 | 1.750 | 0.637 | **0.757** |
| **LightGBM** | **1.600** | **1.605** | 0.681 | 0.761 |

Spatial holdout is the number that matters for real deployment. Takeaways:
- LightGBM clearly wins on **temperature** (~30% better than no-correction,
  meaningfully ahead of ridge).
- On **precipitation**, ridge and LightGBM are statistically close on the
  spatial holdout (0.757 vs 0.761) — **LightGBM does not clearly beat the
  linear baseline for precip**, unlike for temp. Worth investigating rather
  than assuming LightGBM is unconditionally the right architecture for both
  targets.
- LightGBM feature importance, temperature: `gfs_t2m_k` 0.325, `lead_hours`
  0.283, `lat` 0.138, `elevation_m` 0.115, `lon` 0.083, `gfs_apcp_mm` 0.056.
  Precipitation: `gfs_apcp_mm` 0.243, `lead_hours` 0.235, `gfs_t2m_k` 0.216,
  `elevation_m` 0.107, `lon` 0.102, `lat` 0.096.
- A residual MLP was also built (architecture below) but **never completed
  a real GPU training run** — only CPU-smoke-tested 2–4 epochs. It has **no
  validated metric comparable to the table above.** Don't treat any MLP
  number as validated until a real run happens and this table is updated.

## MLP architecture attempted — a starting point, not a mandate

`in_dim=12, width=128, depth=4` pre-norm residual blocks (LayerNorm, not
BatchNorm — batch stats judged too noisy at bs=256 on a 12-dim input), GELU,
dropout 0.15. Two heads: `head_temp` (1 output, Huber loss β=1.0);
`head_precip` (2 outputs — `[bias_pred, wet_logit]`; `wet_logit` is a
train-time-only auxiliary BCE regularizer against "is this a wet event,"
**never used at inference** — only `bias_pred` is the real precip
correction). EMA of weights (decay 0.999) used for eval/export. AdamW,
lr 3e-3, weight_decay 0.01, cosine schedule with 3-epoch warmup, 60 epochs,
batch size 512/GPU, grad clip norm 1.0. Composite eval score =
`0.6·spatial_rmse_temp + 0.4·spatial_rmse_precip` (optimizes for the
unseen-location number). DDP-ready, bf16 autocast + `torch.compile` on GPU.
None of this is prescriptive — free to keep, discard, or replace; documented
so prior work isn't silently lost.

## Known gaps / open decisions for the new repo

1. Ground truth is ERA5 reanalysis, not real station observations — no
   real-world validation has been done.
2. **`elevation_m` has no live source in the `weathergpt` orchestration
   repo.** The dataset-build script fetched it from Open-Meteo per training
   point, but the live orchestration repo has no equivalent lookup —
   real API requests from `weathergpt` will send `elevation_m=0.0` unless
   `weathergpt` adds a lookup later, or this model's API does its own
   lat/lon→elevation lookup server-side.
3. Real GPU training for the MLP never happened — that's the actual next
   step for that path, not incremental tuning.
4. LightGBM's precipitation edge over ridge is marginal on the spatial
   holdout — worth investigating (more data? different features? a
   precip-specific model?) before assuming LightGBM is unconditionally best.
5. Dataset is India-only, 26 fixed points, single year (2024) — no
   multi-year variability, no locations outside India.

## Integration contract — what `weathergpt` expects from this model as an external API

Implement an HTTP service satisfying this; internal architecture, features,
and weights are entirely this repo's business — `weathergpt` sends only raw
physical quantities and expects bias values back.

**`POST /v1/correct`** — batched (one call per `/query`, covering a whole
forecast horizon for one location — not one call per hour):

Request:
```json
{
  "instances": [
    {
      "gfs_t2m_k": 301.2,
      "gfs_apcp_mm": 0.4,
      "elevation_m": 0.0,
      "lead_hours": 18,
      "lat": 21.14,
      "lon": 79.08,
      "valid_from": "2026-09-03T06:00:00Z"
    }
  ]
}
```

Response:
```json
{
  "model_version": "m3-<version>",
  "corrections": [
    {"temp_bias_c": -1.3, "precip_bias_mm": 0.2}
  ]
}
```

`corrections` must be the same length and order as `instances`.
`weathergpt` applies: `corrected_temp_c = (gfs_t2m_k − 273.15) + temp_bias_c`,
`corrected_precip_mm = max(0.0, gfs_apcp_mm + precip_bias_mm)`.

**`GET /health`** — plain 200 (e.g. `{"status": "ok"}`), used by
`weathergpt`'s circuit breaker to decide whether to keep sending traffic.

**Auth**: `weathergpt` sends `Authorization: Bearer <MODEL_API_KEY>` when
`MODEL_API_KEY` is configured — implement bearer-token auth to match.

**Failure behavior `weathergpt` assumes**: any non-200, timeout, malformed
JSON, or missing `corrections` array is treated as "correction unavailable"
— the raw uncorrected forecast is used instead, nothing breaks downstream.
Fail loudly in your own logs; `weathergpt` will never surface a 5xx from
this API directly to an end user.

**Latency expectation**: `weathergpt` applies an outer timeout (default 8s,
configurable) plus circuit-breaking after repeated failures. Aim for well
under 1s per batched call at whatever batch size a typical forecast horizon
needs (~72 hourly points for a 3-day forecast).
