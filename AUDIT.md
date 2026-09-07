# AUDIT.md — pre-production teardown, 2026-09-05

**Current open/closed status for every finding below is tracked in `FIXES.md`** — this
file is the detailed narrative behind each one, not the live index.

Scope: `app/`, against the coding rules in `CLAUDE.md`. Storage (`app/storage/**`) excluded
by request. `BUG.md`'s existing findings are not repeated here. Everything below was read or
executed, never inferred; anything unverified says so.

## A. Verdict

**Do not ship at the time of the audit.** Fusion counted Open-Meteo's ensemble endpoint as a
second independent source against Open-Meteo's own forecast endpoint, so `full_agreement` was
produced by one vendor agreeing with itself and an ensemble's internal spread was reported as
a between-source disagreement. That value drives RADE confidence and the big-LLM trigger, so
the flagship decision path was the worst affected. **A1-A6 are now fixed** (see `CLAUDE.md`'s
2026-09-05 session record); A7-A9 remain open, A10 verified with no defect found (below).

## B. Findings

| ID | Sev | Location | Defect | State |
|---|---|---|---|---|
| A1 | CRITICAL | `ranker.py:82` | Ensemble members bucketed with deterministic rows, so one vendor corroborated itself and its own spread read as disagreement | **fixed** |
| A2 | LOW (was CRITICAL) | `open_meteo_ensemble.py:18`, `ranker.py:18` | `GEFS` labels an Open-Meteo endpoint; authority 0.72 outranked `OPEN_METEO`'s 0.70 | **fixed (reduced)** |
| A3 | HIGH | `rade/v2.py:121` | `single_source` scored 0.8, identical to `full_agreement` — corroboration bought nothing | **fixed** |
| A4 | HIGH | `query_guardrail.py:185` | Guardrail LLM ran on every request, uncached, ahead of location resolution | **fixed** |
| A5 | HIGH | `retrieval.py:120`, `config.py:67` | 3 attempts x 20s per source, no total budget: 61.2s per request on one slow source | **fixed** |
| A6 | HIGH | `orchestrator.py:144` | Raw user question interpolated into the explanation model's prompt | **fixed** |
| A7 | MED | `config.py:197` | `query_understanding_confidence_threshold` has zero readers; its comment claims it gates low-confidence classifications | open |
| A8 | MED | `wio_builder.py:83` | Rain panel reports the window's peak probability but cites `probabilities[0]` | open |
| A9 | MED | `main.py:75` | Reviewer rejections pooled with all 5xx; `wio_latency_ms_mean` divides by a count omitting two endpoints | open |
| A10 | MED | `llm/client.py:72` | `reasoning_format: "hidden"` sent to every endpoint, justified only by a comment | **verified 2026-09-07, no defect** — see note |

### A10 verification (2026-09-07)

