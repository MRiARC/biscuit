"""Tests for pluggable LLM providers, custom base URLs, and OpenAI compatibility."""

from __future__ import annotations

import json
import urllib.error
from pathlib import Path
from typing import Self

import pytest
from typer.testing import CliRunner

from biscuit.cli.main import app
from biscuit.evaluate.embeddings import (
    LocalOllamaEmbedder,
    OpenAIEmbedder,
    get_embedder,
)
from biscuit.extract.client import LLMClient
from biscuit.extract.pass1_discovery import Pass1ProposalDiscovery
from biscuit.extract.pass3_arguments import Pass3ArgumentMining
from biscuit.extract.pass4_synthesis import Pass4SchemaSynthesis
from biscuit.extract.pipeline import ExtractionPipeline


class MockHTTPResponse:
    def __init__(self, data: dict, status: int = 200) -> None:
        self.body = json.dumps(data).encode("utf-8")
        self.status = status

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> bool:
        return False

    def read(self, size: int = -1) -> bytes:
        return self.body


def test_llm_client_provider_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    # 1. Default to Ollama localhost
    client = LLMClient()
    assert client.base_url == "http://localhost:11434"
    assert client.provider == "ollama"
    assert client.skip_unload is False

    # 2. When API key is provided, default to OpenAI provider and endpoint
    client_key = LLMClient(api_key="sk-test-123")
    assert client_key.base_url == "https://api.openai.com/v1"
    assert client_key.provider == "openai"
    assert client_key.skip_unload is True

    # 3. Environment variables
    monkeypatch.setenv("BISCUIT_LLM_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("BISCUIT_LLM_API_KEY", "sk-or-test")
    client_env = LLMClient()
    assert client_env.base_url == "https://openrouter.ai/api/v1"
    assert client_env.api_key == "sk-or-test"
    assert client_env.provider == "openai"
    assert client_env.skip_unload is True

    # 4. Explicit provider override
    client_explicit = LLMClient(base_url="http://remote-server:11434", provider="ollama")
    assert client_explicit.provider == "ollama"

    client_custom_v1 = LLMClient(base_url="http://localhost:8000/v1")
    assert client_custom_v1.provider == "openai"


def test_llm_client_openai_generation(monkeypatch: pytest.MonkeyPatch) -> None:
    seen_requests: list[dict] = []

    def mock_urlopen(req: urllib.request.Request, timeout: float = 60.0) -> MockHTTPResponse:
        seen_requests.append(
            {
                "url": req.full_url,
                "headers": dict(req.headers),
                "payload": json.loads(req.data.decode("utf-8")),
            }
        )
        resp = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": '{"proposals": [{"proposal_id": "P1", "name": "Test"}]}',
                    }
                }
            ]
        }
        return MockHTTPResponse(resp)

    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)

    client = LLMClient(
        base_url="https://api.openai.com/v1",
        api_key="sk-test-key",
        provider="openai",
    )
    result = client.generate(model="gpt-4o", prompt="Extract proposals")

    assert "Test" in result
    assert len(seen_requests) == 1
    req = seen_requests[0]
    assert req["url"] == "https://api.openai.com/v1/chat/completions"
    assert req["headers"]["Authorization"] == "Bearer sk-test-key"
    assert req["payload"]["model"] == "gpt-4o"
    assert req["payload"]["messages"][0]["content"] == "Extract proposals"
    assert req["payload"]["response_format"] == {"type": "json_object"}


def test_llm_client_openai_fallback_without_response_format(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    def mock_urlopen(req: urllib.request.Request, timeout: float = 60.0) -> MockHTTPResponse:
        payload = json.loads(req.data.decode("utf-8"))
        calls.append(payload)
        if "response_format" in payload:
            raise urllib.error.HTTPError(
                req.full_url, 400, "Bad Request: response_format unsupported", {}, None
            )
        return MockHTTPResponse({"choices": [{"text": '{"proposals": []}'}]})

    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)

    client = LLMClient(
        base_url="https://api.custom-llm.org/v1",
        api_key="token-abc",
        provider="openai",
    )
    result = client.generate(model="custom-coder", prompt="Extract")
    assert result == '{"proposals": []}'
    assert len(calls) == 2
    assert "response_format" in calls[0]
    assert "response_format" not in calls[1]


