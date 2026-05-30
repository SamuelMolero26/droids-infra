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
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import httpx
from droids_agents import memquery, sessions
from droids_agents.config import Settings
from droids_agents.memquery import Memory, MemQueryError, Session
from droids_agents.sessions import RegistryFull, SessionRegistry, SessionState
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    ProgressBar,
    RichLog,
    Static,
    TabbedContent,
    TabPane,
)


def _ensure_droids_mem() -> tuple[bool, str]:
    """Best-effort ensure-server call. Returns (ok, detail)."""
    gopath_bin = Path(os.environ.get("GOPATH", "")).expanduser() / "bin" / "droids-mem"
    binary = (
        shutil.which("droids-mem")
        or (str(gopath_bin) if gopath_bin.exists() else None)
        or str(Path.home() / "go" / "bin" / "droids-mem")
    )
    if not Path(binary).exists():
        return False, f"binary not found at {binary}"
    try:
        result = subprocess.run(
            [binary, "ensure-server"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return True, result.stdout.strip()
        return False, result.stderr.strip() or f"exit {result.returncode}"
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)


def _ping_mem(settings: Settings) -> tuple[bool, str]:
    """Best-effort GET on droids-mem /healthz. Returns (ok, detail)."""
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


def _load_logo() -> str:
    """ASCII logo for the prompt screen. Falls back to a plain title."""
    try:
        return (Path(__file__).parent / "assets" / "logo.txt").read_text(
            encoding="utf-8"
        ).rstrip("\n")
    except OSError:
        return "droids-agents"


_LOGO = _load_logo()


# --- Prompt screen --------------------------------------------------------


class PromptScreen(Screen):
    """First screen: enter prompt + optional flags, click Run."""

    BINDINGS = [
        Binding("ctrl+c", "app.quit", "Quit"),
        Binding("ctrl+p", "browse_sessions", "Sessions"),
    ]

    def action_browse_sessions(self) -> None:
        self.app.push_screen(SessionBrowserScreen())

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="prompt-container"):
            yield Static(Text(_LOGO, no_wrap=True), id="logo")
            yield Static(
                "[bold cyan]droids-agents[/]\n"
                "[dim]Enter a task prompt. Optional comma-separated competitor names.[/]",
                id="banner",
            )
            yield Input(placeholder="e.g. research Anthropic vs OpenAI pricing", id="prompt-input")
            yield Input(placeholder="competitors (optional, csv)", id="competitors-input")
            with Horizontal(id="prompt-actions"):
                yield Button("Run", variant="primary", id="run-btn")
                yield Button("Quit", variant="default", id="quit-btn")
        yield Footer()

    @on(Button.Pressed, "#quit-btn")
    def _quit(self) -> None:
        self.app.exit()

    @on(Input.Submitted, "#prompt-input")
    @on(Button.Pressed, "#run-btn")
    def _submit(self) -> None:
        prompt = self.query_one("#prompt-input", Input).value.strip()
        if not prompt:
            self.notify("prompt is required", severity="warning")
            return
        competitors = self.query_one("#competitors-input", Input).value.strip()
        self.app.push_screen(
            SessionsScreen(
                first_prompt=prompt,
                first_competitors_csv=competitors,
            )
        )


# --- Session browser + search (Phase 1: read-only over droids-mem) --------


def _fmt_ts(epoch: int) -> str:
    import datetime as _dt

    if not epoch:
        return "—"
    return _dt.datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M")


