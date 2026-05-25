"""memquery: list_sessions grouping + search, with _run stubbed."""

from __future__ import annotations

import pytest

from droids_agents import memquery
from droids_agents.memquery import MemQueryError, Memory, Session


def _stub_run(monkeypatch, payload: dict) -> None:
    monkeypatch.setattr(memquery, "_run", lambda args, **kw: payload)


def test_list_sessions_groups_by_session_id(monkeypatch) -> None:
    _stub_run(
        monkeypatch,
        {
            "memories": [
                {"id": "m1", "session_id": "s1", "task_type": "competitor_research",
                 "kind": "session_summary", "title": "Run A", "created_at": 200},
                {"id": "m2", "session_id": "s1", "task_type": "competitor_research",
                 "kind": "error_resolution", "title": "Timeout", "created_at": 150},
                {"id": "m3", "session_id": "s2", "task_type": "doc_synthesis",
                 "kind": "session_summary", "title": "Run B", "created_at": 300},
            ],
            "total": 3,
        },
    )
    sessions = memquery.list_sessions()
    assert [s.session_id for s in sessions] == ["s2", "s1"]  # newest first
    s1 = next(s for s in sessions if s.session_id == "s1")
    assert len(s1.memories) == 2
    assert s1.memories[0].created_at == 200  # newest within session first


def test_session_title_prefers_summary() -> None:
    s = Session(
        session_id="s1",
        task_type="competitor_research",
        created_at=200,
        memories=[
            Memory(id="m1", kind="session_summary", title="Run A", created_at=200),
            Memory(id="m2", kind="error_resolution", title="Timeout", created_at=150),
        ],
    )
    assert s.summary.id == "m1"
    assert s.title == "Run A"


def test_session_title_fallback_without_summary() -> None:
    s = Session(
        session_id="s1",
        task_type="competitor_research",
        created_at=150,
        memories=[Memory(id="m2", kind="error_resolution", title="Timeout", created_at=150)],
    )
    assert s.summary is None
    assert s.title == "competitor_research (1 memories)"


def test_list_sessions_skips_memories_without_session_id(monkeypatch) -> None:
    _stub_run(
        monkeypatch,
        {"memories": [{"id": "m1", "kind": "decision", "title": "no sid", "created_at": 100}]},
    )
    assert memquery.list_sessions() == []


def test_search_maps_results(monkeypatch) -> None:
    _stub_run(
        monkeypatch,
        {"results": [
            {"id": "m1", "kind": "error_resolution", "title": "Scrape timeout",
             "learned": "retry with backoff", "task_type": "competitor_research",
             "created_at": 100, "score": -1.2},
        ], "total": 1},
    )
    out = memquery.search("scrape")
    assert len(out) == 1
    assert out[0].id == "m1"
    assert out[0].learned == "retry with backoff"


def test_search_empty_query_skips_subprocess(monkeypatch) -> None:
    # _run must NOT be called for blank queries
    def _boom(*a, **kw):
        raise AssertionError("_run should not be called for empty query")
    monkeypatch.setattr(memquery, "_run", _boom)
    assert memquery.search("   ") == []


def test_memory_from_dict_tolerates_missing_fields() -> None:
    m = Memory.from_dict({"id": "m1", "kind": "task_pattern", "title": "t"})
    assert m.id == "m1"
    assert m.what == "" and m.tags == "" and m.created_at == 0
