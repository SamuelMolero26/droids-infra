"""Pilot tests for the Phase-3 tabbed multi-session screen.

Settings.load, _ensure_droids_mem, and sessions.run_session are stubbed so no
agentspan / droids-mem / LLM is touched. The registry's default runner is
sessions.run_session (resolved at call time), so patching it swaps the worker.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from droids_agents import sessions, tui
from droids_agents.sessions import DONE, RUNNING, SessionState
from droids_agents.tui import DroidsAgentsApp, SessionsScreen

pytestmark = pytest.mark.asyncio


def _evt(type_, **kw):
    base = dict(content=None, tool_name=None, args=None, result=None, target=None,
               output=None, guardrail_name=None)
    base.update(kw)
    return SimpleNamespace(type=type_, **base)


def _stub(monkeypatch) -> None:
    monkeypatch.setattr(tui, "_POLL_INTERVAL_S", 0.05)
    monkeypatch.setattr(tui.Settings, "load", classmethod(lambda cls: object()))
    monkeypatch.setattr(tui, "_ensure_droids_mem", lambda: (True, "ok"))

    def _fake_run(state: SessionState, *, settings, competitors, max_total_tokens):
        state.set_plan(["research"])
        state.status = RUNNING
        state.ingest(_evt("message", content="hi"))
        state.status = DONE

    monkeypatch.setattr(sessions, "run_session", _fake_run)


async def test_first_session_spawns_tab(monkeypatch) -> None:
    _stub(monkeypatch)
    app = DroidsAgentsApp()
    async with app.run_test() as pilot:
        app.push_screen(
            SessionsScreen(first_prompt="research X", first_competitors_csv="X", max_cost_usd=None)
        )
        await pilot.pause(0.4)
        screen = app.screen
        assert isinstance(screen, SessionsScreen)
        tabs = screen.query_one(tui.TabbedContent)
        assert tabs.tab_count == 1
        assert "S1" in tabs.active


async def test_second_spawn_and_close(monkeypatch) -> None:
    _stub(monkeypatch)
    app = DroidsAgentsApp()
    async with app.run_test() as pilot:
        screen = SessionsScreen(first_prompt="a", first_competitors_csv="X", max_cost_usd=None)
        app.push_screen(screen)
        await pilot.pause(0.4)
        screen._spawn("second task", ["Y"])
        await pilot.pause(0.2)
        tabs = screen.query_one(tui.TabbedContent)
        assert tabs.tab_count == 2
        screen.action_close_session()
        await pilot.pause(0.2)
        assert tabs.tab_count == 1


async def test_cap_blocks_excess_spawns(monkeypatch) -> None:
    _stub(monkeypatch)
    app = DroidsAgentsApp()
    async with app.run_test() as pilot:
        screen = SessionsScreen(first_prompt="a", first_competitors_csv="", max_cost_usd=None)
        screen._registry.cap = 2
        app.push_screen(screen)
        await pilot.pause(0.4)
        screen._spawn("b", [])
        screen._spawn("c", [])  # over cap → ignored with notify
        await pilot.pause(0.2)
        tabs = screen.query_one(tui.TabbedContent)
        assert tabs.tab_count == 2


async def test_stats_text_renders() -> None:
    state = SessionState("x")
    state.set_plan(["research"])
    state.ingest(_evt("message", content="hi"))
    rendered = tui._stats_text(state.snapshot())
    assert "Messages:" in rendered.plain
    assert "Status:" in rendered.plain
