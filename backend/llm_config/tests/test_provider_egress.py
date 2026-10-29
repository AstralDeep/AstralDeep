"""Selected private-provider transport snapshots only the existing operator egress policy.
Malformed policy and byte or semantic changes fail closed without granting endpoint authority.
"""

import pytest


def test_existing_operator_policy_is_canonical_and_current(monkeypatch):
    from llm_config.provider_egress import capture_private_policy, ProviderEgressUnavailable
    monkeypatch.setenv("EXTERNAL_AGENT_ALLOWED_PRIVATE_HOSTS", " localhost,127.0.0.1,localhost ")
    policy = capture_private_policy()
    assert policy.hosts == ("127.0.0.1", "localhost")
    policy.assert_current()
    monkeypatch.setenv("EXTERNAL_AGENT_ALLOWED_PRIVATE_HOSTS", "localhost,127.0.0.1")
    with pytest.raises(ProviderEgressUnavailable):
        policy.assert_current()


@pytest.mark.parametrize("value", ["x" * 8193, "bad HOST", "user@host", "https://host", ",".join(str(i) for i in range(33))])
def test_unbounded_or_noncanonical_policy_is_unavailable(monkeypatch, value):
    from llm_config.provider_egress import capture_private_policy, ProviderEgressUnavailable
    monkeypatch.setenv("EXTERNAL_AGENT_ALLOWED_PRIVATE_HOSTS", value)
    with pytest.raises(ProviderEgressUnavailable):
        capture_private_policy()
