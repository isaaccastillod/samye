"""Tests for the Google Docs and Drive API facade."""

import copy
import json
from pathlib import Path
from unittest.mock import Mock, call

import pytest
from googleapiclient.errors import HttpError

from samye.gdocs import Doc, GDocs, NamedRangeInfo, RevisionConflict
from samye.textmap import Span

FIXTURE = Path(__file__).parent / "fixtures" / "t0a_document.json"


def fixture_document() -> dict[str, object]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def facade() -> tuple[GDocs, Mock, Mock, Mock, Mock]:
    documents = Mock()
    comments = Mock()
    replies = Mock()
    files = Mock()
    docs_service = Mock()
    drive_service = Mock()
    docs_service.documents.return_value = documents
    drive_service.comments.return_value = comments
    drive_service.replies.return_value = replies
    drive_service.files.return_value = files
    return (
        GDocs(docs_service=docs_service, drive_service=drive_service),
        documents,
        comments,
        replies,
        files,
    )


def snapshot(**changes: object) -> Doc:
    values = {
        "document_id": "doc-1",
        "title": "Document",
        "revision_id": "rev-1",
        "raw": {},
        "tab_ids": ["tab-1"],
        "named_ranges": {},
    }
    values.update(changes)
    return Doc(**values)


def test_get_doc_parses_capture_tabs_and_named_ranges_losslessly() -> None:
    gdocs, documents, _, _, _ = facade()
    documents.get.return_value.execute.return_value = fixture_document()

    doc = gdocs.get_doc("document-1")

    documents.get.assert_called_once_with(
        documentId="document-1",
        includeTabsContent=True,
        suggestionsViewMode="SUGGESTIONS_INLINE",
    )
    assert doc.title == "Fixture Document"
    assert doc.tab_ids == ["tab-1", "tab-2"]
    assert doc.named_ranges == {
        "t0a-body": [NamedRangeInfo("named-range-1", [Span("tab-1", 9618, 9630)])],
        "t0a-header": [
            NamedRangeInfo("named-range-2", [Span("tab-1", 0, 12, "header-1")])
        ],
    }


def test_get_doc_keeps_multiple_named_ranges_and_discontiguous_spans() -> None:
    raw = copy.deepcopy(fixture_document())
    named = raw["tabs"][0]["documentTab"]["namedRanges"]
    named["smy:multi"] = {
        "namedRanges": [
            {
                "namedRangeId": "range-a",
                "ranges": [
                    {"startIndex": 10, "endIndex": 20},
                    {"startIndex": 30, "endIndex": 40, "segmentId": "header-1"},
                ],
            },
            {
                "namedRangeId": "range-b",
                "ranges": [{"startIndex": 50, "endIndex": 60, "tabId": "tab-1"}],
            },
        ]
    }
    gdocs, documents, _, _, _ = facade()
    documents.get.return_value.execute.return_value = raw

    infos = gdocs.get_doc("document-1").named_ranges["multi"]

    assert infos == [
        NamedRangeInfo(
            "range-a",
            [Span("tab-1", 10, 20), Span("tab-1", 30, 40, "header-1")],
        ),
        NamedRangeInfo("range-b", [Span("tab-1", 50, 60)]),
    ]


def test_direct_replace_sends_segment_aware_revision_controlled_batch() -> None:
    gdocs, documents, _, _, _ = facade()
    span = Span("tab-1", 4, 9, "header-1")

    gdocs.direct_replace(snapshot(), span, "replacement")

    documents.batchUpdate.assert_called_once_with(
        documentId="doc-1",
        body={
            "requests": [
                {
                    "deleteContentRange": {
                        "range": {
                            "startIndex": 4,
                            "endIndex": 9,
                            "tabId": "tab-1",
                            "segmentId": "header-1",
                        }
                    }
                },
                {
                    "insertText": {
                        "location": {
                            "index": 4,
                            "tabId": "tab-1",
                            "segmentId": "header-1",
                        },
                        "text": "replacement",
                    }
                },
            ],
            "writeControl": {"requiredRevisionId": "rev-1"},
        },
    )
    documents.batchUpdate.return_value.execute.assert_called_once()


