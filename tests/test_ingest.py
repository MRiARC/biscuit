"""Unit and integration tests for universal ingestion adapters and versioning."""

from datetime import UTC, datetime
from unittest.mock import MagicMock

import httpx
import pytest
from sqlmodel import Session, create_engine, select

from biscuit.db.session import (
    get_async_engine,
    get_async_session,
    init_async_db,
    init_db,
)
from biscuit.ingest import (
    DiscourseAdapter,
    GitHubAdapter,
    IngestService,
    MboxAdapter,
    clean_derived_content,
    compute_sha256,
    is_bot_message,
    normalize_line_endings,
    verify_sha256,
)
from biscuit.ingest.base import RawMessagePayload, RawThreadPayload
from biscuit.models.enums import MessageEditState, PlatformType
from biscuit.models.provenance import (
    Contributor,
    SourceMessage,
    SourceMessageVersion,
    SourceThread,
)


def test_hasher_wire_byte_preservation():
    """Test SHA-256 hash sensitivity to exact line endings and Unicode."""
    text_crlf = "Hello\r\nWorld"
    text_lf = "Hello\nWorld"

    hash_crlf = compute_sha256(text_crlf)
    hash_lf = compute_sha256(text_lf)

    assert hash_crlf != hash_lf
    assert verify_sha256(text_crlf, hash_crlf) is True
    assert verify_sha256(text_lf, hash_lf) is True
    assert verify_sha256(text_crlf, hash_lf) is False

    # Edge cases
    with pytest.raises(ValueError):
        compute_sha256(None)
    assert verify_sha256(None, "abc") is False
    assert verify_sha256("abc", None) is False


def test_cleaner_transformations():
    """Test derived view cleaners: CRLF normalization, blockquote pointer substitution, and bot pruning."""
    assert clean_derived_content("") == ""
    assert normalize_line_endings("") == ""
    assert is_bot_message("alice", "") is False
    # 1. CRLF normalization
    raw_crlf = "Line 1\r\nLine 2\rLine 3"
    assert normalize_line_endings(raw_crlf) == "Line 1\nLine 2\nLine 3"

    # 2. Discourse quote replacement
    discourse_raw = (
        '[quote="gvanrossum, post:3, topic:1234"]\n'
        "I like the concept.\n"
        "[/quote]\n"
        "Here is my reply."
    )
    cleaned_discourse = clean_derived_content(discourse_raw)
    assert "[Quoting post-3 by @gvanrossum]" in cleaned_discourse
    assert "Here is my reply." in cleaned_discourse

    # 3. Markdown blockquote replacement
    md_raw = "> This is an existing objection\n> over backward compatibility.\n\nI agree."
    cleaned_md = clean_derived_content(md_raw)
    assert '[Quoting: "This is an existing objection"]' in cleaned_md
    assert "I agree." in cleaned_md

    # 4. Bot pruning
    assert is_bot_message("dependabot[bot]", "Updates package foo") is True
    assert is_bot_message("github-actions[bot]", "CI failed") is True
    assert is_bot_message("alice", "This is an automated notification from our system.") is True
    assert is_bot_message("gvanrossum", "I propose we reject this syntax.") is False


def test_discourse_adapter():
    """Test Discourse JSON payload parsing into RawThreadPayload."""
    adapter = DiscourseAdapter()
    payload = {
        "id": 5050,
        "title": "Discussion on None-aware operators",
        "slug": "discussion-on-none-aware-operators",
        "post_stream": {
            "posts": [
                {
                    "id": 101,
                    "post_number": 1,
                    "user_id": 42,
                    "username": "gvanrossum",
                    "name": "Guido van Rossum",
                    "created_at": "2023-01-10T14:30:00.000Z",
                    "raw": "I propose we reconsider PEP 505.\r\nIt would be very helpful.",
                },
                {
                    "id": 102,
                    "post_number": 2,
                    "reply_to_post_number": 1,
                    "user_id": 99,
                    "username": "markshannon",
                    "name": "Mark Shannon",
                    "created_at": "2023-01-10T15:00:00.000Z",
                    "raw": "The syntax creates ambiguities in the parser grammar.",
                },
            ]
        },
    }

    thread_payload = adapter.parse_payload(
        data=payload,
        project_context="python/cpython",
        base_url="https://discuss.python.org",
    )

    assert thread_payload.source_platform == PlatformType.DISCOURSE
    assert thread_payload.project_context == "python/cpython"
    assert thread_payload.external_thread_id == "topic_5050"
    assert len(thread_payload.messages) == 2

    # Post 1
    p1 = thread_payload.messages[0]
    assert p1.external_message_id == "post_101"
    assert p1.author_handle == "gvanrossum"
    assert p1.author_name == "Guido van Rossum"
    assert p1.parent_message_id is None
    assert "\r\n" in p1.raw_content  # Wire bytes untouched

    # Post 2
    p2 = thread_payload.messages[1]
    assert p2.external_message_id == "post_102"
    assert p2.author_handle == "markshannon"
    assert p2.parent_message_id == "post_101"


