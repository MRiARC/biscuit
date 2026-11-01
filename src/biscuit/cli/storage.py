"""Small, offline SQLite workspace for CLI ingestion and explicit decision imports.

This is a developer cache, NOT a replacement for the separate PostgreSQL storage stream.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def text(self) -> str:
        return " ".join(" ".join(self.parts).split())


def _plain_html(content: str) -> str:
    parser = _TextExtractor()
    parser.feed(content)
    return parser.text()


def _read_topic_json(url: str, allowed_host: str) -> dict:
    """Read bounded JSON and reject cross-host or non-HTTPS redirects."""
    request = Request(url, headers={"User-Agent": "Biscuit/0.1 (read-only)"})
    with urlopen(request, timeout=20) as response:
        final = urlparse(response.url)
        if final.scheme != "https" or final.hostname != allowed_host:
            raise ValueError("Cross-origin or non-HTTPS Discourse redirect refused")
        data = response.read(8_000_001)
        if len(data) > 8_000_000:
            raise ValueError("Discourse response exceeds 8 MB")
    payload = json.loads(data)
    if not isinstance(payload, dict):
        raise TypeError("Discourse response must be a JSON object")
    return payload


def _fetch_discourse(url: str) -> dict:
    """Read all posts in a topic using Discourse post-stream pagination."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Discourse URLs must be HTTPS and must not contain credentials")
    if parsed.hostname in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("Localhost is not a valid Discourse ingestion host")
    match = re.fullmatch(r"/t/([^/?#]+)/(\d+)(?:/\d+)?/?", parsed.path)
    if match is None:
        raise ValueError("Expected Discourse topic URL in /t/slug/topic-id form")
    slug, topic_id = match.groups()
    root = f"https://{parsed.netloc}/t/{slug}/{topic_id}"
    payload = _read_topic_json(root + ".json", parsed.hostname)
    stream = payload.get("post_stream", {})
    initial = stream.get("posts", [])
    if not isinstance(initial, list) or not initial:
        raise ValueError("Discourse topic contained no posts")
    posts_by_id = {int(post["id"]): post for post in initial}
    ordered_ids = [int(value) for value in stream.get("stream", [])]
    if not ordered_ids:
        ordered_ids = [int(post["id"]) for post in initial]
    if len(ordered_ids) != len(set(ordered_ids)):
        raise ValueError("Duplicate Discourse post IDs in stream")
    missing = [post_id for post_id in ordered_ids if post_id not in posts_by_id]
    for offset in range(0, len(missing), 20):
        chunk = missing[offset:offset + 20]
        query = urlencode([("post_ids[]", str(post_id)) for post_id in chunk])
        page_url = f"https://{parsed.netloc}/t/{topic_id}/posts.json?{query}"
        page = _read_topic_json(page_url, parsed.hostname)
        fetched = page.get("post_stream", {}).get("posts", page.get("posts", []))
        for post in fetched:
            post_id = int(post["id"])
            if post_id not in chunk:
                raise ValueError("Unexpected post in Discourse paginated response")
            posts_by_id[post_id] = post
    if set(ordered_ids) != set(posts_by_id):
        raise ValueError("Incomplete or inconsistent Discourse post-stream pagination")
    messages = []
    for post_id in ordered_ids:
        post = posts_by_id[post_id]
        raw_text = post.get("raw")
        has_raw = isinstance(raw_text, str)
        if not has_raw:
            raw_text = _plain_html(str(post.get("cooked", "")))
        messages.append(
            {
                "message_id": str(post_id),
                "author": str(post.get("username", "")),
                "created_at": str(post.get("created_at", "")),
                "raw_text": raw_text,
                "content_source": "RAW" if has_raw else "RENDERED_HTML_UNVERIFIED",
            }
        )
    return {
        "thread_id": str(payload.get("id", topic_id)),
        "url": url,
        "title": str(payload.get("title", "")),
        "messages": messages,
    }



