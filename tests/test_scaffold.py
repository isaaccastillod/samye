"""Scaffold smoke tests."""

from samye.cli import build_parser


def test_cli_exposes_all_commands() -> None:
    parser = build_parser()

    help_text = parser.format_help()
    for command in ("run", "auth", "docs", "web"):
        assert command in help_text
