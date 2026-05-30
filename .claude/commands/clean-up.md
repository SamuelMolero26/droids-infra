---
name: cleanup-vibes
description: Transform a vibecoded project into a properly structured, deployment-ready codebase with secrets extracted and organized folders. Triage-gated — refuses to mutate clean projects without explicit override.
---

<objective>
Transform a vibecoded project into a clean, deployment-ready codebase. Vibecoded projects typically have hardcoded API keys, flat/disorganized folder structures, no .env files, and no documentation.

Detects project type (TypeScript, Python, Go, hybrid), scores how "vibecoded" the repo is, then applies only the phases the score warrants. Already-clean projects exit at Phase 0 with a report — no mutations.
</objective>

<context>
Project files: !`find . -maxdepth 1 -not -name '.' -not -name '.git' -not -name 'node_modules' -not -name '__pycache__' -not -name '.venv' -not -name 'venv' | head -40`
Package files: !`ls package.json pyproject.toml requirements.txt setup.py Pipfile Cargo.toml go.mod 2>/dev/null; true`
Current structure: !`find . -type f -not -path '*/node_modules/*' -not -path '*/.git/*' -not -path '*/__pycache__/*' -not -path '*/.venv/*' -not -path '*/venv/*' -not -path '*/.next/*' -not -path '*/dist/*' -not -path '*/.DS_Store' | head -80`
Existing env files: !`ls -la .env* 2>/dev/null || echo "No .env files found"`
Existing gitignore: !`cat .gitignore 2>/dev/null || echo "No .gitignore found"`
Existing docs: !`ls README.md CLAUDE.md docs/adr/ 2>/dev/null || echo "No docs"`
Git state: !`git status --short 2>/dev/null | head -20; echo "---"; git rev-parse --abbrev-ref HEAD 2>/dev/null`
CodeGraph state: !`ls -la .codegraph/codegraph.db 2>/dev/null && echo "CODEGRAPH_READY" || echo "CODEGRAPH_MISSING"`
</context>

<process>

## Phase -1: CodeGraph Bootstrap (MANDATORY — run before Phase 0)

CodeGraph is a SQLite knowledge graph of every symbol, edge, and file in the workspace. Sub-millisecond lookups via the code graph beat grep+read scans by a wide margin for cross-file moves, import-path rewrites, and impact analysis — exactly what Phase 2 (secret scan), Phase 4 (restructure), and Phase 4b (Go layout) need.

**Decision:**

| State (from `CodeGraph state` context line) | Action |
|---------------------------------------------|--------|
| `CODEGRAPH_READY` | Use codegraph tools for ALL exploration in subsequent phases. No grep+read loops for symbol lookup. |
| `CODEGRAPH_MISSING` | Run `codegraph init -i` (foreground, ~10–60s depending on repo size). Wait for completion. Then proceed. |

**Init failure handling:** if `codegraph init -i` errors (binary missing, unsupported language, indexer fails), print the error, note "proceeding without codegraph — using grep+read for exploration", continue to Phase 0. Do not block the cleanup.

**Tool routing (use throughout all phases):**

| Intent | Tool |
|--------|------|
| Find symbol by name (function, class, type) | `codegraph_search` — NOT grep |
| Get context for a task / area | `codegraph_context` (PRIMARY — composes search + node + callers + callees in one call) |
| What calls this symbol? | `codegraph_callers` |
| What does this symbol call? | `codegraph_callees` |
| What breaks if I rename/move this? | `codegraph_impact` — MANDATORY before any Phase 4 file move that touches exported symbols |
| Show symbol source / signature | `codegraph_node` |
| Survey multiple related symbols | `codegraph_explore` (ONE capped call; prefer over many `codegraph_node`/Read) |
| What's in directory X? | `codegraph_files` |
| Is index ready? | `codegraph_status` |

**Hard rule for Phase 4 (restructure):** before moving any file containing exported symbols, run `codegraph_impact` on each symbol to get the full caller list. Update those callers in the same atomic commit as the move. Skipping this = broken imports the build catches late.

**Sub-agent rule:** when spawning Explore/Task agents (Phase 2 large-repo path), instruct them to use codegraph tools for symbol lookup. Pass `CODEGRAPH_READY` status in the prompt.

## Phase 0: Triage Gate (MANDATORY — run first, decide what follows)

Score the repo on the 8 signals below. Each signal is a single observable check.

