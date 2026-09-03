# WeatherGPT Backend Audit — Request Pipeline, `POST /query` / `POST /wio/query`

Static read-only review of the actual source in `app/`, verified against current code rather than `CLAUDE.md`'s narrative. No files edited, no tests/server run.

## 1. End-to-end flow map, file by file

| Stage | File(s) : Function(s) | Trusts from upstream | Produces for downstream |
|---|---|---|---|
| HTTP entry / size & rate gate | `app/main.py:RequestIDMiddleware.dispatch` | `request.client.host`, `Content-Length` header | request ID, 413/429 short-circuit |
| Route validation | `app/schemas/api.py:QueryRequestV1/LocationInput` (Pydantic) | raw JSON body | typed `req` (question ≤4096 chars, lat/lon bounded, `session_id`/`user_id` ≤128 chars) |
| Cheap guardrail | `app/services/guardrail.py:check_question_fast` | `req.question` | raises 400 or passes through untouched |
| LLM query understanding | `app/services/query_extractor.py:extract_and_normalize` → `_parse`/`_deterministic_fallback` | raw question text | `NormalizedQuery{normalized_location, normalized_time, intent, is_weather_related, confidence_score, extraction_source}` |
| Topic gate on LLM output | `app/main.py:_understand_query` | `NormalizedQuery` | 400 `QUESTION_REJECTED` or the object itself |
| Follow-up short-circuit | `app/services/session_router.py:evaluate_follow_up` / `InMemorySessionStore` (`app/storage/memory.py`) | `req.session_id`, `NormalizedQuery.normalized_location` | `ResolvedContext` (cached lat/lon/tz/window) or `None` |
| Location resolution | `app/services/location_resolver/__init__.py:resolve_location/extract_location`, `detect.py`, `normalize.py`, `cache.py`, `providers/{open_meteo,nominatim,india_post}.py`, `ranking.py`, `seed.py` | `req.location` or LLM-extracted phrase | `ResolvedLocation{lat, lon, timezone, confidence,…}` |
| Time parsing | `app/services/time_parser.py:parse_time_window` | LLM `normalized_time` or raw question, location tz | `(valid_from, valid_to, horizon, time_confidence)` |
| Follow-up store | `session_router.store_context` | resolved location + window | persisted `ResolvedContext` keyed by `session_id` |
| Retrieval planning | `app/orchestrator/retrieval_planner.py:build_retrieval_plan` | question text, horizon | `RetrievalPlan{variables, sources, decision_context,…}` — deterministic, LLM never touches this |
| Retrieval | `app/services/retrieval.py:retrieve/_one/_fetch_with_retry`, `CircuitBreaker`, `app/adapters/*.py .fetch/.normalize`, `app/decoders/*.py` | lat/lon, plan, window | `list[CanonicalEvidenceObject]` + per-source `retrieval_status` |
| Window filter | `app/services/temporal_align.py:filter_by_window` | CEO list, UTC window | CEOs overlapping the window |
| Semantic gate | `app/services/semantic_gate.py:validated_evidence` → `app/services/variable_registry.py:validate_semantics` | CEO list | accepted CEOs + rejection reasons |
| Evidence index | `app/services/evidence_store.py:EvidenceStore.add_many` | accepted CEOs | process-local, TTL/LRU-bounded store backing `GET /evidence/{id}` |
| Fusion | `app/services/wio_builder.py:build_wio` → `ranker.py:rank/corroborated/detect_disagreements`, `spatial_match.py:covers_query`, `units.py:as_kmh` | CEOs, resolved location, window | `WeatherIntelligenceObject` (rain/temperature/wind panels, agreement, warnings, evidence summaries) |
| User profile merge | `app/main.py:_weather_request` → `app/context/store.py:get_context` (via `app/storage/sqlite.py:SqliteMemoryStore`) | `req.user_id`, `req.profile` | merged `profile` dict |
| Decision (RADE) | `app/rade/v2.py:decide` | WIO, profile, decision_context | `DecisionResult` |
| Agents | `app/agents/orchestrator.py:run_all_agents` (forecast/warning/historical/observation/context/decision/explanation/reviewer) | WIO, CEOs, profile, decision | `list[AgentResult]` |
| Reviewer gate | `app/agents/verification.py:verify_claim/check_prose_grounding` (invoked from `run_reviewer_agent`) | every `Claim.extra["derivation"]` + cited CEOs | errors → 503; warnings pass through |
| Synthesis | `app/main.py:_synthesize` | WIO, decision, agents | final answer string |
| Response | `app/main.py:query_v1/wio_query_v1` | everything above | JSON response |

