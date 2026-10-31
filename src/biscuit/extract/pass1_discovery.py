"""
Pass 1: Proposal Discovery module using local Llama 3.2 3B Instruct.
Extracts distinct technical proposals from open-source discussion threads.
"""

import json
import re
from typing import List
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

    def __init__(self, client: LocalLLMClient, model: str = "llama3.2:3b"):
        self.client = client
        self.model = model

    def discover(self, thread_text: str) -> List[ProposalCandidate]:
        """
        Discovers candidate proposals from the given thread text.

        Args:
            thread_text: Formatted text of the discussion thread.

        Returns:
            List[ProposalCandidate]: List of extracted proposal objects.
        """
        prompt = PASS1_PROPOSAL_DISCOVERY_PROMPT.format(thread_text=thread_text)
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
            # Fallback regex extraction if JSON is slightly truncated
            json_candidate = re.search(r"(\{[\s\S]*\})", cleaned)
            if json_candidate:
                try:
                    data = json.loads(json_candidate.group(1))
                except json.JSONDecodeError:
                    return []
            else:
                return []

        proposals_raw = data.get("proposals", [])
        if not isinstance(proposals_raw, list):
            return []

        candidates: List[ProposalCandidate] = []
        for p in proposals_raw:
            if isinstance(p, dict) and "proposal_id" in p and "name" in p:
                candidates.append(
                    ProposalCandidate(
                        proposal_id=str(p.get("proposal_id", "")),
                        name=str(p.get("name", "")),
                        summary=str(p.get("summary", "")),
                        proponent=p.get("proponent"),
                        syntax_or_api=p.get("syntax_or_api"),
                    )
                )

        return candidates
