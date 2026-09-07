# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Ownership boundary — read this first

The developer working in this repo owns **only the ML / data / training / inference-serving portions**. This is a hard scope boundary, not a preference:

**In scope:** the data pipeline (Open-Meteo/ERA5/GFS/IMD/NASA POWER/CAP retrieval), the CEO→WIO fusion pipeline (`app/services/`, `app/schemas/ceo.py`, `app/schemas/wio.py`), the bias-correction model's *integration path* (`app/services/model_client.py` — **not yet written**, see `model.md` for the contract it must implement; training happens in a separate repo), the LLM/agent orchestration layer (`app/agents/`, `app/orchestrator/`), the RADE decision engine (`app/rade/`), and the FastAPI inference-serving surface (`app/main.py`) as an API provider — plus the MLOps around all of that (checkpointing/hosting is the separate model repo's concern now; this repo's MLOps scope is basic EC2 inference deployment of the orchestration API).

**Permanently out of scope — do not propose or generate code for these unless explicitly asked:** any Android/mobile client, any dedicated frontend/UI, general backend/product engineering (auth, user accounts, billing, non-inference API surface), voice/TTS/STT, Nginx/Vercel/HF-Space *website* deployment, and CI/CD or release engineering beyond basic version control.

## Coding rules — follow on every change

Merged here from the former `coding_rules.md` (deleted 2026-09-05) so there is one file to
read. These are binding, not advisory.

### General Principles
1. Write clean, minimal, production-grade code. No exceptions.
2. Simplicity over cleverness. Fewer moving parts, fewer files, fewer abstractions.
3. Every line must justify its existence. If it is not used, delete it.
4. Optimize for readability first, performance second, brevity third — but never sacrifice performance for style.

### Comments
5. No unnecessary comments. Code should be self-explanatory through naming and structure.
6. Only comment non-obvious logic (e.g. algorithmic tricks, external constraints, workarounds).
7. No commented-out code left in files. Delete dead code instead of disabling it.
8. No TODO comments left unresolved. Fix it now or track it in an issue tracker, not inline.

### Debugging / Output
9. No print statements or debug logging left in final code.
10. Use a proper logging framework only when logging is a real requirement, with appropriate log levels (error, warning, info). No debug-level logs in production paths.
11. No emojis anywhere in code, comments, commit messages, or logs.

### Imports and Dependencies
12. Remove all unused imports before finalizing a file.
13. Remove all unused packages/dependencies from requirements files, package.json, etc.
14. Do not import an entire module/package when only one function/class is needed, if selective import is supported.
15. No duplicate or redundant dependencies achieving the same purpose.
16. Pin dependency versions; do not leave loose/unpinned versions in production.

### Structure and Organization
17. One clear responsibility per function. One clear responsibility per file/module.
18. Keep functions short; extract logic when a function exceeds a reasonable single-purpose length.
19. Group related code logically (models, services, utils, config) — no dumping everything into one file.
20. Consistent naming convention across the entire project (no mixing camelCase and snake_case in the same language context).
21. No dead files, no unused functions, no unused variables, no unused classes.

### Performance and Efficiency
22. Avoid unnecessary loops, nested loops, or repeated computation — cache/memoize where it measurably helps.
23. Avoid unnecessary object/data copies; prefer in-place or reference operations when safe.
24. Avoid premature I/O, network, or DB calls inside loops — batch where possible.
25. Use efficient data structures appropriate to the access pattern (set for membership checks, dict for lookups, etc).
26. Minimize third-party dependency overhead — do not add a library for something solvable in a few lines.
27. Lazy-load or defer expensive operations until actually needed.

### Error Handling
28. Handle errors explicitly; no silent except/catch blocks that swallow exceptions.
29. Fail fast and clearly — raise meaningful errors, not generic ones.

### Environment / Housekeeping
30. Delete `__pycache__`, `.pyc`, and other build/cache artifacts after finishing work on a file.
31. Remove unused virtual environments, unused installed packages, and stale lock file entries after finishing a task.
32. Keep `.gitignore` updated to prevent cache/build artifacts from being tracked.
33. No leftover temporary files, test scripts, or scratch files in the final project directory.

### Final Check Before Completion
34. Re-scan the file: remove unused imports, unused variables, dead code, debug prints, and stray comments.
35. Confirm the code runs with minimal overhead and no unnecessary dependencies before marking the task done.

## Session record — 2026-09-02/03 hardening pass

A five-phase audit and hardening pass ran over `app/`. `docs/SERVICES.md` is the
plain-language explanation of every service (mechanism, connections, faults) and is the
best starting point for anyone new to this code.

**Verified state at end of session:** `pytest -q` 107 passed · `ruff check` clean ·
`mypy app` clean · 6 of 8 sources live. (A later same-day session took this to 120 tests —
see the reviewer-gate record below.)

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

### Session record — 2026-09-03, reviewer gate and LLM seam

The outstanding-work register's reviewer item is closed. The reviewer no longer takes a claim's value on trust.

