"""Biscuit universal ingestion engine and source adapters."""

from biscuit.ingest.base import (
    BaseIngestAdapter,
    RawMessagePayload,
    RawThreadPayload,
)
from biscuit.ingest.cleaner import (
    clean_derived_content,
    is_bot_message,
    normalize_line_endings,
)
from biscuit.ingest.discourse import DiscourseAdapter
from biscuit.ingest.github import GitHubAdapter
from biscuit.ingest.hasher import compute_sha256, verify_sha256
from biscuit.ingest.mbox import MboxAdapter
from biscuit.ingest.service import IngestResult, IngestService

__all__ = [
    "BaseIngestAdapter",
    "DiscourseAdapter",
    "GitHubAdapter",
    "IngestResult",
    "IngestService",
    "MboxAdapter",
    "RawMessagePayload",
    "RawThreadPayload",
    "clean_derived_content",
    "compute_sha256",
    "is_bot_message",
    "normalize_line_endings",
    "verify_sha256",
]