class SessionBrowserScreen(Screen):
    """Ctrl+P browser: previous sessions saved to droids-mem (image 3)."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("slash", "search", "Search"),
        Binding("j", "cursor_down", "Down"),
        Binding("k", "cursor_up", "Up"),
        Binding("r", "reload", "Reload"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._sessions: list[Session] = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="browser-container"):
            yield Static("[bold]Previous sessions[/] — droids-mem", id="browser-title")
            yield DataTable(id="sessions-table", zebra_stripes=True, cursor_type="row")
            yield Static(
                "[dim]j/k navigate • enter detail • / search • r reload • esc back[/]",
                id="browser-hint",
            )
        yield Footer()

    def on_mount(self) -> None:
        tbl = self.query_one("#sessions-table", DataTable)
        tbl.add_columns("when", "task_type", "title", "mems")
        self._load()

    def action_reload(self) -> None:
        self._load()

    @work(thread=True, exclusive=True)
    def _load(self) -> None:
        try:
            sessions = memquery.list_sessions()
        except MemQueryError as e:
            self.app.call_from_thread(
                self.notify, f"droids-mem: {e}", severity="error"
            )
            return
        self.app.call_from_thread(self._populate, sessions)

    def _populate(self, sessions: list[Session]) -> None:
        self._sessions = sessions
        tbl = self.query_one("#sessions-table", DataTable)
        tbl.clear()
        for s in sessions:
            tbl.add_row(_fmt_ts(s.created_at), s.task_type or "—", s.title, str(len(s.memories)))
        tbl.focus()
        if not sessions:
            self.notify("no sessions saved yet", severity="information")

    def action_cursor_down(self) -> None:
        self.query_one("#sessions-table", DataTable).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#sessions-table", DataTable).action_cursor_up()

    def action_search(self) -> None:
        self.app.push_screen(SearchScreen())

    @on(DataTable.RowSelected, "#sessions-table")
    def _open(self, event: DataTable.RowSelected) -> None:
        row = event.cursor_row
        if 0 <= row < len(self._sessions):
            self.app.push_screen(SessionDetailScreen(self._sessions[row]))


class SessionDetailScreen(Screen):
    """One session's stored memories (the rollup: summary + patterns/errors/rules)."""

    BINDINGS = [Binding("escape", "app.pop_screen", "Back")]

    def __init__(self, session: Session) -> None:
        super().__init__()
        self._session = session

    def compose(self) -> ComposeResult:
        s = self._session
        yield Header(show_clock=True)
        with Vertical(id="detail-container"):
            yield Static(
                f"[bold]{s.title}[/]\n[dim]{s.task_type} • {s.session_id} • {_fmt_ts(s.created_at)}[/]",
                id="detail-title",
            )
            tbl = DataTable(id="detail-table", zebra_stripes=True, cursor_type="row")
            yield tbl
            yield Static("[dim]esc back[/]", id="detail-hint")
        yield Footer()

    def on_mount(self) -> None:
        tbl = self.query_one("#detail-table", DataTable)
        tbl.add_columns("kind", "title", "learned")
        for m in self._session.memories:
            learned = (m.learned or m.what or "")[:80]
            tbl.add_row(m.kind, m.title, learned)


class SearchScreen(Screen):
    """Search memories across droids-mem (image 4)."""

    BINDINGS = [Binding("escape", "app.pop_screen", "Back")]

    def __init__(self) -> None:
        super().__init__()
        self._results: list[Memory] = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="search-container"):
            yield Static("[bold]Search memories[/]", id="search-title")
            yield Input(placeholder="Search memories…", id="search-input")
            yield DataTable(id="results-table", zebra_stripes=True, cursor_type="row")
            yield Static("[dim]type a query and press enter • esc back[/]", id="search-hint")
        yield Footer()

    def on_mount(self) -> None:
        tbl = self.query_one("#results-table", DataTable)
        tbl.add_columns("kind", "title", "task_type", "learned")
        self.query_one("#search-input", Input).focus()

    @on(Input.Submitted, "#search-input")
    def _on_query(self, event: Input.Submitted) -> None:
        self._run_search(event.value.strip())

    @work(thread=True, exclusive=True)
    def _run_search(self, query: str) -> None:
        if not query:
            return
        try:
            results = memquery.search(query)
        except MemQueryError as e:
            self.app.call_from_thread(self.notify, f"droids-mem: {e}", severity="error")
            return
        self.app.call_from_thread(self._populate, results)

    def _populate(self, results: list[Memory]) -> None:
        self._results = results
        tbl = self.query_one("#results-table", DataTable)
        tbl.clear()
        for m in results:
            tbl.add_row(m.kind, m.title, m.task_type or "—", (m.learned or "")[:60])
        self.notify(f"{len(results)} result(s)", severity="information")


# --- Session pane + multi-session screen (Phase 3) ------------------------


def _csv_list(raw: str) -> list[str]:
    return [c.strip() for c in raw.split(",") if c.strip()]


