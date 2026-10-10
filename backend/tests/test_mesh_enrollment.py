"""One-time owner-confirmed mesh enrollment: real-Postgres store tests plus API
flow tests covering creation, confirmation, possession-bound redemption, replay,
expiry, revocation, isolation and concurrent redemption.
"""

from __future__ import annotations

import base64
import json
import threading
import time
import uuid
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.testclient import TestClient

from orchestrator import mesh_api
from orchestrator import mesh_enrollment as me
from orchestrator.mesh_api import mesh_router
from tests.helpers.voice_plane_runtime import isolated_plane_runtime

OWNER = "owner-api"
_SECURITY = HTTPBearer(auto_error=False)


@pytest.fixture(scope="module")
def plane_runtime():
    with isolated_plane_runtime("mesh_enrollment") as runtime:
        yield runtime


@pytest.fixture(autouse=True)
def encryption_key(monkeypatch):
    monkeypatch.setenv(
        me._MESHCREDENTIAL_KEY_SOURCE, "uR3Wb5rK8pHq2nTxE1sYd0cV7mLwZa4Gf9OiUjBhNkQ="
    )


@pytest.fixture()
def store(plane_runtime) -> me.MeshEnrollmentStore:
    return me.MeshEnrollmentStore(
        plane_runtime=plane_runtime,
        plane_repositories=plane_runtime.repositories,
    )


@pytest.fixture()
def audit_events(monkeypatch) -> list:
    events: list = []

    class _Recorder:
        def record_blocking(self, event):
            events.append(event)
            return None

    monkeypatch.setattr("audit.recorder.get_recorder", lambda: _Recorder())
    return events


def _device() -> tuple[dict[str, str], Ed25519PrivateKey]:
    private_key = Ed25519PrivateKey.generate()
    raw = private_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    jwk = {"kty": "OKP", "crv": "Ed25519", "x": me._b64url_encode(raw)}
    return jwk, private_key


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _payload_fields(payload: str) -> dict[str, str]:
    return json.loads(_b64url_decode(payload))


def _signature_b64(private_key: Ed25519PrivateKey, challenge_b64: str) -> str:
    return me._b64url_encode(private_key.sign(_b64url_decode(challenge_b64)))


def _make_invitation(
    store: me.MeshEnrollmentStore,
    owner_id: str = OWNER,
    *,
    label: str = "Kitchen tablet",
    scopes: list[str] | None = None,
) -> tuple[dict, dict[str, str], Ed25519PrivateKey]:
    jwk, private_key = _device()
    result = store.create_invitation(
        owner_id,
        label=label,
        device_key=jwk,
        scopes=scopes if scopes is not None else ["tools:read", "mesh:confirm"],
        creator_kind="owner",
        creator_id=owner_id,
    )
    return result, _payload_fields(result["payload"]), private_key


def _confirm(
    store: me.MeshEnrollmentStore, fields: dict[str, str], *, kind: str = "owner",
    decider_id: str | None = None,
) -> dict:
    return store.decide_invitation(
        fields["o"],
        fields["i"],
        decision="confirmed",
        decider_kind=kind,
        decider_id=decider_id or fields["o"],
    )


def _confirm_and_redeem(
    store: me.MeshEnrollmentStore,
    invitation: dict,
    fields: dict[str, str],
    private_key: Ed25519PrivateKey,
) -> dict:
    _confirm(store, fields)
    return store.redeem_invitation(
        owner_id=fields["o"],
        invite_id=fields["i"],
        token=fields["t"],
        challenge=fields["c"],
        signature=_signature_b64(private_key, fields["c"]),
    )


def _member_header(owner_id: str, member_id: str, member_key: str) -> dict[str, str]:
    return {"X-Astral-Member-Key": f"{owner_id}.{member_id}.{member_key}"}


# --- key handling and input validation --------------------------------------


def test_load_or_create_key_file_is_stable(tmp_path):
    path = str(tmp_path / "keys" / ".credential_key")
    first = me.load_or_create_key_file(path)
    second = me.load_or_create_key_file(path)
    assert first == second
    assert len(first) > 30


def test_normalize_device_key_rejects_non_ed25519():
    with pytest.raises(ValueError):
        me.normalize_device_key({"kty": "EC", "crv": "P-256", "x": "aaa"})
    with pytest.raises(ValueError):
        me.normalize_device_key("not-a-jwk")
    with pytest.raises(ValueError):
        me.normalize_device_key({"kty": "OKP", "crv": "Ed25519", "x": "####"})
    with pytest.raises(ValueError):
        me.normalize_device_key({"kty": "OKP", "crv": "Ed25519", "x": "a"})


def test_key_file_fallback_encrypts_without_environment(tmp_path, monkeypatch):
    monkeypatch.delenv(me._MESHCREDENTIAL_KEY_SOURCE, raising=False)
    assert me._key_file_path().endswith(".credential_key")
    monkeypatch.setattr(me, "_key_file_path", lambda: str(tmp_path / "data" / ".credential_key"))
    first = me._fernet()
    second = me._fernet()
    token = first.encrypt(b"roundtrip")
    assert second.decrypt(token) == b"roundtrip"
    record_ciphertext = first.encrypt(json.dumps({"ok": True}).encode()).decode("ascii")
    assert me._decode_record(record_ciphertext) == {"ok": True}


