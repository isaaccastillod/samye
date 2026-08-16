"""Mapping between Python text offsets and Google Docs UTF-16 indexes."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Span:
    """A UTF-16 range in one document tab."""

    tab_id: str
    start: int
    end: int


class TextMap:
    """Flat tab text with mappings back to Google Docs indexes."""

    text: str

    def to_utf16_span(self, py_start: int, py_end: int) -> Span:
        """Convert a Python string slice to a Docs range."""
        raise NotImplementedError

    def to_py_span(self, span: Span) -> tuple[int, int]:
        """Convert a Docs range to Python string offsets."""
        raise NotImplementedError

    def text_for_span(self, span: Span) -> str:
        """Return the text covered by a Docs range."""
        raise NotImplementedError

    def find(self, needle: str, *, near: Span | None = None) -> Span | None:
        """Find an exact, structurally continuous occurrence."""
        raise NotImplementedError

    def is_clean_span(self, span: Span) -> bool:
        """Return whether Docs can delete the range without structural damage."""
        raise NotImplementedError


def build_text_map(tab: dict[str, object], tab_id: str) -> TextMap:
    """Build a text map from a DocumentTab body."""
    raise NotImplementedError
