"""Semantic embedders for predicted rationale text.

Supports strictly local loopback Ollama embedders as well as remote OpenAI-compatible
embedding APIs (OpenAI, Voyage, Azure, LiteLLM, vLLM, etc.).

The scoring oracle's withheld rationale is never sent to this service. Instead
historical annotations must carry independently generated, model-version-pinned
vectors to enable a legitimate semantic comparison.
"""

from __future__ import annotations

import json
import math
import os
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


class LocalOllamaEmbedder:
    """Use Ollama /api/embed with optional loopback enforcement and immediate unload."""

    def __init__(
        self,
        model: str = "nomic-embed-text",
        base_url: str = "http://127.0.0.1:11434",
        timeout_seconds: float = 60.0,
        allow_remote: bool = False,
    ) -> None:
        address = urlsplit(base_url)
        if not allow_remote and (
            address.scheme != "http"
            or address.hostname not in {"localhost", "127.0.0.1", "::1"}
            or address.username
            or address.password
            or (address.path not in {"", "/"})
            or address.query
            or address.fragment
        ):
            raise ValueError("Embedding endpoint must be loopback-only Ollama HTTP")
        if not model.strip():
            raise ValueError("Embedding model identifier is required")
        self.model = model
        endpoint = base_url.rstrip("/")
        if endpoint.endswith("/api/embed"):
            self.endpoint = endpoint
        elif endpoint.endswith("/api"):
            self.endpoint = f"{endpoint}/embed"
        else:
            self.endpoint = f"{endpoint}/api/embed"
        self.timeout_seconds = timeout_seconds

    def __call__(self, text: str) -> list[float]:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Embedding source text must be nonempty Unicode")
        body = json.dumps(
            {
                "model": self.model,
                "input": text,
                "keep_alive": 0,
            }
        ).encode("utf-8")
        request = Request(
            self.endpoint,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=self.timeout_seconds) as response:
            raw = response.read(4_000_001)
        if len(raw) > 4_000_000:
            raise ValueError("Embedding response exceeds 4 MB limit")
        payload = json.loads(raw)
        embeddings = payload.get("embeddings")
        if not isinstance(embeddings, list) or len(embeddings) != 1:
            raise ValueError("Ollama embedder returned an invalid embedding batch")
        vector = embeddings[0]
        if (
            not isinstance(vector, list)
            or not vector
            or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in vector)
            or not any(float(v) != 0 for v in vector)
        ):
            raise ValueError("Ollama embedder returned an invalid numeric vector")
        return [float(v) for v in vector]


class OpenAIEmbedder:
    """OpenAI-compatible semantic embedder supporting remote APIs and authentication."""

    def __init__(
        self,
        model: str = "text-embedding-3-small",
        base_url: str | None = None,
        api_key: str | None = None,
        timeout_seconds: float = 60.0,
        headers: dict[str, str] | None = None,
    ) -> None:
        resolved_base = (
            base_url
            or os.getenv("BISCUIT_EMBEDDING_BASE_URL")
            or os.getenv("OPENAI_BASE_URL")
            or "https://api.openai.com/v1"
        ).rstrip("/")
        if resolved_base.endswith("/embeddings"):
            self.endpoint = resolved_base
        elif resolved_base.endswith("/v1"):
            self.endpoint = f"{resolved_base}/embeddings"
        else:
            self.endpoint = f"{resolved_base}/v1/embeddings"

        self.model = model.strip()
        if not self.model:
            raise ValueError("Embedding model identifier is required")

        self.api_key = (
            api_key or os.getenv("BISCUIT_EMBEDDING_API_KEY") or os.getenv("OPENAI_API_KEY")
        )
        self.timeout_seconds = timeout_seconds
        self.extra_headers = headers or {}

    def __call__(self, text: str) -> list[float]:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Embedding source text must be nonempty Unicode")
        body = json.dumps(
            {
                "model": self.model,
                "input": text,
            }
        ).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if self.extra_headers:
            headers.update(self.extra_headers)

        request = Request(self.endpoint, data=body, headers=headers, method="POST")
        with urlopen(request, timeout=self.timeout_seconds) as response:
            raw = response.read(8_000_001)
        if len(raw) > 8_000_000:
            raise ValueError("Embedding response exceeds 8 MB limit")
        payload = json.loads(raw)
        data = payload.get("data")
        if not isinstance(data, list) or len(data) < 1:
            raise ValueError("Embedder returned an invalid embedding response")
        vector = data[0].get("embedding")
        if (
            not isinstance(vector, list)
            or not vector
            or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in vector)
            or not any(float(v) != 0 for v in vector)
        ):
            raise ValueError("Embedder returned an invalid numeric vector")
        return [float(v) for v in vector]


def get_embedder(
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    provider: str = "auto",
    timeout_seconds: float = 60.0,
) -> LocalOllamaEmbedder | OpenAIEmbedder:
    """Factory creating the appropriate semantic embedder from args and environment."""
    env_provider = os.getenv("BISCUIT_EMBEDDING_PROVIDER") or os.getenv("BISCUIT_LLM_PROVIDER")
    resolved_provider = (provider if provider != "auto" else env_provider) or "auto"
    resolved_provider = resolved_provider.lower().strip()

    resolved_key = api_key or os.getenv("BISCUIT_EMBEDDING_API_KEY") or os.getenv("OPENAI_API_KEY")
    resolved_base = (
        base_url or os.getenv("BISCUIT_EMBEDDING_BASE_URL") or os.getenv("OPENAI_BASE_URL")
    )

    # Explicit provider selection takes precedence over auto-detection
    if resolved_provider == "ollama":
        resolved_model = model or os.getenv("BISCUIT_EMBEDDING_MODEL") or "nomic-embed-text"
        allow_remote = bool(
            resolved_base
            and not any(
                resolved_base.startswith(prefix)
                for prefix in ("http://localhost", "http://127.0.0.1", "http://::1")
            )
        )
        return LocalOllamaEmbedder(
            model=resolved_model,
            base_url=resolved_base or "http://127.0.0.1:11434",
            timeout_seconds=timeout_seconds,
            allow_remote=allow_remote,
        )

    if (
        resolved_provider in ("openai", "remote")
        or resolved_key
        or (resolved_base and ("api.openai.com" in resolved_base or "/v1" in resolved_base))
    ):
        return OpenAIEmbedder(
            model=model or os.getenv("BISCUIT_EMBEDDING_MODEL") or "text-embedding-3-small",
            base_url=resolved_base,
            api_key=resolved_key,
            timeout_seconds=timeout_seconds,
        )

    if resolved_base and not any(
        resolved_base.startswith(prefix)
        for prefix in ("http://localhost", "http://127.0.0.1", "http://::1")
    ):
        return LocalOllamaEmbedder(
            model=model or os.getenv("BISCUIT_EMBEDDING_MODEL") or "nomic-embed-text",
            base_url=resolved_base,
            timeout_seconds=timeout_seconds,
            allow_remote=True,
        )

    return LocalOllamaEmbedder(
        model=model or os.getenv("BISCUIT_EMBEDDING_MODEL") or "nomic-embed-text",
        base_url=resolved_base or "http://127.0.0.1:11434",
        timeout_seconds=timeout_seconds,
    )
