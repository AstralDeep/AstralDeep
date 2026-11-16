"""Tests the draft validator against the committed drafts and against deliberately broken copies."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import validate_drafts as v  # noqa: E402

URL = f"{v.SITE_ORIGIN}/{v.METADATA_PATH}"
METADATA = json.loads((HERE / v.METADATA_PATH).read_text())
ASSOCIATION = json.loads((HERE / v.ASSOCIATION_PATH).read_text())


def test_committed_drafts_validate() -> None:
    assert v.validate(HERE) == []


def test_association_names_bundle_team_and_callback_path() -> None:
    app_ids = ASSOCIATION["applinks"]["details"][0]["appIDs"]
    assert app_ids == ["593LGH35GS.com.personalailabs.astraldeep"]
    assert ASSOCIATION["webcredentials"]["apps"] == app_ids
    assert ASSOCIATION["applinks"]["details"][0]["components"] == [{"/": "/oauth/callback"}]
    assert METADATA["redirect_uris"] == ["https://astraldeep.github.io/oauth/callback"]


@pytest.mark.parametrize("field", ["client_id", "client_name", "redirect_uris"])
def test_missing_required_field_is_reported(field: str) -> None:
    broken = copy.deepcopy(METADATA)
    del broken[field]
    assert any(field in p for p in v.check_metadata(broken, URL))


@pytest.mark.parametrize("client_id", [
    "http://astraldeep.github.io/oauth/client-metadata.json",
    "https://astraldeep.github.io",
    "https://astraldeep.github.io/oauth/../client-metadata.json",
    "https://astraldeep.github.io/oauth/other.json",
])
def test_client_id_must_be_the_https_document_url(client_id: str) -> None:
    broken = {**METADATA, "client_id": client_id}
    assert v.check_metadata(broken, URL)


@pytest.mark.parametrize("uri", [
    "astral://oauth/callback",
    "http://astraldeep.github.io/oauth/callback",
    "https://astraldeep.github.io/oauth/*",
    "https://astraldeep.github.io/oauth/callback#frag",
])
def test_redirect_uri_rules(uri: str) -> None:
    assert v.check_metadata({**METADATA, "redirect_uris": [uri]}, URL)


def test_loopback_http_redirect_is_allowed() -> None:
    assert v.check_metadata({**METADATA, "redirect_uris": ["http://127.0.0.1:8123/callback"]}, URL) == []


@pytest.mark.parametrize("name", v.SECRET_FIELDS)
def test_secret_fields_are_rejected(name: str) -> None:
    assert any(name in p for p in v.check_metadata({**METADATA, name: "x"}, URL))


def test_shared_secret_auth_method_is_rejected() -> None:
    assert v.check_metadata({**METADATA, "token_endpoint_auth_method": "client_secret_basic"}, URL)


def test_association_rejects_wrong_bundle_or_unclaimed_path() -> None:
    wrong_bundle = copy.deepcopy(ASSOCIATION)
    wrong_bundle["applinks"]["details"][0]["appIDs"] = ["593LGH35GS.com.example.other"]
    assert v.check_association(wrong_bundle, METADATA["redirect_uris"])
    wrong_path = copy.deepcopy(ASSOCIATION)
    wrong_path["applinks"]["details"][0]["components"] = [{"/": "/other"}]
    assert v.check_association(wrong_path, METADATA["redirect_uris"])
    assert v.check_association([], [])
