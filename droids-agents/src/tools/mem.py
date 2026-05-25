"""droids-mem MCP tool wiring + direct fetch helper for CLI use.

Two surfaces:

1. ``mem_tools(settings)`` — builds the MCP tool list attached to the Rollup
   agent (the sole writer per the broker pattern, ADR 0004).
2. ``fetch_mem_context(settings, task_type, query)`` — direct JSON-RPC call
   used by the CLI BEFORE compiling Root, so Subteam factories can bake the
   sliced Bundle into their instructions. This is a V1 simplification of the
   plan's separate ``memory_loader`` Agent: agentspan compiles a static
   workflow, so the Bundle must exist at build time. The minted ``session_id``
   is reused across the run.

Wire-format key discipline: droids-mem `SaveRequest` JSON tag is `session_id`
and `mem_context` envelope is `{session_id, context}`. Always use the constant
``MEM_SESSION_KEY`` when reading/writing payloads — never the shorthand
``sess_id`` (that lives in prose only).
"""

from __future__ import annotations

import json

import httpx
from agentspan.agents import mcp_tool
from droids_agents.config import Settings
from droids_agents.schemas import ContextResponse, MemoryLoaderResult, TaskType

MEM_SESSION_KEY: str = "session_id"
"""Wire-format key for session identifier across mem_save / mem_context."""

MEM_TOOL_NAMES: tuple[str, ...] = ("mem_save", "mem_search", "mem_context", "mem_get")
"""Tools exposed by `droids-mem-mcp` (operator commands stay CLI-only)."""

_MCP_FETCH_TIMEOUT_S: float = 15.0


def mem_tools(settings: Settings) -> list:
    """Build the MCP tool list for the Rollup agent."""
    return [
        mcp_tool(
            server_url=settings.droids_mem_mcp_url,
            headers={"Authorization": f"Bearer {settings.droids_mem_mcp_token}"},
            tool_names=list(MEM_TOOL_NAMES),
        )
    ]


class MemFetchError(RuntimeError):
    """Raised when the direct mem_context fetch fails (HTTP, auth, parse)."""


def _parse_mcp_body(resp: httpx.Response) -> dict:
    """Parse JSON-RPC body from either ``application/json`` or
    ``text/event-stream`` (SSE) responses. mcp-go Streamable HTTP can return
    either; SSE wraps each frame as ``event: message\\ndata: <json>\\n\\n``.
    """
    ctype = resp.headers.get("content-type", "")
    text = resp.text
    if "text/event-stream" in ctype:
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data:
                continue
            try:
                return json.loads(data)
            except ValueError:
                continue
        raise MemFetchError(f"SSE body had no parseable data frame: {text[:200]!r}")
    try:
        return resp.json()
    except ValueError as e:
        raise MemFetchError(f"droids-mem-mcp response is not JSON: {e}") from e


def _mcp_post(
    client: httpx.Client,
    url: str,
    *,
    token: str,
    mcp_session_id: str | None,
    payload: dict,
) -> httpx.Response:
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if mcp_session_id:
        headers["Mcp-Session-Id"] = mcp_session_id
    return client.post(url, headers=headers, content=json.dumps(payload))


def fetch_mem_context(
    settings: Settings, *, task_type: TaskType, query: str
) -> MemoryLoaderResult:
    """Call ``mem_context`` over JSON-RPC, return ``MemoryLoaderResult``.

    Performs the full MCP Streamable HTTP handshake:
    1. POST ``initialize`` → server returns ``Mcp-Session-Id`` response header
    2. POST ``notifications/initialized`` (session-scoped)
    3. POST ``tools/call`` for ``mem_context``

    Reads droids-mem's ``session_id`` from the tool result's TOP-LEVEL field
    (distinct from MCP's transport-layer ``Mcp-Session-Id``).
    """
    url = settings.droids_mem_mcp_url
    tok = settings.droids_mem_mcp_token
    try:
        with httpx.Client(timeout=_MCP_FETCH_TIMEOUT_S) as client:
            # 1. initialize
            init_payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "droids-agents", "version": "0.1.0"},
                },
            }
            init_resp = _mcp_post(
                client, url, token=tok, mcp_session_id=None, payload=init_payload
            )
            if init_resp.status_code == 401:
                raise MemFetchError("droids-mem-mcp returned HTTP 401 (bearer auth)")
            if init_resp.status_code != 200:
                raise MemFetchError(
                    f"droids-mem-mcp initialize failed: HTTP "
                    f"{init_resp.status_code}: {init_resp.text[:200]}"
                )
            mcp_sid = init_resp.headers.get("Mcp-Session-Id")
            if not mcp_sid:
                raise MemFetchError(
                    "droids-mem-mcp did not return an Mcp-Session-Id header"
                )

            # 2. notifications/initialized (no id → no response expected)
            _mcp_post(
                client,
                url,
                token=tok,
                mcp_session_id=mcp_sid,
                payload={
                    "jsonrpc": "2.0",
                    "method": "notifications/initialized",
                    "params": {},
                },
            )

            # 3. tools/call mem_context
            call_resp = _mcp_post(
                client,
                url,
                token=tok,
                mcp_session_id=mcp_sid,
                payload={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "mem_context",
                        "arguments": {"task_type": task_type, "query": query},
                    },
                },
            )
    except httpx.HTTPError as e:
        raise MemFetchError(f"droids-mem-mcp unreachable: {e}") from e

    if call_resp.status_code != 200:
        raise MemFetchError(
            f"droids-mem-mcp returned HTTP {call_resp.status_code}: "
            f"{call_resp.text[:200]}"
        )

    body = _parse_mcp_body(call_resp)
    if "error" in body:
        raise MemFetchError(f"mem_context tool error: {body['error']}")

    result = body.get("result") or {}
    inner = result.get("structuredContent")
    if inner is None:
        content = result.get("content") or []
        for item in content:
            if item.get("type") == "text":
                try:
                    inner = json.loads(item.get("text", ""))
                    break
                except ValueError:
                    continue
    if not isinstance(inner, dict):
        raise MemFetchError(f"mem_context returned no parseable payload: {body!r}")

    session_id = inner.get(MEM_SESSION_KEY)
    bundle_dict = inner.get("context") or inner
    if not isinstance(session_id, str) or not session_id:
        raise MemFetchError(
            f"mem_context envelope missing top-level {MEM_SESSION_KEY!r}: {inner!r}"
        )

    bundle = ContextResponse.model_validate(bundle_dict)
    return MemoryLoaderResult(
        session_id=session_id, task_type=task_type, bundle=bundle
    )
