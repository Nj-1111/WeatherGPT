# Architecture

Request pipeline for `POST /query` / `POST /wio/query` (`app/main.py:_weather_request`).
Plain-language walkthrough of every box is in [SERVICES.md](SERVICES.md); this is the
shape of it. Numbers come from deterministic pipelines only — the one LLM box that can
influence dispatch (the guardrail) never originates a value, and the one LLM box that
writes prose (explanation) runs after fusion and is checked against it before it ships.

```mermaid
flowchart TD
    Q["POST /query"] --> G{{"query_guardrail.run_guardrail\n(1 LLM call → GuardrailAction)"}}

    G -- "REJECT_OFF_TOPIC / CLARIFY\nVERIFY / UNSUPPORTED_TOPIC" --> T["fixed template message\nreturn now"]
    G -- "ACCEPT_LOCATION_ONLY" --> LOC1["location_resolver only"] --> R1["return now\nno retrieval/fusion/agents/RADE"]
    G -- "ACCEPT_WEATHER_FULL" --> LOC["location_resolver\n(coords → cache → PIN → providers → rank → ambiguity)"]

    LOC --> TIME["time_parser\n(resolves in the location's own timezone)"]
    TIME --> PLAN["retrieval_planner\n(deterministic — LLM never picks sources)"]
    PLAN --> RET["retrieval.retrieve\n(concurrent per-source fetch + cache,\ncircuit breaker, isolated failures)"]

    subgraph SRC ["adapters/*.py .fetch() → decoders/*.py"]
        direction LR
        S1["Open-Meteo\nforecast/ensemble/marine"]
        S2["ERA5 / NASA POWER\n(historical)"]
        S3["CAP\n(NDMA warnings)"]
        S4["MET Norway"]
        S5["IMD / GFS / StormGlass\n(gated on credentials)"]
    end

    RET --> SRC
    SRC --> CEO["CanonicalEvidenceObject list\n(variable/statistic/unit/window, never mixed)"]

    CEO --> WIN["temporal_align.filter_by_window"]
    WIN --> GATE["semantic_gate.validated_evidence"]
    GATE --> WIO["wio_builder.build_wio\n(ranker.rank + spatial_match + detect_disagreements)"]
    WIO --> OBJ[("WeatherIntelligenceObject\nrain / temperature / wind / marine panels\nagreement + disagreements + evidence[]")]

    OBJ --> AGENTS["agents.orchestrator.run_all_agents"]

    subgraph AG ["7 deterministic agents + 1 LLM seam"]
        direction LR
        A1["forecast / warning\nhistorical / observation"]
        A2["context / decision"]
        A3["reviewer\n(hard gate — recomputes\nevery claim from cited evidence)"]
        A4["explanation\n(LLM prose only,\ngrounded against WIO,\noff by default)"]
    end

    AGENTS --> AG
    OBJ --> RADE["rade.v2.decide\n(only when a decision context is present)"]

    AG --> SYN["main._synthesize\n(template-built answer string)"]
    RADE --> SYN
    SYN --> RESP["JSON response"]
```

## Reading it

- **Guardrail is a decision, not a filter.** `GuardrailAction` is a real dispatch key —
  `ACCEPT_LOCATION_ONLY` skips retrieval/fusion/agents/RADE entirely (~1s instead of the
  full pipeline); everything left of "ACCEPT_WEATHER_FULL" returns a fixed template
  string, never LLM-composed prose.
- **The reviewer is the anti-hallucination gate.** Every numeric claim declares how it was
  derived (`sum`/`max`/`min`/`identity`/`none`); the reviewer re-runs that derivation over
  the evidence the claim actually cites and 503s the whole request on a mismatch — a claim
  citing real evidence can still fail review if the *value* attached to it is wrong.
- **Two sources are never averaged.** Fusion (`wio_builder.build_wio`) keeps every
  surviving CEO in `evidence[]`, presents the highest-ranked value per panel, and only
  ever *flags* disagreement — it never blends a government warning into a numeric mean.
- **The explanation LLM is inert by default** (`WEATHERGPT_LLM_ENABLED=false`), and even
  when on, writes prose *after* fusion, checked by the same reviewer gate — an ungrounded
  number in its output suppresses the explanation rather than reaching the client.
