"""Denies accidental outbound traffic in native provider contract tests. Individual fixtures replace only the intended deterministic wire boundary."""

import asyncio
import socket

import httpx
import pytest
import requests


@pytest.fixture(autouse=True)
def native_offline(monkeypatch):
    def refused(*_, **__):
        raise AssertionError("unexpected_outbound_boundary")

    async def refused_async(*_, **__):
        raise AssertionError("unexpected_outbound_boundary")

    monkeypatch.setattr(httpx.Client, "send", refused)
    monkeypatch.setattr(httpx.AsyncClient, "send", refused_async)
    monkeypatch.setattr(requests.Session, "request", refused)
    monkeypatch.setattr(socket, "create_connection", refused)
    monkeypatch.setattr(socket, "getaddrinfo", refused)
    monkeypatch.setattr(asyncio, "open_connection", refused_async)
