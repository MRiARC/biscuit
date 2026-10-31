"""Small, offline SQLite workspace for CLI ingestion and explicit decision imports.

This is a developer cache, NOT a replacement for the separate PostgreSQL storage stream.
"""

from __future__ import annotations

import json
import re
import sqlite3
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
    return conn


def ingest_discourse(url: str, path: Path) -> int:
    record = _fetch_discourse(url)
    with connect_db(path) as conn:
        conn.execute(
            """INSERT INTO threads (thread_id, source_url, title, raw_json)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(thread_id) DO UPDATE SET
               source_url=excluded.source_url, title=excluded.title,
               raw_json=excluded.raw_json""",
            (record["thread_id"], record["url"], record["title"], json.dumps(record)),
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
        validated.append(
            (
                decision_id,
                str(item["title"]), status, str(item["context"]),
                str(item["rationale"]), str(item["consequences"]),
            )
        )
    with connect_db(db_path) as conn:
        conn.executemany(
            """INSERT INTO decisions VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(decision_id) DO UPDATE SET
               title=excluded.title, status=excluded.status, context=excluded.context,
               rationale=excluded.rationale, consequences=excluded.consequences""",
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


def export_madr(db_path: Path, output: Path) -> int:
    """Export explicitly imported decisions, without falsely attributing inference."""
    rows = list_decisions(db_path)
    output.mkdir(parents=True, exist_ok=True)
    for item in rows:
        title = item["title"].replace("\n", " ").replace("\r", " ")
        doc = (
            f"# {title}\n\n"
            f"- ID: {item['decision_id']}\n"
            f"- Status: {item['status']}\n\n"
            f"## Context\n\n{item['context']}\n\n"
            f"## Decision\n\n{item['rationale']}\n\n"
            f"## Consequences\n\n{item['consequences']}\n"
        )
        (output / f"{item['decision_id']}.md").write_text(doc, encoding="utf-8")
    return len(rows)
