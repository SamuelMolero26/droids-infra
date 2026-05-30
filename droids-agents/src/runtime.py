"""Thin wrapper around agentspan's AgentRuntime.

Pinned in its own module so test code can monkey-patch a mock runtime without
reaching into ``cli.py``.
"""

from __future__ import annotations

from agentspan.agents import AgentRuntime
from droids_agents.config import Settings


def connect_runtime(settings: Settings) -> AgentRuntime:
    """Connect to the running agentspan server. Refuses if URL is unreachable.

    Per plan: the agentspan server is started out-of-band (``agentspan server
    start`` pinned to ``~/.droids-agents``). The CLI only connects.
    """
    return AgentRuntime(server_url=settings.agentspan_url)


def reset_tool_circuit_breakers() -> None:
    """Best-effort reset of agentspan's in-process tool circuit breakers.

    The breaker is process-local in the Python SDK. Resetting before each new
    Execution prevents one bad run from disabling a fixed tool for subsequent
    runs in the same CLI/TUI process.
    """
    try:
        from agentspan.agents.runtime._dispatch import reset_all_circuit_breakers
    except Exception:  # noqa: BLE001
        return
    reset_all_circuit_breakers()
