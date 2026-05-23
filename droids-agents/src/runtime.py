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
