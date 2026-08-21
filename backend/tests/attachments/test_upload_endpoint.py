"""HTTP contract tests for /api/upload and /api/attachments.

We mount the attachments_router into a fresh FastAPI app, stub out the
``require_user_id`` dependency to return a configurable test user, and stub the
repository factory to return a StubDatabase-backed repo. This exercises the
real router code (validation, sniffing-aware path, JSON shapes) without
needing a running PostgreSQL or Keycloak.
"""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from astralplane import BlobSizeLimitError
from astralplane.errors import PlaneError
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from orchestrator.attachments import router as attachments_router_module
from orchestrator.attachments.repository import AttachmentRepository
from orchestrator.attachments.router import attachments_router
from orchestrator.auth import require_user_id
from orchestrator.plane_repository_context import ApplicationPlaneSource

from .conftest import (
    StubDatabase,
    seed_attachment_for_test,
    soft_delete_attachment_for_test,
)


class _PlaneRuntime:
    def __init__(self, repositories):
        self.repositories = repositories

    @contextmanager
    def transaction(self):
        yield object()


class _ImmediatePurgeCoordinator:
    """Router-level durable-acceptance double; recovery has separate proofs."""

    def __init__(self, repo, blob_store) -> None:
        self.repo = repo
        self.blob_store = blob_store
        self.scheduled: set[tuple[str, str]] = set()
        self.owner_cleanups: dict[tuple[str, str], object] = {}

    async def aschedule_attachment(self, *, owner_id, attachment_id):
        identity = (owner_id, attachment_id)
        if identity not in self.scheduled:
            if self.repo.get_by_id(attachment_id, owner_id) is None:
                raise PlaneError("not found", code="purge_object_not_found")
            assert soft_delete_attachment_for_test(
                self.repo,
                attachment_id=attachment_id,
                user_id=owner_id,
            )
            self.scheduled.add(identity)
        return SimpleNamespace(cleanup_id=f"purge-{owner_id}-{attachment_id}")

    async def aschedule_owner(self, *, owner_id):
        cleanup_id = f"purge-owner-{owner_id}"
        self.owner_cleanups[(owner_id, cleanup_id)] = SimpleNamespace(
            cleanup_id=cleanup_id,
            status="pending",
            requested_at=datetime(2026, 8, 21, tzinfo=UTC),
            attempt_count=0,
            verified_absent_at=None,
            last_error_code=None,
        )
        return SimpleNamespace(cleanup_id=cleanup_id)

    async def aowner_cleanup_status(self, *, owner_id, cleanup_id):
        return self.owner_cleanups.get((owner_id, cleanup_id))


class _MaterializationService:
    """Router test double for the single pending->READY production boundary."""

    def __init__(self, repo) -> None:
        self.repo = repo
        self.calls: list[dict[str, object]] = []
        self.failure: BaseException | None = None

    async def materialize_stream(self, **values):
        self.calls.append(values)
        if self.failure is not None:
            raise self.failure
        payload = bytearray()
        async for chunk in values["chunks"]:
            payload.extend(chunk)
            if len(payload) > values["max_bytes"]:
                raise BlobSizeLimitError("fixture upload exceeded its bound")
        content_type = values["resolve_content_type"](bytes(payload[:8192]))
        return seed_attachment_for_test(
            self.repo,
            attachment_id=values["attachment_id"],
            user_id=values["owner_id"],
            filename=values["filename"],
            content_type=content_type,
            category=values["category"],
            extension=values["extension"],
            size_bytes=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
            created_at=1_786_726_800_000,
        )


def test_repository_factory_uses_app_plane_and_fails_closed(stub_db):
    runtime = _PlaneRuntime(stub_db.plane_repositories)
    source = ApplicationPlaneSource(runtime, runtime.repositories)
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                orchestrator=SimpleNamespace(plane_repository_source=source)
            )
        )
    )
    repository = attachments_router_module._get_repository(request)
    assert repository.db is None

    request.app.state.orchestrator = object()
    with pytest.raises(HTTPException) as exc_info:
        attachments_router_module._get_repository(request)
    assert getattr(exc_info.value, "status_code", None) == 503

    injected = object()
    request.app.state.orchestrator = SimpleNamespace(attachment_repository=injected)
    assert attachments_router_module._get_repository(request) is injected

    request.app.state.orchestrator = None
    with pytest.raises(HTTPException) as exc_info:
        attachments_router_module._get_repository(request)
    assert exc_info.value.status_code == 503


