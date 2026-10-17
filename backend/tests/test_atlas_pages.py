"""Tests for the pure Atlas page model (personalization/atlas_pages.py): normalization
bounds, stable page identities, owner-bound Fernet envelopes, and tamper-evident
ciphertext openings.
"""

from __future__ import annotations

import sys
import base64
import json
from pathlib import Path
from uuid import uuid4

from cryptography.fernet import Fernet
import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from personalization.atlas_pages import (  # noqa: E402
    FORMAT,
    KIND_PAGE,
    KIND_REVISION,
    MAX_BODY_BYTES,
    MAX_CIPHERTEXT_BYTES,
    MAX_HISTORY,
    MAX_LINKS,
    MAX_SLUG_CHARACTERS,
    MAX_SOURCES,
    PageCipher,
    PageSealed,
    AtlasPageError,
    canonical_page_id,
    decode_page_cursor,
    encode_page_cursor,
    envelope_bytes,
    envelope_fingerprint,
    normalize_body,
    normalize_project,
    normalize_slug,
    normalize_sources,
    normalize_title,
    page_identity,
    page_keywords,
    parse_envelope,
    revision_identity,
)


def test_page_cursor_round_trips_with_exact_owner_project_and_global_scope():
    page_id = str(uuid4())
    for project in ("project", None):
        cursor = encode_page_cursor("owner", project, 123, page_id)
        assert decode_page_cursor(cursor, "owner", project) == (123, page_id)
        for owner, wrong_project in [("foreign", project), ("owner", "different")]:
            with pytest.raises(AtlasPageError, match="page_cursor_invalid"):
                decode_page_cursor(cursor, owner, wrong_project)
    assert decode_page_cursor(None, "owner", None) is None


@pytest.mark.parametrize("value", ["", "x" * 4097, "!", "a", "YQ", "YR", "Yg", "W10", 1, True])
def test_page_cursor_refuses_malformed_or_oversized_tokens(value):
    with pytest.raises(AtlasPageError, match="page_cursor_invalid"):
        decode_page_cursor(value, "owner", "project")


@pytest.mark.parametrize("depth", [20, 1100])
def test_page_cursor_refuses_nested_json_within_token_bound(depth):
    raw = ("[" * depth + "0" + "]" * depth).encode()
    value = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    assert len(value) < 4096
    with pytest.raises(AtlasPageError, match="page_cursor_invalid"):
        decode_page_cursor(value, "owner", "project")


@pytest.mark.parametrize("change", [
    {"v": True}, {"v": 2}, {"created": True}, {"created": -1},
    {"created": 2 ** 63}, {"id": "invalid"}, {"id": 1}, {"extra": "x"},
])
def test_page_cursor_refuses_unbound_schema_and_types(change):
    payload = {"v": 1, "owner": "owner", "project": "project", "created": 1, "id": str(uuid4())}
    payload.update(change)
    value = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    with pytest.raises(AtlasPageError, match="page_cursor_invalid"):
        decode_page_cursor(value, "owner", "project")


def _sealed(**changes):
    values = dict(
        page_id=page_identity("owner-1", "proj", "welcome"),
        owner_id="owner-1",
        project_id="proj",
        slug="welcome",
        title="Welcome",
        revision=1,
        sources=("https://example.invalid/a",),
        ciphertext="",
    )
    values.update(changes)
    return PageSealed(**values)


def test_slug_normalization_accepts_bounded_ascii_and_rejects_garbage():
    assert normalize_slug("My Page!") == "my-page"
    assert normalize_slug("  Ärger-- Boulevard  ") == "arger-boulevard"
    with pytest.raises(AtlasPageError):
        normalize_slug("A")
    assert normalize_slug("-leading-and-trailing-") == "leading-and-trailing"
    with pytest.raises(AtlasPageError):
        normalize_slug("!!")
    with pytest.raises(AtlasPageError):
        normalize_slug("x" * (MAX_SLUG_CHARACTERS + 1))
    with pytest.raises(AtlasPageError):
        normalize_slug(42)


def test_title_and_body_normalization_enforce_bounds_and_control_characters():
    assert normalize_title("  Rundown  ") == "Rundown"
    assert normalize_body("a\r\nb\rc") == "a\nb\nc"
    with pytest.raises(AtlasPageError):
        normalize_title(" " * 4)
    with pytest.raises(AtlasPageError):
        normalize_body("x" * (MAX_BODY_BYTES + 1))
    with pytest.raises(AtlasPageError):
        normalize_title("bad\x00null")
    with pytest.raises(AtlasPageError):
        normalize_body("tab\tallowed\nnewline\n\x0bvertical")


