"""RAG seam: run_explanation_agent calls retrieve() and appends whatever comes back to the fact sheet as "Reference material". Returns [] today — a real implementation plugs in here without changing that call site or the fact-sheet format."""
from __future__ import annotations


class ContextRetriever:
    async def retrieve(self, query: str, k: int = 3) -> list[str]:
        return []


context_retriever = ContextRetriever()