class SessionPane(Vertical):
    """One live session: conversation feed (left) + Statistics (right) + join
    input (bottom). Polls a SessionState the registry already drives — does NOT
    start the worker itself. Holds direct widget refs so multiple panes coexist
    in tabs without id collisions."""

    def __init__(self, state: SessionState) -> None:
        super().__init__()
        self._state = state
        self._log = RichLog(highlight=True, markup=True, wrap=True, classes="pane-log")
        self._stats = Static("", classes="pane-stats-text")
        # ETA is computed in SessionState (rate from monotonic start), so the
        # bar's own ETA renderer is suppressed via show_eta=False.
        self._progress = ProgressBar(
            total=None,
            show_eta=False,
            show_percentage=True,
            classes="pane-progress",
        )
        self._eta = Static("ETA: —", classes="pane-eta")
        self._input = Input(
            placeholder="Type your message to join the conversation…", classes="pane-input"
        )
        self._rendered = 0

    def compose(self) -> ComposeResult:
        with Horizontal(classes="pane-row"):
            with VerticalScroll(classes="pane-feed"):
                yield self._log
            with Vertical(classes="pane-stats"):
                yield self._stats
                yield self._progress
                yield self._eta
        yield self._input

    def on_mount(self) -> None:
        self._poll()

    @on(Input.Submitted)
    def _join(self, event: Input.Submitted) -> None:
        msg = event.value.strip()
        if msg and not self._state.send(msg):
            self.notify("no active session to send to", severity="warning")
        self._input.value = ""

    @work(exclusive=True)
    async def _poll(self) -> None:
        while True:
            await asyncio.sleep(_POLL_INTERVAL_S)
            snap = self._state.snapshot()
            for line in snap.feed[self._rendered:]:
                self._log.write(line)
            self._rendered = len(snap.feed)
            self._stats.update(_stats_text(snap))
            self._refresh_progress(snap)
            if snap.status in {sessions.DONE, sessions.ERROR, sessions.CLOSED}:
                return

    def _refresh_progress(self, snap: sessions.SessionSnapshot) -> None:
        if snap.tasks_total > 0:
            self._progress.update(total=snap.tasks_total, progress=snap.tasks_done)
        else:
            # No task data yet — keep indeterminate (total=None).
            self._progress.update(total=None, progress=0)
        self._eta.update(_eta_text(snap))


