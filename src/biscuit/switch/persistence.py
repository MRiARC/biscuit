"""Persistent SQLite-backed transcript and evidence store.

Preserves exact decoded message content (UTF-8 byte SHA-256 digests),
append-only message versions, exact Unicode code-point evidence spans, and
draft living decisions across restarts.
"""

from __future__ import annotations

import hashlib
import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Any

from biscuit.switch.types import (
    LiveMessagePayload,
    LivingDecisionDraft,
)


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class TranscriptStore:
    """Durably stores transcripts, versioned messages, and evidence spans."""

    def __init__(self, db_path: str = ":memory:") -> None:
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self) -> None:
        with self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS source_threads (
                    id TEXT PRIMARY KEY,
                    switch_session_id TEXT NOT NULL,
                    source_platform TEXT NOT NULL,
                    channel_id TEXT NOT NULL,
                    thread_ts TEXT NOT NULL,
                    initiating_author_handle TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(source_platform, channel_id, thread_ts)
                );

                CREATE TABLE IF NOT EXISTS source_messages (
                    id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL REFERENCES source_threads(id),
                    external_message_id TEXT NOT NULL,
                    parent_message_id TEXT,
                    author_platform_id TEXT NOT NULL,
                    author_handle TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(thread_id, external_message_id)
                );

                CREATE TABLE IF NOT EXISTS source_message_versions (
                    id TEXT PRIMARY KEY,
                    message_id TEXT NOT NULL REFERENCES source_messages(id),
                    version_num INTEGER NOT NULL,
                    raw_content TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    is_current INTEGER NOT NULL DEFAULT 1,
                    UNIQUE(message_id, version_num)
                );

                CREATE TABLE IF NOT EXISTS living_decision_drafts (
                    id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL REFERENCES source_threads(id),
                    switch_session_id TEXT NOT NULL,
                    topic TEXT NOT NULL,
                    proposal_text TEXT NOT NULL,
                    draft_status TEXT NOT NULL,
                    initiating_author_handle TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS evidence_spans (
                    id TEXT PRIMARY KEY,
                    draft_id TEXT NOT NULL REFERENCES living_decision_drafts(id),
                    source_message_version_id TEXT NOT NULL REFERENCES source_message_versions(id),
                    exact_quote TEXT NOT NULL,
                    start_char INTEGER NOT NULL,
                    end_char INTEGER NOT NULL,
                    content_hash TEXT NOT NULL,
                    verification_status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

    def record_thread(
        self,
        session_id: str,
        platform: str,
        channel_id: str,
        thread_ts: str,
        initiating_author_handle: str,
    ) -> str:
        """Find or create a canonical source thread."""
        with self._conn:
            row = self._conn.execute(
                """
                SELECT id FROM source_threads
                WHERE source_platform = ? AND channel_id = ? AND thread_ts = ?
                """,
                (platform, channel_id, thread_ts),
            ).fetchone()
            if row:
                return str(row["id"])

            thread_id = str(uuid.uuid4())
            self._conn.execute(
                """
                INSERT INTO source_threads (
                    id, switch_session_id, source_platform, channel_id,
                    thread_ts, initiating_author_handle, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    thread_id,
                    session_id,
                    platform,
                    channel_id,
                    thread_ts,
                    initiating_author_handle,
                    _utc_now_iso(),
                ),
            )
            return thread_id

    def record_message(self, thread_id: str, message: LiveMessagePayload) -> tuple[str, str]:
        """Record an incoming message or remote edit into append-only versions."""
        with self._conn:
            row = self._conn.execute(
                """
                SELECT id FROM source_messages
                WHERE thread_id = ? AND external_message_id = ?
                """,
                (thread_id, message.external_message_id),
            ).fetchone()

            if row:
                message_id = str(row["id"])
            else:
                message_id = str(uuid.uuid4())
                self._conn.execute(
                    """
                    INSERT INTO source_messages (
                        id, thread_id, external_message_id, parent_message_id,
                        author_platform_id, author_handle, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        message_id,
                        thread_id,
                        message.external_message_id,
                        message.parent_message_id,
                        message.author_platform_id,
                        message.author_handle,
                        _utc_now_iso(),
                    ),
                )

            # Determine next monotonic version number
            v_row = self._conn.execute(
                """
                SELECT MAX(version_num) AS max_v FROM source_message_versions
                WHERE message_id = ?
                """,
                (message_id,),
            ).fetchone()
            max_v = v_row["max_v"] if v_row and v_row["max_v"] is not None else 0
            version_num = max_v + 1

            if max_v > 0:
                self._conn.execute(
                    """
                    UPDATE source_message_versions
                    SET is_current = 0
                    WHERE message_id = ?
                    """,
                    (message_id,),
                )

            version_id = str(uuid.uuid4())
            content_hash = _sha256_text(message.raw_content)
            self._conn.execute(
                """
                INSERT INTO source_message_versions (
                    id, message_id, version_num, raw_content,
                    content_hash, created_at, is_current
                ) VALUES (?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    version_id,
                    message_id,
                    version_num,
                    message.raw_content,
                    content_hash,
                    _utc_now_iso(),
                ),
            )
            return message_id, version_id

    def save_draft(
        self,
        draft: LivingDecisionDraft,
        thread_id: str,
        evidence_spans: list[dict[str, Any]],
    ) -> None:
        """Persist a draft living decision and its associated evidence spans."""
        with self._conn:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO living_decision_drafts (
                    id, thread_id, switch_session_id, topic,
                    proposal_text, draft_status, initiating_author_handle, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    draft.id,
                    thread_id,
                    draft.switch_session_id,
                    draft.topic,
                    draft.proposal_text,
                    draft.draft_status.value,
                    draft.initiating_author_handle,
                    _utc_now_iso(),
                ),
            )

            for span in evidence_spans:
                span_id = span.get("id") or str(uuid.uuid4())
                self._conn.execute(
                    """
                    INSERT OR REPLACE INTO evidence_spans (
                        id, draft_id, source_message_version_id, exact_quote,
                        start_char, end_char, content_hash, verification_status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        span_id,
                        draft.id,
                        span["source_message_version_id"],
                        span["exact_quote"],
                        span["start_char"],
                        span["end_char"],
                        span["content_hash"],
                        span.get("verification_status", "UNVERIFIED"),
                        _utc_now_iso(),
                    ),
                )

    def get_draft(self, draft_id: str) -> dict[str, Any] | None:
        """Fetch a saved draft and its evidence spans."""
        with self._conn:
            row = self._conn.execute(
                "SELECT * FROM living_decision_drafts WHERE id = ?", (draft_id,)
            ).fetchone()
            if not row:
                return None
            draft = dict(row)
            spans = self._conn.execute(
                "SELECT * FROM evidence_spans WHERE draft_id = ? ORDER BY created_at ASC",
                (draft_id,),
            ).fetchall()
            draft["evidence_spans"] = [dict(s) for s in spans]
            return draft

    def get_message_versions(self, external_message_id: str) -> list[dict[str, Any]]:
        """Fetch all versions of a message by external message ID."""
        with self._conn:
            rows = self._conn.execute(
                """
                SELECT smv.* FROM source_message_versions smv
                JOIN source_messages sm ON sm.id = smv.message_id
                WHERE sm.external_message_id = ?
                ORDER BY smv.version_num ASC
                """,
                (external_message_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def verify_span(self, span_id: str) -> bool:
        """Verify an evidence span against its source message version's raw content."""
        with self._conn:
            row = self._conn.execute(
                """
                SELECT es.*, smv.raw_content
                FROM evidence_spans es
                JOIN source_message_versions smv ON smv.id = es.source_message_version_id
                WHERE es.id = ?
                """,
                (span_id,),
            ).fetchone()
            if not row:
                return False

            raw_content = row["raw_content"]
            exact_quote = row["exact_quote"]
            start_char = row["start_char"]
            end_char = row["end_char"]
            content_hash = row["content_hash"]

            # Exact Unicode substring slice
            if raw_content[start_char:end_char] != exact_quote:
                return False

            # Byte-for-byte UTF-8 hash match
            if _sha256_text(exact_quote) != content_hash:
                return False

            self._conn.execute(
                "UPDATE evidence_spans SET verification_status = ? WHERE id = ?",
                ("VERIFIED_EXACT", span_id),
            )
            return True

    def close(self) -> None:
        """Close the underlying database connection."""
        self._conn.close()
