"""Optional strictly local Ollama semantic embedder for *predicted* rationale text.

The scoring oracle's withheld rationale is never sent to this service. Instead
historical annotations must carry independently generated, model-version-pinned
vectors to enable a legitimate semantic comparison.
"""

from __future__ import annotations

import json
import math
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


class LocalOllamaEmbedder:
    """Use /api/embed with no remote provider, no telemetry, and immediate unload."""

    def __init__(
        self,
        model: str = "nomic-embed-text",
        base_url: str = "http://127.0.0.1:11434",
        timeout_seconds: float = 60.0,
    ) -> None:
        address = urlsplit(base_url)
        if (
            address.scheme != "http"
            or address.hostname not in {"localhost", "127.0.0.1", "::1"}
            or address.username or address.password
            or (address.path not in {"", "/"})
            or address.query or address.fragment
        ):
            raise ValueError("Embedding endpoint must be loopback-only Ollama HTTP")
        if not model.strip():
            raise ValueError("Embedding model identifier is required")
        self.model = model
        self.endpoint = base_url.rstrip("/") + "/api/embed"
        self.timeout_seconds = timeout_seconds

    def __call__(self, text: str) -> list[float]:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Embedding source text must be nonempty Unicode")
        body = json.dumps({
            "model": self.model, "input": text, "keep_alive": 0,
        }).encode("utf-8")
        request = Request(
            self.endpoint, data=body,
            headers={"Content-Type": "application/json"}, method="POST",
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
