"""
Pass 1: Proposal Discovery module using local Llama 3.2 3B Instruct.
Extracts distinct technical proposals from open-source discussion threads.
"""

import json
import re

from biscuit.extract.client import LocalLLMClient
from biscuit.extract.prompts import PASS1_PROPOSAL_DISCOVERY_PROMPT
from biscuit.extract.types import ProposalCandidate


def _clean_json_response(raw_text: str) -> str:
    """Strips markdown code blocks, whitespace, and leading/trailing noise from JSON string."""
    text = raw_text.strip()
    # Match markdown code block ```json ... ``` or ``` ... ```
    match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    if match:
        return match.group(1).strip()
    return text


class Pass1ProposalDiscovery:
    """
    Pass 1: Extracts candidate technical proposals from raw discussion thread text.
    Uses Llama 3.2 3B Instruct with keep_alive: 0 for strict sequential memory budget.
    """

    def __init__(
        self, client: LocalLLMClient, model: str = "llama3.2:3b",
        chunk_word_limit: int = 1400,
    ):
        if chunk_word_limit < 1:
            raise ValueError("chunk_word_limit must be positive")
        self.client = client
        self.model = model
        self.chunk_word_limit = chunk_word_limit

    def _chunks(self, thread_text: str) -> list[str]:
        """Pack whole messages where possible; split oversized messages by words."""
        if not thread_text.strip():
            return []
        parts = re.split(r"(?=^\[\S+\] @)", thread_text, flags=re.MULTILINE)
        blocks = [part for part in parts if part.strip()]
        chunks: list[str] = []
        current: list[str] = []
        count = 0
        for block in blocks:
            words = block.split()
            for offset in range(0, len(words), self.chunk_word_limit):
                segment_words = words[offset:offset + self.chunk_word_limit]
                segment = " ".join(segment_words)
                if count + len(segment_words) > self.chunk_word_limit and current:
                    chunks.append("\n".join(current))
                    current, count = [], 0
                current.append(segment)
                count += len(segment_words)
        if current:
            chunks.append("\n".join(current))
        return chunks

    def discover(self, thread_text: str) -> list[ProposalCandidate]:
        """
        Discovers candidate proposals from the given thread text.

        Args:
            thread_text: Formatted text of the discussion thread.

        Returns:
            List[ProposalCandidate]: List of extracted proposal objects.
        """
        chunks = self._chunks(thread_text)
        if not chunks:
            return []

        candidates: list[ProposalCandidate] = []
        seen: set[tuple[str, str]] = set()
        for chunk in chunks:
            prompt = PASS1_PROPOSAL_DISCOVERY_PROMPT.format(thread_text=chunk)
            try:
                raw_response = self.client.generate(
                    model=self.model, prompt=prompt, temperature=0.0, keep_alive=0
                )
            finally:
                if not self.client.unload_model(self.model):
                    raise RuntimeError(f"Model {self.model} could not be unloaded after Pass 1")

            cleaned = _clean_json_response(raw_response)
            if not cleaned:
                continue
            try:
                data = json.loads(cleaned)
            except json.JSONDecodeError:
                match = re.search(r"(\{[\s\S]*\})", cleaned)
                try:
                    data = json.loads(match.group(1)) if match else {}
                except json.JSONDecodeError:
                    data = {}
            if not isinstance(data, dict):
                continue
            proposals_raw = data.get("proposals", [])
            if not isinstance(proposals_raw, list):
                continue

            for proposal in proposals_raw:
                if not isinstance(proposal, dict) or not all(
                    proposal.get(field) for field in ("proposal_id", "name")
                ):
                    continue
                name = str(proposal["name"]).strip()
                syntax = str(proposal.get("syntax_or_api") or "").strip()
                # IDs generated per chunk are not globally unique. Names and APIs
                # identify a recurring candidate across adjacent chunks.
                key = (name.casefold(), syntax.casefold())
                if key in seen:
                    continue
                seen.add(key)
                candidates.append(
                    ProposalCandidate(
                        proposal_id=(
                            str(proposal["proposal_id"]) if len(chunks) == 1
                            else f"P{len(candidates) + 1}"
                        ),
                        name=name,
                        summary=str(proposal.get("summary") or ""),
                        proponent=proposal.get("proponent"),
                        syntax_or_api=proposal.get("syntax_or_api"),
                    )
                )

        return candidates
