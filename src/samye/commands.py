"""Pure parsing for comment commands."""

from __future__ import annotations

import re
from dataclasses import dataclass

TRIGGER = re.compile(r"^@ai(?=$|\s)", re.IGNORECASE)
NAME = re.compile(r"^[a-z0-9-]{1,32}$")
REFERENCE = re.compile(r"@\[([a-z0-9-]{1,32})\]")
USAGE = (
    "usage: @ai <instruction>, @ai pin <name>, or @ai unpin <name>; "
    "names use 1-32 lowercase letters, numbers, or hyphens"
)


@dataclass
class Pin:
    """Create or replace a named pointer."""

    name: str


@dataclass
class Unpin:
    """Delete every named pointer with a given name."""

    name: str


@dataclass
class Instruct:
    """Request a suggested replacement from a provider."""

    instruction: str
    refs: list[str]


@dataclass
class ParseError:
    """A recognized but malformed samye command."""

    message: str


def parse(comment_text: str) -> Pin | Unpin | Instruct | ParseError | None:
    """Parse one comment into a samye command."""
    trigger = TRIGGER.match(comment_text)
    if trigger is None:
        return None

    content = comment_text[trigger.end() :].strip()
    if not content:
        return ParseError(USAGE)

    parts = content.split()
    command = parts[0]
    if command not in {"pin", "unpin"}:
        return Instruct(instruction=content, refs=REFERENCE.findall(content))

    if len(parts) != 2 or NAME.fullmatch(parts[1]) is None:
        return ParseError(USAGE)
    if command == "pin":
        return Pin(parts[1])
    return Unpin(parts[1])
