# cloud.md — Outstanding Work Register

Rewritten 2026-09-03 at the end of the five-phase hardening pass, updated the same day when
the reviewer gate closed. This records what **remains**; `CLAUDE.md` records what was fixed
and `docs/SERVICES.md` explains how each service works.

Verified at time of writing: `pytest -q` 120 passed · `ruff check` clean · `mypy app`
clean · 6 of 8 sources live.

---

## 1. Blocked on credentials

| Source | Missing | Effect | Action |
|---|---|---|---|
| **IMD** (authority 0.95) | `IMD_API_KEY`, `IMD_API_BASE` | India's own met authority absent from the pipeline. The 5 decoder variants in `imd_json.py` have never run against real data. | Register at `api.imd.gov.in/public/login.php`. Uses IP whitelisting, so the EC2 elastic IP must exist first. |
| **GFS/GRIB2** | `cfgrib`/`eccodes`/`xarray` | Adapter self-reports unavailable. | `requirements-full.txt`; eccodes needs system libraries, so use the Docker path. |

CAP (authority 1.0) is now live and keyless, and carries IMD's official warnings, so the
IMD API is no longer the only route to Indian warnings — it adds forecasts, observations
and rainfall on top.

---

## 2. Reviewer gate — closed 2026-09-03

`run_reviewer_agent` now **recomputes** every claimed value from the evidence the claim
cites, instead of only checking that the cited IDs exist. Claims declare their derivation in
`Claim.extra["derivation"]`; `app/agents/verification.py` re-runs it. Citing real evidence is
no longer enough — the number attached to the citation has to be what that evidence says.

LLM prose is checked separately: every quantity carrying a unit must match something the
deterministic pipeline produced. Failure is split — a contradicted value or unknown ID is a
503; an unrecognised claim shape is a warning.

The LLM seam is wired, off by default: `run_explanation_agent` calls Groq only when
`WEATHERGPT_LLM_ENABLED=true` **and** `GROQ_API_KEY` is set, writes prose only, and degrades
to the deterministic template answer on any failure.

**What this gate still cannot do** — it confirms a claim is arithmetically faithful to the
evidence it cites, not that it cites the *right* evidence. An agent citing a real but
irrelevant CEO and reporting its value honestly passes. Relevance is currently guaranteed by
construction (claims are built from the fused panels), not by the reviewer.

**Not yet verified against a real Groq response.** No valid `GROQ_API_KEY` was available, so
the success path is covered only by tests with a stubbed client. The failure path was
verified live against api.groq.com with an invalid key: 401 → explanation `partial`, request
still 200 on the template answer.

---

## 3. Seams — intentionally empty, do not delete

| Seam | Location | How it plugs in |
|---|---|---|
| **ML bias correction** | `app/services/model_client.py` **does not exist** | Create per `model.md`; call between the semantic gate and `build_wio` in `main.py` so corrected values flow into fusion with a `transformations[]` entry. `CanonicalEvidenceObject` already carries the unused `parent_ids`/`transformation`/`transformation_timestamp`/`algorithm_version` fields for exactly this. |
| **Multilingual** | none | Transliteration in front of `location_resolver/normalize.py`, plus language routing in `time_parser`. Hindi keywords already exist in `retrieval_planner.py` and `time_parser.py`. |

---

## 4. Known limitations

- **Rate limiting keys on `request.client.host`.** Behind a load balancer or reverse proxy
  every caller presents the proxy's IP and the whole world shares one bucket. Direct-to-EC2
  is correct. `X-Forwarded-For` is deliberately not trusted — a spoofable header would
  defeat the limit entirely. Revisit if a proxy is introduced.
- **Agreement thresholds are absolute** (10mm precipitation, 3C temperature). Two sources
  agreeing on "no rain" therefore reads identically to two sources agreeing on 40mm. Not
  wrong, but weaker evidence than the label suggests. Consider a relative tolerance.
- **`historical` and `observation` agents still slice `[:2]`** off class-filtered evidence
  rather than reading ranked output. Each claim cites its own CEO so the citations are
  self-consistent; the selection is just arbitrary. The forecast agent was the one that
  cited unrelated evidence, and that is fixed.
- **Caches and the evidence store are per-process.** With multiple uvicorn workers,
  `GET /evidence/{id}` 404s whenever the follow-up lands on a different worker. Run one
  worker, or move all three to Redis. SQLite is likewise a local file and is the ceiling on
  horizontal scaling; pin systemd's `WorkingDirectory` since the path is relative.
- **Nominatim** is throttled to 1 req/s in-process. With multiple workers the aggregate can
  still exceed OSM's policy.

---

## 5. Deployment checklist

- `WEATHERGPT_CORS_ORIGINS` must be set to the web app's origin, or CORS middleware is
  never installed.
- Outbound HTTPS to 8 external hosts must be allowed by the security group.
- `WEATHERGPT_MET_NORWAY_USER_AGENT` must identify the deployment; api.met.no returns 403
  to generic User-Agents.
- Set `WEATHERGPT_LOG_JSON=true` for machine-readable logs.
- Run **one** uvicorn worker until the caches move to Redis.
- The LLM explanation is off unless `WEATHERGPT_LLM_ENABLED=true` **and** `GROQ_API_KEY` is
  set. Leaving it off is a supported configuration: the answer is then entirely template-built.
- Outbound HTTPS to `api.groq.com` is needed only when the LLM is enabled.

---

## 6. Would another language help?

No. The request is I/O-bound — 63% retrieval, 36% location resolution, under 1% compute
(measured, see `CLAUDE.md`). The only CPU-bound step is GRIB2 decoding, already native C
behind `cfgrib`. The wins were connection pooling, caching and the circuit breaker, all
pure Python, and they are done: cold 2515ms, warm 19ms.
