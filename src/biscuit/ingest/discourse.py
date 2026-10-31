"""Discourse REST API ingestion adapter for discussions and topics."""

from datetime import UTC, datetime
from typing import Any

from dateutil import parser as dt_parser

from biscuit.ingest.base import BaseIngestAdapter, RawMessagePayload, RawThreadPayload
from biscuit.models.enums import PlatformType


class DiscourseAdapter(BaseIngestAdapter):
    """Adapter for ingesting Discourse topic JSON payloads (/t/{id}.json)."""

    @property
    def platform(self) -> PlatformType:
        return PlatformType.DISCOURSE

    def parse_payload(
        self,
        data: dict[str, Any],
        project_context: str,
        base_url: str = "https://discuss.python.org",
        **kwargs: Any,
    ) -> RawThreadPayload:
        """Parse Discourse topic JSON dict into canonical RawThreadPayload."""
        topic_id = str(data.get("id", ""))
        title = data.get("title", f"Discourse Topic {topic_id}")
        slug = data.get("slug", "")

        canonical_url = f"{base_url.rstrip('/')}/t/{slug}/{topic_id}".rstrip("/")

        post_stream = data.get("post_stream", {})
        posts_data: list[dict[str, Any]] = post_stream.get("posts", [])

        # If posts are provided at top level
        if not posts_data and "posts" in data:
            posts_data = data["posts"]

        messages: list[RawMessagePayload] = []
        for post in posts_data:
            post_id = str(post.get("id", ""))
            post_number = post.get("post_number", 1)
            raw_content = post.get("raw") or post.get("cooked", "")

            # Discourse stores raw markdown in 'raw'
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
            parent_msg_id = f"post_{reply_to_num}" if reply_to_num else None
            post_permalink = f"{canonical_url}/{post_number}"

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
                metadata={
                    "post_number": post_number,
                    "reads": post.get("reads", 0),
                    "score": post.get("score", 0),
                    "reply_count": post.get("reply_count", 0),
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
            },
            messages=messages,
        )
