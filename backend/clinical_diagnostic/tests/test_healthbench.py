"""Tests for clinical_diagnostic.healthbench: Professional conversation/rubric_items
adaptation, strict JSON hygiene, bounds, reviewed-asset identity, and zip member handling.
"""

from __future__ import annotations

import hashlib
import json
import zipfile

import pytest

from clinical_diagnostic.healthbench import (
    MAX_CORPUS_BYTES,
    REVIEWED_ASSET_SHA256,
    SchemaError,
    AssetIdentityError,
    asset_identity_for_file,
    load_examples_file,
    load_reviewed_asset_bundle,
    parse_document,
    parse_example,
    parse_examples_jsonl,
)
from clinical_diagnostic.models import CriterionKind, Partition, Setting


def example(**over):
    payload = {
        "prompt_id": "case-1",
        "partition": "development",
        "setting": "consultation",
        "specialty": "primary care",
        "conversation": [{"role": "user", "content": "Synthetic question?"}],
        "rubric_items": [
            {
                "criterion_id": "required",
                "criterion": "States the fact.",
                "points": 1.0,
                "tags": ["required_fact"],
                "evidence_terms": ["the fact"],
            },
            {
                "criterion_id": "harmful",
                "criterion": "Claims the harmful thing.",
                "points": -2.0,
                "tags": ["harmful_claim"],
            },
        ],
    }
    payload.update(over)
    return payload


def document(*entries):
    return json.dumps(list(entries or [example()])).encode("utf-8")


def test_reviewed_asset_identity_is_the_source_reviewed_sha():
    assert REVIEWED_ASSET_SHA256 == (
        "a5286ead1f03d178c7f7b134a5ac16944998428303012f0921814701e0f3995e"
    )


def test_valid_example_adapts_with_inferred_kinds_and_defaults():
    cases = parse_document(document())
    case = cases[0]
    assert case.case_id == "case-1"
    assert case.setting is Setting.CONSULTATION
    assert [item.kind for item in case.rubric_items] == [
        CriterionKind.REQUIRED_FACT, CriterionKind.HARMFUL_CLAIM
    ]
    assert case.positive_weight == 1.0


def test_example_extensions_parse_frozen_answers_and_review_observations():
    payload = example(
        frozen_answers={"candidate": "A supported answer."},
        review={
            "status": "draft",
            "labels_source": "author_draft",
            "observations": [
                {"answer_key": "candidate", "labels": {"required": True, "harmful": None}}
            ],
        },
    )
    case = parse_example(payload)
    assert case.frozen_answers["candidate"] == "A supported answer."
    assert case.review.observations[0].labels["harmful"] is None


def test_unknown_fields_are_refused_at_every_level():
    with pytest.raises(SchemaError, match="unknown fields"):
        parse_document(json.dumps([example(extra=1)]).encode())
    bad_message = example(conversation=[{"role": "user", "content": "x", "name": "y"}])
    with pytest.raises(SchemaError, match="message 0 has unknown fields"):
        parse_document(json.dumps([bad_message]).encode())
    bad_item = example()
    bad_item["rubric_items"][0]["severity"] = "high"
    with pytest.raises(SchemaError, match="rubric item 0 has unknown fields"):
        parse_document(json.dumps([bad_item]).encode())


def test_duplicate_json_keys_and_non_finite_values_are_refused():
    with pytest.raises(SchemaError, match="duplicate JSON key"):
        parse_document(b'[{"prompt_id": "a", "prompt_id": "b"}]')
    with pytest.raises(SchemaError, match="non-finite"):
        parse_document(b'[{"prompt_id": "a", "rubric_items": [{"criterion": "x", '
                       b'"points": NaN}]}]')
    bad = example()
    bad["rubric_items"][0]["points"] = 1e400
    with pytest.raises(SchemaError, match="finite"):
        parse_document(json.dumps([bad]).encode())


def test_document_requires_utf8_json_and_bounded_size():
    with pytest.raises(SchemaError, match="UTF-8"):
        parse_document(b"\xff\xfe")
    with pytest.raises(SchemaError, match="valid JSON"):
        parse_document(b"{")
    with pytest.raises(SchemaError, match="exceeds"):
        parse_document(b" " * (MAX_CORPUS_BYTES + 1))
    with pytest.raises(SchemaError, match="no examples"):
        parse_document(b"[]")


