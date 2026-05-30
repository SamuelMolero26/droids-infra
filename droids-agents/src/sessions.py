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

import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from droids_agents.config import Settings
from droids_agents.execution import (
    ExecutionError,
    build_execution,
    plan_execution,
)
from droids_agents.logging import SessionLogger
from droids_agents.naming import NamePool
from droids_agents.router import make_client
from droids_agents.runtime import connect_runtime, reset_tool_circuit_breakers

# Status lifecycle.
STARTING = "starting"
RUNNING = "running"
WAITING_HITL = "waiting_hitl"
DONE = "done"
ERROR = "error"
CLOSED = "closed"


@dataclass
class SessionSnapshot:
    """Immutable view the TUI renders. Cheap to copy under the lock."""

    status: str
    exec_id: str
    agentspan_url: str
    session_id: str
    steps: list[str]
    feed: list[str]
    messages: int
    tool_calls: int
    turns: int
    agents_seen: int
    error: str | None
    final_output: Any
    tasks_total: int = 0
    tasks_done: int = 0
    task_groups: dict[str, dict[str, int]] = field(default_factory=dict)
    eta_seconds: float | None = None
    progress_percent: float = 0.0
    elapsed_seconds: float | None = None


class SessionState:
    """Thread-safe live state for one session."""

    def __init__(self, prompt: str) -> None:
        self.prompt = prompt
        self._lock = threading.Lock()
        self.status = STARTING
        self.exec_id = ""
        self.agentspan_url = ""
        self.session_id = ""
        self.steps: list[str] = []
        self.feed: list[str] = []
        self.messages = 0
        self.tool_calls = 0
        self.turns = 0
        self._agents_seen: set[str] = set()
        self.error: str | None = None
        self.final_output: Any = None
        self._stream: Any = None  # AgentStream, once started
        self._runtime: Any = None  # AgentRuntime, set by attach_runtime
        self._stopped = False  # set by request_stop(); loop breaks, status → CLOSED
        self.started_at: float | None = None  # monotonic, set by attach_stream
        self.tasks_total = 0
        self.tasks_done = 0
        self.task_groups: dict[str, dict[str, int]] = {}

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

    def attach_runtime(self, runtime: Any) -> None:
        with self._lock:
            self._runtime = runtime

    def attach_stream(self, stream: Any) -> None:
        with self._lock:
            self._stream = stream
            self.exec_id = getattr(getattr(stream, "handle", None), "execution_id", "") or ""
            self.status = RUNNING
            self.started_at = time.monotonic()

    def update_task_progress(
        self, total: int, done: int, groups: dict[str, dict[str, int]]
    ) -> None:
        """Called by the task poller. Snapshot exposes the derived ETA."""
        with self._lock:
            self.tasks_total = total
            self.tasks_done = done
            self.task_groups = groups

    def request_stop(self) -> None:
        """Signal the run loop to stop and best-effort cancel the agentspan
        execution server-side. Idempotent. agentspan durable executions may not
        terminate instantly; the loop stops ingesting immediately regardless."""
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            if self.status not in (DONE, ERROR):
                self.status = CLOSED
            stream = self._stream
            runtime = self._runtime
            exec_id = self.exec_id
        # Cancel server-side execution first (covers HITL-paused workflows).
        if runtime is not None and exec_id:
            try:
                runtime.cancel(exec_id, reason="user requested stop")
            except Exception:  # noqa: BLE001 — best-effort
                pass
        # Also stop the local stream iterator.
        for target in (getattr(stream, "handle", None), stream):
            stop = getattr(target, "stop", None)
            if callable(stop):
                try:
                    stop()
                except Exception:  # noqa: BLE001
                    pass
                break

    @property
    def stopped(self) -> bool:
        with self._lock:
            return self._stopped

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
            elapsed = (
                time.monotonic() - self.started_at if self.started_at is not None else None
            )
            eta: float | None = None
            percent = 0.0
            if self.tasks_total > 0:
                percent = min(100.0, (self.tasks_done / self.tasks_total) * 100.0)
            if (
                elapsed is not None
                and self.tasks_done > 0
                and self.tasks_total > self.tasks_done
            ):
                eta = (elapsed / self.tasks_done) * (self.tasks_total - self.tasks_done)
            elif self.tasks_total > 0 and self.tasks_done >= self.tasks_total:
                eta = 0.0
            return SessionSnapshot(
                status=self.status,
                exec_id=self.exec_id,
                agentspan_url=self.agentspan_url,
                session_id=self.session_id,
                steps=list(self.steps),
                feed=list(self.feed),
                messages=self.messages,
                tool_calls=self.tool_calls,
                turns=self.turns,
                agents_seen=len(self._agents_seen),
                error=self.error,
                final_output=self.final_output,
                tasks_total=self.tasks_total,
                tasks_done=self.tasks_done,
                task_groups={k: dict(v) for k, v in self.task_groups.items()},
                eta_seconds=eta,
                progress_percent=percent,
                elapsed_seconds=elapsed,
            )


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
        """Drop a session from the registry, signalling its run loop to stop and
        best-effort cancelling the agentspan execution. The daemon thread exits on
        the next event (or once the stream unblocks); durable executions may take
        a moment to wind down server-side, but no further tokens are ingested."""
        with self._lock:
            handle = self._handles.pop(key, None)
        if handle is not None:
            handle.state.request_stop()