def test_sources_are_bounded_deduplicated_and_stripped():
    assert normalize_sources([" a ", "a", "b"]) == ("a", "b")
    assert normalize_sources(None) == ()
    assert normalize_sources([]) == ()
    with pytest.raises(AtlasPageError):
        normalize_sources(["s"] * (MAX_SOURCES + 1))
    with pytest.raises(AtlasPageError):
        normalize_sources(["x" * 513])
    with pytest.raises(AtlasPageError):
        normalize_sources("not-a-list")


def test_normalize_project_maps_global_marker_to_none():
    assert normalize_project(None) is None
    assert normalize_project("-") is None
    assert normalize_project("  ") is None
    assert normalize_project("ops") == "ops"
    with pytest.raises(AtlasPageError):
        normalize_project("p" * 129)


def test_page_identity_is_stable_per_owner_project_slug_and_differs_across_scopes():
    first = page_identity("owner-1", "proj", "welcome")
    assert first == page_identity("owner-1", "proj", "welcome")
    assert canonical_page_id(first) == first
    assert page_identity("owner-2", "proj", "welcome") != first
    assert page_identity("owner-1", "proj2", "welcome") != first
    assert page_identity("owner-1", None, "welcome") != first
    assert page_identity("owner-1", "proj", "farewell") != first
    with pytest.raises(AtlasPageError):
        page_identity("owner-1", "proj", "")


def test_revision_identity_is_deterministic_per_token():
    page_id = page_identity("owner-1", "proj", "welcome")
    assert revision_identity(page_id, 2, "tok") == revision_identity(page_id, 2, "tok")
    assert revision_identity(page_id, 3, "tok") != revision_identity(page_id, 2, "tok")


def test_cipher_round_trips_the_body_against_the_exact_page_identity():
    cipher = PageCipher(Fernet(Fernet.generate_key()))
    sealed = _sealed()
    token = cipher.seal(sealed, "Project kickoff notes.")
    assert 0 < len(token) <= MAX_CIPHERTEXT_BYTES
    opened = cipher.open(_sealed(ciphertext=token), token)
    assert opened == "Project kickoff notes."


def test_cipher_rejects_cross_page_and_cross_owner_replay():
    cipher = PageCipher(Fernet(Fernet.generate_key()))
    token = cipher.seal(_sealed(), "Secret project plan.")
    replayed = _sealed(ciphertext=token, revision=2)
    with pytest.raises(AtlasPageError) as conflict:
        cipher.open(replayed, token)
    assert conflict.value.status_code == 503
    tampered_owner = _sealed(ciphertext=token, owner_id="owner-2")
    with pytest.raises(AtlasPageError):
        cipher.open(tampered_owner, token)


def test_cipher_fails_closed_on_foreign_key_and_corrupt_ciphertext():
    cipher = PageCipher(Fernet(Fernet.generate_key()))
    token = cipher.seal(_sealed(), "Plan.")
    other = PageCipher(Fernet(Fernet.generate_key()))
    with pytest.raises(AtlasPageError) as failure:
        other.open(_sealed(ciphertext=token), token)
    assert failure.value.status_code == 503
    with pytest.raises(AtlasPageError):
        cipher.open(_sealed(ciphertext=token), token[:-8] + "AAAAAA==")
    with pytest.raises(AtlasPageError):
        PageCipher("not-a-fernet")


def test_seal_rejects_oversized_bodies():
    cipher = PageCipher(Fernet(Fernet.generate_key()))
    with pytest.raises(AtlasPageError) as bound:
        cipher.seal(_sealed(), "x" * (MAX_BODY_BYTES * 3))
    assert bound.value.code == "page_body_out_of_bounds"


def test_envelope_round_trips_through_parse_and_binds_kind_and_revision():
    cipher = PageCipher(Fernet(Fernet.generate_key()))
    token = cipher.seal(_sealed(), "Content.")
    envelope = envelope_bytes(_sealed(ciphertext=token), kind=KIND_PAGE, prev_revision_id=None)
    parsed = parse_envelope(envelope)
    assert parsed is not None
    assert parsed["format"] == FORMAT
    assert parsed["kind"] == KIND_PAGE
    assert parsed["revision"] == 1
    assert parsed["body"] == token
    revision = envelope_bytes(
        _sealed(ciphertext=token, revision=1),
        kind=KIND_REVISION,
        prev_revision_id="0" * 36,
    )
    assert parse_envelope(revision)["kind"] == KIND_REVISION


