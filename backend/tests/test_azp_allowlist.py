"""Tests for shared/auth_clients.py: the desktop client's azp claim is accepted only
when listed in KEYCLOAK_ALLOWED_AZP, while tokens omitting azp entirely are still
tolerated.
"""

from __future__ import annotations

from shared import auth_clients


def test_only_primary_when_no_allowlist(monkeypatch):
    monkeypatch.setenv("KEYCLOAK_CLIENT_ID", "astral-frontend")
    monkeypatch.setenv("KEYCLOAK_CLIENT_ID", "astral-frontend")
    monkeypatch.delenv("KEYCLOAK_ALLOWED_AZP", raising=False)
    assert auth_clients.allowed_azps() == {"astral-frontend"}
    assert auth_clients.is_azp_allowed("astral-frontend")
    assert not auth_clients.is_azp_allowed("astral-desktop")


def test_missing_azp_is_tolerated(monkeypatch):
    monkeypatch.setenv("KEYCLOAK_CLIENT_ID", "astral-frontend")
    monkeypatch.delenv("KEYCLOAK_ALLOWED_AZP", raising=False)
    assert auth_clients.is_azp_allowed("")
    assert auth_clients.is_azp_allowed(None)  # type: ignore[arg-type]


def test_allowlist_adds_desktop_client(monkeypatch):
    monkeypatch.setenv("KEYCLOAK_CLIENT_ID", "astral-frontend")
    monkeypatch.setenv("KEYCLOAK_ALLOWED_AZP", "astral-desktop")
    assert auth_clients.allowed_azps() == {"astral-frontend", "astral-desktop"}
    assert auth_clients.is_azp_allowed("astral-desktop")
    assert auth_clients.is_azp_allowed("astral-frontend")
    assert not auth_clients.is_azp_allowed("some-other-client")


def test_allowlist_parses_csv_and_whitespace(monkeypatch):
    monkeypatch.setenv("KEYCLOAK_CLIENT_ID", "astral-frontend")
    monkeypatch.setenv("KEYCLOAK_ALLOWED_AZP", " astral-desktop , astral-cli ,, ")
    assert auth_clients.allowed_azps() == {"astral-frontend", "astral-desktop", "astral-cli"}
    assert not auth_clients.is_azp_allowed("evil-client")
