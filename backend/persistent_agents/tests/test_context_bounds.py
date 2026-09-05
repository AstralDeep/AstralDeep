"""Large delegated findings stay bounded without changing owner authority."""
import pytest
from persistent_agents.dispatch_context import canonical
from persistent_agents.runtime_values import bounded_context


def test_small_context_preserves_exact_evidence():
    context = {"instructions": "Watch releases", "source": "Public evidence"}
    assert bounded_context(context) == context


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


def test_unbounded_owner_instructions_are_refused_without_truncation():
    with pytest.raises(ValueError, match="assignment_model_input_limit"):
        bounded_context({"instructions": "x" * 5501, "source": [1, None]})
