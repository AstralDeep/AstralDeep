import unittest
from typing import Any


def preserve_gemini_metadata(tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for call in tool_calls:
        if "extra_content" in call and "google" in call["extra_content"]:
            if call["extra_content"]["google"].get("thought_signature") is None:
                call["extra_content"]["google"]["thought_signature"] = ""
            elif not isinstance(call["extra_content"]["google"]["thought_signature"], str):
                call["extra_content"]["google"]["thought_signature"] = str(call["extra_content"]["google"]["thought_signature"])
    return tool_calls


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