Live-called the configured fallback endpoint (`SMALL_LLM_FALLBACK_1_BASE_URL`, Gemini)
directly with `reasoning_format: "hidden"`: it does not reject the field — HTTP 200,
answered normally. No defect in what A10 asked about. A related but distinct problem was
found in the same testing session and fixed: `reasoning_effort: "low"` (added separately,
for the guardrail call's `max_tokens` budget on the *primary* Groq endpoint) also applies
to this fallback endpoint, which has different reasoning-token economics — at
`max_tokens=280` it returned HTTP 200 with `finish_reason: "length"` and the JSON truncated
mid-object, a non-exceptional "success" that skipped the retry-as-failure path and instead
correctly fell through to `_parse()`'s own unparseable-JSON handling. Fixed by raising the
guardrail's shared `max_tokens` to 500 (`app/services/query_guardrail.py`); confirmed live
afterward that the fallback completes cleanly (`finish_reason: "stop"`) with headroom.

### A2 correction

Ranked CRITICAL on corroboration damage that belongs entirely to A1. Verified inert
afterwards: `_best_source` skips `ensemble_member is not None`, and `_evidence_summaries`
collapses members, so `AUTHORITY["GEFS"]` has no reachable effect on any output. Downgraded.

## C. Dead list

| Path | Symbol | Reachable | Action |
|---|---|---|---|
| `config.py` | `rank_weights_total` | no | delete |
| `config.py` | `conversation_max_turns` | no | delete |
| `config.py` | `query_understanding_confidence_threshold` | no | delete or wire (A7) |
| `.env.example`, `.env` | `HF_TOKEN`, `HF_REPO_ID` | no | delete — leftover credentials from the removed training repo |
| `app/storage` | `session_store`, `conversation_log` | no | out of scope this run; already in `BUG.md` |

Repo hygiene: no `__pycache__`, `.pyc`, `.venv` or `.env` tracked in git. There is still **no
`.dockerignore`** and `Dockerfile:11` is `COPY . .`, so `.env` is baked into an image layer
(`BUG.md` B20 — the one finding with blast radius outside this repo).

Roughly 56 `WEATHERGPT_*` variables are read by `config.py` but absent from `.env.example`.

## D. Latency

Measured live, Kolkata, 2026-09-05:

| | cold | warm |
|---|---|---|
| before this session | 3815ms | 1591ms |
| after A4/A5 | 3815ms | 529-983ms |

Best ms-saved-per-line-changed:
1. **A4 guardrail memoization** (~8 lines) — removes ~1.1s from every repeated query.
2. **A5 timeout/retry constants** (2 values) — 61.2s -> 16.4s worst case per source.
3. **A1** (1 line) — stops waking the big LLM tier on fabricated disagreements.

Still unbounded: `retrieve()`'s `asyncio.gather` has no total deadline, and the explanation
LLM call (~0.7-1.6s) runs on every request with no equivalent cache.

## E. Coding-rule violations

| Rule | Location | Violation |
|---|---|---|
| 28 | `query_guardrail.py:173` | bare `except Exception` read as "off-topic"; a bug in `check_question` becomes a user-facing rejection |
| 3, 21 | `config.py` | three settings with zero readers (section C) |
| 20 | `orchestrator.py` | `start=time.time()` spacing, single-letter names `c/w/h/k/v/r`, inconsistent with the rest of `app/` |
| 6 | `orchestrator.py:1` | module docstring claims a "4-model queue" that does not exist |
| 10 | `orchestrator.py` | `time.time()` for durations instead of `time.monotonic()` used elsewhere; a clock step yields negative `execution_time_ms` |

## F. Unfalsifiable claims

- *"Two or more sources agree on the same variable and time within tolerance."* Was produced
  by one vendor and its own ensemble (A1, fixed). **Still** produced by two sources whose day
  totals differ 2.7x — 1.4mm vs 3.8mm, live Kolkata — because the threshold is an absolute
  10mm per timestamp and cannot fire at light-rain magnitudes.
- *"Numbers come from deterministic pipelines, never from an LLM's imagination."* Holds. But
  `check_prose_grounding` only checks quantities carrying one of four unit spellings, so
  qualitative LLM statements are unconstrained (A6 closed the steering path, not this gap).
- *"`plan.variables` is applied."* True, but the plan's **source** selection was gated on
  keywords, which hid live official warnings until fixed 2026-09-05.
- *"The reviewer is a hard gate."* True for claims declaring a recognised derivation. RADE's
  `recommended_action` and `scenario_*` probabilities declare `op: "none"` and are never
  recomputed.

## G. Not verified

- GEFS ensemble decode fidelity and both marine adapters were not compared against raw APIs.
- StormGlass has never run (needs a paid key).
- Multi-worker behaviour, and any claim about behaviour behind a proxy.

## H. What breaks first in production

The retrieval fan-out. A5 bounded the per-source retry loop but not the `gather`, so the
request still waits on the slowest source, and slow is far more common than dead — the
circuit breaker only counts failures. `/health` will keep reporting that source `available`
while users receive gateway timeouts.

## Out-of-scope touchpoints

None of the findings above root in `app/storage/**`.
