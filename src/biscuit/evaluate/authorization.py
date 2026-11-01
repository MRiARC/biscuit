"""Fail-closed scorer-side authorization of real watchdog warnings.

Trusted proofs and project-role grants MUST be obtained from independently
verified ingestion/storage, never from the predictor, model, or PR diff. A
model-supplied authorization flag is only a claim until this check succeeds.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from typing import Any

_MAINTAINER = frozenset({"maintainer", "core_maintainer", "core maintainer"})
_CHAIR = frozenset({"working_group_chair", "working group chair", "wg_chair"})
_MEMBER = frozenset({"working_group_member", "working group member", "wg_member"})


def _authenticated_proof(
    proof: Mapping[str, Any], alternative_id: str,
    trusted_roles: Mapping[str, str],
) -> tuple[str, str, str] | None:
    """Verify human annotation, immutable source bytes and exact quote offsets."""
    author = proof.get("author_handle")
    role = trusted_roles.get(author.lower(), "") if isinstance(author, str) else ""
    text, quote_hash = proof.get("raw_text"), proof.get("quote_sha256")
    source_hash = proof.get("source_content_sha256")
    start, end = proof.get("start_char"), proof.get("end_char")
    if (
        not role or not isinstance(text, str) or not isinstance(quote_hash, str)
        or not isinstance(source_hash, str)
        or not isinstance(start, int) or isinstance(start, bool)
        or not isinstance(end, int) or isinstance(end, bool)
        or not 0 <= start < end <= len(text)
        or proof.get("review_status") != "HUMAN_VERIFIED"
        or proof.get("is_edited") is not False
        or proof.get("alternative_id") != alternative_id
        or proof.get("evidence_role") not in {"RULING", "SIGNOFF"}
        or not isinstance(proof.get("source_message_version_id"), str)
        or not proof["source_message_version_id"].strip()
        or not isinstance(proof.get("source_message_id"), str)
        or not proof["source_message_id"].strip()
    ):
        return None
    if hashlib.sha256(text.encode("utf-8")).hexdigest() != source_hash:
        return None
    if hashlib.sha256(text[start:end].encode("utf-8")).hexdigest() != quote_hash:
        return None
    # Textually bounded proof avoids accepting an empty or whitespace-only quote.
    if not text[start:end].strip():
        return None
    return author.lower(), role.strip().lower(), proof["evidence_role"]


def verify_production_warning(
    warning: Mapping[str, Any],
    *,
    trusted_proofs: Mapping[str, Sequence[Mapping[str, Any]]] | None,
    verified_project_roles: Mapping[str, str] | None,
) -> bool:
    """Require independently authenticated ruling plus project-specific authority.

    For the claimed rejected alternative, a verified BDFL ruling suffices.
    A maintainer ruling needs a second distinct maintainer sign-off; a
    working-group chair needs a distinct WG member or maintainer sign-off.
    All proof records must be bound by human review to the *same* alternative.
    """
    if (
        warning.get("authorization") != "VERIFIED_HUMAN_RULING"
        or not trusted_proofs or not verified_project_roles
    ):
        return False
    alt = warning.get("alternative_id")
    if not isinstance(alt, str) or not alt:
        return False
    claimed_message_id = warning.get("ruling_source_message_id")
    claimed_version = warning.get("ruling_source_message_version_id")
    claimed_digest = warning.get("ruling_quote_sha256")
    if not (
        isinstance(claimed_message_id, str) and claimed_message_id
        and isinstance(claimed_version, str) and claimed_version
        and isinstance(claimed_digest, str) and re.fullmatch(r"[a-f0-9]{64}", claimed_digest)
    ):
        return False

    roles = {
        author.lower(): role for author, role in verified_project_roles.items()
        if isinstance(author, str) and isinstance(role, str)
    }
    ruling_authors: list[tuple[str, str]] = []
    signoffs: set[tuple[str, str]] = set()
    for proof in trusted_proofs.get(alt, ()):
        if not isinstance(proof, Mapping):
            return False
        verified = _authenticated_proof(proof, alt, roles)
        if verified is None:
            continue
        author, role, evidence_role = verified
        if evidence_role == "SIGNOFF":
            signoffs.add((author, role))
        elif (
            proof["source_message_id"] == claimed_message_id
            and proof["source_message_version_id"] == claimed_version
            and proof["quote_sha256"] == claimed_digest
        ):
            ruling_authors.append((author, role))
    for author, role in ruling_authors:
        if role == "bdfl":
            return True
        if role in _MAINTAINER and any(
            signer != author and other_role in _MAINTAINER
            for signer, other_role in signoffs
        ):
            return True
        if role in _CHAIR and any(
            signer != author and (other_role in _MEMBER or other_role in _MAINTAINER)
            for signer, other_role in signoffs
        ):
            return True
    return False
