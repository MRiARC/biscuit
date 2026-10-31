"""
4-Pass Deliberation Extraction Pipeline Orchestrator.
Sequences Pass 1 (Discovery) -> Pass 2 (Slicing) -> Pass 3 (Arguments) -> Pass 4 (MADR),
followed by zero-based exact evidence verification.
Strictly adheres to sequential memory offloading (<=8 GB RAM budget) and auditability.
"""

from typing import Any
from uuid import NAMESPACE_DNS, UUID, uuid4, uuid5

from biscuit.extract.client import LocalLLMClient
from biscuit.extract.pass1_discovery import Pass1ProposalDiscovery
from biscuit.extract.pass2_slicing import Pass2DeliberationSlicing
from biscuit.extract.pass3_arguments import Pass3ArgumentMining
from biscuit.extract.pass4_synthesis import Pass4SchemaSynthesis
from biscuit.extract.prompts import get_prompt_templates_hash
from biscuit.extract.types import (
    ArgumentClassification,
    DeliberationPacket,
    ExtractedMADR,
    ExtractionRunResult,
    ProposalCandidate,
)
from biscuit.provenance.matcher import CandidateSpanMatcher
from biscuit.provenance.types import (
    EvidenceRole,
    EvidenceSpan,
    EvidenceStance,
    VerificationStatus,
)
from biscuit.provenance.verifier import EvidenceVerifier


def _to_uuid(val: Any, fallback_seed: str) -> UUID:
    """Converts a value to a UUID, falling back to deterministic uuid5 if not a valid UUID."""
    if isinstance(val, UUID):
        return val
    if isinstance(val, str):
        try:
            return UUID(val)
        except ValueError:
            pass
    return uuid5(NAMESPACE_DNS, fallback_seed)


