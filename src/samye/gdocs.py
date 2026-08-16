"""Thin wrappers around Google Docs and Drive API operations."""

from __future__ import annotations

from dataclasses import dataclass

from samye.textmap import Span


class RevisionConflict(Exception):
    """A write was rejected because its document revision was stale."""


@dataclass
class NamedRangeInfo:
    """One named-range ID and all ranges belonging to it."""

    named_range_id: str
    spans: list[Span]


@dataclass
class CommentAnchor:
    """A comment ID and its anchored ranges."""

    comment_id: str
    spans: list[Span]


@dataclass
class Doc:
    """An immutable-enough snapshot used for revision-controlled writes."""

    document_id: str
    revision_id: str
    raw: dict[str, object]
    first_tab_id: str
    named_ranges: dict[str, list[NamedRangeInfo]]
    anchors: dict[str, CommentAnchor]


class GDocs:
    """Google Docs and Drive API facade."""

    def get_doc(self, document_id: str) -> Doc:
        """Fetch and parse one document snapshot."""
        raise NotImplementedError

    def suggest_replace(self, doc: Doc, span: Span, new_text: str) -> None:
        """Suggest replacing one clean span."""
        raise NotImplementedError

    def replace_pin(self, doc: Doc, name: str, span: Span, old: list[NamedRangeInfo]) -> None:
        """Atomically replace all existing ranges for one pin name."""
        raise NotImplementedError

    def delete_named_ranges(self, doc: Doc, name: str, infos: list[NamedRangeInfo]) -> None:
        """Delete all named ranges for one pin name."""
        raise NotImplementedError

    def list_comments(
        self, file_id: str, start_modified_time: str | None
    ) -> list[dict[str, object]]:
        """List all comment pages from an optional timestamp."""
        raise NotImplementedError

    def reply(
        self,
        file_id: str,
        comment_id: str,
        text: str,
        *,
        resolve: bool = False,
    ) -> None:
        """Reply to a Drive comment and optionally resolve it."""
        raise NotImplementedError

    def list_shared_docs(self) -> list[str]:
        """List Google Docs visible to the bot."""
        raise NotImplementedError
