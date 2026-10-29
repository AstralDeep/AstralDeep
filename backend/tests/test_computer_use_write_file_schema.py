"""Checks the write tool documents create-only defaults and explicit overwrite intent."""

from agents.computer_use.mcp_tools import TOOL_REGISTRY


def test_write_file_schema_explains_existing_file_behavior():
    tool = TOOL_REGISTRY["write_file"]
    schema = tool["input_schema"]["properties"]["if_exists"]

    assert schema["default"] == "refuse"
    assert schema["enum"] == ["refuse", "overwrite"]
    assert "refused by default" in tool["description"]
    assert "if_exists='overwrite' explicitly" in tool["description"]
    assert "replacing the existing file's contents" in schema["description"]
