"""Tests for Google Docs text and UTF-16 index mapping."""

import json
from pathlib import Path

import pytest

from samye.textmap import Span, build_text_map

FIXTURE = Path(__file__).parent / "fixtures" / "t0a_document.json"


def captured_document() -> dict[str, object]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_captured_body_maps_emoji_and_round_trips() -> None:
    tab = captured_document()["tabs"][0]
    text_map = build_text_map(tab["documentTab"]["body"], "tab-1")
    py_start = text_map.text.index("😀")

    span = text_map.to_utf16_span(py_start, py_start + 1)

    assert span.end - span.start == 2
    assert text_map.to_py_span(span) == (py_start, py_start + 1)
    assert text_map.text_for_span(span) == "😀"


def test_captured_repeated_phrase_is_found_in_body_child_and_header() -> None:
    root = captured_document()["tabs"][0]
    child = root["childTabs"][0]
    document_tab = root["documentTab"]
    header_id, header = next(iter(document_tab["headers"].items()))
    maps = [
        build_text_map(document_tab["body"], "tab-1"),
        build_text_map(child["documentTab"]["body"], "tab-2"),
        build_text_map(header, "tab-1", header_id),
    ]

    matches = [span for text_map in maps for span in text_map.find_all("SAMYE_REPEAT")]

    assert [(span.tab_id, span.segment_id) for span in matches] == [
        ("tab-1", None),
        ("tab-2", None),
        ("tab-1", "header-1"),
    ]
    assert matches[-1].start == 0


def test_styled_runs_are_searchable_as_one_continuous_match() -> None:
    segment = {
        "content": [
            {
                "startIndex": 1,
                "endIndex": 12,
                "paragraph": {
                    "elements": [
                        {"startIndex": 1, "endIndex": 6, "textRun": {"content": "hello"}},
                        {
                            "startIndex": 6,
                            "endIndex": 12,
                            "textRun": {"content": " world", "textStyle": {"bold": True}},
                        },
                    ]
                },
            }
        ]
    }

    assert build_text_map(segment, "tab").find_all("lo wo") == [Span("tab", 4, 9)]


def test_cross_cell_false_adjacency_is_rejected_on_capture() -> None:
    body = captured_document()["tabs"][0]["documentTab"]["body"]
    text_map = build_text_map(body, "tab-1")
    table = next(item["table"] for item in body["content"] if "table" in item)
    first_element = table["tableRows"][0]["tableCells"][0]["content"][0]["paragraph"][
        "elements"
    ][0]
    _, first_cell_end = text_map.to_py_span(
        Span("tab-1", first_element["startIndex"], first_element["endIndex"])
    )

    with pytest.raises(ValueError, match="structural boundary"):
        text_map.to_utf16_span(first_cell_end - 2, first_cell_end + 2)


def test_cross_cell_needle_is_not_a_match() -> None:
    segment = {
        "content": [
            {
                "startIndex": 1,
                "endIndex": 14,
                "table": {
                    "tableRows": [
                        {
                            "tableCells": [
                                {
                                    "content": [
                                        {
                                            "startIndex": 3,
                                            "endIndex": 7,
                                            "paragraph": {
                                                "elements": [
                                                    {
                                                        "startIndex": 3,
                                                        "endIndex": 7,
                                                        "textRun": {"content": "left"},
                                                    }
                                                ]
                                            },
                                        }
                                    ]
                                },
                                {
                                    "content": [
                                        {
                                            "startIndex": 9,
                                            "endIndex": 14,
                                            "paragraph": {
                                                "elements": [
                                                    {
                                                        "startIndex": 9,
                                                        "endIndex": 14,
                                                        "textRun": {"content": "right"},
                                                    }
                                                ]
                                            },
                                        }
                                    ]
                                },
                            ]
                        }
                    ]
                },
            }
        ]
    }
    text_map = build_text_map(segment, "tab")

    assert "leftright" in text_map.text
    assert text_map.find_all("leftright") == []


def test_pending_suggestion_span_is_not_clean() -> None:
    body = captured_document()["tabs"][0]["documentTab"]["body"]
    text_map = build_text_map(body, "tab-1")
    suggested = next(
        element
        for structural in body["content"]
        for element in structural.get("paragraph", {}).get("elements", [])
        if element.get("textRun", {}).get("suggestedInsertionIds")
    )
    content = suggested["textRun"]["content"]
    py_start = text_map.text.index(content)
    span = text_map.to_utf16_span(py_start, py_start + 8)

    assert not text_map.is_clean_span(span)
    clean_start = text_map.text.index("SAMYE_REPEAT")
    assert text_map.is_clean_span(
        text_map.to_utf16_span(clean_start, clean_start + len("SAMYE_REPEAT"))
    )


def test_table_cell_terminal_newline_is_protected() -> None:
    body = captured_document()["tabs"][0]["documentTab"]["body"]
    text_map = build_text_map(body, "tab-1")
    table = next(item["table"] for item in body["content"] if "table" in item)
    first_element = table["tableRows"][0]["tableCells"][0]["content"][0]["paragraph"][
        "elements"
    ][0]
    span = Span("tab-1", first_element["endIndex"] - 1, first_element["endIndex"])

    assert not text_map.is_clean_span(span)


def test_wrong_segment_and_surrogate_interior_are_rejected() -> None:
    segment = {
        "content": [
            {
                "endIndex": 3,
                "paragraph": {"elements": [{"endIndex": 3, "textRun": {"content": "😀x"}}]},
            }
        ]
    }
    text_map = build_text_map(segment, "tab", "header")

    with pytest.raises(ValueError, match="different tab or segment"):
        text_map.to_py_span(Span("tab", 0, 2))
    with pytest.raises(ValueError, match="character boundaries"):
        text_map.to_py_span(Span("tab", 1, 2, "header"))
