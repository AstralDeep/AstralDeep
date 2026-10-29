"""Checks the write tool documents create-only defaults and explicit overwrite intent."""

from agents.computer_use.mcp_tools import TOOL_REGISTRY
from orchestrator.computer_use_policy import MAX_WRITE_BYTES
from orchestrator.goal_verification import PLANNING_GUIDANCE


def test_write_file_schema_explains_existing_file_behavior():
    tool = TOOL_REGISTRY["write_file"]
    schema = tool["input_schema"]["properties"]["if_exists"]
    content = tool["input_schema"]["properties"]["content"]

    assert schema["default"] == "refuse"
    assert schema["enum"] == ["refuse", "overwrite"]
    assert "refused by default" in tool["description"]
    assert "if_exists='overwrite' explicitly" in tool["description"]
    assert "replacing the existing file's contents" in schema["description"]
    assert content["maxLength"] * 4 <= MAX_WRITE_BYTES
    assert "64 KiB" in content["description"]
    assert "256 KiB" in content["description"]
    assert "Split larger source files into smaller modules" in PLANNING_GUIDANCE
