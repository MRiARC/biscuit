"""Discourse REST API ingestion adapter for discussions and topics."""

from datetime import UTC, datetime
from typing import Any

import httpx
from dateutil import parser as dt_parser

from biscuit.ingest.base import BaseIngestAdapter, RawMessagePayload, RawThreadPayload
from biscuit.models.enums import PlatformType


class DiscourseAdapter(BaseIngestAdapter):
    """Adapter for ingesting Discourse topic JSON payloads (/t/{id}.json) and paginated streams."""

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

    def parse_payload(
        self,
        data: dict[str, Any],
        project_context: str,
        base_url: str = "https://discuss.python.org",
        additional_posts: list[dict[str, Any]] | None = None,
        client: httpx.Client | None = None,
        chunk_size: int = 50,
        **kwargs: Any,
    ) -> RawThreadPayload:
        """Parse Discourse topic JSON dict into canonical RawThreadPayload.

        Handles pagination via post_stream.stream when post IDs exceed the embedded posts.
        Resolves reply_to_post_number into post_{id} namespace for graph integrity.
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

        # Build post_number -> id lookup map for exact reply resolving
        post_num_to_id: dict[int, str] = {}
        for p in posts_data:
            if "post_number" in p and "id" in p:
                post_num_to_id[p["post_number"]] = str(p["id"])

        messages: list[RawMessagePayload] = []
        for post in posts_data:
            post_id = str(post.get("id", ""))
            post_number = post.get("post_number", 1)
            raw_content = post.get("raw") or post.get("cooked", "")

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
                },
            )
            messages.append(msg_payload)

        return RawThreadPayload(
            source_platform=self.platform,
            project_context=project_context,
            external_thread_id=f"topic_{topic_id}",
            title=title,
            canonical_url=canonical_url,
            source_revision=kwargs.get("source_revision", "v1"),
            ingestion_metadata={
                "base_url": base_url,
                "topic_id": topic_id,
                "posts_count": data.get("posts_count", len(messages)),
                "stream_total": len(stream_ids),
            },
            messages=messages,
        )
