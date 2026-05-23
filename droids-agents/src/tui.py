"""Textual TUI for live droids-agents executions.

Two screens:
- ``PromptScreen``: input field + submit (Ctrl+Enter or button).
- ``DashboardScreen``: steps progress, per-agent status table, scrolling event
  log, HITL pause panel with a clickable agentspan UI URL.

Event source contract (best-effort polling fallback):
- The execution thread calls ``agentspan.AgentRuntime.run`` synchronously in a
  worker; on completion the dashboard transitions to the terminal panel.
- While running, the dashboard polls ``agentspan`` for execution status via
  ``runtime.get_execution(exec_id)``. If the agentspan client does not expose
  that method, the dashboard simply shows "running…" until the run thread
  yields its result.
- HITL approvals are NOT handled inside the TUI — the panel surfaces the
  agentspan web URL (``http://localhost:6767/executions/<exec_id>``); user
  approves there.

This module is self-contained: it owns its own NamePool, build_root call, and
event loop. The click-mounted entrypoint is in ``cli.py``.
"""

from __future__ import annotations

import asyncio
import threading
import traceback
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.reactive import reactive
from textual.screen import Screen
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, RichLog, Static

import httpx

from droids_agents.config import Settings
from droids_agents.display import color_for_droid, color_for_role
from droids_agents.naming import NamePool
from droids_agents.pricing import usd_to_max_total_tokens
from droids_agents.router import build_root, classify_prompt, plan_mixed_steps
from droids_agents.runtime import connect_runtime
from droids_agents.schemas import label_to_task_type, LABEL_TO_TASK_TYPE
from droids_agents.slicing import slice_for
from droids_agents.tools.mem import MemFetchError, fetch_mem_context


def _ping_mem(settings: Settings) -> tuple[bool, str]:
    """Best-effort GET on droids-mem-mcp /healthz. Returns (ok, detail)."""
    healthz = settings.droids_mem_mcp_url.rsplit("/mcp", 1)[0].rstrip("/") + "/healthz"
    try:
        r = httpx.get(healthz, timeout=2.0)
    except httpx.HTTPError as e:
        return False, f"{healthz}: {e}"
    if r.status_code != 200:
        return False, f"{healthz}: HTTP {r.status_code}"
    return True, healthz


def _ping_agentspan(settings: Settings) -> tuple[bool, str]:
    """Best-effort GET on agentspan root. 200 / 404 both count as 'up'."""
    try:
        r = httpx.get(settings.agentspan_url, timeout=2.0)
    except httpx.HTTPError as e:
        return False, f"{settings.agentspan_url}: {e}"
    if r.status_code not in (200, 404):
        return False, f"{settings.agentspan_url}: HTTP {r.status_code}"
    return True, settings.agentspan_url

_POLL_INTERVAL_S: float = 1.0


