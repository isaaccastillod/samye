"""Pure parsing for comment commands."""

from __future__ import annotations

from dataclasses import dataclass


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
    raise NotImplementedError
