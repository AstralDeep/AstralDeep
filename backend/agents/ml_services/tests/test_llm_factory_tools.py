"""Tests for ml_services/llm_factory_tools.py: credential checks, base-URL /v1
normalization, list_models, chat_with_model, create_embedding, and transcribe_audio.
"""

import socket
from unittest.mock import patch

import pytest

from agents.ml_services import llm_factory_tools as mcp_tools
from shared.tests._http_mock import HttpMock


SAFE_HOST = "llm-factory.example.com"
BASE_URL = f"https://{SAFE_HOST}"
GOOD_CREDS = {"LLM_FACTORY_URL": BASE_URL, "LLM_FACTORY_API_KEY": "sentinel-api-key"}


@pytest.fixture
def rmock():
    with HttpMock() as m:
        yield m


@pytest.fixture(autouse=True)
def stub_dns():
    def _fake(host, *_a, **_kw):
        if host == SAFE_HOST:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0))]
        raise socket.gaierror(host)
    with patch("socket.getaddrinfo", _fake):
        yield


def test_credentials_check_v1_models_ok(rmock: HttpMock) -> None:
    rmock.add("GET", f"{BASE_URL}/v1/models", status=200, json={"data": [{"id": "gpt-4"}]})
    result = mcp_tools._credentials_check(_credentials=GOOD_CREDS)
    assert result == {"credential_test": "ok"}


def test_credentials_check_auth_failed(rmock: HttpMock) -> None:
    rmock.add("GET", f"{BASE_URL}/v1/models", status=401, body=b"{}")
    result = mcp_tools._credentials_check(_credentials=GOOD_CREDS)
    assert result["credential_test"] == "auth_failed"


def test_credentials_check_unreachable() -> None:
    import requests
    with patch("requests.request", side_effect=requests.ConnectionError("nope")):
        result = mcp_tools._credentials_check(_credentials=GOOD_CREDS)
        assert result["credential_test"] == "unreachable"


def test_credentials_check_missing_creds() -> None:
    result = mcp_tools._credentials_check()
    assert result["credential_test"] == "unexpected"


def test_base_url_with_v1_suffix_is_stripped(rmock: HttpMock) -> None:
    creds_with_v1 = {
        "LLM_FACTORY_URL": f"{BASE_URL}/v1",
        "LLM_FACTORY_API_KEY": "sentinel",
    }
    rmock.add("GET", f"{BASE_URL}/v1/models", status=200, json={"data": [{"id": "x"}]})
    result = mcp_tools._credentials_check(_credentials=creds_with_v1)
    assert result == {"credential_test": "ok"}


def test_build_client_stale_credentials_message() -> None:
    with pytest.raises(ValueError, match="could not be decrypted"):
        mcp_tools._build_client({"_credentials": {}, "_credentials_stale": True})


def test_list_models(rmock: HttpMock) -> None:
    rmock.add("GET", f"{BASE_URL}/v1/models", status=200,
              json={"data": [{"id": "gpt-4"}, {"id": "llama-3"}]})
    result = mcp_tools.list_models(_credentials=GOOD_CREDS)
    assert result["_data"]["models"] == [{"id": "gpt-4"}, {"id": "llama-3"}]


def test_list_models_surfaces_router2_metadata(rmock: HttpMock) -> None:
    rmock.add("GET", f"{BASE_URL}/v1/models", status=200, json={
        "data": [
            {"id": "llama-3-8b", "owned_by": "vllm", "max_model_len": 8192},
        ],
    })
    result = mcp_tools.list_models(_credentials=GOOD_CREDS)
    rendered = result["_ui_components"][0]
    body = rendered["content"][0]["content"]
    assert "llama-3-8b" in body
    assert "vllm" in body
    assert "8192" in body


def test_list_models_auth_failed_renders_alert(rmock: HttpMock) -> None:
    rmock.add("GET", f"{BASE_URL}/v1/models", status=401, body=b"{}")
    result = mcp_tools.list_models(_credentials=GOOD_CREDS)
    assert result["_ui_components"][0]["variant"] == "error"


