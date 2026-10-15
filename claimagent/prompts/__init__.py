"""Prompt texts, kept as Markdown files next to this module so they can be read and edited without touching code."""

from __future__ import annotations

from functools import cache
from pathlib import Path

PROMPTS_DIR = Path(__file__).parent


@cache
def load_prompt(name: str) -> str:
    """Text of prompts/<name>.md."""
    return (PROMPTS_DIR / f"{name}.md").read_text().strip()