| # | Signal | Clean marker (+0 pts) | Vibecoded marker (+1 pt) |
|---|--------|----------------------|--------------------------|
| 1 | Secret hygiene | `grep -rEn "(?i)(api[_-]?key\|secret\|token\|password)\s*[:=]\s*['\"][A-Za-z0-9_/+=-]{16,}['\"]" --include="*.{ts,tsx,js,jsx,py,go}"` (excl. tests) returns 0 lines | ≥1 hit |
| 2 | `.env` exists with real values | `.env` present OR no secrets to externalize | Hardcoded secrets present AND no `.env` |
| 3 | `.gitignore` covers `.env` + build artifacts | Present in repo OR parent dir, includes `.env`, lang build dirs, IDE | Missing or incomplete |
| 4 | Layered structure | Lang-appropriate: TS has `src/`; Python has `src/`/`app/`; Go has `cmd/`+`internal/` OR is a deliberately flat <500 LOC tool | All source in repo root, mixed concerns |
| 5 | Config centralized | Typed config struct/module reads env in one place | Env access scattered across files |
| 6 | Build/test green | `go build ./...` / `npm run build` / `python -c "import <pkg>"` exits 0 | Errors |
| 7 | Docs present | `README.md` OR `CLAUDE.md` describes setup + run | None or stub |
| 8 | Architecture notes | `docs/adr/`, `CONTEXT.md`, or equivalent decision log | None |

**Decision table:**

| Score | Action |
|-------|--------|
| 0–2 | **REPORT-ONLY.** Print the table with per-signal status. List the 0–2 signals that flagged with file:line evidence. Ask user explicitly: "Run cleanup anyway? (y/N)". Do NOT mutate, do NOT spawn sub-agents, do NOT run Phases 1–6. |
| 3–5 | **TARGETED.** Run only phases that address flagged signals. Skip phases tied to clean signals. Always run Phase 5 verification. |
| 6–8 | **FULL RUN.** Execute Phases 1–6 as written. |

**Hard preconditions (abort regardless of score):**
- `git status --short` shows uncommitted changes → print diff stat, refuse to mutate unless user passes `--force-dirty`. Folder restructure on a dirty tree destroys WIP.
- Detached HEAD or rebase in progress → abort.
- No `git` repo → warn, ask for confirmation before any mutation (nothing to revert to).

**Output of Phase 0 (always emit, even on full run):**
```
Triage: <score>/8 — <REPORT-ONLY | TARGETED | FULL RUN>
Flagged: <list signals>
Skipping: <list phases>
Will run: <list phases>
```

## Phase 1: Project Detection

1. Determine project type:
   - **TypeScript/JavaScript**: `package.json`, `.ts`/`.tsx`/`.js`/`.jsx` files
   - **Python**: `requirements.txt`, `pyproject.toml`, `setup.py`, `.py` files
   - **Go**: `go.mod`, `.go` files
   - **Hybrid**: Multiple present
2. Identify framework(s): Next.js, React, Express, FastAPI, Flask, Django, cobra/viper, gin/echo/chi, etc.
3. Identify entry points (`package main` + `func main()` under `cmd/` for Go).
4. Count source LOC. Tools <500 LOC stay flat — note this for Phase 4.

## Phase 2: Secret Extraction

**Gating:** Skip entirely if Phase 0 signal #1 (secret hygiene) is clean.

For repos with >50 source files OR Phase 0 score ≥5, deploy 3 parallel sub-agents via Task. Otherwise run the three scans inline (cheaper, fewer false positives the main agent can adjudicate immediately).

**Scan rules — sharpened to cut false positives:**

- **Always exclude**: `_test.go`, `*.test.ts`, `*.spec.ts`, `tests/`, `__tests__/`, `node_modules/`, `vendor/`, `.git/`, generated files (`*.pb.go`, `*_generated.*`).
- **Skip lines matching**: `os.Getenv(`, `process.env.`, `os.environ.get(`, `Default*` constants, struct field tags, doc comments (`//`, `#`, `/**`), markdown code fences.
- **Treat as NOT a secret**: env var *names* in config loaders, default values for env-overridable bind addresses (e.g. `:7777`), `localhost`/`127.0.0.1` in dev-server defaults inside a `Default*` named var or `os.Getenv` fallback.

**Agent 1 — Credential Scanner** (regex must be anchored to assignment):
- `(?i)(api[_-]?key|secret|access[_-]?token|password|client[_-]?secret)\s*[:=]\s*['"][A-Za-z0-9_/+=-]{16,}['"]`
- `sk-[a-zA-Z0-9]{20,}`, `pk_(live|test)_[a-zA-Z0-9]{20,}`, `xox[baprs]-[a-zA-Z0-9-]{10,}`
- `AKIA[0-9A-Z]{16}`, `AIza[0-9A-Za-z_-]{35}`
- `Bearer\s+[A-Za-z0-9._-]{20,}` (excluding doc comments)
- Postgres/Mongo/MySQL/Redis URLs with embedded password: `://[^:]+:[^@]+@`
- JWT shape: `eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}`

**Agent 2 — URL/Endpoint Scanner**:
- Production-shaped URLs hardcoded in source (`https://[a-z0-9.-]+\.(com|io|dev|net)`) NOT inside a `Default*` var, comment, or env loader.
- Webhook URLs.
- DB connection strings outside env loaders.

