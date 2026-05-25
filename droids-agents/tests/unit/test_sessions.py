"""run_session streaming runner — fake stream, stubbed plan/build (no LLM/mem)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from droids_agents import sessions
from droids_agents.execution import ExecutionPlan, PreparedExecution
from droids_agents.sessions import DONE, ERROR, RUNNING, WAITING_HITL, SessionState, run_session


def _event(type_, **kw):
    base = dict(content=None, tool_name=None, args=None, result=None, target=None,
               output=None, guardrail_name=None)
    base.update(kw)
    return SimpleNamespace(type=type_, **base)


class _FakeStream:
    def __init__(self, events, *, output=None, token_usage=None):
        self._events = events
        self.handle = SimpleNamespace(execution_id="exec_1", correlation_id="c")
        self._output = output
        self._token_usage = token_usage
        self.sent: list[str] = []

    def __iter__(self):
        return iter(self._events)

    def get_result(self):
        return SimpleNamespace(execution_id="exec_1", output=self._output,
                               token_usage=self._token_usage)

    def send(self, msg):
        self.sent.append(msg)


def _fake_runtime(stream):
    return SimpleNamespace(stream=lambda root, prompt, context=None: stream)


def _stub_plan_build(monkeypatch):
    plan = ExecutionPlan(steps=["research"], task_type="competitor_research",
                         competitors=["OpenAI"])
    monkeypatch.setattr(sessions, "plan_execution", lambda **kw: plan)
    monkeypatch.setattr(sessions, "make_client", lambda settings: object())
    prepared = PreparedExecution(plan=plan, session_id="sess_x", root=object(),
                                 roles=["competitor"])
    monkeypatch.setattr(sessions, "build_execution", lambda **kw: prepared)


def test_run_session_streams_to_done(monkeypatch) -> None:
    _stub_plan_build(monkeypatch)
    events = [
        _event("message", content="hello"),
        _event("tool_call", tool_name="web_navigate", args={"url": "x"}),
        _event("tool_result", tool_name="web_navigate"),
        _event("message", content="found it"),
        _event("done", output={"result": "ok"}),
    ]
    stream = _FakeStream(events, output={"result": "ok"})
    state = SessionState("research OpenAI")
    run_session(state, settings=object(), competitors=["OpenAI"],
                runtime_factory=lambda s: _fake_runtime(stream))
    snap = state.snapshot()
    assert snap.status == DONE
    assert snap.messages == 2
    assert snap.tool_calls == 1
    assert snap.exec_id == "exec_1"
    assert snap.session_id == "sess_x"
    assert snap.agents_total == 1  # roles_for_steps(["research"]) == ["competitor"]


def test_run_session_error_event(monkeypatch) -> None:
    _stub_plan_build(monkeypatch)
    stream = _FakeStream([_event("error", content="boom")])
    state = SessionState("x")
    run_session(state, settings=object(), competitors=["X"],
                runtime_factory=lambda s: _fake_runtime(stream))
    snap = state.snapshot()
    assert snap.status == ERROR
    assert snap.error == "boom"


def test_run_session_waiting_then_send(monkeypatch) -> None:
    _stub_plan_build(monkeypatch)
    stream = _FakeStream([_event("waiting")])
    state = SessionState("x")
    run_session(state, settings=object(), competitors=["X"],
                runtime_factory=lambda s: _fake_runtime(stream))
    # waiting event seen → status WAITING_HITL, stream finalized to DONE after iter ends.
    # send() forwards to the attached stream.
    assert state.send("approve please") is True
    assert stream.sent == ["approve please"]


def test_run_session_plan_error_surfaces(monkeypatch) -> None:
    from droids_agents.execution import CompetitorsRequired

    def _raise(**kw):
        raise CompetitorsRequired("competitors_required", "need names")

    monkeypatch.setattr(sessions, "make_client", lambda settings: object())
    monkeypatch.setattr(sessions, "plan_execution", _raise)
    state = SessionState("x")
    run_session(state, settings=object(), competitors=[],
                runtime_factory=lambda s: _fake_runtime(_FakeStream([])))
    snap = state.snapshot()
    assert snap.status == ERROR
    assert "need names" in snap.error


def test_send_without_stream_returns_false() -> None:
    state = SessionState("x")
    assert state.send("hi") is False


# --- SessionRegistry ---

from droids_agents.sessions import RegistryFull, SessionRegistry  # noqa: E402


def _noop_runner(state, *, settings, competitors, max_total_tokens):
    state.note("ran")
    state.status = DONE


def test_registry_spawn_and_cap() -> None:
    reg = SessionRegistry(cap=2)
    h1 = reg.spawn(prompt="a", competitors=[], settings=object(), runner=_noop_runner)
    h2 = reg.spawn(prompt="b", competitors=[], settings=object(), runner=_noop_runner)
    assert {h1.key, h2.key} == {"S1", "S2"}
    assert len(reg) == 2
    with pytest.raises(RegistryFull):
        reg.spawn(prompt="c", competitors=[], settings=object(), runner=_noop_runner)
    h1.thread.join(timeout=1)
    h2.thread.join(timeout=1)


def test_registry_runner_receives_state_and_runs() -> None:
    reg = SessionRegistry(cap=3)
    h = reg.spawn(prompt="go", competitors=["X"], settings=object(), runner=_noop_runner)
    h.thread.join(timeout=1)
    snap = h.state.snapshot()
    assert snap.status == DONE
    assert "ran" in snap.feed


def test_registry_get_all_close() -> None:
    reg = SessionRegistry(cap=3)
    h = reg.spawn(prompt="a", competitors=[], settings=object(), runner=_noop_runner)
    assert reg.get(h.key) is h
    assert reg.all() == [h]
    reg.close(h.key)
    assert reg.get(h.key) is None
    assert len(reg) == 0
    h.thread.join(timeout=1)


def test_registry_close_frees_a_slot() -> None:
    reg = SessionRegistry(cap=1)
    h1 = reg.spawn(prompt="a", competitors=[], settings=object(), runner=_noop_runner)
    with pytest.raises(RegistryFull):
        reg.spawn(prompt="b", competitors=[], settings=object(), runner=_noop_runner)
    reg.close(h1.key)
    h2 = reg.spawn(prompt="b", competitors=[], settings=object(), runner=_noop_runner)
    assert h2.key == "S2"  # counter keeps incrementing
    h1.thread.join(timeout=1)
    h2.thread.join(timeout=1)
