"""Tests for the command-line parser."""

import pytest

from samye.cli import build_parser


@pytest.mark.parametrize("command", ["run", "auth", "docs"])
def test_supported_subcommands(command: str) -> None:
    assert build_parser().parse_args([command]).command == command


def test_web_subcommand_is_removed() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["web"])
