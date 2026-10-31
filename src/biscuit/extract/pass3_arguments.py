"""
Pass 3: Argument and Stance Mining module using local Phi-3.5-mini-instruct.
Differentiates between individual member objections and authoritative maintainer rulings.
Enforces the invariant that MODEL_INFERRED cannot act as a ruling authority,
and validates all claimed rulings against actual message author project roles.
"""

import json
import re
from typing import Any

from biscuit.extract.client import LocalLLMClient
from biscuit.extract.prompts import PASS3_ARGUMENT_MINING_PROMPT
from biscuit.extract.types import ArgumentClassification, DeliberationPacket
from biscuit.provenance.types import EvidenceStance

# Authorized ruling authorities per docs/STORAGE_AND_RETRIEVAL.md and docs/INGESTION_AND_EXTRACTION.md
AUTHORIZED_RULING_ROLE_MAP = {
    "bdfl": "BDFL",
    "core maintainer": "CORE_MAINTAINER_CONSENSUS",
    "core_maintainer": "CORE_MAINTAINER_CONSENSUS",
    "maintainer": "CORE_MAINTAINER_CONSENSUS",
    "working group chair": "WORKING_GROUP",
    "working_group_chair": "WORKING_GROUP",
    "wg_chair": "WORKING_GROUP",
}


def _clean_json_response(raw_text: str) -> str:
    """Strips markdown code blocks, whitespace, and leading/trailing noise from JSON string."""
    text = raw_text.strip()
    match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    if match:
        return match.group(1).strip()
    return text


def _format_packet_text(packet: DeliberationPacket) -> str:
    """Formats deliberation packet messages into a clean chronological text block."""
    lines = []
    for m in packet.messages:
        mid = m.get("message_id", "unknown")
        author = m.get("author", "unknown")
        role = m.get("author_role") or m.get("role") or "contributor"
        content = m.get("content") or m.get("raw_content") or ""
        lines.append(f"[{mid}] @{author} ({role}):\n{content}\n")
    return "\n".join(lines)


def _has_unquoted_occurrence(content: str, quote: str) -> bool:
    """Checks if `quote` appears anywhere in `content` outside of blockquote lines (lines starting with '>')."""
    if not quote or quote not in content:
        return False
    unquoted_lines = [line for line in content.splitlines() if not line.lstrip().startswith(">")]
    unquoted_text = "\n".join(unquoted_lines)
    return quote in unquoted_text


def find_originating_message(
    quote: str,
    messages: list[dict[str, Any]],
    claimed_author: str | None = None,
    require_unquoted: bool = True,
) -> dict[str, Any] | None:
    """
    Locates the message that originally authored the quote.
    Distinguishes original author text from quoted replies (lines starting with '>').
    If require_unquoted is True, messages where the quote only appears inside
    blockquotes (> ...) are never returned as authored speech.
    Prioritizes original text matches where message author matches claimed_author.
    """
    if not quote or not messages:
        return None

    candidates = []
    for m in messages:
        content = str(m.get("content") or m.get("raw_content") or "")
        if quote not in content:
            continue

        has_unquoted = _has_unquoted_occurrence(content, quote)
        author = (m.get("author") or "").strip().lower()
        claimed = (claimed_author or "").strip().lower()
        matches_author = bool(claimed and author == claimed)

        candidates.append(
            {
                "message": m,
                "has_unquoted": has_unquoted,
                "matches_author": matches_author,
            }
        )

    if not candidates:
        return None

    # Priority 1: author matches AND has unquoted occurrence
    p1 = [c["message"] for c in candidates if c["matches_author"] and c["has_unquoted"]]
    if p1:
        return p1[0]

    # Priority 2: has unquoted occurrence
    p2 = [c["message"] for c in candidates if c["has_unquoted"]]
    if p2:
        return p2[0]

    # If require_unquoted is True, quoted-only matches must NEVER be treated as authored speech
    if require_unquoted:
        return None

    # Priority 3: author matches (in blockquote fallback)
    p3 = [c["message"] for c in candidates if c["matches_author"]]
    if p3:
        return p3[0]

    return candidates[0]["message"]


