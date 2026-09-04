#!/usr/bin/env python3
"""Latency harness: cold and warm request timings, plus the compute-only stage.

Cold timings are dominated by two upstream calls and vary widely run to run, so a single
sample proves nothing — this reports a median over several distinct locations. The warm
path and the orchestration stage make no network calls and are the numbers that actually
detect a regression in our own code.

    python scripts/bench.py                      # in-process ASGI, no server needed
    python scripts/bench.py --url http://localhost:8001
    python scripts/bench.py --warm-runs 20 --json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import statistics
import sys
import time
from pathlib import Path
from typing import Any

import httpx

# Run directly (`python scripts/bench.py`) as well as via `-m`; pyproject's pythonpath
# setting applies to pytest only.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

COLD_LOCATIONS = ("Nashik", "Guntur", "Karnal", "Bhopal", "Udaipur")
WARM_QUESTION = "will it rain in Indore tomorrow"


async def _timed(client: httpx.AsyncClient, question: str) -> tuple[int, float]:
    started = time.monotonic()
    response = await client.post("/query", json={"question": question})
    return response.status_code, (time.monotonic() - started) * 1000


async def _stage_only(runs: int) -> float | None:
    """Time run_all_agents alone — no network anywhere in it, so it isolates our code."""
    try:
        from datetime import datetime, timezone

        from app.agents.orchestrator import run_all_agents
        from app.services.evidence_store import evidence_store
        from app.services.wio_builder import build_wio
    except ImportError:
        return None
    ceos = [item for _, item in evidence_store._items.values()]
    if not ceos:
        return None
    now = datetime.now(timezone.utc)
    location = {"lat": 22.72, "lon": 75.86}
    timings = []
    for _ in range(runs):
        started = time.monotonic()
        wio = build_wio("bench", location, now, now, "short", ceos)
        await run_all_agents(ceos, wio, {}, "en", None)
        timings.append((time.monotonic() - started) * 1000)
    return statistics.median(timings)


async def run(url: str | None, warm_runs: int, cold_count: int) -> dict[str, Any]:
    if url:
        transport, base = None, url.rstrip("/")
    else:
        from app.main import app
        transport, base = httpx.ASGITransport(app=app), "http://bench"

    async with httpx.AsyncClient(transport=transport, base_url=base, timeout=120) as client:
        cold = []
        for location in COLD_LOCATIONS[:cold_count]:
            status, ms = await _timed(client, f"will it rain in {location} tomorrow")
            cold.append({"location": location, "ms": round(ms, 1), "status": status})

        status, first = await _timed(client, WARM_QUESTION)
        warm = []
        for _ in range(warm_runs):
            _, ms = await _timed(client, WARM_QUESTION)
            warm.append(ms)

    cold_ms = [entry["ms"] for entry in cold if entry["status"] == 200]
    result: dict[str, Any] = {
        "cold": {"samples": cold,
                 "median_ms": round(statistics.median(cold_ms), 1) if cold_ms else None,
                 "min_ms": round(min(cold_ms), 1) if cold_ms else None,
                 "max_ms": round(max(cold_ms), 1) if cold_ms else None},
        "warm": {"runs": warm_runs, "first_ms": round(first, 1),
                 "median_ms": round(statistics.median(warm), 2),
                 "min_ms": round(min(warm), 2), "max_ms": round(max(warm), 2)},
    }
    if not url:
        stage = await _stage_only(20)
        result["run_all_agents_median_ms"] = round(stage, 2) if stage else None
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", help="target a running server instead of in-process ASGI")
    parser.add_argument("--warm-runs", type=int, default=10)
    parser.add_argument("--cold-count", type=int, default=3, choices=range(1, len(COLD_LOCATIONS) + 1))
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()

    logging.disable(logging.INFO)
    result = asyncio.run(run(args.url, args.warm_runs, args.cold_count))

    if args.json:
        print(json.dumps(result, indent=2))
        return
    cold, warm = result["cold"], result["warm"]
    print("cold (fresh location, real upstreams — network-dominated, high variance)")
    for sample in cold["samples"]:
        print(f"   {sample['location']:<10} {sample['ms']:>8.0f} ms   [{sample['status']}]")
    print(f"   median {cold['median_ms']} ms   range {cold['min_ms']}–{cold['max_ms']} ms\n")
    print(f"warm (cached, {warm['runs']} runs — no external calls)")
    print(f"   median {warm['median_ms']} ms   range {warm['min_ms']}–{warm['max_ms']} ms")
    if result.get("run_all_agents_median_ms") is not None:
        print(f"\nrun_all_agents  {result['run_all_agents_median_ms']} ms median over 20 runs")


if __name__ == "__main__":
    main()