_TASK_REF_RE = re.compile(
    r".+?_(?:handoff|agent|step|sequential|parallel|round_robin|router|swarm|random|manual|transfer)_(?:\d+_)?(.*)"
)
_TERMINAL_TASK_STATUSES = {"COMPLETED", "FAILED", "SKIPPED", "TIMED_OUT", "CANCELED"}
_TERMINAL_WF_STATUSES = {"COMPLETED", "FAILED", "TERMINATED", "TIMED_OUT"}


def _short_ref(ref: str) -> str:
    m = _TASK_REF_RE.match(ref or "")
    return m.group(1) if m else (ref or "child")


def _gather_task_progress(
    workflow_client: Any,
    parent_exec_id: str,
) -> tuple[int, int, dict[str, dict[str, int]]]:
    """Walk parent workflow + one level of SUB_WORKFLOW children, summing tasks.

    agentspan emits parallel/handoff topologies as SUB_WORKFLOW tasks whose
    internal LLM/tool calls never reach the parent's task list. Following
    ``sub_workflow_id`` once is enough for V1 (research_team PARALLEL fan-out is
    a single nesting level). Returns (total, done, groups) where ``groups`` is
    keyed by short ref name → {"total", "done"} for per-agent surfacing.
    """
    groups: dict[str, dict[str, int]] = {}
    total = 0
    done = 0

    def add(group: str, t: int, d: int) -> None:
        nonlocal total, done
        g = groups.setdefault(group, {"total": 0, "done": 0})
        g["total"] += t
        g["done"] += d
        total += t
        done += d

    try:
        parent = workflow_client.get_workflow(parent_exec_id, include_tasks=True)
    except Exception:  # noqa: BLE001 — best-effort polling, never raise
        return 0, 0, {}

    parent_tasks = list(getattr(parent, "tasks", []) or [])
    own_t = own_d = 0
    sub_refs: list[tuple[str, str]] = []

    for t in parent_tasks:
        ttype = str(getattr(t, "task_type", "")).upper()
        tstatus = str(getattr(t, "status", "")).upper()
        if "SUB_WORKFLOW" in ttype:
            sub_id = getattr(t, "sub_workflow_id", None)
            ref = _short_ref(getattr(t, "reference_task_name", ""))
            if sub_id:
                sub_refs.append((ref, sub_id))
            else:
                own_t += 1
                if tstatus in _TERMINAL_TASK_STATUSES:
                    own_d += 1
        else:
            own_t += 1
            if tstatus in _TERMINAL_TASK_STATUSES:
                own_d += 1
    if own_t:
        add("root", own_t, own_d)

    for ref, sid in sub_refs:
        try:
            child = workflow_client.get_workflow(sid, include_tasks=True)
        except Exception:  # noqa: BLE001
            continue
        ctasks = list(getattr(child, "tasks", []) or [])
        ct = cd = 0
        for t in ctasks:
            ttype = str(getattr(t, "task_type", "")).upper()
            tstatus = str(getattr(t, "status", "")).upper()
            ct += 1
            if tstatus in _TERMINAL_TASK_STATUSES:
                cd += 1
            # one more nesting level (e.g. nested handoff/agent_tool) — count but
            # don't recurse deeper to keep poll cost bounded.
            if "SUB_WORKFLOW" in ttype:
                grand_id = getattr(t, "sub_workflow_id", None)
                if grand_id:
                    try:
                        grand = workflow_client.get_workflow(grand_id, include_tasks=True)
                    except Exception:  # noqa: BLE001
                        continue
                    for gt in list(getattr(grand, "tasks", []) or []):
                        ct += 1
                        if str(getattr(gt, "status", "")).upper() in _TERMINAL_TASK_STATUSES:
                            cd += 1
        if ct:
            add(ref, ct, cd)

    return total, done, groups