## 2. Vulnerabilities and correctness faults, per stage

### 2.1 Authorization — no binding between `user_id`/`session_id` and any identity (Critical)

`user_id` and `session_id` are free client-supplied strings (`app/schemas/api.py:24-31`, ≤128 chars, no format constraint) with **no authentication anywhere in the app** — there is no auth middleware, no API key check, nothing. Consequences, all confirmed in code:

- **Cross-user data readback.** `app/main.py:_weather_request` (line 229-231):
  ```python
  profile = dict(req.profile)
  if req.user_id:
      profile.update({key: value["value"] for key, value in memory_store.get_context(req.user_id).items() if key not in profile})
  ```
  `memory_store.get_context(user_id)` (`app/context/store.py:83`) returns every non-expired fact stored for that `user_id` via `POST /context`. This merged `profile` is passed to `run_context_agent` (`app/agents/orchestrator.py:22-29`), which turns up to 5 of those facts into `Claim(claim=f"context.{k}", value=v, ...)` — and these claims are returned verbatim in the `agents` field of `/query` and `/wio/query` responses. **Any client that knows or guesses another user's `user_id` can read that user's stored context facts back out, by simply issuing a normal weather query with `user_id` set to the victim's ID.** Iterating requests with different `profile` overrides (to force different facts to "win" the `key not in profile` merge) allows enumerating more than 5 facts.
- **Cross-user data poisoning.** `POST /context` (`app/main.py:324-329`) accepts any `user_id` and unconditionally overwrites (`INSERT OR REPLACE`, `app/context/store.py:69-80`) that user's fact — no ownership check, no confirmation. An attacker can silently rewrite a victim's stored `risk_tolerance` (consumed by `app/rade/v2.py:108-109`, directly changing the RADE risk-aversion multiplier) or any other fact.
- **Cross-user follow-up hijack.** `session_id` gates `session_router.evaluate_follow_up`/`store_context` (`app/services/session_router.py`) the same way — anyone guessing/reusing a `session_id` inherits or overwrites that conversation's resolved location/time window.
- **Feedback spoofing.** `POST /feedback` similarly writes to any `user_id`'s feedback log.

This is a textbook IDOR/broken-access-control chain and is by far the most serious finding in the codebase — everything else here is defense-in-depth or resource-hardening by comparison.

### 2.2 `app/services/query_extractor.py` prompt injection — blast radius assessment

The system prompt (`_SYSTEM_PROMPT`, lines 24-52) is placed ahead of raw user text in the same chat call, and `check_question_fast` (the only pre-LLM gate) does *not* check topic relevance — only length/control-chars/known-injection-phrase-regex/URLs. A crafted question that avoids the `_INJECTION` regex's specific phrasings (e.g. avoids "ignore previous", "system prompt", "you are now", triple backticks, `{{`, `${`, a leading `role:` line) could still plausibly manipulate the small LLM into emitting an attacker-chosen JSON payload for `normalized_location`, `normalized_time`, `is_weather_related=true`, `confidence_score=1.0`.

