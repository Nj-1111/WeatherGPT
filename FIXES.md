# FIXES.md — what's left, compact

Current-status index across security, correctness, and roadmap. Supersedes and removes
`docs/security-audit-2026-09-03.md` (fully merged below). Deep-dive narrative and
reproduction steps for each item still live in `BUG.md` (defect register) and `AUDIT.md`
(2026-09-05 teardown) — this file is the short "what's open" list, not a replacement for
that detail. Last updated 2026-09-08 (input-pipeline consolidation + session-aware-guardrail round).

## Security — 8 of 11 closed

| # | Issue | Status |
|---|---|---|
| §2.1 | Unauthenticated `user_id`/`session_id` (IDOR) | closed |
| §2.2 | Guardrail LLM output fields had no `max_length` | closed 2026-09-07 |
| §2.3 | Nominatim throttle is a shared cross-tenant lock | **open** — scoped, declined (private VPC, no proxy in front) |
| §2.4 | Unrecognized claim `derivation.op` degrades to a warning, not an error | **open** |
| §2.5 | Prose-grounding regex missed mph/inches/°F | closed 2026-09-07 |
| §2.6 | Request-size cap trusted `Content-Length` | closed 2026-09-07 |
| §2.7 | No per-user cap on stored context facts | closed 2026-09-07 |
| §2.8 | Synchronous SQLite calls block the event loop | **open** |
| §2.9 | `/health` unauthenticated 8-way fan-out | closed |
| §2.10 | CAP feed SSRF/XXE surface | closed 2026-09-07 |
| §2.11 | Raw exception text leaked into `retrieval_status` | closed 2026-09-07 |

## Correctness bugs — open (BUG.md; F1-F6/B1/B4/B5/B21 already closed there)

| ID | Sev | Issue |
|---|---|---|
| B2 | P1 | Pure Devanagari rejected outright when the LLM is down — deterministic fallback has no Hindi-script keywords |
| B3 | P1 | LLM guardrail inconsistent on Indic queries (6/15 failed in a live batch) — prompt behavior, needs a live-harness re-run |
| B6 | P2 | `CLARIFY(no_location)` unreachable in practice — "will it rain tomorrow" hits a raw 422 instead |
| B7 | P2 | General class: same intent, different phrasing → different guardrail action (specific reported case fixed, class still open) |
| B18 | P2 | Reviewer's 503 diagnostic echoes both the fabricated and re-derived value to the client |
| B19 | P3 | `REVIEW_FAILED` is the only 503 today; nothing stops a future one from being indistinguishable |
| B20 | P3 | `.dockerignore`/`.env`-in-image half closed 2026-09-08; `pytest` still installs into the runtime image (dependency-split not done) |
| — | P2 | **Mitigated, not eliminated 2026-09-08**: an unpaced burst can exhaust the Groq primary tier then cascade-exhaust the Gemini fallback too. A 3rd `small`-tier endpoint (OpenRouter, `SMALL_LLM_FALLBACK_2_*`) now gives a burst 3 quotas to exhaust instead of 2, but that endpoint itself showed transient 502s live — accepted for prototype scale (~10-15 concurrent), not production-hardened. See `BUG.md` B21's note. |

## AUDIT.md teardown — open (A1-A6, A10 already closed there)

| ID | Issue |
|---|---|
| A7 | `query_understanding_confidence_threshold` has zero readers; its comment claims it gates something it doesn't |
| A8 | Rain panel reports the window's peak probability but cites `probabilities[0]` — value and citation disagree |
| A9 | Reviewer 503s pooled with all 5xx in metrics; `wio_latency_ms_mean` divides by the wrong count |
| — | `retrieve()`'s `asyncio.gather` has no total deadline — one slow source still stalls the whole request |
| — | Explanation LLM call (~0.7-1.6s) runs on every request, uncached |

## Dead code / hygiene

- ~~`app.storage.session_store`~~ — **removed 2026-09-08**: confirmed genuinely unused
  (imported nowhere, not even in tests) and removed along with its `build_session_store()`
  factory. Kept the `SessionStore` Protocol and `InMemorySessionStore` class — both still
  live via `session_router.py`'s own four store instances. See `BUG.md`'s dead-code section.
- ~~`app.storage.conversation_log`~~ — **wired 2026-09-08**, no longer dead; see `BUG.md`
  B21 and `io.md`'s `session-aware-guardrail` entry.
- ~~`config.py`: `rank_weights_total`, `conversation_max_turns`~~ — **removed 2026-09-08**,
  confirmed zero readers. `session_ttl_seconds`/`session_max_entries` also removed in the
  same pass — they became dead the moment `build_session_store()` (their only caller) was
  deleted above.
- ~~`app/services/location_resolver/seed.py`~~ — **removed 2026-09-08** (8-city gazetteer +
  7-entry PIN fallback); see `BUG.md`'s dead-code section for the test-flakiness finding it
  surfaced.
- ~~`.env.example`/`.env`: `HF_TOKEN`, `HF_REPO_ID` — leftover from the removed training
  repo~~ — closed 2026-09-07, removed from both files along with the stale
  `WEATHERGPT_EXPLANATION_TONE` entry (`.env.example` only; the setting itself was already
  removed from `config.py` in the capability-selector round).

## Blocked on credentials / infra

- `IMD_API_KEY` — needs the EC2 elastic IP first (registration is IP-whitelisted)
- `STORMGLASS_API_KEY` — marine fallback built, never live-verified (needs a paid key)
- `app/services/model_client.py` — bias-correction integration doesn't exist yet (`model.md` has the contract)

## Roadmap — not started (`io.md`, build order)

1. `poi-geocoding` — landmark/POI resolution, needs a provider evaluated (Photon rejected)
2. `new-source-aqi` / `new-source-sunrise-sunset` (each needs a live smoke test first) → `new-source-tides-moon-astro` (no source picked yet)
3. `response-shape` — trim the response payload
4. `personalization` — deferred, unscoped

**Done since the last update**: `capability-selector`, `multi-location`/`multi-time`
(closes B8), `new-source-visibility`, `session-aware-guardrail` (closes B21),
`gfs-full-wiring` (Docker image now installs `requirements-full.txt` + `libeccodes0`/
`libeccodes-data`, GFS decodes 6 variables live-verified, was 2) — see `io.md`'s
"Done: notes" for each mechanism.

**Accepted, not bugs:** sub-locality queries (Kalyani/Howrah-style) cost one clarification
round-trip by design (`ranking.py` deliberately untouched); `lang-match` translates the LLM
explanation plus (as of 2026-09-08) 2 of the 4 fixed guardrail templates for Hindi only via
a static table — every other language and the `verify`/`unsupported_topic` templates still
fall back to English, see `io.md`'s `lang-match` note; the `warning-agent-geofilter` fix
shipped without a live before/after re-measurement of the original nationwide-CAP scenario.

## Suggested order

1. B2 — deterministic, unit-testable
2. §2.8 / A9 — throughput + metrics correctness, cheap together
3. A8 — data-accuracy bug (wrong probability cited)
4. B3 / B6 / B7's general class — prompt-behavior cluster, needs a live harness
5. B20's `pytest`-in-runtime-image half — requirements-dev.txt split, cheap