**Agent 3 — Config Scanner**:
- Hardcoded ports outside `Default*` constants or env loaders.
- Magic strings `"prod"`, `"production"`, `"staging"` compared against without an env-driven `Environment` field.
- Feature flags hardcoded `true`/`false`.

Compile findings into a unified inventory with `file:line` for every hit. Each finding must include the offending literal (redacted last 4 chars if it looks like a real secret) so the user can verify before extraction.

## Phase 3: Create .env Files

**Gating:** Skip if Phase 2 found 0 real secrets to externalize.

1. Create `.env` with extracted secrets organized by category (Application / Database / Authentication / Third-party).
2. Create `.env.example` with same keys, placeholder values (`your_<key>_here`).
3. Replace hardcoded values with env references:
   - TypeScript/JS: `process.env.VARIABLE_NAME`
   - Python: `os.environ.get("VARIABLE_NAME")` or `python-dotenv`
   - Go: `os.Getenv("VARIABLE_NAME")` behind a typed config struct (see `samber/cc-skills-golang@golang-spf13-viper`)
4. **Preserve existing env defaults.** If code already has `os.Getenv("X")` with a fallback, do not rewrite — only add `X` to `.env.example`.

## Phase 4: Folder Restructure

**Gating:** Skip entirely if Phase 0 signal #4 (layered structure) is clean OR project is a deliberately flat tool (<500 LOC, single binary).

**Hard skip markers — if ANY present, do not restructure:**
- `docs/adr/` exists with ≥1 ADR (architectural decisions already made).
- `internal/` exists with ≥2 sub-packages (layering already chosen).
- `CLAUDE.md` exists and references file paths (moving them invalidates docs).
- Frontend + backend already split into `frontend/`/`backend/` or `client/`/`server/`.

Based on project type, reorganize into:

**TypeScript/Next.js project:**
```
src/
  app/              # Next.js App Router (or pages/)
  components/       # React components
  lib/              # Shared utilities, API clients
  hooks/            # Custom React hooks
  types/            # TypeScript type definitions
  styles/           # Global styles
  config/           # App configuration (reads from env)
public/             # Static assets
tests/              # Test files
```

**TypeScript/Express or Node project:**
```
src/
  routes/           # API route handlers
  controllers/      # Business logic
  models/           # Data models
  middleware/        # Express middleware
  services/         # External service integrations
  utils/            # Shared utilities
  types/            # TypeScript types
  config/           # Configuration (reads from env)
tests/              # Test files
```

**Python project:**
```
src/ (or app/)
  api/              # API routes/views
  models/           # Data models
  services/         # Business logic
  utils/            # Shared utilities
  config/           # Configuration (reads from env)
tests/              # Test files
```

**Go project:**
```
cmd/
  {binary-name}/    # package main, one dir per binary entrypoint
    main.go
internal/           # private packages — not importable by other modules
  {domain}/         # business logic grouped by domain, not by layer
pkg/                # optional: packages safe for external import
api/                # optional: OpenAPI/proto definitions (services)
go.mod
go.sum
```
Right-size it: small CLI / single-binary tool stays flat (`main.go` + a few packages). Defer to `samber/cc-skills-golang@golang-project-layout` for the full decision table.

**Hybrid (Python/Go backend + TS frontend):**
```
backend/
  app/              # Python application
    api/
    models/
    services/
    config/
  requirements.txt
  pyproject.toml
frontend/
  src/
    app/
    components/
    lib/
    hooks/
    types/
  package.json
  tsconfig.json
```

Rules:
- Do NOT move files if project already has sensible structure — only reorganize scattered files.
- Use `git mv` (preserves history) — never `mv` followed by `git add`.
- **Before each move:** `codegraph_impact <symbol>` on every exported symbol in the file. Capture caller list.
- **After each move:** rewrite import paths in callers atomically (same commit). Re-run `codegraph_status` if index lags.
- Update all import paths after moving files.
- Verify no circular dependencies are introduced.
- After every move batch, run build/test to fail fast.

## Phase 4b: Go Standards (Go projects only)

**Gating:** Skip entirely for non-Go projects. For Go projects: skip if Phase 0 signals 4, 5, 6 all clean (project already idiomatic + builds). Run only the sub-items tied to flagged signals.

Each item delegates to a focused skill in `samber/cc-skills-golang`.

