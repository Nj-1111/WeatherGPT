"""
Build training/datasets/matched_pairs.csv — GFS forecast vs ERA5 reanalysis,
paired by (lat, lon, valid_from), sampled across four seasons so the
bias-correction model sees winter/pre-monsoon/monsoon/post-monsoon patterns,
not just one arbitrary window.

Ground truth is ERA5 reanalysis, not real station observations. Columns
are named obs_* to match what kaggle_kernel_m2/train_m2.py's load_data()
expects.

Elevation is fetched live from the GFS API response for every point — never
hardcoded — since a wrong hardcoded elevation would silently corrupt one of
the model's five (now six, with lon) input features.

Usage:
    python training/build_matched_pairs.py
"""
from __future__ import annotations

import asyncio
import csv
import time
from pathlib import Path

import httpx

GFS_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
ERA5_URL = "https://archive-api.open-meteo.com/v1/era5"

# (lat, lon) only — elevation is fetched live from the API, never hardcoded.
# First 20 points are an elevation-diverse spread across India; the last 6
# add agricultural-belt coverage (wheat/rice/cotton/soy/horticulture regions),
# since spray/irrigate/harvest decisions are this app's actual use case.
POINTS: list[tuple[float, float, str]] = [
    (21.14, 79.08, "Nagpur"),
    (19.07, 72.87, "Mumbai"),
    (28.61, 77.20, "Delhi"),
    (22.57, 88.36, "Kolkata"),
    (13.08, 80.27, "Chennai"),
    (12.97, 77.59, "Bengaluru"),
    (18.52, 73.85, "Pune"),
    (26.91, 75.78, "Jaipur"),
    (23.02, 72.57, "Ahmedabad"),
    (25.43, 81.84, "Prayagraj"),
    (17.38, 78.48, "Hyderabad"),
    (15.31, 75.12, "Dharwad region"),
    (11.01, 76.96, "Coimbatore"),
    (30.73, 76.77, "Chandigarh"),
    (34.08, 74.79, "Srinagar"),
    (20.29, 85.82, "Bhubaneswar"),
    (26.14, 91.73, "Guwahati"),
    (21.25, 81.62, "Raipur"),
    (24.58, 73.71, "Udaipur"),
    (15.91, 75.56, "Belagavi region"),
    (30.90, 75.85, "Ludhiana — wheat/rice belt"),
    (31.63, 74.87, "Amritsar — wheat/rice belt"),
    (29.68, 76.99, "Karnal — wheat belt"),
    (22.72, 75.86, "Indore — soybean/cotton belt"),
    (19.99, 73.79, "Nashik — horticulture"),
    (16.30, 80.44, "Guntur — cotton/chili belt"),
]

# 10-day windows spread across all four seasons in 2024 (recent enough for
# complete ERA5 coverage, far enough in the past to avoid any archive lag).
SEASON_WINDOWS = [
    ("2024-01-05", "2024-01-14", "winter"),
    ("2024-04-05", "2024-04-14", "pre-monsoon"),
    ("2024-07-05", "2024-07-14", "monsoon"),
    ("2024-10-05", "2024-10-14", "post-monsoon"),
]

OUT_PATH = Path("training/datasets/matched_pairs.csv")


async def fetch_window(client: httpx.AsyncClient, lat: float, lon: float, start: str, end: str):
    gfs_resp = await client.get(GFS_URL, params={
        "latitude": lat, "longitude": lon, "start_date": start, "end_date": end,
        "hourly": "temperature_2m,precipitation", "models": "gfs_seamless", "timezone": "UTC",
    })
    era5_resp = await client.get(ERA5_URL, params={
        "latitude": lat, "longitude": lon, "start_date": start, "end_date": end,
        "hourly": "temperature_2m,precipitation", "timezone": "UTC",
    })
    gfs_resp.raise_for_status()
    era5_resp.raise_for_status()
    gfs = gfs_resp.json()
    era5 = era5_resp.json()
    return gfs.get("elevation"), gfs["hourly"], era5["hourly"]


async def build() -> None:
    rows: list[tuple] = []
    failures: list[tuple] = []
    async with httpx.AsyncClient(timeout=40) as client:
        for lat, lon, name in POINTS:
            for start, end, season in SEASON_WINDOWS:
                try:
                    elevation, gfs_hourly, era5_hourly = await fetch_window(client, lat, lon, start, end)
                except Exception as exc:
                    failures.append((name, lat, lon, season, str(exc)))
                    print(f"[matched_pairs] {name} ({lat},{lon}) {season} FAILED: {exc}")
                    continue

                times = gfs_hourly["time"]
                kept = 0
                for i, valid_from in enumerate(times):
                    gfs_t = gfs_hourly["temperature_2m"][i]
                    gfs_p = gfs_hourly["precipitation"][i]
                    obs_t = era5_hourly["temperature_2m"][i] if i < len(era5_hourly["temperature_2m"]) else None
                    obs_p = era5_hourly["precipitation"][i] if i < len(era5_hourly["precipitation"]) else None
                    if gfs_t is None or obs_t is None:
                        continue
                    lead_hours = i % 72  # resets each 10-day window; GFS 0-71h reforecast cycle
                    rows.append((
                        valid_from, lat, lon, elevation or 0.0, lead_hours,
                        gfs_t + 273.15, gfs_p or 0.0, obs_t, obs_p or 0.0,
                    ))
                    kept += 1
                print(f"[matched_pairs] {name} ({lat},{lon}) {season}: {kept}/{len(times)} hours kept")
                time.sleep(0.12)  # be polite to the free API

    # Sort chronologically. train_m2.py's three_way_split() takes the tail
    # 15% of *rows* as validation and trusts the file is time-sorted — with this
    # row count and four season blocks, a chronological sort makes the tail land
    # entirely within the last (post-monsoon) window: a genuine out-of-season
    # holdout, not an arbitrary row slice.
    rows.sort(key=lambda r: r[0])

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_PATH, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["valid_from", "lat", "lon", "elevation_m", "lead_hours",
                    "gfs_t2m_k", "gfs_apcp_mm", "obs_t2m_c", "obs_apcp_mm"])
        w.writerows(rows)

    print(f"\n[matched_pairs] wrote {len(rows)} rows to {OUT_PATH}")
    print(f"[matched_pairs] {len(POINTS)} points x {len(SEASON_WINDOWS)} seasons "
          f"({', '.join(s for _, _, s in SEASON_WINDOWS)})")
    if failures:
        print(f"[matched_pairs] {len(failures)} point/window fetches failed:")
        for name, lat, lon, season, err in failures:
            print(f"  - {name} ({lat},{lon}) {season}: {err}")
    if len(rows) < 4000:
        print("[matched_pairs] WARNING: fewer than 4000 rows — downstream training scripts "
              "may fall back to a random split instead of a chronological holdout.")


if __name__ == "__main__":
    asyncio.run(build())
