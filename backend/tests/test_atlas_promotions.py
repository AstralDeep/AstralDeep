"""Tests for the pure Atlas promotion model (personalization/atlas_promotions.py):
bounded source snapshots, deterministic sections, and owner/project/expiry-bound
approval tokens with tamper and malformed-input refusal.
"""

from __future__ import annotations

import json
from uuid import uuid4

from cryptography.fernet import Fernet
import pytest

from personalization.atlas_pages import AtlasPageError
from personalization.atlas_promotions import (
    PROMOTION_FORMAT,
    PROMOTION_TTL_MS,
    PromotionSource,
    canonical_source_identifier,
    normalize_source_revision,
    normalize_source_value,
    open_proposal,
    promotion_body,
    promotion_section,
    promotion_source_references,
    promotion_title,
    proposal_digest,
    seal_proposal,
    source_reference,
    source_snapshot,
)


def _source(**changes):
    values = {
        "kind": "note",
        "source_id": str(uuid4()),
        "revision": 3,
        "category": "preference",
        "value": "Keep answers concise.",
    }
    values.update(changes)
    return source_snapshot(**values)


def _payload(source=None, **changes):
    source = source or _source()
    values = {
        "v": 1,
        "format": PROMOTION_FORMAT,
        "proposal_id": str(uuid4()),
        "owner": "owner-a",
        "project": "project-a",
        "action": "create",
        "sources": [
            {
                "kind": source.kind,
                "id": source.source_id,
                "revision": source.revision,
                "category": source.category,
                "digest": source.digest,
            }
        ],
        "destination": {
            "page_id": str(uuid4()),
            "slug": "owner-instructions",
            "revision": None,
            "fingerprint": None,
        },
        "title": "Owner instructions",
        "body": promotion_body((source,), title="Owner instructions", existing_body=None),
        "source_references": [source_reference(source)],
        "created": 1_000,
        "expires": 1_000 + PROMOTION_TTL_MS,
    }
    values.update(changes)
    return values


def test_source_snapshot_is_bound_to_identity_revision_category_and_value():
    first = _source()
    assert first.digest == _source(
        kind="note",
        source_id=first.source_id,
        revision=first.revision,
        category=first.category,
        value=first.value,
    ).digest
    assert first.digest != _source(
        source_id=first.source_id,
        revision=4,
        category=first.category,
        value=first.value,
    ).digest
    assert source_reference(first) == f"note:{first.source_id}@3"
    with pytest.raises(AtlasPageError):
        _source(kind="page")
    with pytest.raises(AtlasPageError):
        _source(source_id="not-a-uuid")
    with pytest.raises(AtlasPageError):
        _source(revision=True)
    with pytest.raises(AtlasPageError):
        _source(value="   ")
    with pytest.raises(AtlasPageError):
        _source(value="x" * 4097)


def test_source_normalizers_refuse_uppercase_uuids_and_unbounded_revisions():
    identifier = str(uuid4())
    assert canonical_source_identifier(identifier) == identifier
    with pytest.raises(AtlasPageError):
        canonical_source_identifier(identifier.upper())
    assert normalize_source_revision(None, required=False) is None
    with pytest.raises(AtlasPageError):
        normalize_source_revision(2**53)
    assert normalize_source_value(" first\r\nsecond ") == "first\nsecond"
    with pytest.raises(AtlasPageError):
        normalize_source_value("bad\x00value")


def test_sections_are_deterministic_and_append_preserves_owner_text():
    note = _source(value="Keep answers concise.")
    memory = _source(
        kind="memory", category="goal", value="Ship the Atlas promotion workflow."
    )
    section = promotion_section((note,), title="Owner instructions")
    assert "Keep answers concise." in section
    assert promotion_body((note,), title="Owner instructions", existing_body=None) == section
    existing = "Owner-authored instruction that must remain unchanged."
    combined = promotion_body((memory,), title="Owner instructions", existing_body=existing)
    assert combined.startswith(existing + "\n\n")
    assert "Ship the Atlas promotion workflow." in combined
    assert promotion_title((memory,), None) == "Promoted Goal Memory"
    with pytest.raises(AtlasPageError):
        promotion_body((note,), title="x", existing_body="y" * 8192)


def test_source_references_are_deduplicated_and_bounded():
    first = _source()
    assert promotion_source_references((first, first)) == (source_reference(first),)
    assert promotion_source_references(
        (first,), existing=(source_reference(first), "https://example.invalid")
    ) == (source_reference(first), "https://example.invalid")
    many = tuple(
        _source(source_id=str(uuid4()), value=f"Fact {index}.") for index in range(9)
    )
    with pytest.raises(AtlasPageError):
        promotion_source_references(many)


