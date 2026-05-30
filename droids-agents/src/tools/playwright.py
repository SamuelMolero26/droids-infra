"""Playwright tools with per-Execution BrowserContext lifecycle.

agentspan does not expose arbitrary per-execution Python object storage
(``ToolContext.state`` is JSON-serialisable shared state, not for live objects
like a Playwright ``BrowserContext``). We therefore keep an explicit
module-level registry keyed by ``execution_id``, guarded by ``threading.RLock``.

Each ``_Entry`` owns a dedicated **worker thread** that started Playwright and
solely operates on it. agentspan's tool dispatcher hands tool invocations to a
worker thread pool, so the same Execution's ``web_navigate`` and
``web_extract_text`` calls can land on different threads. Playwright's
``sync_api`` is thread-bound (its greenlet event loop lives on the thread that
called ``sync_playwright().start()``); cross-thread access silently corrupts
the context — observable as ``ctx.pages`` returning an empty list immediately
after a successful ``page.goto``. The worker thread + queue marshals every
tool call back to the owning thread, eliminating that class of bug.

- Parallel Sub-agents within one Execution share the same context (no
  duplicate-login burden during a research fan-out).
- Different Executions get isolated contexts → no cross-run state bleed.
- Worker restart → registry is empty after process boot; next tool call
  recreates the context lazily. Read-only actions replay safely; write
  actions (``web_submit``) re-prompt HITL.
- A daemon sweep closes contexts older than 1 hour as a leak safety net.

Lifecycle teardown of a normally-completed Execution is the CLI's job: it
should call ``close_context(exec_id)`` from an on_complete / on_fail hook.
"""

from __future__ import annotations

import contextlib
import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from agentspan.agents import ToolContext, tool
from playwright.sync_api import BrowserContext, sync_playwright

_log = logging.getLogger(__name__)

_SWEEP_AGE_SECONDS: float = 60 * 60  # 1 hour
_TEXT_EXTRACT_CAP_CHARS: int = 8_000
_WORKER_BOOT_TIMEOUT_SECONDS: float = 30.0
_WORKER_SHUTDOWN_TIMEOUT_SECONDS: float = 10.0
_BLOCKED_TITLE_MARKERS: tuple[str, ...] = (
    "just a moment",
    "attention required",
    "access denied",
    "verify you are human",
)
_BLOCKED_TEXT_MARKERS: tuple[str, ...] = (
    "checking your browser",
    "verify you are human",
    "enable javascript and cookies",
    "cf-ray",
    "cloudflare",
)
_BLOCKED_URL_MARKERS: tuple[str, ...] = (
    "__cf_chl_",
    "challenge-platform",
    "/cdn-cgi/challenge-platform/",
)


_Op = Callable[[BrowserContext], Any]


@dataclass
class _Entry:
    inbox: queue.Queue[tuple[_Op, queue.Queue[tuple[str, Any]]] | None]
    thread: threading.Thread
    created_at: float


_browsers: dict[str, _Entry] = {}
_lock = threading.RLock()
_sweep_thread: threading.Thread | None = None


def _worker(inbox: queue.Queue[tuple[_Op, queue.Queue[tuple[str, Any]]] | None],
            boot: queue.Queue[BaseException | None]) -> None:
    """Own the Playwright lifecycle on a single thread.

    Boots Playwright + browser + context, signals readiness via ``boot``, then
    serves operations from ``inbox`` until it receives ``None``. All Playwright
    access (including teardown) happens on this thread to honour the library's
    thread-affinity contract.
    """
    pw = None
    browser = None
    ctx: BrowserContext | None = None
    try:
        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context()
    except BaseException as e:  # noqa: BLE001 — propagate boot failure to caller
        boot.put(e)
        if pw is not None:
            with contextlib.suppress(Exception):
                pw.stop()
        return
    boot.put(None)
    try:
        while True:
            item = inbox.get()
            if item is None:
                break
            fn, reply = item
            try:
                reply.put(("ok", fn(ctx)))
            except BaseException as e:  # noqa: BLE001 — relay to caller thread
                reply.put(("err", e))
    finally:
        if ctx is not None:
            with contextlib.suppress(Exception):
                ctx.close()
        if browser is not None:
            with contextlib.suppress(Exception):
                browser.close()
        if pw is not None:
            with contextlib.suppress(Exception):
                pw.stop()


