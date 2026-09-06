"""Large delegated findings stay bounded without changing owner authority."""
import pytest
from persistent_agents.dispatch_context import canonical
from persistent_agents.runtime_values import bounded_context, legacy_bounded_context


def test_small_context_preserves_exact_evidence():
    context = {"instructions": "Watch releases", "source": "Public evidence"}
    assert bounded_context(context) == context
    assert legacy_bounded_context(context) == context


def test_available_context_keeps_release_evidence_beyond_legacy_prefix():
    source = "download guidance " * 80 + "stable release 3.15.4 " + "details " * 600
    context = {"instructions": "Watch releases. " * 180, "source": source}
    legacy = legacy_bounded_context(context)
    current = bounded_context(context)
    assert "stable release 3.15.4" not in legacy["source"]
    assert "stable release 3.15.4" in current["source"]
    assert len(canonical(current).encode("utf-8")) <= 5500
    assert len(canonical(current).encode("utf-8")) > len(canonical(legacy).encode("utf-8"))
    assert current["instructions"] == context["instructions"]
    assert current["source"].endswith(" [evidence excerpt]")
    assert context["source"] == source


@pytest.mark.parametrize("text", ['quote " slash \\ newline\n' * 800, "Evidence α界🧭" * 800])
def test_excerpt_choices_count_utf8_and_json_escaping_without_changing_authority(text):
    context = {"instructions": "Observe " * 300, "instruction": "Compare releases",
               "completion_condition": "Owner stops the assignment",
               "tools": ["web-research-1:fetch_page"],
               "results": [{"task_id": "retained-task", "result_digest": "a" * 64, "text": text}]}
    before = canonical(context)
    bounded = bounded_context(context)
    assert len(canonical(bounded).encode("utf-8")) <= 5500
    for key in ("instructions", "instruction", "completion_condition", "tools"):
        assert bounded[key] == context[key]
    assert bounded["results"][0]["task_id"] == "retained-task"
    assert bounded["results"][0]["result_digest"] == "a" * 64
    assert bounded["results"][0]["text"].endswith(" [evidence excerpt]")
    assert "\ufffd" not in bounded["results"][0]["text"]
    assert canonical(context) == before


def test_legacy_large_projection_retains_exact_original_shape_and_prefix():
    source = "x" * 8000
    context = {"instructions": "Watch releases", "source": {"text": source, "redacted": False}}
    assert legacy_bounded_context(context) == {
        "instructions": "Watch releases",
        "source": {"text": "x" * 512 + " [evidence excerpt]", "redacted": False},
    }


def test_eight_large_results_keep_each_identity_and_explicit_excerpt():
    text = "Evidence α" * 800
    context = {"instructions": "x" * 4000,
               "results": [{"task_id": str(index), "result": text} for index in range(8)]}
    bounded = bounded_context(context)
    assert len(canonical(bounded).encode("utf-8")) <= 5500
    assert bounded["instructions"] == context["instructions"]
    assert [item["task_id"] for item in bounded["results"]] == list(map(str, range(8)))
    assert all(item["result"].endswith(" [evidence excerpt]") for item in bounded["results"])
    assert context["results"][0]["result"] == text


@pytest.mark.parametrize("project", [bounded_context, legacy_bounded_context])
def test_unbounded_owner_instructions_are_refused_without_truncation(project):
    with pytest.raises(ValueError, match="assignment_model_input_limit"):
        project({"instructions": "x" * 5501, "source": [1, None]})
