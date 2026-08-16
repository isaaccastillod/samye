"""Common completion provider protocol and factory."""

from __future__ import annotations

from typing import Protocol

from samye.config import ProviderCfg


class ProviderError(Exception):
    """A provider could not return a usable completion."""


class Provider(Protocol):
    """A named asynchronous text completion provider."""

    name: str

    async def complete(self, system: str, user: str) -> str:
        """Return replacement text or raise ProviderError."""
        ...


def make_provider(name: str, cfg: ProviderCfg) -> Provider:
    """Construct the configured provider adapter."""
    raise NotImplementedError
