"""Feature-066 pins for ``KnowledgeSynthesizer._refresh_client``.

The synthesizer is a cross-user system flow: every cycle re-resolves the
admin-managed system LLM credential (054). No resolver or no stored record
means the cycle skips honestly; a resolved config builds an OpenAI-compatible
client through the shared ``openai_auth_kwargs`` seam (keyless sentinel and
real bearer alike) without any network contact.
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

from astralplane import create_repository_catalog
from orchestrator.knowledge_synthesis import KnowledgeSynthesizer


class _PlaneRuntime:
    def __init__(self) -> None:
        self.repositories = create_repository_catalog()

    @contextmanager
    def transaction(self):
        yield object()


def _synthesizer(tmp_path, config_resolver) -> KnowledgeSynthesizer:
    runtime = _PlaneRuntime()
    return KnowledgeSynthesizer(
        knowledge_dir=str(tmp_path),
        config_resolver=config_resolver,
        plane_runtime=runtime,
        plane_repositories=runtime.repositories,
        # Client-refresh tests do not create or claim maintenance units.
        maintenance_repository=object(),
    )


def test_refresh_client_skips_without_a_resolver(tmp_path) -> None:
    synth = _synthesizer(tmp_path, config_resolver=None)
    assert synth._refresh_client() is False
    assert synth.client is None
    assert synth.model is None


def test_refresh_client_skips_when_no_system_config_is_stored(tmp_path) -> None:
    synth = _synthesizer(tmp_path, config_resolver=lambda: None)
    assert synth._refresh_client() is False
    assert synth.client is None
    assert synth.model is None


def test_refresh_client_builds_the_client_from_the_system_config(tmp_path) -> None:
    cfg = SimpleNamespace(
        base_url="http://system-llm.test/v1",
        api_key="sk-test-066",
        model="glm-test",
    )
    synth = _synthesizer(tmp_path, config_resolver=lambda: cfg)
    assert synth._refresh_client() is True
    assert synth.model == "glm-test"
    assert synth.client is not None
    # The real key rode the shared auth seam onto the constructed client.
    assert synth.client.api_key == "sk-test-066"
    assert str(synth.client.base_url).startswith("http://system-llm.test/v1")
