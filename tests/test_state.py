"""Tests for crash-safe daemon state."""

import json
import logging
import stat
from pathlib import Path

import pytest

from samye.state import FileState, Inflight, PendingReply, Proposal, ProposalStatus, State


def make_proposal(index: int, status: ProposalStatus = "pending") -> Proposal:
    return Proposal(
        id=f"proposal-{index}",
        comment_id=f"comment-{index}",
        comment_modified_time="2026-08-16T09:00:00Z",
        tab_id="tab-1",
        document_title="Test document",
        target_text="before",
        replacement="after",
        provider="local",
        model="model",
        created=f"2026-08-16T10:{index:02d}:00Z",
        status=status,
    )


def test_round_trip_state(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "state.json"
    original = State(
        files={
            "doc-1": FileState(
                watermark="2026-08-16T10:00:00Z",
                seen={"comment-1": "2026-08-16T09:00:00Z"},
                inflight={
                    "comment-2": Inflight(
                        stage="replying",
                        comment_modified_time="2026-08-16T09:01:00Z",
                    )
                },
                pending_replies={
                    "comment-2": PendingReply(
                        text="proposal ready",
                        resolve=False,
                        comment_modified_time="2026-08-16T09:01:00Z",
                        attempts=1,
                    )
                },
                proposals={"proposal-1": make_proposal(1)},
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
                "files": {
                    "doc": {
                        "inflight": {
                            "comment": {
                                "stage": "invalid-stage",
                                "comment_modified_time": "2026-08-16T09:00:00Z",
                            }
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING, logger="samye.state"):
        state = State.load(path)

    assert state == State()
    assert "inflight stage" in caplog.text


def test_loads_old_shape_with_defaults_and_drops_unrecoverable_entries(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "files": {
                    "doc": {
                        "watermark": "2026-08-16T10:00:00Z",
                        "seen": {"handled": "2026-08-16T09:00:00Z"},
                        "inflight": {"legacy-inflight": "replying"},
                        "pending_replies": {
                            "legacy-reply": {
                                "text": "done",
                                "resolve": True,
                                "attempts": 1,
                            }
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING, logger="samye.state"):
        state = State.load(path)

    file_state = state.files["doc"]
    assert file_state.watermark == "2026-08-16T10:00:00Z"
    assert file_state.seen == {"handled": "2026-08-16T09:00:00Z"}
    assert file_state.inflight == {}
    assert file_state.pending_replies == {}
    assert file_state.proposals == {}
    assert "dropping legacy inflight entry" in caplog.text
    assert "dropping legacy pending reply" in caplog.text


@pytest.mark.parametrize(
    "status",
    ["pending", "applying", "applied", "rejected", "stale", "indeterminate"],
)
def test_proposal_round_trip_for_every_status(
    tmp_path: Path, status: ProposalStatus
) -> None:
    proposal = make_proposal(1, status)
    state = State(files={"doc": FileState(proposals={proposal.id: proposal})})
    path = tmp_path / "state.json"

    state.save(path)

    assert State.load(path) == state


def test_terminal_proposals_are_bounded_to_newest_50(tmp_path: Path) -> None:
    proposals = {
        proposal.id: proposal
        for proposal in [
            *(make_proposal(index, "applied") for index in range(55)),
            make_proposal(55, "pending"),
            make_proposal(56, "applying"),
        ]
    }
    state = State(files={"doc": FileState(proposals=proposals)})
    path = tmp_path / "state.json"

    state.save(path)
    restored = State.load(path)

    retained = restored.files["doc"].proposals
    assert len(retained) == 52
    for index in range(5):
        assert f"proposal-{index}" not in retained
    assert "proposal-54" in retained
    assert "proposal-55" in retained
    assert "proposal-56" in retained


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
