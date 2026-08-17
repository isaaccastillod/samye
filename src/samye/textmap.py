"""Mapping between Python text offsets and Google Docs UTF-16 indexes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Span:
    """A UTF-16 range in one document tab and segment."""

    tab_id: str
    start: int
    end: int
    segment_id: str | None = None


@dataclass(frozen=True)
class _MappedChar:
    start: int
    end: int
    region: int
    dirty: bool
    protected: bool = False


class TextMap:
    """Flat segment text with mappings back to Google Docs indexes."""

    def __init__(
        self,
        text: str,
        mapped: list[_MappedChar],
        tab_id: str,
        segment_id: str | None,
    ) -> None:
        if len(text) != len(mapped):
            raise ValueError("text and index map lengths differ")
        self.text = text
        self._mapped = mapped
        self._tab_id = tab_id
        self._segment_id = segment_id

    def _location_at(self, py_offset: int) -> int:
        if not 0 <= py_offset <= len(self.text):
            raise ValueError("Python offset is outside the mapped text")
        if py_offset < len(self._mapped):
            return self._mapped[py_offset].start
        if self._mapped:
            return self._mapped[-1].end
        return 0

    def _is_continuous(self, py_start: int, py_end: int) -> bool:
        if py_start == py_end:
            return True
        selected = self._mapped[py_start:py_end]
        region = selected[0].region
        return all(
            current.region == region and previous.end == current.start
            for previous, current in zip(selected, selected[1:], strict=False)
        )

    def to_utf16_span(self, py_start: int, py_end: int) -> Span:
        """Convert a Python string slice to a structurally continuous Docs range."""
        if not 0 <= py_start <= py_end <= len(self.text):
            raise ValueError("invalid Python string range")
        if not self._is_continuous(py_start, py_end):
            raise ValueError("range crosses a structural boundary")
        if py_start == py_end:
            start = end = self._location_at(py_start)
        else:
            start = self._mapped[py_start].start
            end = self._mapped[py_end - 1].end
        return Span(
            tab_id=self._tab_id,
            start=start,
            end=end,
            segment_id=self._segment_id,
        )

    def to_py_span(self, span: Span) -> tuple[int, int]:
        """Convert a structurally continuous Docs range to Python offsets."""
        if span.tab_id != self._tab_id or span.segment_id != self._segment_id:
            raise ValueError("span belongs to a different tab or segment")
        if span.start > span.end:
            raise ValueError("span start exceeds its end")

        if span.start == span.end:
            for offset in range(len(self.text) + 1):
                if self._location_at(offset) == span.start:
                    return offset, offset
            raise ValueError("span is outside the mapped text")

        py_start = next(
            (index for index, mapped in enumerate(self._mapped) if mapped.start == span.start),
            None,
        )
        py_end = next(
            (index + 1 for index, mapped in enumerate(self._mapped) if mapped.end == span.end),
            None,
        )
        if py_start is None or py_end is None or py_start >= py_end:
            raise ValueError("span does not align with mapped character boundaries")
        if not self._is_continuous(py_start, py_end):
            raise ValueError("span crosses a structural boundary")
        return py_start, py_end

    def text_for_span(self, span: Span) -> str:
        """Return the text covered by a Docs range."""
        py_start, py_end = self.to_py_span(span)
        return self.text[py_start:py_end]

    def find_all(self, needle: str) -> list[Span]:
        """Find every exact, structurally continuous occurrence."""
        if not needle:
            return []
        matches: list[Span] = []
        offset = 0
        while (found := self.text.find(needle, offset)) != -1:
            end = found + len(needle)
            if self._is_continuous(found, end):
                matches.append(self.to_utf16_span(found, end))
            offset = found + 1
        return matches

    def is_clean_span(self, span: Span) -> bool:
        """Return whether Docs can safely delete the range."""
        try:
            py_start, py_end = self.to_py_span(span)
        except ValueError:
            return False
        return all(
            not mapped.dirty and not mapped.protected
            for mapped in self._mapped[py_start:py_end]
        )


class _Builder:
    def __init__(self) -> None:
        self.parts: list[str] = []
        self.mapped: list[_MappedChar] = []
        self.next_region = 1

    def add_text(self, run: dict[str, Any], start: int, region: int) -> None:
        text_run = run.get("textRun")
        if not isinstance(text_run, dict):
            return
        content = text_run.get("content")
        if not isinstance(content, str):
            return
        dirty = bool(
            text_run.get("suggestedInsertionIds") or text_run.get("suggestedDeletionIds")
        )
        index = start
        for character in content:
            width = len(character.encode("utf-16-le")) // 2
            self.mapped.append(
                _MappedChar(start=index, end=index + width, region=region, dirty=dirty)
            )
            index += width
        self.parts.append(content)

    def walk_content(self, content: object, region: int) -> None:
        if not isinstance(content, list):
            return
        cursor = 0
        for structural in content:
            if not isinstance(structural, dict):
                continue
            start = _index(structural.get("startIndex"), cursor)
            end = _index(structural.get("endIndex"), start)
            paragraph = structural.get("paragraph")
            table = structural.get("table")
            table_of_contents = structural.get("tableOfContents")
            if isinstance(paragraph, dict):
                self.walk_paragraph(paragraph, start, region)
            elif isinstance(table, dict):
                self.walk_table(table)
            elif isinstance(table_of_contents, dict):
                toc_region = self.allocate_region()
                self.walk_content(table_of_contents.get("content"), toc_region)
            cursor = end

    def walk_paragraph(self, paragraph: dict[str, Any], start: int, region: int) -> None:
        elements = paragraph.get("elements")
        if not isinstance(elements, list):
            return
        cursor = start
        for element in elements:
            if not isinstance(element, dict):
                continue
            element_start = _index(element.get("startIndex"), cursor)
            element_end = _index(element.get("endIndex"), element_start)
            self.add_text(element, element_start, region)
            cursor = element_end

    def walk_table(self, table: dict[str, Any]) -> None:
        rows = table.get("tableRows")
        if not isinstance(rows, list):
            return
        for row in rows:
            if not isinstance(row, dict):
                continue
            cells = row.get("tableCells")
            if not isinstance(cells, list):
                continue
            for cell in cells:
                if not isinstance(cell, dict):
                    continue
                region = self.allocate_region()
                before = len(self.mapped)
                self.walk_content(cell.get("content"), region)
                if len(self.mapped) > before and self.parts and self.parts[-1].endswith("\n"):
                    last = self.mapped[-1]
                    self.mapped[-1] = _MappedChar(
                        start=last.start,
                        end=last.end,
                        region=last.region,
                        dirty=last.dirty,
                        protected=True,
                    )

    def allocate_region(self) -> int:
        region = self.next_region
        self.next_region += 1
        return region


def _index(value: object, fallback: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else fallback


def build_text_map(
    segment: dict[str, object],
    tab_id: str,
    segment_id: str | None = None,
) -> TextMap:
    """Build a text map from any Docs structural-element container."""
    builder = _Builder()
    builder.walk_content(segment.get("content"), region=0)
    return TextMap("".join(builder.parts), builder.mapped, tab_id, segment_id)