def test_mbox_adapter():
    """Test RFC-822 email payload parsing."""
    adapter = MboxAdapter()
    email_text = (
        "From: Guido van Rossum <guido@python.org>\n"
        "To: python-dev@python.org\n"
        "Subject: [Python-Dev] Re: PEP 505: None-aware operators\n"
        "Date: Mon, 15 Jan 2024 10:00:00 +0000\n"
        "Message-ID: <msg_001@mail.python.org>\n"
        "\n"
        "I am rejecting this proposal due to syntactic ambiguity.\n"
    )

    thread_payload = adapter.parse_payload(
        data=email_text,
        project_context="python/cpython",
    )

    assert thread_payload.source_platform == PlatformType.MBOX
    assert thread_payload.title == "PEP 505: None-aware operators"
    assert len(thread_payload.messages) == 1

    msg = thread_payload.messages[0]
    assert msg.external_message_id == "msg_001@mail.python.org"
    assert msg.author_handle == "guido"
    assert msg.author_name == "Guido van Rossum"
    assert "rejecting this proposal" in msg.raw_content

    # Test multipart and mbox From envelope
    multipart_mbox = (
        "From guido@python.org Mon Jan 15 10:00:00 2024\n"
        "From: Guido van Rossum <guido@python.org>\n"
        "Subject: Multipart Test\n"
        "MIME-Version: 1.0\n"
        "Content-Type: multipart/mixed; boundary=\"BOUNDARY\"\n"
        "\n"
        "--BOUNDARY\n"
        "Content-Type: text/plain; charset=utf-8\n"
        "\n"
        "Plain text body part.\n"
        "--BOUNDARY--\n"
    )
    mp_payload = adapter.parse_payload(multipart_mbox, project_context="python/cpython")
    assert len(mp_payload.messages) == 1
    assert "Plain text body part." in mp_payload.messages[0].raw_content


def test_github_adapter():
    """Test GitHub Pull Request thread parsing."""
    adapter = GitHubAdapter()
    pr_data = {
        "number": 505,
        "title": "Add None-aware operators",
        "html_url": "https://github.com/python/cpython/pull/505",
        "body": "This PR adds the ?. operator.",
        "user": {"id": 1, "login": "alice", "name": "Alice Contributor"},
        "created_at": "2024-02-01T12:00:00Z",
        "comments": [
            {
                "id": 9001,
                "user": {"id": 2, "login": "reviewer", "name": "Bob Reviewer"},
                "body": "Consider the impact on tokenization.",
                "created_at": "2024-02-01T13:00:00Z",
                "diff_hunk": "@@ -10,3 +10,4 @@",
            }
        ],
    }

    thread_payload = adapter.parse_payload(pr_data, project_context="python/cpython")
    assert thread_payload.source_platform == PlatformType.GITHUB_PR
    assert thread_payload.external_thread_id == "pull_505"
    assert len(thread_payload.messages) == 2

    pr_desc = thread_payload.messages[0]
    assert pr_desc.external_message_id == "pr_desc_505"
    assert pr_desc.author_handle == "alice"

    comment = thread_payload.messages[1]
    assert comment.external_message_id == "comment_9001"
    assert comment.author_handle == "reviewer"
    assert comment.parent_message_id == "pr_desc_505"
    assert comment.metadata.get("diff_hunk") == "@@ -10,3 +10,4 @@"