def test_tokens_round_trip_and_are_bound_to_owner_project_and_expiry():
    fernet = Fernet(Fernet.generate_key())
    payload = _payload()
    token = seal_proposal(fernet, payload)
    opened = open_proposal(
        fernet,
        token,
        owner_id="owner-a",
        project_id="project-a",
        now_ms=1_000,
    )
    assert opened == payload
    assert proposal_digest(opened) == proposal_digest(payload)
    with pytest.raises(AtlasPageError):
        open_proposal(
            fernet,
            token,
            owner_id="owner-b",
            project_id="project-a",
            now_ms=1_000,
        )
    with pytest.raises(AtlasPageError):
        open_proposal(
            fernet,
            token,
            owner_id="owner-a",
            project_id="project-b",
            now_ms=1_000,
        )
    with pytest.raises(AtlasPageError) as expired:
        open_proposal(
            fernet,
            token,
            owner_id="owner-a",
            project_id="project-a",
            now_ms=1_000 + PROMOTION_TTL_MS,
        )
    assert expired.value.code == "promotion_expired"


def test_tokens_refuse_tampering_wrong_keys_and_malformed_payloads():
    fernet = Fernet(Fernet.generate_key())
    payload = _payload()
    token = seal_proposal(fernet, payload)
    with pytest.raises(AtlasPageError):
        open_proposal(
            Fernet(Fernet.generate_key()),
            token,
            owner_id="owner-a",
            project_id="project-a",
            now_ms=1_000,
        )
    with pytest.raises(AtlasPageError):
        open_proposal(
            fernet,
            token[:-8] + "AAAAAAAA",
            owner_id="owner-a",
            project_id="project-a",
            now_ms=1_000,
        )
    for bad in ("", "not-a-token", None, 1):
        with pytest.raises(AtlasPageError):
            open_proposal(
                fernet,
                bad,
                owner_id="owner-a",
                project_id="project-a",
                now_ms=1_000,
            )
    for change in (
        {"project": "other"},
        {"owner": "other"},
        {"sources": []},
        {"sources": [{"kind": "note"}]},
        {"destination": {"page_id": str(uuid4()), "slug": "x"}},
        {"action": "replace"},
        {"expires": 1_000},
    ):
        broken = dict(payload)
        broken.update(change)
        forged = seal_proposal(fernet, broken)
        with pytest.raises(AtlasPageError):
            open_proposal(
                fernet,
                forged,
                owner_id="owner-a",
                project_id="project-a",
                now_ms=1_000,
            )


def test_tokens_reject_overlong_and_noncanonical_source_values():
    fernet = Fernet(Fernet.generate_key())
    payload = _payload()
    payload["sources"][0]["id"] = payload["sources"][0]["id"].upper()
    with pytest.raises(AtlasPageError):
        open_proposal(
            fernet,
            seal_proposal(fernet, payload),
            owner_id="owner-a",
            project_id="project-a",
            now_ms=1_000,
        )
    source = _source()
    with pytest.raises(AtlasPageError):
        seal_proposal(fernet, _payload(body="x" * 1_000_000, sources=[
            {
                "kind": source.kind,
                "id": source.source_id,
                "revision": source.revision,
                "category": source.category,
                "digest": source.digest,
            }
        ]))
    assert json.loads(
        json.dumps({"kind": source.kind, "id": source.source_id})
    ) == {"kind": "note", "id": source.source_id}


def test_promotion_source_is_repr_safe():
    source = PromotionSource(
        kind="note",
        source_id=str(uuid4()),
        revision=1,
        category="preference",
        digest="0" * 64,
        value="private note value",
    )
    assert "private note value" not in repr(source)

def test_source_and_section_boundaries_are_typed():
    with pytest.raises(AtlasPageError):
        _source(value=object())
    with pytest.raises(AtlasPageError):
        _source(value="bad\ud800")
    with pytest.raises(AtlasPageError):
        _source(category="")
    with pytest.raises(AtlasPageError):
        promotion_title((), None)
    first = _source()
    second = _source(kind="memory", category="context", value="Second.")
    assert promotion_title((first, second), None) == "Promoted Atlas sources"
    assert promotion_title((first,), "  Custom title  ") == "Custom title"
    with pytest.raises(AtlasPageError):
        promotion_section((), title="Empty")


def test_token_sealing_rejects_untyped_inputs_and_open_rejects_non_objects():
    fernet = Fernet(Fernet.generate_key())
    with pytest.raises(AtlasPageError):
        seal_proposal(fernet, [])
    with pytest.raises(AtlasPageError):
        seal_proposal("not-a-fernet", {})
    token = fernet.encrypt(b"[]").decode()
    with pytest.raises(AtlasPageError):
        open_proposal(
            fernet,
            token,
            owner_id="owner-a",
            project_id="project-a",
            now_ms=1_000,
        )
