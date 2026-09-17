from __future__ import annotations

import pytest

from tests.fake_server import FakeAstralServer, FakeAstralState


@pytest.fixture
def fake_server():
    server = FakeAstralServer().start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def fake_server_factory():
    servers: list[FakeAstralServer] = []

    def _make(state: FakeAstralState | None = None) -> FakeAstralServer:
        server = FakeAstralServer(state).start()
        servers.append(server)
        return server

    yield _make
    for server in servers:
        server.stop()
