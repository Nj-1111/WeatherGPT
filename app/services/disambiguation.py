"""Matches a user's free-text reply against a short list of already-geocoded location
candidates, after LocationAmbiguousError. Never geocodes and never invents a place — it
only selects an index into data the pipeline already produced, or returns None. Degrades
to a deterministic substring/ordinal match when the LLM is unavailable, the same
resilience convention every other LLM call in this app follows.
"""
from __future__ import annotations

import json
import logging
import re

from app.llm.client import small_llm
from app.orchestrator.retrieval_planner import has_word
from app.prompts.loader import load_prompt

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = load_prompt("disambiguation")
_JSON_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)
_ORDINAL_WORDS = ("first", "second", "third", "fourth", "fifth")


def _describe(candidate: dict, index: int) -> str:
    parts = [candidate["name"]]
    if candidate.get("state"):
        parts.append(candidate["state"])
    if candidate.get("country"):
        parts.append(candidate["country"])
    return f"{index}. {', '.join(parts)}"


def _deterministic_match(text: str, candidates: list[dict]) -> dict | None:
    # User-facing messages present candidates 1-indexed ("1) ... 2) ..."), so a bare
    # digit reply must be read the same way, not as a raw list index.
    digits = re.findall(r"\d+", text)
    if digits:
        idx = int(digits[0]) - 1
        if 0 <= idx < len(candidates):
            return candidates[idx]
    casefolded = text.casefold()
    for i, word in enumerate(_ORDINAL_WORDS):
        if i < len(candidates) and has_word(casefolded, (word,)):
            return candidates[i]
    # "name" is deliberately excluded: every candidate in a disambiguation list shares
    # essentially the same name (that's why they're ambiguous) — matching on it would
    # match all of them at once instead of narrowing anything down. Only state/country
    # actually distinguish one candidate from its siblings.
    matches = [c for c in candidates
              if any(str(c.get(field) or "").casefold() in casefolded
                     for field in ("state", "country") if c.get(field))]
    return matches[0] if len(matches) == 1 else None


async def match_candidate(text: str, candidates: list[dict]) -> dict | None:
    listing = "\n".join(_describe(c, i) for i, c in enumerate(candidates))
    result = await small_llm(
        [{"role": "system", "content": _SYSTEM_PROMPT},
         {"role": "user", "content": f'Candidates:\n{listing}\n\nUser\'s reply: "{text}"'}],
        temperature=0.0, max_tokens=20,
    )
    if not result.available or not result.text:
        logger.info("disambiguation.llm_unavailable", extra={"error": result.error})
        return _deterministic_match(text, candidates)
    try:
        data = json.loads(_JSON_FENCE.sub("", result.text.strip()))
        idx = data.get("selected_index")
    except (json.JSONDecodeError, AttributeError):
        return _deterministic_match(text, candidates)
    if isinstance(idx, int) and 0 <= idx < len(candidates):
        return candidates[idx]
    return None
