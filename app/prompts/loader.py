"""Loads a 3-file prompt contract (system_role.md, behavior_rules.md, output_format.md)
for one LLM call site into a single system-prompt string. Read once at import time by each
caller — not re-read per request. Wording in system_role.md/behavior_rules.md is free to
edit; output_format.md's JSON shape is read by code downstream (see the caveat each
output_format.md carries) and needs a matching code change if the keys/enum values change.
Takes effect on the next process restart — this app has no hot-reload.
"""
from __future__ import annotations

from pathlib import Path

_DIR = Path(__file__).parent


def load_prompt(agent: str) -> str:
    base = _DIR / agent
    sections = [(base / name).read_text(encoding="utf-8").strip()
                for name in ("system_role.md", "behavior_rules.md", "output_format.md")]
    return "\n\n".join(sections)
