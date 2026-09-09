# WeatherGPT

An evidence-grounded weather-intelligence backend, not a "call an LLM with a weather
prompt" system. Numbers always come from deterministic pipelines — an LLM (when configured)
only ever explains or reasons over data that's already been fetched, validated, and fused;
it never selects sources, never invents a value, and never bypasses evidence citation.

Full architecture detail: [`docs/architecture.md`](docs/architecture.md) (diagram),
[`docs/SERVICES.md`](docs/SERVICES.md) (every service, plain language),
[`docs/REPORT.md`](docs/REPORT.md) (current status, defect history, roadmap).

## System architecture

```
POST /query
    │
    ▼
guardrail (1 LLM call: topic + language + intent, session-aware)
    │  reject/clarify/verify → fixed template response, return now
    ▼
location resolver  →  time parser  →  retrieval plan (deterministic)
    │
    ▼
concurrent fetch across sources (Open-Meteo, MET Norway, CAP warnings,
GFS, marine, IMD when configured, ...) → per-source isolated failure
    │
    ▼
fusion: rank sources, detect disagreement, build one WeatherIntelligenceObject
    │
    ▼
deterministic agents (forecast/warning/historical/decision) + optional
LLM explanation, prose-checked against the fused numbers
    │
    ▼
RADE (risk-aware decision engine) when the question is a decision, e.g.
"should I spray tomorrow" — expected-utility scoring, never a guess
    │
    ▼
response: answer text + full evidence trail + machine-readable WIO
```

## Run it — Docker (recommended)

```bash
git clone <this-repo> && cd weathergpt
cp .env.example .env        # fill in at minimum WEATHERGPT_API_KEYS — see "Configuration" below
docker compose up --build
```

Exposes port **8001**. The image installs `requirements-full.txt` (adds GRIB2/GFS decoding)
plus the system `libeccodes0`/`libeccodes-data` packages that need — GFS works out of the
box in the container.

```bash
curl -s http://localhost:8001/health | python3 -m json.tool
```

## Run it — bare Python (development)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-api.txt      # API + test suite; add requirements-full.txt for GFS/GRIB2 locally
uvicorn app.main:app --host 0.0.0.0 --port 8001
pytest -q
```

## Configuration

Copy `.env.example` to `.env` and fill in what you need — every setting is documented
inline there. Minimum for a real deployment:

| Variable | Why |
|---|---|
| `WEATHERGPT_API_KEYS` | Comma-separated bearer keys. **Empty disables the auth gate entirely** — always set this outside local dev. |
| `WEATHERGPT_CORS_ORIGINS` | Required or CORS middleware is never installed; a browser client calling this directly needs it. |
| `WEATHERGPT_MET_NORWAY_USER_AGENT` | Must identify your deployment or `api.met.no` returns 403. |

Everything else (`GEOAPIFY_API_KEY`, `SMALL_LLM_*`/`BIG_LLM_*`, `STORMGLASS_API_KEY`,
`IMD_API_KEY`, `CAP_FEED_URL`, ...) is optional — each source or LLM tier degrades cleanly
when unconfigured (a source just doesn't get fetched; the LLM explanation falls back to a
fully deterministic template answer). See `docs/REPORT.md` for what's currently
blocked/unconfigured and why.

Run **one** uvicorn worker (the default) — in-process caches and the evidence store are
per-process until they move to Redis.

## Deploying to EC2 (scp/rsync + Docker + systemd)

Condensed path; full detail (security groups, systemd unit, key rotation notes) is in
[`docs/AWS.md`](docs/AWS.md).

```bash
# 1. Launch Ubuntu 22.04/24.04, t3.small+ (this app is I/O-bound, not CPU-bound).
#    Security group: inbound 22 (your IP) + 8001 (or put a reverse proxy on 443 in front).

# 2. Install Docker on the instance
ssh -i <KEY.pem> ubuntu@<EC2_IP>
sudo apt-get update && sudo apt-get install -y ca-certificates curl gnupg
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update && sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
sudo usermod -aG docker ubuntu   # log out/in after this

# 3. Copy the repo up (from your LOCAL machine) — rsync so you control what gets excluded
ssh -i <KEY.pem> ubuntu@<EC2_IP> "sudo mkdir -p /opt/weathergpt && sudo chown ubuntu:ubuntu /opt/weathergpt"
rsync -avz -e "ssh -i <KEY.pem>" --exclude='.venv' --exclude='.git' --exclude='__pycache__' \
  ~/weathergpt/ ubuntu@<EC2_IP>:/opt/weathergpt/
