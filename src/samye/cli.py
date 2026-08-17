"""Command-line entry points for samye."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
from collections.abc import Sequence
from pathlib import Path

import uvicorn

from samye.auth import get_credentials
from samye.config import Config, load_config
from samye.engine import Engine
from samye.gdocs import GDocs
from samye.providers.base import make_provider
from samye.state import State
from samye.web.app import create_app

SCOPES = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive",
]
STATE_PATH = Path("~/.local/state/samye/state.json")


def build_parser() -> argparse.ArgumentParser:
    """Build the samye command-line parser."""
    parser = argparse.ArgumentParser(prog="samye")
    parser.add_argument("--config", help="path to the TOML configuration file")
    parser.add_argument("-v", "--verbose", action="store_true", help="enable debug logging")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("run", help="run the comment polling daemon")
    subparsers.add_parser("auth", help="authorize the Google bot account")
    subparsers.add_parser("docs", help="list Google Docs visible to the bot")
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    """Parse command-line arguments and dispatch to a command."""
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = load_config(Path(args.config) if args.config else None)
    credentials = get_credentials(cfg, SCOPES)
    if args.command == "auth":
        print("authorization complete")
        return

    gdocs = GDocs(credentials)
    if args.command == "docs":
        for document_id in gdocs.list_shared_docs():
            print(document_id)
        return

    providers = {
        name: make_provider(name, provider_cfg)
        for name, provider_cfg in cfg.providers.items()
    }
    state_path = STATE_PATH.expanduser()
    engine = Engine(gdocs, providers, cfg, State.load(state_path), state_path)
    asyncio.run(_serve(engine, cfg))


async def _serve(engine: Engine, cfg: Config) -> None:
    """Run the poller and local review server as one process."""
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(engine),
            host=cfg.web_bind_host,
            port=cfg.web_port,
            log_level="debug" if logging.getLogger().isEnabledFor(logging.DEBUG) else "info",
        )
    )
    poller = asyncio.create_task(engine.run_forever(), name="samye-poller")
    web = asyncio.create_task(server.serve(), name="samye-web")
    try:
        done, _ = await asyncio.wait({poller, web}, return_when=asyncio.FIRST_COMPLETED)
        if poller in done:
            server.should_exit = True
            await web
            poller.result()
        web.result()
    finally:
        if not poller.done():
            poller.cancel()
        if not web.done():
            server.should_exit = True
            web.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await poller
        with contextlib.suppress(asyncio.CancelledError):
            await web
