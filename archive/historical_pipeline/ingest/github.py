"""GitHub Pull Request discussion and review comment ingestion adapter."""

from datetime import UTC, datetime
from typing import Any

from dateutil import parser as dt_parser

from biscuit.ingest.base import BaseIngestAdapter, RawMessagePayload, RawThreadPayload
from biscuit.models.enums import PlatformType


class GitHubAdapter(BaseIngestAdapter):
    """Adapter for ingesting GitHub Pull Request review threads and comments.

    Accepts either the pre-aggregated ``comments`` array (synthetic shape) or
    the native GitHub REST API collections: ``issue_comments``, ``review_comments``,
    and ``reviews`` (the three separately-paginated endpoints exposed by the
    REST API). When none of the detailed collections are present and
    ``comments`` is an integer (the native PR detail payload stores only a
    count there), the adapter treats the thread as description-only rather
    than crashing.
    """

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

        Expected native REST API aggregation:
        {
            "number": 505,
            "title": "Add PEP 505: None-aware operators",
            "html_url": "https://github.com/python/cpython/pull/505",
            "body": "PR description text...",
            "user": {"id": 1, "login": "author_user"},
            "created_at": "2020-01-01T00:00:00Z",
            "issue_comments": [ ... /repos/{org}/{repo}/issues/{n}/comments ... ],
            "review_comments": [ ... /repos/{org}/{repo}/pulls/{n}/comments ... ],
            "reviews": [ ... /repos/{org}/{repo}/pulls/{n}/reviews ... ]
        }
        The legacy synthetic "comments" list is still accepted.
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

        # 2. Issue comments (conversation level, /issues/{n}/comments)
        for comment in data.get("issue_comments", []) or []:
            messages.append(
                self._parse_issue_comment(comment, pr_number, canonical_url)
            )

        # 3. Review-level submissions (/pulls/{n}/reviews): a review carries its
        # own body (the review summary ruling text) distinct from inline comments.
        for review in data.get("reviews", []) or []:
            messages.append(self._parse_review(review, pr_number, canonical_url))

        # 4. Inline review comments (/pulls/{n}/comments): threaded diffs,
        # preserving in_reply_to_id for reply chains.
        for comment in data.get("review_comments", []) or []:
            messages.append(
                self._parse_review_comment(comment, pr_number, canonical_url)
            )

        # 5. Legacy synthetic aggregated comments array (backward compatible)
        comments_field = data.get("comments")
        if isinstance(comments_field, list):
            for comment in comments_field:
                messages.append(
                    self._parse_issue_comment(comment, pr_number, canonical_url)
                )
        # A numeric comments field in a native payload is just a count; nothing to do.

        return RawThreadPayload(
            source_platform=self.platform,
            project_context=project_context,
            external_thread_id=f"pull_{pr_number}",
            title=pr_title,
            canonical_url=canonical_url,
            source_revision=kwargs.get("source_revision", "v1"),
            ingestion_metadata={
                "pr_number": int(pr_number),
                "base_ref": (data.get("base") or {}).get("ref"),
                "head_ref": (data.get("head") or {}).get("ref"),
                "state": data.get("state", "open"),
                "issue_comments_count": len(data.get("issue_comments") or []),
                "review_comments_count": len(data.get("review_comments") or []),
                "reviews_count": len(data.get("reviews") or []),
            },
            messages=messages,
        )

    @staticmethod
    def _parse_timestamp(created_str: Any) -> datetime:
        if created_str:
            parsed = dt_parser.isoparse(created_str)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed
        return datetime.now(UTC)

    def _parse_issue_comment(
        self, comment: dict[str, Any], pr_number: str, canonical_url: str
    ) -> RawMessagePayload:
        """Parse an issue-level conversation comment."""
        c_id = str(comment.get("id", ""))
        c_user = comment.get("user") or {}
        c_handle = c_user.get("login", "ghost")
        reply_to = comment.get("in_reply_to_id")

        comment_metadata: dict[str, Any] = {
            "pr_number": int(pr_number),
            "comment_kind": "issue_comment",
        }
        if "diff_hunk" in comment:
            comment_metadata["diff_hunk"] = comment["diff_hunk"]
        if "path" in comment:
            comment_metadata["file_path"] = comment["path"]
        if "line" in comment:
            comment_metadata["line"] = comment["line"]

        return RawMessagePayload(
            external_message_id=f"comment_{c_id}",
            parent_message_id=f"comment_{reply_to}" if reply_to else f"pr_desc_{pr_number}",
            author_platform_id=str(c_user.get("id", "0")),
            author_handle=c_handle,
            author_name=c_user.get("name") or c_handle,
            author_email_prefix=None,
            original_timestamp=self._parse_timestamp(comment.get("created_at")),
            canonical_permalink=comment.get("html_url")
            or f"{canonical_url}#issuecomment-{c_id}",
            raw_content=comment.get("body", ""),
            metadata=comment_metadata,
        )

    def _parse_review(
        self, review: dict[str, Any], pr_number: str, canonical_url: str
    ) -> RawMessagePayload:
        """Parse a review-level submission (summary body + state)."""
        r_id = str(review.get("id", ""))
        r_user = review.get("user") or {}
        r_handle = r_user.get("login", "ghost")

        return RawMessagePayload(
            external_message_id=f"review_{r_id}",
            parent_message_id=f"pr_desc_{pr_number}",
            author_platform_id=str(r_user.get("id", "0")),
            author_handle=r_handle,
            author_name=r_user.get("name") or r_handle,
            author_email_prefix=None,
            original_timestamp=self._parse_timestamp(review.get("submitted_at")),
            canonical_permalink=review.get("html_url")
            or f"{canonical_url}#review-{r_id}",
            raw_content=review.get("body", ""),
            metadata={
                "pr_number": int(pr_number),
                "comment_kind": "review",
                "review_state": review.get("state"),
                "commit_id": review.get("commit_id"),
            },
        )

    def _parse_review_comment(
        self, comment: dict[str, Any], pr_number: str, canonical_url: str
    ) -> RawMessagePayload:
        """Parse an inline review comment attached to a diff position."""
        c_id = str(comment.get("id", ""))
        c_user = comment.get("user") or {}
        c_handle = c_user.get("login", "ghost")
        reply_to = comment.get("in_reply_to_id")

        return RawMessagePayload(
            external_message_id=f"review_comment_{c_id}",
            parent_message_id=f"review_comment_{reply_to}"
            if reply_to
            else f"pr_desc_{pr_number}",
            author_platform_id=str(c_user.get("id", "0")),
            author_handle=c_handle,
            author_name=c_user.get("name") or c_handle,
            author_email_prefix=None,
            original_timestamp=self._parse_timestamp(comment.get("created_at")),
            canonical_permalink=comment.get("html_url")
            or f"{canonical_url}#discussion_r{c_id}",
            raw_content=comment.get("body", ""),
            metadata={
                "pr_number": int(pr_number),
                "comment_kind": "review_comment",
                "diff_hunk": comment.get("diff_hunk"),
                "file_path": comment.get("path"),
                "line": comment.get("line"),
                "original_line": comment.get("original_line"),
                "side": comment.get("side"),
                "pull_request_review_id": comment.get("pull_request_review_id"),
            },
        )