def canonical_ingested_thread(record: dict) -> dict:
    """Convert a complete Discourse snapshot into the evaluator's verifiable wire schema.

    A hash authenticates the stored text bytes; it does NOT promote HTML-derived
    text or unverified author roles into historical evidence.
    """
    from biscuit.evaluate.fixtures import thread_digest, verify_thread

    messages = []
    for item in record["messages"]:
        raw = str(item["raw_text"])
        messages.append({
            "message_id": str(item["message_id"]),
            "author_handle": str(item.get("author", "")),
            "author_role": "UNVERIFIED",
            "created_at": str(item["created_at"]),
            "raw_text": raw,
            "content_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
            "content_source": item.get("content_source", "UNVERIFIED"),
        })
    snapshot = {
        "thread_id": str(record["thread_id"]),
        "source_platform": "DISCOURSE",
        "external_url": str(record["url"]),
        "expected_source_message_count": len(messages),
        "canonicalization_version": 1,
        "source_provenance": {
            "license_identifier": "UNKNOWN_UNVERIFIED",
            "terms_url": None,
            "retrieval_date": datetime.now(UTC).date().isoformat(),
            "source_text_fidelity": "RAW_OR_DERIVED_TEXT_WITH_PER_MESSAGE_FLAGS",
        },
        "messages": messages,
    }
    snapshot["thread_sha256"] = thread_digest(snapshot)
    verify_thread(snapshot)
    return snapshot