def test_normalize_scopes_rejects_unknown_scope():
    assert me.normalize_scopes(["tools:read", "tools:read", "mesh:confirm"]) == [
        "mesh:confirm",
        "tools:read",
    ]
    with pytest.raises(ValueError):
        me.normalize_scopes(["tools:admin"])
    with pytest.raises(ValueError):
        me.normalize_scopes("tools:read")


def test_create_invitation_validates_input(store):
    jwk, _ = _device()
    with pytest.raises(ValueError):
        store.create_invitation(
            OWNER, label="", device_key=jwk, scopes=[], creator_kind="owner",
            creator_id=OWNER,
        )
    with pytest.raises(ValueError):
        store.create_invitation(
            OWNER, label="x" * 100, device_key=jwk, scopes=[],
            creator_kind="owner", creator_id=OWNER,
        )
    with pytest.raises(ValueError):
        store.create_invitation(
            OWNER, label="ok", device_key=jwk, scopes=["tools:admin"],
            creator_kind="owner", creator_id=OWNER,
        )
    with pytest.raises(ValueError):
        store.create_invitation(
            OWNER, label="ok", device_key=jwk, scopes=[], creator_kind="owner",
            creator_id=OWNER, ttl_seconds=5,
        )
    with pytest.raises(ValueError):
        store.create_invitation(
            OWNER, label="ok", device_key=jwk, scopes=[], creator_kind="owner",
            creator_id=OWNER, ttl_seconds=10_000_000,
        )
    with pytest.raises(ValueError):
        store.create_invitation(
            OWNER, label=None, device_key=jwk, scopes=[], creator_kind="owner",
            creator_id=OWNER,
        )
    with pytest.raises(ValueError):
        store.create_invitation(
            OWNER, label="ok", device_key=jwk, scopes=[], creator_kind="robot",
            creator_id=OWNER,
        )


def test_non_dict_record_fails_closed(store):
    result, fields, _ = _make_invitation(store, "owner-listjson")
    store._credentials.call(
        store._credentials.repository.upsert_credential,
        owner_id="owner-listjson",
        agent_id=me.INVITATION_NAMESPACE,
        credential_key=fields["i"],
        encrypted_value=me._fernet().encrypt(b"[1,2]").decode("ascii"),
        updated_at=1,
    )
    with pytest.raises(me.MeshEnrollmentError):
        store.get_invitation("owner-listjson", fields["i"])


# --- creation binding --------------------------------------------------------


def test_invitation_binds_mesh_owner_key_and_challenge(store):
    result, fields, _ = _make_invitation(store, "owner-bind")
    invitation = result["invitation"]
    assert invitation["status"] == "pending"
    assert invitation["mesh_id"] == me.mesh_id_for_owner("owner-bind")
    assert fields["o"] == "owner-bind"
    assert fields["i"] == invitation["invite_id"]
    stored = store.get_invitation("owner-bind", invitation["invite_id"])
    assert stored["token_sha256"] == me._sha256_hex(_b64url_decode(fields["t"]))
    assert stored["challenge_sha256"] == me._sha256_hex(_b64url_decode(fields["c"]))
    assert stored["created_by"] == {"kind": "owner", "id": "owner-bind"}
    assert stored["expires_at"] > stored["created_at"]


def test_list_invitations_returns_owner_scoped_records(store):
    _make_invitation(store, "owner-list-a")
    _make_invitation(store, "owner-list-a")
    _make_invitation(store, "owner-list-b")
    assert len(store.list_invitations("owner-list-a")) == 2
    assert len(store.list_invitations("owner-list-b")) == 1


# --- confirmation authority --------------------------------------------------


def test_owner_confirmation_fixes_scopes(store):
    result, fields, _ = _make_invitation(
        store, "owner-conf", scopes=["tools:read", "tools:system"]
    )
    record = _confirm(store, fields)
    assert record["status"] == "confirmed"
    assert record["confirmed_scopes"] == ["tools:read", "tools:system"]
    assert record["confirmed_by"] == {"kind": "owner", "id": "owner-conf"}


def test_member_confirmation_attenuates_to_member_scopes(store):
    bootstrap, bootstrap_fields, bootstrap_key = _make_invitation(
        store, "owner-atten", scopes=["tools:read", "mesh:confirm"]
    )
    member = _confirm_and_redeem(
        store, bootstrap["invitation"], bootstrap_fields, bootstrap_key
    )
    member_id = member["member"]["member_id"]

    jwk, _ = _device()
    wide = store.create_invitation(
        "owner-atten",
        label="wide device",
        device_key=jwk,
        scopes=["tools:read", "tools:execute", "mesh:confirm"],
        creator_kind="owner",
        creator_id="owner-atten",
    )
    record = store.decide_invitation(
        "owner-atten",
        wide["invitation"]["invite_id"],
        decision="confirmed",
        decider_kind="member",
        decider_id=member_id,
    )
    assert record["confirmed_scopes"] == ["mesh:confirm", "tools:read"]


