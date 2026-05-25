"""Rich-powered display layer for CLI banners and HITL prompts.

Centralizes the color palette so the agentspan UI fallback (which shows
``metadata.droid_name`` and ``metadata.role_label`` as plain text) and the
local CLI banner stay visually consistent.

Color contract:
- Droid name → deterministic from a hash → cycles through ``_DROID_PALETTE``.
  Same name across runs → same color, so eye-tracking across logs is stable.
- Role → fixed per role label, picked to be distinguishable from the droid
  palette (``_ROLE_PALETTE``).
"""

from __future__ import annotations

import hashlib
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# Stable per-name palette. Avoids reds (reserved for HITL warnings) and very
# faint colors so output stays readable on light + dark terminals.
_DROID_PALETTE: tuple[str, ...] = (
    "bright_cyan",
    "bright_magenta",
    "bright_green",
    "bright_yellow",
    "bright_blue",
    "cyan",
    "magenta",
    "green",
    "yellow",
    "blue",
    "purple",
)

# Role colors (fixed per role). Sharply distinct from the droid palette.
_ROLE_PALETTE: dict[str, str] = {
    "Researcher": "bold dodger_blue1",
    "Doc-Extractor": "bold spring_green3",
    "Doc-Synth": "bold dark_sea_green4",
    "Form-Planner": "bold orange3",
    "Form-Executor": "bold dark_orange",
    "Email-Drafter": "bold medium_orchid",
    "Email-Sender": "bold deep_pink3",
    "Router": "bold grey70",
    "Memory-Loader": "bold steel_blue1",
    "Rollup": "bold gold3",
}

_HITL_STYLE = "bold red on grey15"
_EXEC_HEADER_STYLE = "bold white on grey23"


def color_for_droid(name: str) -> str:
    """Stable color for a droid name. Same name → same color across runs."""
    h = hashlib.sha256(name.encode("utf-8")).digest()
    return _DROID_PALETTE[h[0] % len(_DROID_PALETTE)]


def color_for_role(role_label: str) -> str:
    return _ROLE_PALETTE.get(role_label, "bold white")


def render_agent_display(droid_name: str, role_label: str) -> Text:
    """``C-3PO`` in droid color + ``: [Researcher]`` in role color."""
    t = Text()
    t.append(droid_name, style=color_for_droid(droid_name))
    t.append(": [", style="dim white")
    t.append(role_label, style=color_for_role(role_label))
    t.append("]", style="dim white")
    return t


def make_console(*, json_mode: bool) -> Console:
    """JSON mode → no color, no markup, stderr-only. Human mode → full Rich."""
    return Console(stderr=True, no_color=json_mode, highlight=not json_mode)


# --- Banners --------------------------------------------------------------


def print_execution_header(
    console: Console,
    *,
    exec_id: str,
    task_type_override: str | None,
) -> None:
    """t=0 banner: exec_id known but sess_id not yet."""
    t = Text()
    t.append(" Execution ", style=_EXEC_HEADER_STYLE)
    t.append(f" {exec_id} ", style="bold bright_white")
    t.append("started", style="dim white")
    if task_type_override:
        t.append(f"  task_type_override={task_type_override}", style="cyan")
    console.print(t)


def print_session_header(
    console: Console, *, session_id: str, task_type: str
) -> None:
    """Second banner once memory_loader emits sess_id."""
    t = Text()
    t.append(" session ", style=_EXEC_HEADER_STYLE)
    t.append(f" {session_id} ", style="bold bright_white")
    t.append(f"  task_type={task_type}", style="cyan")
    console.print(t)


def _truncate(value: Any, cap: int = 200) -> str:
    s = str(value)
    return s if len(s) <= cap else s[: cap - 1] + "…"


def print_hitl_pause(
    console: Console,
    *,
    droid_name: str,
    role_label: str,
    tool_name: str,
    tool_args: dict[str, Any],
    session_id: str,
    exec_id: str,
    ui_base_url: str,
    reason: str | None = None,
) -> None:
    """HITL pause banner. Shown on every ``is_waiting`` event."""
    table = Table.grid(padding=(0, 1))
    table.add_row(
        Text("agent", style="dim white"),
        render_agent_display(droid_name, role_label),
    )
    table.add_row(Text("tool", style="dim white"), Text(tool_name, style="bold yellow"))
    if reason:
        table.add_row(Text("reason", style="dim white"), Text(reason, style="red"))
    for k, v in tool_args.items():
        table.add_row(Text(f"  arg.{k}", style="dim white"), Text(_truncate(v)))
    table.add_row(
        Text("session_id", style="dim white"), Text(session_id, style="bright_black")
    )
    table.add_row(
        Text("exec_id", style="dim white"), Text(exec_id, style="bright_black")
    )
    table.add_row(
        Text("approve", style="dim white"),
        Text(f"{ui_base_url.rstrip('/')}/executions/{exec_id}", style="underline cyan"),
    )
    console.print(
        Panel(
            table,
            title=Text("HITL PAUSE", style=_HITL_STYLE),
            border_style="red",
            expand=False,
        )
    )


def print_doctor_results(console: Console, results: list[dict[str, Any]]) -> None:
    """Pretty-print the ``doctor`` subcommand checks."""
    table = Table(title="droids-agents doctor", show_lines=False)
    table.add_column("check", style="dim white")
    table.add_column("status")
    table.add_column("detail")
    for r in results:
        ok = r.get("ok", False)
        status = Text("PASS" if ok else "FAIL", style="bold green" if ok else "bold red")
        table.add_row(r.get("name", ""), status, _truncate(r.get("detail", ""), 160))
    console.print(table)
