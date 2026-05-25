"""Live session state + streaming runner.

Phase 2: one streaming session. ``run_session`` builds the Execution
(``execution.plan_execution`` + ``build_execution``) then iterates
``runtime.stream(...)`` — replacing the old blocking ``runtime.run`` so the TUI
sees live progress (messages, tool calls, status) instead of nothing-until-done.

``SessionState`` is thread-safe: the runner thread mutates it under a lock; the
TUI polls ``snapshot()``. ``runtime_factory`` is injected so tests drive a fake
stream with no agentspan server.

Phase 3 will wrap N of these in a capped SessionRegistry.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from droids_agents.config import Settings
from droids_agents.execution import (
    ExecutionError,
    build_execution,
    plan_execution,
    roles_for_steps,
)
from droids_agents.naming import NamePool
from droids_agents.router import make_client
from droids_agents.runtime import connect_runtime

# Status lifecycle.
STARTING = "starting"
RUNNING = "running"
WAITING_HITL = "waiting_hitl"
DONE = "done"
ERROR = "error"


@dataclass
class SessionSnapshot:
    """Immutable view the TUI renders. Cheap to copy under the lock."""

    status: str
    exec_id: str
    session_id: str
    steps: list[str]
    feed: list[str]
    messages: int
    tool_calls: int
    turns: int
    agents_total: int
    agents_seen: int
    cost_usd: float | None
    error: str | None
    final_output: Any


class SessionState:
    """Thread-safe live state for one session."""

    def __init__(self, prompt: str) -> None:
        self.prompt = prompt
        self._lock = threading.Lock()
        self.status = STARTING
        self.exec_id = ""
        self.session_id = ""
        self.steps: list[str] = []
        self.feed: list[str] = []
        self.messages = 0
        self.tool_calls = 0
        self.turns = 0
        self.agents_total = 0
        self._agents_seen: set[str] = set()
        self.cost_usd: float | None = None
        self.error: str | None = None
        self.final_output: Any = None
        self._stream: Any = None  # AgentStream, once started

    # --- runner-side mutations ---

    def _push(self, line: str) -> None:
        self.feed.append(line)

    def note(self, line: str) -> None:
        """Append a free-form line to the feed (pre-flight / status messages)."""
        with self._lock:
            self._push(line)

    def set_plan(self, steps: list[str]) -> None:
        with self._lock:
            self.steps = list(steps)
            self.agents_total = len(roles_for_steps(steps))  # type: ignore[arg-type]

    def attach_stream(self, stream: Any) -> None:
        with self._lock:
            self._stream = stream
            self.exec_id = getattr(getattr(stream, "handle", None), "execution_id", "") or ""
            self.status = RUNNING

    def ingest(self, event: Any) -> None:
        """Fold one AgentEvent into the state. ``event.type`` is a str enum."""
        t = getattr(event, "type", "")
        with self._lock:
            if t == "message":
                self.messages += 1
                self.turns += 1
                if event.content:
                    self._push(f"[message] {event.content}")
            elif t == "thinking":
                if event.content:
                    self._push(f"[dim]…{event.content}[/dim]")
            elif t == "tool_call":
                self.tool_calls += 1
                self._push(f"[tool] {event.tool_name}({event.args or {}})")
            elif t == "tool_result":
                self._push(f"[tool✓] {event.tool_name}")
            elif t == "handoff":
                if event.target:
                    self._agents_seen.add(event.target)
                    self._push(f"[handoff] → {event.target}")
            elif t == "waiting":
                self.status = WAITING_HITL
                self._push("[yellow]waiting for human input[/yellow]")
            elif t == "guardrail_fail":
                self._push(f"[red]guardrail fail: {event.guardrail_name}[/red]")
            elif t == "error":
                self.status = ERROR
                self.error = event.content or "stream error"
                self._push(f"[red]error: {self.error}[/red]")
            elif t == "done":
                self.final_output = event.output

    def finalize(self, result: Any) -> None:
        with self._lock:
            if self.status != ERROR:
                self.status = DONE
            self.exec_id = getattr(result, "execution_id", self.exec_id) or self.exec_id
            tu = getattr(result, "token_usage", None)
            if tu is not None:
                self.cost_usd = _usd_from_tokens(tu)
            out = getattr(result, "output", None)
            if out is not None:
                self.final_output = out

    def fail(self, message: str) -> None:
        with self._lock:
            self.status = ERROR
            self.error = message
            self._push(f"[red]{message}[/red]")

    def send(self, message: str) -> bool:
        """Forward a user message to a waiting agent (HITL / join). Returns
        True if a stream was available to receive it."""
        with self._lock:
            stream = self._stream
        if stream is None:
            return False
        # AgentStream.send delegates to respond({"message": ...}).
        send = getattr(stream, "send", None)
        if send is None:
            return False
        send(message)
        with self._lock:
            self._push(f"[cyan]you: {message}[/cyan]")
            self.status = RUNNING
        return True

    def snapshot(self) -> SessionSnapshot:
        with self._lock:
            return SessionSnapshot(
                status=self.status,
                exec_id=self.exec_id,
                session_id=self.session_id,
                steps=list(self.steps),
                feed=list(self.feed),
                messages=self.messages,
                tool_calls=self.tool_calls,
                turns=self.turns,
                agents_total=self.agents_total,
                agents_seen=len(self._agents_seen),
                cost_usd=self.cost_usd,
                error=self.error,
                final_output=self.final_output,
            )


def _usd_from_tokens(token_usage: Any) -> float | None:
    """Best-effort cost. agentspan TokenUsage may already carry a cost field;
    fall back to None if not derivable here."""
    for attr in ("cost_usd", "total_cost", "cost"):
        v = getattr(token_usage, attr, None)
        if isinstance(v, (int, float)):
            return float(v)
    return None


DEFAULT_CAP = 3


class RegistryFull(RuntimeError):
    """Spawn rejected: the concurrency cap is reached."""


@dataclass
class SessionHandle:
    key: str
    state: SessionState
    thread: threading.Thread
    competitors: list[str]


class SessionRegistry:
    """Cap-bounded set of concurrent live sessions, one worker thread each.

    Keys are local (``S1``, ``S2``…) — distinct from droids-mem ``session_id``,
    which is only minted mid-run by build_execution. ``runner`` is injected so
    tests avoid real agentspan threads.
    """

    def __init__(self, cap: int = DEFAULT_CAP) -> None:
        self.cap = cap
        self._handles: dict[str, SessionHandle] = {}
        self._counter = 0
        self._lock = threading.Lock()

    def __len__(self) -> int:
        with self._lock:
            return len(self._handles)

    def spawn(
        self,
        *,
        prompt: str,
        competitors: list[str],
        settings: Settings,
        max_total_tokens: int | None = None,
        runner: Callable[..., None] = None,  # type: ignore[assignment]
    ) -> SessionHandle:
        runner = runner or run_session
        with self._lock:
            if len(self._handles) >= self.cap:
                raise RegistryFull(f"session cap reached ({self.cap})")
            self._counter += 1
            key = f"S{self._counter}"
        state = SessionState(prompt)
        thread = threading.Thread(
            target=runner,
            args=(state,),
            kwargs={
                "settings": settings,
                "competitors": competitors,
                "max_total_tokens": max_total_tokens,
            },
            name=f"droids-agents-{key}",
            daemon=True,
        )
        handle = SessionHandle(key=key, state=state, thread=thread, competitors=competitors)
        with self._lock:
            self._handles[key] = handle
        thread.start()
        return handle

    def get(self, key: str) -> SessionHandle | None:
        with self._lock:
            return self._handles.get(key)

    def all(self) -> list[SessionHandle]:
        with self._lock:
            return list(self._handles.values())

    def close(self, key: str) -> None:
        """Drop a session from the registry. The daemon thread is left to finish
        (agentspan executions are durable server-side and cannot be force-killed
        from here)."""
        with self._lock:
            self._handles.pop(key, None)


def run_session(
    state: SessionState,
    *,
    settings: Settings,
    competitors: list[str],
    max_total_tokens: int | None = None,
    runtime_factory: Callable[[Settings], Any] = connect_runtime,
) -> None:
    """Plan → build → stream. Mutates ``state`` as events arrive. Designed to
    run in its own thread (the stream iteration blocks)."""
    try:
        client = make_client(settings)
        plan = plan_execution(
            settings=settings,
            client=client,
            prompt=state.prompt,
            competitors=competitors,
            task_type_override=None,
        )
        state.set_plan(plan.steps)
        pool = NamePool()
        prepared = build_execution(
            settings=settings,
            plan=plan,
            prompt=state.prompt,
            pool=pool,
            docs_basenames=[],
            session_id_override=None,
            max_total_tokens=max_total_tokens,
        )
        with state._lock:
            state.session_id = prepared.session_id

        runtime = runtime_factory(settings)
        stream = runtime.stream(
            prepared.root,
            state.prompt,
            context={"task_type_override": None, "dry_run": False},
        )
        state.attach_stream(stream)
        for event in stream:
            state.ingest(event)
        state.finalize(stream.get_result())
    except ExecutionError as e:
        state.fail(e.message)
    except Exception as e:  # noqa: BLE001 — surface any runtime/stream error
        state.fail(f"{type(e).__name__}: {e}")