def test_member_cannot_confirm_own_invitation(store):
    bootstrap, bootstrap_fields, bootstrap_key = _make_invitation(
        store, "owner-selfconfirm", scopes=["mesh:confirm"]
    )
    member = _confirm_and_redeem(
        store, bootstrap["invitation"], bootstrap_fields, bootstrap_key
    )
    member_id = member["member"]["member_id"]

    jwk, _ = _device()
    own = store.create_invitation(
        "owner-selfconfirm",
        label="member proposed",
        device_key=jwk,
        scopes=[],
        creator_kind="member",
        creator_id=member_id,
    )
    with pytest.raises(me.ConfirmForbidden):
        store.decide_invitation(
            "owner-selfconfirm",
            own["invitation"]["invite_id"],
            decision="confirmed",
            decider_kind="member",
            decider_id=member_id,
        )
    record = store.decide_invitation(
        "owner-selfconfirm",
        own["invitation"]["invite_id"],
        decision="confirmed",
        decider_kind="owner",
        decider_id="owner-selfconfirm",
    )
    assert record["status"] == "confirmed"


def test_decide_twice_is_rejected(store):
    _, fields, _ = _make_invitation(store, "owner-twice")
    _confirm(store, fields)
    with pytest.raises(me.InvitationStateInvalid):
        store.decide_invitation(
            "owner-twice", fields["i"], decision="rejected",
            decider_kind="owner", decider_id="owner-twice",
        )


def test_decide_rejects_unknown_decision(store):
    _, fields, _ = _make_invitation(store, "owner-baddecision")
    with pytest.raises(ValueError):
        store.decide_invitation(
            "owner-baddecision", fields["i"], decision="maybe",
            decider_kind="owner", decider_id="owner-baddecision",
        )


def test_member_confirmation_of_unknown_member_denied(store):
    _, fields, _ = _make_invitation(store, "owner-ghostmember")
    with pytest.raises(me.MemberNotFound):
        store.decide_invitation(
            "owner-ghostmember", fields["i"], decision="confirmed",
            decider_kind="member", decider_id="missing-member",
        )


def test_concurrent_confirmation_conflict_denied(store, monkeypatch):
    from astralplane.repositories import RepositoryConflictError

    _, fields, _ = _make_invitation(store, "owner-confirmrace")

    def conflicting(*args, **kwargs):
        raise RepositoryConflictError("stale")

    monkeypatch.setattr(
        store._credentials.repository, "compare_and_set_ciphertext", conflicting
    )
    with pytest.raises(me.InvitationStateInvalid):
        _confirm(store, fields)
    assert store.get_invitation(fields["o"], fields["i"]) is not None


def test_decide_expired_invitation_fails(store, monkeypatch):
    _, fields, _ = _make_invitation(store, "owner-expdecide")
    monkeypatch.setattr(
        me, "_now_ms", lambda: int(time.time() * 1000) + 60 * 60 * 1000
    )
    with pytest.raises(me.InvitationExpired):
        _confirm(store, fields)


# --- redemption possession and state ----------------------------------------


def test_full_activation_grants_scopes_and_member_key(store):
    result, fields, private_key = _make_invitation(
        store, "owner-full", scopes=["tools:read", "mesh:confirm"]
    )
    outcome = _confirm_and_redeem(store, result["invitation"], fields, private_key)
    member = outcome["member"]
    assert member["status"] == "active"
    assert member["mesh_id"] == me.mesh_id_for_owner("owner-full")
    assert member["scopes"] == ["mesh:confirm", "tools:read"]
    assert (
        member["device_key_fingerprint"]
        == result["invitation"]["device_key_fingerprint"]
    )
    assert len(outcome["member_key"]) >= 32
    stored = store.get_invitation("owner-full", fields["i"])
    assert stored["status"] == "redeemed"
    assert stored["member_id"] == member["member_id"]
    assert store.get_member("owner-full", member["member_id"]) is not None


def test_replay_of_redeemed_invitation_denied(store):
    result, fields, private_key = _make_invitation(store, "owner-replay")
    _confirm_and_redeem(store, result["invitation"], fields, private_key)
    with pytest.raises(me.InvitationStateInvalid):
        store.redeem_invitation(
            owner_id=fields["o"],
            invite_id=fields["i"],
            token=fields["t"],
            challenge=fields["c"],
            signature=_signature_b64(private_key, fields["c"]),
        )
    assert len(store.list_members("owner-replay")) == 1


def test_stolen_link_without_possession_denied(store):
    result, fields, _ = _make_invitation(store, "owner-stolen")
    attacker = Ed25519PrivateKey.generate()
    _confirm(store, fields)
    with pytest.raises(me.RedeemInvalid):
        store.redeem_invitation(
            owner_id=fields["o"],
            invite_id=fields["i"],
            token=fields["t"],
            challenge=fields["c"],
            signature=_signature_b64(attacker, fields["c"]),
        )
    with pytest.raises(me.RedeemInvalid):
        store.redeem_invitation(
            owner_id=fields["o"],
            invite_id=fields["i"],
            token=fields["t"],
            challenge=fields["c"],
            signature=me._b64url_encode(attacker.sign(b"0" * 32)),
        )
    assert len(store.list_members("owner-stolen")) == 0


def test_wrong_token_denied(store):
    result, fields, private_key = _make_invitation(store, "owner-token")
    _confirm(store, fields)
    with pytest.raises(me.RedeemInvalid):
        store.redeem_invitation(
            owner_id=fields["o"],
            invite_id=fields["i"],
            token=me._b64url_encode(b"9" * 32),
            challenge=fields["c"],
            signature=_signature_b64(private_key, fields["c"]),
        )