@dataclass
class _RunState:
    """Owned by the worker thread; mutated under ``lock`` only."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    status: str = "preparing"
    exec_id: str = ""
    session_id: str = ""
    task_type: str = ""
    steps: list[str] = field(default_factory=list)
    agents: list[dict[str, str]] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    hitl: dict[str, Any] | None = None
    error: str | None = None
    final_output: Any = None

    def event(self, line: str) -> None:
        with self.lock:
            self.events.append(line)


def _roles_for(steps: list[str]) -> list[str]:
    mapping = {
        "research": ["competitor"],
        "docs": ["extractor", "synthesizer"],
        "form": ["form_planner", "form_executor"],
        "messaging": ["drafter", "sender"],
    }
    out: list[str] = []
    for s in steps:
        out.extend(mapping.get(s, []))
    return out


# --- Prompt screen --------------------------------------------------------


class PromptScreen(Screen):
    """First screen: enter prompt + optional flags, click Run."""

    BINDINGS = [Binding("ctrl+c", "app.quit", "Quit")]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="prompt-container"):
            yield Static(
                "[bold cyan]droids-agents[/]\n"
                "[dim]Enter a task prompt. Optional comma-separated competitor names.[/]",
                id="banner",
            )
            yield Input(placeholder="e.g. research Anthropic vs OpenAI pricing", id="prompt-input")
            yield Input(placeholder="competitors (optional, csv)", id="competitors-input")
            yield Input(placeholder="--max-cost-usd (optional, e.g. 0.5)", id="cost-input")
            with Horizontal(id="prompt-actions"):
                yield Button("Run", variant="primary", id="run-btn")
                yield Button("Quit", variant="default", id="quit-btn")
        yield Footer()

    @on(Button.Pressed, "#quit-btn")
    def _quit(self) -> None:
        self.app.exit()

    @on(Button.Pressed, "#run-btn")
    def _submit(self) -> None:
        prompt = self.query_one("#prompt-input", Input).value.strip()
        if not prompt:
            self.notify("prompt is required", severity="warning")
            return
        competitors = self.query_one("#competitors-input", Input).value.strip()
        cost_raw = self.query_one("#cost-input", Input).value.strip()
        max_cost: float | None = None
        if cost_raw:
            try:
                max_cost = float(cost_raw)
            except ValueError:
                self.notify(f"max-cost-usd not a float: {cost_raw}", severity="error")
                return
        self.app.push_screen(
            DashboardScreen(prompt=prompt, competitors_csv=competitors, max_cost_usd=max_cost)
        )


# --- Dashboard screen -----------------------------------------------------


class DashboardScreen(Screen):
    """Live dashboard: steps, agent status, event log, HITL pause."""

    BINDINGS = [
        Binding("ctrl+c", "app.quit", "Quit"),
        Binding("escape", "app.pop_screen", "Back"),
    ]

    status: reactive[str] = reactive("preparing")

    def __init__(
        self,
        *,
        prompt: str,
        competitors_csv: str,
        max_cost_usd: float | None,
    ) -> None:
        super().__init__()
        self._prompt = prompt
        self._competitors = [c.strip() for c in competitors_csv.split(",") if c.strip()]
        self._max_cost_usd = max_cost_usd
        self._state = _RunState()
        self._run_thread: threading.Thread | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="dash-container"):
            yield Static(self._prompt_banner(), id="prompt-banner")
            with Horizontal(id="status-row"):
                yield Label("status:", id="status-label")
                yield Static("preparing", id="status-text")
                yield Label("exec:", id="exec-label")
                yield Static("…", id="exec-text")
                yield Label("session:", id="session-label")
                yield Static("…", id="session-text")
            yield Static("steps: …", id="steps-text")
            yield DataTable(id="agent-table", zebra_stripes=True)
            with VerticalScroll(id="events-pane"):
                yield RichLog(id="event-log", highlight=True, markup=True, wrap=True)
            yield Static("", id="hitl-panel")
        yield Footer()

    def _prompt_banner(self) -> str:
        comp = ", ".join(self._competitors) if self._competitors else "(none)"
        return f"[bold]prompt:[/] {self._prompt}\n[dim]competitors:[/] {comp}"

    def on_mount(self) -> None:
        tbl = self.query_one("#agent-table", DataTable)
        tbl.add_columns("droid", "role", "status")
        self._start_run()
        self._poll_state()

    # --- worker thread management ---

    def _start_run(self) -> None:
        thread = threading.Thread(
            target=self._run_worker, name="droids-agents-run", daemon=True
        )
        thread.start()
        self._run_thread = thread

    def _run_worker(self) -> None:
        state = self._state
        try:
            settings = Settings.load()
            state.event("[green]settings loaded[/]")

            # Pre-flight: droids-mem-mcp /healthz. Surface result explicitly
            # so the user can see whether the memory backend is up BEFORE
            # any LLM call is made.
            mem_ok, mem_detail = _ping_mem(settings)
            if mem_ok:
                state.event(f"[bold green]droids-mem: connected[/] ({mem_detail})")
            else:
                state.event(f"[bold red]droids-mem: UNREACHABLE[/] ({mem_detail})")
                state.event(
                    "[red]hint: build with `go build ./cmd/droids-mem-mcp` in "
                    "droids-mem/, then run `DROIDS_MEM_MCP_TOKEN=$TOKEN "
                    "./droids-mem-mcp &`[/]"
                )
                state.error = f"droids-mem-mcp not reachable: {mem_detail}"
                state.status = "error"
                return

            # Pre-flight: agentspan reachable. Same surfacing.
            as_ok, as_detail = _ping_agentspan(settings)
            if as_ok:
                state.event(f"[bold green]agentspan: connected[/] ({as_detail})")
            else:
                state.event(f"[bold red]agentspan: UNREACHABLE[/] ({as_detail})")
                state.event(
                    "[red]hint: `cd ~/.droids-agents && agentspan server start`[/]"
                )
                state.error = f"agentspan not reachable: {as_detail}"
                state.status = "error"
                return

            label = classify_prompt(self._prompt, settings=settings)
            state.event(f"classifier → {label}")
            steps: list[str] = (
                plan_mixed_steps(self._prompt, settings=settings)
                if label == "mixed"
                else [label]
            )
            with state.lock:
                state.steps = steps
                state.task_type = label_to_task_type(steps[0])
            state.event(f"steps: {steps}")
            if "messaging" in steps and not settings.gmail_enabled:
                state.event(
                    "[bold red]ERROR[/] messaging step requested but Gmail not configured"
                )
                state.error = "Gmail is not configured; this prompt needs the messaging Subteam."
                state.status = "error"
                return

            try:
                mem_result = fetch_mem_context(
                    settings, task_type=state.task_type, query=self._prompt
                )
            except MemFetchError as e:
                state.event(f"[bold red]mem_context failed[/]: {e}")
                state.error = f"droids-mem unreachable: {e}"
                state.status = "error"
                return

            with state.lock:
                state.session_id = mem_result.session_id
            state.event(f"session_id minted: {mem_result.session_id}")

            slice_map = {
                role: slice_for(role, mem_result.bundle, self._prompt)
                for role in _roles_for(steps)
            }
            pool = NamePool()
            for role in _roles_for(steps):
                droid = pool.claim()
                with state.lock:
                    state.agents.append(
                        {"droid_name": droid, "role": role, "status": "pending"}
                    )

            max_total_tokens = (
                usd_to_max_total_tokens(self._max_cost_usd)
                if self._max_cost_usd is not None
                else None
            )
            root = build_root(
                settings,
                pool=pool,
                prompt=self._prompt,
                steps=steps,
                session_id=mem_result.session_id,
                competitors=self._competitors,
                docs_basenames=[],
                slice_map=slice_map,
                max_total_tokens=max_total_tokens,
            )

            state.status = "running"
            state.event("agentspan: running…")
            runtime = connect_runtime(settings)
            result = runtime.run(  # type: ignore[attr-defined]
                root,
                self._prompt,
                context={"task_type_override": None, "dry_run": False},
            )
            with state.lock:
                state.exec_id = getattr(result, "execution_id", "") or getattr(
                    result, "exec_id", ""
                )
                state.final_output = getattr(result, "output", None)
                if getattr(result, "is_waiting", False):
                    pending = getattr(result, "pending_approval", None) or {}
                    state.hitl = pending if isinstance(pending, dict) else {}
                    state.status = "waiting_for_hitl"
                else:
                    state.status = "done"
            state.event(f"agentspan: {state.status}")
        except Exception as exc:  # noqa: BLE001 — surface as TUI status
            state.error = f"{type(exc).__name__}: {exc}"
            state.status = "error"
            state.events.append(traceback.format_exc(limit=4))

    # --- polling loop ---

    @work(exclusive=True)
    async def _poll_state(self) -> None:
        log = self.query_one("#event-log", RichLog)
        tbl = self.query_one("#agent-table", DataTable)
        rendered_events = 0
        rendered_agents = 0
        while True:
            await asyncio.sleep(_POLL_INTERVAL_S)
            with self._state.lock:
                events_snap = list(self._state.events)
                agents_snap = list(self._state.agents)
                status = self._state.status
                exec_id = self._state.exec_id
                sess = self._state.session_id
                steps = list(self._state.steps)
                hitl = dict(self._state.hitl) if self._state.hitl else None
                error = self._state.error
                final = self._state.final_output

            for line in events_snap[rendered_events:]:
                log.write(line)
            rendered_events = len(events_snap)

            for row in agents_snap[rendered_agents:]:
                tbl.add_row(
                    Text(row["droid_name"], style=color_for_droid(row["droid_name"])),
                    Text(row["role"], style=color_for_role(row["role"].title())),
                    Text(row["status"], style="dim"),
                )
            rendered_agents = len(agents_snap)

            self.query_one("#status-text", Static).update(
                _status_text(status, error=error)
            )
            self.query_one("#exec-text", Static).update(exec_id or "…")
            self.query_one("#session-text", Static).update(sess or "…")
            self.query_one("#steps-text", Static).update(
                f"steps: {' → '.join(steps) if steps else '…'}"
            )

            if hitl is not None:
                self.query_one("#hitl-panel", Static).update(_hitl_text(hitl, exec_id))
            elif error:
                self.query_one("#hitl-panel", Static).update(
                    Text(f"error: {error}", style="bold red")
                )
            elif status == "done":
                self.query_one("#hitl-panel", Static).update(
                    Text(f"final output ready (see RichLog): {final!r}", style="bold green")
                )

            if status in {"done", "error", "waiting_for_hitl"}:
                return


# --- helpers --------------------------------------------------------------


def _status_text(status: str, *, error: str | None) -> Text:
    style_map = {
        "preparing": "yellow",
        "running": "bold cyan",
        "waiting_for_hitl": "bold red",
        "done": "bold green",
        "error": "bold red",
    }
    style = style_map.get(status, "white")
    return Text(status, style=style)


def _hitl_text(hitl: dict[str, Any], exec_id: str) -> Text:
    meta = hitl.get("metadata") or {}
    droid = meta.get("droid_name", "?")
    role = meta.get("role_label", meta.get("role", "?"))
    tool = hitl.get("tool_name", "?")
    args = hitl.get("tool_args") or {}
    reason = hitl.get("reason")
    url = f"http://localhost:6767/executions/{exec_id}" if exec_id else "(unknown)"
    text = Text()
    text.append("HITL PAUSE\n", style="bold red on grey15")
    text.append(droid, style=color_for_droid(droid))
    text.append(": [", style="dim")
    text.append(role, style=color_for_role(role))
    text.append("] ", style="dim")
    text.append(f"tool={tool}\n", style="bold yellow")
    if reason:
        text.append(f"reason: {reason}\n", style="red")
    for k, v in args.items():
        s = str(v)
        if len(s) > 200:
            s = s[:199] + "…"
        text.append(f"  {k}: {s}\n", style="dim white")
    text.append("approve at: ", style="dim")
    text.append(url, style="underline cyan")
    return text


# --- App ------------------------------------------------------------------


class DroidsAgentsApp(App):
    """Top-level Textual App."""

    CSS = """
    #prompt-container {
        padding: 2;
        align: center middle;
        width: 100%;
    }
    #banner { padding-bottom: 1; }
    #prompt-actions { padding-top: 1; }

    #dash-container { padding: 1; }
    #status-row { height: 3; padding: 0 1; }
    #status-row Label { padding-right: 1; }
    #status-row Static { padding-right: 2; }
    #steps-text { padding: 1; color: $accent; }
    #agent-table { height: 10; }
    #events-pane { height: 14; border: round $primary; }
    #event-log { padding: 1; }
    #hitl-panel { padding: 1; border: round $error; min-height: 4; }
    """

    def on_mount(self) -> None:
        self.push_screen(PromptScreen())


def run_tui() -> None:
    """Entrypoint invoked by the ``droids-agents tui`` subcommand."""
    # mouse=False so the terminal handles drag-to-select for copy.
    DroidsAgentsApp().run(mouse=True)