def _spawn_task_poller(
    state: SessionState,
    workflow_client: Any,
    interval_s: float = 1.5,
) -> tuple[threading.Thread, threading.Event]:
    """Start a daemon thread that refreshes task progress every ``interval_s``.

    Exits when ``stop`` is set, the session is stopped, or status hits a
    terminal state. Errors swallowed — visibility is best-effort.
    """
    stop = threading.Event()

    def loop() -> None:
        while not stop.is_set() and not state.stopped:
            exec_id = state.snapshot().exec_id
            if exec_id:
                total, done, groups = _gather_task_progress(workflow_client, exec_id)
                if total or groups:
                    state.update_task_progress(total, done, groups)
            if state.snapshot().status in (DONE, ERROR, CLOSED):
                return
            stop.wait(interval_s)

    thread = threading.Thread(target=loop, daemon=True, name="droids-agents-task-poller")
    thread.start()
    return thread, stop


def run_session(
    state: SessionState,
    *,
    settings: Settings,
    competitors: list[str],
    runtime_factory: Callable[[Settings], Any] = connect_runtime,
) -> None:
    """Plan → build → stream. Mutates ``state`` as events arrive. Designed to
    run in its own thread (the stream iteration blocks)."""
    session_logger = SessionLogger(getattr(settings, "log_dir", None))
    poller_stop: threading.Event | None = None
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
        )
        with state._lock:
            state.session_id = prepared.session_id
        session_logger.bind(prepared.session_id)
        session_logger.log("session_started", steps=plan.steps, competitors=competitors)

        with state._lock:
            state.agentspan_url = getattr(settings, "agentspan_url", "")
        reset_tool_circuit_breakers()
        runtime = runtime_factory(settings)
        state.attach_runtime(runtime)
        stream = runtime.stream(
            prepared.root,
            state.prompt,
            context={"task_type_override": None, "dry_run": False},
        )
        state.attach_stream(stream)
        wfc = getattr(runtime, "_workflow_client", None)
        if wfc is not None:
            _, poller_stop = _spawn_task_poller(state, wfc)
        for event in stream:
            if state.stopped:
                return  # tab closed — stop ingesting, skip finalize
            state.ingest(event)
        if state.stopped:
            return
        state.finalize(stream.get_result())
        session_logger.log("session_done")
    except ExecutionError as e:
        session_logger.log("session_error", code=e.code, message=e.message)
        state.fail(e.message)
    except Exception as e:  # noqa: BLE001 — surface any runtime/stream error
        session_logger.log("session_error", type=type(e).__name__, message=str(e))
        state.fail(f"{type(e).__name__}: {e}")
    finally:
        if poller_stop is not None:
            poller_stop.set()
        session_logger.close()