def test_ingest_service_sync_and_versioning():
    """Test IngestService sync ingestion, deduplication, and version rotation on remote edits."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    init_db(engine)

    msg1 = RawMessagePayload(
        external_message_id="post_1",
        author_platform_id="u_1",
        author_handle="gvanrossum",
        author_name="Guido",
        original_timestamp=datetime.now(UTC),
        canonical_permalink="https://discuss.python.org/t/10/1",
        raw_content="Original text.\r\nLine 2",
    )

    thread_payload = RawThreadPayload(
        source_platform=PlatformType.DISCOURSE,
        project_context="python/cpython",
        external_thread_id="topic_10",
        title="Test Topic",
        canonical_url="https://discuss.python.org/t/10",
        messages=[msg1],
    )

    # 1. Initial Ingestion
    with Session(engine) as session:
        res1 = IngestService.ingest_thread(session, thread_payload)
        session.commit()

    assert res1.messages_created == 1
    assert res1.versions_created == 1
    assert "new_msgs=1" in repr(res1)

    # Verify DB records
    with Session(engine) as session:
        v1 = session.exec(select(SourceMessageVersion)).first()
        assert v1 is not None
        assert v1.version_number == 1
        assert v1.is_current is True
        assert v1.edit_state == MessageEditState.ORIGINAL
        assert v1.raw_content == "Original text.\r\nLine 2"
        assert v1.content_hash == compute_sha256("Original text.\r\nLine 2")
        assert v1.derived_normalized_content == "Original text.\nLine 2"

    # 2. Re-ingest identical thread -> No changes
    with Session(engine) as session:
        res2 = IngestService.ingest_thread(session, thread_payload)
        session.commit()

    assert res2.messages_created == 0
    assert res2.versions_created == 0
    assert res2.unchanged_count == 1

    # 3. Re-ingest with remote edit
    edited_msg1 = msg1.model_copy(update={"raw_content": "Edited text.\r\nLine 2 modified"})
    thread_payload_edited = thread_payload.model_copy(update={"messages": [edited_msg1]})

    with Session(engine) as session:
        res3 = IngestService.ingest_thread(session, thread_payload_edited)
        session.commit()

    assert res3.versions_rotated == 1
    assert res3.versions_created == 1

    # Verify both versions exist and is_current rotated
    with Session(engine) as session:
        versions = session.exec(
            select(SourceMessageVersion).order_by(SourceMessageVersion.version_number)
        ).all()
        assert len(versions) == 2

        old_v, new_v = versions[0], versions[1]
        assert old_v.version_number == 1
        assert old_v.is_current is False

        assert new_v.version_number == 2
        assert new_v.is_current is True
        assert new_v.edit_state == MessageEditState.EDITED
        assert new_v.raw_content == "Edited text.\r\nLine 2 modified"
        assert new_v.content_hash == compute_sha256("Edited text.\r\nLine 2 modified")


@pytest.mark.anyio
async def test_async_ingest_service():
    """Test asynchronous IngestService workflow."""
    engine = get_async_engine("sqlite+aiosqlite:///:memory:")
    await init_async_db(engine)

    msg = RawMessagePayload(
        external_message_id="async_post_1",
        author_platform_id="u_async",
        author_handle="async_author",
        author_name="Async Author",
        original_timestamp=datetime.now(UTC),
        canonical_permalink="https://example.com/msg1",
        raw_content="Async wire content",
    )

    thread_payload = RawThreadPayload(
        source_platform=PlatformType.GITHUB_PR,
        project_context="python/cpython",
        external_thread_id="pull_999",
        title="Async PR",
        canonical_url="https://github.com/python/cpython/pull/999",
        messages=[msg],
    )

    async with get_async_session(engine) as session:
        res = await IngestService.async_ingest_thread(session, thread_payload)
        await session.commit()

    assert res.messages_created == 1
    assert res.versions_created == 1

    async with get_async_session(engine) as session:
        statement = select(SourceThread).where(SourceThread.external_thread_id == "pull_999")
        result = await session.exec(statement)
        thread = result.first()
        assert thread is not None
        assert thread.title == "Async PR"

    # Async unchanged test
    async with get_async_session(engine) as session:
        res2 = await IngestService.async_ingest_thread(session, thread_payload)
        await session.commit()
    assert res2.unchanged_count == 1

    # Async edit test
    edited_msg = msg.model_copy(update={"raw_content": "Async wire content edited"})
    edited_payload = thread_payload.model_copy(update={"messages": [edited_msg]})
    async with get_async_session(engine) as session:
        res3 = await IngestService.async_ingest_thread(session, edited_payload)
        await session.commit()
    assert res3.versions_rotated == 1
    assert res3.versions_created == 1


def test_enum_value_string_persistence():
    """Verify that PlatformType is stored as its canonical string value ('DISCOURSE'), not repr."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    init_db(engine)

    msg = RawMessagePayload(
        external_message_id="post_plat_1",
        author_platform_id="u_plat",
        author_handle="alice",
        author_name="Alice",
        original_timestamp=datetime.now(UTC),
        canonical_permalink="https://example.com/plat/1",
        raw_content="Content",
    )
    thread = RawThreadPayload(
        source_platform=PlatformType.DISCOURSE,
        project_context="python/cpython",
        external_thread_id="topic_plat",
        title="Plat Test",
        canonical_url="https://example.com/plat",
        messages=[msg],
    )

    with Session(engine) as session:
        IngestService.ingest_thread(session, thread)
        session.commit()

    with Session(engine) as session:
        db_thread = session.exec(select(SourceThread)).first()
        assert db_thread is not None
        assert db_thread.source_platform == "DISCOURSE"
        assert db_thread.source_platform != "PlatformType.DISCOURSE"

        db_author = session.exec(select(Contributor)).first()
        assert db_author is not None
        assert db_author.source_platform == "DISCOURSE"
        assert db_author.source_platform != "PlatformType.DISCOURSE"