def test_conversation_and_rubric_bounds_are_enforced():
    with pytest.raises(SchemaError, match="conversation must hold"):
        parse_example(example(conversation=[]))
    with pytest.raises(SchemaError, match="role"):
        parse_example(example(conversation=[{"role": "tool", "content": "x"}]))
    with pytest.raises(SchemaError, match="end with a user"):
        parse_example(example(conversation=[
            {"role": "user", "content": "x"}, {"role": "assistant", "content": "y"}
        ]))
    with pytest.raises(SchemaError, match="criterion must not be empty"):
        parse_example(example(rubric_items=[{"criterion": "", "points": 1.0}]))
    with pytest.raises(SchemaError, match="numeric points"):
        parse_example(example(rubric_items=[{"criterion": "x", "points": True}]))
    with pytest.raises(SchemaError, match="must not be zero"):
        parse_example(example(rubric_items=[{"criterion": "x", "points": 0.0}]))
    with pytest.raises(SchemaError, match="positive weight"):
        parse_example(example(rubric_items=[
            {"criterion": "x", "points": -1.0}
        ]))


def test_item_id_is_accepted_as_a_criterion_id_alias():
    payload = example(rubric_items=[
        {"id": "alias", "criterion": "States it.", "points": 1.0},
        {"id": "harm", "criterion": "Claims harm.", "points": -1.0},
    ])
    assert [item.criterion_id for item in parse_example(payload).rubric_items] == [
        "alias", "harm"
    ]


def test_partition_setting_and_tags_are_validated():
    assert parse_example(example(partition="held_out")).partition is Partition.HELD_OUT
    with pytest.raises(SchemaError, match="unknown partition"):
        parse_example(example(partition="train"))
    with pytest.raises(SchemaError, match="unknown setting"):
        parse_example(example(setting="billing"))
    with pytest.raises(SchemaError, match="tags"):
        parse_example(example(example_tags=[f"t{i}" for i in range(9)]))
    with pytest.raises(SchemaError, match="bounded non-empty"):
        parse_example(example(example_tags=[""]))


def test_duplicate_rubric_ids_and_case_ids_are_refused():
    duplicate_items = example()
    duplicate_items["rubric_items"][1]["criterion_id"] = "required"
    with pytest.raises(SchemaError, match="unique"):
        parse_document(json.dumps([duplicate_items]).encode())
    with pytest.raises(SchemaError, match="duplicate case id"):
        parse_document(document(example(), example()))


def test_example_requires_safe_identifier():
    with pytest.raises(SchemaError, match="prompt_id or id"):
        parse_example(example(prompt_id="not safe/.."))
    assert parse_example(example(prompt_id="ok_1.2:3")).case_id == "ok_1.2:3"


def test_json_lines_adapt_and_reject_bad_lines():
    lines = b"\n".join([json.dumps(example()).encode(), b""])
    cases = parse_examples_jsonl(lines)
    assert len(cases) == 1
    with pytest.raises(SchemaError, match="JSON lines"):
        parse_examples_jsonl(b'{"prompt_id": "x"}\n{bad}\n')


def test_load_examples_file_returns_cases_and_digest(tmp_path):
    path = tmp_path / "cases.json"
    path.write_bytes(document())
    cases, digest = load_examples_file(str(path))
    assert len(cases) == 1
    assert digest == hashlib.sha256(document()).hexdigest()


def test_asset_identity_for_file_hashes_local_bytes(tmp_path):
    path = tmp_path / "cases.json"
    path.write_bytes(document())
    identity = asset_identity_for_file(
        str(path), source="synthetic fixture", license_name="Apache-2.0"
    )
    assert identity.sha256 == hashlib.sha256(document()).hexdigest()
    assert identity.license_name == "Apache-2.0"


def _bundle(tmp_path, member="examples.json", payload=None, name="assets.zip"):
    path = tmp_path / name
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(member, payload if payload is not None else document())
    return path


