"""Crash-safe JSON state for polling and trigger delivery."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)
STATE_VERSION = 1
SEEN_LIMIT = 500


def _require_dict(value: object, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be an object")
    if not all(isinstance(key, str) for key in value):
        raise ValueError(f"{field_name} keys must be strings")
    return value


def _timestamp_key(value: str) -> tuple[datetime, str]:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC), value
    except ValueError:
        return datetime.min.replace(tzinfo=UTC), value


@dataclass
class PendingReply:
    """A reply awaiting best-effort delivery."""

    text: str
    resolve: bool
    attempts: int = 0

    @classmethod
    def from_dict(cls, data: object) -> PendingReply:
        """Decode and validate one pending reply."""
        values = _require_dict(data, "pending reply")
        text = values.get("text")
        resolve = values.get("resolve")
        attempts = values.get("attempts", 0)
        if not isinstance(text, str):
            raise ValueError("pending reply text must be a string")
        if not isinstance(resolve, bool):
            raise ValueError("pending reply resolve must be a boolean")
        if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 0:
            raise ValueError("pending reply attempts must be a nonnegative integer")
        return cls(text=text, resolve=resolve, attempts=attempts)

    def to_dict(self) -> dict[str, object]:
        """Encode one pending reply."""
        return {"text": self.text, "resolve": self.resolve, "attempts": self.attempts}


@dataclass
class FileState:
    """Polling and trigger state for one Drive file."""

    watermark: str | None = None
    seen: dict[str, str] = field(default_factory=dict)
    inflight: dict[str, str] = field(default_factory=dict)
    pending_replies: dict[str, PendingReply] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: object) -> FileState:
        """Decode and validate one file's state."""
        values = _require_dict(data, "file state")
        watermark = values.get("watermark")
        if watermark is not None and not isinstance(watermark, str):
            raise ValueError("watermark must be a string or null")

        seen_data = _require_dict(values.get("seen", {}), "seen")
        if not all(isinstance(value, str) for value in seen_data.values()):
            raise ValueError("seen values must be timestamps")

        inflight_data = _require_dict(values.get("inflight", {}), "inflight")
        if not all(stage in {"suggesting", "replying"} for stage in inflight_data.values()):
            raise ValueError("inflight stages must be 'suggesting' or 'replying'")

        pending_data = _require_dict(values.get("pending_replies", {}), "pending_replies")
        state = cls(
            watermark=watermark,
            seen={key: str(value) for key, value in seen_data.items()},
            inflight={key: str(value) for key, value in inflight_data.items()},
            pending_replies={
                key: PendingReply.from_dict(value) for key, value in pending_data.items()
            },
        )
        state.bound_seen()
        return state

    def to_dict(self) -> dict[str, object]:
        """Encode one file's state."""
        self.bound_seen()
        return {
            "watermark": self.watermark,
            "seen": self.seen,
            "inflight": self.inflight,
            "pending_replies": {
                key: value.to_dict() for key, value in self.pending_replies.items()
            },
        }

    def mark_seen(self, comment_id: str, modified_time: str) -> None:
        """Record a handled comment while enforcing the history bound."""
        self.seen[comment_id] = modified_time
        self.bound_seen()

    def bound_seen(self) -> None:
        """Retain only the 500 most recently modified handled comments."""
        overflow = len(self.seen) - SEEN_LIMIT
        if overflow <= 0:
            return
        oldest = sorted(self.seen.items(), key=lambda item: (_timestamp_key(item[1]), item[0]))
        for comment_id, _ in oldest[:overflow]:
            del self.seen[comment_id]


@dataclass
class State:
    """All persisted daemon state, keyed by Drive file ID."""

    files: dict[str, FileState] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> State:
        """Load state, falling back to a fresh instance when unavailable."""
        state_path = path.expanduser()
        try:
            raw = json.loads(state_path.read_text(encoding="utf-8"))
            data = _require_dict(raw, "state")
            if data.get("version") != STATE_VERSION:
                raise ValueError(f"unsupported state version: {data.get('version')!r}")
            files_data = _require_dict(data.get("files", {}), "files")
            return cls(files={key: FileState.from_dict(value) for key, value in files_data.items()})
        except FileNotFoundError:
            return cls()
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
            LOGGER.warning("could not load state from %s; starting fresh: %s", state_path, exc)
            return cls()

    def save(self, path: Path) -> None:
        """Atomically persist state to disk."""
        state_path = path.expanduser()
        state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": STATE_VERSION,
            "files": {key: value.to_dict() for key, value in self.files.items()},
        }

        temporary_path: Path | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                dir=state_path.parent,
                prefix=f".{state_path.name}.",
                suffix=".tmp",
            )
            temporary_path = Path(temporary_name)
            os.chmod(temporary_path, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, state_path)
            temporary_path = None
            directory_fd = os.open(state_path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