def _get_or_create(exec_id: str) -> _Entry:
    with _lock:
        entry = _browsers.get(exec_id)
        if entry is not None:
            return entry
        inbox: queue.Queue[tuple[_Op, queue.Queue[tuple[str, Any]]] | None] = queue.Queue()
        boot: queue.Queue[BaseException | None] = queue.Queue(maxsize=1)
        thread = threading.Thread(
            target=_worker,
            args=(inbox, boot),
            name=f"droids-agents-playwright-{exec_id[:8]}",
            daemon=True,
        )
        thread.start()
        err = boot.get(timeout=_WORKER_BOOT_TIMEOUT_SECONDS)
        if err is not None:
            raise RuntimeError(f"playwright worker boot failed: {err!r}") from err
        entry = _Entry(inbox=inbox, thread=thread, created_at=time.monotonic())
        _browsers[exec_id] = entry
        _ensure_sweep_running()
        return entry


def _call[T](exec_id: str, fn: Callable[[BrowserContext], T]) -> T:
    """Marshal ``fn`` onto the worker thread owning the ``exec_id`` context."""
    entry = _get_or_create(exec_id)
    reply: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)
    entry.inbox.put((fn, reply))
    status, val = reply.get()
    if status == "err":
        raise val  # type: ignore[misc]
    return val  # type: ignore[no-any-return]


def _ensure_sweep_running() -> None:
    global _sweep_thread
    if _sweep_thread is not None and _sweep_thread.is_alive():
        return
    _sweep_thread = threading.Thread(
        target=_sweep_forever,
        name="droids-agents-playwright-sweeper",
        daemon=True,
    )
    _sweep_thread.start()


def _sweep_forever() -> None:
    """Close any BrowserContext older than _SWEEP_AGE_SECONDS."""
    while True:
        time.sleep(300)
        now = time.monotonic()
        stale: list[str] = []
        with _lock:
            for exec_id, entry in _browsers.items():
                if now - entry.created_at > _SWEEP_AGE_SECONDS:
                    stale.append(exec_id)
        for exec_id in stale:
            with contextlib.suppress(Exception):
                close_context(exec_id)
            _log.warning("playwright: swept stale BrowserContext for exec_id=%s", exec_id)


def close_context(exec_id: str) -> None:
    """Tear down a BrowserContext + Playwright instance for one Execution."""
    with _lock:
        entry = _browsers.pop(exec_id, None)
    if entry is None:
        return
    entry.inbox.put(None)
    entry.thread.join(timeout=_WORKER_SHUTDOWN_TIMEOUT_SECONDS)


def _blocked_reason(url: str, *, title: str = "", text: str = "") -> str | None:
    """Return a reason when the current page is an anti-bot/challenge page."""
    url_lower = (url or "").lower()
    title_lower = (title or "").strip().lower()
    text_lower = (text or "").lower()

    if any(marker in url_lower for marker in _BLOCKED_URL_MARKERS):
        return "anti-bot challenge URL"
    if title_lower and any(marker in title_lower for marker in _BLOCKED_TITLE_MARKERS):
        return f"anti-bot challenge title: {title!r}"
    if any(marker in text_lower for marker in _BLOCKED_TEXT_MARKERS):
        return "anti-bot challenge body"
    return None


@tool
def web_navigate(url: str, context: ToolContext) -> dict:
    """Navigate the per-Execution browser to ``url``. Returns final URL + title.

    Returns a structured ``{"ok": False, "error": ...}`` on navigation failure
    (DNS, timeout, TLS) instead of raising — raised exceptions trip agentspan's
    consecutive-failure circuit breaker and disable the tool mid-Execution.
    """
    def op(ctx: BrowserContext) -> dict:
        page = ctx.pages[-1] if ctx.pages else ctx.new_page()
        page.goto(url, wait_until="domcontentloaded")
        title = page.title()
        if reason := _blocked_reason(page.url, title=title):
            return {
                "ok": False,
                "url": page.url,
                "title": title,
                "blocked": True,
                "error": reason,
            }
        return {"ok": True, "url": page.url, "title": title}

    try:
        return _call(context.execution_id, op)
    except Exception as e:
        return {"ok": False, "url": url, "error": f"{type(e).__name__}: {e}"}


