"""droids-agents CLI.

Subcommands:
- ``run <prompt>``        — execute a task (default; bare ``droids-agents <prompt>`` aliases here).
- ``auth gmail``          — one-time OAuth desktop flow.
- ``doctor``              — pre-flight checks; exit 0 / 1.

Exit codes:
    0   success
    1   runtime error
    2   usage error
    3   dependency unreachable / not found
    4   HITL pause (execution paused awaiting human approval)
    5   cost cap hit
    10  dry-run completed
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any

import click
import httpx
import structlog

from droids_agents import logging as dlog
from droids_agents.config import Settings, SettingsError
from droids_agents.display import (
    color_for_droid,
    color_for_role,
    make_console,
    print_doctor_results,
    print_execution_header,
    print_hitl_pause,
    print_session_header,
    render_agent_display,
)
from droids_agents.naming import NamePool
from droids_agents.pricing import usd_to_max_total_tokens
from droids_agents.router import build_root, classify_prompt, plan_mixed_steps
from droids_agents.runtime import connect_runtime
from droids_agents.schemas import (
    ClassifierLabel,
    label_to_task_type,
)
from droids_agents.slicing import slice_for
from droids_agents.tools.gmail import GMAIL_SCOPES
from droids_agents.tools.mem import MemFetchError, fetch_mem_context

_DOCS_TOTAL_CAP_BYTES: int = 5 * 1024 * 1024
_ALLOWED_DOC_EXTS: frozenset[str] = frozenset({".pdf", ".md", ".txt"})

_log = structlog.get_logger("cli")


# --- helpers --------------------------------------------------------------


def _emit_err(console, code: str, message: str, **extra: Any) -> None:
    """Stderr-only JSON error envelope (for --json) or rich text."""
    payload = {"status": "error", "code": code, "message": message, **extra}
    if console.no_color:  # json mode
        console.print(json.dumps(payload))
    else:
        console.print(f"[bold red]error[/]: {message}")
        if extra:
            console.print(f"[dim]{extra}[/]")


def _load_settings_or_exit(console) -> Settings:
    try:
        return Settings.load()
    except SettingsError as e:
        _emit_err(console, "config_missing", str(e))
        sys.exit(2)


def _validate_docs(raw_paths: tuple[str, ...]) -> list[tuple[Path, str]]:
    """Eager --docs validation: existence, ext, basename uniqueness, total size cap."""
    seen_basenames: set[str] = set()
    out: list[tuple[Path, str]] = []
    total = 0
    for raw in raw_paths:
        p = Path(raw).expanduser().resolve()
        if not p.exists() or not p.is_file():
            raise click.UsageError(f"--docs path not found or not a file: {p}")
        ext = p.suffix.lower()
        if ext not in _ALLOWED_DOC_EXTS:
            raise click.UsageError(
                f"--docs {p}: extension {ext!r} not in {sorted(_ALLOWED_DOC_EXTS)}"
            )
        basename = p.name
        if basename in seen_basenames:
            raise click.UsageError(
                f"--docs basename {basename!r} is not unique across the list"
            )
        seen_basenames.add(basename)
        total += p.stat().st_size
        if total > _DOCS_TOTAL_CAP_BYTES:
            raise click.UsageError(
                f"--docs total size > {_DOCS_TOTAL_CAP_BYTES} bytes "
                f"({total} after {p.name})"
            )
        out.append((p, basename))
    return out


def _resolve_steps(
    *,
    settings: Settings,
    prompt: str,
    task_type_override: str | None,
) -> list[ClassifierLabel]:
    """Run classifier (and mixed_planner if needed). Honors --task-type."""
    if task_type_override:
        # Reverse map: task_type → label. Used to short-circuit the classifier
        # so a known task_type bypasses the LLM.
        for label, tt in _LABEL_TO_TT.items():
            if tt == task_type_override:
                return [label]
        raise click.UsageError(
            f"--task-type {task_type_override!r} is not a valid TaskType"
        )

    label = classify_prompt(prompt, settings=settings)
    if label != "mixed":
        return [label]
    return plan_mixed_steps(prompt, settings=settings)


# Lazy reverse map to avoid a circular import at top level.
from droids_agents.schemas import LABEL_TO_TASK_TYPE as _LABEL_TO_TT  # noqa: E402


def _build_slice_map(*, bundle, prompt: str, steps: list[ClassifierLabel]) -> dict[str, list[str]]:
    """Map role → sliced lines for every Sub-agent role that this Execution uses."""
    roles_for_label = {
        "research": ("competitor",),
        "docs": ("extractor", "synthesizer"),
        "form": ("form_planner", "form_executor"),
        "messaging": ("drafter", "sender"),
    }
    needed: set[str] = set()
    for s in steps:
        for r in roles_for_label.get(s, ()):
            needed.add(r)
    return {role: slice_for(role, bundle, prompt) for role in needed}


def _result_get(result: Any, *names: str, default: Any = None) -> Any:
    """Tolerate either object-attr or dict-key shapes from agentspan results."""
    for n in names:
        if hasattr(result, n):
            return getattr(result, n)
        if isinstance(result, dict) and n in result:
            return result[n]
    return default


# --- main group ----------------------------------------------------------


@click.group(invoke_without_command=True, context_settings={"help_option_names": ["-h", "--help"]})
@click.pass_context
def main(ctx: click.Context) -> None:
    """droids-agents — local-first multi-agent BI runtime."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())
        sys.exit(2)