class ExtractionPipeline:
    """
    Orchestrates the 4-Pass Local LLM Extraction Pipeline.
    Enforces sequential model unloading to adhere to the <=8 GB RAM budget.
    Performs zero-based exact substring evidence verification against source message versions.
    """

    def __init__(
        self,
        client: LocalLLMClient | None = None,
        pass1_model: str = "llama3.2:3b",
        pass3_model: str = "phi3.5",
        pass4_model: str = "qwen2.5-coder:7b",
    ):
        self.client = client or LocalLLMClient()
        self.pass1 = Pass1ProposalDiscovery(self.client, model=pass1_model)
        self.pass2 = Pass2DeliberationSlicing()
        self.pass3 = Pass3ArgumentMining(self.client, model=pass3_model)
        self.pass4 = Pass4SchemaSynthesis(self.client, model=pass4_model)
        self.verifier = EvidenceVerifier()
        self.matcher = CandidateSpanMatcher()

    def run(
        self,
        thread_id: str,
        topic: str,
        messages: list[dict[str, Any]],
        edges: list[dict[str, Any]] | None = None,
        decision_id: str | None = None,
    ) -> ExtractionRunResult:
        """
        Executes the full 4-pass extraction and provenance verification workflow.

        Args:
            thread_id: Unique identifier of the discussion thread.
            topic: Title or subject of the deliberation thread.
            messages: Chronological list of message dicts.
            edges: Optional graph edges (discussion_edges).
            decision_id: Optional decision ID (defaults to DEC-<thread_id>).

        Returns:
            ExtractionRunResult: Complete auditable extraction result.
        """
        run_id = str(uuid4())
        dec_id = decision_id or f"DEC-{thread_id[:8]}"
        dec_uuid = _to_uuid(dec_id, f"decision:{dec_id}")
        prompt_templates_hash = get_prompt_templates_hash()

        # Build chronological thread text for Pass 1
        thread_text_lines = []
        for idx, m in enumerate(messages):
            mid = m.get("message_id") or m.get("id") or f"msg-{idx}"
            author = m.get("author", "unknown")
            role = m.get("author_role") or m.get("role") or "contributor"
            content = m.get("content") or m.get("raw_content") or ""
            thread_text_lines.append(f"[{mid}] @{author} ({role}):\n{content}\n")
        thread_text = "\n".join(thread_text_lines)

        # -------------------------------------------------------------
        # PASS 1: Candidate Proposal Discovery (Llama 3.2 3B)
        # -------------------------------------------------------------
        proposals: list[ProposalCandidate] = self.pass1.discover(thread_text)

        # -------------------------------------------------------------
        # PASS 2: Deliberation Slicing (Deterministic DAG, 0 GB RAM)
        # -------------------------------------------------------------
        packets: list[DeliberationPacket] = self.pass2.slice_deliberation(
            messages=messages, proposals=proposals, edges=edges
        )

        # -------------------------------------------------------------
        # PASS 3: Argument & Stance Mining (Phi-3.5-mini)
        # -------------------------------------------------------------
        arguments: list[ArgumentClassification] = []
        for packet in packets:
            packet_args = self.pass3.mine_arguments(packet)
            arguments.extend(packet_args)

        # -------------------------------------------------------------
        # PASS 4: Schema & MADR Synthesis (Qwen 2.5 Coder 7B)
        # -------------------------------------------------------------
        madr: ExtractedMADR = self.pass4.synthesize_madr(
            topic=topic, decision_id=dec_id, proposals=proposals, arguments=arguments
        )

        # -------------------------------------------------------------
        # STEP 5: Exact Evidence Verification & Span Matching
        # -------------------------------------------------------------
        evidence_spans: list[EvidenceSpan] = []
        verified_spans: list[EvidenceSpan] = []

        # Index messages for exact substring matching
        msg_lookup: list[dict[str, Any]] = []
        for idx, m in enumerate(messages):
            mid = m.get("message_id") or m.get("id") or f"msg-{idx}"
            content = m.get("raw_content") or m.get("content") or ""
            v_id = m.get("source_message_version_id") or m.get("version_id")
            v_uuid = _to_uuid(v_id, f"version:{thread_id}:{mid}")
            is_edited = bool(m.get("is_edited", False))
            msg_lookup.append(
                {
                    "message_id": mid,
                    "version_uuid": v_uuid,
                    "content": content,
                    "is_edited": is_edited,
                }
            )

        # Collect candidate quotes from Pass 3 arguments
        for arg in arguments:
            if not arg.quote_text:
                continue

            quote = arg.quote_text.strip()
            role = (
                EvidenceRole.OBJECTION
                if arg.stance == EvidenceStance.OBJECT
                else (EvidenceRole.RULING if arg.is_ruling else EvidenceRole.CONTEXT)
            )

            # Search in raw messages for exact match
            for msg_info in msg_lookup:
                span = self.matcher.create_evidence_span(
                    source_message_version_id=msg_info["version_uuid"],
                    message_content=msg_info["content"],
                    quote=quote,
                    evidence_role=role,
                    stance=arg.stance,
                    decision_id=dec_uuid,
                )
                if span:
                    # Run exact verification
                    is_valid, checked_span = self.verifier.verify_span(
                        span=span,
                        message_content=msg_info["content"],
                        is_edited=msg_info["is_edited"],
                    )
                    evidence_spans.append(checked_span)
                    if (
                        is_valid
                        and checked_span.verification_status == VerificationStatus.VERIFIED_EXACT
                    ):
                        verified_spans.append(checked_span)
                    break

        # Collect candidate quotes from Pass 4 MADR evidence_quotes
        for eq in madr.evidence_quotes:
            if not isinstance(eq, dict):
                continue
            quote = str(eq.get("quote_text", "")).strip()
            if not quote:
                continue

            role_str = str(eq.get("role", "CONTEXT")).upper()
            if "RULING" in role_str:
                role = EvidenceRole.RULING
            elif "OBJECTION" in role_str:
                role = EvidenceRole.OBJECTION
            elif "PROPOSAL" in role_str:
                role = EvidenceRole.PROPOSAL
            else:
                role = EvidenceRole.CONTEXT

            for msg_info in msg_lookup:
                span = self.matcher.create_evidence_span(
                    source_message_version_id=msg_info["version_uuid"],
                    message_content=msg_info["content"],
                    quote=quote,
                    evidence_role=role,
                    stance=EvidenceStance.NEUTRAL,
                    decision_id=dec_uuid,
                )
                if span:
                    is_valid, checked_span = self.verifier.verify_span(
                        span=span,
                        message_content=msg_info["content"],
                        is_edited=msg_info["is_edited"],
                    )
                    evidence_spans.append(checked_span)
                    if (
                        is_valid
                        and checked_span.verification_status == VerificationStatus.VERIFIED_EXACT
                    ):
                        verified_spans.append(checked_span)
                    break

        audit_metadata = {
            "pipeline_version": "1.0.0",
            "schema_version": "MADR-3.0",
            "pass1_model": self.pass1.model,
            "pass3_model": self.pass3.model,
            "pass4_model": self.pass4.model,
            "total_messages": len(messages),
            "total_proposals": len(proposals),
            "total_deliberation_packets": len(packets),
            "total_arguments": len(arguments),
            "evidence_spans_count": len(evidence_spans),
            "verified_spans_count": len(verified_spans),
        }

        return ExtractionRunResult(
            run_id=run_id,
            thread_id=thread_id,
            proposals=proposals,
            deliberation_packets=packets,
            arguments=arguments,
            madr=madr,
            evidence_spans=evidence_spans,
            verified_spans=verified_spans,
            prompt_templates_hash=prompt_templates_hash,
            run_status="SUCCESS",
            audit_metadata=audit_metadata,
        )