Actual blast radius, verified against every downstream consumer of `NormalizedQuery`:
- `normalized_location` → fed only into `location_resolver.resolve_location()` (`app/main.py:148`), which hands it to `OpenMeteoGeocoder`/`NominatimGeocoder` as an httpx `params=` value (URL-encoded, not string-concatenated) or matched against the 6-digit PIN regex. No SSRF (hosts are hardcoded), no SQLi (no SQL here), no path traversal. Worst case: wasted upstream geocoding calls, a `LOCATION_NOT_FOUND`/`LOCATION_AMBIGUOUS` response, or resolving to an attacker-chosen *real* place (nuisance, not a security boundary break). **`NormalizedQuery.normalized_location` has no `max_length`** (`app/schemas/query.py:28`) — unlike every other user-facing string field in the codebase — so an attacker-lengthened LLM output could produce an oversized geocoding query and an oversized location-cache key; bounded in practice only by the LLM's `max_tokens=200` cap in `extract_and_normalize` (`app/services/query_extractor.py:117-121`).
- `normalized_time` → fed into `parse_time_window`, pure regex parsing, no `eval`, no code execution. Worst case is a mis-parsed date (wrong horizon → wrong source selection), never a crash the code doesn't already guard (`ValueError` is caught, falls back to whole-day window).
- `intent`/`is_weather_related`/`confidence_score` → gate whether the request proceeds at all (`_understand_query`, `app/main.py:170-174`). A successful injection here just lets an off-topic question through the topic filter — i.e. it defeats the *guardrail's purpose* (burning upstream weather-API quota on non-weather questions) but originates no false weather data, since RADE/WIO/reviewer are all downstream and independently grounded in retrieved evidence.

**Conclusion:** the injection surface is real (no schema/prompt hardening beyond a keyword regex), but the actual exploitable outcome is topic-gate bypass / quota waste, not data fabrication or code execution — the LLM's output never reaches a shell, a SQL query, a raw URL host, or a Claim's derivation (see 2.4). This matches the intent described in `app/llm/client.py`'s docstring ("never selects a source... never resolves a coordinate") and holds up under inspection.

### 2.3 Nominatim global throttle is a cross-tenant DoS knob (Medium)

`NominatimGeocoder._lock`/`_last_request_at` (`app/services/location_resolver/providers/nominatim.py:32-33`) are **class-level** attributes — one shared 1-req/sec throttle across the *entire process*, for every concurrent request from every user. This is correct for OSM fair-use, but it means: any client sending several queries with place names that Open-Meteo's geocoder doesn't resolve (forcing the Nominatim fallback — trivially achievable with any small town, colloquial alias, or misspelling) can serialize the location-resolution path for *all other concurrent users* to 1/sec. Combined with 2.1 (no auth), this is an easy, cheap, unauthenticated latency-DoS lever with no rate-limit exemption protecting it specifically (only the general 30/min IP limit applies, which one client alone can exhaust the shared resource well within).

### 2.4 Reviewer/verification gate — unrecognized derivation is a warning, confirmed still true, not currently exploitable

Confirmed in `app/agents/verification.py:verify_claim` (line 86-87): any `derivation["op"]` outside `{"sum","max","min","identity","none"}` returns `([], [warning])` — a **warning**, not an error, and `run_reviewer_agent` (`app/agents/orchestrator.py:131`) only flips `status="partial"` on a non-empty **errors** list, not warnings. So yes, an unrecognized claim shape passes review with an unverified numeric value attached.

Is it exploitable today? No — traced every call site: every `Claim` built in `app/agents/orchestrator.py` sets `extra={"derivation": ...}` to one of a fixed, hardcoded set of dicts (`_NOT_MEASURED = {"op": "none"}`, or `{"op": "sum"/"max"/"identity", ...}` built from panel dicts the deterministic fusion code produced). **The one LLM-authored claim (`explanation`) always uses `_NOT_MEASURED` and is routed through `check_prose_grounding` instead of `verify_claim`** (special-cased at `orchestrator.py:112`, never reaches `verify_claim`). So there is currently no code path where attacker-influenced text determines `derivation["op"]`. This is a real but *latent* gap: the safety margin depends entirely on every future agent continuing to hardcode correct derivations — nothing in the type system enforces that `op` is one of the recognized values, and a typo'd `op` string in a future agent would silently degrade from a 503-blocking gate to a logged warning.

### 2.5 `check_prose_grounding` unit-regex bypass (Medium)

