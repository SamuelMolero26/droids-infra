# Repository Guidelines

## Project Structure & Module Organization
`droids-agents` is a Python 3.12+ package for local-first multi-agent BI workflows. Core code lives in `src/`, packaged as `droids_agents` via `pyproject.toml`. Key modules are `cli.py` for commands, `router.py` for prompt classification, `execution.py` and `runtime.py` for run orchestration, `sessions.py`/`tui.py` for the Textual dashboard, and `schemas.py` for Pydantic contracts. Subteam factories live in `src/agents/`; guardrails mirror those domains in `src/guardrails/`; tool wrappers are in `src/tools/`; bundled text assets are in `src/assets/`. Tests live in `tests/unit/`. Product and architecture references live in `CONTEXT.md`, `CLAUDE.md`, `V1-droids-agents-plan.md`, and `docs/`.

## Build, Test, and Development Commands
- `uv sync --extra dev` — install runtime and dev dependencies.
- `uv run droids-agents doctor` — run pre-flight dependency checks.
- `uv run droids-agents run "<prompt>"` — execute one Execution locally.
- `uv run droids-agents tui` — open the Textual session dashboard.
- `uv run pytest` — run the default unit test suite.
- `uv run pytest -m integration` — run slower integration tests that spawn real services.
- `uv run ruff check src tests` / `uv run ruff format src tests` — lint and format Python files.

## Coding Style & Naming Conventions
Use Ruff defaults configured in `pyproject.toml`: Python 3.12 target, 100-character line length, lint families `E`, `F`, `I`, `B`, `UP`, and `ASYNC` with `E501` ignored. Prefer explicit Pydantic models for external payloads. Preserve domain terms from `CONTEXT.md`: Execution, Root agent, Subteam, Sub-agent, Bundle, Slice, Memory broker, and Rollup. Keep `agentspan.Agent.name` stable for cache keys; put display Droid names in metadata.

## Testing Guidelines
Write unit tests under `tests/unit/test_*.py`, matching the module or behavior under test (for example, `test_slicing.py`). Use `pytest` and `pytest-asyncio`; async mode is automatic. Mark service-spawning tests with `@pytest.mark.integration` so they stay out of default runs.

## Commit & Pull Request Guidelines
Git history uses Conventional Commit-style subjects such as `feat: initial versioning + mem updates for droids-mem`; follow `type: short imperative summary` (`feat`, `fix`, `docs`, `test`, `refactor`). PRs should include a concise description, linked issue or rationale, test results (`uv run pytest`, Ruff output), and screenshots or terminal output for TUI/CLI behavior changes.

## Security & Configuration Tips
Do not commit secrets or generated local state. Use `.env.example` as the template. Runtime config loads from `~/.droids-agents/.env` and then `./.env` with the local file winning. Required credentials include Anthropic, droids-mem MCP, agentspan, and Google OAuth paths when Gmail tools are used.
