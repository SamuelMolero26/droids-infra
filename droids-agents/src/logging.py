"""structlog setup with pre-session buffer.

The session_id is unknown until memory_loader returns it from the MCP server.
Until then, every structlog record is held in a bounded in-memory queue.
Once `bind_session(session_id)` is called exactly once, the buffer is flushed
to `<log_dir>/<session_id>.jsonl` and structlog's processor chain is switched
to direct file writes.

Overflow (buffer >10k records) is a fail-loud bug — it means session_id never
resolved. The buffer raises RuntimeError on the next emit attempt past the cap.
"""

from __future__ import annotations

import json
import queue
import sys
import threading
from pathlib import Path
from typing import Any

import structlog

_BUFFER_CAP = 10_000

_lock = threading.Lock()
_state: dict[str, Any] = {
    "buffer": queue.Queue(maxsize=_BUFFER_CAP),
    "session_id": None,
    "log_path": None,
    "log_dir": None,
    "file": None,
    "configured": False,
    "overflowed": False,
}


def _buffer_processor(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """structlog processor: tee event to file if session bound, else buffer."""
    line = json.dumps(event_dict, default=str)
    sink = _state.get("file")
    if sink is not None:
        sink.write(line + "\n")
        sink.flush()
    else:
        try:
            _state["buffer"].put_nowait(line)
        except queue.Full as e:
            _state["overflowed"] = True
            raise RuntimeError(
                f"droids-agents log buffer overflow (>{_BUFFER_CAP} records) — "
                "session_id never resolved; memory_loader likely failed"
            ) from e
    return event_dict


def configure(log_dir: Path, dev_stderr: bool = False) -> None:
    """Set up structlog. Idempotent within a process."""
    with _lock:
        if _state["configured"]:
            return
        _state["log_dir"] = log_dir
        processors: list[Any] = [
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            _buffer_processor,
        ]
        if dev_stderr:
            processors.append(structlog.dev.ConsoleRenderer(colors=False, file=sys.stderr))
        else:
            processors.append(structlog.processors.JSONRenderer())
        structlog.configure(
            processors=processors,
            wrapper_class=structlog.make_filtering_bound_logger(20),
            logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
            cache_logger_on_first_use=True,
        )
        _state["configured"] = True


def bind_session(session_id: str) -> Path:
    """Resolve session, flush buffered records to file, switch to direct writes.

    Returns the resolved log file path. Must be called exactly once.
    """
    with _lock:
        if _state["session_id"] is not None:
            raise RuntimeError(
                f"bind_session called twice (already bound to {_state['session_id']!r})"
            )
        log_dir = _state["log_dir"]
        if log_dir is None:
            raise RuntimeError("configure() must be called before bind_session()")
        log_path = Path(log_dir) / f"{session_id}.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fh = log_path.open("a", encoding="utf-8")

        buf: queue.Queue[str] = _state["buffer"]
        while True:
            try:
                line = buf.get_nowait()
            except queue.Empty:
                break
            fh.write(line + "\n")
        fh.flush()

        _state["file"] = fh
        _state["session_id"] = session_id
        _state["log_path"] = log_path
        return log_path


def close() -> None:
    """Flush + close the log file. Call on process shutdown."""
    with _lock:
        fh = _state.get("file")
        if fh is not None:
            fh.flush()
            fh.close()
            _state["file"] = None


def get_logger(*, agent_display: str | None = None) -> structlog.BoundLogger:
    """Return a logger optionally bound with agent display name (e.g. 'C-3PO: [Researcher]')."""
    log = structlog.get_logger()
    if agent_display:
        log = log.bind(agent_display=agent_display)
    return log


class SessionLogger:
    """Per-session file logger for TUI concurrent sessions.

    Narrow scope: only run_session writes to it. Writes key milestones to
    <log_dir>/<session_id>.jsonl. No-ops silently when log_dir is None.
    Safe to use from a worker thread; bind() may be called at most once.
    """

    def __init__(self, log_dir: Path | None) -> None:
        self._log_dir = Path(log_dir) if log_dir else None
        self._buffer: list[str] = []
        self._file: Any = None
        self._lock = threading.Lock()

    def bind(self, session_id: str) -> None:
        """Open log file and flush pre-bind buffer. Call once after session_id is known."""
        if self._log_dir is None:
            return
        with self._lock:
            log_path = self._log_dir / f"{session_id}.jsonl"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            fh = log_path.open("a", encoding="utf-8")
            for line in self._buffer:
                fh.write(line + "\n")
            fh.flush()
            self._buffer.clear()
            self._file = fh

    def log(self, event: str, **kw: Any) -> None:
        if self._log_dir is None:
            return
        from datetime import UTC, datetime
        line = json.dumps(
            {"ts": datetime.now(UTC).isoformat(), "event": event, **kw},
            default=str,
        )
        with self._lock:
            if self._file is not None:
                self._file.write(line + "\n")
                self._file.flush()
            else:
                self._buffer.append(line)

    def close(self) -> None:
        with self._lock:
            if self._file is not None:
                self._file.flush()
                self._file.close()
                self._file = None
