"""Playwright tools with per-Execution BrowserContext lifecycle.

agentspan does not expose arbitrary per-execution Python object storage
(``ToolContext.state`` is JSON-serialisable shared state, not for live objects
like a Playwright ``BrowserContext``). We therefore keep an explicit
module-level registry keyed by ``execution_id``, guarded by an ``asyncio.Lock``.

- Parallel Sub-agents within one Execution share the same context (no
  duplicate-login burden during a research fan-out).
- Different Executions get isolated contexts → no cross-run state bleed.
- Worker restart → registry is empty after process boot; next tool call
  recreates the context lazily. Read-only actions replay safely; write
  actions (``web_submit``) re-prompt HITL.
- A daemon sweep closes contexts older than 1 hour as a leak safety net.

Lifecycle teardown of a normally-completed Execution is the CLI's job: it
should ``await close_context(exec_id)`` from an on_complete / on_fail hook.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass

from agentspan.agents import ToolContext, tool
from playwright.async_api import BrowserContext, Playwright, async_playwright

_log = logging.getLogger(__name__)

_SWEEP_AGE_SECONDS: float = 60 * 60  # 1 hour
_TEXT_EXTRACT_CAP_CHARS: int = 50_000


@dataclass
class _Entry:
    context: BrowserContext
    playwright: Playwright
    created_at: float


_browsers: dict[str, _Entry] = {}
_lock = asyncio.Lock()
_sweep_task: asyncio.Task | None = None


async def _get_or_create(exec_id: str) -> BrowserContext:
    async with _lock:
        entry = _browsers.get(exec_id)
        if entry is not None:
            return entry.context
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context()
        _browsers[exec_id] = _Entry(context=ctx, playwright=pw, created_at=time.monotonic())
        _ensure_sweep_running()
        return ctx


def _ensure_sweep_running() -> None:
    global _sweep_task
    if _sweep_task is not None and not _sweep_task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _sweep_task = loop.create_task(_sweep_forever())


async def _sweep_forever() -> None:
    """Close any BrowserContext older than _SWEEP_AGE_SECONDS."""
    while True:
        await asyncio.sleep(300)
        now = time.monotonic()
        stale: list[str] = []
        async with _lock:
            for exec_id, entry in _browsers.items():
                if now - entry.created_at > _SWEEP_AGE_SECONDS:
                    stale.append(exec_id)
        for exec_id in stale:
            with contextlib.suppress(Exception):
                await close_context(exec_id)
            _log.warning("playwright: swept stale BrowserContext for exec_id=%s", exec_id)


async def close_context(exec_id: str) -> None:
    """Tear down a BrowserContext + Playwright instance for one Execution."""
    async with _lock:
        entry = _browsers.pop(exec_id, None)
    if entry is None:
        return
    with contextlib.suppress(Exception):
        await entry.context.close()
    with contextlib.suppress(Exception):
        await entry.playwright.stop()


@tool
async def web_navigate(url: str, context: ToolContext) -> dict:
    """Navigate the per-Execution browser to ``url``. Returns final URL + title."""
    ctx = await _get_or_create(context.execution_id)
    page = ctx.pages[-1] if ctx.pages else await ctx.new_page()
    await page.goto(url, wait_until="domcontentloaded")
    return {"url": page.url, "title": await page.title()}


@tool
async def web_extract_text(context: ToolContext, selector: str | None = None) -> dict:
    """Extract visible text from the current page (or a CSS selector). Capped at 50k chars."""
    ctx = await _get_or_create(context.execution_id)
    if not ctx.pages:
        return {"text": "", "truncated": False, "error": "no open page"}
    page = ctx.pages[-1]
    text = await (page.locator(selector).inner_text() if selector else page.inner_text("body"))
    truncated = len(text) > _TEXT_EXTRACT_CAP_CHARS
    return {"text": text[:_TEXT_EXTRACT_CAP_CHARS], "truncated": truncated}


@tool
async def web_screenshot(path: str, context: ToolContext) -> dict:
    """Take a PNG screenshot of the current page to ``path``."""
    ctx = await _get_or_create(context.execution_id)
    if not ctx.pages:
        return {"saved": False, "error": "no open page"}
    page = ctx.pages[-1]
    await page.screenshot(path=path, full_page=True)
    return {"saved": True, "path": path}


@tool
async def web_fill(selector: str, value: str, context: ToolContext) -> dict:
    """Fill a form field. Side-effect-free in the sense of no network submit."""
    ctx = await _get_or_create(context.execution_id)
    if not ctx.pages:
        return {"filled": False, "error": "no open page"}
    page = ctx.pages[-1]
    await page.locator(selector).fill(value)
    return {"filled": True, "selector": selector}


@tool
async def web_click(selector: str, context: ToolContext) -> dict:
    """Click an element. Use ``web_submit`` for form submissions (HITL-gated)."""
    ctx = await _get_or_create(context.execution_id)
    if not ctx.pages:
        return {"clicked": False, "error": "no open page"}
    page = ctx.pages[-1]
    await page.locator(selector).click()
    return {"clicked": True, "selector": selector}


@tool(approval_required=True)
async def web_submit(form_selector: str, context: ToolContext) -> dict:
    """Submit a form. Always pauses for HITL approval (irreversible side effect)."""
    ctx = await _get_or_create(context.execution_id)
    if not ctx.pages:
        return {"submitted": False, "error": "no open page"}
    page = ctx.pages[-1]
    async with page.expect_navigation(wait_until="domcontentloaded"):
        await page.locator(form_selector).evaluate("(el) => el.submit ? el.submit() : el.click()")
    return {"submitted": True, "response_url": page.url}