`_QUANTITY`/`_UNIT_ALIASES` in `app/agents/verification.py:100-104` only recognize `mm`, `%`, `C`/`°C`/`celsius`, and `km/h`/`kmph`/`kph`. The explanation system prompt (`_EXPLANATION_SYSTEM`, `orchestrator.py:134-140`) forbids introducing any number, but the grounding check that's supposed to *enforce* that is unit-blind outside those four forms: a hallucinated `"50 mph winds"`, `"41°F"`, `"2 inches of rain"`, or a bare unitless number (`"expect around 50"`) is never matched by `_QUANTITY` and passes through ungrounded, uncaught. This only matters if the explanation LLM (Groq, off by default per `settings.llm_enabled`) is actually compromised or badly hallucinates in a unit the regex doesn't cover — the system prompt is the only defense in that specific case, since the "gate" silently no-ops for those units.

### 2.6 Request-size guard is trivially bypassable (Medium)

`RequestIDMiddleware.dispatch` (`app/main.py:81-83`) checks `request.headers.get("content-length")`; if the header is absent (e.g., chunked transfer-encoding, or a client that simply omits it) `declared` is `None` and the 413 check is skipped entirely — nothing downstream re-checks actual bytes read. `request_max_bytes` defaults to 64KB, but it's enforced only against a client-supplied header value, not the real stream.

### 2.7 `POST /context` fact storage has no per-user/global bound (Medium)

`ContextFactInput.value: Any` (`app/schemas/api.py:43`) has no size limit (only `fact` is capped at 64 chars); `user_context.upsert_fact` writes `str(value)` to SQLite with `PRIMARY KEY (user_id, fact)` — an attacker can create unbounded distinct `fact` names per `user_id` (no cap on fact *count*) each carrying an arbitrarily large `value` (bounded only by the weakly-enforced 64KB request-size check in 2.6). Combined with 2.1 (unauthenticated `user_id`), this is unbounded, attacker-controlled, permanent disk growth in `weathergpt.db`.

### 2.8 Synchronous SQLite I/O on the event loop (Medium, perf/DoS-adjacent)

`app/context/store.py:_connect/upsert_fact/get_context` and `app/storage/sqlite.py:SqliteConversationLog` all do blocking `sqlite3` calls directly inside `async def` request handlers, with no `run_in_threadpool`. Every `/query` call with a `user_id` set (`memory_store.get_context`), and every `/context`/`/feedback` call, blocks the single-process event loop for the duration of a disk write/read. Under concurrent load, this serializes all requests behind SQLite I/O — a cheap amplifiable slowdown for every other in-flight request, and it defeats the async concurrency the rest of the pipeline (`asyncio.gather` in retrieval, `run_all_agents`) is built for.

### 2.9 `/health` is unauthenticated, unrated, and fans out 8 upstream calls (Medium — self-documented)

`app/adapters/registry.py:health_all` is explicitly exempted from rate limiting (`app/main.py:84`) and, per its own docstring, costs ~11.5s of upstream round-trips run concurrently across all 8 registered adapters (`OPEN_METEO, ERA5, GEFS, CAP, NASA_POWER, IMD, GFS, MET_NORWAY`) — several of which (`ImdAdapter.health_check`) perform a full authenticated `fetch()` when a key is configured. An unauthenticated client can hammer `/health` with no rate limit, holding worker capacity and generating repeated traffic against third-party free APIs (Open-Meteo, MET Norway, NASA POWER, OSM-adjacent, India Post) under this server's identity — a real amplification/reputation-risk DoS vector, and one the code comment already flags as a known tradeoff without a fix.

### 2.10 CAP feed → SSRF-shaped fetch of feed-controlled URLs (Low, supply-chain-gated)

`CapAdapter.fetch` (`app/adapters/cap_adapter.py:33-49`) parses `<item><link>` elements out of a trusted, server-configured `settings.cap_feed_url`, then does `client.get(link)` for each — with `follow_redirects=True` (`app/adapters/http.py:24`) and no scheme/host allowlist. Not user-reachable directly, but if the CAP feed (an S3-hosted XML index, `cap-sources.s3.amazonaws.com`) is ever compromised or MITM'd, this adapter will fetch and parse whatever URLs the feed contains, including internal/metadata addresses, with no validation. `xml.etree.ElementTree.fromstring` is also used on both the index and every linked alert document (`cap_adapter.py:54`, `cap_decoder.py:70`) without `defusedxml`; stdlib `ElementTree` is not hardened against entity-expansion ("billion laughs") — a compromised feed could trigger memory exhaustion. Low likelihood (requires feed compromise), but zero defense-in-depth currently exists against it.

