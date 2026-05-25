from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


def _load_env_files() -> None:
    """Load .env from ~/.droids-agents/ first, then cwd (cwd overrides home)."""
    home_env = Path.home() / ".droids-agents" / ".env"
    if home_env.exists():
        load_dotenv(home_env, override=False)
    cwd_env = Path.cwd() / ".env"
    if cwd_env.exists():
        load_dotenv(cwd_env, override=True)


class SettingsError(RuntimeError):
    """Raised when a required setting is missing or malformed."""


def _require(key: str) -> str:
    val = os.environ.get(key, "").strip()
    if not val:
        raise SettingsError(f"required env var {key} is not set")
    return val


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


def _parse_allowlist(raw: str) -> tuple[str, ...]:
    return tuple(d.strip().lower() for d in raw.split(",") if d.strip())


@dataclass(frozen=True)
class Settings:
    anthropic_api_key: str
    droids_mem_mcp_token: str
    droids_mem_mcp_url: str
    agentspan_url: str
    google_credentials_json: Path | None
    google_token_json: Path | None
    log_dir: Path
    email_allowlist: tuple[str, ...] = field(default_factory=tuple)
    droids_names_file: Path | None = None

    @property
    def gmail_enabled(self) -> bool:
        """True iff both Gmail paths are set. Gmail tools / messaging Subteam
        refuse to run unless this is true."""
        return self.google_credentials_json is not None and self.google_token_json is not None

    @classmethod
    def load(cls) -> Settings:
        _load_env_files()

        log_dir = Path(
            os.environ.get("DROIDS_AGENTS_LOG_DIR")
            or (Path.home() / ".droids-agents" / "logs")
        ).expanduser()
        log_dir.mkdir(parents=True, exist_ok=True)

        names_file_env = os.environ.get("DROIDS_NAMES_FILE")
        names_file = Path(names_file_env).expanduser() if names_file_env else None

        # Gmail is OPTIONAL. Set both vars to enable messaging Subteam;
        # leave either unset to bypass Gmail entirely.
        gcj = os.environ.get("GOOGLE_CREDENTIALS_JSON", "").strip()
        gtj = os.environ.get("GOOGLE_TOKEN_JSON", "").strip()
        gmail_creds = Path(gcj).expanduser() if gcj else None
        gmail_token = Path(gtj).expanduser() if gtj else None

        return cls(
            anthropic_api_key=_require("ANTHROPIC_API_KEY"),
            droids_mem_mcp_token=_droids_mem_token(),
            droids_mem_mcp_url=os.environ.get(
                "DROIDS_MEM_MCP_URL", "http://localhost:7777/mcp"
            ),
            agentspan_url=os.environ.get("AGENTSPAN_URL", "http://localhost:6767"),
            google_credentials_json=gmail_creds,
            google_token_json=gmail_token,
            log_dir=log_dir,
            email_allowlist=_parse_allowlist(
                os.environ.get("DROIDS_AGENTS_EMAIL_ALLOWLIST", "")
            ),
            droids_names_file=names_file,
        )
