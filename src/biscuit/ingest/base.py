"""Abstract interfaces and payloads for universal thread ingestion."""

import abc
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from biscuit.models.enums import PlatformType


class RawMessagePayload(BaseModel):
    """Normalized wire payload for an individual message in a thread."""

    external_message_id: str = Field(
        ..., description="Unique message ID on the source platform"
    )
    parent_message_id: str | None = Field(
        default=None, description="Platform ID of replied-to parent message"
    )
    author_platform_id: str = Field(
        ..., description="Platform-scoped user or author ID"
    )
    author_handle: str = Field(
        ..., description="User handle or username on platform"
    )
    author_name: str = Field(..., description="Human display name of author")
    author_email_prefix: str | None = Field(
        default=None, description="Sanitized email prefix for mailing list posts"
    )
    original_timestamp: datetime = Field(
        ..., description="Timestamp when message was originally published"
    )
    canonical_permalink: str = Field(
        ..., description="Direct permalink to this specific message"
    )
    raw_content: str = Field(
        ...,
        description="Exact decoded wire string as received; NEVER normalized or trimmed",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict, description="Platform-specific message metadata"
    )


class RawThreadPayload(BaseModel):
    """Normalized wire payload for an entire discussion thread."""

    source_platform: PlatformType = Field(
        ..., description="Canonical platform type"
    )
    project_context: str = Field(
        ..., description="Target repository or project context, e.g. python/cpython"
    )
    external_thread_id: str = Field(
        ..., description="Platform thread identifier, e.g. topic_12345"
    )
    title: str = Field(..., description="Thread title / discussion subject")
    canonical_url: str = Field(
        ..., description="Canonical URL to full discussion thread"
    )
    source_revision: str = Field(
        default="v1", description="Ingestion revision or snapshot tag"
    )
    ingestion_metadata: dict[str, Any] = Field(
        default_factory=dict, description="Thread-level metadata from source API"
    )
    messages: list[RawMessagePayload] = Field(
        default_factory=list, description="Ordered list of messages in thread"
    )


class BaseIngestAdapter(abc.ABC):
    """Abstract base class for all source platform ingestion adapters."""

    @property
    @abc.abstractmethod
    def platform(self) -> PlatformType:
        """The platform type this adapter ingests."""

    @abc.abstractmethod
    def parse_payload(
        self, data: Any, project_context: str, **kwargs: Any
    ) -> RawThreadPayload:
        """Parse raw platform payload into canonical RawThreadPayload."""