def test_wrong_challenge_denied(store):
    result, fields, private_key = _make_invitation(store, "owner-challenge")
    _confirm(store, fields)
    with pytest.raises(me.RedeemInvalid):
        store.redeem_invitation(
            owner_id=fields["o"],
            invite_id=fields["i"],
            token=fields["t"],
            challenge=me._b64url_encode(b"7" * 32),
            signature=_signature_b64(private_key, fields["c"]),
        )
    assert len(store.list_members("owner-challenge")) == 0


def test_malformed_redemption_fields_denied(store):
    result, fields, private_key = _make_invitation(store, "owner-badb64")
    _confirm(store, fields)
    with pytest.raises(me.RedeemInvalid):
        store.redeem_invitation(
            owner_id=fields["o"],
            invite_id=fields["i"],
            token="a",
            challenge=fields["c"],
            signature=_signature_b64(private_key, fields["c"]),
        )
    with pytest.raises(me.RedeemInvalid):
        store.redeem_invitation(
            owner_id=fields["o"],
            invite_id=fields["i"],
            token=fields["t"],
            challenge=fields["c"],
            signature="a",
        )
    assert len(store.list_members("owner-badb64")) == 0


def test_pending_and_rejected_invitations_denied(store):
    pending, pending_fields, private_key = _make_invitation(store, "owner-denial")
    with pytest.raises(me.InvitationStateInvalid):
        store.redeem_invitation(
            owner_id=pending_fields["o"],
            invite_id=pending_fields["i"],
            token=pending_fields["t"],
            challenge=pending_fields["c"],
            signature=_signature_b64(private_key, pending_fields["c"]),
        )

    rejected, rejected_fields, rejected_key = _make_invitation(store, "owner-denial")
    store.decide_invitation(
        "owner-denial", rejected_fields["i"], decision="rejected",
        decider_kind="owner", decider_id="owner-denial",
    )
    with pytest.raises(me.InvitationStateInvalid):
        store.redeem_invitation(
            owner_id=rejected_fields["o"],
            invite_id=rejected_fields["i"],
            token=rejected_fields["t"],
            challenge=rejected_fields["c"],
            signature=_signature_b64(rejected_key, rejected_fields["c"]),
        )
    assert len(store.list_members("owner-denial")) == 0


def test_expired_invitation_denied_at_redemption(store, monkeypatch):
    result, fields, private_key = _make_invitation(store, "owner-expired")
    _confirm(store, fields)
    monkeypatch.setattr(
        me, "_now_ms", lambda: int(time.time() * 1000) + 2 * 60 * 60 * 1000
    )
    with pytest.raises(me.InvitationExpired):
        store.redeem_invitation(
            owner_id=fields["o"],
            invite_id=fields["i"],
            token=fields["t"],
            challenge=fields["c"],
            signature=_signature_b64(private_key, fields["c"]),
        )


def test_unknown_invitation_denied(store):
    with pytest.raises(me.InvitationNotFound):
        store.redeem_invitation(
            owner_id="owner-unknown",
            invite_id=uuid.uuid4().hex,
            token=me._b64url_encode(b"0" * 32),
            challenge=me._b64url_encode(b"0" * 32),
            signature=me._b64url_encode(b"0" * 64),
        )


def test_malformed_payload_denied():
    with pytest.raises(me.RedeemInvalid):
        me.parse_enrollment_payload(None)
    with pytest.raises(me.RedeemInvalid):
        me.parse_enrollment_payload("x" * 5000)
    with pytest.raises(me.RedeemInvalid):
        me.parse_enrollment_payload(me._b64url_encode(b"not-json"))
    with pytest.raises(me.RedeemInvalid):
        me.parse_enrollment_payload(me._b64url_encode(json.dumps({"v": 2}).encode()))


# --- atomicity ---------------------------------------------------------------


def test_lost_race_denies_redemption_and_leaves_no_member(store, monkeypatch):
    result, fields, private_key = _make_invitation(store, "owner-race")
    _confirm(store, fields)
    owner_id = fields["o"]
    invite_id = fields["i"]
    original_get = store._get
    live = original_get(owner_id, me.INVITATION_NAMESPACE, invite_id)

    def racing_get(race_owner, namespace, key):
        if namespace == me.INVITATION_NAMESPACE and key == invite_id:
            concurrent = {
                **live,
                "status": "redeemed",
                "member_id": "someone-else",
                "updated_at": int(live["updated_at"]) + 5,
            }
            store._put(owner_id, me.INVITATION_NAMESPACE, invite_id, concurrent)
            return live
        return original_get(race_owner, namespace, key)

    monkeypatch.setattr(store, "_get", racing_get)
    with pytest.raises(me.InvitationStateInvalid):
        store.redeem_invitation(
            owner_id=owner_id,
            invite_id=invite_id,
            token=fields["t"],
            challenge=fields["c"],
            signature=_signature_b64(private_key, fields["c"]),
        )
    monkeypatch.setattr(store, "_get", original_get)
    assert len(store.list_members(owner_id)) == 0
    assert store.get_invitation(owner_id, invite_id)["status"] == "redeemed"