def test_remote_deletion_tombstone():
    """Verify that remote deletions create an immutable tombstone version and retain prior content."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    init_db(engine)

    msg = RawMessagePayload(
        external_message_id="post_del_1",
        author_platform_id="u_del",
        author_handle="bob",
        author_name="Bob",
        original_timestamp=datetime.now(UTC),
        canonical_permalink="https://example.com/del/1",
        raw_content="Prior valuable content",
    )
    thread = RawThreadPayload(
        source_platform=PlatformType.GITHUB_PR,
        project_context="python/cpython",
        external_thread_id="pr_del",
        title="Del Test",
        canonical_url="https://example.com/del",
        messages=[msg],
    )

    # 1. Ingest initial active message
    with Session(engine) as session:
        IngestService.ingest_thread(session, thread)
        session.commit()

    # 2. Ingest deletion tombstone
    deleted_msg = msg.model_copy(update={"is_deleted": True, "delete_reason": "Post removed by author"})
    thread_del = thread.model_copy(update={"messages": [deleted_msg]})

    with Session(engine) as session:
        res = IngestService.ingest_thread(session, thread_del)
        session.commit()

    assert res.versions_rotated == 1
    assert res.versions_created == 1

    with Session(engine) as session:
        versions = session.exec(select(SourceMessageVersion).order_by(SourceMessageVersion.version_number)).all()
        assert len(versions) == 2

        v1, v2 = versions[0], versions[1]
        assert v1.version_number == 1
        assert v1.is_current is False

        assert v2.version_number == 2
        assert v2.is_current is True
        assert v2.edit_state == MessageEditState.DELETED
        assert v2.deleted_at is not None
        # Retained prior content for auditability
        assert v2.raw_content == "Prior valuable content"

    # 3. Subsequent ingestion of same deleted message is idempotent (unchanged)
    with Session(engine) as session:
        res2 = IngestService.ingest_thread(session, thread_del)
        session.commit()
    assert res2.unchanged_count == 1


def test_discourse_pagination_and_stream():
    """Verify DiscourseAdapter correctly detects un-fetched stream IDs and incorporates additional posts."""
    adapter = DiscourseAdapter()
    data = {
        "id": 9999,
        "title": "Long Paginated Topic",
        "slug": "long-topic",
        "post_stream": {
            "posts": [
                {"id": 1, "post_number": 1, "user_id": 1, "username": "alice", "raw": "Post 1"},
                {"id": 2, "post_number": 2, "user_id": 2, "username": "bob", "raw": "Post 2", "reply_to_post_number": 1},
            ],
            "stream": [1, 2, 3, 4],
        },
    }

    additional_posts = [
        {"id": 3, "post_number": 3, "user_id": 3, "username": "carol", "raw": "Post 3", "reply_to_post_number": 2},
        {"id": 4, "post_number": 4, "user_id": 4, "username": "dave", "raw": "Post 4", "deleted_at": "2023-01-01T00:00:00Z"},
    ]

    payload = adapter.parse_payload(
        data=data,
        project_context="python/cpython",
        additional_posts=additional_posts,
    )

    assert len(payload.messages) == 4
    assert payload.messages[0].external_message_id == "post_1"
    assert payload.messages[1].external_message_id == "post_2"
    assert payload.messages[1].parent_message_id == "post_1"
    assert payload.messages[2].external_message_id == "post_3"
    assert payload.messages[2].parent_message_id == "post_2"
    assert payload.messages[3].is_deleted is True


def test_mbox_byte_level_non_utf8():
    """Verify MboxAdapter parses non-UTF-8 bytes without destructive pre-decoding."""
    adapter = MboxAdapter()
    raw_email_bytes = (
        b"From: Author <author@example.com>\n"
        b"Subject: Non-UTF8 Message\n"
        b"Message-ID: <non_utf8_001@example.com>\n"
        b"Content-Type: text/plain; charset=latin-1\n"
        b"\n"
        b"Character in Latin-1: \xe9\n"
    )

    thread = adapter.parse_payload(raw_email_bytes, project_context="test/repo")
    assert len(thread.messages) == 1
    assert "\xe9" in thread.messages[0].raw_content


def test_discourse_hidden_post_not_marked_deleted():
    """Verify that a moderated/hidden Discourse post is not marked as deleted, but preserves hidden metadata."""
    adapter = DiscourseAdapter()
    data = {
        "id": 101,
        "title": "Moderated Topic",
        "slug": "moderated-topic",
        "post_stream": {
            "posts": [
                {
                    "id": 11,
                    "post_number": 1,
                    "user_id": 1,
                    "username": "mod_user",
                    "raw": "Flagged under review",
                    "hidden": True,
                },
                {
                    "id": 12,
                    "post_number": 2,
                    "user_id": 2,
                    "username": "del_user",
                    "raw": "Deleted post",
                    "deleted_at": "2024-01-01T00:00:00Z",
                    "hidden": True,
                },
            ],
            "stream": [11, 12],
        },
    }
    payload = adapter.parse_payload(data=data, project_context="python/cpython")
    assert len(payload.messages) == 2

    # Post 1: Hidden but not deleted
    msg1 = payload.messages[0]
    assert msg1.is_deleted is False
    assert msg1.metadata.get("hidden") is True

    # Post 2: Deleted (has deleted_at)
    msg2 = payload.messages[1]
    assert msg2.is_deleted is True
    assert msg2.metadata.get("hidden") is True


def test_discourse_internal_client_pagination_with_mock(monkeypatch):
    """Verify DiscourseAdapter uses managed internal HTTP client when client=None and base_url is supplied."""
    adapter = DiscourseAdapter()
    data = {
        "id": 500,
        "title": "Paginated Topic",
        "slug": "paginated-topic",
        "post_stream": {
            "posts": [
                {"id": 100, "post_number": 1, "user_id": 1, "username": "alice", "raw": "Initial post"},
            ],
            "stream": [100, 200, 300],
        },
    }

    def mock_get(url, *args, **kwargs):
        params = kwargs.get("params", [])
        mock_response = MagicMock()
        mock_response.status_code = 200
        pids = [val for key, val in params if key == "post_ids[]"]
        posts = [
            {
                "id": pid,
                "post_number": pid // 100,
                "user_id": pid // 100,
                "username": f"user_{pid}",
                "raw": f"Post {pid}",
            }
            for pid in pids
        ]
        mock_response.json.return_value = {"post_stream": {"posts": posts}}
        return mock_response

    monkeypatch.setattr(httpx.Client, "get", mock_get)
    payload = adapter.parse_payload(
        data=data,
        project_context="python/cpython",
        base_url="https://forum.example.com",
        chunk_size=1,
    )

    # Assert every stream ID is accounted for across multiple chunks
    assert len(payload.messages) == 3
    assert payload.messages[0].external_message_id == "post_100"
    assert payload.messages[1].external_message_id == "post_200"
    assert payload.messages[2].external_message_id == "post_300"

    # Also verify that missing IDs without client or base_url raises ValueError
    with pytest.raises(ValueError, match="neither an HTTP client nor a valid base_url"):
        adapter.parse_payload(data=data, project_context="python/cpython", base_url="")


def test_live_to_deleted_to_restored_lifecycle():
    """Verify live -> deleted -> restored (same content bytes) rotates versions and clears deleted_at."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    init_db(engine)

    msg = RawMessagePayload(
        external_message_id="post_cycle_1",
        author_platform_id="u_cycle",
        author_handle="charlie",
        author_name="Charlie",
        original_timestamp=datetime.now(UTC),
        canonical_permalink="https://example.com/cycle/1",
        raw_content="Valuable restored content",
    )
    thread = RawThreadPayload(
        source_platform=PlatformType.DISCOURSE,
        project_context="python/cpython",
        external_thread_id="topic_cycle",
        title="Lifecycle Test",
        canonical_url="https://example.com/cycle",
        messages=[msg],
    )

    # 1. Ingest initial live version (v1, ORIGINAL)
    with Session(engine) as session:
        r1 = IngestService.ingest_thread(session, thread)
        session.commit()
    assert r1.messages_created == 1
    assert r1.versions_created == 1

    # 2. Ingest deletion (v2, DELETED)
    del_msg = msg.model_copy(update={"is_deleted": True, "delete_reason": "Author flagged for deletion"})
    thread_del = thread.model_copy(update={"messages": [del_msg]})
    with Session(engine) as session:
        r2 = IngestService.ingest_thread(session, thread_del)
        session.commit()
    assert r2.versions_rotated == 1
    assert r2.versions_created == 1

    with Session(engine) as session:
        v2 = session.exec(
            select(SourceMessageVersion).where(
                SourceMessageVersion.is_current == True
            )
        ).first()
        assert v2 is not None
        assert v2.version_number == 2
        assert v2.edit_state == MessageEditState.DELETED
        assert v2.deleted_at is not None

    # 3. Ingest restored post with EXACT SAME content bytes
    restored_msg = msg.model_copy(update={"is_deleted": False, "delete_reason": None})
    thread_restored = thread.model_copy(update={"messages": [restored_msg]})
    with Session(engine) as session:
        r3 = IngestService.ingest_thread(session, thread_restored)
        session.commit()
    assert r3.versions_rotated == 1
    assert r3.versions_created == 1
    assert r3.unchanged_count == 0

    with Session(engine) as session:
        v3 = session.exec(
            select(SourceMessageVersion).where(
                SourceMessageVersion.is_current == True
            )
        ).first()
        assert v3 is not None
        assert v3.version_number == 3
        assert v3.is_current is True
        assert v3.edit_state == MessageEditState.EDITED
        assert v3.deleted_at is None
        assert v3.raw_content == "Valuable restored content"
        assert v3.edit_reason == "Remote post restored from deletion"

    # 4. Ingesting again while still live is an idempotent no-op
    with Session(engine) as session:
        r4 = IngestService.ingest_thread(session, thread_restored)
        session.commit()
    assert r4.unchanged_count == 1
    assert r4.versions_created == 0


