"""Table-driven tests for the comment command grammar."""

import pytest

from samye.commands import Instruct, ParseError, Pin, Unpin, parse


@pytest.mark.parametrize(
    "comment",
    [
        "",
        "please revise this",
        " @ai revise this",
        "@aide revise this",
        "@ai: revise this",
        "prefix @ai revise this",
    ],
)
def test_ignores_non_triggers(comment: str) -> None:
    assert parse(comment) is None


@pytest.mark.parametrize(
    ("comment", "expected"),
    [
        ("@ai pin topic", Pin("topic")),
        ("@AI pin topic-2", Pin("topic-2")),
        ("@Ai\tpin\t0", Pin("0")),
        ("@ai unpin topic", Unpin("topic")),
        ("@AI\nunpin topic-2", Unpin("topic-2")),
    ],
)
def test_parses_pin_commands(comment: str, expected: Pin | Unpin) -> None:
    assert parse(comment) == expected


@pytest.mark.parametrize(
    "comment",
    [
        "@ai pin",
        "@ai pin ",
        "@ai pin two names",
        "@ai pin UPPER",
        "@ai pin under_score",
        "@ai pin name!",
        "@ai pin abcdefghijklmnopqrstuvwxyz1234567",
        "@ai unpin",
        "@ai unpin two names",
        "@ai unpin UPPER",
        "@ai unpin under_score",
    ],
)
def test_reports_malformed_pin_commands(comment: str) -> None:
    result = parse(comment)

    assert isinstance(result, ParseError)
    assert result.message.startswith("usage:")


@pytest.mark.parametrize("comment", ["@ai", "@AI ", "@ai\n\t"])
def test_reports_missing_instruction(comment: str) -> None:
    assert isinstance(parse(comment), ParseError)


@pytest.mark.parametrize(
    ("comment", "instruction", "refs"),
    [
        ("@ai rewrite this", "rewrite this", []),
        ("@AI   keep internal   spacing  ", "keep internal   spacing", []),
        (
            "@ai compare @[intro] with @[ending-2]",
            "compare @[intro] with @[ending-2]",
            ["intro", "ending-2"],
        ),
        (
            "@ai use @[same] then @[same]",
            "use @[same] then @[same]",
            ["same", "same"],
        ),
        ("@ai pinpoint the issue", "pinpoint the issue", []),
        ("@ai PIN this wording", "PIN this wording", []),
        ("@ai mention @[UPPER] and @[under_score]", "mention @[UPPER] and @[under_score]", []),
        (
            "@ai mention @[abcdefghijklmnopqrstuvwxyz1234567]",
            "mention @[abcdefghijklmnopqrstuvwxyz1234567]",
            [],
        ),
    ],
)
def test_parses_instructions(comment: str, instruction: str, refs: list[str]) -> None:
    assert parse(comment) == Instruct(instruction=instruction, refs=refs)