# plain `scp -r` also works but has no --exclude — it will drag your local .env along too,
# which may be exactly what you want (production keys) or exactly what you don't (dev keys)

# 4. Set production .env directly on the instance (see .env.example for every key)
ssh -i <KEY.pem> ubuntu@<EC2_IP> "cd /opt/weathergpt && nano .env"

# 5. Build and start
ssh -i <KEY.pem> ubuntu@<EC2_IP> "cd /opt/weathergpt && docker compose up -d --build"
curl -s http://<EC2_IP>:8001/health | python3 -m json.tool

# 6. Survive reboots
ssh -i <KEY.pem> ubuntu@<EC2_IP> "sudo cp /opt/weathergpt/weathergpt.service /etc/systemd/system/ && \
  sudo systemctl daemon-reload && sudo systemctl enable --now weathergpt"
```

Generate an API key for a caller: `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`,
add it to `WEATHERGPT_API_KEYS` in `.env`, `sudo systemctl restart weathergpt`.

## Integrating with your app or website

Base URL is wherever you deployed it (`http://<EC2_IP>:8001` or your domain). Every
request needs `Authorization: Bearer <your-key>` once `WEATHERGPT_API_KEYS` is set.

**Single question:**

```bash
curl -X POST http://localhost:8001/query \
  -H 'Authorization: Bearer <your-key>' \
  -H 'Content-Type: application/json' \
  -d '{"question": "will it rain in Indore tomorrow afternoon?"}'
```

```js
// Minimal fetch example — any frontend/backend that speaks HTTP works the same way
const res = await fetch("http://localhost:8001/query", {
  method: "POST",
  headers: {
    "Authorization": "Bearer <your-key>",
    "Content-Type": "application/json",
  },
  body: JSON.stringify({ question: "will it rain in Indore tomorrow afternoon?" }),
});
const data = await res.json();
console.log(data.answer);   // the synthesized answer text
console.log(data.wio);      // the full machine-readable evidence-grounded object
```

**Multi-turn / chat-style integration** — pass a stable `session_id` per conversation so
follow-ups ("what about tomorrow?", "should I bring an umbrella then?") resolve against
the same location/time context instead of re-asking:

```json
{"question": "weather in Pune", "session_id": "user-42-conversation-7"}
```

```json
{"question": "and tomorrow?", "session_id": "user-42-conversation-7"}
```

The response's `answer` field is always safe to show directly to an end user (it's
grounded and reviewer-checked). `wio`/`evidence`/`comparisons` are there if your
application wants to build its own UI on the structured data instead of the prose.

## API reference

All request bodies are Pydantic-validated. Errors use
`{"error": {"code", "message", "details", "request_id"}}`.

| Endpoint | Purpose |
|---|---|
| `GET /health` | Liveness, readiness, per-source configuration status. |
| `POST /wio/query` | Validated evidence retrieval and fused WIO only, no synthesized answer text. |
| `POST /query` | WIO plus deterministic, evidence-grounded synthesized answer — the main endpoint. |
| `POST /decision`, `POST /rade/advise` | WIO plus a RADE risk-aware decision (spray/travel/marine/etc.). |
| `GET /evidence/{id}` | A specific evidence record generated in this running process. |
| `GET /forecast?location=...` | Convenience WIO forecast view. |
| `GET /warnings/active?location=...` | Active official warnings for a location. |
| `POST /context`, `POST /feedback` | User-scoped stored context and outcome feedback. |
| `GET /metrics` | Process metrics and cache hit rate. |

The equivalent `/api/v1/...` routes exist for every endpoint above where listed by
OpenAPI (visit `/docs` on a running instance). Location must be supplied explicitly or
occur unambiguously in the question — an ambiguous location returns a structured `409`
with numbered candidates, never a silent guess.

## Notes

- User context and feedback are `user_id`-scoped; no endpoint enumerates another user's
  stored data.
- The LLM explanation layer is entirely optional — off by default, and the answer is
  fully template-built (deterministic, still evidence-grounded) when it's not configured.
- The checked-in dataset/model claims mentioned in `docs/REPORT.md` are historical —
  ML training happens in a separate repo; this repo owns only the inference-serving
  integration path.

See [`CLAUDE.md`](CLAUDE.md) for the full coding rules and detailed session-by-session
history, and [`docs/REPORT.md`](docs/REPORT.md) for the current "what's open" index.
