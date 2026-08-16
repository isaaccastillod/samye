"""Comment orchestration and daemon polling loop."""

from __future__ import annotations

from samye.config import Config
from samye.gdocs import GDocs
from samye.providers.base import Provider
from samye.state import State


class Engine:
    """Coordinate polling, providers, suggestions, replies, and state."""

    def __init__(
        self,
        gdocs: GDocs,
        providers: dict[str, Provider],
        cfg: Config,
        state: State,
    ) -> None:
        self.gdocs = gdocs
        self.providers = providers
        self.cfg = cfg
        self.state = state

    async def handle_comment(self, file_id: str, comment: dict[str, object]) -> None:
        """Handle one comment without leaking an exception to the poll loop."""
        raise NotImplementedError

    async def run_forever(self) -> None:
        """Poll configured or discovered documents indefinitely."""
        raise NotImplementedError
