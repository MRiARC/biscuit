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
                            return payload.decode(charset, errors="replace")
                        except (UnicodeDecodeError, LookupError, ValueError):
                            return payload.decode("utf-8", errors="replace")
            # If no text/plain found, fallback to first payload
            payload = msg.get_payload(decode=True)
            if isinstance(payload, bytes):
                return payload.decode("utf-8", errors="replace")
            return str(payload or "")
        else:
            payload = msg.get_payload(decode=True)
            if isinstance(payload, bytes):
                charset = msg.get_content_charset() or "utf-8"
                try:
                    return payload.decode(charset, errors="replace")
                except (UnicodeDecodeError, LookupError, ValueError):
                    return payload.decode("utf-8", errors="replace")
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

    def parse_payload(
        self,
        data: str | bytes | list[email.message.Message],
        project_context: str,
        archive_base_url: str = "https://mail.python.org/archives",
        **kwargs: Any,
    ) -> RawThreadPayload:
        """Parse mbox string, bytes, or email messages into a RawThreadPayload."""
        messages_list: list[email.message.Message] = []

        if isinstance(data, list):
            messages_list = data
        elif isinstance(data, (str, bytes, os.PathLike)):
            if isinstance(data, (str, os.PathLike)) and os.path.exists(str(data)):
                mbox = mailbox.mbox(str(data))
                for key in mbox:
                    messages_list.append(mbox.get_message(key))
            else:
                raw_bytes = data.encode("utf-8") if isinstance(data, str) else data
                # Check for multiple messages delimited by mbox From_ lines
                raw_str = raw_bytes.decode("utf-8", errors="replace")
                chunks = re.split(r"(?:\r?\n)(?=From [^\r\n]+(?:\r?\n))", raw_str)
                for chunk in chunks:
                    chunk_bytes = chunk.encode("utf-8")
                    if chunk.startswith("From "):
                        # Strip mbox From_ envelope line
                        first_newline = chunk_bytes.find(b"\n")
                        if first_newline != -1:
                            chunk_bytes = chunk_bytes[first_newline + 1 :]
                    msg = email.message_from_bytes(chunk_bytes, policy=email.policy.compat32)
                    if msg.get("Message-ID") or msg.get("Subject"):
                        messages_list.append(msg)

        if not messages_list:
            raise ValueError("No valid RFC-822 email messages found in payload")

        # Determine thread metadata from earliest message
        first_msg = messages_list[0]
        raw_subject = first_msg.get("Subject", "Untitled Thread")
        thread_title = self._clean_subject_for_thread(raw_subject)

        # Thread ID based on first message ID or subject slug
        thread_msg_id = first_msg.get("Message-ID", "")
        clean_thread_id = re.sub(r"[<>\s]", "", thread_msg_id) or "thread_0"
        canonical_url = f"{archive_base_url.rstrip('/')}/thread/{clean_thread_id}"

        parsed_messages: list[RawMessagePayload] = []

        for msg in messages_list:
            raw_id = msg.get("Message-ID", "")
            ext_msg_id = re.sub(r"[<>\s]", "", raw_id) or f"msg_{len(parsed_messages)}"

            parent_id_raw = msg.get("In-Reply-To") or msg.get("References")
            parent_id = re.sub(r"[<>\s]", "", parent_id_raw.split()[0]) if parent_id_raw else None

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
                    },
                )
            )

        return RawThreadPayload(
            source_platform=self.platform,
            project_context=project_context,
            external_thread_id=clean_thread_id,
            title=thread_title,
            canonical_url=canonical_url,
            source_revision=kwargs.get("source_revision", "v1"),
            ingestion_metadata={
                "message_count": len(parsed_messages),
                "original_subject": raw_subject,
            },
            messages=parsed_messages,
        )
