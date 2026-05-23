"""fetch_mem_context: success + error paths with mocked httpx."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from droids_agents.config import Settings
from droids_agents.tools import mem as mem_mod


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        anthropic_api_key="sk-x",
        droids_mem_mcp_token="tok",
        droids_mem_mcp_url="http://mem.local/mcp",
        agentspan_url="http://as.local",
        google_credentials_json=tmp_path / "c.json",
        google_token_json=tmp_path / "t.json",
        log_dir=tmp_path / "logs",
        email_allowlist=(),
    )


class _MockResp:
    def __init__(self, status_code: int, body: dict | str) -> None:
        self.status_code = status_code
        self._body = body
        self.text = body if isinstance(body, str) else json.dumps(body)

    def json(self) -> dict:
        if isinstance(self._body, str):
            raise ValueError("not json")
        return self._body


class _MockClient:
    def __init__(self, resp: _MockResp) -> None:
        self._resp = resp
        self.last_call = None

    def __enter__(self) -> "_MockClient":
        return self

    def __exit__(self, *a) -> None:
        return None

    def post(self, url, headers, content):
        self.last_call = {"url": url, "headers": headers, "content": content}
        return self._resp


def _patch_client(monkeypatch, resp: _MockResp) -> _MockClient:
    client = _MockClient(resp)
    monkeypatch.setattr(httpx, "Client", lambda **kw: client)
    return client


def test_fetch_mem_context_structured_envelope(monkeypatch, settings) -> None:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "structuredContent": {
                "session_id": "sess_01ABC",
                "context": {
                    "task_type": "competitor_research",
                    "last_session": None,
                    "user_rules": [],
                    "browse": [],
                },
            },
        },
    }
    client = _patch_client(monkeypatch, _MockResp(200, payload))

    out = mem_mod.fetch_mem_context(
        settings, task_type="competitor_research", query="anthropic"
    )
    assert out.session_id == "sess_01ABC"
    assert out.task_type == "competitor_research"
    assert out.bundle.task_type == "competitor_research"
    # bearer auth header threaded
    assert client.last_call["headers"]["Authorization"] == "Bearer tok"


def test_fetch_mem_context_content_array_envelope(monkeypatch, settings) -> None:
    """Older mcp-go shape: result.content[0].text contains the JSON."""
    inner = {
        "session_id": "sess_X",
        "context": {
            "task_type": "doc_synthesis",
            "user_rules": [],
            "browse": [],
        },
    }
    payload = {
        "result": {
            "content": [{"type": "text", "text": json.dumps(inner)}],
        }
    }
    _patch_client(monkeypatch, _MockResp(200, payload))
    out = mem_mod.fetch_mem_context(settings, task_type="doc_synthesis", query="x")
    assert out.session_id == "sess_X"


def test_fetch_mem_context_http_error(monkeypatch, settings) -> None:
    _patch_client(monkeypatch, _MockResp(500, "boom"))
    with pytest.raises(mem_mod.MemFetchError, match="HTTP 500"):
        mem_mod.fetch_mem_context(settings, task_type="competitor_research", query="x")


def test_fetch_mem_context_tool_error_envelope(monkeypatch, settings) -> None:
    _patch_client(monkeypatch, _MockResp(200, {"error": {"code": -1, "message": "nope"}}))
    with pytest.raises(mem_mod.MemFetchError, match="tool error"):
        mem_mod.fetch_mem_context(settings, task_type="competitor_research", query="x")


def test_fetch_mem_context_missing_session_id(monkeypatch, settings) -> None:
    payload = {"result": {"structuredContent": {"context": {"task_type": "x"}}}}
    _patch_client(monkeypatch, _MockResp(200, payload))
    with pytest.raises(mem_mod.MemFetchError, match="session_id"):
        mem_mod.fetch_mem_context(settings, task_type="competitor_research", query="x")


def test_fetch_mem_context_transport_failure(monkeypatch, settings) -> None:
    class _Boom:
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def post(self, *a, **kw): raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "Client", lambda **kw: _Boom())
    with pytest.raises(mem_mod.MemFetchError, match="unreachable"):
        mem_mod.fetch_mem_context(settings, task_type="competitor_research", query="x")
