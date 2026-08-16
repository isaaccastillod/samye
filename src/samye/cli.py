"""Command-line entry points for samye."""

from __future__ import annotations

import argparse
from collections.abc import Sequence


def build_parser() -> argparse.ArgumentParser:
    """Build the samye command-line parser."""
    parser = argparse.ArgumentParser(prog="samye")
    parser.add_argument("--config", help="path to the TOML configuration file")
    parser.add_argument("-v", "--verbose", action="store_true", help="enable debug logging")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("run", help="run the comment polling daemon")
    subparsers.add_parser("auth", help="authorize the Google bot account")
    subparsers.add_parser("docs", help="list Google Docs visible to the bot")
    web = subparsers.add_parser("web", help="serve the read-only suggestion viewer")
    web.add_argument("--port", type=int, default=8321)
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    """Parse command-line arguments and dispatch to a command."""
    args = build_parser().parse_args(argv)
    raise NotImplementedError(f"the {args.command!r} command is not implemented")
