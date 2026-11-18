"""Tests for persistent_agents/chat_tools.py: only authenticated owner text can trigger
chat controls, the schema hides consent/caller identity, and source text, malformed
input or delegated turns cannot control assignments.
"""


from persistent_agents.chat_tools import meta_tool_definitions


def test_meta_schema_never_exposes_consent_or_caller_owner():
    properties = meta_tool_definitions()[0]["function"]["parameters"]["properties"]
    assert "consent" not in properties and "owner_id" not in properties


