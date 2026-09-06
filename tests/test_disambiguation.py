"""The disambiguation agent: matches a user's free-text reply against a short list of
already-geocoded candidates. Never invents a place — only selects an index, or None.

`small_llm` is stubbed directly, mirroring tests/test_query_guardrail.py's convention.
"""
from __future__ import annotations

import asyncio
import json

from app.llm.client import LLMResult
from app.services import disambiguation

CANDIDATES = [
    {"name": "Kalyani", "state": "West Bengal", "country": "India"},
    {"name": "Kalyani", "state": "Madhya Pradesh", "country": "India"},
]


def _stub_llm(monkeypatch, payload: dict | None, *, available: bool = True):
    async def fake_small_llm(messages, **kwargs):
        if not available:
            return LLMResult(tier="small", available=False, error="stubbed unavailable")
        return LLMResult(tier="small", available=True, text=json.dumps(payload))
    monkeypatch.setattr(disambiguation, "small_llm", fake_small_llm)


def test_llm_selects_the_named_index(monkeypatch):
    _stub_llm(monkeypatch, {"selected_index": 0})
    result = asyncio.run(disambiguation.match_candidate("the west bengal one", CANDIDATES))
    assert result == CANDIDATES[0]


def test_llm_returns_none_for_an_unclear_reply(monkeypatch):
    _stub_llm(monkeypatch, {"selected_index": None})
    result = asyncio.run(disambiguation.match_candidate("hmm not sure", CANDIDATES))
    assert result is None


def test_llm_out_of_range_index_is_rejected(monkeypatch):
    """Never trust the LLM's index blindly — it must point at a real candidate."""
    _stub_llm(monkeypatch, {"selected_index": 5})
    result = asyncio.run(disambiguation.match_candidate("the third one", CANDIDATES))
    assert result is None


def test_llm_unavailable_falls_back_to_state_substring_match(monkeypatch):
    _stub_llm(monkeypatch, None, available=False)
    result = asyncio.run(disambiguation.match_candidate("the madhya pradesh one", CANDIDATES))
    assert result == CANDIDATES[1]


def test_llm_unavailable_falls_back_to_numeric_pick(monkeypatch):
    _stub_llm(monkeypatch, None, available=False)
    result = asyncio.run(disambiguation.match_candidate("2", CANDIDATES))
    assert result == CANDIDATES[1]


def test_llm_unavailable_falls_back_to_ordinal_word(monkeypatch):
    _stub_llm(monkeypatch, None, available=False)
    result = asyncio.run(disambiguation.match_candidate("the first one", CANDIDATES))
    assert result == CANDIDATES[0]


def test_llm_unavailable_and_reply_matches_nothing_returns_none(monkeypatch):
    _stub_llm(monkeypatch, None, available=False)
    result = asyncio.run(disambiguation.match_candidate("what about tomorrow", CANDIDATES))
    assert result is None


def test_llm_unavailable_ambiguous_reply_matching_both_returns_none(monkeypatch):
    """Both candidates are named "Kalyani" — a reply naming only that must not guess."""
    _stub_llm(monkeypatch, None, available=False)
    result = asyncio.run(disambiguation.match_candidate("Kalyani", CANDIDATES))
    assert result is None


def test_llm_unavailable_state_match_is_not_drowned_out_by_shared_place_name(monkeypatch):
    """Regression: every candidate here is named "Digha" (or a near-variant) — matching on
    "name" used to match all of them at once and force a None instead of narrowing to the
    one whose state is actually named in the reply."""
    _stub_llm(monkeypatch, None, available=False)
    candidates = [
        {"name": "Dighwara", "state": "Bihar", "country": "India"},
        {"name": "Digha", "state": "West Bengal", "country": "India"},
        {"name": "Digha", "state": "Rajshahi Division", "country": "Bangladesh"},
        {"name": "Digha", "state": "Rajshahi Division", "country": "Bangladesh"},
    ]
    result = asyncio.run(disambiguation.match_candidate("digha west bengal", candidates))
    assert result == candidates[1]
