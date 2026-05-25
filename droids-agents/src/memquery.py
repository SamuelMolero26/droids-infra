"""Read-only queries into droids-mem via the CLI subprocess.

The TUI browses + searches previously-saved memories. droids-mem's ``list`` and
``search`` are operator/CLI commands (not on the MCP bridge, ADR 0003), so the
TUI shells out to the binary — same pattern as ``ensure-server``.

Pure and testable: ``_run`` is the only subprocess seam; inject a fake in tests.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


class MemQueryError(RuntimeError):
    """droids-mem CLI was unreachable, errored, or returned unparseable output."""


def droids_mem_binary() -> str:
    """Resolve the droids-mem binary: PATH → $GOPATH/bin → ~/go/bin."""
    gopath_bin = Path(os.environ.get("GOPATH", "")).expanduser() / "bin" / "droids-mem"
    return (
        shutil.which("droids-mem")
        or (str(gopath_bin) if gopath_bin.exists() else None)
        or str(Path.home() / "go" / "bin" / "droids-mem")
    )


@dataclass
class Memory:
    id: str
    kind: str
    title: str
    task_type: str = ""
    session_id: str = ""
    what: str = ""
    learned: str = ""
    tags: str = ""
    created_at: int = 0

    @classmethod
    def from_dict(cls, d: dict) -> Memory:
        return cls(
            id=str(d.get("id", "")),
            kind=str(d.get("kind", "")),
            title=str(d.get("title", "")),
            task_type=str(d.get("task_type", "")),
            session_id=str(d.get("session_id", "")),
            what=str(d.get("what", "")),
            learned=str(d.get("learned", "")),
            tags=str(d.get("tags", "")),
            created_at=int(d.get("created_at", 0) or 0),
        )


@dataclass
class Session:
    session_id: str
    task_type: str
    created_at: int  # most-recent memory in the session
    memories: list[Memory]

    @property
    def summary(self) -> Memory | None:
        """The session_summary memory, if the rollup wrote one."""
        return next((m for m in self.memories if m.kind == "session_summary"), None)

    @property
    def title(self) -> str:
        s = self.summary
        if s and s.title:
            return s.title
        return f"{self.task_type or 'session'} ({len(self.memories)} memories)"


def _run(args: list[str], *, timeout: float = 10.0) -> dict:
    """Run `droids-mem <args>` and return parsed JSON stdout. The single
    subprocess seam — tests monkeypatch this."""
    binary = droids_mem_binary()
    if not Path(binary).exists():
        raise MemQueryError(f"droids-mem binary not found at {binary}")
    try:
        r = subprocess.run(
            [binary, *args], capture_output=True, text=True, timeout=timeout
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise MemQueryError(str(e)) from e
    if r.returncode != 0:
        raise MemQueryError(r.stderr.strip() or f"droids-mem exit {r.returncode}")
    try:
        return json.loads(r.stdout)
    except ValueError as e:
        raise MemQueryError(f"bad JSON from droids-mem: {e}") from e


def list_sessions(limit: int = 100) -> list[Session]:
    """Recent memories grouped into Sessions by session_id, newest first.

    V1 caps at `limit` recent memories (no --session-id filter yet; see CLI.todo).
    """
    data = _run(["list", "--limit", str(limit)])
    by_sid: dict[str, list[Memory]] = {}
    for raw in data.get("memories", []):
        m = Memory.from_dict(raw)
        if not m.session_id:
            continue
        by_sid.setdefault(m.session_id, []).append(m)

    sessions: list[Session] = []
    for sid, mems in by_sid.items():
        mems.sort(key=lambda m: m.created_at, reverse=True)
        sessions.append(
            Session(
                session_id=sid,
                task_type=mems[0].task_type,
                created_at=mems[0].created_at,
                memories=mems,
            )
        )
    sessions.sort(key=lambda s: s.created_at, reverse=True)
    return sessions


def search(query: str) -> list[Memory]:
    """Full-text search across all memories. Returns matches ranked by droids-mem."""
    if not query.strip():
        return []
    data = _run(["search", "--query", query])
    return [Memory.from_dict(m) for m in data.get("results", [])]
