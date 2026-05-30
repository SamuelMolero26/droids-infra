"""Playwright tool contract tests that avoid launching a real browser.

The tools are thin wrappers over a closure that runs on the per-Execution
worker thread (see ``_call``). These tests inject a fake ``_call`` that
invokes the closure synchronously against a fake ``BrowserContext``.
"""

from __future__ import annotations

import inspect
import queue
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest
from droids_agents.tools import playwright as pwtools


class _FakePage:
    def __init__(self) -> None:
        self.url = "about:blank"
        self.title_text = "Example"
        self.body_text = "Visible page text"

    def goto(self, url: str, *, wait_until: str) -> None:
        assert wait_until == "domcontentloaded"
        self.url = url

    def title(self) -> str:
        return self.title_text

    def inner_text(self, selector: str) -> str:
        assert selector == "body"
        return self.body_text


class _FakeContext:
    def __init__(self) -> None:
        self.pages: list[_FakePage] = []

    def new_page(self) -> _FakePage:
        page = _FakePage()
        self.pages.append(page)
        return page


def _install_fake_call(monkeypatch: pytest.MonkeyPatch, ctx: _FakeContext) -> None:
    def fake_call(exec_id: str, fn: Any) -> Any:
        return fn(ctx)

    monkeypatch.setattr(pwtools, "_call", fake_call)


def test_playwright_tools_are_sync_for_agentspan_dispatcher() -> None:
    assert not inspect.iscoroutinefunction(pwtools.web_navigate)
    assert not inspect.iscoroutinefunction(pwtools.web_extract_text)
    assert not inspect.iscoroutinefunction(pwtools.web_submit)


def test_web_navigate_returns_serializable_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _FakeContext()
    _install_fake_call(monkeypatch, ctx)

    out = pwtools.web_navigate(
        "https://example.test", SimpleNamespace(execution_id="exec-1")
    )

    assert out == {"ok": True, "url": "https://example.test", "title": "Example"}


def test_web_extract_text_returns_structured_success(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _FakeContext()
    page = ctx.new_page()
    page.url = "https://example.test"
    _install_fake_call(monkeypatch, ctx)

    out = pwtools.web_extract_text(SimpleNamespace(execution_id="exec-1"))

    assert out == {
        "ok": True,
        "text": "Visible page text",
        "truncated": False,
    }


def test_web_extract_text_reports_no_open_page_when_context_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _FakeContext()
    _install_fake_call(monkeypatch, ctx)

    out = pwtools.web_extract_text(SimpleNamespace(execution_id="exec-1"))

    assert out["ok"] is False
    assert out["error"] == "no open page"


def test_web_navigate_marks_cloudflare_challenge_as_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _FakeContext()
    page = ctx.new_page()

    def _goto(url: str, *, wait_until: str) -> None:
        assert wait_until == "domcontentloaded"
        page.url = f"{url}?__cf_chl_rt_tk=token"
        page.title_text = ""

    page.goto = _goto  # type: ignore[method-assign]
    _install_fake_call(monkeypatch, ctx)

    out = pwtools.web_navigate(
        "https://openai.com/api/pricing/", SimpleNamespace(execution_id="exec-1")
    )

    assert out["ok"] is False
    assert out["blocked"] is True
    assert "anti-bot" in out["error"]


def test_web_extract_text_marks_challenge_body_as_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _FakeContext()
    page = ctx.new_page()
    page.url = "https://example.test"
    page.body_text = "Checking your browser before accessing this site. Cloudflare"
    _install_fake_call(monkeypatch, ctx)

    out = pwtools.web_extract_text(SimpleNamespace(execution_id="exec-1"))

    assert out["ok"] is False
    assert out["blocked"] is True
    assert out["text"] == ""


def test_call_marshals_ops_onto_single_owning_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: nav on thread A + extract on thread B both reach the same
    worker thread, so the BrowserContext is never accessed cross-thread.

    The original bug was that ``sync_playwright`` is thread-bound and
    ``ctx.pages`` returned an empty list when ``web_extract_text`` ran on a
    different worker thread than ``web_navigate``.
    """
    ctx = _FakeContext()
    observed_thread_ids: list[int] = []

    def fake_get_or_create(exec_id: str) -> pwtools._Entry:
        inbox: queue.Queue[Any] = queue.Queue()

        def loop() -> None:
            while True:
                item = inbox.get()
                if item is None:
                    return
                fn, reply = item
                observed_thread_ids.append(threading.get_ident())
                try:
                    reply.put(("ok", fn(ctx)))
                except BaseException as e:  # noqa: BLE001
                    reply.put(("err", e))

        thread = threading.Thread(target=loop, daemon=True)
        thread.start()
        return pwtools._Entry(inbox=inbox, thread=thread, created_at=time.monotonic())

    entry = fake_get_or_create("exec-1")
    monkeypatch.setattr(pwtools, "_get_or_create", lambda exec_id: entry)

    results: list[dict] = []

    def call_nav() -> None:
        results.append(
            pwtools.web_navigate(
                "https://example.test", SimpleNamespace(execution_id="exec-1")
            )
        )

    def call_extract() -> None:
        results.append(pwtools.web_extract_text(SimpleNamespace(execution_id="exec-1")))

    t_a = threading.Thread(target=call_nav)
    t_a.start()
    t_a.join(timeout=5)
    t_b = threading.Thread(target=call_extract)
    t_b.start()
    t_b.join(timeout=5)

    entry.inbox.put(None)
    entry.thread.join(timeout=5)

    assert results[0] == {"ok": True, "url": "https://example.test", "title": "Example"}
    assert results[1]["ok"] is True
    assert results[1]["text"] == "Visible page text"
    assert len(set(observed_thread_ids)) == 1, (
        "all Playwright ops must run on the single owning worker thread"
    )