1. **Layout** — `samber/cc-skills-golang@golang-project-layout`. Module name matches repo path; private code under `internal/`; one `package main` per `cmd/{name}/`.
2. **Naming & style** — `samber/cc-skills-golang@golang-naming` + `@golang-code-style`. Exported docs; no stutter; short receivers; `gofmt`/`goimports` clean.
3. **Error handling** — `samber/cc-skills-golang@golang-error-handling`. `fmt.Errorf("...: %w", err)`; sentinels via `errors.Is`/`errors.As`; no silent `_ =` on meaningful errors; no `panic` in library code.
4. **Config** — typed config struct from env at startup (12-Factor). `samber/cc-skills-golang@golang-spf13-viper` if viper is in use; otherwise stdlib `os.Getenv` behind one loader.
5. **Testing** — `samber/cc-skills-golang@golang-testing` (+ `@golang-stretchr-testify` if used). Table-driven; isolate external state per test; `go test ./...` green.
6. **Lint & safety** — `samber/cc-skills-golang@golang-lint` + `@golang-security`. `golangci-lint run` clean; check SQL/cmd injection, unvalidated input.
7. **Docs** — `samber/cc-skills-golang@golang-documentation`. Package doc per package; exported symbols documented starting with identifier name.

Verification: `go build ./...`, `go vet ./...`, `go test ./...`, `golangci-lint run` (if available) all pass.

## Phase 5: Configuration & Deployment Readiness

Always run a subset — minimum is the `.gitignore` check.

1. `.gitignore` (project root OR parent if monorepo) includes:
   - `.env`, `.env.*`, allowlist `!.env.example`
   - `node_modules/`, `__pycache__/`, `.venv/`, `dist/`, `.next/`
   - Go: compiled binaries (`/{binary-name}`, `*.exe`), `vendor/` if not committed, coverage (`*.out`, `*.test`)
   - OS: `.DS_Store`, `Thumbs.db`
   - IDE: `.vscode/`, `.idea/`, `*.swp`
2. `tsconfig.json` exists and is correctly configured (TS projects).
3. Dependency manifests tidy: `requirements.txt`/`pyproject.toml`; `go mod tidy`.
4. Centralized config module reads all env vars with validation — only add if missing.
5. Verify project builds: `go build ./...`, `npm run build`, `python -c "import <pkg>"`.

## Phase 6: README Generation

**Gating:** Skip if `README.md` exists AND is >40 lines AND mentions setup + run. Otherwise:
- If no README: generate.
- If stub README (<40 lines or missing setup): propose additions inline, **diff before write**, ask user to confirm. Do NOT overwrite without confirmation.

Sections to include:
- Project name + one-line description
- Tech stack summary
- Prerequisites (lang versions)
- Setup: clone, install deps, copy `.env.example` to `.env` and fill values
- Development commands (start, test, build, lint)
- Folder structure overview
- Deployment instructions (if hosting platform detected)
- Environment variables table (name, description, required/optional)

If `CLAUDE.md` exists, cross-link it from README — do not duplicate its content.

</process>

<verification>
Before completing:
- CodeGraph status reported (READY pre-existing, READY newly-initialized, or UNAVAILABLE with reason)
- Triage table emitted with score and per-signal status
- `grep -rEn "(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*['\"][A-Za-z0-9_/+=-]{16,}['\"]" --include="*.{ts,tsx,js,jsx,py,go}"` returns 0 hits (excluding tests and env loaders)
- If Phase 3 ran: `.env` present with real values, `.env.example` mirrors keys
- `.gitignore` (project root or parent) lists `.env`
- Project builds: `go build ./...` / `npm run build` / `python -c "import <pkg>"` exits 0
- Go projects: `go vet ./...`, `go test ./...` pass; `golangci-lint run` if installed
- All imports resolve after restructure (`go build` fail = broken imports)
- If Phase 6 ran: README accurate, no claims that contradict actual file paths
- `git status` shows only intentional changes
</verification>

<output>
Always emit:
- Triage report: score `/8`, per-signal table, list of phases run vs skipped, list of mutations made.

When mutations occur:
- `.env` — secrets organized by category (only if real secrets were extracted)
- `.env.example` — placeholder template
- `.gitignore` — created or updated
- `README.md` — generated or amended (with diff confirmation if amended)
- `src/config/` or `internal/config/` — centralized env loader (only if missing)
- Restructured files with updated imports (only if Phase 4 ran)

When score ≤2 (REPORT-ONLY): only the triage report, plus a punch list of any 0–2 flagged signals with file:line evidence and a yes/no prompt.
</output>

<success_criteria>
- Phase 0 always runs first; mutations gated by score
- Dirty git tree blocks mutation unless explicitly overridden
- Zero hardcoded secrets remain (excluding tests and env-loader defaults)
- Existing `.env`, `.env.example`, `.gitignore`, `README.md`, `CLAUDE.md`, `docs/adr/` preserved or amended with confirmation — never silently overwritten
- Already-clean projects exit at Phase 0 with a report, not 6 phases of churn
- No restructure of projects with existing ADRs or layered `internal/`
- Build + test green after any mutation
- Triage report makes the skill's decisions auditable
</success_criteria>
