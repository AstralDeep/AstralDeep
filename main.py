# preserve_gemini_metadata.py

import json
from typing import Any, Dict, List

def preserve_gemini_metadata(tool_calls: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Preserves and qualifies Gemini continuation metadata through the current stream/non-stream and outgoing-wire paths.
    
    Args:
    tool_calls (List[Dict[str, Any]]): List of tool call dictionaries.
    
    Returns:
    List[Dict[str, Any]]: List of tool call dictionaries with metadata preserved.
    """
    for tool_call in tool_calls:
        if 'extra_content' in tool_call and 'google' in tool_call['extra_content']:
            thought_signature = tool_call['extra_content']['google'].get('thought_signature')
            tool_call['extra_content']['google']['thought_signature'] = thought_signature if thought_signature is not None else ''
    return tool_calls

# Example usage
if __name__ == "__main__":
    sample_tool_calls = [
        {
            "id": "123",
            "function_name": "get_weather",
            "arguments": {"city": "New York"},
            "extra_content": {
                "google": {
                    "thought_signature": "Fetching weather for New York"
                }
            }
        },
        {
            "id": "456",
            "function_name": "get_temperature",
            "arguments": {"location": "Boston"},
            "extra_content": {
                "google": {
                    "thought_signature": None
                }
            }
        }
    ]
    
    preserved_tool_calls = preserve_gemini_metadata(sample_tool_calls)
    print(json.dumps(preserved_tool_calls, indent=2))

# test_solution.py

import unittest
from preserve_gemini_metadata import preserve_gemini_metadata

class TestPreserveGeminiMetadata(unittest.TestCase):
    def test_preserve_metadata_with_thought_signature(self):
        tool_calls = [
            {
                "id": "123",
                "function_name": "get_weather",
                "arguments": {"city": "New York"},
                "extra_content": {
                    "google": {
                        "thought_signature": "Fetching weather for New York"
                    }
                }
            }
        ]
        expected_result = [
            {
                "id": "123",
                "function_name": "get_weather",
                "arguments": {"city": "New York"},
                "extra_content": {
                    "google": {
                        "thought_signature": "Fetching weather for New York"
                    }
                }
            }
        ]
        self.assertEqual(preserve_gemini_metadata(tool_calls), expected_result)

    def test_preserve_metadata_with_empty_thought_signature(self):
        tool_calls = [
            {
                "id": "456",
                "function_name": "get_temperature",
                "arguments": {"location": "Boston"},
                "extra_content": {
                    "google": {
                        "thought_signature": None
                    }
                }
            }
        ]
        expected_result = [
            {
                "id": "456",
                "function_name": "get_temperature",
                "arguments": {"location": "Boston"},
                "extra_content": {
                    "google": {
                        "thought_signature": ""
                    }
                }
            }
        ]
        self.assertEqual(preserve_gemini_metadata(tool_calls), expected_result)

    def test_preserve_metadata_with_missing_google_key(self):
        tool_calls = [
            {
                "id": "789",
                "function_name": "get_temperature",
                "arguments": {"location": "Boston"},
                "extra_content": {
                    "google": {}
                }
            }
        ]
        expected_result = [
            {
                "id": "789",
                "function_name": "get_temperature",
                "arguments": {"location": "Boston"},
                "extra_content": {
                    "google": {
                        "thought_signature": ""
                    }
                }
            }
        ]
        self.assertEqual(preserve_gemini_metadata(tool_calls), expected_result)

    def test_preserve_metadata_with_missing_extra_content_key(self):
        tool_calls = [
            {
                "id": "101",
                "function_name": "get_temperature",
                "arguments": {"location": "Boston"}
            }
        ]
        expected_result = [
            {
                "id": "101",
                "function_name": "get_temperature",
                "arguments": {"location": "Boston"}
            }
        ]
        self.assertEqual(preserve_gemini_metadata(tool_calls), expected_result)

if __name__ == "__main__":
    unittest.main()