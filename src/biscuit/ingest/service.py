"""Universal thread ingestion and immutable message versioning service."""

from typing import Any

from sqlmodel import Session, select
from sqlmodel.ext.asyncio.session import AsyncSession

from biscuit.ingest.base import RawThreadPayload
from biscuit.ingest.cleaner import clean_derived_content, normalize_line_endings
from biscuit.ingest.hasher import compute_sha256
from biscuit.models.base import utcnow
from biscuit.models.enums import MessageEditState
from biscuit.models.provenance import (
    Contributor,
    SourceMessage,
    SourceMessageVersion,
    SourceThread,
)


def _get_platform_str(platform: Any) -> str:
    """Extract canonical string value from PlatformType enum or string."""
    if hasattr(platform, "value"):
        return str(platform.value)
    return str(platform)


class IngestResult:
    """Summary of thread ingestion operations."""

    def __init__(
        self,
        thread: SourceThread,
        messages_created: int = 0,
        versions_created: int = 0,
        versions_rotated: int = 0,
        unchanged_count: int = 0,
    ) -> None:
        self.thread = thread
        self.thread_id = thread.id
        self.messages_created = messages_created
        self.versions_created = versions_created
        self.versions_rotated = versions_rotated
        self.unchanged_count = unchanged_count

    def __repr__(self) -> str:
        return (
            f"<IngestResult thread_id={self.thread_id} "
            f"new_msgs={self.messages_created} "
            f"new_versions={self.versions_created} "
            f"rotated={self.versions_rotated} "
            f"unchanged={self.unchanged_count}>"
        )


