"""Covers shared/node_identity.py: profile selection, key creation and permissions, EdDSA token minting and refusal, issuer owner ids for the server profile, and the mesh-id owner rules of a personal node."""

from __future__ import annotations

import base64

import pytest

from shared import node_identity as ni


def second_node(tmp_path, monkeypatch):
    monkeypatch.setenv(ni.DATA_DIR_ENV, str(tmp_path / "other"))
    return ni.load_node_identity()


def b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def test_unwritable_data_directory_is_reported(tmp_path, monkeypatch):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setenv(ni.PROFILE_ENV, "personal")
    monkeypatch.setenv(ni.DATA_DIR_ENV, str(blocker / "sub"))
    ni.reset_cache()
    with pytest.raises(ni.NodeKeyError):
        ni.load_node_identity()


MESH = "sha256:" + "a" * 64
STORED = "mesh." + "a" * 64
OTHER = "sha256:" + "b" * 64
LEGACY = "oidc.0123456789abcdef.owner"


def test_the_mesh_id_and_its_stored_owner_id_convert_exactly_both_ways():
    assert ni.owner_id_for_mesh(MESH) == STORED
    assert ni.mesh_id_for_owner(STORED) == MESH
    every_digit = "sha256:" + "0123456789abcdef" * 4
    assert ni.mesh_id_for_owner(ni.owner_id_for_mesh(every_digit)) == every_digit


@pytest.mark.parametrize("value", [
    None, 7, "", STORED, LEGACY, "sha256:" + "A" * 64, "sha256:" + "a" * 63, "sha256:" + "a" * 65,
    "sha256-" + "a" * 64, " " + MESH, MESH + "\n",
])
def test_owner_id_for_mesh_refuses_anything_but_a_kernel_mesh_id(value):
    with pytest.raises(ni.NodeIdentityError):
        ni.owner_id_for_mesh(value)


@pytest.mark.parametrize("value", [
    None, 7, "", MESH, LEGACY, "mesh." + "A" * 64, "mesh." + "a" * 63, "mesh:" + "a" * 64, STORED + " ",
])
def test_mesh_id_for_owner_refuses_anything_but_a_stored_mesh_owner(value):
    with pytest.raises(ni.NodeIdentityError):
        ni.mesh_id_for_owner(value)


def test_no_generic_owner_writer_remains():
    assert not hasattr(ni, "bind_owner")


def test_enforce_owner_is_a_no_op_outside_the_personal_profile(tmp_path, monkeypatch):
    monkeypatch.delenv(ni.PROFILE_ENV, raising=False)
    monkeypatch.setenv(ni.DATA_DIR_ENV, str(tmp_path / "n"))
    ni.enforce_owner("anyone")
    assert not (tmp_path / "n").exists()


def test_mesh_owner_binding_is_personal_only(tmp_path, monkeypatch):
    monkeypatch.setenv(ni.DATA_DIR_ENV, str(tmp_path / "n"))
    for profile in ("", "institution"):
        monkeypatch.setenv(ni.PROFILE_ENV, profile)
        with pytest.raises(ni.NodeIdentityError):
            ni.bind_mesh_owner(MESH)
    assert not (tmp_path / "n").exists()


def test_unwritable_owner_binding_is_reported(tmp_path, monkeypatch):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setenv(ni.PROFILE_ENV, "personal")
    monkeypatch.setenv(ni.DATA_DIR_ENV, str(blocker / "sub"))
    monkeypatch.delenv(ni.OWNER_ENV, raising=False)
    with pytest.raises(ni.NodeIdentityError):
        ni.bind_mesh_owner(MESH)


def test_principal_hash_is_stable():
    assert ni.owner_principal_hash("o") == ni.owner_principal_hash("o")
    assert ni.owner_principal_hash("o").startswith("sha256:")


