"""Unit tests for the pure (non-API) helpers in ``analyzer``.

These exercise parsing/classification logic only; no Anthropic API call is
made, so an API key is not required.
"""
import pytest

import analyzer


# ─── _safe ───────────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_safe_returns_placeholder_for_empty():
    assert analyzer._safe("") == "(sin contenido)"
    assert analyzer._safe(None) == "(sin contenido)"


@pytest.mark.unit
def test_safe_trims_to_max_len():
    assert analyzer._safe("abcdef", max_len=3) == "abc"


@pytest.mark.unit
def test_safe_strips_whitespace():
    assert analyzer._safe("  hi  ") == "hi"


# ─── _parse_json ─────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_parse_json_plain_object():
    assert analyzer._parse_json('{"a": 1}') == {"a": 1}


@pytest.mark.unit
def test_parse_json_strips_code_fence():
    fenced = '```json\n{"a": 1}\n```'
    assert analyzer._parse_json(fenced) == {"a": 1}


@pytest.mark.unit
def test_parse_json_array():
    assert analyzer._parse_json("[1, 2, 3]") == [1, 2, 3]


@pytest.mark.unit
def test_parse_json_raises_on_invalid():
    with pytest.raises(ValueError):
        analyzer._parse_json("not json")


# ─── classify_hook_type ──────────────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.parametrize(
    "title, transcript, expected",
    [
        ("nadie te dice esto", "", "Dato shock"),
        ("", "gasté mucho dinero", "Contraste"),
        ("", "mi historia personal", "Testimonio"),
        ("producto A vs producto B", "", "Comparación"),
        ("compra ya", "texto neutro", "Directo"),
    ],
)
def test_classify_hook_type(title, transcript, expected):
    assert analyzer.classify_hook_type(transcript, title) == expected