def test_materialization_factory_fails_closed_without_application_composition():
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(orchestrator=None))
    )
    with pytest.raises(HTTPException) as exc_info:
        attachments_router_module._get_materialization_service(request)
    assert exc_info.value.status_code == 503

    injected = object()
    request.app.state.orchestrator = SimpleNamespace(
        attachment_materialization_service=injected
    )
    assert attachments_router_module._get_materialization_service(request) is injected


def test_purge_factory_requires_the_application_composition():
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(orchestrator=None))
    )
    with pytest.raises(HTTPException) as exc_info:
        attachments_router_module._get_purge_coordinator(request)
    assert exc_info.value.status_code == 503

    injected = object()
    request.app.state.orchestrator = SimpleNamespace(
        attachment_purge_coordinator=injected
    )
    assert attachments_router_module._get_purge_coordinator(request) is injected

    request.app.state.orchestrator = object()
    with pytest.raises(HTTPException) as exc_info:
        attachments_router_module._get_materialization_service(request)
    assert exc_info.value.status_code == 503


@pytest.fixture
def app(monkeypatch, stub_db: StubDatabase) -> FastAPI:
    """A minimal FastAPI app wired only with the attachments router."""
    app = FastAPI()
    app.include_router(attachments_router)
    app.state.orchestrator = SimpleNamespace()

    # Stub repo factory to bypass the orchestrator dependency.
    runtime = _PlaneRuntime(stub_db.plane_repositories)
    repo = AttachmentRepository(
        plane_runtime=runtime,
        plane_repositories=runtime.repositories,
    )
    app.state.orchestrator.attachment_purge_coordinator = _ImmediatePurgeCoordinator(
        repo,
        None,
    )
    app.state.orchestrator.attachment_materialization_service = (
        _MaterializationService(repo)
    )
    monkeypatch.setattr(
        attachments_router_module, "_get_repository", lambda request: repo,
    )

    # Default user override (per-test can re-override).
    app.dependency_overrides[require_user_id] = lambda: "user-A"

    return app


