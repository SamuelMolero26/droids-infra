"""Droid name pool for human-readable CLI/log identifiers.

Names are cosmetic. `sess_id` and `exec_id` are the authoritative identifiers;
two concurrent Executions may both claim `C-3PO` — UI cards and CLI banners
show the IDs alongside the name to disambiguate.

Within one Execution, names are guaranteed unique. The pool replenishes from
the yaml; if exhausted, names get a numeric suffix (`C-3PO-2`).
"""

from __future__ import annotations

import random
import threading
from pathlib import Path

import yaml

# Stable role labels surfaced in `agent_display()`. Map from internal agent role
# (the name used in factories / Sub-agent identifiers) to human label.
ROLE_LABELS: dict[str, str] = {
    "competitor": "Researcher",
    "extractor": "Doc-Extractor",
    "synthesizer": "Doc-Synth",
    "form_planner": "Form-Planner",
    "form_executor": "Form-Executor",
    "drafter": "Email-Drafter",
    "sender": "Email-Sender",
    "router_classifier": "Router",
    "memory_loader": "Memory-Loader",
    "rollup": "Rollup",
}


class NamePoolError(RuntimeError):
    """Raised when the name pool cannot load or is misused."""


def _default_names_file() -> Path:
    """Resolve droid-infra/droids-name.yml relative to this package.

    Layout: droid-infra/droids-agents/src/naming.py
            parents[2] -> droid-infra/  (repo root, where droids-name.yml lives)
    """
    here = Path(__file__).resolve()
    return here.parents[2] / "droids-name.yml"


def load_names(path: Path | None = None) -> list[str]:
    p = path or _default_names_file()
    if not p.exists():
        raise NamePoolError(f"droid name yaml not found at {p}")
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    names = data.get("droids") if isinstance(data, dict) else None
    if not names or not isinstance(names, list):
        raise NamePoolError(f"droid name yaml at {p} missing 'droids' list")
    return [str(n).strip() for n in names if str(n).strip()]


class NamePool:
    """Thread-safe per-Execution name pool. Not shared across Executions."""

    def __init__(self, names: list[str] | None = None, *, seed: int | None = None) -> None:
        loaded = names if names is not None else load_names()
        if not loaded:
            raise NamePoolError("name pool is empty")
        rng = random.Random(seed)
        shuffled = list(loaded)
        rng.shuffle(shuffled)
        self._available: list[str] = shuffled
        self._claimed: set[str] = set()
        self._suffix_counter: int = 0
        self._base_pool: tuple[str, ...] = tuple(loaded)
        self._lock = threading.Lock()

    def claim(self) -> str:
        with self._lock:
            if self._available:
                name = self._available.pop()
                self._claimed.add(name)
                return name
            # Exhausted base pool — rotate base names with numeric suffix.
            self._suffix_counter += 1
            base = self._base_pool[self._suffix_counter % len(self._base_pool)]
            name = f"{base}-{self._suffix_counter + 1}"
            self._claimed.add(name)
            return name

    def release(self, name: str) -> None:
        with self._lock:
            if name not in self._claimed:
                return
            self._claimed.discard(name)
            # Only re-pool the original base names (suffixed ones stay retired).
            if name in self._base_pool:
                self._available.append(name)


def agent_display(droid_name: str, role: str) -> str:
    """Format ``C-3PO: [Researcher]`` from a claimed droid name and an internal role key."""
    label = ROLE_LABELS.get(role, role)
    return f"{droid_name}: [{label}]"


def claim_for_role(pool: NamePool, role: str) -> dict[str, str]:
    """Claim a droid name and return the metadata bundle every factory MUST attach.

    Returns a dict with keys ``droid_name``, ``role``, ``role_label``, ``agent_display``.
    Pass the result straight into ``Agent(..., metadata=...)`` so the agentspan UI
    surfaces the droid identity; the CLI banner / structlog binder read the same
    keys via ``ToolContext.metadata`` for consistent visibility on both surfaces.
    """
    droid = pool.claim()
    label = ROLE_LABELS.get(role, role)
    return {
        "droid_name": droid,
        "role": role,
        "role_label": label,
        "agent_display": f"{droid}: [{label}]",
    }
