"""Gmail tools (draft, send, list).

Auth invariants:
- This module NEVER runs the OAuth flow. Agent workers must never open
  browsers. The ``droids-agents auth gmail`` CLI subcommand handles consent
  out-of-band and writes the token to ``GOOGLE_TOKEN_JSON``.
- ``_service()`` only loads, validates, and refreshes the token. Three
  distinct failure cases bubble up as ``GmailAuthError`` with actionable
  remediation in the message (M6 in plan).
"""

from __future__ import annotations

import base64
import json
import logging
from email.message import EmailMessage
from pathlib import Path

from agentspan.agents import tool
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from droids_agents.config import Settings

_log = logging.getLogger(__name__)

GMAIL_SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
)


class GmailAuthError(RuntimeError):
    """Auth failures for the Gmail API. Message always includes the remediation."""


def _load_token(path: Path) -> Credentials:
    if not path.exists():
        raise GmailAuthError(
            f"token file not found at {path}; run `droids-agents auth gmail` to create it"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        creds = Credentials.from_authorized_user_info(data, list(GMAIL_SCOPES))
    except (json.JSONDecodeError, ValueError, KeyError) as e:
        raise GmailAuthError(
            f"token file at {path} is malformed ({e}); "
            "re-run `droids-agents auth gmail` to regenerate"
        ) from e
    return creds


def _service(settings: Settings):
    """Build a Gmail API client. Refresh if needed; do NOT prompt for consent.

    Raises ``GmailAuthError`` if Gmail is not configured (Settings.gmail_enabled
    is False) — the messaging Subteam is opt-in.
    """
    if not settings.gmail_enabled or settings.google_token_json is None:
        raise GmailAuthError(
            "Gmail is not configured; set GOOGLE_CREDENTIALS_JSON and "
            "GOOGLE_TOKEN_JSON, then run `droids-agents auth gmail`"
        )
    creds = _load_token(settings.google_token_json)
    if not creds.valid:
        if not (creds.expired and creds.refresh_token):
            raise GmailAuthError(
                f"token at {settings.google_token_json} is expired without a refresh token; "
                "re-run `droids-agents auth gmail` to grant fresh consent"
            )
        try:
            creds.refresh(Request())
        except RefreshError as e:
            raise GmailAuthError(
                "refresh token rejected by Google (likely revoked); "
                "re-run `droids-agents auth gmail` to grant fresh consent"
            ) from e
        settings.google_token_json.write_text(creds.to_json(), encoding="utf-8")
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _build_raw(to: str, subject: str, body: str) -> str:
    msg = EmailMessage()
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")


# --- Tools ----------------------------------------------------------------
# Note: the @tool decorator does not see ``settings`` directly. The CLI
# composes the Settings instance into the Agent ``dependencies`` map and the
# tool reads it via ``ToolContext.dependencies['settings']``. Inline calls in
# tests can patch ``_service_for_tests``.


def _settings_from(context) -> Settings:
    deps = getattr(context, "dependencies", None) or {}
    settings = deps.get("settings")
    if settings is None:
        raise GmailAuthError(
            "Gmail tool invoked without a `settings` dependency; "
            "wire Settings into Agent(dependencies={'settings': settings})"
        )
    return settings


@tool
def gmail_draft(to: str, subject: str, body: str, context) -> dict:
    """Create a draft message. Returns the draft id."""
    svc = _service(_settings_from(context))
    raw = _build_raw(to, subject, body)
    draft = svc.users().drafts().create(userId="me", body={"message": {"raw": raw}}).execute()
    return {"draft_id": draft["id"]}


@tool(approval_required=True)
def gmail_send(to: str, subject: str, body: str, context) -> dict:
    """Send an email. Always pauses for HITL approval."""
    svc = _service(_settings_from(context))
    raw = _build_raw(to, subject, body)
    sent = svc.users().messages().send(userId="me", body={"raw": raw}).execute()
    return {"message_id": sent["id"], "thread_id": sent.get("threadId")}


@tool
def gmail_list(query: str, context, max: int = 10) -> dict:
    """List up to ``max`` message ids + snippets matching a Gmail search query."""
    svc = _service(_settings_from(context))
    listing = (
        svc.users()
        .messages()
        .list(userId="me", q=query, maxResults=max)
        .execute()
    )
    ids = [m["id"] for m in listing.get("messages", [])]
    results = []
    for mid in ids:
        msg = svc.users().messages().get(userId="me", id=mid, format="metadata").execute()
        results.append({"id": mid, "snippet": msg.get("snippet", "")})
    return {"results": results}
