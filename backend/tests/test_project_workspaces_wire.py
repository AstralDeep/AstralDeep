"""Exercises whole-manifest validation and immutable project context without granting host authority."""

import copy
import importlib

import pytest


PROJECT = "12345678-1234-1234-9234-123456789abc"


def manifest():
    return {
        "generation": 1,
        "projects": [
            {
                "project_id": PROJECT,
                "grant_revision": 2,
                "roots": [
                    {"name": "Primary", "path": "/tmp/one"},
                    {"name": "Second", "path": "/tmp/two"},
                ],
            }
        ],
    }


def module():
    return importlib.import_module("shared.project_workspaces")


def test_manifest_preserves_non_v4_identity_and_order():
    wire = module()
    value = wire.ProjectManifest.from_dict(manifest())
    assert value.to_dict() == manifest()
    assert value.projects[0].roots[0].path == "/tmp/one"
    assert (
        value.digest == wire.ProjectManifest.from_dict(copy.deepcopy(manifest())).digest
    )
    changed = manifest()
    changed["projects"][0]["roots"].reverse()
    assert wire.ProjectManifest.from_dict(changed).digest != value.digest


@pytest.mark.parametrize(
    "field,value", [("generation", x) for x in (0, -1, 1.5, True, 2**53, None, "1")]
)
def test_invalid_manifest_generation_is_not_coerced(field, value):
    wire = module()
    data = manifest()
    data[field] = value
    with pytest.raises(wire.ProjectWorkspaceError):
        wire.ProjectManifest.from_dict(data)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda x: x.update(extra=True),
        lambda x: x["projects"].append(x["projects"][0]),
        lambda x: x["projects"][0].update(project_id=PROJECT.upper()),
        lambda x: x["projects"][0].update(grant_revision=True),
        lambda x: x["projects"][0].update(roots=[{"name": "Root", "path": "/"}]),
        lambda x: x["projects"][0].update(
            roots=[{"name": "n" * 129, "path": "/tmp/one"}]
        ),
        lambda x: x["projects"][0].update(
            roots=[{"name": "Root", "path": "/tmp//one"}]
        ),
        lambda x: x["projects"][0].update(
            roots=[{"name": "Root", "path": "/tmp/../one"}]
        ),
        lambda x: x["projects"][0].update(
            roots=[{"name": "Root", "path": "/tmp/one/"}]
        ),
        lambda x: x["projects"][0]["roots"][0].update(extra=True),
        lambda x: x["projects"][0].update(roots=x["projects"][0]["roots"] * 9),
    ],
)
def test_malformed_manifest_rejects_whole_payload(mutate):
    wire = module()
    data = manifest()
    mutate(data)
    with pytest.raises(wire.ProjectWorkspaceError):
        wire.ProjectManifest.from_dict(data)


def test_context_preserves_legacy_chat_bytes_and_rejects_forged_fields():
    wire = module()
    data = {
        "chat_id": " legacy\nchat ",
        "project_id": PROJECT,
        "grant_revision": 2,
        "manifest_generation": 1,
    }
    assert wire.ProjectContext.from_dict(data).to_dict() == data
    for key, value in [
        ("grant_revision", 0),
        ("manifest_generation", True),
        ("chat_id", ""),
        ("project_id", "x"),
    ]:
        with pytest.raises(wire.ProjectWorkspaceError):
            wire.ProjectContext.from_dict({**data, key: value})
    assert (
        wire.ProjectContext.from_dict({**data, "grant_revision": None}).grant_revision
        is None
    )


@pytest.mark.parametrize(
    "count,roots,long", [(65, 0, False), (17, 16, False), (16, 16, True)]
)
def test_whole_manifest_limits(count, roots, long):
    wire = module()
    value = {
        "generation": 1,
        "projects": [
            {
                "project_id": f"12345678-1234-1234-9234-{index:012x}",
                "grant_revision": 1,
                "roots": [
                    {
                        "name": "Root",
                        "path": f"/tmp/{root}/" + ("界" * 1000 if long else "folder"),
                    }
                    for root in range(roots)
                ],
            }
            for index in range(count)
        ],
    }
    with pytest.raises(wire.ProjectWorkspaceError):
        wire.ProjectManifest.from_dict(value)


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        {},
        {"generation": 1, "projects": {}},
        {"generation": 1, "projects": [None]},
        {
            "generation": 1,
            "projects": [{"project_id": PROJECT, "grant_revision": 1, "roots": {}}],
        },
    ],
)
def test_shape_errors_fail_closed(value):
    wire = module()
    with pytest.raises(wire.ProjectWorkspaceError):
        wire.ProjectManifest.from_dict(value)


def test_empty_manifest_tombstones_and_missing_project():
    wire = module()
    value = wire.ProjectManifest.from_dict({"generation": 9, "projects": []})
    assert value.generation == 9
    assert value.project(PROJECT) is None
    full = wire.ProjectManifest.from_dict(manifest())
    assert full.project(PROJECT) == full.projects[0]
    value = manifest()
    value["projects"][0]["roots"] = []
    assert wire.ProjectManifest.from_dict(value).projects[0].grant_revision == 2


def test_unencodable_surrogate_is_refused():
    wire = module()
    value = manifest()
    value["projects"][0]["roots"][0]["name"] = "\ud800"
    with pytest.raises(wire.ProjectWorkspaceError):
        wire.ProjectManifest.from_dict(value)


def test_shared_fixture_digest_and_context():
    import json
    from pathlib import Path

    wire = module()
    fixture = json.loads(
        (Path(__file__).parent / "fixtures/project_workspaces_wire.json").read_text()
    )
    assert (
        wire.ProjectManifest.from_dict(fixture["manifest"]).digest == fixture["sha256"]
    )
    assert (
        wire.ProjectContext.from_dict(fixture["context"]).to_dict()
        == fixture["context"]
    )
    value = manifest()
    value["projects"][0]["roots"].append(value["projects"][0]["roots"][0])
    with pytest.raises(wire.ProjectWorkspaceError):
        wire.ProjectManifest.from_dict(value)