**The gate.** Every claim declares how its value was derived in `Claim.extra["derivation"]`
— `sum` / `max` / `min` / `identity` / `none`, with the variable, unit and accumulation
window it operated on. `app/agents/verification.py:verify_claim` re-runs that derivation over
the CEOs the claim actually cites and compares. Citing real evidence is necessary but no
longer sufficient. Two traps the verifier has to handle, both real in the fused panels: the
rain panel cites a `precipitation_probability` record alongside the amounts it summed (so the
verifier filters by declared variable *and* window before summing), and the wind panel reports
km/h from records that may be m/s (so the same conversion is re-applied). Panels now record
their own `variable` in `wio_builder.py`, which they previously did not — the temperature panel
picks between `temperature_2m` and `temperature_max` and the claim could not otherwise say which.

Comparison is `math.isclose` against `reviewer_value_rel_tol` / `reviewer_value_abs_tol`,
because panels round and exact equality would false-fail.

**Failure is split.** A contradicted value, a unit mismatch or an unknown evidence ID is an
error → 503, as before. A claim shape no verifier recognises is a *warning* — a future claim
type must not 503 the API just because nobody has written a verifier for it.

**Free text.** `check_prose_grounding` extracts every quantity carrying a unit (mm, %, C,
km/h) and requires each to match something the pipeline produced, allowing rounding to 0–2
places. Bare numerals are deliberately not checked — "the next 24 hours" is prose, not a
weather claim, and checking it yields only false rejections.

**LLM seam wired, off by default.** `run_explanation_agent` calls Groq only when
`WEATHERGPT_LLM_ENABLED=true` **and** `GROQ_API_KEY` is set. It receives a fact sheet built
from the WIO panels — never the raw CEO list — and is instructed to introduce no number. The
explanation is produced *before* the reviewer so the reviewer can check it, but stays last in
the returned agent list, preserving the existing response contract. Any failure (timeout, dead
key, empty response) degrades to the deterministic template answer with `status="partial"`; an
ungrounded number suppresses the explanation rather than failing the request
(`WEATHERGPT_REVIEWER_PROSE_FAILURE_MODE=fail` makes it fatal). `groq_client.py` was moved onto
the shared pooled `httpx` client and reads its key from `settings`, matching every adapter.

**Verified:** `pytest -q` 120 passed (13 new in `tests/test_reviewer.py`) · `ruff check` clean
· `mypy app` clean. Live against real sources: a 72-record Indore window summed to 22.7mm
passes the reviewer clean; with an invalid `GROQ_API_KEY` the 401 lands as explanation
`partial` and the request still returns 200 on the template answer. **The Groq success path
has never run against a real key** — it is covered only by tests with a stubbed client.

**What the gate still cannot do.** It confirms a claim is arithmetically faithful to the
evidence it cites, not that it cites the *right* evidence. An agent citing a real but
irrelevant CEO and reporting its value honestly still passes. Relevance is guaranteed by
construction (claims are built from the fused panels), not by the reviewer.

## Session record — 2026-09-04, intelligent guardrail/dispatch + marine data + deploy

Closed §2.9 (`/health` unauthenticated 8-way fan-out — now cached, `WEATHERGPT_HEALTH_CACHE_TTL_SECONDS`,
default 30s, in `app/adapters/registry.py`). Then a spec-driven initiative
(`CAPABILITY_MAP.md`, since-merged per-module specs) replaced the dead
`is_weather_related` boolean with a real dispatch key.

**The guardrail is now a decision, not a flag.** `app/services/query_guardrail.py`
replaces `query_extractor.py` (deleted, along with `NormalizedQuery`/`QueryIntent`, which
extracted an intent nothing ever branched on). One LLM call classifies every query into
`GuardrailAction`: `ACCEPT_LOCATION_ONLY` (skips retrieval/fusion/agents/RADE entirely —
"what are the coordinates of X" now answers in ~1s instead of running the full pipeline),
`ACCEPT_WEATHER_FULL` (unchanged), `REJECT_OFF_TOPIC`, `CLARIFY`, `VERIFY` (asks "did you
mean X?", confirmed via `session_router.py`'s new pending-verification store), and
`UNSUPPORTED_TOPIC` (a disaster type with no data source — earthquake, tsunami, wildfire,
landslide, volcanic, drought — answered honestly instead of rejected as off-topic or
fabricated with irrelevant weather data). The LLM applies a fixed decision-tree prompt,
never its own judgment; every non-accept action's message is a fixed Python template,
never LLM prose. Deterministic fallback (LLM down) covers every action except
CLARIFY(garbled)/VERIFY, which genuinely need language understanding.

**Scope actually widened, not just gated differently.** Marine/fishing, mountain/trek,
weather-driven disaster (cyclone/flood/storm/heat — already backed by CAP, just blocked
by the old narrow topic list), and route/travel planning are now accepted
(`guardrail.py`'s `TOPIC_WORDS`, the guardrail's system prompt). Found and fixed along the
way: `extract_place_phrase` had no lead pattern for "to" — "cyclone coming **to** Chennai"
passed the widened guardrail but then 422'd, since the location was never extracted.

**Marine data — genuinely new, not just unlocked.** `app/adapters/open_meteo_marine.py`
(primary, keyless) — verified live against the real API before writing it, which caught
that `ocean_current_velocity` is natively km/h, not m/s as first planned.
`app/adapters/stormglass_adapter.py` (fallback, keyed, `STORMGLASS_API_KEY`) — built to
its documented shape but **not** live-verified (needs a paid key). Six new
`CanonicalVariable`s, a `WIOWeather.marine` panel, `retrieval_planner.py` wiring. Proves
`adapter-extensibility` for real: both registered in `app/adapters/registry.py` with zero
other pipeline changes.

**Geoapify added as primary geocoder** (`app/services/location_resolver/providers/
geoapify.py`), existing Open-Meteo→Nominatim→India Post chain kept as fallback —
live-verified (correct fields, and ambiguity detection still works against its result
shape unmodified).

**`CAP_FEED_URL` switched to NDMA's Sachet feed** (`sachet.ndma.gov.in`, India's
multi-hazard alert aggregator, broader than the old IMD-only feed) — surfaced a real bug:
`cap_adapter.py`'s `_alert_links` filtered on `.xml`-suffixed links (IMD's shape); NDMA's
links are `FetchXMLFile?identifier=...` and were silently all dropped, which would have
made the new feed return zero warnings with no error. Fixed to accept any `http(s)` link,
live-verified (25 alert documents, 36 CEOs decoded correctly).

