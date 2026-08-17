"""Tests for pin and unpin handlers."""

from collections.abc import Callable
from typing import Any
from unittest.mock import Mock

import pytest

from samye.commands import Pin, Unpin
from samye.gdocs import Doc, NamedRangeInfo
from samye.pins import PinOutcome, handle_pin, handle_unpin
from samye.textmap import Span


@pytest.fixture(autouse=True)
def run_threads_inline(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run(function: Callable[..., Any], *args: object, **kwargs: object) -> Any:
        return function(*args, **kwargs)

    monkeypatch.setattr("samye.pins.asyncio.to_thread", run)


def snapshot(named_ranges: dict[str, list[NamedRangeInfo]] | None = None) -> Doc:
    return Doc(
        document_id="doc",
        title="Document",
        revision_id="revision",
        raw={},
        tab_ids=["tab"],
        named_ranges=named_ranges or {},
    )


@pytest.mark.asyncio
async def test_fresh_pin() -> None:
    gdocs = Mock()
    doc = snapshot()
    span = Span("tab", 10, 20)

    outcome = await handle_pin(gdocs, doc, span, Pin("context"))

    gdocs.replace_pin.assert_called_once_with(doc, "context", span, [])
    assert outcome == PinOutcome("pinned @[context]", True)


@pytest.mark.asyncio
async def test_overwrite_pin() -> None:
    gdocs = Mock()
    old = [NamedRangeInfo("old", [Span("tab", 1, 2)])]
    doc = snapshot({"context": old})
    span = Span("tab", 10, 20)

    outcome = await handle_pin(gdocs, doc, span, Pin("context"))

    gdocs.replace_pin.assert_called_once_with(doc, "context", span, old)
    assert outcome == PinOutcome("updated @[context]", True)


@pytest.mark.asyncio
async def test_unpin() -> None:
    gdocs = Mock()
    infos = [NamedRangeInfo("old", [Span("tab", 1, 2)])]
    doc = snapshot({"context": infos})

    outcome = await handle_unpin(gdocs, doc, Unpin("context"))

    gdocs.delete_named_ranges.assert_called_once_with(doc, "context", infos)
    assert outcome == PinOutcome("unpinned @[context]", True)


@pytest.mark.asyncio
async def test_unpin_nonexistent() -> None:
    gdocs = Mock()

    outcome = await handle_unpin(gdocs, snapshot(), Unpin("missing"))

    gdocs.delete_named_ranges.assert_not_called()
    assert outcome == PinOutcome("no pin named @[missing] exists", True)
