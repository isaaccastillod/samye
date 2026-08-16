"""Pin and unpin command handlers."""

from __future__ import annotations

from samye.commands import Pin, Unpin
from samye.gdocs import Doc, GDocs


async def handle_pin(
    gdocs: GDocs, doc: Doc, file_id: str, comment: dict[str, object], cmd: Pin
) -> None:
    """Handle a pin command and its terminal reply."""
    raise NotImplementedError


async def handle_unpin(
    gdocs: GDocs, doc: Doc, file_id: str, comment: dict[str, object], cmd: Unpin
) -> None:
    """Handle an unpin command and its terminal reply."""
    raise NotImplementedError
