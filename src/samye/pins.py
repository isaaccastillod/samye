"""Pin and unpin command handlers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from samye.commands import Pin, Unpin
from samye.gdocs import Doc, GDocs
from samye.textmap import Span


@dataclass
class PinOutcome:
    """The terminal reply produced by a pin mutation."""

    reply: str
    resolve: bool


async def handle_pin(
    gdocs: GDocs,
    doc: Doc,
    span: Span,
    cmd: Pin,
) -> PinOutcome:
    """Create or atomically replace a pin over an engine-resolved span."""
    old = doc.named_ranges.get(cmd.name, [])
    await asyncio.to_thread(gdocs.replace_pin, doc, cmd.name, span, old)
    verb = "updated" if old else "pinned"
    return PinOutcome(reply=f"{verb} @[{cmd.name}]", resolve=True)


async def handle_unpin(gdocs: GDocs, doc: Doc, cmd: Unpin) -> PinOutcome:
    """Delete every named range belonging to a pin."""
    infos = doc.named_ranges.get(cmd.name, [])
    if not infos:
        return PinOutcome(reply=f"no pin named @[{cmd.name}] exists", resolve=True)
    await asyncio.to_thread(gdocs.delete_named_ranges, doc, cmd.name, infos)
    return PinOutcome(reply=f"unpinned @[{cmd.name}]", resolve=True)