@pytest.mark.anyio
async def test_async_live_to_deleted_to_restored_lifecycle():
    """Verify async live -> deleted -> restored (same content bytes) state transitions."""
    engine = get_async_engine("sqlite+aiosqlite:///:memory:")
    await init_async_db(engine)

    msg = RawMessagePayload(
        external_message_id="post_async_cycle_1",
        author_platform_id="u_acycle",
        author_handle="dana",
        author_name="Dana",
        original_timestamp=datetime.now(UTC),
        canonical_permalink="https://example.com/async_cycle/1",
        raw_content="Async valuable content",
    )
    thread = RawThreadPayload(
        source_platform=PlatformType.DISCOURSE,
        project_context="python/cpython",
        external_thread_id="topic_async_cycle",
        title="Async Lifecycle Test",
        canonical_url="https://example.com/async_cycle",
        messages=[msg],
    )

    # 1. Live
    async with get_async_session(engine) as session:
        await IngestService.async_ingest_thread(session, thread)
        await session.commit()

    # 2. Deleted
    del_msg = msg.model_copy(update={"is_deleted": True, "delete_reason": "Transient delete"})
    thread_del = thread.model_copy(update={"messages": [del_msg]})
    async with get_async_session(engine) as session:
        await IngestService.async_ingest_thread(session, thread_del)
        await session.commit()

    # 3. Restored
    restored_msg = msg.model_copy(update={"is_deleted": False, "delete_reason": None})
    thread_restored = thread.model_copy(update={"messages": [restored_msg]})
    async with get_async_session(engine) as session:
        r3 = await IngestService.async_ingest_thread(session, thread_restored)
        await session.commit()

    assert r3.versions_rotated == 1
    assert r3.versions_created == 1

    async with get_async_session(engine) as session:
        v_res = await session.exec(
            select(SourceMessageVersion).where(SourceMessageVersion.is_current == True)
        )
        current_v = v_res.first()
        assert current_v is not None
        assert current_v.version_number == 3
        assert current_v.edit_state == MessageEditState.EDITED
        assert current_v.deleted_at is None
        assert current_v.edit_reason == "Remote post restored from deletion"



# ─────────────────────────────────────────────────────────────────────────────
# Review follow-up tests: noise suppression wiring, Discourse completeness,
# raw/cooked provenance, URL entrypoint, native GitHub shapes, mbox archive
# partitioning, References immediate parent, and thread metadata refresh.
# ─────────────────────────────────────────────────────────────────────────────


def _thread_payload_with_messages(messages):
    """Build a RawThreadPayload with explicit message payloads."""
    from biscuit.ingest.base import RawThreadPayload

    return RawThreadPayload(
        source_platform=PlatformType.DISCOURSE,
        project_context="python/cpython",
        external_thread_id="noise_thread",
        title="Noise Thread",
        canonical_url="https://example.com/noise",
        messages=messages,
    )


def _msg(external_id, handle, content, platform_id=None):
    return RawMessagePayload(
        external_message_id=external_id,
        author_platform_id=platform_id or external_id,
        author_handle=handle,
        author_name=handle,
        original_timestamp=datetime.now(UTC),
        canonical_permalink=f"https://example.com/n/{external_id}",
        raw_content=content,
    )