class NewSessionModal(ModalScreen):
    """Prompt for a new concurrent session (Ctrl+N). Dismisses with
    (prompt, competitors) or None on cancel."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-box"):
            yield Static("[bold]New session[/]")
            yield Input(placeholder="task prompt", id="m-prompt")
            yield Input(placeholder="competitors (csv, optional)", id="m-comp")
            with Horizontal(id="modal-actions"):
                yield Button("Start", variant="primary", id="m-start")
                yield Button("Cancel", id="m-cancel")

    @on(Button.Pressed, "#m-start")
    def _start(self) -> None:
        prompt = self.query_one("#m-prompt", Input).value.strip()
        if not prompt:
            self.notify("prompt required", severity="warning")
            return
        comps = _csv_list(self.query_one("#m-comp", Input).value)
        self.dismiss((prompt, comps))

    @on(Button.Pressed, "#m-cancel")
    def _cancel_btn(self) -> None:
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class SessionsScreen(Screen):
    """Tabbed multi-session view (image 1). Each tab is a SessionPane over one
    registry session. Cap-bounded concurrency; Ctrl+N new, Ctrl+W close."""

    BINDINGS = [
        Binding("ctrl+c", "quit_all", "Quit"),
        Binding("ctrl+n", "new_session", "New"),
        Binding("ctrl+w", "close_session", "Close"),
        Binding("ctrl+p", "browse", "Sessions"),
    ]

    def __init__(
        self, *, first_prompt: str, first_competitors_csv: str
    ) -> None:
        super().__init__()
        self._registry = SessionRegistry()
        self._settings: Any = None
        self._pending_first = (first_prompt, _csv_list(first_competitors_csv))

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield TabbedContent(id="session-tabs")
        yield Footer()

    def on_mount(self) -> None:
        try:
            self._settings = Settings.load()
        except Exception as e:  # noqa: BLE001
            self.notify(f"settings error: {e}", severity="error")
            return
        self._boot()

    @work(thread=True)
    def _boot(self) -> None:
        self.app.call_from_thread(self.notify, "Starting droids-mem…", severity="information")
        ok, detail = _ensure_droids_mem()
        if not ok:
            self.app.call_from_thread(
                self.notify,
                f"droids-mem unavailable: {detail} — run `go install ./cmd/droids-mem`",
                severity="error",
            )
            return
        prompt, comps = self._pending_first
        self.app.call_from_thread(self._spawn, prompt, comps)

    def _spawn(self, prompt: str, competitors: list[str]) -> None:
        try:
            handle = self._registry.spawn(
                prompt=prompt,
                competitors=competitors,
                settings=self._settings,
            )
        except RegistryFull as e:
            self.notify(str(e), severity="warning")
            return
        tabs = self.query_one(TabbedContent)
        pane = SessionPane(handle.state)
        label = (prompt[:18] + "…") if len(prompt) > 18 else prompt
        tabs.add_pane(TabPane(f"{handle.key}: {label}", pane, id=handle.key))
        tabs.active = handle.key

    def action_new_session(self) -> None:
        def _after(result: Any) -> None:
            if result:
                prompt, comps = result
                self._spawn(prompt, comps)

        self.app.push_screen(NewSessionModal(), _after)

    def action_close_session(self) -> None:
        tabs = self.query_one(TabbedContent)
        key = tabs.active
        if key:
            self._registry.close(key)
            tabs.remove_pane(key)

    def action_browse(self) -> None:
        self.app.push_screen(SessionBrowserScreen())

    @work(thread=True)
    def action_quit_all(self) -> None:
        """Cancel every live agentspan execution, then exit the app."""
        for handle in self._registry.all():
            handle.state.request_stop()
        self.app.call_from_thread(self.app.exit)


# --- helpers --------------------------------------------------------------


_STATUS_STYLE = {
    sessions.STARTING: ("yellow", "🟡"),
    sessions.RUNNING: ("bold green", "🟢"),
    sessions.WAITING_HITL: ("bold yellow", "🟠"),
    sessions.DONE: ("bold green", "✅"),
    sessions.ERROR: ("bold red", "🔴"),
}


def _status_text(status: str, *, error: str | None) -> Text:
    style, _ = _STATUS_STYLE.get(status, ("white", ""))
    return Text(status, style=style)


def _stats_text(snap: sessions.SessionSnapshot) -> Text:
    """Render the Statistics panel (image 2)."""
    style, dot = _STATUS_STYLE.get(snap.status, ("white", ""))
    t = Text()
    t.append("📊 Statistics\n\n", style="bold blue")
    t.append(f"Messages:    {snap.messages}\n")
    t.append(f"Tool calls:  {snap.tool_calls}\n")
    t.append(f"Agents:      {snap.agents_seen}\n")
    t.append(f"Turns:       {snap.turns}\n")
    t.append(f"Tasks:       {snap.tasks_done}/{snap.tasks_total}\n\n")
    t.append("Status:      ")
    t.append(f"{dot} {snap.status}", style=style)
    if snap.task_groups:
        t.append("\n\nProgress by agent:\n", style="bold")
        for name, g in sorted(snap.task_groups.items()):
            t.append(f"  {name}: {g['done']}/{g['total']}\n", style="dim")
    if snap.error:
        t.append(f"\n[error] {snap.error}", style="red")
    if snap.exec_id:
        t.append(f"\n\nexec: {snap.exec_id}", style="dim")
        if snap.agentspan_url:
            ui_url = f"{snap.agentspan_url.rstrip('/')}/executions/{snap.exec_id}"
            t.append(f"\n{ui_url}", style="dim underline cyan")
    return t


def _fmt_duration(seconds: float) -> str:
    if seconds < 1:
        return "<1s"
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        m, s = divmod(int(seconds), 60)
        return f"{m}m {s}s"
    h, rem = divmod(int(seconds), 3600)
    m, _ = divmod(rem, 60)
    return f"{h}h {m}m"


def _eta_text(snap: sessions.SessionSnapshot) -> str:
    elapsed = (
        _fmt_duration(snap.elapsed_seconds) if snap.elapsed_seconds is not None else "—"
    )
    eta = _fmt_duration(snap.eta_seconds) if snap.eta_seconds is not None else "—"
    return f"Elapsed: {elapsed}   ETA: {eta}"


# --- App ------------------------------------------------------------------


class DroidsAgentsApp(App):
    """Top-level Textual App."""

    # Free Ctrl+P for the sessions browser (Textual binds it to its command
    # palette by default).
    ENABLE_COMMAND_PALETTE = False

    CSS = """
    #prompt-container {
        padding: 2;
        align: center middle;
        width: 100%;
    }
    #logo { width: auto; height: auto; color: $accent; padding-bottom: 1; }
    #banner { padding-bottom: 1; }
    #prompt-actions { padding-top: 1; }

    #session-tabs { height: 1fr; }
    .pane-row { height: 1fr; }
    .pane-feed { width: 2fr; height: 1fr; border: round $primary; }
    .pane-log { padding: 1; }
    .pane-stats { width: 1fr; height: 1fr; padding: 1; border: round $accent; margin-left: 1; }
    .pane-stats-text { height: auto; }
    .pane-progress { margin-top: 1; width: 100%; }
    .pane-eta { color: $text-muted; margin-top: 1; height: auto; }
    .pane-input { margin-top: 1; }
    #modal-box { padding: 2; width: 60%; height: auto; border: round $accent; background: $surface; }
    #modal-actions { padding-top: 1; height: auto; }

    #browser-container, #detail-container, #search-container { padding: 1; }
    #browser-title, #detail-title, #search-title { padding-bottom: 1; color: $accent; }
    #sessions-table, #detail-table, #results-table { height: 1fr; }
    #browser-hint, #detail-hint, #search-hint { padding-top: 1; color: $text-muted; }
    #search-input { margin-bottom: 1; }
    """

    def on_mount(self) -> None:
        self.push_screen(PromptScreen())


def run_tui() -> None:
    """Entrypoint invoked by the ``droids-agents tui`` subcommand."""
    # mouse=False so the terminal handles drag-to-select for copy.
    DroidsAgentsApp().run(mouse=True)
