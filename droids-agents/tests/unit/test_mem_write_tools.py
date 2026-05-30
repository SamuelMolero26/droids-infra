"""Memory write tool tests."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from droids_agents.config import Settings
from droids_agents.naming import NamePool
from droids_agents.router import rollup_agent
from droids_agents.tools import mem


def _settings() -> Settings:
    return Settings(
        anthropic_api_key="anthropic-test",
        droids_mem_mcp_token="mem-test-token",
        droids_mem_mcp_url="http://mem.test/mcp",
        agentspan_url="http://agentspan.test",
        google_credentials_json=None,
        google_token_json=None,
        log_dir=Path("/tmp/droids-agents-test-logs"),
    )


def test_mem_write_tool_honors_dry_run(monkeypatch) -> None:
    def fail_if_called(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("dry-run mem_save should not call droids-mem")

    monkeypatch.setattr(mem, "_call_mcp_tool", fail_if_called)
    [mem_save] = mem.mem_write_tools(_settings())

    out = mem_save(
        kind="session_summary",
        title="Run summary",
        what="What happened",
        learned="What to reuse",
        task_type="competitor_research",
        session_id="sid-1",
        context=SimpleNamespace(state={"dry_run": True}),
    )

    assert out["dry_run"] is True
    assert out["session_id"] == "sid-1"


def test_mem_write_tool_uses_fresh_direct_mcp_call(monkeypatch) -> None:
    captured = {}

    def fake_call(settings, name, arguments):  # noqa: ANN001
        captured["settings"] = settings
        captured["name"] = name
        captured["arguments"] = arguments
        return {"id": "mem-1", "session_id": arguments["session_id"]}

    settings = _settings()
    monkeypatch.setattr(mem, "_call_mcp_tool", fake_call)
    [mem_save] = mem.mem_write_tools(settings)

    out = mem_save(
        kind="session_summary",
        title="Run summary",
        what="What happened",
        learned="What to reuse",
        task_type="competitor_research",
        session_id="sid-1",
        context=SimpleNamespace(state={}),
    )

    assert out == {"id": "mem-1", "session_id": "sid-1"}
    assert captured["settings"] is settings
    assert captured["name"] == "mem_save"
    assert captured["arguments"]["kind"] == "session_summary"


def test_rollup_uses_native_mem_save_not_server_side_mcp_tool() -> None:
    agent = rollup_agent(
        NamePool(names=["R2-D2"]),
        settings=_settings(),
        task_type="competitor_research",
        session_id="sid-1",
    )

    [mem_save] = agent.tools
    assert callable(mem_save)
    assert mem_save._tool_def.name == "mem_save"


def test_rollup_instruction_does_not_ask_when_subteam_output_missing() -> None:
    agent = rollup_agent(
        NamePool(names=["R2-D2"]),
        settings=_settings(),
        task_type="competitor_research",
        session_id="sid-1",
    )

    instructions = agent.instructions()

    assert "preceding Subteam output is empty" in instructions
    assert "DO NOT ask the user" in instructions
    assert "Never ask follow-up questions" in instructions
