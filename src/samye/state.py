"""Crash-safe JSON state for polling and trigger delivery."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

LOGGER = logging.getLogger(__name__)
STATE_VERSION = 1
SEEN_LIMIT = 500
TERMINAL_PROPOSAL_LIMIT = 50
ProposalStatus = Literal[
    "pending",
    "applying",
    "applied",
    "rejected",
    "stale",
    "indeterminate",
]
TERMINAL_PROPOSAL_STATUSES = {"applied", "rejected", "stale", "indeterminate"}


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
    comment_modified_time: str
    attempts: int = 0

    @classmethod
    def from_dict(cls, data: object) -> PendingReply:
        """Decode and validate one pending reply."""
        values = _require_dict(data, "pending reply")
        text = values.get("text")
        resolve = values.get("resolve")
        comment_modified_time = values.get("comment_modified_time")
        attempts = values.get("attempts", 0)
        if not isinstance(text, str):
            raise ValueError("pending reply text must be a string")
        if not isinstance(resolve, bool):
            raise ValueError("pending reply resolve must be a boolean")
        if not isinstance(comment_modified_time, str):
            raise ValueError("pending reply comment_modified_time must be a string")
        if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 0:
            raise ValueError("pending reply attempts must be a nonnegative integer")
        return cls(
            text=text,
            resolve=resolve,
            comment_modified_time=comment_modified_time,
            attempts=attempts,
        )

    def to_dict(self) -> dict[str, object]:
        """Encode one pending reply."""
        return {
            "text": self.text,
            "resolve": self.resolve,
            "comment_modified_time": self.comment_modified_time,
            "attempts": self.attempts,
        }


@dataclass
class Inflight:
    """A persisted remote-operation intent awaiting completion or recovery."""

    stage: Literal["mutating", "replying"]
    comment_modified_time: str

    @classmethod
    def from_dict(cls, data: object) -> Inflight:
        """Decode and validate one inflight operation."""
        values = _require_dict(data, "inflight entry")
        stage = values.get("stage")
        comment_modified_time = values.get("comment_modified_time")
        if stage not in {"mutating", "replying"}:
            raise ValueError("inflight stage must be 'mutating' or 'replying'")
        if not isinstance(comment_modified_time, str):
            raise ValueError("inflight comment_modified_time must be a string")
        return cls(stage=stage, comment_modified_time=comment_modified_time)

    def to_dict(self) -> dict[str, str]:
        """Encode one inflight operation."""
        return {
            "stage": self.stage,
            "comment_modified_time": self.comment_modified_time,
        }


@dataclass
class Proposal:
    """A locally reviewable replacement proposed for a Google Doc."""

    id: str
    comment_id: str
    comment_modified_time: str
    tab_id: str
    document_title: str
    target_text: str
    replacement: str
    provider: str
    model: str
    created: str
    status: ProposalStatus = "pending"

    @classmethod
    def from_dict(cls, data: object) -> Proposal:
        """Decode and validate one proposal."""
        values = _require_dict(data, "proposal")
        fields = (
            "id",
            "comment_id",
            "comment_modified_time",
            "tab_id",
            "document_title",
            "target_text",
            "replacement",
            "provider",
            "model",
            "created",
        )
        decoded: dict[str, str] = {}
        for field_name in fields:
            value = values.get(field_name)
            if not isinstance(value, str):
                raise ValueError(f"proposal {field_name} must be a string")
            decoded[field_name] = value
        status = values.get("status", "pending")
        if status not in {
            "pending",
            "applying",
            "applied",
            "rejected",
            "stale",
            "indeterminate",
        }:
            raise ValueError("invalid proposal status")
        return cls(**decoded, status=status)

    def to_dict(self) -> dict[str, str]:
        """Encode one proposal."""
        return {
            "id": self.id,
            "comment_id": self.comment_id,
            "comment_modified_time": self.comment_modified_time,
            "tab_id": self.tab_id,
            "document_title": self.document_title,
            "target_text": self.target_text,
            "replacement": self.replacement,
            "provider": self.provider,
            "model": self.model,
            "created": self.created,
            "status": self.status,
        }


@dataclass
class FileState:
    """Polling and trigger state for one Drive file."""

    watermark: str | None = None
    seen: dict[str, str] = field(default_factory=dict)
    inflight: dict[str, Inflight] = field(default_factory=dict)
    pending_replies: dict[str, PendingReply] = field(default_factory=dict)
    proposals: dict[str, Proposal] = field(default_factory=dict)

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
        inflight: dict[str, Inflight] = {}
        for comment_id, entry in inflight_data.items():
            if isinstance(entry, str):
                LOGGER.warning("dropping legacy inflight entry for comment %s", comment_id)
                continue
            inflight[comment_id] = Inflight.from_dict(entry)

        pending_data = _require_dict(values.get("pending_replies", {}), "pending_replies")
        pending_replies: dict[str, PendingReply] = {}
        for comment_id, entry in pending_data.items():
            if isinstance(entry, dict) and "comment_modified_time" not in entry:
                LOGGER.warning("dropping legacy pending reply for comment %s", comment_id)
                continue
            pending_replies[comment_id] = PendingReply.from_dict(entry)

        proposals_data = _require_dict(values.get("proposals", {}), "proposals")
        state = cls(
            watermark=watermark,
            seen={key: str(value) for key, value in seen_data.items()},
            inflight=inflight,
            pending_replies=pending_replies,
            proposals={key: Proposal.from_dict(value) for key, value in proposals_data.items()},
        )
        state.bound_seen()
        state.bound_proposals()
        return state

    def to_dict(self) -> dict[str, object]:
        """Encode one file's state."""
        self.bound_seen()
        self.bound_proposals()
        return {
            "watermark": self.watermark,
            "seen": self.seen,
            "inflight": {key: value.to_dict() for key, value in self.inflight.items()},
            "pending_replies": {
                key: value.to_dict() for key, value in self.pending_replies.items()
            },
            "proposals": {key: value.to_dict() for key, value in self.proposals.items()},
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

    def bound_proposals(self) -> None:
        """Retain only the 50 newest terminal proposals."""
        terminal = [
            (proposal_id, proposal)
            for proposal_id, proposal in self.proposals.items()
            if proposal.status in TERMINAL_PROPOSAL_STATUSES
        ]
        overflow = len(terminal) - TERMINAL_PROPOSAL_LIMIT
        if overflow <= 0:
            return
        oldest = sorted(
            terminal,
            key=lambda item: (_timestamp_key(item[1].created), item[0]),
        )
        for proposal_id, _ in oldest[:overflow]:
            del self.proposals[proposal_id]


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
