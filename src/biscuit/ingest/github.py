"""GitHub Pull Request discussion and review comment ingestion adapter."""

from datetime import UTC, datetime
from typing import Any

from dateutil import parser as dt_parser

from biscuit.ingest.base import BaseIngestAdapter, RawMessagePayload, RawThreadPayload
from biscuit.models.enums import PlatformType


class GitHubAdapter(BaseIngestAdapter):
    """Adapter for ingesting GitHub Pull Request review threads and comments."""

    @property
    def platform(self) -> PlatformType:
        return PlatformType.GITHUB_PR

    def parse_payload(
        self,
        data: dict[str, Any],
        project_context: str,
        **kwargs: Any,
    ) -> RawThreadPayload:
        """Parse GitHub PR data containing PR metadata and review comments.

        Expected format:
        {
            "number": 505,
            "title": "Add PEP 505: None-aware operators",
            "html_url": "https://github.com/python/cpython/pull/505",
            "body": "PR description text...",
            "user": {"id": 1, "login": "author_user", "name": "Author Name"},
            "created_at": "2020-01-01T00:00:00Z",
            "comments": [
                {
                    "id": 1001,
                    "user": {"id": 2, "login": "reviewer", "name": "Reviewer Name"},
                    "body": "I object to syntax ambiguity...",
                    "created_at": "2020-01-02T00:00:00Z",
                    "html_url": "https://github.com/python/cpython/pull/505#issuecomment-1001",
                    "in_reply_to_id": null,
                    "diff_hunk": "@@ -1,5 +1,5 @@"
                }
            ]
        }
        """
        pr_number = str(data.get("number", "0"))
        pr_title = data.get("title", f"PR #{pr_number}")
        canonical_url = data.get(
            "html_url", f"https://github.com/{project_context}/pull/{pr_number}"
        )

        messages: list[RawMessagePayload] = []

        # 1. PR Opener Description (initial message)
        pr_user = data.get("user") or {}
        pr_body = data.get("body") or ""
        pr_created_str = data.get("created_at")
        if pr_created_str:
            pr_dt = dt_parser.isoparse(pr_created_str)
            if pr_dt.tzinfo is None:
                pr_dt = pr_dt.replace(tzinfo=UTC)
        else:
            pr_dt = datetime.now(UTC)

        pr_opener_handle = pr_user.get("login", "ghost")
        messages.append(
            RawMessagePayload(
                external_message_id=f"pr_desc_{pr_number}",
                parent_message_id=None,
                author_platform_id=str(pr_user.get("id", "0")),
                author_handle=pr_opener_handle,
                author_name=pr_user.get("name") or pr_opener_handle,
                author_email_prefix=None,
                original_timestamp=pr_dt,
                canonical_permalink=canonical_url,
                raw_content=pr_body,
                metadata={"is_pr_description": True, "pr_number": int(pr_number)},
            )
        )

        # 2. PR Review Comments & Discussion Comments
        comments_list: list[dict[str, Any]] = data.get("comments", [])
        for comment in comments_list:
            c_id = str(comment.get("id", ""))
            c_user = comment.get("user") or {}
            c_handle = c_user.get("login", "ghost")
            c_name = c_user.get("name") or c_handle
            c_body = comment.get("body", "")
            c_url = comment.get("html_url") or f"{canonical_url}#issuecomment-{c_id}"

            created_str = comment.get("created_at")
            if created_str:
                c_dt = dt_parser.isoparse(created_str)
                if c_dt.tzinfo is None:
                    c_dt = c_dt.replace(tzinfo=UTC)
            else:
                c_dt = datetime.now(UTC)

            reply_to = comment.get("in_reply_to_id")
            parent_id = f"comment_{reply_to}" if reply_to else f"pr_desc_{pr_number}"

            comment_metadata: dict[str, Any] = {
                "pr_number": int(pr_number),
            }
            if "diff_hunk" in comment:
                comment_metadata["diff_hunk"] = comment["diff_hunk"]
            if "path" in comment:
                comment_metadata["file_path"] = comment["path"]
            if "line" in comment:
                comment_metadata["line"] = comment["line"]

            messages.append(
                RawMessagePayload(
                    external_message_id=f"comment_{c_id}",
                    parent_message_id=parent_id,
                    author_platform_id=str(c_user.get("id", "0")),
                    author_handle=c_handle,
                    author_name=c_name,
                    author_email_prefix=None,
                    original_timestamp=c_dt,
                    canonical_permalink=c_url,
                    raw_content=c_body,
                    metadata=comment_metadata,
                )
            )

        return RawThreadPayload(
            source_platform=self.platform,
            project_context=project_context,
            external_thread_id=f"pull_{pr_number}",
            title=pr_title,
            canonical_url=canonical_url,
            source_revision=kwargs.get("source_revision", "v1"),
            ingestion_metadata={
                "pr_number": int(pr_number),
                "base_ref": data.get("base", {}).get("ref"),
                "head_ref": data.get("head", {}).get("ref"),
                "state": data.get("state", "open"),
            },
            messages=messages,
        )
