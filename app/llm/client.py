"""Provider-agnostic two-tier LLM gateway: a transport only — never selects a weather source, judges safety, or resolves a coordinate/timestamp — and never raises; a fully failed chain returns `LLMResult(available=False)`.
Each tier is an ordered chain of OpenAI-compatible endpoints from the environment alone (no provider name/model id/per-provider rule in code, just a base URL + model string), tried in order on timeout or error, so switching providers is a config edit."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Literal

from app.adapters.http import get_client
from app.config import LLMEndpoint, settings

logger = logging.getLogger(__name__)

Tier = Literal["small", "big"]

# Reported on AgentResult.model when no model was involved at all, so the field describes
# what actually happened instead of naming a vendor that never ran.
DETERMINISTIC = "deterministic"


@dataclass(frozen=True)
class LLMResult:
    """The only thing the gateway ever returns — `available` is the flag callers branch on; `text` is meaningful only when it's True."""
    tier: Tier
    available: bool
    text: str | None = None
    model: str | None = None
    host: str | None = None
    fallback_used: bool = False
    attempts: int = 0
    latency_ms: int = 0
    error: str | None = None


def _chain(tier: Tier) -> tuple[LLMEndpoint, ...]:
    return settings.small_llm_chain if tier == "small" else settings.big_llm_chain


def is_configured(tier: Tier) -> bool:
    return bool(settings.llm_enabled and _chain(tier))


async def _post(endpoint: LLMEndpoint, messages: list[dict[str, Any]],
                temperature: float, max_tokens: int, reasoning_effort: str | None = None) -> str:
    headers = {"Content-Type": "application/json"}
    if endpoint.api_key:
        headers["Authorization"] = f"Bearer {endpoint.api_key}"
    body: dict[str, Any] = {"model": endpoint.model, "messages": messages,
                            "temperature": temperature, "max_tokens": max_tokens,
                            # Without this, a reasoning-capable model inlines a <think> block into `content`, eating the answer's token budget; endpoints that don't recognize the field just ignore it.
                            "reasoning_format": "hidden"}
    if reasoning_effort is not None:
        # `reasoning_format: hidden` only hides the reasoning from `content` — the tokens
        # are still spent from `max_tokens`. Confirmed live: a reasoning-capable small-tier
        # model exhausted a short, tightly-budgeted structured-output call's max_tokens=200
        # on hidden reasoning alone, returning empty content every time and silently
        # falling back to the deterministic path. `reasoning_effort: low` fixed it. Left
        # opt-in (not default) since the big tier is deliberately woken for real
        # multi-step reasoning and must not have it throttled.
        body["reasoning_effort"] = reasoning_effort
    response = await get_client().post(
        f"{endpoint.base_url.rstrip('/')}/chat/completions",
        headers=headers, json=body, timeout=settings.llm_timeout_seconds,
    )
    response.raise_for_status()
    payload = response.json()
    return payload["choices"][0]["message"]["content"] or ""


async def generate(tier: Tier, messages: list[dict[str, Any]], *,
                   temperature: float = 0.35, max_tokens: int = 1024,
                   reasoning_effort: str | None = None) -> LLMResult:
    started = time.monotonic()

    def elapsed() -> int:
        return int((time.monotonic() - started) * 1000)

    chain = _chain(tier)
    if not settings.llm_enabled or not chain:
        reason = "LLM_ENABLED is false" if not settings.llm_enabled else f"{tier} tier has no endpoint configured"
        return LLMResult(tier=tier, available=False, error=reason, latency_ms=elapsed())

    last_error = "no endpoint attempted"
    attempted = 0
    for index, endpoint in enumerate(chain):
        if index and elapsed() >= settings.llm_total_timeout_seconds * 1000:
            last_error = f"total budget {settings.llm_total_timeout_seconds}s exhausted after {index} endpoint(s)"
            break
        attempted += 1
        try:
            text = await _post(endpoint, messages, temperature, max_tokens, reasoning_effort)
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            logger.warning("llm.endpoint_failed",
                           extra={"tier": tier, "host": endpoint.host, "attempt": index + 1,
                                  "error": type(exc).__name__})
            continue
        logger.info("llm.called",
                    extra={"tier": tier, "host": endpoint.host, "fallback_used": index > 0,
                           "attempts": index + 1, "latency_ms": elapsed()})
        return LLMResult(tier=tier, available=True, text=text, model=endpoint.model,
                         host=endpoint.host, fallback_used=index > 0, attempts=index + 1,
                         latency_ms=elapsed())

    logger.warning("llm.chain_exhausted",
                   extra={"tier": tier, "attempts": attempted, "error": last_error})
    return LLMResult(tier=tier, available=False, attempts=attempted,
                     latency_ms=elapsed(), error=last_error)


async def small_llm(messages: list[dict[str, Any]], **kwargs: Any) -> LLMResult:
    """Routing, understanding, tone, chit-chat and simple answers."""
    return await generate("small", messages, **kwargs)


async def big_llm(messages: list[dict[str, Any]], **kwargs: Any) -> LLMResult:
    """Complex multi-step reasoning only — woken by `_requires_big_llm`'s deterministic trigger (an unresolved RADE decision or disagreeing fused sources), never by asking the small model if it feels out of its depth."""
    return await generate("big", messages, **kwargs)
