# BUG.md — known defects

**Current open/closed status for every item below is tracked in `FIXES.md`** — this file
is the detailed narrative and reproduction steps behind each entry, not the live index.

Status as of 2026-09-05. The 2026-09-05 teardown's findings live in `AUDIT.md`;
this file stays the defect register. Every entry here was verified against real code or live data,
not inferred. Where something is a deliberate design trade rather than a defect it is
listed under "Not bugs" at the end, so this file stays trustworthy.

Severity: **P0** actively produces wrong output · **P1** wrong under realistic conditions ·
**P2** degraded/incorrect in narrow cases · **P3** latent, no current trigger.

---

## Fixed this session (in the working tree, NOT yet committed)

### F1 · P0 · CAP warnings broadcast nationwide — corrupted every RADE recommendation
`covers_query()` returns `None` when a warning has no usable polygon, and
`wio_builder._warning()` filtered with `is not False`, so `None` was treated as "covers
this user". **All 31 of 31 live NDMA alerts ship without polygons**, so every warning in
India was active for every user — and `rade/v2.decide()` forces
`risk_lambda = max(risk_lambda, 1.0)` whenever a severe warning is active, so spray /
irrigate / harvest / travel advice nationwide was computed under maxed-out risk aversion
triggered by unrelated alerts (an Assam river warning was Nagpur's "active warning").

Fixed in `spatial_match.area_names_query` + `wio_builder._covers`: polygon stays
authoritative; otherwise match the alert's area text against the location's
district/city/state, whole-word. A listed-districts alert
(`"Dhule, Jalgaon, Nashik districts of Maharashtra"`) requires the user's *own* district,
because the trailing state there names where those districts are rather than claiming the
whole state. Live: Nagpur 31→0, Nashik 31→1 (its own alert), Ferozepur 31→2, Dhubri 31→4,
Chennai/Delhi/Ludhiana 31→0. 7 new tests.

### F2 · P1 · "No compatible weather evidence" reported despite having evidence
`wio.weather.summary` is written **only** by `_rain_panel`. Any question that fetched no
precipitation (temperature-only, wind-only, marine-only) left it empty, and both consumers
— `main._synthesize` and `orchestrator._fact_sheet` (the explanation LLM's input) — read
empty as "no evidence at all". A Kolkata temperature query returned *"No compatible
weather evidence was available"* alongside a fully populated 27–31°C panel, and the LLM
then produced self-contradicting prose. Fixed once at source via
`wio_builder._fallback_summary` so every consumer benefits.

### F3 · P2 · `"right now"` leaked into extracted place names
`_TRAILING_TIME` matched `now` but not `right`, so `"temperature in Nagpur right now"`
extracted place `"Nagpur right"` → wrong geocode or 404. Fixed in `normalize.py`.

### F4 · dead code cleared (2026-09-05) · `big_llm()` had no caller
Config, chain and fallback were fully wired but nothing called `big_llm()`, so `BIG_LLM_*`
env config did nothing (was listed under "Dead code" below). Fixed: `run_explanation_agent`
now picks the tier via a new deterministic function, `_requires_big_llm`
(`app/agents/orchestrator.py`) — fires on a fused-source disagreement
(`wio.disagreements`) or a RADE decision that ran and landed below
`WEATHERGPT_BIG_LLM_COMPLEXITY_CONFIDENCE_THRESHOLD` (default 0.6, between RADE's real
0.55/0.8 confidence values), including a deferred decision (confidence 0). Gated on
`is_configured("big")`, so an unset `BIG_LLM_*` is a no-op fallback to the small tier —
zero behavior change for anyone who hasn't configured it. 5 new tests in
`tests/test_reviewer.py`. Wired to Groq (`qwen/qwen3.6-27b`), **live-verified**: a
disagreement/low-confidence query showed `tier='big'` in the logs against the real key,
and a confident query correctly stayed on `tier='small'`. Also found and fixed live: this
model is a reasoning model that inlines a `<think>` block into `content` by default,
eating the token budget meant for the answer — `reasoning_format: "hidden"` added to every
outbound request in `app/llm/client.py` fixes it (harmless no-op for endpoints that don't
recognize the field).

### F5 · P0/P1 · Location resolver was not India-first — closed 2026-09-05
Four live-verified failures, all fixed together (`ranking.py`, `normalize.py`,
`location_resolver/__init__.py`, `query_guardrail.py`):

- **India was a soft +2.0 bonus, not a hard tier** (`ranking.py`) — a large enough foreign
  population gap could still outscore it, which is exactly how `"fishing near
  Kochi"` → Kōchi, Japan happened. `rank()`'s sort key is now `(is_india, score)`: every
  Indian candidate outranks every non-Indian one whenever at least one exists, regardless
  of population.
- **A single-candidate provider response was unconditionally "dominant"** with no
  plausibility check (`ranking.select()`, the mechanism behind both `"climate in
  Chenai"` → a French village and `"weather near me"` → Cameroon: a free-text geocoder
  fuzzy-matched one low-quality candidate and it was accepted with full confidence). A lone
  candidate now only auto-wins with a known population, a capital-tier code, or an
  exact-name match; otherwise it's treated as ambiguous rather than a guess.
- **`"chenai"` (and 8 other common abbreviations/typos) were missing from the alias
  table** (`normalize.py:ALIASES`) — added.
- **`"near me"`/`"here"`/`"my location"` were geocoded as literal place names** —
  `normalize.is_self_referential()` now short-circuits `extract_place_phrase()` and
  `resolve_location()` itself (a backstop for when the guardrail's LLM hands the string
  straight to the resolver), plus a guardrail prompt example so the LLM path stops
  extracting these as locations in the first place.
- **`"where is Coimbatore"` was rejected as off-topic** instead of answered as a location
  lookup — one guardrail prompt example clarifying rule 6 vs rule 3 (prompt steering, not
  logic — closes B7's specific case, not the general prompt-inconsistency class B3
  documents).

Live-verified against the exact failing phrasings from B4/B5/B7 below: `"fishing near
Kochi"` → Kerala, `"climate in Chenai"` → Chennai, `"weather near me"` → a clean `422
LOCATION_REQUIRED` (not a fabricated location), `"where is Coimbatore located"` →
`ACCEPT_LOCATION_ONLY`, 200. 9 new tests in `tests/test_location.py` (46 total, all
passing), `ruff`/`mypy` clean.

### F6 · P0 · The fabrication guard's end-to-end proof — closed 2026-09-05 (was B1)
The single test behind this system's central safety claim asserted only
`status_code == 503` and `error.code == "REVIEW_FAILED"`, and had been failing for a day.
The stale hardcoded fixture date was the visible symptom; the real defect was that the
assertion was never coupled to fabrication detection — it could only ever fail from clock
drift, so it proved nothing about the guard.

Replaced by three cases (`tests/test_reviewer.py`), all driven through `/wio/query`:
**HONEST** (claim equals the re-derived 8.4mm sum → 200, claim present in the response),
**FABRICATED** (same evidence, claim tampered to 40.0 → 503), **BOUNDARY** (claim inside
`reviewer_value_abs_tol` → 200, proving the guard is `math.isclose`, not `==`, and does not
reject on float noise).

Time coupling fixed at the root: the fixture's evidence timestamps are derived by calling the
real `parse_time_window` with a frozen `now` (its `now: datetime | None = None` seam already
existed — production code needed no change), so the evidence always lands inside whatever
window the query resolves to. No date is hardcoded and no relative-date arithmetic is
duplicated in the test, making it correct across DST and local-midnight boundaries by
construction rather than by re-deriving the math.

The FABRICATED case asserts a structural invariant rather than a string: the 503 body has
exactly one top-level key (`error`) with exactly the fields `code`/`message`/`details`/
`request_id` — so a refactor that grows an answer surface on the error path fails the test —
the re-derived 8.4 is present, and 40.0 appears nowhere outside the reviewer's diagnostic
list. (That the diagnostic echoes it at all is recorded separately as **B18**, not fixed.)

**Mutation-verified**, which is what makes this more than a green check: with
`main.py:282`'s mismatch→503 branch disabled, FABRICATED fails (`200 != 503`); restored, it
passes. The test cannot pass while the guard is broken.

---

## Open bugs

### B1 · P0 · The reviewer's end-to-end fabrication guard is unverified — CLOSED 2026-09-05
**Fixed — see F6 above.** Original report retained below for the record.

### B1 (original report) · The test silently expired
`tests/test_reviewer.py::test_fabricated_value_returns_503_end_to_end` has been failing
all session and was repeatedly dismissed (including in `CLAUDE.md`) as "environmental —
small LLM tier unconfigured". **That diagnosis is wrong.** Proven:

```
fixture START      = 2026-09-04 00:00 UTC   (hardcoded in the test)
"tomorrow" window  = 2026-09-04 18:30 → 2026-09-05 18:29 UTC
fixture CEOs: 7  →  surviving the window: 0
```

The fixture hardcodes a date while the query says "tomorrow". It passed on 2026-09-03 (when
that date *was* tomorrow) and broke the next day. With no evidence surviving, there is no
rain panel, no `precipitation_amount` claim to tamper with, and nothing for the reviewer to
reject — so it returns 200 and the assertion fails.

Why P0: the anti-hallucination gate is this system's central safety claim, and its only
end-to-end proof has been dead for a day while being written off as noise. The gate's unit
tests (12 of them, on `verify_claim` directly) do pass, so the logic is probably fine — but
"probably" is not what this test exists to establish. Fix: make the fixture relative to the
query window instead of hardcoding a date.

### B2 · P1 · Pure Devanagari is rejected outright whenever the LLM is unavailable
`guardrail.py:TOPIC_WORDS` contains Romanized Hindi (`mausam`, `barish`, `hawa`) and **zero
Devanagari**. The deterministic fallback therefore rejects every pure-Hindi query as
off-topic:
```
check_question('कल दिल्ली का मौसम कैसा रहेगा')  -> REJECT ("not a weather request")
check_question('आज मुंबई में बारिश होगी क्या')   -> REJECT
```
Related: `normalize.py:_LEAD_PATTERNS` only recognises English prepositions, so no
Indic-phrased location is extractable at all —
`extract_place_phrase('aaj Mumbai mein barish hogi kya')` → `None`. Deterministic and
unit-testable. Not triggered while the LLM is up, which is why live batch testing missed it.

### B3 · P1 · LLM guardrail is inconsistent on Indic queries (6/15 failed)
Live batch: 6 of 15 multilingual queries returned `422 LOCATION_REQUIRED` (accepted as
weather but `location=null`), and one pure-Devanagari query was classified
`REJECT_OFF_TOPIC` outright.

**The obvious explanation is wrong.** The batch report concluded "Hindi SOV word order
fails"; the raw data contradicts it:

| Query | Structure | Result |
|---|---|---|
| `ccu mein mausam kaisa hai` | SOV + `mein` | works |
| `aaj Mumbai mein barish hogi kya` | SOV + `mein` | **422** |
| `आज मुंबई में बारिश होगी क्या` | pure Devanagari | works |
| `कल दिल्ली का मौसम कैसा रहेगा` | pure Devanagari | **rejected off-topic** |

Same structures, opposite outcomes → the model is inconsistent, not blind to a grammar.
Anyone fixing to the SOV theory would fix the wrong thing. `_SYSTEM_PROMPT` has no
Devanagari vocabulary, no statement that script is never grounds for rejection, and no
postposition guidance. Note this is prompt steering, not logic: it cannot be proven by
pytest (tests stub the LLM) and needs repeated live runs to distinguish improvement from
noise. Full plan in `~/.claude/plans/context-you-re-resuming-work-atomic-tiger.md`.

### B6 · P2 · `CLARIFY(no_location)` does not fire
`"will it rain tomorrow"` and `"how hot is it today"` — textbook no-location cases, rule 4
of the guardrail prompt — are classified `ACCEPT_WEATHER_FULL` with `location=null`, then
hit the pipeline's raw `422 LOCATION_REQUIRED` instead of the friendlier
`400 CLARIFICATION_NEEDED`. The nicer branch exists and is unreachable in practice.

### B7 · P2 · Same intent classified differently depending on phrasing — specific case closed 2026-09-05
`"where is Coimbatore located"` → `REJECT_OFF_TOPIC`, while
`"what are the coordinates of Bangalore?"` → correctly `ACCEPT_LOCATION_ONLY`. Both are
rule 6. Same class of prompt inconsistency as B3. **The specific reported phrasing is now
live-verified fixed** (F5 — one added rule-6 example in `_SYSTEM_PROMPT`). Left open
because it's prompt steering, not logic: the general class (some other phrasing tripping
the same inconsistency) is not provably closed by a unit test the way B3 isn't either.

### B8 · P2 · Multi-location questions silently answer for one location — CLOSED 2026-09-07
`"compare weather in Delhi and Mumbai"` returns a normal 200 for a single location with no
indication the other was dropped. Unsupported feature presented as a successful answer.
**Fixed**: the guardrail's `GuardrailDecision` now extracts `locations`/`time_phrases` as
plural arrays with an LLM-declared `pairing_mode`; `main.py` fans out every extra
(location, time) pair via `asyncio.gather` into an additive `comparisons` field on the
response (the primary `wio` is unchanged, so no existing caller's response shape breaks).
Live-verified: "compare Delhi and Mumbai this weekend" now resolves both locations with
`pairing_mode=locations_x_shared_time`.

### B9 · P2 · Synchronous SQLite blocks the event loop (audit §2.8)
`context/store.py` and `storage/sqlite.py` do blocking `sqlite3` calls inside `async def`
handlers with no `run_in_threadpool`. Every `/context`, `/feedback`, and every `/query`
carrying a `user_id` serialises the whole worker behind disk I/O — worsens with traffic and
defeats the async design of the rest of the pipeline.

### B10 · P2 · No cap on stored facts per user (audit §2.7)
`ContextFactInput.value` has no size limit and there is no cap on distinct fact names per
`user_id`. One authenticated caller can grow `weathergpt.db` without bound. The API-key work
scoped *whose* rows get written but not *how much*.

### B11 · P2 · Request size limit is bypassable (audit §2.6)
`RequestIDMiddleware` trusts the `Content-Length` header. Omit it (or use chunked encoding)
and the 64KB cap is skipped entirely — nothing re-checks bytes actually read. Combines with
B10.

### B12 · P2 · Nominatim throttle is a shared cross-tenant bottleneck (audit §2.3)
`NominatimGeocoder._lock`/`_last_request_at` are class-level: one process-wide 1 req/s
throttle. Any client issuing queries that miss the primary geocoder can serialise location
resolution for every concurrent user.

### B13 · P3 · Prose grounding only recognises four unit spellings (audit §2.5)
`check_prose_grounding` matches `mm`, `%`, `C`, `km/h`. A hallucinated `"50 mph"`,
`"41°F"`, or `"2 inches"` passes ungrounded. Only reachable if the explanation LLM
misbehaves in one of those units.

### B14 · P3 · Unrecognised claim derivation degrades to a warning (audit §2.4)
`verify_claim` treats an unknown `derivation["op"]` as a warning, not an error, so a claim
with an unverifiable shape passes review. Not currently reachable — every claim hardcodes a
valid op — but nothing in the type system enforces that for future agents.

### B15 · P3 · CAP adapter fetches feed-supplied URLs without an allowlist (audit §2.10)
`CapAdapter.fetch` follows `<link>` URLs from the feed with `follow_redirects=True`, no
scheme/host allowlist, parsed with stdlib `ElementTree` (not hardened against entity
expansion). Requires the upstream feed to be compromised. **Note:** this session widened the
link filter from `.xml`-suffix to any `http(s)` link to support NDMA's feed, which slightly
widens this surface — still gated on feed compromise.

### B16 · P3 · Raw exception text surfaced to clients (audit §2.11)
`retrieval._one` puts `f"{type(exc).__name__}: {exc}"` into the client-visible
`retrieval_status`. Currently only upstream HTTP errors, but it is uncurated exception text.

### B17 · P3 · Prompt-injection surface moved, not closed (audit §2.2)
The audit assessed injection against `query_extractor.py`, which **no longer exists** — the
same surface is now `query_guardrail.py`, whose output controls dispatch (`GuardrailAction`)
rather than just a boolean. Blast radius is still bounded (a manipulated action cannot
fabricate weather values, which come from deterministic fusion), but the finding should be
re-assessed against the new module rather than assumed closed.

### B18 · P2 · Reviewer mismatch diagnostic echoes both values to the client
`verification.py:96` builds `f"claim {claim.claim} states {claim.value!r} but {op} of its
cited evidence is {expected!r}"`, and `main.py:284` forwards the reviewer's error list to the
client in `error.details.errors`. So a rejected fabrication returns both the fabricated value
and the re-derived one to the caller, not only to the logs. Not a leak of the number *as
data* — the 503 body carries no answer/wio surface at all, and the value appears only labeled
as the rejected claim (this is asserted structurally by
`test_e2e_fabricated_value_returns_503_and_never_reaches_the_client`). Same class as **B16**;
resolve the two together — full detail to logs, correlation ID to the client.

### B19 · P3 · The 503 is unambiguous only by accident
`REVIEW_FAILED` is currently the only 503 in the app (`grep -rn ", 503)" app/` → one hit,
`main.py:284`), so a reviewer mismatch cannot today be confused with an upstream timeout.
Nothing enforces that: a second 503 source added later would be indistinguishable at the
status-code level, and any client branching on `503` alone would silently conflate them.
Callers should be steered to `error.code`, not the status code.

### B20 · P3 · Tests and test-only deps ship inside the deployed image — and so does `.env`
**`.dockerignore`/`.env`-in-image half CLOSED 2026-09-08** (found and fixed as a side effect
of live-testing the GFS Docker build below — a real build hit "no space left on device"
copying this repo's own `.venv/`, which is exactly the same class of problem as `.env`/
`tests/` being copied). Added `.dockerignore` excluding `.venv/`, `.git/`, `__pycache__/`,
`.env`, `tests/`, `*.db`. Live-verified: the image now builds without copying any of these.

**Still open**: `requirements-api.txt` still installs `pytest==9.1.1` into the runtime
image (a dependency-split issue, not a `.dockerignore` one — excluding `tests/` from the
build context doesn't stop `pip install -r requirements-api.txt` from installing the
`pytest` package itself). Splitting test-only deps into a separate `requirements-dev.txt`
is the fix; not done here, out of this session's scope.

**Still open, and still the more severe half**: rotate any `GROQ`/`GEOAPIFY`/etc. keys from
`.env` if an image built from this tree *before* 2026-09-08's `.dockerignore` fix has
already left this machine (registry push, shared host) — `.gitignore` never applied to the
Docker build context, so any such image has the keys baked into a layer regardless of this
fix.

### B21 · P1 · Guardrail rejected valid conversational follow-ups (context-blind) — CLOSED 2026-09-08
Not previously logged under a bug ID (found this session, not carried over from an earlier
audit). The guardrail classified every query in isolation with zero conversation memory:
`"will it rain in Newtown"` → `"can I go play in the evening"` hard-rejected the second turn
(`REJECT_OFF_TOPIC`) because it has no weather vocabulary of its own — even though it's an
obvious continuation. Distinct from B6 (a genuinely location-less first turn) and B7 (a
rule-6 phrasing inconsistency): this is a first turn with an established location but a
*second* turn carrying no topic signal at all, which no prompt-wording fix to a stateless
classifier could ever solve.

**Fixed**: `app.storage.sqlite.SqliteConversationLog` (see "Dead code" above — built,
tested, never called) is now wired via `app/services/input_pipeline/conversation_history.py`;
`app/main.py`'s `_resolve_guardrail_decision` fetches the last `settings.guardrail_history_turns`
turns and passes them to `run_guardrail`, rendered into the **user** message (never the
system prompt, so the decision-tree stays stable/cacheable independent of history).
`app/prompts/guardrail/behavior_rules.md` gained rule 0 (continuation). Decision cache key
changed from question-text-alone to `(text, history)` — necessary, since the same text can
now mean different things depending on context.

**Live-verified 2026-09-08** (paced to avoid LLM rate limits — see note below): "will it
rain in Pune tomorrow" → "can I go play in the evening" now returns a real 200 answer citing
Pune's rain probability, not a rejection; a Hinglish follow-up ("chatri le jaani chahiye
kya" after "weather in Indore tomorrow") also resolved correctly; the existing marine
2-turn follow-up flow (`should I go fishing` → `small boat, four of us`) still works
unchanged. **New finding from the same live run, not yet filed as its own bug**: a burst of
~20 requests in under a minute exhausted the Groq primary tier's rate limit, cascading every
subsequent guardrail/explanation call onto the Gemini fallback fast enough to exhaust
*its* quota too (`llm.chain_exhausted`, both tiers 400/429 within the same burst) — several
turns silently degraded to the deterministic fallback (which has none of rule 0's
continuation logic) until the burst subsided. Worth a dedicated bug entry if bursty
production traffic is expected; not filed as a numbered item here since it wasn't isolated
to a single reproducible request shape in this session.

**Mitigated 2026-09-08 (same day)**: a 3rd `small` tier endpoint added
(`SMALL_LLM_FALLBACK_2_*` in `.env`, OpenRouter — `nvidia/nemotron-3-super-120b-a12b:free`,
picked up automatically by `app/config.py:_llm_chain`'s existing generic loop, no code
change to the chain-walk itself). A burst now has to exhaust three independent providers'
quotas, not two, before degrading to the deterministic fallback. Two things live-verified
about this specific endpoint before trusting it: `qwen/qwen2.5-coder-7b-instruct` (the
initially requested model) doesn't exist on OpenRouter (404) and no qwen-coder variant is
free, so a general-purpose free model was substituted; and that model's reasoning trace
lands in a separate `reasoning` field (unlike the earlier `<think>`-in-content bug) but
still spends from the same `max_tokens` budget, truncating the guardrail's JSON on a
history-bearing rule-0 prompt at the old budget of 500 — raised to 1100
(`query_guardrail.py`'s `_LLM_KWARGS`) to fix it, confirmed live with `finish_reason: stop`
and a correct rule-0 continuation answer. **Not a complete fix**: this specific free
endpoint 502'd twice in 3 live test calls (upstream overload) — accepted as-is for a
prototype expected to handle ~10-15 concurrent requests, not hardened for production burst
traffic (no circuit breaker, no per-endpoint budget beyond the shared bump above).

---

## Dead code (not defects, but violates the coding rules in `CLAUDE.md`, rule 21)

- ~~**`app.storage.session_store`**~~ — **removed 2026-09-08**, confirmed genuinely unused
  (repo-wide grep found no importer, not even in tests). Deleted the singleton and its
  `build_session_store()` factory from `app/storage/__init__.py`/`memory.py`; kept the
  `SessionStore` Protocol (`app/storage/base.py`) and `InMemorySessionStore` class, both
  still live — `session_router.py` builds its own four separate instances directly (location,
  VERIFY, disambiguation, pending-followup), and `tests/test_storage.py`'s protocol test
  checks the class against the Protocol independent of the deleted singleton.
- ~~**`app.storage.conversation_log`** (`SqliteConversationLog`)~~ — **wired 2026-09-08**,
  no longer dead. See B21 below.
- **`app/services/location_resolver/seed.py`** — **removed 2026-09-08**. Its 8-city
  `GAZETTEER`/7-entry `SEED_PINCODES` were a last-resort offline fallback (used only when
  every live geocoding provider failed). Removing it surfaced a real, separate finding:
  several tests (`test_api_contracts.py`, `test_auth.py`, `test_reviewer.py`) posted
  "Will it rain in Nagpur tomorrow?" against the real `/wio/query`/`/query` endpoints
  without mocking location resolution at all — they were passing only because `seed.py`'s
  Nagpur entry silently absorbed occasional live Open-Meteo/Nominatim flakiness under full
  test-suite load (isolated single-test runs never showed it; full-suite runs intermittently
  did). Fixed by mocking `app.services.location_resolver.resolve_location` in all 6 affected
  tests, the same pattern `test_multi_location.py` already used. The underlying flakiness
  itself (why full-suite load occasionally makes `open_meteo`/`nominatim` raise
  `RuntimeError`) was not root-caused — worth investigating if it recurs outside a test
  environment; candidate suspects are shared `httpx` client lifecycle across many tests or
  Nominatim's shared throttle lock under rapid-fire real requests.

---

## Not bugs (deliberate trades — documented so they don't get "fixed")

- **CAP text matching is imprecise by design.** With polygons absent 100% of the time, an
  alert naming only a river or landmark (no district/state) is now missed rather than
  broadcast nationwide. Chosen over the alternative, which was F1.
- **Caches and evidence store are per-process.** `GET /evidence/{id}` 404s across workers.
  Run one worker until Redis; `app/storage/base.py` exists to make that a config change.
- **Rate limiting keys on `request.client.host`.** `X-Forwarded-For` is deliberately not
  trusted because it is spoofable. Correct direct-to-EC2; revisit behind a proxy.
- **Agreement thresholds are absolute** (10mm / 3°C), so "both sources say no rain" reads
  identically to "both say 40mm".
- **`historical`/`observation` agents slice `[:2]`** off class-filtered evidence rather
  than ranked output — arbitrary selection, but each claim cites its own CEO so citations
  stay self-consistent.
- **StormGlass adapter is unverified**, not broken — built to documented shape, needs a paid
  key to exercise.
- **GFS/GRIB2 unavailable** — needs `requirements-full.txt`; eccodes needs system libs not
  in the Docker image.

---

## Suggested order

1. **B20's `.env`-in-image half** — secrets baked into a Docker layer; cheap to fix, and the
   only item here with a blast radius outside this repo. (B1 closed 2026-09-05 — see F6.)
2. **B2** — deterministic, unit-testable half of the multilingual problem.
3. **B9 / B10 / B11 / B18** — the resource/throughput/leak cluster, cheap together.
4. **B3 / B6 / B7's general class** — prompt-behaviour cluster; needs a live harness,
   verify as one batch. (B4/B5 closed 2026-09-05 — see F5; B7's specific reported phrasing
   closed alongside it, general class left open same as B3.)
