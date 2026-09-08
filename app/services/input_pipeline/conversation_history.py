"""Turns the previously-dead app.storage.conversation_log into the guardrail's session memory: recent() feeds prior turns into run_guardrail so a contextless follow-up ("can I go play in the evening") can be read as a continuation instead of hard-rejected. SqliteConversationLog is synchronous, so both operations run off the event loop via asyncio.to_thread."""
from __future__ import annotations

import asyncio
from typing import Any

from app.storage import conversation_log


async def recent_turns(session_id: str, limit: int) -> list[dict[str, Any]]:
    return await asyncio.to_thread(conversation_log.recent, session_id, limit)


async def record_turn(session_id: str, role: str, **fields: Any) -> None:
    await asyncio.to_thread(conversation_log.append, session_id, {"role": role, **fields})