def test_two_threads_race_redeem_exactly_one_wins(store):
    result, fields, private_key = _make_invitation(store, "owner-threads")
    _confirm(store, fields)
    signature = _signature_b64(private_key, fields["c"])
    outcomes: list[str] = []
    errors: list[Exception] = []
    start = threading.Barrier(2)

    def attempt():
        try:
            start.wait(timeout=10)
            store.redeem_invitation(
                owner_id=fields["o"],
                invite_id=fields["i"],
                token=fields["t"],
                challenge=fields["c"],
                signature=signature,
            )
            outcomes.append("won")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
            outcomes.append("lost")

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert outcomes.count("won") == 1
    assert errors == [] or all(
        isinstance(error, me.InvitationStateInvalid) for error in errors
    )
    assert len(store.list_members("owner-threads")) == 1


# --- revocation and isolation ------------------------------------------------


def test_revoked_member_loses_authority(store):
    result, fields, private_key = _make_invitation(store, "owner-revoke")
    outcome = _confirm_and_redeem(store, result["invitation"], fields, private_key)
    member_id = outcome["member"]["member_id"]
    assert store.authenticate_member("owner-revoke", member_id, outcome["member_key"])
    store.revoke_member("owner-revoke", member_id)
    with pytest.raises(me.MemberUnauthorized):
        store.authenticate_member("owner-revoke", member_id, outcome["member_key"])
    with pytest.raises(me.MemberUnauthorized):
        store.authenticate_member("owner-revoke", member_id, "wrong-key")
    revoked = store.get_member("owner-revoke", member_id)
    assert revoked["status"] == "revoked"
    assert revoked["revoked_at"] is not None
    store.revoke_member("owner-revoke", member_id)


def test_member_of_unknown_owner_not_found(store):
    with pytest.raises(me.MemberNotFound):
        store.authenticate_member("owner-nobody", uuid.uuid4().hex, "key")


def test_authenticate_member_rejects_malformed_credential(store):
    with pytest.raises(me.MemberUnauthorized):
        store.authenticate_member("", "", "")
    with pytest.raises(me.MemberUnauthorized):
        store.authenticate_member(None, None, None)


def test_owner_isolation_on_invitations_and_members(store):
    result, fields, private_key = _make_invitation(store, "owner-iso-a")
    with pytest.raises(me.InvitationNotFound):
        store.get_invitation("owner-iso-b", fields["i"])
    with pytest.raises(me.MemberNotFound):
        store.get_member("owner-iso-b", "missing")
    outcome = _confirm_and_redeem(store, result["invitation"], fields, private_key)
    member_ids = [m["member_id"] for m in store.list_members("owner-iso-a")]
    assert outcome["member"]["member_id"] in member_ids
    assert store.list_members("owner-iso-b") == []


# --- origin guard ------------------------------------------------------------


def test_origin_guard_rejects_structurally_invalid_values():
    with pytest.raises(HTTPException) as padded:
        mesh_api._origin_of(" http://testserver ")
    assert padded.value.status_code == 403
    with pytest.raises(HTTPException):
        mesh_api._origin_of("http://user:pass@testserver")
    with pytest.raises(HTTPException):
        mesh_api._origin_of("http://testserver/enroll#frag")
    with pytest.raises(HTTPException):
        mesh_api._origin_of("ftp://testserver")
    with pytest.raises(HTTPException):
        mesh_api._origin_of("http://testserver:99999")
    assert mesh_api._origin_of("http://testserver/x", base=True) == (
        "http",
        "testserver",
        80,
    )
    assert mesh_api._origin_of("https://testserver") == ("https", "testserver", 443)


# --- API flow ----------------------------------------------------------------


@pytest.fixture()
def api_client(plane_runtime):
    owner_id = f"owner-{uuid.uuid4().hex[:10]}"
    app = FastAPI()
    app.include_router(mesh_router)

    def fake_source(request: Request) -> Any:
        return plane_runtime

    def fake_owner_claims(
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Depends(_SECURITY),
    ) -> dict | None:
        if "x-astral-member-key" in request.headers:
            return None
        return {"sub": owner_id, "realm_access": {"roles": ["user"]}}

    app.dependency_overrides[mesh_api._plane_source] = fake_source
    app.dependency_overrides[mesh_api._owner_claims_optional] = fake_owner_claims
    client = TestClient(app, raise_server_exceptions=True)
    client.owner_id = owner_id
    return client


def _owner_headers() -> dict[str, str]:
    return {"Authorization": "Bearer test-token"}


def test_api_requires_authentication(plane_runtime, monkeypatch):
    monkeypatch.delenv("USE_MOCK_AUTH", raising=False)
    app = FastAPI()
    app.include_router(mesh_router)
    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/api/mesh/invitations")
    assert response.status_code == 401


