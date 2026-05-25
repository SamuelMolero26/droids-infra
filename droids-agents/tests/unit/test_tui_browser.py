"""Pilot smoke tests for the Phase-1 session browser + search screens.

memquery is stubbed so no subprocess/droids-mem is touched.
"""

from __future__ import annotations

import pytest
from droids_agents import memquery
from droids_agents.memquery import Memory, Session
from droids_agents.tui import (
    DroidsAgentsApp,
    SearchScreen,
    SessionBrowserScreen,
    SessionDetailScreen,
)

pytestmark = pytest.mark.asyncio


def _fake_sessions() -> list[Session]:
    return [
        Session(
            session_id="sess_A",
            task_type="competitor_research",
            created_at=200,
            memories=[
                Memory(id="m1", kind="session_summary", title="Run A", created_at=200),
                Memory(id="m2", kind="error_resolution", title="Timeout",
                       learned="retry", created_at=150),
            ],
        )
    ]


async def test_ctrl_p_opens_browser_and_loads(monkeypatch) -> None:
    monkeypatch.setattr(memquery, "list_sessions", lambda limit=100: _fake_sessions())
    app = DroidsAgentsApp()
    async with app.run_test() as pilot:
        await pilot.press("ctrl+p")
        assert isinstance(app.screen, SessionBrowserScreen)
        await app.workers.wait_for_complete()
        await pilot.pause()
        tbl = app.screen.query_one("#sessions-table")
        assert tbl.row_count == 1


async def test_enter_opens_session_detail(monkeypatch) -> None:
    monkeypatch.setattr(memquery, "list_sessions", lambda limit=100: _fake_sessions())
    app = DroidsAgentsApp()
    async with app.run_test() as pilot:
        await pilot.press("ctrl+p")
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("enter")
        assert isinstance(app.screen, SessionDetailScreen)
        tbl = app.screen.query_one("#detail-table")
        assert tbl.row_count == 2  # summary + error_resolution


async def test_slash_opens_search_and_runs_query(monkeypatch) -> None:
    monkeypatch.setattr(memquery, "list_sessions", lambda limit=100: _fake_sessions())
    monkeypatch.setattr(
        memquery, "search",
        lambda q: [Memory(id="m9", kind="task_pattern", title="Hit", task_type="x")],
    )
    app = DroidsAgentsApp()
    async with app.run_test() as pilot:
        await pilot.press("ctrl+p")
        await pilot.pause()
        await pilot.press("slash")
        assert isinstance(app.screen, SearchScreen)
        await pilot.press("s", "c", "r", "a", "p", "e", "enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        tbl = app.screen.query_one("#results-table")
        assert tbl.row_count == 1