def test_noise_suppression_bot_and_administrative_filtered():
    """Bot and administrative messages get empty derived views; raw stays intact."""
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    init_db(engine)
    with Session(engine) as session:
        payload = _thread_payload_with_messages(
            [
                _msg("m1", "gvanrossum", "I propose we adopt the new operator."),
                _msg("m2", "codecov[bot]", "Coverage report generated by service."),
                _msg("m3", "randomuser", "+1"),
                _msg("m4", "anotheruser", "friendly ping"),
                _msg("m5", "pyperformance", "Benchmark: 3.2x slowdown on json"),
                _msg("m6", "contributor", "Can I work on this issue?"),
            ]
        )
        IngestService.ingest_thread(session, payload)

        statement = select(SourceMessageVersion).join(SourceMessage).where(
            SourceMessage.external_message_id == "m1"
        )
        human = session.exec(statement).first()
        assert human.derived_cleaned_content == "I propose we adopt the new operator."
        assert human.raw_content == "I propose we adopt the new operator."

        def derived_for(external_id):
            stmt = (
                select(SourceMessageVersion)
                .join(SourceMessage)
                .where(SourceMessage.external_message_id == external_id)
            )
            return session.exec(stmt).first().derived_cleaned_content

        # Bots and administrative chatter are pruned from the derived view...
        assert derived_for("m2") == ""
        assert derived_for("m3") == ""
        assert derived_for("m4") == ""
        assert derived_for("m6") == ""

        # ...but benchmark bots are preserved as meaningful evidence.
        assert "Benchmark" in derived_for("m5")

        # Raw content is never modified for pruned messages.
        raw_m3 = (
            session.exec(
                select(SourceMessageVersion).join(SourceMessage).where(
                    SourceMessage.external_message_id == "m3"
                )
            )
            .first()
            .raw_content
        )
        assert raw_m3 == "+1"


@pytest.mark.anyio
async def test_noise_suppression_async_path():
    """Async ingestion applies the same Tier-2/Tier-3 suppression."""
    aengine = get_async_engine("sqlite+aiosqlite:///:memory:")
    await init_async_db(aengine)

    async with get_async_session(aengine) as session:
        payload = _thread_payload_with_messages(
            [
                _msg("a1", "human", "Detailed objection to the proposal."),
                _msg("a2", "stale[bot]", "Stale issue notice."),
                _msg("a3", "user", "LGTM"),
            ]
        )
        await IngestService.async_ingest_thread(session, payload)

        stmt = (
            select(SourceMessageVersion)
            .join(SourceMessage)
            .where(SourceMessage.external_message_id == "a2")
        )
        result = await session.exec(stmt)
        bot_version = result.first()
        assert bot_version.derived_cleaned_content == ""
        assert bot_version.raw_content == "Stale issue notice."

        stmt2 = (
            select(SourceMessageVersion)
            .join(SourceMessage)
            .where(SourceMessage.external_message_id == "a3")
        )
        result2 = await session.exec(stmt2)
        assert result2.first().derived_cleaned_content == ""


def test_discourse_partial_response_raises_on_missing_ids():
    """A partial 200 response that cannot return all stream IDs fails closed."""
    adapter = DiscourseAdapter()
    payload = {
        "id": 900,
        "title": "Partial topic",
        "slug": "partial",
        "post_stream": {
            "stream": [100, 200, 300],
            "posts": [
                {"id": 100, "post_number": 1, "raw": "first"},
                {"id": 200, "post_number": 2, "raw": "second"},
                # 300 never returned
            ],
        },
    }

    class _PartialClient(httpx.Client):
        def get(self, url, params=None):
            class _Resp:
                def raise_for_status(self):
                    pass

                def json(self):
                    # Server returns only post 200 despite being asked for 300
                    return {"post_stream": {"posts": [{"id": 200, "post_number": 2}]}}

            return _Resp()

    from biscuit.ingest.discourse import DiscourseIncompletenessError

    with pytest.raises(DiscourseIncompletenessError):
        adapter.parse_payload(
            data=payload,
            project_context="python/cpython",
            base_url="https://discuss.python.org",
            client=_PartialClient(),
        )


def test_discourse_partial_response_non_strict_records_unavailable():
    """Non-strict mode records unavailable post IDs explicitly instead of raising."""
    adapter = DiscourseAdapter()
    payload = {
        "id": 901,
        "title": "Partial topic 2",
        "slug": "partial2",
        "post_stream": {
            "stream": [10, 20, 30],
            "posts": [{"id": 10, "post_number": 1, "raw": "only one"}],
        },
    }

    class _PartialClient(httpx.Client):
        def get(self, url, params=None):
            class _Resp:
                def raise_for_status(self):
                    pass

                def json(self):
                    return {"post_stream": {"posts": [{"id": 20, "post_number": 2}]}}

            return _Resp()

    thread = adapter.parse_payload(
        data=payload,
        project_context="python/cpython",
        base_url="https://discuss.python.org",
        client=_PartialClient(),
        strict_completeness=False,
    )
    assert thread.ingestion_metadata["unavailable_post_ids"] == [30]
    assert len(thread.messages) == 2