# --- tui subcommand (Typer-defined, click-mounted) -----------------------
#
# New subcommands are written in Typer per project convention; the click
# `main` group remains the single CLI entrypoint. We use ``typer.main.get_command``
# to convert the Typer app into a click sub-command and attach it below.

import typer  # noqa: E402

_tui_app = typer.Typer(add_completion=False, no_args_is_help=False)


@_tui_app.command(help="Launch the interactive Textual dashboard.")
def _tui_entry() -> None:
    from droids_agents.tui import run_tui

    run_tui()


main.add_command(typer.main.get_command(_tui_app), name="tui")


# --- run subcommand ------------------------------------------------------


@main.command("run")
@click.argument("prompt", nargs=-1, required=True)
@click.option("--docs", multiple=True, help="Local doc path. Repeatable. .pdf/.md/.txt. ≤5MB total.")
@click.option("--competitors", default="", help="Comma-separated competitor names (research subteam).")
@click.option("--task-type", default=None, help="Skip classifier; force a TaskType.")
@click.option("--session-id", default=None, help="Reuse an existing droids-mem session_id.")
@click.option("--max-cost-usd", type=float, default=None, help="USD budget; converts to max_total_tokens.")
@click.option("--dry-run", is_flag=True, help="Run pipeline end-to-end but skip side effects (gmail_send, web_submit, mem_save).")
@click.option("--json", "json_mode", is_flag=True, help="JSON stream to stdout. No colors. No prompts.")
def run(
    prompt: tuple[str, ...],
    docs: tuple[str, ...],
    competitors: str,
    task_type: str | None,
    session_id: str | None,
    max_cost_usd: float | None,
    dry_run: bool,
    json_mode: bool,
) -> None:
    """Execute a task. Bare ``droids-agents <prompt>`` aliases here."""
    prompt_text = " ".join(prompt).strip()
    if not prompt_text:
        raise click.UsageError("prompt is required")

    console = make_console(json_mode=json_mode)
    settings = _load_settings_or_exit(console)
    dlog.configure(settings.log_dir, dev_stderr=not json_mode)

    docs_validated = _validate_docs(docs)
    docs_basenames = [b for _, b in docs_validated]
    competitor_list = [c.strip() for c in competitors.split(",") if c.strip()]

    # Pre-classify before agentspan compile.
    steps = _resolve_steps(
        settings=settings, prompt=prompt_text, task_type_override=task_type
    )
    task_type_final = label_to_task_type(steps[0])

    if "messaging" in steps and not settings.gmail_enabled:
        _emit_err(
            console,
            "gmail_not_configured",
            "this prompt needs the messaging Subteam but Gmail is not configured. "
            "Set GOOGLE_CREDENTIALS_JSON + GOOGLE_TOKEN_JSON and run "
            "`droids-agents auth gmail`, or rephrase to avoid email tasks.",
        )
        sys.exit(2)

    # Direct mem_context fetch (V1 deviation — see tools/mem.py for rationale).
    try:
        mem_result = fetch_mem_context(
            settings, task_type=task_type_final, query=prompt_text
        )
    except MemFetchError as e:
        _emit_err(console, "mem_unreachable", str(e))
        sys.exit(3)

    sess_id = session_id or mem_result.session_id
    dlog.bind_session(sess_id)
    _log.info(
        "session_resolved",
        session_id=sess_id,
        task_type=task_type_final,
        steps=steps,
    )

    slice_map = _build_slice_map(
        bundle=mem_result.bundle, prompt=prompt_text, steps=steps
    )

    pool = NamePool(names=None)  # OS-entropy seeded → random per Execution
    max_total_tokens = (
        usd_to_max_total_tokens(max_cost_usd) if max_cost_usd is not None else None
    )

    root = build_root(
        settings,
        pool=pool,
        prompt=prompt_text,
        steps=steps,
        session_id=sess_id,
        competitors=competitor_list,
        docs_basenames=docs_basenames,
        slice_map=slice_map,
        max_total_tokens=max_total_tokens,
    )

    runtime = connect_runtime(settings)

    try:
        result = runtime.run(  # type: ignore[attr-defined]
            root,
            prompt_text,
            context={
                "task_type_override": task_type,
                "session_id_override": session_id,
                "dry_run": dry_run,
            },
        )
    except httpx.HTTPError as e:
        _emit_err(
            console,
            "agentspan_unreachable",
            f"agentspan server not reachable at {settings.agentspan_url}: {e}. "
            "Start it with `agentspan server start`.",
        )
        sys.exit(3)
    except Exception as e:  # noqa: BLE001 — surface any agentspan runtime error
        _emit_err(console, "runtime_error", str(e))
        sys.exit(1)

    exec_id = _result_get(result, "execution_id", "exec_id", default="<unknown>")
    print_execution_header(console, exec_id=exec_id, task_type_override=task_type)
    print_session_header(console, session_id=sess_id, task_type=task_type_final)

    # HITL pause surfacing.
    if _result_get(result, "is_waiting", default=False):
        pending = _result_get(result, "pending_approval", "waiting_for", default={})
        if not isinstance(pending, dict):
            pending = {}
        meta = pending.get("metadata") or {}
        print_hitl_pause(
            console,
            droid_name=meta.get("droid_name", "?"),
            role_label=meta.get("role_label", meta.get("role", "?")),
            tool_name=pending.get("tool_name", "?"),
            tool_args=pending.get("tool_args", {}) or {},
            session_id=sess_id,
            exec_id=exec_id,
            ui_base_url=settings.agentspan_url,
            reason=pending.get("reason"),
        )
        sys.exit(4)

    if dry_run:
        payload = _result_get(result, "output", default={})
        # Pydantic / dict-safe dump
        if hasattr(payload, "model_dump"):
            payload = payload.model_dump()
        console.print(json.dumps({"status": "dry_run_pass", "output": payload}, default=str))
        sys.exit(10)

    # Success path. Cost cap detection: if termination fired on token usage,
    # agentspan typically marks the result. Heuristic — surface in log.
    termination_reason = _result_get(result, "termination_reason", default="")
    if "token" in str(termination_reason).lower():
        _emit_err(
            console,
            "cost_cap_hit",
            f"TokenUsageTermination fired: {termination_reason}",
        )
        sys.exit(5)

    output = _result_get(result, "output", default={})
    if hasattr(output, "model_dump"):
        output = output.model_dump()
    if json_mode:
        console.print(json.dumps({"status": "ok", "output": output}, default=str))
    else:
        console.print("[bold green]done[/]")
        console.print(output)
    sys.exit(0)


