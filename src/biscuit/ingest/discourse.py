"""Discourse REST API ingestion adapter for discussions and topics."""

import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import httpx
from dateutil import parser as dt_parser

from biscuit.ingest.base import BaseIngestAdapter, RawMessagePayload, RawThreadPayload
from biscuit.models.enums import PlatformType

# Topic URL shapes: /t/{slug}/{id}, /t/{slug}/{id}/{post_number}, /t/{id}
_TOPIC_URL_PATTERN = re.compile(
    r"^/t/(?:[^/]+/)?(\d+)(?:/\d+)?/?$"
)

DEFAULT_ALLOWED_HOSTS = (
    "discuss.python.org",
    "discourse.python.org",
)


class DiscourseIncompletenessError(RuntimeError):
    """Raised when a Discourse stream cannot be fully retrieved after fetching.

    A partial 200 response (posts missing due to moderation, deletion, or
    transient API behavior) must never be silently treated as a complete
    thread: a dropped maintainer ruling would corrupt downstream decisions.
    """


class DiscourseAdapter(BaseIngestAdapter):
    """Adapter for ingesting Discourse topic JSON payloads (/t/{id}.json) and paginated streams."""

    def __init__(self, allowed_hosts: tuple[str, ...] | None = None) -> None:
        self.allowed_hosts = allowed_hosts or DEFAULT_ALLOWED_HOSTS

    @property
    def platform(self) -> PlatformType:
        return PlatformType.DISCOURSE

    @staticmethod
    def fetch_topic_posts_chunk(
        base_url: str,
        topic_id: str | int,
        post_ids: list[int],
        client: httpx.Client | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch a batch of posts by post IDs using Discourse REST API."""
        if not post_ids:
            return []
        url = f"{base_url.rstrip('/')}/t/{topic_id}/posts.json"
        params = [("post_ids[]", pid) for pid in post_ids]
        if client:
            resp = client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
            return data.get("post_stream", {}).get("posts", [])
        with httpx.Client(timeout=30.0) as local_client:
            resp = local_client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
            return data.get("post_stream", {}).get("posts", [])

    @staticmethod
    async def async_fetch_topic_posts_chunk(
        base_url: str,
        topic_id: str | int,
        post_ids: list[int],
        client: httpx.AsyncClient | None = None,
    ) -> list[dict[str, Any]]:
        """Asynchronously fetch a batch of posts by post IDs using Discourse REST API."""
        if not post_ids:
            return []
        url = f"{base_url.rstrip('/')}/t/{topic_id}/posts.json"
        params = [("post_ids[]", pid) for pid in post_ids]
        if client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
            return data.get("post_stream", {}).get("posts", [])
        async with httpx.AsyncClient(timeout=30.0) as local_client:
            resp = await local_client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
            return data.get("post_stream", {}).get("posts", [])

    def parse_topic_url(self, topic_url: str) -> tuple[str, str]:
        """Extract (base_url, topic_id) from a canonical Discourse topic URL.

        Enforces the allowed-host policy: URLs pointing outside the configured
        hosts are rejected rather than fetched blindly.
        """
        parsed = urlparse(topic_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError(f"Invalid Discourse topic URL: {topic_url}")
        if parsed.hostname not in self.allowed_hosts:
            raise ValueError(
                f"Discourse host {parsed.hostname!r} is not in the allowed hosts "
                f"{list(self.allowed_hosts)}"
            )
        match = _TOPIC_URL_PATTERN.match(parsed.path)
        if not match:
            raise ValueError(
                f"URL path {parsed.path!r} does not match a Discourse topic URL"
            )
        base_url = f"{parsed.scheme}://{parsed.netloc}"
        return base_url, match.group(1)

    def fetch_topic_from_url(
        self,
        topic_url: str,
        client: httpx.Client | None = None,
    ) -> dict[str, Any]:
        """Fetch the topic JSON (/t/{id}.json) from a supplied topic URL."""
        base_url, topic_id = self.parse_topic_url(topic_url)
        url = f"{base_url.rstrip('/')}/t/{topic_id}.json"
        if client:
            resp = client.get(url)
            resp.raise_for_status()
            return resp.json()
        with httpx.Client(timeout=30.0) as local_client:
            resp = local_client.get(url)
            resp.raise_for_status()
            return resp.json()

    async def async_fetch_topic_from_url(
        self,
        topic_url: str,
        client: httpx.AsyncClient | None = None,
    ) -> dict[str, Any]:
        """Asynchronously fetch the topic JSON from a supplied topic URL."""
        base_url, topic_id = self.parse_topic_url(topic_url)
        url = f"{base_url.rstrip('/')}/t/{topic_id}.json"
        if client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.json()
        async with httpx.AsyncClient(timeout=30.0) as local_client:
            resp = await local_client.get(url)
            resp.raise_for_status()
            return resp.json()

    def parse_payload(
        self,
        data: dict[str, Any],
        project_context: str,
        base_url: str = "https://discuss.python.org",
        additional_posts: list[dict[str, Any]] | None = None,
        client: httpx.Client | None = None,
        chunk_size: int = 50,
        strict_completeness: bool = True,
        **kwargs: Any,
    ) -> RawThreadPayload:
        """Parse Discourse topic JSON dict into canonical RawThreadPayload.

        Handles pagination via post_stream.stream when post IDs exceed the
        embedded posts, resolving reply_to_post_number into post_{id} namespace.

        When ``strict_completeness`` is set (default), a stream that cannot be
        fully retrieved raises :class:`DiscourseIncompletenessError` instead of
        silently returning a truncated thread. Callers may pass
        ``strict_completeness=False`` to accept partial data; unavailable posts
        are then recorded in ``ingestion_metadata`` as explicit
        ``unavailable_post_ids``.
        """
        topic_id = str(data.get("id", ""))
        title = data.get("title", f"Discourse Topic {topic_id}")
        slug = data.get("slug", "")

        canonical_url = f"{base_url.rstrip('/')}/t/{slug}/{topic_id}".rstrip("/")

        post_stream = data.get("post_stream", {})
        posts_data: list[dict[str, Any]] = list(post_stream.get("posts", []))

        # If posts are provided at top level
        if not posts_data and "posts" in data:
            posts_data = list(data["posts"])

        if additional_posts:
            posts_data.extend(additional_posts)

        # Check for un-fetched posts in the stream
        stream_ids: list[int] = post_stream.get("stream", [])
        loaded_ids = {p.get("id") for p in posts_data if "id" in p}
        missing_ids = [pid for pid in stream_ids if pid not in loaded_ids]

        if missing_ids:
            if client is not None:
                for i in range(0, len(missing_ids), chunk_size):
                    chunk = missing_ids[i : i + chunk_size]
                    fetched_posts = self.fetch_topic_posts_chunk(
                        base_url=base_url,
                        topic_id=topic_id,
                        post_ids=chunk,
                        client=client,
                    )
                    posts_data.extend(fetched_posts)
            elif base_url:
                with httpx.Client() as internal_client:
                    for i in range(0, len(missing_ids), chunk_size):
                        chunk = missing_ids[i : i + chunk_size]
                        fetched_posts = self.fetch_topic_posts_chunk(
                            base_url=base_url,
                            topic_id=topic_id,
                            post_ids=chunk,
                            client=internal_client,
                        )
                        posts_data.extend(fetched_posts)
            else:
                raise ValueError(
                    f"Discourse stream contains {len(missing_ids)} un-fetched post IDs "
                    "but neither an HTTP client nor a valid base_url was supplied."
                )

        # Completeness validation: every stream ID must be accounted for after
        # fetching, either as an embedded/fetched post or an explicit tombstone.
        final_ids = {p.get("id") for p in posts_data if "id" in p}
        unavailable_ids = [pid for pid in stream_ids if pid not in final_ids]
        if unavailable_ids and strict_completeness:
            raise DiscourseIncompletenessError(
                f"Discourse topic {topic_id} stream contains {len(unavailable_ids)} "
                f"post IDs not returned by the API: {unavailable_ids[:10]}"
                + ("..." if len(unavailable_ids) > 10 else "")
            )

        # Build post_number -> id lookup map for exact reply resolving
        post_num_to_id: dict[int, str] = {}
        for p in posts_data:
            if "post_number" in p and "id" in p:
                post_num_to_id[p["post_number"]] = str(p["id"])

        messages: list[RawMessagePayload] = []
        for post in posts_data:
            post_id = str(post.get("id", ""))
            post_number = post.get("post_number", 1)

            # Raw/cooked provenance separation: prefer the exact raw Markdown
            # whenever the key is present (even empty); only fall back to the
            # rendered HTML when raw is genuinely unavailable, and record the
            # representation actually used so evidence verification never
            # mistakes one document format for the other.
            if "raw" in post:
                raw_content = post.get("raw") or ""
                content_representation = "raw"
            else:
                raw_content = post.get("cooked", "")
                content_representation = "cooked"

            author_platform_id = str(post.get("user_id", "0"))
            author_handle = post.get("username", "anonymous")
            author_name = post.get("name") or author_handle

            # Parse timestamp
            created_str = post.get("created_at")
            if created_str:
                orig_dt = dt_parser.isoparse(created_str)
                if orig_dt.tzinfo is None:
                    orig_dt = orig_dt.replace(tzinfo=UTC)
            else:
                orig_dt = datetime.now(UTC)

            reply_to_num = post.get("reply_to_post_number")
            if reply_to_num is not None:
                # Map to post_{id} if target is in topic, else fallback to post_num_{num}
                target_id = post_num_to_id.get(reply_to_num)
                parent_msg_id = f"post_{target_id}" if target_id else f"post_num_{reply_to_num}"
            else:
                parent_msg_id = None

            post_permalink = f"{canonical_url}/{post_number}"

            # Upstream deletion markers (distinguished from moderation flags)
            is_deleted = bool(
                post.get("deleted_at")
                or post.get("user_deleted")
            )
            delete_reason = "Discourse post marked deleted by user/moderator" if is_deleted else None
            is_hidden = bool(post.get("hidden", False))

            msg_payload = RawMessagePayload(
                external_message_id=f"post_{post_id}",
                parent_message_id=parent_msg_id,
                author_platform_id=author_platform_id,
                author_handle=author_handle,
                author_name=author_name,
                author_email_prefix=None,
                original_timestamp=orig_dt,
                canonical_permalink=post_permalink,
                raw_content=raw_content,
                is_deleted=is_deleted,
                delete_reason=delete_reason,
                metadata={
                    "post_number": post_number,
                    "reads": post.get("reads", 0),
                    "score": post.get("score", 0),
                    "reply_count": post.get("reply_count", 0),
                    "hidden": is_hidden,
                    "content_representation": content_representation,
                },
            )
            messages.append(msg_payload)

        ingestion_metadata: dict[str, Any] = {
            "base_url": base_url,
            "topic_id": topic_id,
            "posts_count": data.get("posts_count", len(messages)),
            "stream_total": len(stream_ids),
        }
        if unavailable_ids:
            ingestion_metadata["unavailable_post_ids"] = unavailable_ids
        if any(m.metadata.get("content_representation") == "cooked" for m in messages):
            ingestion_metadata["contains_cooked_only_posts"] = True

        return RawThreadPayload(
            source_platform=self.platform,
            project_context=project_context,
            external_thread_id=f"topic_{topic_id}",
            title=title,
            canonical_url=canonical_url,
            source_revision=kwargs.get("source_revision", "v1"),
            ingestion_metadata=ingestion_metadata,
            messages=messages,
        )
