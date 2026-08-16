"""Tests for crash-safe daemon state."""

import json
import logging
import stat
from pathlib import Path

import pytest

from samye.state import FileState, PendingReply, State


def test_round_trip_state(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "state.json"
    original = State(
        files={
            "doc-1": FileState(
                watermark="2026-08-16T10:00:00Z",
                seen={"comment-1": "2026-08-16T09:00:00Z"},
                inflight={"comment-2": "replying"},
                pending_replies={
                    "comment-2": PendingReply(text="suggested", resolve=False, attempts=1)
                },
            )
        }
    )

    original.save(path)

    assert State.load(path) == original
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not list(path.parent.glob(".state.json.*.tmp"))


def test_missing_state_is_fresh(tmp_path: Path) -> None:
    assert State.load(tmp_path / "missing.json") == State()


def test_corrupt_state_warns_and_is_fresh(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "state.json"
    path.write_text("not JSON", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="samye.state"):
        state = State.load(path)

    assert state == State()
    assert "starting fresh" in caplog.text


def test_rejects_semantically_invalid_state(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "files": {"doc": {"inflight": {"comment": "invalid-stage"}}},
            }
        ),
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING, logger="samye.state"):
        state = State.load(path)

    assert state == State()
    assert "inflight stages" in caplog.text


def test_seen_is_bounded_to_newest_500(tmp_path: Path) -> None:
    file_state = FileState()
    for index in range(505):
        file_state.mark_seen(
            f"comment-{index}",
            f"2026-08-{1 + index // 24:02d}T{index % 24:02d}:00:00Z",
        )
    state = State(files={"doc": file_state})
    path = tmp_path / "state.json"

    state.save(path)
    restored = State.load(path)

    assert len(restored.files["doc"].seen) == 500
    for index in range(5):
        assert f"comment-{index}" not in restored.files["doc"].seen
    assert "comment-504" in restored.files["doc"].seen
