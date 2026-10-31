"""Canonical thread integrity and strict separation of withheld benchmark labels."""

# ruff: noqa: I001

from __future__ import annotations

import hashlib
import hmac
import json
import re
from pathlib import Path
from typing import Any


DEFAULT_FIXTURE_ROOT = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "eval"


def _json_file(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def canonical_thread_bytes(thread: dict[str, Any]) -> bytes:
    """v1: omit checksum/comments; sort messages before canonical UTF-8 JSON."""
    document = {k: v for k, v in thread.items() if k != "thread_sha256" and not k.startswith("_")}
    document["messages"] = sorted(
        document["messages"],
        key=lambda row: (str(row["created_at"]), str(row["message_id"])),
    )
    return json.dumps(
        document, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def thread_digest(thread: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_thread_bytes(thread)).hexdigest()


def verify_thread(thread: dict[str, Any]) -> str:
    """Reject incomplete, altered, or non-canonically checksummed snapshots."""
    messages = thread.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ValueError("Thread must contain nonempty messages")
    if thread.get("expected_source_message_count") != len(messages):
        raise ValueError("Incomplete thread: expected message count differs")
    ids: set[str] = set()
    for message in messages:
        mid = str(message["message_id"])
        if mid in ids:
            raise ValueError("Duplicate message ID: " + mid)
        ids.add(mid)
        raw = message["raw_text"]
        if not isinstance(raw, str):
            raise TypeError("Raw text must be Unicode")
        actual = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        checksum = message.get("content_sha256")
        if not isinstance(checksum, str) or not hmac.compare_digest(actual, checksum):
            raise ValueError("Message SHA-256 mismatch: " + mid)
    supplied = thread.get("thread_sha256")
    actual_thread = thread_digest(thread)
    if not isinstance(supplied, str) or not hmac.compare_digest(actual_thread, supplied):
        raise ValueError("Thread SHA-256 mismatch or missing digest")
    return actual_thread


def _benchmark_directory(name: str, root: Path | None = None) -> Path:
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name):
        raise ValueError("Invalid benchmark identifier")
    return (root or DEFAULT_FIXTURE_ROOT) / name


def load_thread(name: str, root: Path | None = None) -> dict[str, Any]:
    thread = _json_file(_benchmark_directory(name, root) / "raw_thread.json")
    verify_thread(thread)
    return thread


def load_ground_truth(name: str, root: Path | None = None) -> dict[str, Any]:
    """Scorer-only function; never call this from a model/predictor adapter."""
    return _json_file(_benchmark_directory(name, root) / "ground_truth.json")


def load_pr_cases(root: Path | None = None) -> list[dict[str, Any]]:
    case_root = (root or DEFAULT_FIXTURE_ROOT) / "pr_cases"
    return [_json_file(path) for path in sorted(case_root.glob("pr-*.json"))]


def load_pr_labels(root: Path | None = None) -> dict[str, str]:
    """Scorer-only evaluation oracle, stored separately from input diff cases."""
    return _json_file((root or DEFAULT_FIXTURE_ROOT) / "pr_labels.json")


def predictor_input(thread: dict[str, Any], root: Path | None = None) -> dict[str, Any]:
    """Construct prediction input with no ground truth, expected outcome, or PEP text."""
    verified = verify_thread(thread)
    return {
        "thread_id": thread["thread_id"],
        "source_platform": thread["source_platform"],
        "thread_sha256": verified,
        "messages": [
            {
                "message_id": item["message_id"],
                "author_handle": item["author_handle"],
                "created_at": item["created_at"],
                "raw_text": item["raw_text"],
            }
            for item in thread["messages"]
        ],
        "pr_cases": load_pr_cases(root),
    }