# --- auth gmail ----------------------------------------------------------


@main.group("auth")
def auth() -> None:
    """One-time auth flows (Gmail OAuth, etc.)."""


@auth.command("gmail")
def auth_gmail() -> None:
    """Run the Gmail OAuth desktop flow. Idempotent — re-running rotates the token."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    console = make_console(json_mode=False)
    settings = _load_settings_or_exit(console)

    creds_path = settings.google_credentials_json
    token_path = settings.google_token_json
    if creds_path is None or token_path is None:
        _emit_err(
            console,
            "gmail_paths_missing",
            "set GOOGLE_CREDENTIALS_JSON and GOOGLE_TOKEN_JSON in your .env first",
        )
        sys.exit(2)
    if not creds_path.exists():
        _emit_err(
            console,
            "credentials_missing",
            f"GOOGLE_CREDENTIALS_JSON not found at {creds_path}",
        )
        sys.exit(3)

    flow = InstalledAppFlow.from_client_secrets_file(
        str(creds_path), scopes=list(GMAIL_SCOPES)
    )
    creds = flow.run_local_server(port=0, open_browser=True)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    console.print(
        f"[bold green]gmail token written[/] to [underline]{token_path}[/]"
    )


# --- doctor --------------------------------------------------------------


@main.command("doctor")
@click.option("--json", "json_mode", is_flag=True, help="Emit JSON to stdout instead of a Rich table.")
def doctor(json_mode: bool) -> None:
    """Pre-flight checks. Exit 0 / 1."""
    console = make_console(json_mode=json_mode)
    results: list[dict[str, Any]] = []

    # 1. Settings load + required env.
    try:
        settings = Settings.load()
        results.append({"name": "settings.load", "ok": True, "detail": "all required env vars set"})
    except SettingsError as e:
        results.append({"name": "settings.load", "ok": False, "detail": str(e)})
        settings = None  # type: ignore[assignment]

    # 2. droids-mem-mcp /healthz reachable.
    if settings is not None:
        healthz = settings.droids_mem_mcp_url.rsplit("/mcp", 1)[0].rstrip("/") + "/healthz"
        try:
            r = httpx.get(healthz, timeout=3.0)
            ok = r.status_code == 200
            results.append({"name": "droids-mem /healthz", "ok": ok, "detail": f"HTTP {r.status_code} at {healthz}"})
        except httpx.HTTPError as e:
            results.append({"name": "droids-mem /healthz", "ok": False, "detail": f"{e} (start with `./droids-mem-mcp`)"})

    # 3. agentspan reachable.
    if settings is not None:
        try:
            r = httpx.get(settings.agentspan_url, timeout=3.0)
            ok = r.status_code in (200, 404)
            results.append({"name": "agentspan", "ok": ok, "detail": f"HTTP {r.status_code} at {settings.agentspan_url}"})
        except httpx.HTTPError as e:
            results.append({"name": "agentspan", "ok": False, "detail": f"{e} (start with `agentspan server start`)"})

    # 4. Gmail token loadable. Gmail is OPTIONAL — report SKIP when not configured.
    if settings is not None:
        if not settings.gmail_enabled:
            results.append(
                {
                    "name": "gmail token",
                    "ok": True,
                    "detail": "skipped — Gmail not configured (messaging Subteam disabled)",
                }
            )
        else:
            from droids_agents.tools.gmail import GmailAuthError, _load_token

            try:
                creds = _load_token(settings.google_token_json)
                detail = "loaded; valid" if creds.valid else "loaded; refresh required (token expired or near expiry)"
                results.append({"name": "gmail token", "ok": True, "detail": detail})
            except GmailAuthError as e:
                results.append({"name": "gmail token", "ok": False, "detail": str(e)})

    # 5. Playwright Chromium installed.
    pw_bin = shutil.which("chromium") or shutil.which("chromium-browser")
    pw_cache = Path.home() / "Library" / "Caches" / "ms-playwright"
    has_pw = any(pw_cache.glob("chromium*")) if pw_cache.exists() else False
    results.append(
        {
            "name": "playwright chromium",
            "ok": has_pw or pw_bin is not None,
            "detail": (
                f"found at {pw_bin}" if pw_bin else
                f"found in {pw_cache}" if has_pw else
                "not installed; run `uv run playwright install chromium`"
            ),
        }
    )

    if json_mode:
        sys.stdout.write(json.dumps({"checks": results}, default=str) + "\n")
    else:
        print_doctor_results(console, results)

    sys.exit(0 if all(r["ok"] for r in results) else 1)


# Keep these display helpers used so linters don't strip the import.
_ = (color_for_droid, color_for_role, render_agent_display)


if __name__ == "__main__":
    main()
