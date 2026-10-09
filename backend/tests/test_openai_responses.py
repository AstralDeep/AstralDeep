import unittest
from unittest.mock import MagicMock, patch
from backend.llm_config.providers import OpenAIResponsesAdapter, ProviderRoutingManager

class TestOpenAIResponsesAdapter(unittest.TestCase):
    def setUp(self):
        self.mock_client = MagicMock()
        # Mock the responses and chat.completions attributes
        self.mock_client.responses = MagicMock()
        self.mock_client.chat = MagicMock()
        self.mock_client.chat.completions = MagicMock()

    def test_is_eligible(self):
        # Default disabled
        adapter = OpenAIResponsesAdapter(enabled=False)
        self.assertFalse(adapter.is_eligible("gpt-4o"))

        # Enabled but unsupported model
        adapter = OpenAIResponsesAdapter(enabled=True)
        self.assertFalse(adapter.is_eligible("unsupported-model"))

        # Enabled and supported model
        self.assertTrue(adapter.is_eligible("gpt-4o"))

    def test_execute_success_responses_api(self):
        adapter = OpenAIResponsesAdapter(enabled=True)
        messages = [{"role": "user", "content": "Hello"}]
        
        # Mock responses.create return value
        mock_response = MagicMock()
        self.mock_client.responses.create.return_value = mock_response

        result = adapter.execute(
            client=self.mock_client,
            model="gpt-4o",
            messages=messages,
            store=False
        )

        self.assertEqual(result["type"], "responses_response")
        self.assertEqual(result["response"], mock_response)
        self.mock_client.responses.create.assert_called_once_with(
            model="gpt-4o",
            input=messages,
            store=False
        )

    def test_execute_fallback_when_disabled(self):
        adapter = OpenAIResponsesAdapter(enabled=False)
        messages = [{"role": "user", "content": "Hello"}]

        mock_response = MagicMock()
        self.mock_client.chat.completions.create.return_value = mock_response

        result = adapter.execute(
            client=self.mock_client,
            model="gpt-4o",
            messages=messages
        )

        self.assertEqual(result["type"], "chat_response")
        self.assertEqual(result["response"], mock_response)
        self.mock_client.chat.completions.create.assert_called_once_with(
            model="gpt-4o",
            messages=messages
        )

    def test_execute_fallback_when_unsupported_model(self):
        adapter = OpenAIResponsesAdapter(enabled=True)
        messages = [{"role": "user", "content": "Hello"}]

        mock_response = MagicMock()
        self.mock_client.chat.completions.create.return_value = mock_response

        result = adapter.execute(
            client=self.mock_client,
            model="unsupported-model",
            messages=messages
        )

        self.assertEqual(result["type"], "chat_response")
        self.assertEqual(result["response"], mock_response)

    def test_execute_fallback_when_sdk_outdated(self):
        adapter = OpenAIResponsesAdapter(enabled=True)
        messages = [{"role": "user", "content": "Hello"}]

        # Remove responses attribute to simulate outdated SDK
        delattr(self.mock_client, "responses")

        mock_response = MagicMock()
        self.mock_client.chat.completions.create.return_value = mock_response

        result = adapter.execute(
            client=self.mock_client,
            model="gpt-4o",
            messages=messages
        )

        self.assertEqual(result["type"], "chat_response")
        self.assertEqual(result["response"], mock_response)

    def test_execute_fallback_on_exception(self):
        adapter = OpenAIResponsesAdapter(enabled=True)
        messages = [{"role": "user", "content": "Hello"}]

        # Force responses.create to raise an exception
        self.mock_client.responses.create.side_effect = Exception("API Error")

        mock_response = MagicMock()
        self.mock_client.chat.completions.create.return_value = mock_response

        result = adapter.execute(
            client=self.mock_client,
            model="gpt-4o",
            messages=messages
        )

        self.assertEqual(result["type"], "chat_response")
        self.assertEqual(result["response"], mock_response)

    def test_execute_streaming(self):
        adapter = OpenAIResponsesAdapter(enabled=True)
        messages = [{"role": "user", "content": "Hello"}]

        mock_stream = MagicMock()
        self.mock_client.responses.create.return_value = mock_stream

        result = adapter.execute(
            client=self.mock_client,
            model="gpt-4o",
            messages=messages,
            stream=True
        )

        self.assertEqual(result["type"], "responses_stream")
        self.assertEqual(result["stream"], mock_stream)
        self.mock_client.responses.create.assert_called_once_with(
            model="gpt-4o",
            input=messages,
            store=False,
            stream=True
        )


class TestProviderRoutingManager(unittest.TestCase):
    @patch("backend.llm_config.providers.OpenAI")
    def test_get_client_success(self, mock_openai_class):
        manager = ProviderRoutingManager()
        client = manager.get_client("openai", api_key="test-key")
        mock_openai_class.assert_called_once_with(api_key="test-key", base_url=None)

    def test_get_client_no_key_raises_value_error(self):
        manager = ProviderRoutingManager()
        with self.assertRaises(ValueError):
            manager.get_client("openai", api_key="")

    def test_get_client_unsupported_provider_raises_value_error(self):
        manager = ProviderRoutingManager()
        with self.assertRaises(ValueError):
            manager.get_client("unsupported-provider", api_key="test-key")

if __name__ == "__main__":
    unittest.main()
