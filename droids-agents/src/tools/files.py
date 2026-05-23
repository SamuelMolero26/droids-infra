"""Local document read tool.

Stateless: ``read_doc`` only executes one path on demand. The ``--docs`` list
itself is parsed, validated, and plumbed by the CLI; this tool simply opens
the requested path if its extension is allowed.
"""

from __future__ import annotations

from pathlib import Path

from agentspan.agents import tool
from pypdf import PdfReader

_READ_CAP_CHARS: int = 50_000
_ALLOWED_EXTS: frozenset[str] = frozenset({".pdf", ".md", ".txt"})


class DocReadError(RuntimeError):
    """Raised on missing, unsupported, or malformed document reads."""


def _read_pdf(path: Path) -> str:
    reader = PdfReader(str(path))
    parts: list[str] = []
    total = 0
    for page in reader.pages:
        text = page.extract_text() or ""
        parts.append(text)
        total += len(text)
        if total >= _READ_CAP_CHARS:
            break
    return "".join(parts)


@tool
def read_doc(path: str) -> dict:
    """Read a local document (``.pdf``, ``.md``, ``.txt``). Caps at 50k chars.

    Returns ``{"text": str, "truncated": bool, "basename": str}``. Raises
    ``DocReadError`` on missing path or unsupported extension.
    """
    p = Path(path).expanduser()
    if not p.exists() or not p.is_file():
        raise DocReadError(f"path not found or not a file: {p}")
    ext = p.suffix.lower()
    if ext not in _ALLOWED_EXTS:
        raise DocReadError(
            f"unsupported extension {ext!r} for {p}; allowed: {sorted(_ALLOWED_EXTS)}"
        )

    if ext == ".pdf":
        text = _read_pdf(p)
    else:
        text = p.read_text(encoding="utf-8", errors="replace")

    truncated = len(text) > _READ_CAP_CHARS
    return {"text": text[:_READ_CAP_CHARS], "truncated": truncated, "basename": p.name}