def test_llm_client_unload_graceful_on_remote_and_openai() -> None:
    client_remote = LLMClient(
        base_url="https://api.openai.com/v1",
        api_key="sk-test",
        provider="openai",
    )
    # Under OpenAI / remote, unload is a graceful no-op returning True
    assert client_remote.unload_model("gpt-4o") is True

    client_skip = LLMClient(
        base_url="http://localhost:11434",
        skip_unload=True,
    )
    assert client_skip.unload_model("llama3.2:3b") is True


def test_pipeline_and_pass_model_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BISCUIT_LLM_MODEL", "deepseek-coder-v2")
    pipe = ExtractionPipeline()
    assert pipe.pass1.model == "deepseek-coder-v2"
    assert pipe.pass3.model == "deepseek-coder-v2"
    assert pipe.pass4.model == "deepseek-coder-v2"

    monkeypatch.setenv("BISCUIT_PASS1_MODEL", "gpt-4o-mini")
    monkeypatch.setenv("BISCUIT_PASS3_MODEL", "claude-3-5-sonnet")
    monkeypatch.setenv("BISCUIT_PASS4_MODEL", "qwen-2.5-72b")

    client = LLMClient(mock_mode=True)
    p1 = Pass1ProposalDiscovery(client)
    p3 = Pass3ArgumentMining(client)
    p4 = Pass4SchemaSynthesis(client)
    assert p1.model == "gpt-4o-mini"
    assert p3.model == "claude-3-5-sonnet"
    assert p4.model == "qwen-2.5-72b"


def test_openai_embedder_and_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    # 1. get_embedder default returns LocalOllamaEmbedder
    embedder_default = get_embedder()
    assert isinstance(embedder_default, LocalOllamaEmbedder)
    assert embedder_default.endpoint == "http://127.0.0.1:11434/api/embed"

    # 2. get_embedder with api_key returns OpenAIEmbedder
    embedder_openai = get_embedder(api_key="sk-test-embed")
    assert isinstance(embedder_openai, OpenAIEmbedder)
    assert embedder_openai.endpoint == "https://api.openai.com/v1/embeddings"
    assert embedder_openai.model == "text-embedding-3-small"

    # 3. Test OpenAIEmbedder invocation
    seen = []

    def mock_urlopen(req: urllib.request.Request, timeout: float = 60.0) -> MockHTTPResponse:
        seen.append((req.full_url, dict(req.headers), json.loads(req.data.decode("utf-8"))))
        return MockHTTPResponse({"data": [{"embedding": [0.1, 0.2, 0.3]}]})

    monkeypatch.setattr("biscuit.evaluate.embeddings.urlopen", mock_urlopen)

    vector = embedder_openai("Semantic rationale query")
    assert vector == [0.1, 0.2, 0.3]
    assert seen[0][0] == "https://api.openai.com/v1/embeddings"
    assert seen[0][1]["Authorization"] == "Bearer sk-test-embed"
    assert seen[0][2]["input"] == "Semantic rationale query"


def test_cli_extract_command(tmp_path: Path) -> None:
    raw_thread = {
        "thread_id": "PEP-999",
        "topic": "PEP 999: Add pattern matching operator",
        "messages": [
            {
                "message_id": "m1",
                "author": "guido",
                "author_role": "BDFL",
                "raw_text": "We propose ~> pattern operator. Rejecting alternative syntax <=.",
            }
        ],
    }
    thread_file = tmp_path / "raw_thread.json"
    thread_file.write_text(json.dumps(raw_thread), encoding="utf-8")
    output_file = tmp_path / "madr_out.json"

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "extract",
            "--input",
            str(thread_file),
            "--output",
            str(output_file),
            "--llm-provider",
            "openai",
            "--llm-api-key",
            "sk-mock",
        ],
        env={"BISCUIT_MOCK_LLM": "1"},
    )
    assert result.exit_code == 0, result.output
    assert output_file.is_file()
    data = json.loads(output_file.read_text(encoding="utf-8"))
    assert "madr" in data
    assert data["thread_id"] == "PEP-999"