def test_chat_with_model_extracts_content(rmock: HttpMock) -> None:
    rmock.add(
        "POST",
        f"{BASE_URL}/v1/chat/completions",
        status=200,
        json={
            "choices": [{"message": {"role": "assistant", "content": "4"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 1},
        },
    )
    result = mcp_tools.chat_with_model(
        model_id="gpt-4",
        messages=[{"role": "user", "content": "what is 2 + 2?"}],
        _credentials=GOOD_CREDS,
    )
    assert result["_data"]["content"] == "4"
    assert result["_data"]["usage"]["prompt_tokens"] == 10


def test_chat_with_model_auth_failed(rmock: HttpMock) -> None:
    rmock.add("POST", f"{BASE_URL}/v1/chat/completions", status=401, body=b"{}")
    result = mcp_tools.chat_with_model(
        model_id="gpt-4",
        messages=[{"role": "user", "content": "hi"}],
        _credentials=GOOD_CREDS,
    )
    assert result["_ui_components"][0]["variant"] == "error"


def test_create_embedding_string_input(rmock: HttpMock) -> None:
    rmock.add(
        "POST",
        f"{BASE_URL}/v1/embeddings",
        status=200,
        json={
            "data": [{"embedding": [0.1, 0.2, 0.3], "index": 0}],
            "usage": {"prompt_tokens": 5, "total_tokens": 5},
        },
    )
    result = mcp_tools.create_embedding(
        model_id="text-embedding-3-small",
        input="hello world",
        _credentials=GOOD_CREDS,
    )
    assert result["_data"]["embeddings"] == [[0.1, 0.2, 0.3]]
    assert result["_data"]["dimension"] == 3
    assert result["_data"]["count"] == 1
    assert result["_data"]["model_id"] == "text-embedding-3-small"


def test_create_embedding_list_input(rmock: HttpMock) -> None:
    rmock.add(
        "POST",
        f"{BASE_URL}/v1/embeddings",
        status=200,
        json={
            "data": [
                {"embedding": [0.1, 0.2], "index": 0},
                {"embedding": [0.3, 0.4], "index": 1},
            ],
            "usage": {"prompt_tokens": 6, "total_tokens": 6},
        },
    )
    result = mcp_tools.create_embedding(
        model_id="text-embedding-3-small",
        input=["foo", "bar"],
        _credentials=GOOD_CREDS,
    )
    assert result["_data"]["count"] == 2
    assert result["_data"]["dimension"] == 2


def test_create_embedding_auth_failed(rmock: HttpMock) -> None:
    rmock.add("POST", f"{BASE_URL}/v1/embeddings", status=401, body=b"{}")
    result = mcp_tools.create_embedding(
        model_id="m",
        input="hi",
        _credentials=GOOD_CREDS,
    )
    assert result["_ui_components"][0]["variant"] == "error"


def test_create_embedding_rejects_empty_input() -> None:
    result = mcp_tools.create_embedding(
        model_id="m",
        input="",
        _credentials=GOOD_CREDS,
    )
    assert result["_ui_components"][0]["variant"] == "error"


def test_transcribe_audio_happy_path(rmock: HttpMock, tmp_path) -> None:
    audio = tmp_path / "voice.wav"
    audio.write_bytes(b"RIFF\x00\x00\x00\x00WAVE")
    rmock.add(
        "POST",
        f"{BASE_URL}/v1/audio/transcriptions",
        status=200,
        json={"text": "Hello there.", "language": "en"},
    )
    result = mcp_tools.transcribe_audio(
        model_id="whisper-1",
        file_handle=str(audio),
        _credentials=GOOD_CREDS,
        user_id="alice",
    )
    assert result["_data"]["text"] == "Hello there."
    assert result["_data"]["model_id"] == "whisper-1"
    assert result["_data"]["filename"] == "voice.wav"


def test_transcribe_audio_with_language_hint(rmock: HttpMock, tmp_path) -> None:
    audio = tmp_path / "voice.wav"
    audio.write_bytes(b"RIFF")
    captured = {}

    def _capture(method, url, **kwargs):
        captured.update(kwargs)
        from shared.tests._http_mock import _FakeResponse
        return _FakeResponse(200, b'{"text":"Bonjour"}')

    with patch("requests.request", side_effect=_capture):
        result = mcp_tools.transcribe_audio(
            model_id="whisper-1",
            file_handle=str(audio),
            language="fr",
            _credentials=GOOD_CREDS,
            user_id="alice",
        )
    assert result["_data"]["text"] == "Bonjour"
    assert captured["data"]["language"] == "fr"
    assert captured["data"]["model"] == "whisper-1"


def test_transcribe_audio_auth_failed(rmock: HttpMock, tmp_path) -> None:
    audio = tmp_path / "voice.wav"
    audio.write_bytes(b"RIFF")
    rmock.add("POST", f"{BASE_URL}/v1/audio/transcriptions", status=401, body=b"{}")
    result = mcp_tools.transcribe_audio(
        model_id="whisper-1",
        file_handle=str(audio),
        _credentials=GOOD_CREDS,
        user_id="alice",
    )
    assert result["_ui_components"][0]["variant"] == "error"


def test_transcribe_audio_requires_user_id(tmp_path) -> None:
    audio = tmp_path / "voice.wav"
    audio.write_bytes(b"RIFF")
    result = mcp_tools.transcribe_audio(
        model_id="whisper-1",
        file_handle=str(audio),
        _credentials=GOOD_CREDS,
    )
    assert result["_ui_components"][0]["variant"] == "error"


def test_long_running_tools_set_is_empty() -> None:
    assert mcp_tools.LONG_RUNNING_TOOLS == set()


def test_tool_registry_has_required_entries() -> None:
    expected = {
        "list_models",
        "chat_with_model",
        "create_embedding",
        "transcribe_audio",
    }
    assert set(mcp_tools.TOOL_REGISTRY.keys()) == expected


def test_no_api_key_in_response_data(rmock: HttpMock) -> None:
    rmock.add("GET", f"{BASE_URL}/v1/models", status=200, json={"data": [{"id": "x"}]})
    result = mcp_tools.list_models(_credentials=GOOD_CREDS)
    assert "sentinel-api-key" not in str(result)
