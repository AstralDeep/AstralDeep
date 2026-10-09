import os
import logging
from typing import Dict, Any, List, Optional, Generator, Union
from openai import OpenAI

logger = logging.getLogger(__name__)

class OpenAIResponsesAdapter:
    """
    Adapter for the OpenAI Responses API (client.responses.create).
    This is a default-off, manually scoped capability adapter that falls back
    to the unchanged generic/local route (Chat Completions) if disabled,
    unsupported, or on error.
    """
    def __init__(self, enabled: bool = False, supported_models: Optional[List[str]] = None):
        self.enabled = enabled
        self.supported_models = supported_models or ["gpt-4o", "gpt-4o-mini", "o1-preview", "o1-mini"]

    def is_eligible(self, model: str) -> bool:
        return self.enabled and model in self.supported_models

    def execute(
        self,
        client: OpenAI,
        model: str,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        stream: bool = False,
        store: bool = False,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Executes a request using the OpenAI Responses API.
        Falls back to standard chat completions if any error occurs or if not eligible.
        """
        if not self.is_eligible(model):
            logger.debug(f"Model {model} or Responses API adapter not eligible. Falling back to Chat Completions.")
            return self._fallback_chat_completions(client, model, messages, tools, temperature, max_tokens, stream, **kwargs)

        try:
            # Prepare parameters for client.responses.create
            # The Responses API uses 'input' instead of 'messages'
            params: Dict[str, Any] = {
                "model": model,
                "input": messages,
                "store": store,
            }
            if tools:
                params["tools"] = tools
            if temperature is not None:
                params["temperature"] = temperature
            if max_tokens is not None:
                params["max_tokens"] = max_tokens

            # Add any other valid kwargs
            for k, v in kwargs.items():
                if k not in ["messages", "stream", "input", "store"]:
                    params[k] = v

            logger.info(f"Executing OpenAI Responses API call for model {model} (store={store})")
            
            # Check if responses attribute exists on client
            if not hasattr(client, "responses"):
                raise AttributeError("OpenAI client does not have 'responses' attribute. SDK might be outdated.")

            if stream:
                params["stream"] = True
                response_stream = client.responses.create(**params)
                return {"stream": response_stream, "type": "responses_stream"}
            else:
                response = client.responses.create(**params)
                return {"response": response, "type": "responses_response"}

        except Exception as e:
            logger.warning(f"OpenAI Responses API call failed: {e}. Falling back to Chat Completions.")
            return self._fallback_chat_completions(client, model, messages, tools, temperature, max_tokens, stream, **kwargs)

    def _fallback_chat_completions(
        self,
        client: OpenAI,
        model: str,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        stream: bool = False,
        **kwargs
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "model": model,
            "messages": messages,
        }
        if tools:
            params["tools"] = tools
        if temperature is not None:
            params["temperature"] = temperature
        if max_tokens is not None:
            params["max_tokens"] = max_tokens
        if stream:
            params["stream"] = True

        for k, v in kwargs.items():
            if k not in ["input", "store", "messages", "stream"]:
                params[k] = v

        logger.info(f"Executing fallback OpenAI Chat Completions call for model {model}")
        if stream:
            response_stream = client.chat.completions.create(**params)
            return {"stream": response_stream, "type": "chat_stream"}
        else:
            response = client.chat.completions.create(**params)
            return {"response": response, "type": "chat_response"}


class ProviderRoutingManager:
    """
    Manages LLM provider routing, credential resolution, and adapter execution.
    """
    def __init__(self, responses_adapter_enabled: bool = False):
        self.responses_adapter = OpenAIResponsesAdapter(enabled=responses_adapter_enabled)

    def get_client(self, provider: str, api_key: str, base_url: Optional[str] = None) -> Any:
        """
        Resolves and returns the appropriate client for the provider.
        Never uses environment-backed provider keys; relies on the passed credential.
        """
        if not api_key:
            raise ValueError("API key must be provided. Environment-backed keys are disabled for security.")

        if provider.lower() == "openai":
            return OpenAI(api_key=api_key, base_url=base_url)
        else:
            # For other providers, return a generic/local route client or raise
            raise ValueError(f"Unsupported or unconfigured provider: {provider}")

    def generate(
        self,
        provider: str,
        api_key: str,
        model: str,
        messages: List[Dict[str, Any]],
        base_url: Optional[str] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        stream: bool = False,
        store: bool = False,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Routes and executes the generation request.
        """
        client = self.get_client(provider, api_key, base_url)
        
        if provider.lower() == "openai":
            return self.responses_adapter.execute(
                client=client,
                model=model,
                messages=messages,
                tools=tools,
                temperature=temperature,
                max_tokens=max_tokens,
                stream=stream,
                store=store,
                **kwargs
            )
        else:
            # Non-OpenAI providers retain the current compatible path
            # (e.g., standard chat completions or local route)
            raise NotImplementedError(f"Provider {provider} is not supported by this routing manager.")