def test_revision_mismatch_is_typed() -> None:
    gdocs, documents, _, _, _ = facade()
    response = Mock(status=409, reason="Conflict")
    documents.batchUpdate.return_value.execute.side_effect = HttpError(response, b"conflict")

    with pytest.raises(RevisionConflict):
        gdocs.direct_replace(snapshot(), Span("tab-1", 1, 2), "x")


def test_replace_pin_is_one_batch_with_tab_scoped_deletes() -> None:
    gdocs, documents, _, _, _ = facade()
    old = [
        NamedRangeInfo("old-1", [Span("tab-2", 2, 4)]),
        NamedRangeInfo("old-2", [Span("tab-1", 7, 9, "header-1")]),
    ]

    gdocs.replace_pin(snapshot(), "context", Span("tab-1", 10, 20), old)

    body = documents.batchUpdate.call_args.kwargs["body"]
    assert body["requests"] == [
        {
            "deleteNamedRange": {
                "namedRangeId": "old-1",
                "tabsCriteria": {"tabIds": ["tab-2"]},
            }
        },
        {
            "deleteNamedRange": {
                "namedRangeId": "old-2",
                "tabsCriteria": {"tabIds": ["tab-1"]},
            }
        },
        {
            "createNamedRange": {
                "name": "smy:context",
                "range": {"startIndex": 10, "endIndex": 20, "tabId": "tab-1"},
            }
        },
    ]
    assert documents.batchUpdate.call_count == 1


def test_delete_named_ranges_is_a_noop_when_name_is_absent() -> None:
    gdocs, documents, _, _, _ = facade()

    gdocs.delete_named_ranges(snapshot(), "missing", [])

    documents.batchUpdate.assert_not_called()


def test_list_comments_paginates_and_filters_bot_authored_comments() -> None:
    gdocs, _, comments, _, _ = facade()
    first = {
        "comments": [
            {"id": "user-1", "author": {"me": False}},
            {"id": "bot", "author": {"me": True}},
        ],
        "nextPageToken": "next",
    }
    second = {"comments": [{"id": "user-2", "author": {"me": False}}]}
    comments.list.return_value.execute.side_effect = [first, second]

    result = gdocs.list_comments("doc-1", "2026-01-01T00:00:00Z")

    assert [comment["id"] for comment in result] == ["user-1", "user-2"]
    assert comments.list.call_args_list == [
        call(
            fileId="doc-1",
            fields=comments.list.call_args_list[0].kwargs["fields"],
            includeDeleted=False,
            pageSize=100,
            startModifiedTime="2026-01-01T00:00:00Z",
        ),
        call(
            fileId="doc-1",
            fields=comments.list.call_args_list[1].kwargs["fields"],
            includeDeleted=False,
            pageSize=100,
            startModifiedTime="2026-01-01T00:00:00Z",
            pageToken="next",
        ),
    ]


def test_reply_resolves_only_when_requested() -> None:
    gdocs, _, _, replies, _ = facade()

    gdocs.reply("doc-1", "comment-1", "done", resolve=True)
    gdocs.reply("doc-1", "comment-2", "waiting")

    assert replies.create.call_args_list == [
        call(
            fileId="doc-1",
            commentId="comment-1",
            body={"content": "done", "action": "resolve"},
        ),
        call(fileId="doc-1", commentId="comment-2", body={"content": "waiting"}),
    ]


def test_list_shared_docs_paginates() -> None:
    gdocs, _, _, _, files = facade()
    files.list.return_value.execute.side_effect = [
        {"files": [{"id": "doc-1"}], "nextPageToken": "next"},
        {"files": [{"id": "doc-2"}]},
    ]

    assert gdocs.list_shared_docs() == ["doc-1", "doc-2"]
    assert files.list.call_count == 2
    assert files.list.call_args_list[1].kwargs["pageToken"] == "next"
