"""
Pass 4: Schema and MADR Synthesis module using local Qwen 2.5 Coder 7B Instruct.
Synthesizes structured MADR 3.0 records and extracts candidate verbatim evidence quotes.
"""

import json
import re
from typing import Any

from biscuit.extract.client import LocalLLMClient
from biscuit.extract.prompts import PASS4_SCHEMA_SYNTHESIS_PROMPT
from biscuit.extract.types import ArgumentClassification, ExtractedMADR, ProposalCandidate


def _clean_json_response(raw_text: str) -> str:
    """Strips markdown code blocks, whitespace, and leading/trailing noise from JSON string."""
    text = raw_text.strip()
    match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    if match:
        return match.group(1).strip()
    return text


class Pass4SchemaSynthesis:
    """
    Pass 4: Synthesizes complete Markdown Architectural Decision Record (MADR 3.0)
    data structures and verbatim evidence quotes.
    Uses Qwen 2.5 Coder 7B Instruct with keep_alive: 0.
    """

    def __init__(self, client: LocalLLMClient, model: str = "qwen2.5-coder:7b"):
        self.client = client
        self.model = model

    def synthesize_madr(
        self,
        topic: str,
        decision_id: str,
        proposals: list[ProposalCandidate],
        arguments: list[ArgumentClassification],
    ) -> ExtractedMADR:
        """
        Synthesizes formal MADR 3.0 output from the extracted proposals and arguments.

        Args:
            topic: Thread title or architectural topic.
            decision_id: Unique identifier for the decision record.
            proposals: List of candidate proposals from Pass 1.
            arguments: List of classified arguments and rulings from Pass 3.

        Returns:
            ExtractedMADR: Synthesized MADR record.
        """
        proposals_json = json.dumps([p.model_dump() for p in proposals], indent=2)
        arguments_json = json.dumps([a.model_dump() for a in arguments], indent=2)

        prompt = PASS4_SCHEMA_SYNTHESIS_PROMPT.format(
            topic=topic,
            decision_id=decision_id,
            proposals_json=proposals_json,
            arguments_json=arguments_json,
        )

        try:
            raw_response = self.client.generate(
                model=self.model, prompt=prompt, temperature=0.0, keep_alive=0
            )
        finally:
            self.client.unload_model(self.model)

        cleaned = _clean_json_response(raw_response)
        warnings: list[str] = []
        data: dict[str, Any] = {}

        if cleaned:
            try:
                parsed: Any = json.loads(cleaned)
            except json.JSONDecodeError:
                match = re.search(r"(\{[\s\S]*\})", cleaned)
                try:
                    parsed = json.loads(match.group(1)) if match else None
                except json.JSONDecodeError:
                    parsed = None
            if isinstance(parsed, dict):
                data = parsed
            else:
                warnings.append("Pass 4 response is not a valid JSON object.")

        status = data.get("status", "UNRESOLVED")
        if status not in ("ACCEPTED", "REJECTED", "DISPUTED", "UNRESOLVED"):
            warnings.append("Invalid MADR status; defaulted to UNRESOLVED.")
            status = "UNRESOLVED"

        fallback_alternatives = [
            {
                "proposal_id": p.proposal_id,
                "name": p.name,
                "status": "DISPUTED",
                "summary": p.summary,
            }
            for p in proposals
        ]
        raw_alternatives = data.get("considered_alternatives")
        if raw_alternatives is None:
            considered_alternatives = fallback_alternatives
        elif isinstance(raw_alternatives, list) and all(
            isinstance(item, dict) for item in raw_alternatives
        ):
            considered_alternatives = raw_alternatives
        else:
            warnings.append("Malformed considered_alternatives; discarded untrusted model values.")
            considered_alternatives = fallback_alternatives

        raw_quotes = data.get("evidence_quotes")
        if raw_quotes is None:
            evidence_quotes: list[dict[str, Any]] = []
        elif isinstance(raw_quotes, list) and all(isinstance(q, dict) for q in raw_quotes):
            evidence_quotes = raw_quotes
        else:
            warnings.append("Malformed evidence_quotes; discarded untrusted model values.")
            evidence_quotes = []

        raw_outcome = data.get("decision_outcome")
        if raw_outcome is None or isinstance(raw_outcome, dict):
            decision_outcome = raw_outcome
        else:
            warnings.append("Malformed decision_outcome; discarded untrusted model value.")
            decision_outcome = None

        # Invalid model structure must fail closed, including a model-proposed REJECTED status.
        if warnings:
            status = "UNRESOLVED"
            for alternative in considered_alternatives:
                if str(alternative.get("status", "")).upper() == "REJECTED":
                    alternative["status"] = "DISPUTED"

        return ExtractedMADR(
            decision_id=str(data.get("decision_id") or decision_id),
            title=str(data.get("title") or topic),
            status=status,
            context_and_problem=str(data.get("context_and_problem") or f"Deliberation on {topic}"),
            considered_alternatives=considered_alternatives,
            decision_outcome=decision_outcome,
            evidence_quotes=evidence_quotes,
            validation_warnings=warnings,
        )
