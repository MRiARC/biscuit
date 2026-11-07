"""Cryptographic hashing for raw message content and evidence quotes."""

import hashlib


def compute_sha256(text: str) -> str:
    """Compute lowercase SHA-256 hex digest over exact UTF-8 bytes of string.

    CRLF/LF line endings and Unicode characters are preserved exactly.
    """
    if text is None:
        raise ValueError("Cannot compute hash of None")
    return hashlib.sha256(text.encode("utf-8")).hexdigest().lower()


def verify_sha256(text: str, expected_hash: str) -> bool:
    """Verify that text hashes to expected SHA-256 hex digest (case-insensitive)."""
    if text is None or expected_hash is None:
        return False
    return compute_sha256(text) == expected_hash.strip().lower()
