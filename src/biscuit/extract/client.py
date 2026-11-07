"""
LLM Client supporting local runtimes (Ollama) and OpenAI-compatible APIs.
Supports sequential model offloading when running on local Ollama,
as well as remote foundation models (OpenAI, DeepSeek, vLLM, OpenRouter, Groq, etc.).
Includes mock replay mode for deterministic CI testing without GPU/daemon.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlsplit


class LLMClient:
    """
    Client for local models (Ollama) or remote OpenAI-compatible LLM endpoints.
    Allows passing custom base URLs, API keys, and arbitrary model names.
    Enforces sequential memory offloading when operating under local Ollama,
    while gracefully bypassing offload for remote endpoints.
    """

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        provider: str = "auto",
        mock_mode: bool | None = None,
        mock_responses: dict[str, str] | None = None,
        timeout_seconds: float = 60.0,
        headers: dict[str, str] | None = None,
        skip_unload: bool | None = None,
    ):
        env_key = os.getenv("BISCUIT_LLM_API_KEY") or os.getenv("OPENAI_API_KEY")
        self.api_key = api_key if api_key is not None else env_key

        if base_url:
            resolved_url = base_url
        elif os.getenv("BISCUIT_LLM_BASE_URL"):
            resolved_url = os.getenv("BISCUIT_LLM_BASE_URL")
        elif os.getenv("OPENAI_BASE_URL"):
            resolved_url = os.getenv("OPENAI_BASE_URL")
        elif self.api_key:
            resolved_url = "https://api.openai.com/v1"
        else:
            resolved_url = "http://localhost:11434"

        self.base_url = resolved_url.rstrip("/")
        self.headers = headers or {}
        self.timeout_seconds = timeout_seconds

        # Resolve provider
        env_provider = os.getenv("BISCUIT_LLM_PROVIDER")
        chosen_provider = (provider if provider != "auto" else env_provider) or "auto"
        chosen_provider = chosen_provider.lower().strip()

        if chosen_provider in ("ollama", "local"):
            self.provider = "ollama"
        elif chosen_provider in ("openai", "openai_compatible", "chat", "v1", "remote"):
            self.provider = "openai"
        else:
            # Auto-detection
            parsed = urlsplit(self.base_url)
            is_loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
            has_v1 = "/v1" in parsed.path or parsed.path.endswith("/v1")
            has_openai = "openai" in (parsed.hostname or "").lower()
            if (
                self.api_key
                or has_openai
                or (has_v1 and not is_loopback)
                or (parsed.scheme == "https")
            ):
                self.provider = "openai"
            elif is_loopback and not has_v1:
                self.provider = "ollama"
            elif has_v1:
                self.provider = "openai"
            else:
                self.provider = "ollama"

        # Resolve skip_unload
        if skip_unload is not None:
            self.skip_unload = skip_unload
        else:
            env_skip = os.getenv("BISCUIT_SKIP_UNLOAD", "0") in ("1", "true", "True")
            self.skip_unload = env_skip or (self.provider == "openai")

        # Mock mode
        env_mock = os.getenv("BISCUIT_MOCK_LLM", "0") in ("1", "true", "True")
        self.mock_mode = env_mock if mock_mode is None else mock_mode
        self.mock_responses = mock_responses or {}

        self.loaded_model: str | None = None
        self.call_history: list[dict[str, Any]] = []

    def set_mock_response(self, key: str, response_json: str) -> None:
        """Register a deterministic mock JSON response for CI testing."""
        self.mock_responses[key] = response_json

    def _chat_endpoint(self) -> str:
        base = self.base_url
        if base.endswith("/chat/completions"):
            return base
        if base.endswith("/v1"):
            return f"{base}/chat/completions"
        return f"{base}/v1/chat/completions"

    def _ollama_generate_endpoint(self) -> str:
        base = self.base_url
        if base.endswith("/api/generate"):
            return base
        if base.endswith("/api"):
            return f"{base}/generate"
        return f"{base}/api/generate"

    def _ollama_ps_endpoint(self) -> str:
        base = self.base_url
        if base.endswith("/api/generate"):
            base = base[: -len("/generate")]
        elif not base.endswith("/api"):
            base = f"{base}/api"
        return f"{base}/ps"

    def generate(
        self, model: str, prompt: str, temperature: float = 0.0, keep_alive: int = 0
    ) -> str:
        """
        Generates text using the configured model endpoint.

        Args:
            model: Model identifier (e.g. 'llama3.2:3b', 'gpt-4o', 'qwen2.5-coder:7b')
            prompt: Text prompt
            temperature: Sampling temperature (0.0 for deterministic schema extraction)
            keep_alive: Seconds to keep model in RAM for Ollama (0 forces immediate unload)

        Returns:
            str: Generated raw response string (typically JSON)
        """
        self.call_history.append({"model": model, "prompt": prompt, "keep_alive": keep_alive})

        # 1. Deterministic Mock Mode (For CI / Offline Testing)
        if self.mock_mode:
            if model in self.mock_responses:
                return self.mock_responses[model]
            for key, resp in self.mock_responses.items():
                if key in prompt:
                    return resp
            return "{}"

        # 2. Live HTTP Request
        if self.provider == "openai":
            return self._generate_openai(model, prompt, temperature)
        return self._generate_ollama(model, prompt, temperature, keep_alive)

    def _generate_openai(self, model: str, prompt: str, temperature: float = 0.0) -> str:
        endpoint = self._chat_endpoint()
        headers = {
            "Content-Type": "application/json",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if self.headers:
            headers.update(self.headers)

        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "user", "content": prompt},
            ],
            "temperature": temperature,
            "response_format": {"type": "json_object"},
        }

        req = urllib.request.Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            # Fall back without response_format if provider returns 400 (unsupported response_format)
            if err.code == 400 and "response_format" in payload:
                payload.pop("response_format", None)
                req_fallback = urllib.request.Request(
                    endpoint,
                    data=json.dumps(payload).encode("utf-8"),
                    headers=headers,
                    method="POST",
                )
                try:
                    with urllib.request.urlopen(req_fallback, timeout=self.timeout_seconds) as resp:
                        data = json.loads(resp.read().decode("utf-8"))
                except (urllib.error.URLError, TimeoutError) as e:
                    raise ConnectionError(
                        f"Failed to communicate with LLM server at {endpoint}: {e}."
                    ) from e
            else:
                raise ConnectionError(
                    f"HTTP error {err.code} communicating with LLM server at {endpoint}: {err}. "
                    "Ensure your API key and model name are valid."
                ) from err
        except (urllib.error.URLError, TimeoutError) as e:
            raise ConnectionError(
                f"Failed to communicate with LLM server at {self.base_url}: {e}. "
                "Ensure the server is running or enable mock mode with BISCUIT_MOCK_LLM=1."
            ) from e

        choices = data.get("choices")
        if isinstance(choices, list) and choices:
            choice = choices[0]
            if isinstance(choice, dict):
                msg = choice.get("message")
                if isinstance(msg, dict):
                    return str(msg.get("content") or "")
                if "text" in choice:
                    return str(choice.get("text") or "")
        if "response" in data:
            return str(data.get("response") or "")
        return ""

    def _generate_ollama(
        self, model: str, prompt: str, temperature: float = 0.0, keep_alive: int = 0
    ) -> str:
        payload = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "format": "json",
            "options": {
                "temperature": temperature,
            },
            "keep_alive": f"{keep_alive}s" if keep_alive > 0 else 0,
        }

        endpoint = self._ollama_generate_endpoint()
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if self.headers:
            headers.update(self.headers)

        req = urllib.request.Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                response_text = data.get("response", "")
                self.loaded_model = model if keep_alive > 0 else None
                return response_text
        except (urllib.error.URLError, TimeoutError) as e:
            raise ConnectionError(
                f"Failed to communicate with local Ollama server at {self.base_url}: {e}. "
                "Ensure Ollama is running or enable mock mode with BISCUIT_MOCK_LLM=1."
            ) from e

    def unload_model(self, model: str) -> bool:
        """Request unload and confirm that Ollama no longer reports this model resident.

        When running with OpenAI or remote APIs (or skip_unload=True), model unloading
        is not applicable and returns True immediately.
        Under local Ollama, an unreachable or incompatible process-status endpoint
        fails closed (returns False), and callers must abort before loading the next model.
        """
        if self.mock_mode or self.skip_unload or self.provider == "openai":
            self.loaded_model = None
            return True

        payload = {"model": model, "keep_alive": 0}
        endpoint = self._ollama_generate_endpoint()
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if self.headers:
            headers.update(self.headers)

        req = urllib.request.Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5.0):
                pass
            ps_endpoint = self._ollama_ps_endpoint()
            for attempt in range(3):
                req_status = urllib.request.Request(ps_endpoint, headers=headers, method="GET")
                with urllib.request.urlopen(req_status, timeout=5.0) as response:
                    status = json.loads(response.read().decode("utf-8"))
                if not isinstance(status, dict) or not isinstance(status.get("models"), list):
                    return False
                running_models = status["models"]
                if not all(isinstance(item, dict) for item in running_models):
                    return False
                names = {
                    str(item.get("name") or item.get("model") or "") for item in running_models
                }
                if model not in names:
                    self.loaded_model = None
                    return True
                if attempt < 2:
                    time.sleep(0.1)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, TypeError):
            return False
        return False


class LocalLLMClient(LLMClient):
    """Backward-compatible alias for LLMClient preserving legacy constructor positional arguments.

    Legacy positional arguments:
        (base_url, mock_mode, mock_responses, timeout_seconds)
    """

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        mock_mode: bool = False,
        mock_responses: dict[str, str] | None = None,
        timeout_seconds: float = 60.0,
        **kwargs: Any,
    ):
        super().__init__(
            base_url=base_url,
            mock_mode=mock_mode,
            mock_responses=mock_responses,
            timeout_seconds=timeout_seconds,
            provider=kwargs.get("provider", "ollama"),
            api_key=kwargs.get("api_key"),
            headers=kwargs.get("headers"),
            skip_unload=kwargs.get("skip_unload"),
        )

