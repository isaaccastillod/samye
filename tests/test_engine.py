"""End-to-end tests for comment orchestration and proposal transitions."""

from __future__ import annotations

import asyncio
import copy
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from googleapiclient.errors import HttpError

from samye.config import Config
from samye.engine import Engine, _safe_error_summary
from samye.gdocs import Doc, NamedRangeInfo, RevisionConflict
from samye.providers.base import ProviderError
from samye.state import FileState, Inflight, PendingReply, Proposal, State
from samye.textmap import Span


@pytest.fixture(autouse=True)
def run_threads_inline(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run(function: Callable[..., Any], *args: object, **kwargs: object) -> Any:
        return function(*args, **kwargs)

    monkeypatch.setattr("samye.engine.asyncio.to_thread", run)


class FakeProvider:
    name = "local"

    def __init__(self, result: str | Exception = "after") -> None:
        self.result = result
        self.calls: list[tuple[str, str]] = []

    async def complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeGDocs:
    def __init__(self, doc: Doc) -> None:
        self.doc = doc
        self.get_results: list[Doc | Exception] = []
        self.direct_results: list[None | Exception] = []
        self.replies: list[tuple[str, str, str, bool]] = []
        self.reply_failures = 0
        self.direct_calls: list[tuple[Doc, Span, str]] = []
        self.pin_calls: list[tuple[str, Span, list[NamedRangeInfo]]] = []
        self.unpin_calls: list[tuple[str, list[NamedRangeInfo]]] = []
        self.comments: list[dict[str, object]] = []

    def get_doc(self, document_id: str) -> Doc:
        del document_id
        if self.get_results:
            result = self.get_results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        return self.doc

    def direct_replace(self, doc: Doc, span: Span, text: str) -> None:
        self.direct_calls.append((doc, span, text))
        if self.direct_results:
            result = self.direct_results.pop(0)
            if isinstance(result, Exception):
                raise result

    def replace_pin(
        self,
        doc: Doc,
        name: str,
        span: Span,
        old: list[NamedRangeInfo],
    ) -> None:
        del doc
        self.pin_calls.append((name, span, old))

    def delete_named_ranges(
        self, doc: Doc, name: str, infos: list[NamedRangeInfo]
    ) -> None:
        del doc
        self.unpin_calls.append((name, infos))

    def reply(
        self,
        file_id: str,
        comment_id: str,
        text: str,
        *,
        resolve: bool = False,
    ) -> None:
        if self.reply_failures:
            self.reply_failures -= 1
            raise OSError("delivery failed")
        self.replies.append((file_id, comment_id, text, resolve))

    def list_comments(
        self, file_id: str, start_modified_time: str | None
    ) -> list[dict[str, object]]:
        del file_id, start_modified_time
        return self.comments

    def list_shared_docs(self) -> list[str]:
        return ["doc"]


def structural_segment(
    text: str,
    *,
    start: int = 1,
    suggested: bool = False,
    omit_start: bool = False,
) -> dict[str, object]:
    width = len(text.encode("utf-16-le")) // 2
    text_run: dict[str, object] = {"content": text}
    if suggested:
        text_run["suggestedInsertionIds"] = ["suggestion"]
    element: dict[str, object] = {"endIndex": start + width, "textRun": text_run}
    structural: dict[str, object] = {
        "endIndex": start + width,
        "paragraph": {"elements": [element]},
    }
    if not omit_start:
        element["startIndex"] = start
        structural["startIndex"] = start
    return {"content": [structural]}


def make_doc(
    text: str = "before",
    *,
    child_text: str | None = None,
    header_text: str | None = None,
    suggested: bool = False,
    revision: str = "rev-1",
    named_ranges: dict[str, list[NamedRangeInfo]] | None = None,
) -> Doc:
    document_tab: dict[str, object] = {
        "body": structural_segment(text, suggested=suggested)
    }
    if header_text is not None:
        document_tab["headers"] = {
            "header": structural_segment(header_text, start=0, omit_start=True)
        }
    root: dict[str, object] = {
        "tabProperties": {"tabId": "tab-1"},
        "documentTab": document_tab,
    }
    tab_ids = ["tab-1"]
    if child_text is not None:
        root["childTabs"] = [
            {
                "tabProperties": {"tabId": "tab-2"},
                "documentTab": {"body": structural_segment(child_text)},
            }
        ]
        tab_ids.append("tab-2")
    raw = {
        "documentId": "doc",
        "title": "Document",
        "revisionId": revision,
        "tabs": [root],
    }
    return Doc(
        document_id="doc",
        title="Document",
        revision_id=revision,
        raw=raw,
        tab_ids=tab_ids,
        named_ranges=named_ranges or {},
    )


def config(*, write_mode: str = "propose", web_base_url: str | None = None) -> Config:
    return Config.model_validate(
        {
            "write_mode": write_mode,
            "web_base_url": web_base_url,
            "default_provider": "local",
            "providers": {
                "local": {
                    "type": "openai_compat",
                    "base_url": "http://localhost:11434",
                    "model": "test-model",
                }
            },
        }
    )


def command_comment(
    content: str = "@ai improve",
    *,
    quote: str | None = "before",
    comment_id: str = "comment",
) -> dict[str, object]:
    result: dict[str, object] = {
        "id": comment_id,
        "content": content,
        "modifiedTime": "2026-08-16T10:00:00Z",
        "author": {"me": False},
        "replies": [],
        "resolved": False,
    }
    if quote is not None:
        result["quotedFileContent"] = {"value": quote}
    return result


def make_engine(
    tmp_path: Path,
    *,
    doc: Doc | None = None,
    write_mode: str = "propose",
    provider_result: str | Exception = "after",
    state: State | None = None,
) -> tuple[Engine, FakeGDocs, FakeProvider]:
    gdocs = FakeGDocs(doc or make_doc())
    provider = FakeProvider(provider_result)
    engine = Engine(
        gdocs,
        {"local": provider},
        config(write_mode=write_mode),
        state or State(),
        tmp_path / "state.json",
    )
    return engine, gdocs, provider


def proposal(status: str = "pending") -> Proposal:
    return Proposal(
        id="proposal",
        comment_id="comment",
        comment_modified_time="2026-08-16T10:00:00Z",
        tab_id="tab-1",
        document_title="Document",
        target_text="before",
        replacement="after",
        provider="local",
        model="test-model",
        created="2026-08-16T10:01:00Z",
        status=status,
    )


def state_with_proposal(status: str = "pending") -> State:
    item = proposal(status)
    return State(files={"doc": FileState(proposals={item.id: item})})


@pytest.mark.asyncio
async def test_propose_mode_creates_proposal_and_ready_reply(tmp_path: Path) -> None:
    engine, gdocs, provider = make_engine(tmp_path, provider_result="```text\nafter\n```")

    await engine.handle_comment("doc", command_comment())

    proposals = engine.list_proposals()
    assert len(proposals) == 1
    assert proposals[0][1].replacement == "after"
    assert proposals[0][1].target_text == "before"
    assert gdocs.direct_calls == []
    assert gdocs.replies[0][3] is False
    assert "proposal ready" in gdocs.replies[0][2]
    assert "Target text:\nbefore" in provider.calls[0][1]
    assert engine.state.files["doc"].seen == {"comment": "2026-08-16T10:00:00Z"}


@pytest.mark.asyncio
async def test_propose_mode_treats_empty_replacement_as_deletion(tmp_path: Path) -> None:
    engine, gdocs, _ = make_engine(tmp_path, provider_result="")

    await engine.handle_comment("doc", command_comment("@ai delete this title"))

    proposals = engine.list_proposals()
    assert len(proposals) == 1
    assert proposals[0][1].replacement == ""
    assert "proposal ready" in gdocs.replies[0][2]


@pytest.mark.asyncio
async def test_proposal_creation_persists_proposal_intent_and_reply_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, _, _ = make_engine(tmp_path)
    snapshots: list[State] = []
    save = engine.state.save

    def record(path: Path) -> None:
        snapshots.append(copy.deepcopy(engine.state))
        save(path)

    monkeypatch.setattr(engine.state, "save", record)

    await engine.handle_comment("doc", command_comment())

    first = snapshots[0].files["doc"]
    assert len(first.proposals) == 1
    assert first.inflight["comment"].stage == "replying"
    assert first.pending_replies["comment"].resolve is False


@pytest.mark.asyncio
async def test_reply_mode_returns_fenced_replacement_without_write(tmp_path: Path) -> None:
    engine, gdocs, _ = make_engine(tmp_path, write_mode="reply")

    await engine.handle_comment("doc", command_comment())

    assert gdocs.direct_calls == []
    assert gdocs.replies == [
        (
            "doc",
            "comment",
            "replacement — local/test-model\n\n```\nafter\n```",
            True,
        )
    ]


@pytest.mark.parametrize(
    ("doc", "quote", "message"),
    [
        (make_doc(), None, "select text"),
        (make_doc(), "missing", "no longer present"),
        (make_doc("before and before"), "before", "not unique"),
        (make_doc("other", child_text="before"), "before", "outside the first tab"),
        (make_doc("other", header_text="before"), "before", "outside the first tab"),
        (make_doc("before", suggested=True), "before", "pending suggestion"),
        (make_doc("before", child_text="before"), "before", "not unique"),
        (make_doc("before", header_text="before"), "before", "not unique"),
    ],
)
@pytest.mark.asyncio
async def test_unsafe_instruction_targets_reply_without_provider_call(
    tmp_path: Path, doc: Doc, quote: str | None, message: str
) -> None:
    engine, gdocs, provider = make_engine(tmp_path, doc=doc)

    await engine.handle_comment("doc", command_comment(quote=quote))

    assert provider.calls == []
    assert message in gdocs.replies[0][2]
    assert gdocs.replies[0][3] is False


@pytest.mark.asyncio
async def test_unpin_does_not_require_a_quoted_span(tmp_path: Path) -> None:
    infos = [NamedRangeInfo("range", [Span("tab-1", 1, 4)])]
    engine, gdocs, _ = make_engine(
        tmp_path, doc=make_doc(named_ranges={"context": infos})
    )

    await engine.handle_comment(
        "doc", command_comment("@ai unpin context", quote=None)
    )

    assert gdocs.unpin_calls == [("context", infos)]
    assert gdocs.replies[-1][2:] == ("unpinned context", True)


@pytest.mark.asyncio
async def test_pin_uses_resolved_span_and_resolves(tmp_path: Path) -> None:
    engine, gdocs, _ = make_engine(tmp_path)

    await engine.handle_comment("doc", command_comment("@ai pin context"))

    assert gdocs.pin_calls == [("context", Span("tab-1", 1, 7), [])]
    assert gdocs.replies[-1][2:] == ("pinned context", True)


@pytest.mark.asyncio
async def test_pin_decodes_html_entities_in_drive_quote(tmp_path: Path) -> None:
    engine, gdocs, _ = make_engine(tmp_path, doc=make_doc("acción"))

    await engine.handle_comment(
        "doc",
        command_comment("@ai pin context", quote="acci&#243;n"),
    )

    assert gdocs.pin_calls == [("context", Span("tab-1", 1, 7), [])]
    assert gdocs.replies[-1][2:] == ("pinned context", True)


@pytest.mark.asyncio
async def test_missing_pointer_replies_without_provider_call(tmp_path: Path) -> None:
    engine, gdocs, provider = make_engine(tmp_path)

    await engine.handle_comment("doc", command_comment("@ai use @[missing]"))

    assert provider.calls == []
    assert "pointer @[missing] was not found" in gdocs.replies[0][2]


@pytest.mark.asyncio
async def test_pointer_text_is_added_to_prompt(tmp_path: Path) -> None:
    infos = [NamedRangeInfo("range", [Span("tab-1", 1, 7)])]
    engine, _, provider = make_engine(
        tmp_path, doc=make_doc(named_ranges={"context": infos})
    )

    await engine.handle_comment("doc", command_comment("@ai use @[context]"))

    assert "@[context]:\nbefore" in provider.calls[0][1]


@pytest.mark.asyncio
async def test_provider_failure_gets_category_only_reply(tmp_path: Path) -> None:
    engine, gdocs, _ = make_engine(
        tmp_path, provider_result=ProviderError("secret provider response")
    )

    await engine.handle_comment("doc", command_comment())

    assert "completion provider failed" in gdocs.replies[0][2]
    assert "secret" not in gdocs.replies[0][2]


@pytest.mark.asyncio
async def test_accept_unchanged_applies_and_resolves(tmp_path: Path) -> None:
    engine, gdocs, _ = make_engine(tmp_path, state=state_with_proposal())

    status = await engine.accept_proposal("doc", "proposal")

    assert status == "applied"
    assert len(gdocs.direct_calls) == 1
    assert gdocs.replies[-1][2:] == ("applied — local/test-model", True)


@pytest.mark.asyncio
async def test_accept_waits_for_ready_reply_delivery_without_clobbering_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, gdocs, _ = make_engine(tmp_path)
    delivery_started = asyncio.Event()
    release_delivery = asyncio.Event()

    async def controlled(
        function: Callable[..., Any], *args: object, **kwargs: object
    ) -> Any:
        if function == gdocs.reply and not delivery_started.is_set():
            delivery_started.set()
            await release_delivery.wait()
        return function(*args, **kwargs)

    monkeypatch.setattr("samye.engine.asyncio.to_thread", controlled)
    creation = asyncio.create_task(engine.handle_comment("doc", command_comment()))
    await delivery_started.wait()
    proposal_id = next(iter(engine.state.files["doc"].proposals))
    accepting = asyncio.create_task(engine.accept_proposal("doc", proposal_id))
    await asyncio.sleep(0)

    assert not accepting.done()
    release_delivery.set()
    await creation
    assert await accepting == "applied"
    assert engine.state.files["doc"].pending_replies == {}
    assert [reply[3] for reply in gdocs.replies] == [False, True]


@pytest.mark.asyncio
async def test_concurrent_duplicate_accept_applies_once(tmp_path: Path) -> None:
    engine, gdocs, _ = make_engine(tmp_path, state=state_with_proposal())

    statuses = await asyncio.gather(
        engine.accept_proposal("doc", "proposal"),
        engine.accept_proposal("doc", "proposal"),
    )

    assert statuses == ["applied", "applied"]
    assert len(gdocs.direct_calls) == 1


@pytest.mark.asyncio
async def test_accept_stale_target_does_not_write(tmp_path: Path) -> None:
    engine, gdocs, _ = make_engine(
        tmp_path, doc=make_doc("changed"), state=state_with_proposal()
    )

    assert await engine.accept_proposal("doc", "proposal") == "stale"
    assert gdocs.direct_calls == []
    assert "stale" in gdocs.replies[-1][2]


@pytest.mark.asyncio
async def test_accept_read_failure_leaves_pending_and_propagates(tmp_path: Path) -> None:
    engine, gdocs, _ = make_engine(tmp_path, state=state_with_proposal())
    gdocs.get_results = [OSError("read failed")]

    with pytest.raises(OSError, match="read failed"):
        await engine.accept_proposal("doc", "proposal")

    assert engine.state.files["doc"].proposals["proposal"].status == "pending"


@pytest.mark.asyncio
async def test_accept_applying_save_failure_rolls_back_before_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, gdocs, _ = make_engine(tmp_path, state=state_with_proposal())

    def fail(path: Path) -> None:
        del path
        raise OSError("disk full")

    monkeypatch.setattr(engine.state, "save", fail)

    with pytest.raises(OSError, match="disk full"):
        await engine.accept_proposal("doc", "proposal")

    assert engine.state.files["doc"].proposals["proposal"].status == "pending"
    assert gdocs.direct_calls == []


@pytest.mark.asyncio
async def test_accept_revision_conflict_revalidates_once_then_applies(tmp_path: Path) -> None:
    engine, gdocs, _ = make_engine(tmp_path, state=state_with_proposal())
    refreshed = make_doc(revision="rev-2")
    gdocs.get_results = [gdocs.doc, refreshed]
    gdocs.direct_results = [RevisionConflict(), None]

    assert await engine.accept_proposal("doc", "proposal") == "applied"
    assert [called[0].revision_id for called in gdocs.direct_calls] == ["rev-1", "rev-2"]


@pytest.mark.asyncio
async def test_accept_second_revision_conflict_becomes_stale(tmp_path: Path) -> None:
    engine, gdocs, _ = make_engine(tmp_path, state=state_with_proposal())
    gdocs.get_results = [gdocs.doc, make_doc(revision="rev-2")]
    gdocs.direct_results = [RevisionConflict(), RevisionConflict()]

    assert await engine.accept_proposal("doc", "proposal") == "stale"
    assert len(gdocs.direct_calls) == 2


@pytest.mark.asyncio
async def test_conflict_refetch_failure_reverts_to_pending(tmp_path: Path) -> None:
    engine, gdocs, _ = make_engine(tmp_path, state=state_with_proposal())
    gdocs.get_results = [gdocs.doc, OSError("read failed")]
    gdocs.direct_results = [RevisionConflict()]

    with pytest.raises(OSError, match="read failed"):
        await engine.accept_proposal("doc", "proposal")

    assert engine.state.files["doc"].proposals["proposal"].status == "pending"


@pytest.mark.asyncio
async def test_non_conflict_write_failure_becomes_indeterminate_without_retry(
    tmp_path: Path,
) -> None:
    engine, gdocs, _ = make_engine(tmp_path, state=state_with_proposal())
    gdocs.direct_results = [TimeoutError("unknown outcome")]

    assert await engine.accept_proposal("doc", "proposal") == "indeterminate"
    assert len(gdocs.direct_calls) == 1
    assert "indeterminate" in gdocs.replies[-1][2]


@pytest.mark.asyncio
async def test_duplicate_transition_returns_current_status_without_side_effects(
    tmp_path: Path,
) -> None:
    engine, gdocs, _ = make_engine(tmp_path, state=state_with_proposal("rejected"))

    assert await engine.accept_proposal("doc", "proposal") == "rejected"
    assert await engine.reject_proposal("doc", "proposal") == "rejected"
    assert gdocs.direct_calls == []
    assert gdocs.replies == []


@pytest.mark.asyncio
async def test_reject_and_unknown_proposals(tmp_path: Path) -> None:
    engine, gdocs, _ = make_engine(tmp_path, state=state_with_proposal())

    assert await engine.reject_proposal("doc", "proposal") == "rejected"
    assert gdocs.replies[-1][2:] == ("proposal rejected", True)
    with pytest.raises(KeyError):
        await engine.accept_proposal("doc", "missing")
    with pytest.raises(KeyError):
        await engine.reject_proposal("missing-doc", "proposal")


@pytest.mark.parametrize("status", ["applied", "rejected", "stale", "indeterminate"])
@pytest.mark.asyncio
async def test_remove_terminal_proposal_persists_deletion(
    tmp_path: Path, status: str
) -> None:
    engine, _, _ = make_engine(tmp_path, state=state_with_proposal(status))

    await engine.remove_proposal("doc", "proposal")

    assert engine.state.files["doc"].proposals == {}
    assert State.load(tmp_path / "state.json").files["doc"].proposals == {}


@pytest.mark.parametrize("status", ["pending", "applying"])
@pytest.mark.asyncio
async def test_remove_refuses_nonterminal_proposal(tmp_path: Path, status: str) -> None:
    engine, _, _ = make_engine(tmp_path, state=state_with_proposal(status))

    with pytest.raises(ValueError, match="only terminal"):
        await engine.remove_proposal("doc", "proposal")

    assert "proposal" in engine.state.files["doc"].proposals


@pytest.mark.asyncio
async def test_remove_rolls_back_in_memory_when_save_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, _, _ = make_engine(tmp_path, state=state_with_proposal("applied"))

    def fail(path: Path) -> None:
        del path
        raise OSError("disk full")

    monkeypatch.setattr(engine.state, "save", fail)

    with pytest.raises(OSError, match="disk full"):
        await engine.remove_proposal("doc", "proposal")

    assert "proposal" in engine.state.files["doc"].proposals


@pytest.mark.asyncio
async def test_startup_recovers_mutation_and_applying_proposal(tmp_path: Path) -> None:
    item = proposal("applying")
    state = State(
        files={
            "doc": FileState(
                inflight={"pin-comment": Inflight("mutating", "2026-08-16T09:00:00Z")},
                proposals={item.id: item},
            )
        }
    )
    engine, gdocs, _ = make_engine(tmp_path, state=state)

    await engine._recover_startup()

    assert item.status == "indeterminate"
    assert gdocs.direct_calls == []
    assert {reply[1] for reply in gdocs.replies} == {"comment", "pin-comment"}
    assert not state.files["doc"].inflight


@pytest.mark.asyncio
async def test_pending_reply_retries_across_recovery(tmp_path: Path) -> None:
    state = State(
        files={
            "doc": FileState(
                inflight={"comment": Inflight("replying", "2026-08-16T10:00:00Z")},
                pending_replies={
                    "comment": PendingReply(
                        "waiting", False, "2026-08-16T10:00:00Z", attempts=1
                    )
                },
            )
        }
    )
    engine, gdocs, _ = make_engine(tmp_path, state=state)

    await engine._recover_startup()

    assert gdocs.replies[-1][2] == "waiting"
    assert state.files["doc"].seen["comment"] == "2026-08-16T10:00:00Z"


@pytest.mark.asyncio
async def test_reply_delivery_is_abandoned_after_three_attempts(tmp_path: Path) -> None:
    engine, gdocs, _ = make_engine(tmp_path)
    gdocs.reply_failures = 3

    await engine.handle_comment("doc", command_comment("@ai", quote=None))
    await engine._deliver_file_pending("doc")
    await engine._deliver_file_pending("doc")

    file_state = engine.state.files["doc"]
    assert file_state.pending_replies == {}
    assert file_state.inflight == {}
    assert file_state.seen["comment"] == "2026-08-16T10:00:00Z"


@pytest.mark.asyncio
async def test_polling_filters_handled_threads_and_advances_watermark(tmp_path: Path) -> None:
    engine, gdocs, provider = make_engine(tmp_path)
    gdocs.comments = [
        command_comment(comment_id="new"),
        {
            **command_comment(comment_id="answered"),
            "replies": [{"author": {"me": True}}],
        },
        {**command_comment(comment_id="resolved"), "resolved": True},
        command_comment("ordinary comment", comment_id="ordinary"),
    ]

    await engine._poll_file("doc")

    assert len(provider.calls) == 1
    assert engine.state.files["doc"].watermark is not None
    assert "new" in engine.state.files["doc"].seen


@pytest.mark.asyncio
async def test_auto_discovery_finds_documents_invited_after_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, gdocs, _ = make_engine(tmp_path)
    discoveries = iter([["doc"], ["doc", "new-doc"]])
    polled: list[str] = []
    sleep_calls = 0

    class StopPolling(Exception):
        pass

    def list_shared_docs() -> list[str]:
        return next(discoveries)

    async def poll_file(file_id: str) -> None:
        polled.append(file_id)

    async def sleep(delay: float) -> None:
        nonlocal sleep_calls
        assert delay == engine.cfg.poll_interval_s
        sleep_calls += 1
        if sleep_calls == 2:
            raise StopPolling

    monkeypatch.setattr(gdocs, "list_shared_docs", list_shared_docs)
    monkeypatch.setattr(engine, "_poll_file", poll_file)
    monkeypatch.setattr("samye.engine.asyncio.sleep", sleep)

    with pytest.raises(StopPolling):
        await engine.run_forever()

    assert polled == ["doc", "doc", "new-doc"]


def test_suggest_mode_fails_at_startup(tmp_path: Path) -> None:
    gdocs = FakeGDocs(make_doc())

    with pytest.raises(ValueError, match="requires the Preview extension"):
        Engine(
            gdocs,
            {"local": FakeProvider()},
            config(write_mode="suggest"),
            State(),
            tmp_path / "state.json",
        )


def test_google_error_summary_omits_response_body_and_url() -> None:
    response = type("Response", (), {"status": 400, "reason": "Bad Request"})()
    error = HttpError(
        response,
        b'{"error":{"message":"The fields parameter is required"},"secret":"body"}',
        uri="https://example.invalid/private-document",
    )

    summary = _safe_error_summary(error)

    assert summary == "Google HTTP 400: The fields parameter is required"
    assert "private-document" not in summary
    assert "secret" not in summary