### 2.11 Error detail exposure — minor, contained

`retrieval.py:_one` puts raw `f"{type(exc).__name__}: {exc}"` into `status["sources"][source]["error"]` (line 159), which flows into the client-visible `retrieval_status` on every response. This is nearly always just an upstream HTTP error string (safe), but it is unfiltered exception text, not a curated message — worth normalizing if any adapter ever raises something carrying a credential or internal path in its `str()`. The `WeatherGPTError`/unhandled-exception handlers in `main.py` are otherwise clean: generic "Internal server error" on 500s, full traceback only server-side via `logger.exception`.

### 2.12 Concurrency / shared mutable state — reviewed, mostly sound

- `TTLCache`, `LocationCache`, `InMemorySessionStore`, `EvidenceStore` are all correctly `asyncio.Lock`-guarded (cache) or single-threaded-safe (evidence store, only ever mutated from the event loop, no `await` mid-mutation).
- `RateLimiter.check` and `CircuitBreaker` are plain sync dict mutation with no lock, but since neither method awaits mid-body, there's no interleaving hazard *within a single worker* under asyncio's cooperative scheduling — this is fine as-is. It's still per-process state (documented limitation, see §3), so it doesn't survive multi-worker deployment, but that's not a race, it's a sharding gap.
- No use of `multiprocessing`/threads elsewhere that could actually race on these structures.

### 2.13 Input validation — largely solid

`LocationInput.latitude/longitude` are range-validated by Pydantic (`ge=-90/le=90`, `ge=-180/le=180`); `detect.parse_coordinates` double-checks range again. All outbound adapter URLs are hardcoded constants with `params=` dict (auto URL-encoded by httpx) — no string-interpolated user input into a URL host or path anywhere except India Post's `f"{LOOKUP_URL}{pincode}"`, where `pincode` is pre-validated by a `^\d{6}$`-shaped regex (`detect.parse_pincode`) before it ever reaches the adapter. All SQL in `context/store.py`/`sqlite.py` is parameterized (`?` placeholders) — no SQL injection found anywhere.

## 3. Known limitations (confirmed, not newly discovered unless marked)

- Single-process in-memory caches/session-store/rate-limiter/circuit-breaker/evidence-store don't survive a restart or share state across workers — already documented in CLAUDE.md (`docs/SERVICES.md`, session_router.py's own docstring). Confirmed still accurate for every module inspected (`cache.py`, `rate_limit.py`, `retrieval.py:CircuitBreaker`, `session_router.py`, `evidence_store.py`).
- `app/services/model_client.py` (bias-correction integration) does not exist — confirmed, no such file; `/health`'s `models.bias_correction` string still reports it honestly.
- IMD adapter blocked on `IMD_API_KEY` — confirmed (`app/adapters/imd_adapter.py:19-20`).
- GFS (`Grib2Adapter`) needs `requirements-full.txt` — not independently re-verified beyond confirming the adapter exists and is registered; out of scope for this pass (no eccodes/cfgrib import inspected).
- Rate limiting is per-IP (`request.client.host`) and spoofable/collapsible behind a proxy — confirmed and already documented in CLAUDE.md's "Next steps" §4.
- **New, not previously documented:** `user_id`/`session_id` carry zero authentication anywhere in the app (§2.1) — this is a materially different and more serious gap than the documented IP-rate-limit spoofing note, and CLAUDE.md does not mention it.
- **New:** synchronous SQLite calls on the event loop (§2.8) are a latent throughput ceiling that will get worse, not better, as `/context`/`/feedback` traffic grows — not mentioned in CLAUDE.md's hardening summary, which focused on the weather-retrieval path.
- **New:** `NormalizedQuery.normalized_location` has no `max_length`, inconsistent with every other user-facing string field in the schemas (§2.2).
- **New:** the Nominatim class-level throttle is a single shared bottleneck across all tenants (§2.3), not called out anywhere as a cross-request risk (only documented as OSM fair-use compliance).