def test_reviewed_asset_bundle_loads_verified_member(tmp_path):
    path = _bundle(tmp_path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    cases, identity = load_reviewed_asset_bundle(
        str(path), member="examples.json", expected_sha256=digest
    )
    assert len(cases) == 1
    assert identity.sha256 == digest
    assert identity.source.startswith("https://openaipublic")


def test_reviewed_asset_bundle_refuses_digest_mismatch_and_bad_members(tmp_path):
    path = _bundle(tmp_path)
    with pytest.raises(AssetIdentityError, match="does not match"):
        load_reviewed_asset_bundle(
            str(path), member="examples.json", expected_sha256="0" * 64
        )
    with pytest.raises(AssetIdentityError, match="full lowercase digest"):
        load_reviewed_asset_bundle(
            str(path), member="examples.json", expected_sha256=""
        )
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(SchemaError, match="no member"):
        load_reviewed_asset_bundle(
            str(path), member="missing.json", expected_sha256=digest
        )
    with pytest.raises(SchemaError, match=".json"):
        load_reviewed_asset_bundle(
            str(path), member="examples.txt", expected_sha256=digest
        )
    not_zip = tmp_path / "not.zip"
    not_zip.write_bytes(b"not a zip")
    with pytest.raises(AssetIdentityError, match="readable zip"):
        load_reviewed_asset_bundle(
            str(not_zip), member="examples.json",
            expected_sha256=hashlib.sha256(b"not a zip").hexdigest(),
        )


def test_reviewed_asset_bundle_accepts_jsonl_member(tmp_path):
    path = _bundle(tmp_path, member="examples.jsonl", payload=json.dumps(example()).encode())
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    cases, _identity = load_reviewed_asset_bundle(
        str(path), member="examples.jsonl", expected_sha256=digest
    )
    assert len(cases) == 1


def test_review_validation_refuses_bad_status_and_labels():
    with pytest.raises(SchemaError, match="status or label source"):
        parse_example(example(review={"status": "approved"}))
    with pytest.raises(SchemaError, match="unknown frozen answer"):
        parse_example(example(review={"observations": [
            {"answer_key": "candidate", "labels": {}}
        ]}))
    payload = example(frozen_answers={"candidate": "a"})
    payload["review"] = {"observations": [
        {"answer_key": "candidate", "labels": {"required": "yes"}}
    ]}
    with pytest.raises(SchemaError, match="true, false, or null"):
        parse_example(payload)
    payload["review"] = {"observations": [
        {"answer_key": "candidate", "labels": {"unknown": True}}
    ]}
    with pytest.raises(SchemaError, match="unknown criterion"):
        parse_example(payload)
    with pytest.raises(SchemaError, match="reviewer_token"):
        parse_example(example(
            frozen_answers={"candidate": "a"},
            review={
                "status": "clinician_reviewed",
                "labels_source": "clinician_review",
                "reviewed_at": "2026-10-20",
            },
        ))

def test_document_accepts_object_examples_bytearray_and_refuses_other_shapes():
    payload = json.dumps({"examples": [example()]}).encode()
    assert len(parse_document(bytearray(payload))) == 1
    with pytest.raises(SchemaError, match="must be bytes"):
        parse_document("not bytes")  # type: ignore[arg-type]
    with pytest.raises(SchemaError, match="JSON array or object"):
        parse_document(b"42")
    with pytest.raises(SchemaError, match="examples must be a list"):
        parse_document(b'{"examples": {}}')
    with pytest.raises(SchemaError, match="example must be an object"):
        parse_document(b'[1]')
    with pytest.raises(SchemaError, match="example 1 must be an object"):
        parse_example(1, index=1)  # type: ignore[arg-type]


def test_metadata_frozen_answers_and_review_shapes_are_validated(monkeypatch):
    with pytest.raises(SchemaError, match="specialty"):
        parse_example(example(specialty=12))
    with pytest.raises(SchemaError, match="metadata must be an object"):
        parse_example(example(metadata=[]))
    with pytest.raises(SchemaError, match="frozen_answers must be an object"):
        parse_example(example(frozen_answers=[]))
    with pytest.raises(SchemaError, match="map strings to strings"):
        parse_example(example(frozen_answers={"candidate": 1}))
    with pytest.raises(SchemaError, match="review must be an object"):
        parse_example(example(review=[]))
    with pytest.raises(SchemaError, match="review has unknown fields"):
        parse_example(example(review={"approver": "x"}))
    with pytest.raises(SchemaError, match="reviewer_token"):
        parse_example(example(review={"reviewer_token": 1}))
    with pytest.raises(SchemaError, match="review observations must be a list"):
        parse_example(example(review={"observations": {}}))
    with pytest.raises(SchemaError, match="answer_key and labels"):
        parse_example(example(review={"observations": [{"answer_key": "candidate"}]}))
    with pytest.raises(SchemaError, match="review labels must be an object"):
        parse_example(example(
            frozen_answers={"candidate": "a"},
            review={"observations": [{"answer_key": "candidate", "labels": []}]},
        ))
    with pytest.raises(SchemaError, match="too long"):
        monkeypatch.setattr("clinical_diagnostic.healthbench.MAX_CORPUS_BYTES", 2)
        parse_example(example(frozen_answers={"candidate": "long"}))


def test_conversation_message_shapes_are_validated(monkeypatch):
    with pytest.raises(SchemaError, match="message 0 must be an object"):
        parse_example(example(conversation=[1]))
    with pytest.raises(SchemaError, match="needs role and content"):
        parse_example(example(conversation=[{"role": 1, "content": "x"}]))
    with pytest.raises(SchemaError, match="too long"):
        monkeypatch.setattr("clinical_diagnostic.healthbench.MAX_MESSAGE_CHARS", 2)
        parse_example(example(conversation=[{"role": "user", "content": "long"}]))


def test_rubric_item_shapes_are_validated(monkeypatch):
    with pytest.raises(SchemaError, match="rubric_items must hold"):
        parse_example(example(rubric_items=[]))
    with pytest.raises(SchemaError, match="rubric item 0 must be an object"):
        parse_example(example(rubric_items=[1]))
    with pytest.raises(SchemaError, match="bounded criterion"):
        monkeypatch.setattr("clinical_diagnostic.healthbench.MAX_CRITERION_CHARS", 2)
        parse_example(example(rubric_items=[{"criterion": "long", "points": 1.0}]))
    with pytest.raises(SchemaError, match="kind must be a string"):
        parse_example(example(rubric_items=[{
            "criterion": "x", "points": 1.0, "kind": 1
        }]))
    with pytest.raises(SchemaError, match="kind is unknown"):
        parse_example(example(rubric_items=[{
            "criterion": "x", "points": 1.0, "kind": "nope"
        }]))
    with pytest.raises(SchemaError, match="criterion_id must be a string"):
        parse_example(example(rubric_items=[{
            "criterion": "x", "points": 1.0, "criterion_id": 1
        }]))
    with pytest.raises(SchemaError, match="tags must be a bounded list"):
        parse_example(example(rubric_items=[{
            "criterion": "x", "points": 1.0, "tags": "tag"
        }]))
    with pytest.raises(SchemaError, match="evidence_terms"):
        parse_example(example(rubric_items=[{
            "criterion": "x", "points": 1.0, "evidence_terms": [1]
        }]))


def test_partition_setting_and_metadata_type_errors(monkeypatch):
    with pytest.raises(SchemaError, match="partition must be a string"):
        parse_example(example(partition=1))
    with pytest.raises(SchemaError, match="setting must be a string"):
        parse_example(example(setting=1))
    with pytest.raises(SchemaError, match="specialty must be a bounded string"):
        monkeypatch.setattr("clinical_diagnostic.healthbench.MAX_METADATA_CHARS", 2)
        parse_example(example(specialty="long"))


def test_jsonl_limits_and_encoding(monkeypatch):
    with pytest.raises(SchemaError, match="must be bytes"):
        parse_examples_jsonl("text")  # type: ignore[arg-type]
    with pytest.raises(SchemaError, match="UTF-8"):
        parse_examples_jsonl(b"\xff")
    monkeypatch.setattr("clinical_diagnostic.healthbench.MAX_CORPUS_BYTES", 1)
    with pytest.raises(SchemaError, match="exceeds"):
        parse_examples_jsonl(b"{}")
    monkeypatch.undo()
    monkeypatch.setattr("clinical_diagnostic.healthbench.MAX_EXAMPLES", 0)
    with pytest.raises(SchemaError, match="examples"):
        parse_examples_jsonl(json.dumps(example()).encode())
    with pytest.raises(SchemaError, match="examples"):
        parse_document(document())


def test_load_examples_file_rejects_oversized_files(tmp_path, monkeypatch):
    path = tmp_path / "cases.json"
    path.write_bytes(document())
    monkeypatch.setattr("clinical_diagnostic.healthbench.MAX_CORPUS_BYTES", 1)
    with pytest.raises(SchemaError, match="exceeds"):
        load_examples_file(str(path))
    with pytest.raises(AssetIdentityError, match="not readable"):
        asset_identity_for_file(str(tmp_path / "missing.json"), source="x", license_name="y")


def test_asset_bundle_rejects_oversized_members(tmp_path, monkeypatch):
    oversized = _bundle(tmp_path, payload=document(), name="oversized.zip")
    monkeypatch.setattr("clinical_diagnostic.healthbench.MAX_ASSET_MEMBER_BYTES", 1)
    with pytest.raises(SchemaError, match="bounded file"):
        load_reviewed_asset_bundle(
            str(oversized), member="examples.json",
            expected_sha256=hashlib.sha256(oversized.read_bytes()).hexdigest(),
        )


def official_example(**over):
    payload = {
        "id": "synthetic-official-1",
        "conversation": {"messages": [{"role": "user", "content": "Synthetic request."}]},
        "rubric_items": [{"criterion_text": "Synthetic criterion.", "points": 2}],
        "use_case": "synthetic", "type": "synthetic", "difficulty": "synthetic",
        "specialty": "synthetic", "physician_response": "Synthetic reference.",
        "canary_string": "synthetic-marker",
    }
    payload.update(over)
    return payload


def test_official_nested_schema_normalizes_without_inventing_review_or_partition():
    case = parse_example(official_example())
    assert case.case_id == "synthetic-official-1"
    assert case.conversation[0].content == "Synthetic request."
    assert case.rubric_items[0].criterion == "Synthetic criterion."
    assert case.rubric_items[0].points == 2
    assert case.review.status.value == "draft"
    assert case.partition is Partition.DEVELOPMENT
    assert case.frozen_answers == {}


def test_jsonl_uses_literal_lf_and_preserves_unicode_separators_in_strings():
    first = official_example(conversation={"messages": [
        {"role": "user", "content": "Synthetic\u2028inside\u2029request."}]})
    second = official_example(id="synthetic-official-2")
    data = (json.dumps(first, ensure_ascii=False) + "\n" + json.dumps(second) + "\n").encode()
    cases = parse_examples_jsonl(data)
    assert len(cases) == 2
    assert cases[0].conversation[0].content == "Synthetic\u2028inside\u2029request."


@pytest.mark.parametrize("parser", [parse_document, parse_examples_jsonl])
def test_corpus_recursion_is_a_schema_failure(parser):
    with pytest.raises(SchemaError, match="nesting"):
        parser(b"[" * 4001 + b"0" + b"]" * 4001)


@pytest.mark.parametrize("over", [
    {"conversation": {"messages": [], "extra": 1}},
    {"conversation": {"messages": "bad"}},
    {"rubric_items": [{"criterion_text": "x", "criterion": "y", "points": 1}]},
    {"rubric_items": [{"criterion_text": "x", "points": float("inf")}]},
    {"physician_response": []}, {"canary_string": {}},
    {"use_case": 1}, {"difficulty": ["synthetic"]}, {"type": False},
    {"extra": "unknown"},
])
def test_official_schema_rejects_unknown_alias_and_invalid_metadata(over):
    with pytest.raises(SchemaError):
        parse_example(official_example(**over))


@pytest.mark.parametrize("field", ["use_case", "type", "difficulty", "canary_string", "physician_response"])
def test_official_discarded_metadata_still_obeys_existing_scalar_bounds(field, monkeypatch):
    bound = "MAX_MESSAGE_CHARS" if field == "physician_response" else "MAX_METADATA_CHARS"
    monkeypatch.setattr("clinical_diagnostic.healthbench." + bound, 32)
    with pytest.raises(SchemaError, match="bounded string"):
        parse_example(official_example(**{field: "x" * 33}))


@pytest.mark.parametrize("value", [[], [1], [{"criterion_text": "x", "points": True}]])
def test_official_rubric_retains_item_and_scalar_rejection(value):
    with pytest.raises(SchemaError):
        parse_example(official_example(rubric_items=value))


def test_official_normalization_rejects_nested_unknown_and_duplicate_json_keys():
    payload = official_example(conversation={"messages": [
        {"role": "user", "content": "Synthetic.", "untrusted": "extra"}]})
    with pytest.raises(SchemaError, match="unknown fields"):
        parse_example(payload)
    text = json.dumps(official_example()).replace('"criterion_text": "Synthetic criterion."',
        '"criterion_text": "Synthetic criterion.", "criterion_text": "Another"')
    with pytest.raises(SchemaError, match="duplicate JSON key"):
        parse_examples_jsonl(text.encode())