class IngestService:
    """Orchestrates storing threads and managing append-only message versions."""

    @staticmethod
    def get_or_create_contributor(
        session: Session,
        source_platform: Any,
        platform_user_id: str,
        canonical_name: str,
        author_handle: str,
        email_prefix: str | None = None,
    ) -> Contributor:
        """Find or create a Contributor record in synchronous session."""
        plat_str = _get_platform_str(source_platform)
        statement = select(Contributor).where(
            Contributor.source_platform == plat_str,
            Contributor.platform_user_id == platform_user_id,
        )
        contributor = session.exec(statement).first()
        if not contributor:
            contributor = Contributor(
                source_platform=plat_str,
                platform_user_id=platform_user_id,
                canonical_name=canonical_name,
                author_handle=author_handle,
                email_prefix=email_prefix,
            )
            session.add(contributor)
            session.flush()
        return contributor

    @staticmethod
    def get_or_create_thread(
        session: Session, payload: RawThreadPayload
    ) -> tuple[SourceThread, bool]:
        """Find or create a SourceThread container."""
        plat_str = _get_platform_str(payload.source_platform)
        statement = select(SourceThread).where(
            SourceThread.source_platform == plat_str,
            SourceThread.project_context == payload.project_context,
            SourceThread.external_thread_id == payload.external_thread_id,
        )
        thread = session.exec(statement).first()
        if thread:
            return thread, False

        thread = SourceThread(
            source_platform=plat_str,
            project_context=payload.project_context,
            external_thread_id=payload.external_thread_id,
            title=payload.title,
            canonical_url=payload.canonical_url,
            source_revision=payload.source_revision,
            ingestion_metadata=payload.ingestion_metadata,
        )
        session.add(thread)
        session.flush()
        return thread, True

    @classmethod
    def ingest_thread(
        cls, session: Session, payload: RawThreadPayload
    ) -> IngestResult:
        """Synchronously ingest a thread with immutable raw message versioning."""
        thread, _ = cls.get_or_create_thread(session, payload)
        plat_str = _get_platform_str(payload.source_platform)

        messages_created = 0
        versions_created = 0
        versions_rotated = 0
        unchanged_count = 0

        for msg_payload in payload.messages:
            # 1. Resolve author
            author = cls.get_or_create_contributor(
                session,
                source_platform=plat_str,
                platform_user_id=msg_payload.author_platform_id,
                canonical_name=msg_payload.author_name,
                author_handle=msg_payload.author_handle,
                email_prefix=msg_payload.author_email_prefix,
            )

            # 2. Compute exact SHA-256 over raw UTF-8 bytes
            content_hash = compute_sha256(msg_payload.raw_content)

            # 3. Lookup existing message
            msg_statement = select(SourceMessage).where(
                SourceMessage.thread_id == thread.id,
                SourceMessage.external_message_id == msg_payload.external_message_id,
            )
            existing_message = session.exec(msg_statement).first()

            # Handle remote deletion tombstone
            if msg_payload.is_deleted:
                if not existing_message:
                    new_msg = SourceMessage(
                        thread_id=thread.id,
                        external_message_id=msg_payload.external_message_id,
                        parent_message_id=msg_payload.parent_message_id,
                        author_id=author.id,
                        original_timestamp=msg_payload.original_timestamp,
                        canonical_permalink=msg_payload.canonical_permalink,
                    )
                    session.add(new_msg)
                    session.flush()
                    messages_created += 1

                    version = SourceMessageVersion(
                        message_id=new_msg.id,
                        version_number=1,
                        raw_content=msg_payload.raw_content,
                        content_hash=content_hash,
                        is_current=True,
                        edit_state=MessageEditState.DELETED,
                        edit_reason=msg_payload.delete_reason or "Remote platform deletion tombstone",
                        deleted_at=utcnow(),
                        derived_normalized_content=normalize_line_endings(
                            msg_payload.raw_content
                        ),
                        derived_cleaned_content=clean_derived_content(
                            msg_payload.raw_content
                        ),
                    )
                    session.add(version)
                    session.flush()
                    versions_created += 1
                else:
                    v_statement = select(SourceMessageVersion).where(
                        SourceMessageVersion.message_id == existing_message.id,
                        SourceMessageVersion.is_current == True,
                    )
                    current_version = session.exec(v_statement).first()
                    if current_version and current_version.edit_state == MessageEditState.DELETED:
                        unchanged_count += 1
                    else:
                        next_version_num = (
                            current_version.version_number + 1 if current_version else 1
                        )
                        if current_version:
                            current_version.is_current = False
                            session.add(current_version)
                            versions_rotated += 1

                        prior_content = (
                            current_version.raw_content if current_version else msg_payload.raw_content
                        )
                        prior_hash = (
                            current_version.content_hash if current_version else content_hash
                        )

                        tombstone_version = SourceMessageVersion(
                            message_id=existing_message.id,
                            version_number=next_version_num,
                            raw_content=prior_content,  # Retain prior captured content
                            content_hash=prior_hash,
                            is_current=True,
                            edit_state=MessageEditState.DELETED,
                            edit_reason=msg_payload.delete_reason or "Remote platform deletion tombstone",
                            deleted_at=utcnow(),
                            derived_normalized_content=normalize_line_endings(
                                prior_content
                            ),
                            derived_cleaned_content=clean_derived_content(
                                prior_content
                            ),
                        )
                        session.add(tombstone_version)
                        session.flush()
                        versions_created += 1
                continue

            if not existing_message:
                # Case A: Brand new active message
                new_msg = SourceMessage(
                    thread_id=thread.id,
                    external_message_id=msg_payload.external_message_id,
                    parent_message_id=msg_payload.parent_message_id,
                    author_id=author.id,
                    original_timestamp=msg_payload.original_timestamp,
                    canonical_permalink=msg_payload.canonical_permalink,
                )
                session.add(new_msg)
                session.flush()
                messages_created += 1

                version = SourceMessageVersion(
                    message_id=new_msg.id,
                    version_number=1,
                    raw_content=msg_payload.raw_content,
                    content_hash=content_hash,
                    is_current=True,
                    edit_state=MessageEditState.ORIGINAL,
                    derived_normalized_content=normalize_line_endings(
                        msg_payload.raw_content
                    ),
                    derived_cleaned_content=clean_derived_content(
                        msg_payload.raw_content
                    ),
                )
                session.add(version)
                session.flush()
                versions_created += 1
            else:
                # Case B: Existing message - check active version
                v_statement = select(SourceMessageVersion).where(
                    SourceMessageVersion.message_id == existing_message.id,
                    SourceMessageVersion.is_current == True,
                )
                current_version = session.exec(v_statement).first()

                is_restoration = (
                    current_version is not None
                    and current_version.edit_state == MessageEditState.DELETED
                )

                if (
                    current_version
                    and not is_restoration
                    and current_version.content_hash == content_hash
                ):
                    unchanged_count += 1
                else:
                    # Message content changed remotely, or post was restored from deletion
                    next_version_num = (
                        current_version.version_number + 1 if current_version else 1
                    )
                    if current_version:
                        current_version.is_current = False
                        session.add(current_version)
                        versions_rotated += 1

                    edit_reason = (
                        "Remote post restored from deletion"
                        if is_restoration
                        else "Remote edit detected during ingestion"
                    )
                    new_version = SourceMessageVersion(
                        message_id=existing_message.id,
                        version_number=next_version_num,
                        raw_content=msg_payload.raw_content,
                        content_hash=content_hash,
                        is_current=True,
                        edit_state=MessageEditState.EDITED,
                        edit_reason=edit_reason,
                        deleted_at=None,
                        derived_normalized_content=normalize_line_endings(
                            msg_payload.raw_content
                        ),
                        derived_cleaned_content=clean_derived_content(
                            msg_payload.raw_content
                        ),
                    )
                    session.add(new_version)
                    session.flush()
                    versions_created += 1

        return IngestResult(
            thread=thread,
            messages_created=messages_created,
            versions_created=versions_created,
            versions_rotated=versions_rotated,
            unchanged_count=unchanged_count,
        )

    @classmethod
    async def async_get_or_create_contributor(
        cls,
        session: AsyncSession,
        source_platform: Any,
        platform_user_id: str,
        canonical_name: str,
        author_handle: str,
        email_prefix: str | None = None,
    ) -> Contributor:
        """Find or create a Contributor record in asynchronous session."""
        plat_str = _get_platform_str(source_platform)
        statement = select(Contributor).where(
            Contributor.source_platform == plat_str,
            Contributor.platform_user_id == platform_user_id,
        )
        result = await session.exec(statement)
        contributor = result.first()
        if not contributor:
            contributor = Contributor(
                source_platform=plat_str,
                platform_user_id=platform_user_id,
                canonical_name=canonical_name,
                author_handle=author_handle,
                email_prefix=email_prefix,
            )
            session.add(contributor)
            await session.flush()
        return contributor

    @classmethod
    async def async_get_or_create_thread(
        cls, session: AsyncSession, payload: RawThreadPayload
    ) -> tuple[SourceThread, bool]:
        """Find or create a SourceThread container asynchronously."""
        plat_str = _get_platform_str(payload.source_platform)
        statement = select(SourceThread).where(
            SourceThread.source_platform == plat_str,
            SourceThread.project_context == payload.project_context,
            SourceThread.external_thread_id == payload.external_thread_id,
        )
        result = await session.exec(statement)
        thread = result.first()
        if thread:
            return thread, False

        thread = SourceThread(
            source_platform=plat_str,
            project_context=payload.project_context,
            external_thread_id=payload.external_thread_id,
            title=payload.title,
            canonical_url=payload.canonical_url,
            source_revision=payload.source_revision,
            ingestion_metadata=payload.ingestion_metadata,
        )
        session.add(thread)
        await session.flush()
        return thread, True

    @classmethod
    async def async_ingest_thread(
        cls, session: AsyncSession, payload: RawThreadPayload
    ) -> IngestResult:
        """Asynchronously ingest a thread with immutable raw message versioning."""
        thread, _ = await cls.async_get_or_create_thread(session, payload)
        plat_str = _get_platform_str(payload.source_platform)

        messages_created = 0
        versions_created = 0
        versions_rotated = 0
        unchanged_count = 0

        for msg_payload in payload.messages:
            author = await cls.async_get_or_create_contributor(
                session,
                source_platform=plat_str,
                platform_user_id=msg_payload.author_platform_id,
                canonical_name=msg_payload.author_name,
                author_handle=msg_payload.author_handle,
                email_prefix=msg_payload.author_email_prefix,
            )

            content_hash = compute_sha256(msg_payload.raw_content)

            msg_statement = select(SourceMessage).where(
                SourceMessage.thread_id == thread.id,
                SourceMessage.external_message_id == msg_payload.external_message_id,
            )
            msg_result = await session.exec(msg_statement)
            existing_message = msg_result.first()

            if msg_payload.is_deleted:
                if not existing_message:
                    new_msg = SourceMessage(
                        thread_id=thread.id,
                        external_message_id=msg_payload.external_message_id,
                        parent_message_id=msg_payload.parent_message_id,
                        author_id=author.id,
                        original_timestamp=msg_payload.original_timestamp,
                        canonical_permalink=msg_payload.canonical_permalink,
                    )
                    session.add(new_msg)
                    await session.flush()
                    messages_created += 1

                    version = SourceMessageVersion(
                        message_id=new_msg.id,
                        version_number=1,
                        raw_content=msg_payload.raw_content,
                        content_hash=content_hash,
                        is_current=True,
                        edit_state=MessageEditState.DELETED,
                        edit_reason=msg_payload.delete_reason or "Remote platform deletion tombstone",
                        deleted_at=utcnow(),
                        derived_normalized_content=normalize_line_endings(
                            msg_payload.raw_content
                        ),
                        derived_cleaned_content=clean_derived_content(
                            msg_payload.raw_content
                        ),
                    )
                    session.add(version)
                    await session.flush()
                    versions_created += 1
                else:
                    v_statement = select(SourceMessageVersion).where(
                        SourceMessageVersion.message_id == existing_message.id,
                        SourceMessageVersion.is_current == True,
                    )
                    v_result = await session.exec(v_statement)
                    current_version = v_result.first()

                    if current_version and current_version.edit_state == MessageEditState.DELETED:
                        unchanged_count += 1
                    else:
                        next_version_num = (
                            current_version.version_number + 1 if current_version else 1
                        )
                        if current_version:
                            current_version.is_current = False
                            session.add(current_version)
                            versions_rotated += 1

                        prior_content = (
                            current_version.raw_content if current_version else msg_payload.raw_content
                        )
                        prior_hash = (
                            current_version.content_hash if current_version else content_hash
                        )

                        tombstone_version = SourceMessageVersion(
                            message_id=existing_message.id,
                            version_number=next_version_num,
                            raw_content=prior_content,
                            content_hash=prior_hash,
                            is_current=True,
                            edit_state=MessageEditState.DELETED,
                            edit_reason=msg_payload.delete_reason or "Remote platform deletion tombstone",
                            deleted_at=utcnow(),
                            derived_normalized_content=normalize_line_endings(
                                prior_content
                            ),
                            derived_cleaned_content=clean_derived_content(
                                prior_content
                            ),
                        )
                        session.add(tombstone_version)
                        await session.flush()
                        versions_created += 1
                continue

            if not existing_message:
                new_msg = SourceMessage(
                    thread_id=thread.id,
                    external_message_id=msg_payload.external_message_id,
                    parent_message_id=msg_payload.parent_message_id,
                    author_id=author.id,
                    original_timestamp=msg_payload.original_timestamp,
                    canonical_permalink=msg_payload.canonical_permalink,
                )
                session.add(new_msg)
                await session.flush()
                messages_created += 1

                version = SourceMessageVersion(
                    message_id=new_msg.id,
                    version_number=1,
                    raw_content=msg_payload.raw_content,
                    content_hash=content_hash,
                    is_current=True,
                    edit_state=MessageEditState.ORIGINAL,
                    derived_normalized_content=normalize_line_endings(
                        msg_payload.raw_content
                    ),
                    derived_cleaned_content=clean_derived_content(
                        msg_payload.raw_content
                    ),
                )
                session.add(version)
                await session.flush()
                versions_created += 1
            else:
                v_statement = select(SourceMessageVersion).where(
                    SourceMessageVersion.message_id == existing_message.id,
                    SourceMessageVersion.is_current == True,
                )
                v_result = await session.exec(v_statement)
                current_version = v_result.first()

                is_restoration = (
                    current_version is not None
                    and current_version.edit_state == MessageEditState.DELETED
                )

                if (
                    current_version
                    and not is_restoration
                    and current_version.content_hash == content_hash
                ):
                    unchanged_count += 1
                else:
                    next_version_num = (
                        current_version.version_number + 1 if current_version else 1
                    )
                    if current_version:
                        current_version.is_current = False
                        session.add(current_version)
                        versions_rotated += 1

                    edit_reason = (
                        "Remote post restored from deletion"
                        if is_restoration
                        else "Remote edit detected during ingestion"
                    )
                    new_version = SourceMessageVersion(
                        message_id=existing_message.id,
                        version_number=next_version_num,
                        raw_content=msg_payload.raw_content,
                        content_hash=content_hash,
                        is_current=True,
                        edit_state=MessageEditState.EDITED,
                        edit_reason=edit_reason,
                        deleted_at=None,
                        derived_normalized_content=normalize_line_endings(
                            msg_payload.raw_content
                        ),
                        derived_cleaned_content=clean_derived_content(
                            msg_payload.raw_content
                        ),
                    )
                    session.add(new_version)
                    await session.flush()
                    versions_created += 1

        return IngestResult(
            thread=thread,
            messages_created=messages_created,
            versions_created=versions_created,
            versions_rotated=versions_rotated,
            unchanged_count=unchanged_count,
        )
