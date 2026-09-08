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
| B20 | P1/P3 | No `.dockerignore` — `.env` (live keys) and `tests/`/pytest get baked into the Docker image |
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

- `app.storage.session_store` — built, imported nowhere. **Flagged, not removed 2026-09-07**:
  `tests/test_storage.py` frames the `SessionStore` Protocol as a deliberate seam ("the seam
  that makes Redis/Postgres a config change, not a rewrite"), which conflicts with this
  line's own "dead code" framing. Needs a call on which framing is right before deleting a
  Protocol-conformance-tested abstraction, not an incidental sweep.
- ~~`app.storage.conversation_log`~~ — **wired 2026-09-08**, no longer dead; see `BUG.md`
  B21 and `io.md`'s `session-aware-guardrail` entry.
- `config.py`: `rank_weights_total`, `conversation_max_turns` — zero readers
- ~~`.env.example`/`.env`: `HF_TOKEN`, `HF_REPO_ID` — leftover from the removed training
  repo~~ — closed 2026-09-07, removed from both files along with the stale
  `WEATHERGPT_EXPLANATION_TONE` entry (`.env.example` only; the setting itself was already
  removed from `config.py` in the capability-selector round).

## Blocked on credentials / infra

- `IMD_API_KEY` — needs the EC2 elastic IP first (registration is IP-whitelisted)
- `STORMGLASS_API_KEY` — marine fallback built, never live-verified (needs a paid key)
- `app/services/model_client.py` — bias-correction integration doesn't exist yet (`model.md` has the contract)
- GFS/GRIB2 — needs `requirements-full.txt` + system libs; unavailable in the Docker image

## Roadmap — not started (`io.md`, build order)

1. `poi-geocoding` — landmark/POI resolution, needs a provider evaluated (Photon rejected)
2. `new-source-aqi` / `new-source-sunrise-sunset` (each needs a live smoke test first) → `new-source-tides-moon-astro` (no source picked yet)
3. `response-shape` — trim the response payload
4. `personalization` — deferred, unscoped

**Done since the last update**: `capability-selector`, `multi-location`/`multi-time`
(closes B8), `new-source-visibility` — see `io.md`'s "Done: notes" for the mechanism.

**Accepted, not bugs:** sub-locality queries (Kalyani/Howrah-style) cost one clarification
round-trip by design (`ranking.py` deliberately untouched); `lang-match` translates the LLM
explanation plus (as of 2026-09-08) 2 of the 4 fixed guardrail templates for Hindi only via
a static table — every other language and the `verify`/`unsupported_topic` templates still
fall back to English, see `io.md`'s `lang-match` note; the `warning-agent-geofilter` fix
shipped without a live before/after re-measurement of the original nationwide-CAP scenario.

## Suggested order

1. B20's `.env`-in-Docker-image half — secrets exposure, cheap fix, only item here with blast radius outside this repo
2. B2 — deterministic, unit-testable
3. §2.8 / A9 — throughput + metrics correctness, cheap together
4. A8 — data-accuracy bug (wrong probability cited)
5. B3 / B6 / B7's general class — prompt-behavior cluster, needs a live harness
