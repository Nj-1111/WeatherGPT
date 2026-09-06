"""Loads one LLM call site's 3-file prompt contract (system_role/behavior_rules/output_format.md) into one system-prompt string, read once at import time per caller, not per request; output_format.md's JSON shape is read by downstream code, so its keys/enums need a matching code change, and edits only take effect on the next process restart (no hot-reload)."""
from __future__ import annotations

from pathlib import Path

_DIR = Path(__file__).parent


def load_prompt(agent: str) -> str:
    base = _DIR / agent
    sections = [(base / name).read_text(encoding="utf-8").strip()
                for name in ("system_role.md", "behavior_rules.md", "output_format.md")]
    return "\n\n".join(sections)
