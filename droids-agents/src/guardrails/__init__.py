"""Guardrail functions and factories.

All guardrails are pure functions of ``content: str`` returning
``GuardrailResult(passed, message)``. Factories close over per-Execution
state (allowlists, docs basenames) without leaking it into module globals.
"""

from __future__ import annotations

import json
from typing import Any


def parse_json_content(content: str) -> dict[str, Any] | None:
    """Best-effort JSON parse for guardrails inspecting structured agent output.

    Returns the parsed dict, or ``None`` if content is plain prose. Lists / scalars
    return ``None`` — guardrails wanting them should call ``json.loads`` directly.

    Strips markdown code fences (```json ... ``` or ``` ... ```) before parsing.
    """
    if not content:
        return None
    text = content.strip()
    # Strip markdown fences: ```json\n...\n``` or ```\n...\n```
    if text.startswith("```"):
        lines = text.splitlines()
        # drop opening fence line, drop closing ``` if present
        inner = lines[1:]
        if inner and inner[-1].strip() == "```":
            inner = inner[:-1]
        text = "\n".join(inner).strip()
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None
