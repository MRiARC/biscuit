"""
Pass 3: Argument and Stance Mining module using local Phi-3.5-mini-instruct.
Differentiates between individual member objections and authoritative maintainer rulings.
Enforces the invariant that MODEL_INFERRED cannot act as a ruling authority.
"""

import json
import re

from biscuit.extract.client import LocalLLMClient
from biscuit.extract.prompts import PASS3_ARGUMENT_MINING_PROMPT
from biscuit.extract.types import ArgumentClassification, DeliberationPacket
from biscuit.provenance.types import EvidenceStance


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


class Pass3ArgumentMining:
    """
    Pass 3: Evaluates deliberation packets to extract structured arguments and rulings.
    Enforces the non-negotiable invariant: MODEL_INFERRED can never act as a ruling authority.
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
            List[ArgumentClassification]: Extracted arguments.
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
            self.client.unload_model(self.model)

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

            # Parse and validate stance
            raw_stance = str(arg.get("stance", "NEUTRAL")).upper().strip()
            if "OBJECT" in raw_stance or "CON" in raw_stance:
                stance = EvidenceStance.OBJECT
            elif "SUPPORT" in raw_stance or "PRO" in raw_stance:
                stance = EvidenceStance.SUPPORT
            else:
                stance = EvidenceStance.NEUTRAL

            # INVARIANT ENFORCEMENT:
            # If the model tried to claim 'MODEL_INFERRED' as a ruling authority, strip it
            is_ruling = bool(arg.get("is_ruling", False))
            ruling_auth = arg.get("ruling_authority")
            if ruling_auth == "MODEL_INFERRED":
                # Model cannot be a ruling authority. Downgrade to member objection
                ruling_auth = None
                is_ruling = False

            # If marked is_ruling but no valid authority specified, downgrade is_ruling
            if is_ruling and not ruling_auth:
                is_ruling = False

            fatal_rejection = bool(arg.get("fatal_rejection", False))
            if fatal_rejection and not is_ruling:
                # Individual contributor objection cannot be an authoritative fatal rejection ruling
                # per docs/INGESTION_AND_EXTRACTION.md Section 6 Pass 3
                fatal_rejection = False

            quote_text = str(arg.get("quote_text", "")).strip()

            arg_obj = ArgumentClassification(
                argument_id=str(arg.get("argument_id") or f"ARG-{idx + 1}"),
                proposal_id=str(arg.get("proposal_id") or packet.proposal_id),
                author=str(arg.get("author", "unknown")),
                author_role=str(arg.get("author_role", "contributor")),
                stance=stance,
                fatal_rejection=fatal_rejection,
                fatal_reason=arg.get("fatal_reason"),
                rejection_category=arg.get("rejection_category"),
                is_ruling=is_ruling,
                ruling_authority=ruling_auth,
                quote_text=quote_text,
            )
            results.append(arg_obj)

        return results
