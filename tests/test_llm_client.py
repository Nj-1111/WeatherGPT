"""The gateway is a transport that must never raise into a caller deciding what is safe."""
import asyncio
import dataclasses

import httpx
import pytest

from app.config import LLMEndpoint, settings
from app.llm.client import DETERMINISTIC, big_llm, generate, is_configured, small_llm

MESSAGES = [{"role": "user", "content": "hello"}]


def _endpoint(name: str) -> LLMEndpoint:
    return LLMEndpoint(model=f"{name}-model", base_url=f"https://{name}.invalid/v1", api_key="k")


class _FakeClient:
    """Answers per host: an Exception instance is raised, anything else is returned."""

    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.calls: list[str] = []

    async def post(self, url, **kwargs):
        host = url.split("://")[1].split("/")[0]
        self.calls.append(host)
        outcome = self.behaviour[host]
        if isinstance(outcome, Exception):
            raise outcome
        return httpx.Response(200, json={"choices": [{"message": {"content": outcome}}]},
                              request=httpx.Request("POST", url))


def _wire(monkeypatch, *, small=(), big=(), behaviour=None, enabled=True, total_timeout=20.0):
    monkeypatch.setattr("app.llm.client.settings", dataclasses.replace(
        settings, llm_enabled=enabled, small_llm_chain=tuple(small), big_llm_chain=tuple(big),
        llm_total_timeout_seconds=total_timeout))
    client = _FakeClient(behaviour or {})
    monkeypatch.setattr("app.llm.client.get_client", lambda: client)
    return client


def test_primary_answers_and_no_fallback_is_recorded(monkeypatch):
    _wire(monkeypatch, small=[_endpoint("a"), _endpoint("b")],
          behaviour={"a.invalid": "ok"})
    result = asyncio.run(small_llm(MESSAGES))
    assert result.available and result.text == "ok"
    assert result.fallback_used is False and result.attempts == 1
    assert result.host == "a.invalid" and result.tier == "small"


def test_chain_falls_through_to_the_next_endpoint(monkeypatch):
    client = _wire(monkeypatch, small=[_endpoint("a"), _endpoint("b"), _endpoint("c")],
                   behaviour={"a.invalid": httpx.ConnectError("refused"),
                              "b.invalid": "second"})
    result = asyncio.run(small_llm(MESSAGES))
    assert result.available and result.text == "second"
    assert result.fallback_used is True and result.attempts == 2
    assert client.calls == ["a.invalid", "b.invalid"]  # c never tried


def test_exhausted_chain_degrades_to_typed_unavailable_not_an_exception(monkeypatch):
    _wire(monkeypatch, small=[_endpoint("a"), _endpoint("b")],
          behaviour={"a.invalid": httpx.ConnectTimeout("slow"),
                     "b.invalid": httpx.HTTPStatusError("500", request=None, response=None)})
    result = asyncio.run(small_llm(MESSAGES))
    assert result.available is False and result.text is None
    assert result.attempts == 2 and result.error


@pytest.mark.parametrize("boom", [
    httpx.ConnectError("refused"), httpx.ReadTimeout("slow"), ValueError("garbage json"),
    KeyError("choices"), RuntimeError("anything at all"),
])
def test_no_exception_type_escapes_the_gateway(monkeypatch, boom):
    _wire(monkeypatch, small=[_endpoint("a")], behaviour={"a.invalid": boom})
    result = asyncio.run(small_llm(MESSAGES))
    assert result.available is False


def test_both_tiers_are_independent(monkeypatch):
    _wire(monkeypatch, small=[_endpoint("s")], big=[_endpoint("b")],
          behaviour={"s.invalid": httpx.ConnectError("down"), "b.invalid": "big answer"})
    assert asyncio.run(small_llm(MESSAGES)).available is False
    big = asyncio.run(big_llm(MESSAGES))
    assert big.available and big.text == "big answer" and big.tier == "big"


def test_unconfigured_tier_reports_unavailable_without_calling_anything(monkeypatch):
    client = _wire(monkeypatch, small=[], behaviour={})
    result = asyncio.run(generate("small", MESSAGES))
    assert result.available is False and "no endpoint configured" in result.error
    assert client.calls == []
    assert is_configured("small") is False


def test_disabled_flag_short_circuits_a_configured_chain(monkeypatch):
    client = _wire(monkeypatch, small=[_endpoint("a")], behaviour={"a.invalid": "ok"}, enabled=False)
    result = asyncio.run(small_llm(MESSAGES))
    assert result.available is False and "LLM_ENABLED is false" in result.error
    assert client.calls == []


def test_total_budget_stops_a_long_fallback_chain(monkeypatch):
    _wire(monkeypatch, small=[_endpoint(n) for n in "abcd"], total_timeout=0.0,
          behaviour={f"{n}.invalid": httpx.ConnectError("down") for n in "abcd"})
    result = asyncio.run(small_llm(MESSAGES))
    assert result.available is False
    # Budget checked before each retry, so it stops after the first rather than trying all four.
    assert result.attempts == 1 and "budget" in result.error


def test_gemini_openai_compat_endpoint_url_joins_correctly(monkeypatch):
    """Wiring in a real provider (Gemini's OpenAI-compat layer) is a config-only change —
    this guards the URL-join logic against ever mangling that specific base_url, since
    unlike the synthetic *.invalid endpoints above, its base_url has a trailing slash and
    a path segment (/v1beta/openai/) rather than being bare."""
    class _URLCapturingClient:
        def __init__(self):
            self.urls: list[str] = []

        async def post(self, url, **kwargs):
            self.urls.append(url)
            return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]},
                                  request=httpx.Request("POST", url))

    endpoint = LLMEndpoint(model="gemini-1.5-flash",
                           base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
                           api_key="k")
    monkeypatch.setattr("app.llm.client.settings", dataclasses.replace(
        settings, llm_enabled=True, small_llm_chain=(endpoint,)))
    client = _URLCapturingClient()
    monkeypatch.setattr("app.llm.client.get_client", lambda: client)
    result = asyncio.run(small_llm(MESSAGES))
    assert result.available and result.text == "ok"
    assert client.urls == ["https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"]


def test_no_provider_name_or_model_id_is_hardcoded():
    source = (__import__("pathlib").Path("app/llm/client.py")).read_text().casefold()
    for forbidden in ("groq", "openai.com", "anthropic", "qwen", "llama", "gpt-oss", "api-key-"):
        assert forbidden not in source, f"{forbidden!r} must come from config, not code"
    assert DETERMINISTIC == "deterministic"