def _client(app: FastAPI) -> TestClient:
    return TestClient(app)


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_upload_returns_201_with_attachment_id(app):
    client = _client(app)
    res = client.post(
        "/api/upload",
        files={"file": ("notes.md", b"# hi\nthere", "text/markdown")},
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["filename"] == "notes.md"
    assert body["category"] == "text"
    assert body["extension"] == "md"
    assert body["size_bytes"] == len(b"# hi\nthere")
    assert len(body["sha256"]) == 64
    assert body["attachment_id"]


def test_upload_then_list_then_get_then_delete(app):
    client = _client(app)
    up = client.post(
        "/api/upload",
        files={"file": ("a.txt", b"hello", "text/plain")},
    ).json()
    aid = up["attachment_id"]

    listed = client.get("/api/attachments").json()
    assert len(listed["attachments"]) == 1
    assert listed["attachments"][0]["attachment_id"] == aid

    one = client.get(f"/api/attachments/{aid}")
    assert one.status_code == 200
    assert one.json()["attachment_id"] == aid

    deleted = client.delete(f"/api/attachments/{aid}")
    assert deleted.status_code == 202
    assert deleted.json() == {
        "status": "cleanup_pending",
        "cleanup_id": f"purge-user-A-{aid}",
    }
    assert client.get(f"/api/attachments/{aid}").status_code == 404
    assert client.get("/api/attachments").json()["attachments"] == []


def test_account_retirement_requires_deliberate_confirmation(app):
    response = _client(app).post(
        "/api/account/retirement",
        json={"confirmation": "logout"},
    )

    assert response.status_code == 422


def test_account_retirement_is_async_owner_bound_and_not_cached(app):
    client = _client(app)
    accepted = client.post(
        "/api/account/retirement",
        json={"confirmation": "retire-my-account"},
    )

    assert accepted.status_code == 202
    assert accepted.headers["cache-control"] == "no-store"
    assert accepted.json() == {
        "status": "cleanup_pending",
        "cleanup_id": "purge-owner-user-A",
    }

    cleanup = client.get("/api/account/retirement/purge-owner-user-A")
    assert cleanup.status_code == 200
    assert cleanup.headers["cache-control"] == "no-store"
    assert cleanup.json()["status"] == "pending"
    assert cleanup.json()["operator_action_required"] is False

    app.dependency_overrides[require_user_id] = lambda: "user-B"
    assert client.get("/api/account/retirement/purge-owner-user-A").status_code == 404


def test_upload_reports_parser_status_from_coverage_check(app, monkeypatch):
    """With an orchestrator on app state, the eager coverage check runs
    off-loop and its status lands in the response body (feature 031/052)."""
    from orchestrator import attachment_autoparse

    seen = {}

    def _coverage(orch, *, extension, category):
        seen.update(extension=extension, category=category)
        return {"status": "covered"}

    monkeypatch.setattr(attachment_autoparse, "coverage_status", _coverage)
    client = _client(app)
    res = client.post(
        "/api/upload",
        files={"file": ("notes.md", b"# hi", "text/markdown")},
    )
    assert res.status_code == 201
    assert res.json()["parser_status"] == "covered"
    assert seen == {"extension": "md", "category": "text"}


def test_upload_delegates_stream_and_policy_to_materialization_service(app):
    service = app.state.orchestrator.attachment_materialization_service
    response = _client(app).post(
        "/api/upload",
        files={"file": ("notes.md", b"# hi", "text/markdown")},
    )

    assert response.status_code == 201
    assert len(service.calls) == 1
    assert service.calls[0]["owner_id"] == "user-A"
    assert callable(service.calls[0]["resolve_content_type"])


# ---------------------------------------------------------------------------
# Negative paths
# ---------------------------------------------------------------------------


def test_upload_unsupported_extension_returns_415(app):
    client = _client(app)
    res = client.post("/api/upload", files={"file": ("blueprint.dwg", b"\x00\x01", "application/octet-stream")})
    assert res.status_code == 415
    assert "dwg" in res.json()["detail"].lower()


def test_upload_legacy_binary_format_returns_415(app):
    client = _client(app)
    # .doc is in LEGACY_BINARY_FORMATS — must be rejected at upload time.
    res = client.post("/api/upload", files={"file": ("legacy.doc", b"some bytes", "application/msword")})
    assert res.status_code == 415


def test_upload_oversize_returns_413(app, monkeypatch):
    """A file larger than the per-category cap is rejected with 413."""
    from orchestrator.attachments import content_type as ct

    monkeypatch.setitem(ct.MAX_BYTES_BY_CATEGORY, "text", 100)
    client = _client(app)
    res = client.post("/api/upload", files={"file": ("big.txt", b"x" * 200, "text/plain")})
    assert res.status_code == 413
    assert "upload limit" in res.json()["detail"].lower()


def test_upload_oversize_respects_per_category_caps(app, monkeypatch):
    """A medical-size file (>30 MB) that fits under the medical cap is accepted;
    the same size uploaded as a text file is rejected."""
    from orchestrator.attachments import content_type as ct

    # Shrink medical cap to make the test fast but still larger than the text cap.
    monkeypatch.setitem(ct.MAX_BYTES_BY_CATEGORY, "text", 1000)
    monkeypatch.setitem(ct.MAX_BYTES_BY_CATEGORY, "medical", 100_000)

    client = _client(app)
    payload = b"x" * 5000  # 5 KB: > text cap (1 KB), < medical cap (100 KB)

    # .txt (text category) → 413
    res_text = client.post(
        "/api/upload", files={"file": ("big.txt", payload, "text/plain")},
    )
    assert res_text.status_code == 413, res_text.text

    # .nii (medical category) → bypasses the smaller text cap.
    # We can't easily satisfy the libmagic sniff for NIfTI in a 5KB blob, but
    # the size check runs before the sniff so it'll pass the 413 gate. If it
    # fails, it must fail with 415 (mismatch), NOT 413 (oversize).
    res_med = client.post(
        "/api/upload", files={"file": ("big.nii", payload, "application/octet-stream")},
    )
    assert res_med.status_code != 413, res_med.text


def test_rejected_upload_never_becomes_visible(app, monkeypatch):
    monkeypatch.setattr(
        attachments_router_module.ct,
        "is_consistent",
        lambda _extension, _sniffed: False,
    )

    response = _client(app).post(
        "/api/upload",
        files={"file": ("notes.md", b"# hi", "text/markdown")},
    )

    assert response.status_code == 415
    assert _client(app).get("/api/attachments").json()["attachments"] == []


def test_plane_materialization_failure_returns_retryable_error(app):
    service = app.state.orchestrator.attachment_materialization_service
    service.failure = PlaneError("verification failed", code="blob_integrity_mismatch")
    response = _client(app).post(
        "/api/upload",
        files={"file": ("notes.md", b"# hi", "text/markdown")},
    )

    assert response.status_code == 503
    assert "materialization" in response.json()["detail"].lower()
    assert _client(app).get("/api/attachments").json()["attachments"] == []


def test_get_foreign_attachment_returns_404_not_403(app):
    """Non-owners must not be able to confirm existence."""
    client = _client(app)
    aid = client.post("/api/upload", files={"file": ("a.txt", b"hi", "text/plain")}).json()["attachment_id"]

    # Switch to a different user and try to read it.
    app.dependency_overrides[require_user_id] = lambda: "user-B"
    res = client.get(f"/api/attachments/{aid}")
    assert res.status_code == 404


def test_delete_foreign_attachment_returns_404(app):
    client = _client(app)
    aid = client.post("/api/upload", files={"file": ("a.txt", b"hi", "text/plain")}).json()["attachment_id"]
    app.dependency_overrides[require_user_id] = lambda: "user-B"
    assert client.delete(f"/api/attachments/{aid}").status_code == 404


def test_delete_acceptance_does_not_wait_for_unavailable_physical_storage(app):
    client = _client(app)
    aid = client.post(
        "/api/upload",
        files={"file": ("a.txt", b"hi", "text/plain")},
    ).json()["attachment_id"]

    response = client.delete(f"/api/attachments/{aid}")
    assert response.status_code == 202
    assert response.json()["status"] == "cleanup_pending"
    assert client.get(f"/api/attachments/{aid}").status_code == 404


def test_delete_replay_returns_the_same_durable_cleanup_identity(app):
    client = _client(app)
    aid = client.post(
        "/api/upload",
        files={"file": ("a.txt", b"hi", "text/plain")},
    ).json()["attachment_id"]
    first = client.delete(f"/api/attachments/{aid}")
    second = client.delete(f"/api/attachments/{aid}")
    assert first.status_code == second.status_code == 202
    assert first.json() == second.json()
    assert client.get(f"/api/attachments/{aid}").status_code == 404


def test_listing_is_per_user(app):
    """Same endpoint, two users — each only sees their own."""
    client = _client(app)
    client.post("/api/upload", files={"file": ("a.txt", b"hi", "text/plain")})
    app.dependency_overrides[require_user_id] = lambda: "user-B"
    client.post("/api/upload", files={"file": ("b.txt", b"yo", "text/plain")})

    bob = client.get("/api/attachments").json()
    assert len(bob["attachments"]) == 1
    assert bob["attachments"][0]["filename"] == "b.txt"

    app.dependency_overrides[require_user_id] = lambda: "user-A"
    alice = client.get("/api/attachments").json()
    assert len(alice["attachments"]) == 1
    assert alice["attachments"][0]["filename"] == "a.txt"
