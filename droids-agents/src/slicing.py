"""Rule-based context slicing per Sub-agent role.

The ``mem_context`` Bundle (``ContextResponse``) returned by droids-mem mixes
two tiers:
- ``always`` (``last_session``, ``user_rules[]``) → carry ``.learned``;
  ``.snippet`` is empty.
- ``browse`` (``browse[]`` rows of kind ``task_pattern`` / ``error_resolution``)
  → carry ``.snippet`` (≤120 chars, truncated from ``what``); ``.learned`` is
  empty.

Slicing MUST read the correct field per tier — reading ``.learned`` on a
browse row silently injects nothing.

Returned ``list[str]`` is injected into each Sub-agent's ``instructions``
closure (Phase 3b factory pattern). Empty list = no prior context to inject.
"""

from __future__ import annotations

from droids_agents.schemas import ContextMemory, ContextResponse, Role


def _text(mem: ContextMemory) -> str:
    """Tier-aware reader. Picks ``learned`` for always-tier, ``snippet`` for browse."""
    return mem.learned if mem.tier == "always" else mem.snippet


def _format(mem: ContextMemory) -> str:
    """Compact one-line render the factory can interpolate verbatim."""
    body = _text(mem).strip()
    if not body:
        return ""
    return f"- [{mem.kind}] {mem.title}: {body}"


def _filter_browse(bundle: ContextResponse, kind: str) -> list[ContextMemory]:
    return [b for b in bundle.browse if b.kind == kind]


def _prompt_token_match(mem: ContextMemory, prompt: str) -> bool:
    """Cheap token-substring match used by the Researcher slice."""
    hay = f"{mem.title} {mem.snippet}".lower()
    tokens = {t for t in prompt.lower().split() if len(t) > 3}
    return any(tok in hay for tok in tokens)


def slice_for(role: Role, bundle: ContextResponse, prompt: str) -> list[str]:
    """Return formatted memory lines to inject into ``role``'s instructions.

    Static V1 rules (see plan L215-223). Lines with empty bodies are dropped
    so the prompt stays compact.
    """
    out: list[ContextMemory] = []

    if role == "competitor":
        if bundle.last_session is not None:
            out.append(bundle.last_session)
        out.extend(
            b for b in _filter_browse(bundle, "task_pattern") if _prompt_token_match(b, prompt)
        )

    elif role == "extractor":
        out.extend(_filter_browse(bundle, "error_resolution"))
        out.extend(_filter_browse(bundle, "task_pattern"))

    elif role == "synthesizer":
        if bundle.last_session is not None:
            out.append(bundle.last_session)
        out.extend(_filter_browse(bundle, "error_resolution"))

    elif role == "form_planner":
        out.extend(bundle.user_rules)
        out.extend(_filter_browse(bundle, "task_pattern"))

    elif role == "form_executor":
        out.extend(bundle.user_rules)
        out.extend(_filter_browse(bundle, "task_pattern"))

    elif role == "drafter":
        out.extend(bundle.user_rules)
        if bundle.last_session is not None:
            out.append(bundle.last_session)

    elif role == "sender":
        out.extend(bundle.user_rules)
        if bundle.last_session is not None:
            out.append(bundle.last_session)

    else:
        # memory_loader and rollup take no slice — they consume the raw Bundle.
        return []

    return [s for s in (_format(m) for m in out) if s]