def test_discourse_raw_vs_cooked_provenance():
    """raw is used even when empty; cooked fallback is recorded in metadata."""
    adapter = DiscourseAdapter()
    payload = {
        "id": 910,
        "title": "Provenance topic",
        "slug": "prov",
        "post_stream": {
            "posts": [
                {"id": 1, "post_number": 1, "raw": "**raw** markdown"},
                {"id": 2, "post_number": 2, "raw": ""},
                {"id": 3, "post_number": 3, "cooked": "<p>rendered</p>"},
            ]
        },
    }
    thread = adapter.parse_payload(
        data=payload, project_context="python/cpython", base_url="https://discuss.python.org"
    )
    m1, m2, m3 = thread.messages
    assert m1.raw_content == "**raw** markdown"
    assert m1.metadata["content_representation"] == "raw"
    # Empty raw string is still "raw present" — no cooked substitution.
    assert m2.raw_content == ""
    assert m2.metadata["content_representation"] == "raw"
    # cooked only when raw key is genuinely absent, and marked as such.
    assert m3.raw_content == "<p>rendered</p>"
    assert m3.metadata["content_representation"] == "cooked"
    assert thread.ingestion_metadata["contains_cooked_only_posts"] is True


def test_discourse_url_entrypoint_parses_and_enforces_host_policy():
    """URL entrypoint extracts topic id; foreign hosts are rejected."""
    adapter = DiscourseAdapter()
    base, topic_id = adapter.parse_topic_url(
        "https://discuss.python.org/t/none-aware-operators/1234/7"
    )
    assert base == "https://discuss.python.org"
    assert topic_id == "1234"

    with pytest.raises(ValueError, match="not in the allowed hosts"):
        adapter.parse_topic_url("https://evil.example.com/t/1234")

    with pytest.raises(ValueError, match="does not match"):
        adapter.parse_topic_url("https://discuss.python.org/u/someuser")


def test_discourse_fetch_topic_from_url_mock():
    """fetch_topic_from_url retrieves /t/{id}.json and feeds the canonical path."""
    adapter = DiscourseAdapter()
    topic_json = {
        "id": 555,
        "title": "URL Topic",
        "slug": "url-topic",
        "post_stream": {"stream": [1], "posts": [{"id": 1, "post_number": 1, "raw": "hi"}]},
    }

    class _MockClient(httpx.Client):
        def get(self, url, params=None):
            assert url == "https://discuss.python.org/t/555.json"

            class _Resp:
                def raise_for_status(self):
                    pass

                def json(self):
                    return topic_json

            return _Resp()

    data = adapter.fetch_topic_from_url(
        "https://discuss.python.org/t/url-topic/555", client=_MockClient()
    )
    thread = adapter.parse_payload(
        data=data, project_context="python/cpython", base_url="https://discuss.python.org"
    )
    assert thread.external_thread_id == "topic_555"
    assert len(thread.messages) == 1


def test_github_native_rest_shapes():
    """Native PR payload with issue_comments, review_comments, reviews arrays."""
    adapter = GitHubAdapter()
    payload = {
        "number": 505,
        "title": "Native PR",
        "html_url": "https://github.com/python/cpython/pull/505",
        "body": "PR description",
        "user": {"id": 1, "login": "author_user"},
        "created_at": "2020-01-01T00:00:00Z",
        "state": "open",
        "comments": 12,  # native payloads store a COUNT here, not a list
        "issue_comments": [
            {
                "id": 1001,
                "user": {"id": 2, "login": "reviewer"},
                "body": "Objection on grammar ambiguity",
                "created_at": "2020-01-02T00:00:00Z",
                "html_url": "https://github.com/python/cpython/issues/505#issuecomment-1001",
            }
        ],
        "reviews": [
            {
                "id": 2001,
                "user": {"id": 3, "login": "guido"},
                "body": "I reject this approach.",
                "state": "CHANGES_REQUESTED",
                "submitted_at": "2020-01-03T00:00:00Z",
                "commit_id": "a" * 40,
            }
        ],
        "review_comments": [
            {
                "id": 3001,
                "user": {"id": 2, "login": "reviewer"},
                "body": "Inline comment",
                "created_at": "2020-01-04T00:00:00Z",
                "path": "Parser/tokenizer.c",
                "line": 42,
                "diff_hunk": "@@ -40,3 +40,3 @@",
                "in_reply_to_id": None,
                "pull_request_review_id": 2001,
            },
            {
                "id": 3002,
                "user": {"id": 4, "login": "someone"},
                "body": "Reply inline",
                "created_at": "2020-01-05T00:00:00Z",
                "in_reply_to_id": 3001,
            },
        ],
    }
    thread = adapter.parse_payload(data=payload, project_context="python/cpython")

    ids = [m.external_message_id for m in thread.messages]
    assert "pr_desc_505" in ids
    assert "comment_1001" in ids
    assert "review_2001" in ids
    assert "review_comment_3001" in ids
    assert "review_comment_3002" in ids

    by_id = {m.external_message_id: m for m in thread.messages}
    # Review summary preserved with state metadata.
    assert by_id["review_2001"].raw_content == "I reject this approach."
    assert by_id["review_2001"].metadata["review_state"] == "CHANGES_REQUESTED"
    # Inline reply chain resolved through in_reply_to_id.
    assert by_id["review_comment_3002"].parent_message_id == "review_comment_3001"
    assert by_id["review_comment_3001"].metadata["file_path"] == "Parser/tokenizer.c"
    assert by_id["comment_1001"].parent_message_id == "pr_desc_505"
    # Numeric comments field (count) must not crash the adapter.
    assert thread.ingestion_metadata["reviews_count"] == 1


