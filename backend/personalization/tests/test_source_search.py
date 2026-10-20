"""Tests Search form normalization against the qualified source/type/project contract,
including cursor reset on submission and fail-closed malformed filter input.
"""

import pytest
from personalization.source_search import form_params


def test_form_fields_reset_cursor_and_source_view_without_losing_filter_binding():
    result = form_params({"q": "old", "cursor": "opaque", "open": {"kind": "memory"}},
                         {"q": "  new query ", "source": "atlas", "type": "atlas_page", "project_id": "p"})
    assert result == {"q": "new query", "source": "atlas", "type": "atlas_page", "project_id": "p"}


def test_navigation_preserves_opaque_cursor_and_current_source_reference():
    params = {"q": "needle", "source": "memory", "cursor": "opaque", "open": {"kind": "memory"}}
    assert form_params(params, None) == params


@pytest.mark.parametrize("fields,expected", [
    ({"q": ""}, "Enter a search term"), ({"q": "x" * 201}, "200 characters"),
    ({"q": "a\x00b"}, "control characters"), ({"q": 3}, "Enter a search term"),
    ({"source": "foreign"}, "source filter"), ({"type": "foreign"}, "type filter"),
    ({"project_id": 3}, "project filter"),
])
def test_invalid_fields_return_a_user_error_and_retire_the_old_cursor(fields, expected):
    result = form_params({"q": "needle", "cursor": "opaque"}, fields)
    assert expected in result["error"]
    assert "cursor" not in result


@pytest.mark.parametrize("cursor", [3, "x" * 2049])
def test_unbounded_or_nonstring_cursor_is_refused(cursor):
    assert "error" in form_params({"q": "needle", "cursor": cursor}, None)


def test_all_source_type_and_project_values_are_explicitly_normalized():
    assert form_params({"q": "needle", "source": "", "type": "", "project_id": ""}, None) == {"q": "needle"}
    assert form_params({"q": "needle", "project_id": " - ", "source": "atlas,memory"}, None) == {
        "q": "needle", "project_id": "-", "source": "memory,atlas"}
    assert form_params(None, None) == {}
