"""Tests that backend/orchestrator/session_store.py's WebSessionStore needs an initialized Plane runtime or an explicit session repository context."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from orchestrator.session_store import WebSessionStore


def test_store_requires_runtime_or_explicit_repository_context(monkeypatch) -> None:
    monkeypatch.setenv("WEB_SESSION_ENC_KEY", Fernet.generate_key().decode())
    with pytest.raises(ValueError, match="initialized Plane runtime"):
        WebSessionStore()
    context = object()
    assert WebSessionStore(session_context=context)._sessions is context