**Groq added as the small LLM tier** (`SMALL_LLM_MODEL=llama-3.3-70b-versatile`,
`SMALL_LLM_BASE_URL=https://api.groq.com/openai/v1`) — **currently 404s on every call**,
falling back to Gemini (which still works, proving the chain's resilience design). Model
name is the suspect; not yet fixed. See Next steps.

`TUNING_GUIDE.md` (which file to edit for a given customization), `AWS.md` (EC2 deploy —
Docker + a `weathergpt.service` systemd unit so it survives reboot) and the coding rules
(now a section of this file) were added this session.

**Verified:** `pytest -q` 256+ passed (1 pre-existing unrelated failure — small LLM tier
unconfigured *in the test environment specifically*, not a real bug); `ruff`/`mypy` clean
throughout. Several live smoke tests against real external APIs (Geoapify, Open-Meteo
Marine, NDMA CAP feed) — this session verified against reality more than any prior one.

### Next steps

**Groq 404 fixed, same day.** `llama-3.3-70b-versatile` still exists in Groq's catalogue
but is gated to Enterprise pricing — a plain API key 404s on it rather than a clearer
403. Switched `SMALL_LLM_MODEL` to `openai/gpt-oss-120b` (standard pay-as-you-go tier).
Live-verified: `fallback_used=False`, `attempts=1`, ~1.0-1.2s latency — noticeably faster
than the Gemini fallback (2-6s) that had been silently absorbing every call until now.

1. **`IMD_API_KEY`** — still blocked on you. Register at `api.imd.gov.in/public/login.php`
   (IP whitelisting; needs the EC2 elastic IP to exist — see `AWS.md`). CAP (NDMA's Sachet
   feed, live and keyless) already covers official warnings including cyclone/flood/storm/
   heat, so this is no longer the only route to Indian warnings, just the richer one
   (forecasts, observations, rainfall on top).
2. **`app/services/model_client.py`** still does not exist — see `model.md` for the
   contract. Call it between the semantic gate and `build_wio` in `main.py`.
3. **`STORMGLASS_API_KEY`** — the fallback marine adapter is built but not live-verified
   (needs a paid signup key, unlike Open-Meteo Marine's keyless primary). If you get one,
   smoke-test it the same way Geoapify/Open-Meteo Marine were verified this session.
4. **`big_llm()` is now wired — closed 2026-09-05.** `run_explanation_agent`
   (`app/agents/orchestrator.py`) calls it behind `_requires_big_llm`, a deterministic
   trigger: fires when fused sources disagree (`wio.disagreements`), or when a RADE
   decision actually ran and landed below `WEATHERGPT_BIG_LLM_COMPLEXITY_CONFIDENCE_THRESHOLD`
   (default 0.6 — between RADE's real 0.55 partial-agreement and 0.8 full-agreement
   values), including a deferred decision (confidence 0). An unconfigured `BIG_LLM_*` is a
   no-op fallback to the small tier, same convention as every other optional source. Wired
   to Groq (`qwen/qwen3.6-27b`, same base URL as the small tier), **live-verified**: a
   disagreement/low-confidence query showed `tier='big'` in the logs, a confident query
   stayed on `tier='small'`. Also found and fixed live: this model is a reasoning model
   that inlines a `<think>` block into `content` by default, eating the token budget meant
   for the answer — `reasoning_format: "hidden"` added to every outbound request in
   `app/llm/client.py` fixes it.
5. **Security audit — §2.2, §2.5, §2.6, §2.7, §2.10, §2.11 closed 2026-09-07** (§2.1/§2.9
   already closed in prior sessions). §2.2: `GuardrailDecision`'s LLM-produced string
   fields (`location`/`time`/`verify_candidate`/`unsupported_topic`/`original_text`) now
   carry `max_length`, matching every other user-facing field. §2.5: `check_prose_grounding`
   now also recognizes `mph`/`inches`/`°F`, converted to their canonical bucket before
   comparison. §2.6: the `Content-Length`-trusting check is replaced by
   `app/main.py:MaxBodySizeMiddleware`, a raw ASGI middleware that bounds the real byte
   stream regardless of any header. §2.7: `app/context/store.py:upsert_fact` now enforces
   `context_max_facts_per_user`/`context_value_max_chars`, raising `ContextLimitExceeded`
   (`app/storage/base.py`) mapped to a 422. §2.10: `CapAdapter._alert_links` now only
   follows links on the feed's own host, and both `cap_adapter.py`/`cap_decoder.py` parse
   with `defusedxml` (new pinned dependency) instead of stdlib `ElementTree`, which was
   verified to block XXE but not internal-entity expansion. §2.11: `retrieval.py`'s
   `_one` now returns `_safe_error_reason(exc)` — a small fixed set of strings — instead
   of the exception's own text, into the client-visible `retrieval_status`.
   **§2.3 scoped, then explicitly declined** — the fix (bound the Nominatim throttle's
   queue depth, fail fast past a cap) was ready but not applied; this repo runs in a
   private VPC with one known caller and Security Group network control, so the shared-IP
   proxy scenario this closes doesn't currently apply — see "Known limitations" below for
   the accepted-tradeoff note. **§2.4 and §2.8 remain open**, not attempted. Current status
   of every item is `FIXES.md`; the old standalone `docs/security-audit-2026-09-03.md` was
   merged into it and removed the same day.
6. Rate limiting keys on `request.client.host`. Behind a proxy every caller shares one
   bucket; `X-Forwarded-For` is deliberately not trusted because it is spoofable.
7. `GFS` needs `requirements-full.txt` (eccodes needs system libraries) — the Docker image
   built this session (`AWS.md`) still only installs `requirements-api.txt`; GFS stays
   unavailable in the container too until that's added.
8. `rag-app-info` and broader (non-weather-driven) disaster types remain deliberately
   deferred — see `CAPABILITY_MAP.md`.

### Resume next session

```bash
cd ~/weathergpt && source .venv/bin/activate
pip install -r requirements-api.txt
pytest -q                                    # expect 256+ passed, 1 pre-existing unrelated failure
ruff check app tests && mypy app             # both clean
set -a && source .env && set +a && uvicorn app.main:app --host 0.0.0.0 --port 8001
```

Read `docs/SERVICES.md` first, then `FIXES.md` for current status, `BUG.md`/`AUDIT.md` for
the detailed narrative behind each item, and `TUNING_GUIDE.md` for "which file do I edit to
change X."

## Session record — 2026-09-05, hostile audit and the fixes it produced

A pre-production teardown ran over `app/` against the coding rules above. Findings are in
`AUDIT.md`; what got fixed is below. Every fix was verified by `pytest -q` plus a live run.

**Fusion's agreement signal was saturated by the ensemble.** `ranker.group_comparable`
bucketed evidence by `(variable, window, valid_from)` without excluding ensemble members, so
30 GEFS member rows shared a bucket with the deterministic row. Since `GEFS` is
`ensemble-api.open-meteo.com` — the same vendor as `OPEN_METEO` — one vendor corroborated
itself, and its own 0-34.8mm spread read as sources disagreeing. Measured before the fix:
"two vendors agree (4.0 vs 4.3mm)" and "two vendors disagree (4.0 vs 38.0mm)" both produced
`partial_agreement` / RADE confidence 0.55 / big-LLM-woken — indistinguishable. One line in
`group_comparable` now skips members; the two cases separate correctly. Mutation-verified.

**RADE's confidence scale did not discriminate.** `rade/v2.py` scored `single_source`
identically to `full_agreement` (both 0.8), so corroboration bought nothing. Now
`{full_agreement: 0.8, single_source: 0.65}`, defaulting to 0.55. The 0.65 sits deliberately
above `WEATHERGPT_BIG_LLM_COMPLEXITY_CONFIDENCE_THRESHOLD` (0.6) — anything at or below it
would wake the big tier on every single-vendor query, turning a source outage into a cost
event.

**One slow source stalled every request for 61s.** `source_timeout_seconds` 20 with
`source_retries` 2 meant 3 attempts per source and no total budget. Now 8 and 1 -> 16.4s
worst case, changed in `config.py`, `.env` and `.env.example` (the env files pinned the old
values, so editing config alone would have been inert). `retrieve()`'s fan-out still has no
total deadline.

**The guardrail LLM ran on every request, uncached.** `run_guardrail` now memoizes on
casefolded question text in a `TTLCache` (`WEATHERGPT_GUARDRAIL_CACHE_TTL_SECONDS`, 3600).
Deterministic at temperature 0, so no output changes; ~1.1s off every repeat. A
`deterministic_fallback` decision is never cached — a degraded read must not outlive the
outage. `tests/conftest.py` clears the cache between tests, because several reuse one
question with a different stubbed LLM. **Caveat:** this makes `BUG.md` B3's guardrail
inconsistency sticky for the TTL rather than per-request.

**The user's raw question reached the explanation model's prompt.** `_fact_sheet` now sends
`Intent:` (from `wio.query.intent`, a closed set the pipeline derived) instead of
`Question: {raw_text}`. `check_prose_grounding` only constrains numbers carrying a unit, not
instructions, so that string was the one steering surface into an answer the user reads.

**Provenance was dropped one layer from the user.** `_evidence_summaries` now forwards
`provenance.original_source`, so a `GEFS` row visibly reads `Open-Meteo ensemble API`.
`AUTHORITY["GEFS"]` 0.72 -> 0.70; that half is inert (`_best_source` skips members) and was
applied for table honesty only.

### Live test against Kolkata, 2026-09-05, during real heavy rain

Retrieval and decoding verified sound: Open-Meteo decodes 24/24 hourly values exactly
(3.8mm = 3.8mm), MET Norway's 1h and 6h accumulations decode and stay separated, 39 CAP
documents decoded and the Kolkata alert geo-matched correctly. **The defects are in
planning and fusion, not retrieval.** Two were fixed on the spot:

- **CAP is now always retrieved** (`retrieval_planner`). It used to be gated on the user
  saying "warning"/"alert"/"cyclone", so `"weather in Kolkata"` returned no mention of a
  live yellow thunderstorm warning for that district. An official warning is safety
  information and must not depend on phrasing.
- **`"right now"` and `"next N hours"` resolve to nowcast windows** (`time_parser`), floored
  to the top of the current hour so the current hour's record is inside the window. Both
  previously resolved to the whole 24-hour calendar day: `"is it raining right now"` was
  answered with a day total including hours already past.

Still open from that run, recorded in `AUDIT.md`: `full_agreement` is claimed between
sources whose day totals differ 2.7x (absolute 10mm threshold cannot fire at light-rain
magnitudes); only the top-ranked source's value is ever reported; there is no observation
source at all, so "is it raining now" is answered from forecast models without saying so.

## Session record — 2026-09-07, de-personafication, capability-selector, multi-location

Two changes shipped together because they extend the same object: `Persona` (one hardcoded
enum value + keyword list + RADE branch + fact-sheet gate + fixed follow-up sentence *per
persona*, added for marine/fishing and not generalizable to the next scenario) is gone
entirely, replaced by a domain-general mechanism; and `GuardrailDecision` gained a
capability-selector and plural `locations`/`time_phrases` in the same schema pass, since
redesigning that one fragile LLM call twice would have meant two rounds of churn on the
most failure-prone call in the pipeline.

**De-personafication.** `apparent_context: str | None` (LLM-inferred, e.g. "planning a
fishing trip", degrades to `None`) replaces the `Persona` enum everywhere it touched
behavior. `DecisionResult.top_margin` (score gap between the top two ranked RADE actions)
and a `CLARIFYING_FIELDS` dict in `rade/v2.py` (per-domain optional context fields worth
asking about on a borderline call — today only `marine: {crew_size, boat_size}`) replace
marine's hardcoded follow-up question with a domain-general mechanism that is a no-op for
every domain without an entry. The follow-up question is now LLM-composed prose in its own
words, not a fixed sentence; `session_router`'s pending-followup store threads the resumed
domain explicitly (`consume_pending_followup` returns `(text, domain)`), fixing a real
latent bug where every resumed follow-up silently defaulted to `"marine"`. One rich,
domain-invariant `_EXPLANATION_SYSTEM` replaces the per-domain tone-directive branch;
`WEATHERGPT_EXPLANATION_TONE`/`WEATHERGPT_MARINE_TONE` are gone (removed from `config.py`
and `.env.example` in this round — `.env.example` still had the former, stale, until this
cleanup).

**Capability-selector.** `retrieval_planner.py` gained a closed vocabulary
(`DATA_CAPABILITIES`: temperature/precipitation/wind/marine/extreme_events/humidity/
pressure/cloud_cover/visibility/heat_stress; `GUIDANCE_FLAGS`: `travel_safety_guidance`,
which carries no data and is invalid alone). The guardrail LLM selects from this list
alongside its existing `action` classification — one more field on the same call, not a
second round-trip — and `build_retrieval_plan(..., capabilities=...)` ORs each selection
into the existing keyword triggers, never replacing them, so the deterministic path is
unaffected when the LLM is down. This replaces keyword-only retrieval triggering (confirmed
missing: "is it safe near the coast" hit no marine keyword, "fog on the road" hit nothing
at all) with genuine understanding while fetching stays exactly as deterministic as before.
`humidity`/`pressure`/`cloud_cover`/`visibility` were wiring-only — Open-Meteo's adapter
already requested and decoded them, nothing ever triggered fetching them; `heat_stress` is
a pure derived computation (`wio_builder._heat_stress_panel`, NWS heat-index/wind-chill
formulas) from panels already built, no new fetch. Every new `GuardrailDecision` field
degrades independently (an invented capability name is dropped, not fatal); only a
`GUIDANCE_FLAGS`-only selection is genuinely malformed and triggers `run_guardrail`'s
existing bounded one-shot retry before falling back deterministically.

**Multi-location/multi-time.** `locations`/`time_phrases` (plural arrays) replaced the
singular `location`/`time` fields, kept as read-only `@computed_field` properties
(`locations[0]`/`time_phrases[0]`) so no existing call site needed an atomic migration. A
guardrail-declared `pairing_mode` (`locations_x_shared_time` / `times_x_shared_location` /
`full_cross_product`) says how to pair them; `main.py`'s `_resolve_pairs` turns that into
concrete pairs (malformed `full_cross_product` — mismatched list lengths — falls back to
`locations_x_shared_time` rather than crashing `zip(strict=True)`), capped at
`settings.max_location_time_pairs` (new setting, default 6) by truncation. Every pair
beyond the primary one fans out through `asyncio.gather` (`_build_comparison_wio`:
resolve + fetch + fuse only, deliberately no RADE/agents — multiplying the explanation LLM
call by the pair count wasn't in this round's budget) into a new, additive `comparisons`
field on `/query`/`/wio/query` — the existing `wio` field is unchanged, so no current
caller's response shape breaks. The pending-followup clarifying question is skipped
whenever more than one pair resolves (no defined rule yet for which borderline pair to ask
about). This closes `BUG.md`'s B8.

**Verified:** `pytest -q` 382 passed (26 new, covering capability triggering, the five new
WIO panels including both heat-index and wind-chill branches, `_resolve_pairs` for all
three pairing modes, the `comparisons` fan-out end-to-end, and the guardrail's
validate-then-retry-then-fallback path for capabilities) · `ruff`/`mypy` clean. **Live
batch-tested** the capability-selector against the real Groq/Gemini chain, 2 runs of 14
queries each spanning every wired capability plus ambiguous/multi-location/multi-time
phrasing (per this file's own precedent — B3 and the reasoning_effort bug both showed a
single smoke test proves nothing about LLM-classification reliability). Every query reached
the real LLM in both runs; `humidity`/`cloud_cover`/`visibility`/`heat_stress` all fired
correctly on phrasings matching *none* of `retrieval_planner.py`'s keyword lists ("is it
humid", "how much cloud cover", "fog on the road", "will it feel very hot"), confirming
genuine generalization past keyword matching; multi-location/time extraction was correct
and consistent both runs.

**A second `reasoning_effort` token-budget bug, same class as Phase 1's, found and fixed
during that live testing.** `reasoning_effort: "low"` (added earlier to stop the Groq
primary endpoint's hidden reasoning from starving `max_tokens`) is applied uniformly across
the whole fallback chain in `llm/client.py`, but the Gemini fallback endpoint has different
reasoning-token economics under the same budget. Reproduced live: at `max_tokens=280`,
Gemini returned HTTP 200 with `finish_reason: "length"` and the guardrail JSON truncated
mid-object — a non-exceptional "success" that `raise_for_status()` never catches, so it
wasn't retried as a network failure; it correctly fell through to `_parse()`'s own
unparseable-JSON handling and degraded to the deterministic path. Fixed by raising the
guardrail's shared `max_tokens` from 280 to 500 (`app/services/query_guardrail.py`);
confirmed live afterward that Gemini completes cleanly (`finish_reason: "stop"`,
~162 completion tokens) with headroom. Closes `AUDIT.md`'s A10 (previously "not verified")
with no defect in what A10 originally asked about — the defect found was adjacent, not the
one A10 named.

**Housekeeping.** `.env`/`.env.example` had a dead `HF_TOKEN`/`HF_REPO_ID` block left over
from the ML training pipeline moved to a separate repo weeks ago (`kaggle_kernel_m2/`,
referenced in a comment, no longer exists) — removed from both files, along with
`.env.example`'s stale `WEATHERGPT_EXPLANATION_TONE` line. A stale docstring in
`tests/test_query_guardrail.py` referencing a deleted sibling test file and module
(`test_robust_pipeline.py`, `query_extractor.py`) was corrected. `io.md`'s `multi-location`/
`multi-time`/`new-source-visibility` rows moved from "not started" to done, and a
`capability-selector` row was added; `docs/SERVICES.md` gained changelog entries in the
`retrieval_planner.py`, `rade/v2.py`, `wio_builder.py` and `main.py` sections for every
mechanism above; `TUNING_GUIDE.md` gained rows for the new tunables. **Flagged, not
acted on**: `BUG.md`/`FIXES.md` document `app.storage.session_store`/`conversation_log` as
dead code, but `tests/test_storage.py`'s own docstring frames the same
`SessionStore`/`ConversationLog` Protocols as a deliberate seam ("the seam that makes
Redis/Postgres a config change, not a rewrite") — the two documents disagree, and deciding
which framing is right (then deleting a Protocol-conformance-tested abstraction, or
formally re-labeling it a seam) needs a call, not an incidental sweep; left for a dedicated
pass.

## Outstanding work register

Merged here from the former `cloud.md` (deleted 2026-09-05). This is what **remains**; the
session records above are what was fixed. `BUG.md` is the defect register, `AUDIT.md` the
2026-09-05 teardown. **`FIXES.md` is the current, compact "what's still open" index across
security/bugs/roadmap — check there first; this section and the two files above carry the
detailed narrative behind each entry.**

### Blocked on credentials

| Source | Missing | Effect |
|---|---|---|
| **IMD** (authority 0.95) | `IMD_API_KEY`, `IMD_API_BASE` | India's own met authority absent. The 5 decoder variants in `imd_json.py` have never run against real data. Register at `api.imd.gov.in/public/login.php`; IP whitelisting, so the EC2 elastic IP must exist first. |
| **GFS/GRIB2** | `cfgrib`/`eccodes`/`xarray` | Adapter self-reports unavailable. Needs `requirements-full.txt`; eccodes needs system libs, and the Docker image still installs only `requirements-api.txt`. |
| **STORMGLASS** | `STORMGLASS_API_KEY` | Marine fallback unexercised. Primary (`OPEN_METEO_MARINE`, keyless) works. Needs a paid key. |

CAP (authority 1.0) is live and keyless on NDMA's Sachet feed, so IMD is no longer the only
route to Indian warnings — it would add forecasts, observations and rainfall on top.

### Seams — intentionally empty, do not delete

- **ML bias correction** — `app/services/model_client.py` does not exist. Create per
  `model.md`; call between the semantic gate and `build_wio` in `main.py`.
  `CanonicalEvidenceObject` already carries the unused `parent_ids`/`transformation`/
  `transformation_timestamp`/`algorithm_version` fields for it.
- **Multilingual** — transliteration in front of `location_resolver/normalize.py` plus
  language routing in `time_parser`. Hindi keywords already exist in both
  `retrieval_planner.py` and `time_parser.py`. See `BUG.md` B2/B3.

### Known limitations

- Rate limiting keys on `request.client.host`; behind a proxy every caller shares one
  bucket. `X-Forwarded-For` is deliberately not trusted. **Accepted for the current
  deployment** (2026-09-06): WeatherGPT runs in a private VPC with a single known
  caller (the other team's backend), and AWS Security Group handles network-level
  access control — there is no public-facing proxy in front of it, so this isn't live.
  Revisit (trust `X-Forwarded-For` only from a configured trusted-proxy IP) if the
  deployment topology changes to sit behind a reverse proxy/load balancer with multiple
  real clients.
- Agreement thresholds are absolute (10mm / 3C). Verified live 2026-09-05: two sources
  whose day totals differ 2.7x (1.4mm vs 3.8mm) still read as `full_agreement`.
- `historical`/`observation` agents slice `[:2]` off class-filtered evidence rather than
  ranked output — arbitrary, but each claim cites its own CEO.
- Caches and the evidence store are per-process. With multiple workers `GET /evidence/{id}`
  404s across workers, and each worker pays its own guardrail cache miss. Run one worker.
- Nominatim is throttled 1 req/s per process; multiple workers can exceed OSM policy. That
  same throttle is also a single shared lock across every concurrent request (§2.3,
  `FIXES.md`) — one client forcing several Nominatim fallbacks
  can serialize location resolution for everyone else to 1/sec. Scoped, then declined
  2026-09-07 (see the security-audit item above); revisit if this ever sits behind a
  proxy with multiple real tenants.

### Deployment checklist

- `WEATHERGPT_API_KEYS` must be set — empty disables the auth gate.
- `WEATHERGPT_CORS_ORIGINS` must be set or CORS middleware is never installed.
- `WEATHERGPT_MET_NORWAY_USER_AGENT` must identify the deployment; generic agents get 403.
- Outbound HTTPS to the weather/geocoding hosts plus any configured LLM endpoint.
- `WEATHERGPT_LOG_JSON=true` for machine-readable logs.
- Run **one** uvicorn worker until the caches move to Redis.
- The LLM explanation stays off unless `LLM_ENABLED=true` and the small tier is configured.
  Leaving it off is supported — the answer is then entirely template-built.

### Would another language help?

No. The request is I/O-bound — retrieval and location resolution dominate, compute is under
1%. The only CPU-bound step is GRIB2 decoding, already native C behind `cfgrib`. Every win
so far (pooling, caching, circuit breaker, guardrail memoization) was pure Python.

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
pytest -q                                # full suite (382+ tests as of last verified run)
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
Only `requirements-api.txt` is installed in the image — GFS/GRIB2 stays unavailable
inside the container too (see Next steps). `AWS.md` has the full EC2 deploy path
(Docker + a `weathergpt.service` systemd unit so it survives reboot).

## Architecture

WeatherGPT is an evidence-grounded weather intelligence backend, not a "call an LLM with a weather prompt" system. The governing rule throughout the codebase: **numbers come from deterministic pipelines, never from an LLM's imagination.** LLMs (when wired in) only ever explain or reason over data that's already been fetched, validated, and fused — they never select data sources, never invent values, and never bypass evidence citation.

### Request flow (`POST /query`, `app/main.py:_weather_request`)

```
query_guardrail.run_guardrail  (one LLM call → GuardrailAction; fixed decision-tree prompt,
                                 never the LLM's own judgment — see Session record 2026-09-04)
  ├─ REJECT_OFF_TOPIC / CLARIFY / VERIFY / UNSUPPORTED_TOPIC → fixed template message, return now
  ├─ ACCEPT_LOCATION_ONLY → location_resolver only → return now (no retrieval/fusion/agents/RADE)
  └─ ACCEPT_WEATHER_FULL ↓
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

**Agents (`app/agents/orchestrator.py`)** — seven deterministic Python functions that each derive a claim from the already-built WIO, plus `run_explanation_agent`, the one seam where a language model runs. `reviewer_agent` is a hard gate on two counts: a claim citing an `evidence_id` not present in the retrieved evidence flips the request to a 503, **and** the value attached to that citation is recomputed from the cited evidence (`app/agents/verification.py`) and must match. LLM prose is checked instead for quantities the pipeline never produced. `run_explanation_agent` is inert unless `LLM_ENABLED=true` and the small LLM tier (`SMALL_LLM_MODEL`/`_BASE_URL`/`_KEY` in `.env`) is configured; it writes prose only, never selects a source, never originates a number, and degrades to the deterministic template answer on any failure.

**RADE (`app/rade/v2.py`, function `decide`)** — the risk-aware decision engine for questions like "should I spray." Builds 2 (or, with ensemble member data, 5-bin) scenarios from `wio.weather.rain`, scores each action as `expected_utility − risk_lambda·downside_risk`, picks the argmax. Returns `defer_decision` rather than guessing when evidence is insufficient — never fabricates a probability or amount. This is the *only* RADE implementation in the repo — an older parallel v1 (`enumerator.py`/`utility.py`/`policy.py`) existed and was silently computed-but-discarded on every request; it and its frozen `kaggle_kernel/` snapshot were both deleted (see "Known state" below).

### Bias-correction model — integration path only, training lives elsewhere (`model.md`, `app/services/model_client.py`)

ML model training (GFS-forecast-vs-ERA5-reanalysis bias correction, formerly `training/` + `kaggle_kernel_m3/` in this repo) was moved out entirely to a separate repo. `model.md` at the repo root is the full handoff document: dataset construction methodology, feature engineering, real validated baseline results (LightGBM vs. ridge vs. no-correction on a real 24,960-row dataset), the MLP architecture that was attempted but never got a real GPU run, and — most importantly — the exact HTTP API contract this repo expects from that model once it exists.

**`app/services/model_client.py` does not exist yet** — this section describes the intended design, not current code. When written it should be a thin HTTP adapter calling the external model API, and be invoked between the semantic gate and `build_wio` in `app/main.py` so corrected values flow into fusion with a `provenance.transformations[]` entry. `CanonicalEvidenceObject` already carries unused `parent_ids`/`transformation`/`transformation_timestamp`/`algorithm_version` fields for exactly this. Until then every response uses raw, uncorrected forecast evidence, which `/health` reports honestly as `models.bias_correction`.

### Location resolution (`app/services/location_resolver/`)

A package, not a module — the old single-file resolver with its 8-city gazetteer and 7-PIN dict is gone. Resolution order is coordinates → cache → PIN → normalized place name → provider chain → rank → ambiguity. Providers (`providers/`) are tried in order: Open-Meteo Geocoding (primary, keyless, structured admin1/admin2/population), then Nominatim/OSM (covers Indian districts, states, small towns and historical aliases that Open-Meteo lacks), with India Post for PIN codes. All keyless.

India is preferred by **scoring, not filtering** (`ranking.py`): `log10(population) + 2.0 if India + 1.0 if capital + 0.5 exact-name`. Filtering by `countryCode=IN` was tried and rejected — it resolves "Springfield" to a Tamil Nadu hamlet. A winner is auto-picked only when it beats the runner-up by `settings.geocoding_dominance_margin`; otherwise `LocationAmbiguousError` → 409, never a coin-flip. `seed.py` is an offline last resort (exact match only) that still raises rather than fabricate coordinates for an unknown place.

`resolve_location`/`extract_location` are **async** — they perform network geocoding.

## Known state, don't assume otherwise

- Root-level `architecture.md`, `implementation.md`, `report.md`, `setup.md`, `INSTALL.md`, and ten dated `docs/*_2026-09-01.md`/planning docs described an earlier/aspirational system built by a previous developer (a different machine path, a different Kaggle account, a fully-live Groq multi-agent pipeline, nonexistent endpoints like `GET /plan`, self-reported metrics later found unverified, and — in `report.md` — a partially-visible API key fragment) that did not match current code. Deleted as stale in this session; `README.md` and `docs/PROOF_OF_WORK.md` remain the accurate source of truth.
- No ML metric currently in this repo is independently validated — the real validated baseline numbers (LightGBM/ridge vs. no-correction) that used to be summarized in `docs/PROOF_OF_WORK.md` now live in `model.md`, since ML training itself moved to a separate repo.
- The old RADE v1 snapshot (`kaggle_kernel/`) and the M1/M3 training kernels (`kaggle_kernel_checker/`, `kaggle_kernel_official/`) were deleted along with M1/M3 themselves — do not reference or try to resurrect them. The bias-correction model (formerly `kaggle_kernel_m3/`, informally "M3") is unrelated to the deleted M3 intent-parser above — don't confuse them if the name resurfaces in old commits.
- The outstanding-work register lives in this file (above `## Commands`). Read it before proposing work.
- `docs/SERVICES.md` explains every service in plain language — mechanism, connections, and known faults. Start there.
- All ML training code (`training/`, `kaggle_kernel_m3/`) and the Kaggle training guide were removed from this repo as of this change — training now happens in a separate repo. See `model.md` for the full handoff.
- `CAPABILITY_MAP.md` tracks the 2026-09-04 guardrail/marine/geoapify initiative module by module — read it for what's built vs. deferred. The per-module `SPEC-*.md` files and `tasks/` were merged into it and deleted 2026-09-05.
- `TUNING_GUIDE.md` maps "I want to change X" to the exact file — check there before searching.
- `io.md` is the input/output generalization roadmap (language matching, multi-location/multi-time, POI geocoding, new meteorological/astronomical sources) — a living document, update it in the same change as anything it describes, not after.
- `AWS.md` + `weathergpt.service` are the EC2 deployment path (Docker + systemd, survives reboot).

## Code style

Match the existing codebase: no unnecessary comments (only ones explaining non-obvious *why*, never restating *what* the code does), no emojis, no debug prints. Don't create new files without a clear reason. The binding rule set is the "Coding rules" section near the top of this file — more exhaustive than this paragraph, and it does not contradict it.
