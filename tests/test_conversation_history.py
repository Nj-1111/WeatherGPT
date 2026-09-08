"""conversation_history.py wires the previously-dead ConversationLog into the input
pipeline so the guardrail can see prior turns. Isolated on a throwaway DB file, same
pattern as tests/test_storage.py's `store` fixture."""
import asyncio

import httpx
import pytest

from app.main import app
from app.services.input_pipeline.conversation_history import recent_turns, record_turn


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr("app.context.store.DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr("app.context.store._initialized", False)


async def _post(path, body):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        return await client.post(path, json=body)


def test_record_turn_then_recent_turns_round_trip():
    async def run():
        await record_turn("s1", "user", text="will it rain in Newtown")
        await record_turn("s1", "assistant", text="Newtown", action="accept_weather_full")
        turns = await recent_turns("s1", 10)
        assert [t["text"] for t in turns] == ["will it rain in Newtown", "Newtown"]
        assert turns[1]["action"] == "accept_weather_full"
        assert await recent_turns("s2", 10) == []

    asyncio.run(run())


def test_guardrail_off_topic_query_still_records_the_turn(monkeypatch):
    """Even a REJECT_OFF_TOPIC turn is worth remembering — a future message might turn out
    to be a continuation of whatever came right before the rejection."""
    from app.schemas.query import GuardrailAction, GuardrailDecision

    async def fake_run_guardrail(text, history=None):
        return GuardrailDecision(original_text=text, action=GuardrailAction.REJECT_OFF_TOPIC, confidence=0.9)
    monkeypatch.setattr("app.main.run_guardrail", fake_run_guardrail)

    response = asyncio.run(_post("/query", {"question": "who won the match", "session_id": "s-history"}))
    assert response.status_code == 400

    turns = asyncio.run(recent_turns("s-history", 10))
    assert turns[0] == {"role": "user", "text": "who won the match"}
    assert turns[1]["action"] == "reject_off_topic"
