"""Anthropic pricing table + USD → max_total_tokens conversion.

agentspan Python `TokenUsageTermination` exposes only `max_total_tokens` (split
prompt/completion params are TypeScript-only). The conversion uses sonnet-4-6
worst-case with a blended 30% prompt / 70% completion mix:

    blended = 0.3 * $3/Mtok + 0.7 * $15/Mtok = $11.4/Mtok

This OVERESTIMATES cost for haiku-heavy or cache-heavy executions — i.e. the
real cap is more generous than the budget implies, never less. Refine to a
per-call USD callback post-V1 if real-world drift exceeds 30%.

Cost-budgeting note: a worst-case `doc_team` Execution with swarm max_turns=10
and `citations_structural` RETRY (max_retries=2) can hit ~30 LLM calls. Set
`--max-cost-usd` accordingly for large doc_team prompts.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelPrice:
    """Per-Mtok USD prices for a single Anthropic model."""

    prompt: float
    completion: float
    cached_read: float


# Pricing per million tokens (USD). Update when Anthropic publishes new rates.
PRICES: dict[str, ModelPrice] = {
    "claude-sonnet-4-6": ModelPrice(prompt=3.0, completion=15.0, cached_read=0.30),
    "claude-haiku-4-5": ModelPrice(prompt=1.0, completion=5.0, cached_read=0.10),
}

# Worst-case billing model: specialists run this, so it bounds real cost from above.
# Used for budget conversion AND post-run cost estimation when TokenUsage carries no model.
BILLING_MODEL: str = "claude-sonnet-4-6"

# Blended worst-case (30% prompt + 70% completion) — used for budget conversion.
_BLENDED_SONNET_USD_PER_MTOK: float = (
    0.3 * PRICES[BILLING_MODEL].prompt + 0.7 * PRICES[BILLING_MODEL].completion
)


def usd_to_max_total_tokens(budget_usd: float) -> int:
    """Convert a USD budget into a conservative `max_total_tokens` cap.

    Uses sonnet-4-6 worst-case blended rate. Returns total tokens (prompt +
    completion summed) — matches agentspan Python `TokenUsageTermination`.
    """
    if budget_usd <= 0:
        raise ValueError(f"budget_usd must be positive, got {budget_usd}")
    return int((budget_usd / _BLENDED_SONNET_USD_PER_MTOK) * 1_000_000)


def estimate_cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Estimate USD cost for a single call. Used by rollup cost line.

    Accepts both bare (``claude-haiku-4-5``) and agentspan provider-prefixed
    (``anthropic/claude-haiku-4-5``) model identifiers.
    """
    key = model.split("/", 1)[1] if "/" in model else model
    if key not in PRICES:
        raise KeyError(f"unknown model {model!r}; add to PRICES table")
    p = PRICES[key]
    return (prompt_tokens / 1_000_000) * p.prompt + (completion_tokens / 1_000_000) * p.completion