## 4. Prioritized punch list

**Critical**
1. **Unauthenticated `user_id`/`session_id` allow reading and overwriting any other user's stored context facts** (§2.1) — `POST /context`, the `/query` context-claim readback, and `session_router` all trust a bare client-supplied string as an identity. Fix direction: require a server-issued/authenticated session token (API key, JWT, or at minimum HMAC-signed opaque session id) and scope all `memory_store`/`session_router` lookups to the authenticated principal, not the request body field.

**High**
2. **`/health` unauthenticated, unrated, 8-way upstream fan-out** (§2.9) — trivial amplification/DoS against this service and third-party free APIs. Fix: rate-limit `/health` like any other route, or serve a cached/short-TTL health snapshot instead of live-probing every adapter per request.
3. **Request-size limit bypassable via missing `Content-Length`** (§2.6) — combined with §2.7's unbounded fact storage, this is an easy disk/memory exhaustion path. Fix: enforce a hard body-read cap (e.g., ASGI-level `max_upload_size` or manual streaming cap), not a header-trust check.

**Medium**
4. **`POST /context` has no per-user fact-count or value-size limit** (§2.7) — unbounded SQLite growth. Fix: cap distinct facts per `user_id` and `len(str(value))`.
5. **Synchronous SQLite calls block the event loop** (§2.8) — throughput ceiling shared across all concurrent requests. Fix: wrap `context.store` calls in `anyio.to_thread.run_sync`/`run_in_threadpool`, or move to an async SQLite driver.
6. **Nominatim's class-level 1 req/sec throttle is a shared cross-tenant bottleneck** (§2.3) — one client can serialize location resolution for everyone. Fix: per-client or queued-with-timeout throttling, or move the fair-use pacing behind a bounded queue that fails fast rather than blocking indefinitely.
7. **`check_prose_grounding` only recognizes 4 unit spellings; other units/unitless numbers bypass the anti-hallucination check entirely** (§2.5) — meaningful only if the (currently-off-by-default) explanation LLM misbehaves. Fix: widen the unit regex (mph, inches, °F, in/hr) or reject prose containing *any* numeral not traceable to the fact sheet, rather than allow-listing units.
8. **CAP adapter follows feed-supplied URLs with no host allowlist, using non-hardened XML parsing** (§2.10) — low-likelihood, supply-chain-gated SSRF/XXE-adjacent exposure. Fix: restrict alert-link fetches to the feed's own host/known CDN, and parse with `defusedxml`.
9. **`Claim.extra["derivation"]` has no schema-level enforcement that `op` is one of the recognized values** (§2.4) — currently unreachable by user input, but a silent safety-margin gap for future agents. Fix: make `op` a validated Literal/Enum on the `Claim` model (or a dedicated `Derivation` model) so an unrecognized shape fails fast in code review/tests rather than degrading to a warning at runtime.

**Low**
10. **`NormalizedQuery.normalized_location` has no `max_length`** (§2.2) — inconsistent with the rest of the schema; bounded in practice by LLM `max_tokens` but worth closing for defense-in-depth. Fix: add `max_length` matching `LocationInput.raw` (256).
11. **Raw exception text (`f"{type(exc).__name__}: {exc}"`) surfaces in `retrieval_status`** (§2.11) — minor internal-detail leak, generally benign (upstream HTTP errors) but uncurated. Fix: map exception types to a small set of user-safe reason strings before returning.

---

Files most relevant to reproduce or extend this audit: `app/main.py`, `app/services/query_extractor.py`, `app/schemas/query.py`, `app/services/session_router.py`, `app/storage/memory.py`, `app/context/store.py`, `app/storage/sqlite.py`, `app/schemas/api.py`, `app/services/rate_limit.py`, `app/services/cache.py`, `app/adapters/registry.py`, `app/adapters/cap_adapter.py`, `app/decoders/cap_decoder.py`, `app/agents/verification.py`, `app/agents/orchestrator.py`, `app/services/location_resolver/providers/nominatim.py`.
