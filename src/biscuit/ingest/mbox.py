"""RFC-822 / Mbox mailing list archive ingestion adapter."""

import email
import email.policy
import email.utils
import mailbox
import os
import re
from datetime import UTC, datetime
from typing import Any

from biscuit.ingest.base import BaseIngestAdapter, RawMessagePayload, RawThreadPayload
from biscuit.models.enums import PlatformType


class MboxAdapter(BaseIngestAdapter):
    """Adapter for ingesting RFC-822 email archives and .mbox files."""

    @property
    def platform(self) -> PlatformType:
        return PlatformType.MBOX

    def _extract_body(self, msg: email.message.Message) -> str:
        """Extract exact raw text body from an email message preserving line endings."""
        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                content_disposition = str(part.get("Content-Disposition", ""))
                if content_type == "text/plain" and "attachment" not in content_disposition:
                    payload = part.get_payload(decode=True)
                    if payload:
                        charset = part.get_content_charset() or "utf-8"
                        try:
                            return payload.decode(charset)
                        except (UnicodeDecodeError, LookupError):
                            try:
                                return payload.decode("utf-8")
                            except UnicodeDecodeError:
                                return payload.decode("latin-1")
            payload = msg.get_payload(decode=True)
            if isinstance(payload, bytes):
                charset = msg.get_content_charset() or "utf-8"
                try:
                    return payload.decode(charset)
                except (UnicodeDecodeError, LookupError):
                    return payload.decode("latin-1")
            return str(payload or "")
        else:
            payload = msg.get_payload(decode=True)
            if isinstance(payload, bytes):
                charset = msg.get_content_charset() or "utf-8"
                try:
                    return payload.decode(charset)
                except (UnicodeDecodeError, LookupError):
                    try:
                        return payload.decode("utf-8")
                    except UnicodeDecodeError:
                        return payload.decode("latin-1")
            return str(payload or "")

    def _clean_subject_for_thread(self, subject: str) -> str:
        """Strip 'Re:', '[Python-Dev]', etc. to find canonical thread subject."""
        cleaned = subject
        prev = ""
        while prev != cleaned:
            prev = cleaned
            cleaned = re.sub(r"\[[^\]]+\]", "", cleaned).strip()
            cleaned = re.sub(r"^(?:re|fwd?):\s*", "", cleaned, flags=re.IGNORECASE).strip()
        return cleaned or "Mailing List Discussion"

    @staticmethod
    def _normalize_message_ids(value: str | None) -> list[str]:
        """Extract all angle-bracket Message-IDs from a (possibly folded) header value."""
        if not value:
            return []
        # Unfold RFC-822 continuation lines (leading whitespace) before matching.
        unfolded = re.sub(r"\r?\n[ \t]+", " ", value)
        return re.findall(r"<[^<>]+>", unfolded)

    @classmethod
    def _resolve_parent_id(cls, msg: email.message.Message) -> str | None:
        """Resolve the immediate parent Message-ID for a mailing list message.

        RFC-style precedence: a (possibly multi-ID) In-Reply-To names the direct
        parent(s); when absent, the References header is ordered root → … →
        immediate ancestor, so the *last* ID is the parent. This keeps deep
        reply chains pointing at their direct parent instead of the thread root.
        """
        in_reply_to = cls._normalize_message_ids(msg.get("In-Reply-To"))
        if in_reply_to:
            return in_reply_to[-1].strip("<>")
        references = cls._normalize_message_ids(msg.get("References"))
        if references:
            return references[-1].strip("<>")
        return None

    def parse_payload(
        self,
        data: str | bytes | list[email.message.Message] | os.PathLike,
        project_context: str,
        archive_base_url: str = "https://mail.python.org/archives",
        **kwargs: Any,
    ) -> RawThreadPayload:
        """Parse a single-conversation mbox into a RawThreadPayload.

        The input must belong to one conversation; multi-conversation archives
        should be partitioned first via :meth:`parse_archive`, which groups
        messages into threads by Message-ID / In-Reply-To / References chains.
        """
        messages_list = self._load_messages(data)
        if not messages_list:
            raise ValueError("No valid RFC-822 email messages found in payload")

        payload = self._build_thread_payload(
            messages_list,
            project_context=project_context,
            archive_base_url=archive_base_url,
            source_revision=kwargs.get("source_revision", "v1"),
        )
        return payload

    def parse_archive(
        self,
        data: str | bytes | list[email.message.Message] | os.PathLike,
        project_context: str,
        archive_base_url: str = "https://mail.python.org/archives",
        **kwargs: Any,
    ) -> list[RawThreadPayload]:
        """Partition a multi-conversation mbox archive into separate thread payloads.

        Messages are grouped by conversation: each root message (no resolvable
        parent within the archive) starts a thread, and every message with a
        parent ID joins that parent's thread. This prevents unrelated
        conversations from cross-contaminating discussion and decision evidence.
        """
        messages_list = self._load_messages(data)
        if not messages_list:
            raise ValueError("No valid RFC-822 email messages found in payload")

        message_ids: dict[str, email.message.Message] = {}
        for msg in messages_list:
            msg_id = self._normalize_message_ids(msg.get("Message-ID"))
            if msg_id:
                message_ids[msg_id[-1].strip("<>")] = msg

        # Union-find style thread assignment via parent chains.
        thread_of: dict[str, str] = {}  # message-id -> root message-id
        roots: list[str] = []

        def resolve_root(msg_key: str) -> str:
            """Walk parent links until an unresolvable root is reached."""
            current = msg_key
            visited = set()
            while True:
                if current in visited:
                    break  # cycle guard
                visited.add(current)
                if current in thread_of:
                    return thread_of[current]
                msg = message_ids.get(current)
                if msg is None:
                    break
                parent = self._resolve_parent_id(msg)
                if parent is None or parent not in message_ids:
                    break
                current = parent
            return current

        ordered_keys: list[str] = []
        for msg in messages_list:
            ids = self._normalize_message_ids(msg.get("Message-ID"))
            key = ids[-1].strip("<>") if ids else f"_anon_{len(ordered_keys)}"
            if key not in message_ids:
                # Anonymous or duplicated-ID message: keep it addressable.
                message_ids[key] = msg
            ordered_keys.append(key)

        for key in ordered_keys:
            root = resolve_root(key)
            thread_of[key] = root
            if root not in roots:
                roots.append(root)

        payloads: list[RawThreadPayload] = []
        for root in roots:
            thread_messages = [
                message_ids[key]
                for key in ordered_keys
                if thread_of.get(key) == root
            ]
            payloads.append(
                self._build_thread_payload(
                    thread_messages,
                    project_context=project_context,
                    archive_base_url=archive_base_url,
                    source_revision=kwargs.get("source_revision", "v1"),
                )
            )
        return payloads

    def _load_messages(
        self, data: str | bytes | list[email.message.Message] | os.PathLike
    ) -> list[email.message.Message]:
        """Load email messages from an mbox path, raw bytes, string, or list."""
        messages_list: list[email.message.Message] = []

        if isinstance(data, list):
            messages_list = data
        elif isinstance(data, (str, bytes, os.PathLike)):
            if isinstance(data, (str, os.PathLike)) and os.path.exists(str(data)):
                mbox = mailbox.mbox(str(data))
                for key in mbox:
                    messages_list.append(mbox.get_message(key))
            else:
                raw_bytes = data if isinstance(data, bytes) else data.encode("utf-8")
                # Split at byte level without destructive decoding
                chunks = re.split(rb"(?:\r?\n)(?=From [^\r\n]+(?:\r?\n))", raw_bytes)
                for chunk_bytes in chunks:
                    if not chunk_bytes.strip():
                        continue
                    if chunk_bytes.startswith(b"From "):
                        first_newline = chunk_bytes.find(b"\n")
                        if first_newline != -1:
                            chunk_bytes = chunk_bytes[first_newline + 1 :]
                    msg = email.message_from_bytes(chunk_bytes, policy=email.policy.compat32)
                    if msg.get("Message-ID") or msg.get("Subject"):
                        messages_list.append(msg)
        return messages_list

    def _build_thread_payload(
        self,
        messages_list: list[email.message.Message],
        project_context: str,
        archive_base_url: str,
        source_revision: str,
    ) -> RawThreadPayload:
        """Build a single RawThreadPayload from one conversation's messages."""
        first_msg = messages_list[0]
        raw_subject = first_msg.get("Subject", "Untitled Thread")
        thread_title = self._clean_subject_for_thread(raw_subject)

        thread_msg_id = first_msg.get("Message-ID", "")
        clean_thread_id = re.sub(r"[<>\s]", "", thread_msg_id) or "thread_0"
        canonical_url = f"{archive_base_url.rstrip('/')}/thread/{clean_thread_id}"

        parsed_messages: list[RawMessagePayload] = []

        for msg in messages_list:
            raw_id = msg.get("Message-ID", "")
            ext_msg_id = re.sub(r"[<>\s]", "", raw_id) or f"msg_{len(parsed_messages)}"

            parent_id = self._resolve_parent_id(msg)

            # Parse sender
            from_header = msg.get("From", "Anonymous <anonymous@example.org>")
            parsed_name, parsed_email = email.utils.parseaddr(from_header)

            email_prefix = parsed_email.split("@")[0] if "@" in parsed_email else parsed_email
            author_handle = email_prefix or "anonymous"
            author_name = parsed_name or author_handle

            # Parse date
            date_header = msg.get("Date")
            if date_header:
                try:
                    orig_dt = email.utils.parsedate_to_datetime(date_header)
                    if orig_dt.tzinfo is None:
                        orig_dt = orig_dt.replace(tzinfo=UTC)
                except (ValueError, TypeError):
                    orig_dt = datetime.now(UTC)
            else:
                orig_dt = datetime.now(UTC)

            body_content = self._extract_body(msg)
            permalink = f"{archive_base_url.rstrip('/')}/message/{ext_msg_id}"

            parsed_messages.append(
                RawMessagePayload(
                    external_message_id=ext_msg_id,
                    parent_message_id=parent_id,
                    author_platform_id=parsed_email or author_handle,
                    author_handle=author_handle,
                    author_name=author_name,
                    author_email_prefix=email_prefix,
                    original_timestamp=orig_dt,
                    canonical_permalink=permalink,
                    raw_content=body_content,
                    metadata={
                        "subject": msg.get("Subject", ""),
                        "in_reply_to": msg.get("In-Reply-To"),
                        "references": msg.get("References"),
                    },
                )
            )

        return RawThreadPayload(
            source_platform=self.platform,
            project_context=project_context,
            external_thread_id=clean_thread_id,
            title=thread_title,
            canonical_url=canonical_url,
            source_revision=source_revision,
            ingestion_metadata={
                "message_count": len(parsed_messages),
                "original_subject": raw_subject,
            },
            messages=parsed_messages,
        )