def connect_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS threads (
            thread_id TEXT PRIMARY KEY, source_url TEXT NOT NULL, title TEXT NOT NULL,
            raw_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS decisions (
            decision_id TEXT PRIMARY KEY, title TEXT NOT NULL, status TEXT NOT NULL,
            context TEXT NOT NULL, rationale TEXT NOT NULL, consequences TEXT NOT NULL
        );
        """
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(decisions)")}
    if "record_json" not in columns:
        conn.execute("ALTER TABLE decisions ADD COLUMN record_json TEXT NOT NULL DEFAULT '{}'")
    return conn


def ingest_discourse(url: str, path: Path, output_thread: Path | None = None) -> int:
    record = _fetch_discourse(url)
    snapshot = canonical_ingested_thread(record)
    record["canonical_thread"] = snapshot
    with connect_db(path) as conn:
        conn.execute(
            """INSERT INTO threads (thread_id, source_url, title, raw_json)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(thread_id) DO UPDATE SET
               source_url=excluded.source_url, title=excluded.title,
               raw_json=excluded.raw_json""",
            (record["thread_id"], record["url"], record["title"], json.dumps(record)),
        )
    if output_thread is not None:
        output_thread.parent.mkdir(parents=True, exist_ok=True)
        output_thread.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return len(record["messages"])


VALID_STATES = {
    "PROPOSED", "UNDER_DISCUSSION", "ACCEPTED", "REJECTED", "WITHDRAWN",
    "DEFERRED", "SUPERSEDED", "UNRESOLVED", "DISPUTED", "ACCEPTED_WITH_MODIFICATION",
}


def import_decisions(json_path: Path, db_path: Path) -> int:
    """Load explicitly supplied, human-reviewed decision records; never infer from raw posts."""
    records = json.loads(json_path.read_text(encoding="utf-8"))
    if not isinstance(records, list):
        raise TypeError("Expected a JSON list of decisions")
    validated = []
    for item in records:
        decision_id = str(item["decision_id"])
        status = str(item["status"])
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", decision_id):
            raise ValueError("Invalid decision ID")
        if status not in VALID_STATES:
            raise ValueError("Invalid decision status: " + status)
        for field_name in ("deciders", "decision_drivers", "links"):
            if field_name in item and (
                not isinstance(item[field_name], list)
                or not all(isinstance(value, str) for value in item[field_name])
            ):
                raise TypeError(field_name + " must be a list of strings")
        if "considered_options" in item and (
            not isinstance(item["considered_options"], list)
            or not all(isinstance(value, dict) for value in item["considered_options"])
        ):
            raise TypeError("considered_options must be a list of objects")
        validated.append(
            (
                decision_id,
                str(item["title"]), status, str(item["context"]),
                str(item["rationale"]), str(item["consequences"]),
                json.dumps(item, ensure_ascii=False, sort_keys=True),
            )
        )
    with connect_db(db_path) as conn:
        conn.executemany(
            """INSERT INTO decisions
               (decision_id, title, status, context, rationale, consequences, record_json)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(decision_id) DO UPDATE SET
               title=excluded.title, status=excluded.status, context=excluded.context,
               rationale=excluded.rationale, consequences=excluded.consequences,
               record_json=excluded.record_json""",
            validated,
        )
    return len(validated)


def list_decisions(db_path: Path, status: str | None = None) -> list[dict]:
    if status is not None and status not in VALID_STATES:
        raise ValueError("Invalid decision state: " + status)
    with connect_db(db_path) as conn:
        conn.row_factory = sqlite3.Row
        if status:
            rows = conn.execute(
                "SELECT * FROM decisions WHERE status=? ORDER BY decision_id", (status,)
            )
        else:
            rows = conn.execute("SELECT * FROM decisions ORDER BY decision_id")
        return [dict(row) for row in rows]


def _madr_list(values: list[str] | None) -> str:
    if not values:
        return "*Not supplied in the source decision record.*"
    return "\n".join(f"- {value}" for value in values)


def _option_text(options: list[dict]) -> str:
    if not options:
        return "*No considered options provided in the source record.*"
    return "\n".join(
        f"- **{item.get('name', item.get('title', 'Unnamed option'))}**"
        + (f": {item['description']}" if item.get("description") else "")
        for item in options
    )


def export_madr(db_path: Path, output: Path) -> int:
    """Render MADR 3.0-style records with explicit provenance gaps, never fabricated claims."""
    rows = list_decisions(db_path)
    output.mkdir(parents=True, exist_ok=True)
    for item in rows:
        record = json.loads(item.get("record_json") or "{}")
        title = item["title"].replace("\n", " ").replace("\r", " ")
        header = [f"# {title}", "", f"* Status: {item['status']}", f"* ID: {item['decision_id']}"]
        if record.get("deciders"):
            header.append("* Deciders: " + ", ".join(record["deciders"]))
        if record.get("date"):
            header.append("* Date: " + str(record["date"]))
        if record.get("technical_story"):
            header.append("* Technical Story: " + str(record["technical_story"]))
        options = record.get("considered_options", [])
        detail = []
        for option in options:
            name = option.get("name", option.get("title", "Unnamed option"))
            pro = _madr_list(option.get("pros"))
            con = _madr_list(option.get("cons"))
            detail.append(f"### {name}\n\n**Pros:**\n\n{pro}\n\n**Cons:**\n\n{con}")
        if not detail:
            detail.append("*No option-specific trade-offs supplied in the source record.*")
        document = (
            "\n".join(header)
            + "\n\n## Status\n\n" + item["status"]
            + "\n\n## Context and Problem Statement\n\n" + item["context"]
            + "\n\n## Decision Drivers\n\n" + _madr_list(record.get("decision_drivers"))
            + "\n\n## Considered Options\n\n" + _option_text(options)
            + "\n\n## Decision Outcome\n\n" + item["rationale"]
            + "\n\n### Consequences\n\n" + item["consequences"]
            + "\n\n## Pros and Cons of the Options\n\n" + "\n\n".join(detail)
            + "\n\n## Links\n\n" + _madr_list(record.get("links"))
            + "\n"
        )
        (output / f"{item['decision_id']}.md").write_text(
            document, encoding="utf-8"
        )
    return len(rows)