@tool
def web_extract_text(context: ToolContext, selector: str | None = None) -> dict:
    """Extract visible text from the current page (or a CSS selector). Capped at 50k chars.

    Empty/whitespace-only extractions return ``ok: False``. This is the gate
    that prevents the leaf agent from emitting a ``CompetitorFinding`` off a
    page that loaded but yielded no text (JS-rendered SPA, paywall, 404 body).
    """
    def op(ctx: BrowserContext) -> dict:
        if not ctx.pages:
            return {
                "ok": False,
                "text": "",
                "truncated": False,
                "error": "no open page",
            }
        page = ctx.pages[-1]
        text = page.locator(selector).inner_text() if selector else page.inner_text("body")
        title = page.title()
        if reason := _blocked_reason(page.url, title=title, text=text):
            return {
                "ok": False,
                "text": "",
                "truncated": False,
                "blocked": True,
                "url": page.url,
                "title": title,
                "error": reason,
            }
        if not text or not text.strip():
            return {
                "ok": False,
                "text": "",
                "truncated": False,
                "error": "empty extraction",
            }
        truncated = len(text) > _TEXT_EXTRACT_CAP_CHARS
        return {
            "ok": True,
            "text": text[:_TEXT_EXTRACT_CAP_CHARS],
            "truncated": truncated,
        }

    try:
        return _call(context.execution_id, op)
    except Exception as e:
        return {"ok": False, "text": "", "truncated": False, "error": f"{type(e).__name__}: {e}"}


@tool
def web_screenshot(path: str, context: ToolContext) -> dict:
    """Take a PNG screenshot of the current page to ``path``."""
    def op(ctx: BrowserContext) -> dict:
        if not ctx.pages:
            return {"saved": False, "error": "no open page"}
        page = ctx.pages[-1]
        page.screenshot(path=path, full_page=True)
        return {"saved": True, "path": path}

    try:
        return _call(context.execution_id, op)
    except Exception as e:
        return {"saved": False, "path": path, "error": f"{type(e).__name__}: {e}"}


@tool
def web_fill(selector: str, value: str, context: ToolContext) -> dict:
    """Fill a form field. Side-effect-free in the sense of no network submit."""
    def op(ctx: BrowserContext) -> dict:
        if not ctx.pages:
            return {"filled": False, "error": "no open page"}
        page = ctx.pages[-1]
        page.locator(selector).fill(value)
        return {"filled": True, "selector": selector}

    try:
        return _call(context.execution_id, op)
    except Exception as e:
        return {"filled": False, "selector": selector, "error": f"{type(e).__name__}: {e}"}


@tool
def web_click(selector: str, context: ToolContext) -> dict:
    """Click an element. Use ``web_submit`` for form submissions (HITL-gated)."""
    def op(ctx: BrowserContext) -> dict:
        if not ctx.pages:
            return {"clicked": False, "error": "no open page"}
        page = ctx.pages[-1]
        page.locator(selector).click()
        return {"clicked": True, "selector": selector}

    try:
        return _call(context.execution_id, op)
    except Exception as e:
        return {"clicked": False, "selector": selector, "error": f"{type(e).__name__}: {e}"}


@tool(approval_required=True)
def web_submit(form_selector: str, context: ToolContext) -> dict:
    """Submit a form. Always pauses for HITL approval (irreversible side effect)."""
    def op(ctx: BrowserContext) -> dict:
        if not ctx.pages:
            return {"submitted": False, "error": "no open page"}
        page = ctx.pages[-1]
        with page.expect_navigation(wait_until="domcontentloaded"):
            page.locator(form_selector).evaluate(
                "(el) => el.submit ? el.submit() : el.click()"
            )
        return {"submitted": True, "response_url": page.url}

    try:
        return _call(context.execution_id, op)
    except Exception as e:
        return {
            "submitted": False,
            "form_selector": form_selector,
            "error": f"{type(e).__name__}: {e}",
        }
