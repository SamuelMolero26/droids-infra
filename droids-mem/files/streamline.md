# Streamline droids-mem Startup

## Context

agentspan's TUI (`tui.py`) calls `_ping_mem()` before every run. If droids-mem isn't running it hard-fails with a stale hint referencing the deleted `cmd/droids-mem-mcp` binary. The binary exists at `~/go/bin/droids-mem` but may not be in agentspan's subprocess PATH. Three-layer fix: always-on LaunchAgent + agentspan auto-start + token fallback so `DROIDS_MEM_MCP_TOKEN` env var is no longer required.

---

## Changes

### 1. Rebuild binary (manual step first)
```bash
cd /Users/samuel/droid-infra/droids-mem && go install ./cmd/droids-mem
```
Ensures `~/go/bin/droids-mem` is current before wiring everything else.

---

### 2. macOS LaunchAgent — always-on

**Create:** `~/Library/LaunchAgents/com.droids.mem.plist`

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.droids.mem</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/samuel/go/bin/droids-mem</string>
    <string>serve</string>
  </array>
  <key>KeepAlive</key>
  <true/>
  <key>RunAtLoad</key>
  <true/>
  <key>StandardOutPath</key>
  <string>/Users/samuel/.droids-mem/mcp.log</string>
  <key>StandardErrorPath</key>
  <string>/Users/samuel/.droids-mem/mcp.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HOME</key>
    <string>/Users/samuel</string>
  </dict>
</dict>
</plist>
```

No `DROIDS_MEM_MCP_TOKEN` — server auto-generates and persists token to `~/.droids-mem/token`.

**Load:**
```bash
launchctl load ~/Library/LaunchAgents/com.droids.mem.plist
```

---

### 3. `droids-agents/src/config.py` — token fallback

Add helper after `_require`:
```python
def _droids_mem_token() -> str:
    """Token precedence: DROIDS_MEM_MCP_TOKEN env → ~/.droids-mem/token file."""
    val = os.environ.get("DROIDS_MEM_MCP_TOKEN", "").strip()
    if val:
        return val
    token_file = Path.home() / ".droids-mem" / "token"
    if token_file.exists():
        tok = token_file.read_text().strip()
        if tok:
            return tok
    raise SettingsError(
        "droids-mem token not found: set DROIDS_MEM_MCP_TOKEN or run `droids-mem ensure-server` first"
    )
```

Change line 75:
```python
# Before:
droids_mem_mcp_token=_require("DROIDS_MEM_MCP_TOKEN"),

# After:
droids_mem_mcp_token=_droids_mem_token(),
```

---

### 4. `droids-agents/src/tui.py` — auto-start + fix stale hint

Add imports:
```python
import shutil
import subprocess
```

Add helper after `_ping_agentspan`:
```python
def _ensure_droids_mem() -> tuple[bool, str]:
    """Best-effort ensure-server call. Returns (ok, detail)."""
    binary = shutil.which("droids-mem") or str(Path.home() / "go" / "bin" / "droids-mem")
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
```

In `_run_worker()`, replace the `_ping_mem` block:
```python
# Auto-start droids-mem if down
mem_ok, mem_detail = _ping_mem(settings)
if not mem_ok:
    state.event("[yellow]droids-mem: starting...[/]")
    ens_ok, ens_detail = _ensure_droids_mem()
    if ens_ok:
        mem_ok, mem_detail = _ping_mem(settings)  # re-check after start
    else:
        state.event(f"[red]ensure-server failed: {ens_detail}[/]")

if mem_ok:
    state.event(f"[bold green]droids-mem: connected[/] ({mem_detail})")
else:
    state.event(f"[bold red]droids-mem: UNREACHABLE[/] ({mem_detail})")
    state.event("[red]hint: run `droids-mem ensure-server` or check ~/Library/LaunchAgents/com.droids.mem.plist[/]")
    state.error = f"droids-mem not reachable: {mem_detail}"
    state.status = "error"
    return
```

---

## Verification

1. `go install ./cmd/droids-mem` — confirm binary updated
2. `launchctl load ~/Library/LaunchAgents/com.droids.mem.plist`
3. `curl http://localhost:7777/healthz` → `{"status":"ok"}`
4. `cat ~/.droids-mem/token` → token exists
5. Unset `DROIDS_MEM_MCP_TOKEN`, run agentspan TUI → loads token from file
6. `launchctl stop com.droids.mem` (simulate crash) → auto-restarts
7. Start agentspan with server down → "droids-mem: starting..." → connects
