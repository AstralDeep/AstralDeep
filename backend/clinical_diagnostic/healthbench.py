"""Adapts the HealthBench Professional ``conversation``/``rubric_items`` schema into
validated clinical cases and verifies the reviewed licensed asset bundle's identity by
SHA-256; malformed examples, duplicate keys, and non-finite values fail closed.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import zipfile
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from clinical_diagnostic.models import (
    LABEL_SOURCES,
    MAX_CRITERION_CHARS,
    MAX_MESSAGE_CHARS,
    MAX_MESSAGES,
    MAX_CASE_ID_CHARS,
    MAX_RUBRIC_ITEMS,
    MAX_TAG_CHARS,
    MAX_TAGS,
    ClinicalCase,
    ConversationMessage,
    CriterionKind,
    LabelSource,
    Partition,
    REVIEW_STATUSES,
    ReviewObservation,
    ReviewRecord,
    ReviewStatus,
    RubricItem,
    Setting,
)

PROFESSIONAL_SCHEMA = "healthbench-professional/v1"
REVIEWED_ASSET_URL = (
    "https://openaipublic.blob.core.windows.net/"
    "simple-evals/healthbench_professional/assets.zip"
)
REVIEWED_ASSET_SHA256 = (
    "a5286ead1f03d178c7f7b134a5ac16944998428303012f0921814701e0f3995e"
)
REVIEWED_ASSET_RETRIEVED = "2026-10-06"
REVIEWED_ASSET_LICENSE = "HealthBench Professional licensed asset bundle"

MAX_CORPUS_BYTES = 2 * 1024 * 1024
MAX_ASSET_BYTES = 256 * 1024 * 1024
MAX_ASSET_MEMBER_BYTES = 64 * 1024 * 1024
MAX_EXAMPLES = 5000
MAX_METADATA_CHARS = 2000

EXAMPLE_FIELDS = frozenset({
    "prompt_id",
    "id",
    "conversation",
    "rubric_items",
    "example_tags",
    "specialty",
    "setting",
    "partition",
    "metadata",
    "canary",
    "frozen_answers",
    "review",
})
OFFICIAL_FIELDS = frozenset({
    "id", "conversation", "rubric_items", "use_case", "type", "difficulty",
    "specialty", "physician_response", "canary_string",
})
MESSAGE_FIELDS = frozenset({"role", "content"})
ITEM_FIELDS = frozenset({
    "criterion_id",
    "id",
    "criterion",
    "points",
    "tags",
    "kind",
    "evidence_terms",
})
CASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}$")
_DUPLICATE = object()


class SchemaError(ValueError):
    pass


class AssetIdentityError(ValueError):
    pass


@dataclass(frozen=True)
class AssetIdentity:
    schema: str
    source: str
    sha256: str
    license_name: str
    license_url: str
    retrieved: str

    def to_dict(self) -> dict:
        return {
            "schema": self.schema,
            "source": self.source,
            "sha256": self.sha256,
            "license_name": self.license_name,
            "license_url": self.license_url,
            "retrieved": self.retrieved,
        }


REVIEWED_PROFESSIONAL_ASSET = AssetIdentity(
    schema=PROFESSIONAL_SCHEMA,
    source=REVIEWED_ASSET_URL,
    sha256=REVIEWED_ASSET_SHA256,
    license_name=REVIEWED_ASSET_LICENSE,
    license_url="",
    retrieved=REVIEWED_ASSET_RETRIEVED,
)


def asset_identity_for_file(path: str, *, source: str, license_name: str,
                            license_url: str = "") -> AssetIdentity:
    digest = _file_sha256(path, MAX_CORPUS_BYTES)
    return AssetIdentity(
        schema=PROFESSIONAL_SCHEMA,
        source=source,
        sha256=digest,
        license_name=license_name,
        license_url=license_url,
        retrieved=REVIEWED_ASSET_RETRIEVED,
    )


def load_examples_file(
    path: str,
    *,
    default_partition: Partition = Partition.DEVELOPMENT,
) -> Tuple[List[ClinicalCase], str]:
    with open(path, "rb") as handle:
        data = handle.read(MAX_CORPUS_BYTES + 1)
    if len(data) > MAX_CORPUS_BYTES:
        raise SchemaError(f"corpus exceeds {MAX_CORPUS_BYTES} bytes")
    return parse_document(data, default_partition=default_partition), hashlib.sha256(data).hexdigest()


def parse_document(
    data: bytes,
    *,
    default_partition: Partition = Partition.DEVELOPMENT,
    max_bytes: Optional[int] = None,
) -> List[ClinicalCase]:
    limit = MAX_CORPUS_BYTES if max_bytes is None else max_bytes
    if not isinstance(data, (bytes, bytearray)):
        raise SchemaError("corpus document must be bytes")
    if len(data) > limit:
        raise SchemaError(f"corpus exceeds {limit} bytes")
    try:
        text = bytes(data).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SchemaError("corpus is not valid UTF-8") from exc
    try:
        payload = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except RecursionError as exc:
        raise SchemaError("corpus exceeds the nesting bound") from exc
    except json.JSONDecodeError as exc:
        raise SchemaError(f"corpus is not valid JSON: {exc.msg}") from exc
    entries = _example_entries(payload)
    if not entries:
        raise SchemaError("corpus holds no examples")
    if len(entries) > MAX_EXAMPLES:
        raise SchemaError(f"corpus exceeds {MAX_EXAMPLES} examples")
    cases = [
        parse_example(entry, default_partition=default_partition, index=index)
        for index, entry in enumerate(entries)
    ]
    _reject_duplicate_case_ids(cases)
    return cases


def parse_examples_jsonl(
    data: bytes,
    *,
    default_partition: Partition = Partition.DEVELOPMENT,
    max_bytes: Optional[int] = None,
) -> List[ClinicalCase]:
    limit = MAX_CORPUS_BYTES if max_bytes is None else max_bytes
    if not isinstance(data, (bytes, bytearray)):
        raise SchemaError("corpus document must be bytes")
    if len(data) > limit:
        raise SchemaError(f"corpus exceeds {limit} bytes")
    try:
        text = bytes(data).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SchemaError("corpus is not valid UTF-8") from exc
    entries: List[Mapping[str, Any]] = []
    for line in text.split("\n"):
        if not line.strip():
            continue
        try:
            entries.append(json.loads(
                line,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_constant,
            ))
        except RecursionError as exc:
            raise SchemaError("corpus exceeds the nesting bound") from exc
        except json.JSONDecodeError as exc:
            raise SchemaError(f"corpus is not valid JSON lines: {exc.msg}") from exc
        if len(entries) > MAX_EXAMPLES:
            raise SchemaError(f"corpus exceeds {MAX_EXAMPLES} examples")
    return parse_document(
        json.dumps(entries, ensure_ascii=False).encode("utf-8"),
        default_partition=default_partition,
        max_bytes=limit,
    )


def load_reviewed_asset_bundle(
    path: str,
    *,
    member: str,
    expected_sha256: str = REVIEWED_ASSET_SHA256,
    default_partition: Partition = Partition.DEVELOPMENT,
) -> Tuple[List[ClinicalCase], AssetIdentity]:
    if not member or not (member.endswith(".json") or member.endswith(".jsonl")):
        raise SchemaError("asset member must name a .json or .jsonl entry")
    if not isinstance(expected_sha256, str) or re.fullmatch(
        "[0-9a-f]{64}", expected_sha256
    ) is None:
        raise AssetIdentityError("expected asset SHA-256 must be a full lowercase digest")
    actual = _file_sha256(path, MAX_ASSET_BYTES)
    if actual != expected_sha256:
        raise AssetIdentityError("asset bundle SHA-256 does not match the reviewed identity")
    try:
        with zipfile.ZipFile(path) as archive:
            info = archive.getinfo(member)
            if info.file_size > MAX_ASSET_MEMBER_BYTES:
                raise SchemaError("asset member is not a bounded file")
            with archive.open(info) as handle:
                data = handle.read(MAX_ASSET_MEMBER_BYTES + 1)
            if len(data) > MAX_ASSET_MEMBER_BYTES:
                raise SchemaError("asset member exceeds the size bound")
    except KeyError as exc:
        raise SchemaError(f"asset bundle has no member {member!r}") from exc
    except zipfile.BadZipFile as exc:
        raise AssetIdentityError("asset bundle is not a readable zip archive") from exc
    cases = (
        parse_examples_jsonl(
            data, default_partition=default_partition, max_bytes=MAX_ASSET_MEMBER_BYTES
        )
        if member.endswith(".jsonl")
        else parse_document(
            data, default_partition=default_partition, max_bytes=MAX_ASSET_MEMBER_BYTES
        )
    )
    return cases, AssetIdentity(
        schema=PROFESSIONAL_SCHEMA,
        source=REVIEWED_ASSET_URL,
        sha256=actual,
        license_name=REVIEWED_ASSET_LICENSE,
        license_url="",
        retrieved=REVIEWED_ASSET_RETRIEVED,
    )


def parse_example(
    payload: Mapping[str, Any],
    *,
    default_partition: Partition = Partition.DEVELOPMENT,
    index: int = 0,
) -> ClinicalCase:
    if not isinstance(payload, Mapping):
        raise SchemaError(f"example {index} must be an object")
    if isinstance(payload.get("conversation"), Mapping):
        payload = _normalize_official(payload)
    unknown = set(payload) - EXAMPLE_FIELDS
    if unknown:
        raise SchemaError(f"example {index} has unknown fields: {sorted(unknown)}")
    case_id = _case_id(payload)
    conversation = _conversation(payload.get("conversation"), case_id)
    rubric_items = _rubric_items(payload.get("rubric_items"), case_id)
    partition = _partition(payload.get("partition"), default_partition, case_id)
    setting = _setting(payload.get("setting"), case_id)
    specialty = _metadata_text(payload.get("specialty"), "specialty", case_id) or "unspecified"
    example_tags = _string_list(payload.get("example_tags"), "example_tags", case_id)
    metadata = payload.get("metadata")
    if metadata is not None and not isinstance(metadata, Mapping):
        raise SchemaError(f"{case_id}: metadata must be an object")
    frozen_answers = _frozen_answers(payload.get("frozen_answers"), case_id)
    try:
        return ClinicalCase(
            case_id=case_id,
            partition=partition,
            setting=setting,
            specialty=specialty,
            conversation=conversation,
            rubric_items=rubric_items,
            review=_review(payload.get("review"), case_id, rubric_items, frozen_answers),
            frozen_answers=frozen_answers,
            example_tags=example_tags,
        )
    except ValueError as exc:
        raise SchemaError(f"{case_id}: {exc}") from exc


def _normalize_official(payload: Mapping[str, Any]) -> Dict[str, Any]:
    if set(payload) != OFFICIAL_FIELDS:
        raise SchemaError("official example must contain exactly its declared fields")
    conversation = payload["conversation"]
    if set(conversation) != {"messages"}:
        raise SchemaError("official conversation must contain only messages")
    case_id = _case_id(payload)
    for name in ("use_case", "type", "difficulty", "canary_string"):
        if not isinstance(payload[name], str) or len(payload[name]) > MAX_METADATA_CHARS:
            raise SchemaError(f"{case_id}: {name} must be a bounded string")
    reference = payload["physician_response"]
    if not isinstance(reference, str) or len(reference) > MAX_MESSAGE_CHARS:
        raise SchemaError(f"{case_id}: physician_response must be a bounded string")
    rubric = payload["rubric_items"]
    if not isinstance(rubric, list) or not 1 <= len(rubric) <= MAX_RUBRIC_ITEMS:
        raise SchemaError(f"{case_id}: rubric_items must be a bounded list")
    normalized = []
    for entry in rubric:
        if not isinstance(entry, Mapping) or set(entry) != {"criterion_text", "points"}:
            raise SchemaError(f"{case_id}: official rubric must contain criterion_text and points")
        normalized.append({"criterion": entry["criterion_text"], "points": entry["points"]})
    return {
        "id": case_id,
        "conversation": conversation["messages"],
        "rubric_items": normalized,
        "specialty": payload["specialty"],
    }


def _frozen_answers(value: Any, case_id: str) -> Dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise SchemaError(f"{case_id}: frozen_answers must be an object")
    answers: Dict[str, str] = {}
    for key, answer in value.items():
        if not isinstance(key, str) or not isinstance(answer, str):
            raise SchemaError(f"{case_id}: frozen answers must map strings to strings")
        if len(answer) > MAX_CORPUS_BYTES:
            raise SchemaError(f"{case_id}: frozen answer is too long")
        answers[key] = answer
    return answers


def _review(
    value: Any,
    case_id: str,
    rubric_items: Sequence[RubricItem],
    frozen_answers: Mapping[str, str],
) -> ReviewRecord:
    if value is None:
        return ReviewRecord()
    if not isinstance(value, Mapping):
        raise SchemaError(f"{case_id}: review must be an object")
    allowed = {"status", "labels_source", "reviewer_token", "reviewed_at", "observations"}
    unknown = set(value) - allowed
    if unknown:
        raise SchemaError(f"{case_id}: review has unknown fields: {sorted(unknown)}")
    status = value.get("status", "draft")
    labels_source = value.get("labels_source", "none")
    if status not in REVIEW_STATUSES or labels_source not in LABEL_SOURCES:
        raise SchemaError(f"{case_id}: review status or label source is unknown")
    reviewer_token = value.get("reviewer_token", "")
    reviewed_at = value.get("reviewed_at", "")
    for name, field in (("reviewer_token", reviewer_token), ("reviewed_at", reviewed_at)):
        if not isinstance(field, str) or len(field) > MAX_CASE_ID_CHARS:
            raise SchemaError(f"{case_id}: review {name} must be a bounded string")
    observations = value.get("observations", [])
    if not isinstance(observations, list):
        raise SchemaError(f"{case_id}: review observations must be a list")
    criteria = {item.criterion_id for item in rubric_items}
    parsed: List[ReviewObservation] = []
    for entry in observations:
        if not isinstance(entry, Mapping) or set(entry) != {"answer_key", "labels"}:
            raise SchemaError(f"{case_id}: review observation must contain answer_key and labels")
        answer_key, labels = entry["answer_key"], entry["labels"]
        if not isinstance(answer_key, str) or answer_key not in frozen_answers:
            raise SchemaError(f"{case_id}: review observation names an unknown frozen answer")
        if not isinstance(labels, Mapping):
            raise SchemaError(f"{case_id}: review labels must be an object")
        clean: Dict[str, Optional[bool]] = {}
        for criterion_id, label in labels.items():
            if criterion_id not in criteria:
                raise SchemaError(f"{case_id}: review label names an unknown criterion")
            if label is not None and not isinstance(label, bool):
                raise SchemaError(f"{case_id}: review label must be true, false, or null")
            clean[criterion_id] = label
        parsed.append(ReviewObservation(answer_key=answer_key, labels=clean))
    try:
        return ReviewRecord(
            status=ReviewStatus(status),
            labels_source=LabelSource(labels_source),
            reviewer_token=reviewer_token,
            reviewed_at=reviewed_at,
            observations=tuple(parsed),
        )
    except ValueError as exc:
        raise SchemaError(f"{case_id}: {exc}") from exc


def _case_id(payload: Mapping[str, Any]) -> str:
    value = payload.get("prompt_id", payload.get("id"))
    if not isinstance(value, str) or CASE_ID.fullmatch(value) is None:
        raise SchemaError("example must declare a prompt_id or id with safe identifier characters")
    return value


def _conversation(value: Any, case_id: str) -> Tuple[ConversationMessage, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_MESSAGES:
        raise SchemaError(f"{case_id}: conversation must hold 1..{MAX_MESSAGES} messages")
    messages: List[ConversationMessage] = []
    for position, entry in enumerate(value):
        if not isinstance(entry, Mapping):
            raise SchemaError(f"{case_id}: conversation message {position} must be an object")
        unknown = set(entry) - MESSAGE_FIELDS
        if unknown:
            raise SchemaError(
                f"{case_id}: conversation message {position} has unknown fields: {sorted(unknown)}"
            )
        role, content = entry.get("role"), entry.get("content")
        if not isinstance(role, str) or not isinstance(content, str):
            raise SchemaError(f"{case_id}: conversation message {position} needs role and content")
        if len(content) > MAX_MESSAGE_CHARS:
            raise SchemaError(f"{case_id}: conversation message {position} is too long")
        try:
            messages.append(ConversationMessage(role=role, content=content))
        except ValueError as exc:
            raise SchemaError(f"{case_id}: {exc}") from exc
    return tuple(messages)


def _rubric_items(value: Any, case_id: str) -> Tuple[RubricItem, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_RUBRIC_ITEMS:
        raise SchemaError(f"{case_id}: rubric_items must hold 1..{MAX_RUBRIC_ITEMS} items")
    items: List[RubricItem] = []
    for position, entry in enumerate(value):
        if not isinstance(entry, Mapping):
            raise SchemaError(f"{case_id}: rubric item {position} must be an object")
        unknown = set(entry) - ITEM_FIELDS
        if unknown:
            raise SchemaError(
                f"{case_id}: rubric item {position} has unknown fields: {sorted(unknown)}"
            )
        criterion = entry.get("criterion")
        if not isinstance(criterion, str) or len(criterion) > MAX_CRITERION_CHARS:
            raise SchemaError(f"{case_id}: rubric item {position} needs a bounded criterion")
        points = entry.get("points")
        if isinstance(points, bool) or not isinstance(points, (int, float)):
            raise SchemaError(f"{case_id}: rubric item {position} needs numeric points")
        if not math.isfinite(float(points)):
            raise SchemaError(f"{case_id}: rubric item {position} points must be finite")
        tags = _string_list(entry.get("tags"), f"rubric item {position} tags", case_id)
        kind = _criterion_kind(entry.get("kind"), tags, float(points), case_id, position)
        evidence_terms = _string_list(
            entry.get("evidence_terms"), f"rubric item {position} evidence_terms", case_id
        )
        criterion_id = entry.get("criterion_id", entry.get("id"))
        if criterion_id is None:
            criterion_id = f"item-{position + 1}"
        if not isinstance(criterion_id, str):
            raise SchemaError(f"{case_id}: rubric item {position} criterion_id must be a string")
        try:
            items.append(RubricItem(
                criterion_id=criterion_id,
                criterion=criterion,
                points=float(points),
                tags=tuple(tags),
                kind=kind,
                evidence_terms=tuple(evidence_terms),
            ))
        except ValueError as exc:
            raise SchemaError(f"{case_id}: {exc}") from exc
    return tuple(items)


def _criterion_kind(
    value: Any,
    tags: Sequence[str],
    points: float,
    case_id: str,
    position: int,
) -> CriterionKind:
    if value is not None:
        if not isinstance(value, str):
            raise SchemaError(f"{case_id}: rubric item {position} kind must be a string")
        try:
            return CriterionKind(value)
        except ValueError as exc:
            raise SchemaError(f"{case_id}: rubric item {position} kind is unknown") from exc
    lowered = {tag.lower() for tag in tags}
    if points < 0:
        return CriterionKind.HARMFUL_CLAIM
    if any("abstain" in tag or "abstention" in tag for tag in lowered):
        return CriterionKind.APPROPRIATE_ABSTENTION
    if any("omission" in tag for tag in lowered):
        return CriterionKind.OMISSION
    return CriterionKind.REQUIRED_FACT


def _string_list(value: Any, name: str, case_id: str) -> Tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > MAX_TAGS:
        raise SchemaError(f"{case_id}: {name} must be a bounded list")
    result: List[str] = []
    for entry in value:
        if not isinstance(entry, str) or not entry.strip() or len(entry) > MAX_TAG_CHARS:
            raise SchemaError(f"{case_id}: {name} entries must be bounded non-empty strings")
        result.append(entry)
    return tuple(result)


def _partition(value: Any, default: Partition, case_id: str) -> Partition:
    if value is None:
        return default
    if not isinstance(value, str):
        raise SchemaError(f"{case_id}: partition must be a string")
    try:
        return Partition(value)
    except ValueError as exc:
        raise SchemaError(f"{case_id}: unknown partition {value!r}") from exc


def _setting(value: Any, case_id: str) -> Setting:
    if value is None:
        return Setting.CONSULTATION
    if not isinstance(value, str):
        raise SchemaError(f"{case_id}: setting must be a string")
    try:
        return Setting(value)
    except ValueError as exc:
        raise SchemaError(f"{case_id}: unknown setting {value!r}") from exc


def _metadata_text(value: Any, name: str, case_id: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > MAX_METADATA_CHARS:
        raise SchemaError(f"{case_id}: {name} must be a bounded string")
    return value.strip()


def _example_entries(payload: Any) -> List[Mapping[str, Any]]:
    if isinstance(payload, list):
        entries = payload
    elif isinstance(payload, Mapping):
        entries = payload.get("examples", [payload])
    else:
        raise SchemaError("corpus must be a JSON array or object")
    if not isinstance(entries, list):
        raise SchemaError("corpus examples must be a list")
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise SchemaError("every corpus example must be an object")
    return entries


def _reject_duplicate_keys(pairs: Iterable[Tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SchemaError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> Any:
    raise SchemaError(f"non-finite JSON constant {value!r} is not allowed")


def _reject_duplicate_case_ids(cases: Sequence[ClinicalCase]) -> None:
    seen = set()
    for case in cases:
        if case.case_id in seen:
            raise SchemaError(f"duplicate case id {case.case_id!r}")
        seen.add(case.case_id)


def _file_sha256(path: str, limit: int) -> str:
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        raise AssetIdentityError("asset is not readable") from exc
    if size > limit:
        raise AssetIdentityError(f"asset exceeds {limit} bytes")
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


