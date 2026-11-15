# Tests for the egress model passthrough: only a caller holding the egress token may spend the model key.

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from egress import app as egress  # noqa: E402


@pytest.mark.parametrize("configured", ["", "vessel-token"])
def test_model_calls_need_the_egress_token(monkeypatch, configured):
    monkeypatch.setattr(egress, "EGRESS_TOKEN", configured)
    monkeypatch.setattr(egress, "LLM_API_KEY", "real-key")
    monkeypatch.setattr(egress, "LLM_BASE_URL", "https://models.test/v1")
    seen = []

    def upstream(request):
        seen.append(request.headers["authorization"])
        return httpx.Response(200, json={"data": []})

    with TestClient(egress.app) as client:
        monkeypatch.setattr(egress, "_client", httpx.AsyncClient(transport=httpx.MockTransport(upstream)))
        for offered in (None, "", "Bearer ", "Bearer wrong", "Bearer vessel-token "):
            headers = {} if offered is None else {"Authorization": offered}
            refused = client.get("/v1/models", headers=headers)
            assert refused.status_code == 403 and "egress token" in refused.text
        assert client.get("/v1/unknown").status_code == 403
        allowed = client.get("/v1/models", headers={"Authorization": "Bearer vessel-token"})
        assert allowed.status_code == (200 if configured else 403)
    assert seen == (["Bearer real-key"] if configured else [])


def test_without_an_endpoint_model_calls_are_refused(monkeypatch):
    monkeypatch.setattr(egress, "EGRESS_TOKEN", "vessel-token")
    monkeypatch.setattr(egress, "LLM_BASE_URL", "")
    with TestClient(egress.app) as client:
        refused = client.get("/v1/models", headers={"Authorization": "Bearer vessel-token"})
        assert refused.status_code == 503 and "endpoint" in refused.text
