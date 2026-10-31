"""
Local LLM Client with Sequential Model Offloading.
Implements the HTTP interface to Ollama / llama.cpp with strict keep_alive: 0 offload.
Includes mock replay mode for deterministic CI testing without GPU/daemon.
"""

import os
import json
import urllib.request
import urllib.error
from typing import Dict, Optional


class LocalLLMClient:
    """
    Client for local open-source models (Llama 3.2 3B, Phi-3.5 mini, Qwen 2.5 Coder 7B).
    Enforces sequential memory offloading (keep_alive: 0) to adhere to the <=8 GB RAM budget.
    """

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        mock_mode: Optional[bool] = None,
        mock_responses: Optional[Dict[str, str]] = None,
        timeout_seconds: float = 60.0,
    ):
        self.base_url = base_url.rstrip("/")
        # Environment variable BISCUIT_MOCK_LLM=1 enables mock mode automatically (e.g. in CI)
        env_mock = os.getenv("BISCUIT_MOCK_LLM", "0") in ("1", "true", "True")
        self.mock_mode = env_mock if mock_mode is None else mock_mode
        self.mock_responses = mock_responses or {}
        self.timeout_seconds = timeout_seconds
        self.loaded_model: Optional[str] = None
        self.call_history: list = []

    def set_mock_response(self, key: str, response_json: str):
        """Register a deterministic mock JSON response for CI testing."""
        self.mock_responses[key] = response_json

    def generate(
        self, model: str, prompt: str, temperature: float = 0.0, keep_alive: int = 0
    ) -> str:
        """
        Generates text using the local model with strict memory offload.

        Args:
            model: Model identifier (e.g. 'llama3.2:3b', 'phi3.5', 'qwen2.5-coder:7b')
            prompt: Text prompt
            temperature: Sampling temperature (0.0 for deterministic schema extraction)
            keep_alive: Seconds to keep model in RAM (0 forces immediate unload post-generation)

        Returns:
            str: Generated raw response string (typically JSON)
        """
        self.call_history.append({"model": model, "prompt": prompt, "keep_alive": keep_alive})

        # 1. Deterministic Mock Mode (For CI / Offline Testing)
        if self.mock_mode:
            # Check for direct key match
            if model in self.mock_responses:
                return self.mock_responses[model]
            # Check for prompt keyword match
            for key, resp in self.mock_responses.items():
                if key in prompt:
                    return resp
            # Default empty json
            return "{}"

        # 2. Live HTTP Request to Ollama API
        payload = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "format": "json",
            "options": {
                "temperature": temperature,
            },
            # Non-negotiable invariant: keep_alive: 0 flushes weights from RAM immediately
            "keep_alive": f"{keep_alive}s" if keep_alive > 0 else 0,
        }

        endpoint = f"{self.base_url}/api/generate"
        req = urllib.request.Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
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
        """
        Explicitly unloads a model from RAM via Ollama API keep_alive: 0.
        Ensures sequential memory budget (<=8 GB) is maintained between passes.
        """
        if self.mock_mode:
            self.loaded_model = None
            return True

        payload = {"model": model, "keep_alive": 0}
        endpoint = f"{self.base_url}/api/generate"
        req = urllib.request.Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5.0):
                self.loaded_model = None
                return True
        except Exception:
            return False
