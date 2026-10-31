"""HTTP client for the Switch agent-session activity API.

``SwitchActivityClient`` talks to Switch agent-controller **0.31.0** at the
pinned upstream commit ``7e57cd433a922bc91a00150c106e6ef1f15b1057`` through
its published HTTP boundary only (Protocol Separation invariant: Biscuit
never imports Switch's internal Python packages):

* ``POST /agent-sessions/{session_id}/approvals`` — open an approval request
  (``ApprovalOpen``: request_id, turn_id, kind, title, detail, options,
  room_id, thread_id, expires_at).
* ``POST /agent-sessions/{session_id}/approvals/{request_id}/close`` — force
  close a request.
* ``POST /agent-sessions/{session_id}/approvals/{request_id}/delivered`` —
  acknowledge delivery of the card.
* ``GET  /agent-sessions/approvals/outcomes`` — poll answered/expired
  approvals not yet acknowledged.

The client is transport-injected: an ``httpx.AsyncClient``-compatible object
(or a mock) is passed in, so tests exercise the full request/response cycle
without a live Switch host.
"""

from __future__ import annotations

from typing import Any, Protocol

from biscuit.switch.types import (
    SWITCH_UPSTREAM_COMMIT,
    ApprovalOutcome,
    HITLApprovalRequest,
)

DEFAULT_BASE_URL = "http://switch.local:8787"
DEFAULT_TIMEOUT_SECONDS = 10.0


class AsyncTransport(Protocol):
    """Minimal async HTTP surface (satisfied by ``httpx.AsyncClient``)."""

    async def post(self, url: str, json: dict | None = None, **kwargs: Any) -> Any: ...

    async def get(self, url: str, **kwargs: Any) -> Any: ...


class MockTransport:
    """In-memory transport recording requests and replaying canned responses."""

    def __init__(
        self,
        responses: dict[tuple[str, str], Any] | None = None,
        default_post: dict | None = None,
    ) -> None:
        self.requests: list[dict] = []
        self._responses = responses or {}
        self._default_post = default_post or {"recorded": True}

    async def post(self, url: str, json: dict | None = None, **kwargs: Any) -> Any:
        self.requests.append({"method": "POST", "url": url, "json": json, **kwargs})
        response = self._responses.get(("POST", url))
        return _MockResponse(response if response is not None else self._default_post)

    async def get(self, url: str, **kwargs: Any) -> Any:
        self.requests.append({"method": "GET", "url": url, **kwargs})
        response = self._responses.get(("GET", url))
        return _MockResponse(response if response is not None else [])


class _MockResponse:
    """Duck-typed response with ``raise_for_status`` and ``json()``."""

    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code} from Switch")

    def json(self) -> Any:
        return self._payload


class SwitchActivityClient:
    """Thin async client over Switch's agent-session activity routes."""

    def __init__(
        self,
        transport: AsyncTransport,
        base_url: str = DEFAULT_BASE_URL,
        api_key: str | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        upstream_commit: str = SWITCH_UPSTREAM_COMMIT,
    ) -> None:
        self._transport = transport
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout_seconds
        self.upstream_commit = upstream_commit

    # ── request headers ─────────────────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._api_key}" if self._api_key else "",
            "X-Biscuit-Switch-Upstream-Commit": self.upstream_commit,
        }
        return {k: v for k, v in headers.items() if v}

    # ── activity API ────────────────────────────────────────────────────

    async def open_approval(
        self,
        session_id: str,
        payload: dict,
    ) -> dict:
        """``POST /agent-sessions/{session_id}/approvals`` — open a request.

        ``payload`` is the ``ApprovalOpen`` body built by
        :class:`biscuit.switch.cards.BlockKitBuilder`.
        """
        url = f"{self._base_url}/agent-sessions/{session_id}/approvals"
        response = await self._transport.post(url, json=payload, headers=self._headers())
        response.raise_for_status()
        return response.json()

    async def close_approval(self, session_id: str, request_id: str) -> dict:
        """``POST /agent-sessions/{session_id}/approvals/{request_id}/close``."""
        url = f"{self._base_url}/agent-sessions/{session_id}/approvals/{request_id}/close"
        response = await self._transport.post(url, json={}, headers=self._headers())
        response.raise_for_status()
        return response.json()

    async def mark_delivered(self, session_id: str, request_id: str) -> dict:
        """``POST /agent-sessions/{session_id}/approvals/{request_id}/delivered``."""
        url = f"{self._base_url}/agent-sessions/{session_id}/approvals/{request_id}/delivered"
        response = await self._transport.post(url, json={}, headers=self._headers())
        response.raise_for_status()
        return response.json()

    async def poll_outcomes(self) -> list[ApprovalOutcome]:
        """``GET /agent-sessions/approvals/outcomes`` — unacknowledged answers.

        Returns parsed :class:`ApprovalOutcome` records oldest-first, as the
        upstream route documents.
        """
        url = f"{self._base_url}/agent-sessions/approvals/outcomes"
        response = await self._transport.get(url, headers=self._headers())
        response.raise_for_status()
        payload = response.json()
        return [ApprovalOutcome.from_json(item) for item in payload]

    # ── convenience orchestration ────────────────────────────────────────

    async def dispatch_approval(
        self,
        session_id: str,
        request: HITLApprovalRequest,
        approval_open_payload: dict,
    ) -> dict:
        """Open a Switch approval and acknowledge its rendered delivery.

        Sequence: ``POST .../approvals`` then ``POST .../delivered`` for the
        returned request id — the two calls a Biscuit agent makes when an
        interactive card has been shown to maintainers.
        """
        view = await self.open_approval(session_id, approval_open_payload)
        request_id = view.get("request_id") or approval_open_payload["request_id"]
        await self.mark_delivered(session_id, request_id)
        return view
