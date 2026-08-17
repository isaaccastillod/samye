"""Thin wrappers around Google Docs and Drive API operations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from google.auth.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from samye.textmap import Span

PIN_PREFIX = "smy:"
COMMENT_FIELDS = (
    "nextPageToken,comments(id,content,createdTime,modifiedTime,resolved,deleted,"
    "author(displayName,emailAddress,me),quotedFileContent(mimeType,value),"
    "replies(id,content,createdTime,modifiedTime,deleted,"
    "author(displayName,emailAddress,me)))"
)


class RevisionConflict(Exception):
    """A write was rejected because its document revision was stale."""


@dataclass
class NamedRangeInfo:
    """One named-range ID and all ranges belonging to it."""

    named_range_id: str
    spans: list[Span]


@dataclass
class Doc:
    """An immutable-enough snapshot used for revision-controlled writes."""

    document_id: str
    title: str
    revision_id: str
    raw: dict[str, object]
    tab_ids: list[str]
    named_ranges: dict[str, list[NamedRangeInfo]]


class GDocs:
    """Google Docs and Drive API facade."""

    def __init__(
        self,
        credentials: Credentials | None = None,
        *,
        docs_service: Any | None = None,
        drive_service: Any | None = None,
    ) -> None:
        if docs_service is None:
            if credentials is None:
                raise ValueError("credentials are required when Docs service is not injected")
            docs_service = build("docs", "v1", credentials=credentials, cache_discovery=False)
        if drive_service is None:
            if credentials is None:
                raise ValueError("credentials are required when Drive service is not injected")
            drive_service = build("drive", "v3", credentials=credentials, cache_discovery=False)
        self._documents = docs_service.documents()
        self._comments = drive_service.comments()
        self._replies = drive_service.replies()
        self._files = drive_service.files()

    def get_doc(self, document_id: str) -> Doc:
        """Fetch and parse one tabs-aware document snapshot."""
        raw = self._documents.get(
            documentId=document_id,
            includeTabsContent=True,
            suggestionsViewMode="SUGGESTIONS_INLINE",
        ).execute()
        if not isinstance(raw, dict):
            raise ValueError("Docs API returned a non-object document")
        return _parse_doc(raw)

    def direct_replace(self, doc: Doc, span: Span, new_text: str) -> None:
        """Replace one span using an optimistic revision precondition."""
        requests: list[dict[str, object]] = [
            {"deleteContentRange": {"range": _range(span)}}
        ]
        if new_text:
            requests.append(
                {
                    "insertText": {
                        "location": _location(span),
                        "text": new_text,
                    }
                }
            )
        self._batch_update(doc, requests)

    def replace_pin(self, doc: Doc, name: str, span: Span, old: list[NamedRangeInfo]) -> None:
        """Atomically replace all existing ranges for one pin name."""
        requests = [_delete_named_range(info) for info in old]
        requests.append(
            {
                "createNamedRange": {
                    "name": f"{PIN_PREFIX}{name}",
                    "range": _range(span),
                }
            }
        )
        self._batch_update(doc, requests)

    def delete_named_ranges(self, doc: Doc, name: str, infos: list[NamedRangeInfo]) -> None:
        """Delete all named ranges for one pin name."""
        del name
        if infos:
            self._batch_update(doc, [_delete_named_range(info) for info in infos])

    def list_comments(
        self, file_id: str, start_modified_time: str | None
    ) -> list[dict[str, object]]:
        """List every page of non-bot-authored comments from an optional timestamp."""
        comments: list[dict[str, object]] = []
        page_token: str | None = None
        while True:
            arguments: dict[str, object] = {
                "fileId": file_id,
                "fields": COMMENT_FIELDS,
                "includeDeleted": False,
                "pageSize": 100,
            }
            if start_modified_time is not None:
                arguments["startModifiedTime"] = start_modified_time
            if page_token is not None:
                arguments["pageToken"] = page_token
            response = self._comments.list(**arguments).execute()
            for comment in response.get("comments", []):
                if isinstance(comment, dict) and not _is_me(comment.get("author")):
                    comments.append(comment)
            token = response.get("nextPageToken")
            if not isinstance(token, str) or not token:
                return comments
            page_token = token

    def reply(
        self,
        file_id: str,
        comment_id: str,
        text: str,
        *,
        resolve: bool = False,
    ) -> None:
        """Reply to a Drive comment and optionally resolve it."""
        body: dict[str, object] = {"content": text}
        if resolve:
            body["action"] = "resolve"
        self._replies.create(fileId=file_id, commentId=comment_id, body=body).execute()

    def list_shared_docs(self) -> list[str]:
        """List every visible, non-trashed Google Doc."""
        document_ids: list[str] = []
        page_token: str | None = None
        while True:
            arguments: dict[str, object] = {
                "q": "mimeType = 'application/vnd.google-apps.document' and trashed = false",
                "spaces": "drive",
                "pageSize": 1000,
                "fields": "nextPageToken,files(id)",
            }
            if page_token is not None:
                arguments["pageToken"] = page_token
            response = self._files.list(**arguments).execute()
            document_ids.extend(
                file["id"]
                for file in response.get("files", [])
                if isinstance(file, dict) and isinstance(file.get("id"), str)
            )
            token = response.get("nextPageToken")
            if not isinstance(token, str) or not token:
                return document_ids
            page_token = token

    def _batch_update(self, doc: Doc, requests: list[dict[str, object]]) -> None:
        try:
            self._documents.batchUpdate(
                documentId=doc.document_id,
                body={
                    "requests": requests,
                    "writeControl": {"requiredRevisionId": doc.revision_id},
                },
            ).execute()
        except HttpError as exc:
            if _is_revision_conflict(exc):
                raise RevisionConflict("document revision changed") from exc
            raise


def _parse_doc(raw: dict[str, object]) -> Doc:
    document_id = raw.get("documentId")
    title = raw.get("title")
    revision_id = raw.get("revisionId")
    tabs = raw.get("tabs")
    if not isinstance(document_id, str) or not isinstance(title, str):
        raise ValueError("document is missing its ID or title")
    if not isinstance(revision_id, str):
        raise ValueError("document is missing its revision ID")
    if not isinstance(tabs, list):
        raise ValueError("document is missing tabs-aware content")

    tab_ids: list[str] = []
    named_ranges: dict[str, list[NamedRangeInfo]] = {}
    for tab in _flatten_tabs(tabs):
        properties = tab.get("tabProperties")
        document_tab = tab.get("documentTab")
        if not isinstance(properties, dict) or not isinstance(document_tab, dict):
            continue
        tab_id = properties.get("tabId")
        if not isinstance(tab_id, str):
            continue
        tab_ids.append(tab_id)
        _parse_named_ranges(document_tab.get("namedRanges"), tab_id, named_ranges)
    if not tab_ids:
        raise ValueError("document has no readable tabs")
    return Doc(
        document_id=document_id,
        title=title,
        revision_id=revision_id,
        raw=raw,
        tab_ids=tab_ids,
        named_ranges=named_ranges,
    )


def _flatten_tabs(tabs: list[object]) -> list[dict[str, Any]]:
    flattened: list[dict[str, Any]] = []
    for tab in tabs:
        if not isinstance(tab, dict):
            continue
        flattened.append(tab)
        children = tab.get("childTabs")
        if isinstance(children, list):
            flattened.extend(_flatten_tabs(children))
    return flattened


def _parse_named_ranges(
    raw_named_ranges: object,
    tab_id: str,
    destination: dict[str, list[NamedRangeInfo]],
) -> None:
    if not isinstance(raw_named_ranges, dict):
        return
    for full_name, collection in raw_named_ranges.items():
        if not isinstance(full_name, str) or not full_name.startswith(PIN_PREFIX):
            continue
        if not isinstance(collection, dict):
            continue
        name = full_name.removeprefix(PIN_PREFIX)
        for named_range in collection.get("namedRanges", []):
            if not isinstance(named_range, dict):
                continue
            named_range_id = named_range.get("namedRangeId")
            if not isinstance(named_range_id, str):
                continue
            spans = [
                parsed
                for raw_range in named_range.get("ranges", [])
                if (parsed := _parse_range(raw_range, tab_id)) is not None
            ]
            destination.setdefault(name, []).append(NamedRangeInfo(named_range_id, spans))


def _parse_range(raw_range: object, enclosing_tab_id: str) -> Span | None:
    if not isinstance(raw_range, dict):
        return None
    start = raw_range.get("startIndex")
    end = raw_range.get("endIndex")
    tab_id = raw_range.get("tabId", enclosing_tab_id)
    segment_id = raw_range.get("segmentId")
    if not isinstance(start, int) or isinstance(start, bool):
        return None
    if not isinstance(end, int) or isinstance(end, bool) or not isinstance(tab_id, str):
        return None
    if segment_id is not None and not isinstance(segment_id, str):
        return None
    return Span(tab_id, start, end, segment_id)


def _range(span: Span) -> dict[str, object]:
    result: dict[str, object] = {
        "startIndex": span.start,
        "endIndex": span.end,
        "tabId": span.tab_id,
    }
    if span.segment_id is not None:
        result["segmentId"] = span.segment_id
    return result


def _location(span: Span) -> dict[str, object]:
    result: dict[str, object] = {"index": span.start, "tabId": span.tab_id}
    if span.segment_id is not None:
        result["segmentId"] = span.segment_id
    return result


def _delete_named_range(info: NamedRangeInfo) -> dict[str, object]:
    tab_ids = sorted({span.tab_id for span in info.spans})
    if not tab_ids:
        raise ValueError("cannot delete a named range without enclosing tab context")
    return {
        "deleteNamedRange": {
            "namedRangeId": info.named_range_id,
            "tabsCriteria": {"tabIds": tab_ids},
        }
    }


def _is_me(author: object) -> bool:
    return isinstance(author, dict) and author.get("me") is True


def _is_revision_conflict(error: HttpError) -> bool:
    status = getattr(error.resp, "status", None)
    if status in {409, 412}:
        return True
    content = error.content.lower() if isinstance(error.content, bytes) else b""
    return status == 400 and b"revision" in content
