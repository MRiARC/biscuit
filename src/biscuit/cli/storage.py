"""Small, offline SQLite workspace for CLI ingestion and explicit decision imports.

This is a developer cache, NOT a replacement for the separate PostgreSQL storage stream.
"""

from __future__ import annotations

import json
import re
import sqlite3
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse
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


def _fetch_discourse(url: str) -> dict:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Discourse URLs must be HTTPS and must not contain credentials")
    if parsed.hostname in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("Localhost is not a valid Discourse ingestion host")
    if not re.fullmatch(r"/t/[^/?#]+/\d+(?:/\d+)?/?", parsed.path):
        raise ValueError("Expected Discourse topic URL in /t/slug/topic-id form")
    topic_url = f"https://{parsed.netloc}{parsed.path.rstrip('/')}.json"
    request = Request(topic_url, headers={"User-Agent": "Biscuit/0.1 (read-only)"})
    with urlopen(request, timeout=20) as response:
        if response.url != topic_url:
            raise ValueError("Unexpected redirect while ingesting Discourse topic")
        body = response.read(8_000_001)
        if len(body) > 8_000_000:
            raise ValueError("Discourse response too large")
    payload = json.loads(body)
    stream = payload.get("post_stream", {})
    posts = stream.get("posts", [])
    if len(stream.get("stream", [])) > len(posts):
        raise ValueError("Partial Discourse post stream; use a complete export")
    if not posts:
        raise ValueError("Discourse topic contained no posts")
    return {
        "thread_id": str(payload.get("id")),
        "url": url,
        "title": str(payload.get("title", "")),
        "messages": [
            {
                "message_id": str(post["id"]),
                "author": str(post.get("username", "")),
                "created_at": str(post.get("created_at", "")),
                "raw_text": str(post.get("raw", "") or _plain_html(post.get("cooked", ""))),
            }
            for post in posts
        ],
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
        raise ValueError("Expected a JSON list of decisions")
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