def test_github_legacy_comments_still_work():
    """Legacy synthetic comments list is still accepted."""
    adapter = GitHubAdapter()
    payload = {
        "number": 9,
        "title": "Legacy",
        "user": {"id": 1, "login": "u1"},
        "comments": [
            {"id": 77, "user": {"id": 2, "login": "u2"}, "body": "legacy comment"}
        ],
    }
    thread = adapter.parse_payload(data=payload, project_context="python/cpython")
    assert any(m.external_message_id == "comment_77" for m in thread.messages)


def test_mbox_multi_conversation_partitioning():
    """Interleaved two-conversation archive splits into separate threads."""
    adapter = MboxAdapter()
    mbox_bytes = (
        b"From a@example.org Mon Jan 01 10:00:00 2020\n"
        b"Message-ID: <conv-a-root@example.org>\n"
        b"Subject: [Python-Dev] Conversation A\n"
        b"From: Alice <alice@example.org>\n"
        b"Date: Mon, 01 Jan 2020 10:00:00 +0000\n"
        b"\n"
        b"Root of conversation A\n"
        b"\n"
        b"From b@example.org Mon Jan 01 11:00:00 2020\n"
        b"Message-ID: <conv-b-root@example.org>\n"
        b"Subject: [Python-Dev] Conversation B\n"
        b"From: Bob <bob@example.org>\n"
        b"Date: Mon, 01 Jan 2020 11:00:00 +0000\n"
        b"\n"
        b"Root of conversation B\n"
        b"\n"
        b"From c@example.org Mon Jan 01 12:00:00 2020\n"
        b"Message-ID: <conv-a-reply@example.org>\n"
        b"In-Reply-To: <conv-a-root@example.org>\n"
        b"Subject: Re: [Python-Dev] Conversation A\n"
        b"From: Carol <carol@example.org>\n"
        b"Date: Mon, 01 Jan 2020 12:00:00 +0000\n"
        b"\n"
        b"Reply in conversation A\n"
    )
    threads = adapter.parse_archive(mbox_bytes, "python/cpython")
    assert len(threads) == 2

    by_id = {t.external_thread_id: t for t in threads}
    assert "conv-a-root@example.org" in by_id
    assert "conv-b-root@example.org" in by_id
    assert len(by_id["conv-a-root@example.org"].messages) == 2
    assert len(by_id["conv-b-root@example.org"].messages) == 1


def test_mbox_references_resolves_immediate_parent():
    """Deep reply chains point at their direct parent, not the thread root."""
    adapter = MboxAdapter()
    mbox_bytes = (
        b"From a@example.org Mon Jan 01 10:00:00 2020\n"
        b"Message-ID: <r1@example.org>\n"
        b"Subject: Chain\n"
        b"From: Alice <alice@example.org>\n"
        b"Date: Mon, 01 Jan 2020 10:00:00 +0000\n"
        b"\n"
        b"Root\n"
        b"\n"
        b"From b@example.org Mon Jan 01 11:00:00 2020\n"
        b"Message-ID: <r2@example.org>\n"
        b"In-Reply-To: <r1@example.org>\n"
        b"Subject: Re: Chain\n"
        b"From: Bob <bob@example.org>\n"
        b"Date: Mon, 01 Jan 2020 11:00:00 +0000\n"
        b"\n"
        b"First reply\n"
        b"\n"
        b"From c@example.org Mon Jan 01 12:00:00 2020\n"
        b"Message-ID: <r3@example.org>\n"
        b"References: <r1@example.org>\n"
        b" <r2@example.org>\n"
        b"Subject: Re: Chain\n"
        b"From: Carol <carol@example.org>\n"
        b"Date: Mon, 01 Jan 2020 12:00:00 +0000\n"
        b"\n"
        b"Second reply (folded References)\n"
    )
    thread = adapter.parse_payload(mbox_bytes, "python/cpython")
    by_id = {m.external_message_id: m for m in thread.messages}
    # Folded multi-ID References: immediate parent is r2, not the root r1.
    assert by_id["r3@example.org"].parent_message_id == "r2@example.org"


def test_thread_metadata_refreshed_on_reingestion():
    """Reingesting a renamed/revised topic refreshes thread-level metadata."""
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    init_db(engine)
    with Session(engine) as session:
        first = RawThreadPayload(
            source_platform=PlatformType.DISCOURSE,
            project_context="python/cpython",
            external_thread_id="refresh_topic",
            title="Original Title",
            canonical_url="https://example.com/t/1",
            source_revision="v1",
            ingestion_metadata={"topic_id": "1", "posts_count": 1},
            messages=[_msg("r1", "u", "content v1")],
        )
        IngestService.ingest_thread(session, first)

        second = RawThreadPayload(
            source_platform=PlatformType.DISCOURSE,
            project_context="python/cpython",
            external_thread_id="refresh_topic",
            title="Renamed Title",
            canonical_url="https://example.com/t/1-renamed",
            source_revision="v2",
            ingestion_metadata={"topic_id": "1", "posts_count": 2},
            messages=[_msg("r1", "u", "content v1")],
        )
        IngestService.ingest_thread(session, second)

        statement = select(SourceThread).where(
            SourceThread.external_thread_id == "refresh_topic"
        )
        thread = session.exec(statement).first()
        assert thread.title == "Renamed Title"
        assert thread.canonical_url == "https://example.com/t/1-renamed"
        assert thread.source_revision == "v2"
        assert thread.ingestion_metadata["reingested_at"] is not None

        # Historical message versions are untouched.
        version = session.exec(
            select(SourceMessageVersion).join(SourceMessage).where(
                SourceMessage.external_message_id == "r1"
            )
        ).first()
        assert version.raw_content == "content v1"
        assert version.version_number == 1
