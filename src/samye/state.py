"""Crash-safe JSON state for polling and trigger delivery."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class PendingReply:
    """A reply awaiting best-effort delivery."""

    text: str
    resolve: bool
    attempts: int = 0


@dataclass
class FileState:
    """Polling and trigger state for one Drive file."""

    watermark: str | None = None
    seen: dict[str, str] = field(default_factory=dict)
    inflight: dict[str, str] = field(default_factory=dict)
    pending_replies: dict[str, PendingReply] = field(default_factory=dict)


@dataclass
class State:
    """All persisted daemon state, keyed by Drive file ID."""

    files: dict[str, FileState] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> State:
        """Load state, falling back to a fresh instance when unavailable."""
        raise NotImplementedError

    def save(self, path: Path) -> None:
        """Atomically persist state to disk."""
        raise NotImplementedError