class Pass3ArgumentMining:
    """
    Pass 3: Evaluates deliberation packets to extract structured arguments and rulings.
    Enforces that MODEL_INFERRED can never act as a ruling authority, and verifies
    actual project roles for any purported ruling.
    Uses Phi-3.5 mini instruct with keep_alive: 0.
    """

    def __init__(self, client: LocalLLMClient, model: str = "phi3.5"):
        self.client = client
        self.model = model

    def mine_arguments(self, packet: DeliberationPacket) -> list[ArgumentClassification]:
        """
        Mines technical arguments and rulings for a specific proposal's deliberation packet.

        Args:
            packet: Targeted deliberation packet for a proposal.

        Returns:
            list[ArgumentClassification]: Extracted and validated arguments.
        """
        packet_text = _format_packet_text(packet)
        prompt = PASS3_ARGUMENT_MINING_PROMPT.format(
            proposal_name=packet.proposal_name,
            proposal_id=packet.proposal_id,
            packet_text=packet_text,
        )

        try:
            raw_response = self.client.generate(
                model=self.model, prompt=prompt, temperature=0.0, keep_alive=0
            )
        finally:
            if not self.client.unload_model(self.model):
                raise RuntimeError(f"Model {self.model} could not be unloaded before the next pass")

        cleaned = _clean_json_response(raw_response)
        if not cleaned:
            return []

        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            json_candidate = re.search(r"(\{[\s\S]*\})", cleaned)
            if json_candidate:
                try:
                    data = json.loads(json_candidate.group(1))
                except json.JSONDecodeError:
                    return []
            else:
                return []

        raw_args = data.get("arguments", [])
        if not isinstance(raw_args, list):
            return []

        results: list[ArgumentClassification] = []
        for idx, arg in enumerate(raw_args):
            if not isinstance(arg, dict):
                continue

            # Parse stance
            raw_stance = str(arg.get("stance", "NEUTRAL")).upper().strip()
            if "OBJECT" in raw_stance or "CON" in raw_stance:
                stance = EvidenceStance.OBJECT
            elif "SUPPORT" in raw_stance or "PRO" in raw_stance:
                stance = EvidenceStance.SUPPORT
            else:
                stance = EvidenceStance.NEUTRAL

            quote_text = str(arg.get("quote_text", "")).strip()
            claimed_author = str(arg.get("author", "unknown")).strip()

            # Cross-check quote against ACTUAL packet messages to prevent false rulings (Reviewer Issue 3 & 4)
            orig_msg = find_originating_message(
                quote=quote_text,
                messages=packet.messages,
                claimed_author=claimed_author,
            )

            if orig_msg is not None:
                actual_author = str(orig_msg.get("author", claimed_author))
                actual_role = (
                    str(orig_msg.get("author_role") or orig_msg.get("role") or "contributor")
                    .strip()
                    .lower()
                )
                mapped_authority = AUTHORIZED_RULING_ROLE_MAP.get(actual_role)
                is_ruling = bool(arg.get("is_ruling", False)) and (mapped_authority is not None)
            else:
                # Source attribution failed or quote was ungrounded/absent from packet.
                # Invariant: An ungrounded quote MUST NEVER inherit the model's authority claim.
                actual_author = claimed_author
                actual_role = "contributor"
                mapped_authority = None
                is_ruling = False

            # Invariant: MODEL_INFERRED can never be a ruling authority
            model_claimed_authority = arg.get("ruling_authority")
            if model_claimed_authority == "MODEL_INFERRED" or not is_ruling:
                is_ruling = False
                mapped_authority = None

            ruling_authority = mapped_authority if is_ruling else None
            fatal_rejection = bool(arg.get("fatal_rejection", False)) and is_ruling

            arg_obj = ArgumentClassification(
                argument_id=str(arg.get("argument_id") or f"ARG-{idx + 1}"),
                proposal_id=str(arg.get("proposal_id") or packet.proposal_id),
                author=actual_author,
                author_role=actual_role,
                stance=stance,
                fatal_rejection=fatal_rejection,
                fatal_reason=arg.get("fatal_reason") if fatal_rejection else None,
                rejection_category=arg.get("rejection_category"),
                is_ruling=is_ruling,
                ruling_authority=ruling_authority,
                quote_text=quote_text,
            )
            results.append(arg_obj)

        return results