def test_parse_envelope_rejects_non_atlas_values_and_malformed_records():
    assert parse_envelope("remember: user likes tea") is None
    assert parse_envelope(None) is None
    assert parse_envelope({"body": "not-a-string-value"}) is None
    assert parse_envelope('{"body": "x", "format": "other/v9", "kind": "page", '
                         '"page_id": "p", "owner_id": "o", "revision": "one"}') is None
    assert parse_envelope('{"body": 5, "format": "%s", "kind": "page", '
                         '"page_id": "p", "owner_id": "o", "revision": 1}' % FORMAT) is None


def test_envelope_bytes_enforce_the_store_bound():
    cipher = PageCipher(Fernet(Fernet.generate_key()))
    token = cipher.seal(_sealed(), "x" * (MAX_BODY_BYTES - 512))
    maximal = envelope_bytes(
        _sealed(ciphertext=token, sources=("s" * 512,) * MAX_SOURCES),
        kind=KIND_PAGE,
        prev_revision_id="r" * 64,
    )
    assert len(maximal) <= MAX_CIPHERTEXT_BYTES
    assert parse_envelope(maximal) is not None
    with pytest.raises(AtlasPageError) as bound:
        envelope_bytes(
            _sealed(ciphertext="x" * (MAX_CIPHERTEXT_BYTES + 64)),
            kind=KIND_PAGE,
            prev_revision_id=None,
        )
    assert bound.value.code == "page_out_of_bounds"


def test_envelope_bytes_reject_unknown_kinds():
    with pytest.raises(AtlasPageError):
        envelope_bytes(_sealed(), kind="mystery", prev_revision_id=None)


def test_fingerprint_is_stable_and_covers_content_and_revision():
    base = {"page_id": "p", "revision": 1, "title": "t", "slug": "s", "body": "b"}
    assert envelope_fingerprint(base) == envelope_fingerprint(dict(base))
    assert envelope_fingerprint(base) != envelope_fingerprint({**base, "revision": 2})
    assert envelope_fingerprint(base) != envelope_fingerprint({**base, "body": "c"})


def test_page_keywords_are_slug_and_title_tokens():
    assert page_keywords("kickoff-notes", "Project Kickoff") == "kickoff notes project"
    assert page_keywords("ab", "Tiny") == "tiny"


def test_unencodable_and_non_string_inputs_are_rejected_cleanly():
    with pytest.raises(AtlasPageError):
        normalize_title("\ud800lone-surrogate")
    with pytest.raises(AtlasPageError):
        normalize_body(None)
    with pytest.raises(AtlasPageError):
        normalize_slug(None)
    with pytest.raises(AtlasPageError):
        page_identity("owner-1", 42, "slug")
    with pytest.raises(AtlasPageError):
        page_identity("", "proj", "slug")
    with pytest.raises(AtlasPageError):
        page_identity("owner-1", "proj", "   ")


def test_cipher_guards_non_typed_inputs_and_undecryptable_payloads():
    cipher = PageCipher(Fernet(Fernet.generate_key()))
    with pytest.raises(AtlasPageError):
        cipher.seal("not-a-page", "body")
    with pytest.raises(AtlasPageError):
        cipher.seal(_sealed(), 42)
    with pytest.raises(AtlasPageError):
        cipher.open(_sealed(), 42)
    with pytest.raises(AtlasPageError) as corrupt:
        cipher.open(_sealed(), "@@@not-a-token@@@")
    assert corrupt.value.status_code == 503


def test_parse_envelope_rejects_overlong_and_wrong_shaped_records():
    assert parse_envelope("{" + '"body":' + '"x"' * 9000 + "}") is None
    assert parse_envelope("not json at all") is None
    assert parse_envelope('{"body":"b","format":"astral.atlas-page/v1","kind":"other",'
                         '"page_id":"p","owner_id":"o","revision":1}') is None


def test_history_and_link_bounds_are_bounded_by_design():
    assert 1 <= MAX_HISTORY <= 50
    assert 1 <= MAX_LINKS <= 64
    assert uuid4() is not None