def test_api_full_flow_owner_creates_confirms_device_redeems(api_client, audit_events):
    jwk, private_key = _device()
    created = api_client.post(
        "/api/mesh/invitations",
        json={
            "label": "Living-room hub",
            "device_key": jwk,
            "scopes": ["tools:read", "mesh:confirm"],
            "ttl_seconds": 600,
        },
        headers=_owner_headers(),
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["invitation"]["status"] == "pending"
    fields = _payload_fields(body["payload"])

    confirmed = api_client.post(
        f"/api/mesh/invitations/{fields['i']}/confirm",
        headers=_owner_headers(),
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["invitation"]["confirmed_scopes"] == [
        "mesh:confirm",
        "tools:read",
    ]

    redeemed = api_client.post(
        "/api/mesh/enrollment/redeem",
        json={
            "payload": body["payload"],
            "signature": _signature_b64(private_key, fields["c"]),
        },
    )
    assert redeemed.status_code == 200, redeemed.text
    redeemed_body = redeemed.json()
    assert redeemed_body["member"]["status"] == "active"
    assert redeemed_body["member_key"]
    assert redeemed.headers.get("cache-control") == "no-store"

    listed = api_client.get("/api/mesh/members", headers=_owner_headers())
    assert listed.status_code == 200
    assert [m["member_id"] for m in listed.json()["members"]] == [
        redeemed_body["member"]["member_id"]
    ]

    myself = api_client.get(
        "/api/mesh/me",
        headers=_member_header(
            api_client.owner_id,
            redeemed_body["member"]["member_id"],
            redeemed_body["member_key"],
        ),
    )
    assert myself.status_code == 200
    assert myself.json()["member"]["label"] == "Living-room hub"

    actions = [event.action_type for event in audit_events]
    assert "mesh.invitation.create" in actions
    assert "mesh.invitation.confirm" in actions
    assert "mesh.member.redeem" in actions


def test_api_replay_denied_with_failure_audit(api_client, audit_events):
    jwk, private_key = _device()
    created = api_client.post(
        "/api/mesh/invitations",
        json={"label": "Pad", "device_key": jwk, "scopes": ["tools:read"]},
        headers=_owner_headers(),
    )
    fields = _payload_fields(created.json()["payload"])
    api_client.post(
        f"/api/mesh/invitations/{fields['i']}/confirm", headers=_owner_headers()
    )
    signature = _signature_b64(private_key, fields["c"])
    assert (
        api_client.post(
            "/api/mesh/enrollment/redeem",
            json={"payload": created.json()["payload"], "signature": signature},
        ).status_code
        == 200
    )
    second = api_client.post(
        "/api/mesh/enrollment/redeem",
        json={"payload": created.json()["payload"], "signature": signature},
    )
    assert second.status_code == 409
    failures = [
        event
        for event in audit_events
        if event.action_type == "mesh.member.redeem" and event.outcome == "failure"
    ]
    assert failures
    assert failures[-1].inputs_meta["reason"] == "invitation_state_invalid"


def test_api_member_authority_matrix(api_client):
    jwk, private_key = _device()
    bootstrap = api_client.post(
        "/api/mesh/invitations",
        json={"label": "Confirming device", "device_key": jwk, "scopes": ["mesh:confirm"]},
        headers=_owner_headers(),
    )
    fields = _payload_fields(bootstrap.json()["payload"])
    api_client.post(
        f"/api/mesh/invitations/{fields['i']}/confirm", headers=_owner_headers()
    )
    member = api_client.post(
        "/api/mesh/enrollment/redeem",
        json={
            "payload": bootstrap.json()["payload"],
            "signature": _signature_b64(private_key, fields["c"]),
        },
    ).json()
    member_headers = _member_header(
        api_client.owner_id, member["member"]["member_id"], member["member_key"]
    )

    peer_jwk, _ = _device()
    member_created = api_client.post(
        "/api/mesh/invitations",
        json={"label": "Member proposed", "device_key": peer_jwk, "scopes": []},
        headers=member_headers,
    )
    assert member_created.status_code == 200, member_created.text
    member_created_id = _payload_fields(member_created.json()["payload"])["i"]

    self_confirm = api_client.post(
        f"/api/mesh/invitations/{member_created_id}/confirm", headers=member_headers
    )
    assert self_confirm.status_code == 403

    owner_confirms = api_client.post(
        f"/api/mesh/invitations/{member_created_id}/confirm",
        headers=_owner_headers(),
    )
    assert owner_confirms.status_code == 200, owner_confirms.text
    assert owner_confirms.json()["invitation"]["confirmed_by"]["kind"] == "owner"

    other_jwk, _ = _device()
    owner_created = api_client.post(
        "/api/mesh/invitations",
        json={"label": "Owner proposed", "device_key": other_jwk, "scopes": []},
        headers=_owner_headers(),
    )
    owner_created_id = _payload_fields(owner_created.json()["payload"])["i"]
    member_confirms = api_client.post(
        f"/api/mesh/invitations/{owner_created_id}/confirm", headers=member_headers
    )
    assert member_confirms.status_code == 200, member_confirms.text
    assert member_confirms.json()["invitation"]["confirmed_by"]["kind"] == "member"


def test_api_member_without_confirm_scope_is_forbidden(api_client):
    jwk, private_key = _device()
    bootstrap = api_client.post(
        "/api/mesh/invitations",
        json={"label": "Reader device", "device_key": jwk, "scopes": ["tools:read"]},
        headers=_owner_headers(),
    )
    fields = _payload_fields(bootstrap.json()["payload"])
    api_client.post(
        f"/api/mesh/invitations/{fields['i']}/confirm", headers=_owner_headers()
    )
    member = api_client.post(
        "/api/mesh/enrollment/redeem",
        json={
            "payload": bootstrap.json()["payload"],
            "signature": _signature_b64(private_key, fields["c"]),
        },
    ).json()
    headers = _member_header(
        api_client.owner_id, member["member"]["member_id"], member["member_key"]
    )

    invitations = api_client.get("/api/mesh/invitations", headers=headers)
    assert invitations.status_code == 403

    propose = api_client.post(
        "/api/mesh/invitations",
        json={"label": "x", "device_key": _device()[0], "scopes": []},
        headers=headers,
    )
    assert propose.status_code == 403


def test_api_revoked_member_credentials_die(api_client):
    jwk, private_key = _device()
    created = api_client.post(
        "/api/mesh/invitations",
        json={"label": "Revoked device", "device_key": jwk, "scopes": []},
        headers=_owner_headers(),
    )
    fields = _payload_fields(created.json()["payload"])
    api_client.post(
        f"/api/mesh/invitations/{fields['i']}/confirm", headers=_owner_headers()
    )
    redeemed = api_client.post(
        "/api/mesh/enrollment/redeem",
        json={
            "payload": created.json()["payload"],
            "signature": _signature_b64(private_key, fields["c"]),
        },
    ).json()
    member_id = redeemed["member"]["member_id"]
    key = redeemed["member_key"]
    owner_id = api_client.owner_id

    assert (
        api_client.get("/api/mesh/me", headers=_member_header(owner_id, member_id, key)).status_code
        == 200
    )
    revoked = api_client.post(
        f"/api/mesh/members/{member_id}/revoke", headers=_owner_headers()
    )
    assert revoked.status_code == 200
    assert (
        api_client.get("/api/mesh/me", headers=_member_header(owner_id, member_id, key)).status_code
        == 401
    )
    denied = api_client.post(
        "/api/mesh/invitations",
        json={"label": "x", "device_key": _device()[0], "scopes": []},
        headers=_member_header(owner_id, member_id, key),
    )
    assert denied.status_code == 401


def test_api_two_device_staging_success_and_denial(api_client):
    jwk_a, key_a = _device()
    jwk_b, key_b = _device()

    invite_a = api_client.post(
        "/api/mesh/invitations",
        json={"label": "Device A", "device_key": jwk_a, "scopes": ["tools:read"]},
        headers=_owner_headers(),
    )
    fields_a = _payload_fields(invite_a.json()["payload"])
    api_client.post(
        f"/api/mesh/invitations/{fields_a['i']}/confirm", headers=_owner_headers()
    )
    redeem_a = api_client.post(
        "/api/mesh/enrollment/redeem",
        json={
            "payload": invite_a.json()["payload"],
            "signature": _signature_b64(key_a, fields_a["c"]),
        },
    )
    assert redeem_a.status_code == 200

    invite_b = api_client.post(
        "/api/mesh/invitations",
        json={"label": "Device B", "device_key": jwk_b, "scopes": ["tools:read"]},
        headers=_owner_headers(),
    )
    fields_b = _payload_fields(invite_b.json()["payload"])
    api_client.post(
        f"/api/mesh/invitations/{fields_b['i']}/reject", headers=_owner_headers()
    )
    redeem_b = api_client.post(
        "/api/mesh/enrollment/redeem",
        json={
            "payload": invite_b.json()["payload"],
            "signature": _signature_b64(key_b, fields_b["c"]),
        },
    )
    assert redeem_b.status_code == 409
    assert redeem_b.json()["detail"] == "invitation_state_invalid"

    stolen = api_client.post(
        "/api/mesh/enrollment/redeem",
        json={
            "payload": invite_b.json()["payload"],
            "signature": _signature_b64(key_a, fields_b["c"]),
        },
    )
    assert stolen.status_code == 409

    members = api_client.get("/api/mesh/members", headers=_owner_headers()).json()
    assert [m["label"] for m in members["members"]] == ["Device A"]


def test_api_reject_then_double_reject_maps_error(api_client):
    jwk, _ = _device()
    created = api_client.post(
        "/api/mesh/invitations",
        json={"label": "Reject twice", "device_key": jwk, "scopes": []},
        headers=_owner_headers(),
    )
    fields = _payload_fields(created.json()["payload"])
    first = api_client.post(
        f"/api/mesh/invitations/{fields['i']}/reject", headers=_owner_headers()
    )
    assert first.status_code == 200, first.text
    assert first.json()["invitation"]["status"] == "rejected"
    second = api_client.post(
        f"/api/mesh/invitations/{fields['i']}/reject", headers=_owner_headers()
    )
    assert second.status_code == 409
    assert second.json()["detail"] == "invitation_state_invalid"


def test_api_write_guard_and_validation(api_client):
    missing_origin = api_client.post(
        "/api/mesh/invitations",
        json={"label": "x", "device_key": _device()[0], "scopes": []},
        headers={},
    )
    assert missing_origin.status_code == 403

    wrong_origin = api_client.post(
        "/api/mesh/invitations",
        json={"label": "x", "device_key": _device()[0], "scopes": []},
        headers={"Origin": "http://evil.example"},
    )
    assert wrong_origin.status_code == 403

    padded_origin = api_client.post(
        "/api/mesh/invitations",
        json={"label": "x", "device_key": _device()[0], "scopes": []},
        headers={"Origin": "http://testserver/?tracker=1"},
    )
    assert padded_origin.status_code == 403

    port_origin = api_client.post(
        "/api/mesh/invitations",
        json={"label": "x", "device_key": _device()[0], "scopes": []},
        headers={"Origin": "http://testserver:99999"},
    )
    assert port_origin.status_code == 403

    query_token = api_client.post(
        "/api/mesh/invitations?token=abc",
        json={"label": "x", "device_key": _device()[0], "scopes": []},
        headers=_owner_headers(),
    )
    assert query_token.status_code == 403

    form_encoded = api_client.post(
        "/api/mesh/invitations",
        data=json.dumps({"label": "x"}),
        headers={
            **_owner_headers(),
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    assert form_encoded.status_code == 415

    bad_key = api_client.post(
        "/api/mesh/invitations",
        json={"label": "x", "device_key": {"kty": "RSA"}, "scopes": []},
        headers=_owner_headers(),
    )
    assert bad_key.status_code == 400

    unknown_member = api_client.post(
        f"/api/mesh/members/{uuid.uuid4().hex}/revoke", headers=_owner_headers()
    )
    assert unknown_member.status_code == 404

    malformed_header = api_client.get(
        "/api/mesh/me", headers={"X-Astral-Member-Key": "only-two.parts"}
    )
    assert malformed_header.status_code == 401

    bad_redeem = api_client.post(
        "/api/mesh/enrollment/redeem",
        json={"payload": None, "signature": ""},
    )
    assert bad_redeem.status_code == 400


def test_api_link_uses_public_base_url(api_client, monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://mesh.example.com")
    jwk, _ = _device()
    created = api_client.post(
        "/api/mesh/invitations",
        json={"label": "Linked", "device_key": jwk, "scopes": []},
        headers=_owner_headers(),
    )
    link = created.json()["link"]
    assert link is not None
    assert link.startswith("https://mesh.example.com/enroll#")
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("BACKEND_PUBLIC_URL", raising=False)
    without = api_client.post(
        "/api/mesh/invitations",
        json={"label": "Linked 2", "device_key": jwk, "scopes": []},
        headers=_owner_headers(),
    )
    assert without.json()["link"] is None


def test_audit_failure_is_swallowed(store, monkeypatch):
    class _Broken:
        def record_blocking(self, event):
            raise RuntimeError("audit down")

    monkeypatch.setattr("audit.recorder.get_recorder", lambda: _Broken())
    me.audit_mesh_event("owner-x", "mesh.test", "should not raise")


def test_member_with_confirm_scope_reads_roster(api_client):
    jwk, private_key = _device()
    bootstrap = api_client.post(
        "/api/mesh/invitations",
        json={"label": "Confirming device", "device_key": jwk, "scopes": ["mesh:confirm"]},
        headers=_owner_headers(),
    )
    fields = _payload_fields(bootstrap.json()["payload"])
    api_client.post(
        f"/api/mesh/invitations/{fields['i']}/confirm", headers=_owner_headers()
    )
    member = api_client.post(
        "/api/mesh/enrollment/redeem",
        json={
            "payload": bootstrap.json()["payload"],
            "signature": _signature_b64(private_key, fields["c"]),
        },
    ).json()
    headers = _member_header(
        api_client.owner_id, member["member"]["member_id"], member["member_key"]
    )

    invitations = api_client.get("/api/mesh/invitations", headers=headers)
    assert invitations.status_code == 200
    assert len(invitations.json()["invitations"]) == 1

    members = api_client.get("/api/mesh/members", headers=headers)
    assert members.status_code == 200
    assert len(members.json()["members"]) == 1


def test_undecryptable_record_fails_closed(store):
    result, fields, _ = _make_invitation(store, "owner-corrupt")
    store._credentials.call(
        store._credentials.repository.upsert_credential,
        owner_id="owner-corrupt",
        agent_id=me.INVITATION_NAMESPACE,
        credential_key=fields["i"],
        encrypted_value="not-a-fernet-token",
        updated_at=1,
    )
    with pytest.raises(me.MeshEnrollmentError):
        store.get_invitation("owner-corrupt", fields["i"])


def test_device_key_validation_edge_cases():
    with pytest.raises(ValueError):
        me.normalize_device_key({"kty": "OKP", "crv": "Ed25519"})
    with pytest.raises(ValueError):
        me.normalize_device_key({"kty": "OKP", "crv": "Ed25519", "x": "!!!"})
    short = me._b64url_encode(b"0" * 16)
    with pytest.raises(ValueError):
        me.normalize_device_key({"kty": "OKP", "crv": "Ed25519", "x": short})


def test_scopeless_defaults_to_no_authority(store):
    jwk, private_key = _device()
    result = store.create_invitation(
        "owner-scopeless",
        label="empty scopes",
        device_key=jwk,
        scopes=None,
        creator_kind="owner",
        creator_id="owner-scopeless",
    )
    fields = _payload_fields(result["payload"])
    outcome = _confirm_and_redeem(store, result["invitation"], fields, private_key)
    assert outcome["member"]["scopes"] == []


def test_signature_body_validation(api_client):
    jwk, _ = _device()
    created = api_client.post(
        "/api/mesh/invitations",
        json={"label": "Sig device", "device_key": jwk, "scopes": []},
        headers=_owner_headers(),
    )
    payload = created.json()["payload"]
    long_signature = "a" * 300
    response = api_client.post(
        "/api/mesh/enrollment/redeem",
        json={"payload": payload, "signature": long_signature},
    )
    assert response.status_code == 400
