"""
4-Pass Deliberation Extraction Pipeline Orchestrator.
Sequences Pass 1 (Discovery) -> Pass 2 (Slicing) -> Pass 3 (Arguments) -> Pass 4 (MADR),
followed by zero-based exact evidence verification.
Strictly adheres to sequential memory offloading (<=8 GB RAM budget) and auditability.
Enforces non-negotiable provenance invariants:
1. Valid EvidenceRole enum values only (no non-existent roles).
2. Unauthorized REJECTED statuses are strictly downgraded to DISPUTED without verified human rulings.
3. Quotes are matched to originating message authors, not quoted replies.
4. Exactly one target ID (decision_id, alternative_id, argument_id) is bound per EvidenceSpan.
5. Missing or invalid source_message_version_id values never receive fabricated synthetic UUIDs.
"""

from typing import Any
from uuid import NAMESPACE_DNS, UUID, uuid4, uuid5

from biscuit.extract.client import LocalLLMClient
from biscuit.extract.pass1_discovery import Pass1ProposalDiscovery
from biscuit.extract.pass2_slicing import Pass2DeliberationSlicing
from biscuit.extract.pass3_arguments import Pass3ArgumentMining, find_originating_message
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


def _parse_valid_uuid(val: Any) -> UUID | None:
    """Parses a valid UUID object or string; returns None if absent or malformed."""
    if isinstance(val, UUID):
        return val
    if isinstance(val, str):
        try:
            return UUID(val)
        except ValueError:
            return None
    return None


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
        dec_uuid = _parse_valid_uuid(dec_id) or uuid5(NAMESPACE_DNS, f"decision:{dec_id}")
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

        # Map proposal IDs to deterministic UUIDs for relational target binding
        proposal_uuid_map: dict[str, UUID] = {
            p.proposal_id: uuid5(NAMESPACE_DNS, f"alt:{thread_id}:{p.proposal_id}")
            for p in proposals
        }

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
        unanchored_warnings: list[str] = []

        # Index messages for exact matching and author lookup
        msg_lookup: list[dict[str, Any]] = []
        maintainer_authors: set[str] = set()

        for idx, m in enumerate(messages):
            mid = str(m.get("message_id") or m.get("id") or f"msg-{idx}")
            content = str(m.get("raw_content") or m.get("content") or "")
            v_id = m.get("source_message_version_id") or m.get("version_id")
            version_uuid = _parse_valid_uuid(v_id)
            is_edited = bool(m.get("is_edited", False))
            author = str(m.get("author", "unknown")).strip()
            role = str(m.get("author_role") or m.get("role") or "contributor").strip().lower()

            if role in (
                "bdfl",
                "maintainer",
                "core maintainer",
                "core_maintainer",
                "working group chair",
            ):
                maintainer_authors.add(author.lower())

            msg_lookup.append(
                {
                    "message_id": mid,
                    "author": author,
                    "author_role": role,
                    "version_uuid": version_uuid,
                    "content": content,
                    "is_edited": is_edited,
                }
            )

        # Process Argument evidence spans
        for arg in arguments:
            if not arg.quote_text:
                continue

            quote = arg.quote_text.strip()

            # Determine valid EvidenceRole (no CONTEXT)
            if arg.is_ruling:
                role = EvidenceRole.RULING
            elif arg.stance == EvidenceStance.OBJECT:
                role = EvidenceRole.OBJECTION
            elif arg.stance == EvidenceStance.SUPPORT:
                role = EvidenceRole.PROPOSAL
            else:
                role = EvidenceRole.PROPOSAL

            # Target binding: exactly one non-null target (Reviewer Issue 4)
            # Rulings bind alternative_id; objections/arguments bind argument_id
            arg_uuid = _parse_valid_uuid(arg.argument_id) or uuid5(
                NAMESPACE_DNS, f"arg:{thread_id}:{arg.argument_id}"
            )
            alt_uuid = proposal_uuid_map.get(arg.proposal_id)

            if role == EvidenceRole.RULING and alt_uuid:
                target_alt_id = alt_uuid
                target_arg_id = None
                target_dec_id = None
            else:
                target_alt_id = None
                target_arg_id = arg_uuid
                target_dec_id = None

            # Locate originating message (Reviewer Issue 4: avoid quoted replies)
            orig_msg_dict = find_originating_message(
                quote=quote,
                messages=msg_lookup,
                claimed_author=arg.author,
            )

            if orig_msg_dict is None:
                continue

            v_uuid = orig_msg_dict["version_uuid"]
            if v_uuid is None:
                # Reviewer Issue 5: Do NOT fabricate synthetic UUIDs for absent version IDs
                unanchored_warnings.append(
                    f"Message {orig_msg_dict['message_id']} lacks a persisted source_message_version_id; "
                    f"cannot create auditable EvidenceSpan."
                )
                continue

            span = self.matcher.create_evidence_span(
                source_message_version_id=v_uuid,
                message_content=orig_msg_dict["content"],
                quote=quote,
                evidence_role=role,
                stance=arg.stance,
                decision_id=target_dec_id,
                alternative_id=target_alt_id,
                argument_id=target_arg_id,
            )
            if span:
                is_valid, checked_span = self.verifier.verify_span(
                    span=span,
                    message_content=orig_msg_dict["content"],
                    is_edited=orig_msg_dict["is_edited"],
                )
                evidence_spans.append(checked_span)
                if (
                    is_valid
                    and checked_span.verification_status == VerificationStatus.VERIFIED_EXACT
                ):
                    verified_spans.append(checked_span)

        # Process MADR evidence quotes
        for eq in madr.evidence_quotes:
            if not isinstance(eq, dict):
                continue
            quote = str(eq.get("quote_text", "")).strip()
            if not quote:
                continue

            raw_role_str = str(eq.get("role", "PROPOSAL")).upper()
            if "RULING" in raw_role_str:
                role = EvidenceRole.RULING
            elif "OBJECTION" in raw_role_str:
                role = EvidenceRole.OBJECTION
            elif "REBUTTAL" in raw_role_str:
                role = EvidenceRole.REBUTTAL
            elif "BENCHMARK" in raw_role_str:
                role = EvidenceRole.BENCHMARK_REPORT
            elif "CONCESSION" in raw_role_str:
                role = EvidenceRole.CONCESSION
            else:
                role = EvidenceRole.PROPOSAL

            claimed_author = eq.get("author")
            orig_msg_dict = find_originating_message(
                quote=quote,
                messages=msg_lookup,
                claimed_author=claimed_author,
            )

            if orig_msg_dict is None:
                continue

            v_uuid = orig_msg_dict["version_uuid"]
            if v_uuid is None:
                unanchored_warnings.append(
                    f"Message {orig_msg_dict['message_id']} lacks a persisted source_message_version_id; "
                    f"cannot create auditable EvidenceSpan."
                )
                continue

            # MADR-level quotes target decision_id
            span = self.matcher.create_evidence_span(
                source_message_version_id=v_uuid,
                message_content=orig_msg_dict["content"],
                quote=quote,
                evidence_role=role,
                stance=EvidenceStance.NEUTRAL,
                decision_id=dec_uuid,
                alternative_id=None,
                argument_id=None,
            )
            if span:
                is_valid, checked_span = self.verifier.verify_span(
                    span=span,
                    message_content=orig_msg_dict["content"],
                    is_edited=orig_msg_dict["is_edited"],
                )
                evidence_spans.append(checked_span)
                if (
                    is_valid
                    and checked_span.verification_status == VerificationStatus.VERIFIED_EXACT
                ):
                    verified_spans.append(checked_span)

        # -------------------------------------------------------------
        # STEP 6: Enforce Ruling Authority & Sign-Off Invariant (Reviewer Issue 2)
        # -------------------------------------------------------------
        # An alternative or decision CANNOT be marked REJECTED without a VERIFIED_EXACT
        # human ruling evidence span tied to the alternative with verified project authority.
        # Core Maintainer consensus requires ruling author + >=1 other distinct maintainer.
        downgraded_rejections: list[str] = []

        verified_ruling_alt_ids: set[UUID] = set()
        for v_span in verified_spans:
            if v_span.evidence_role == EvidenceRole.RULING and v_span.alternative_id:
                # Find author of this span's message
                span_msg = next(
                    (
                        m
                        for m in msg_lookup
                        if m["version_uuid"] == v_span.source_message_version_id
                    ),
                    None,
                )
                if span_msg:
                    author_role = span_msg["author_role"]
                    if author_role == "bdfl":
                        # BDFL ruling is self-authorizing
                        verified_ruling_alt_ids.add(v_span.alternative_id)
                    elif author_role in ("maintainer", "core maintainer", "core_maintainer"):
                        # Core Maintainer consensus requires at least 2 distinct maintainers in discussion
                        if len(maintainer_authors) >= 2:
                            verified_ruling_alt_ids.add(v_span.alternative_id)
                    elif (
                        author_role in ("working group chair", "working_group_chair", "wg_chair")
                        and len(maintainer_authors) >= 2
                    ):
                        verified_ruling_alt_ids.add(v_span.alternative_id)

        # Validate considered_alternatives
        updated_alternatives = []
        for alt in madr.considered_alternatives:
            alt_copy = dict(alt)
            prop_id = str(alt.get("proposal_id", ""))
            alt_uuid = proposal_uuid_map.get(prop_id)
            status = str(alt.get("status", "")).upper()

            if status == "REJECTED" and (not alt_uuid or alt_uuid not in verified_ruling_alt_ids):
                # Downgrade unauthorized rejection to DISPUTED
                alt_copy["status"] = "DISPUTED"
                alt_copy["unauthorized_ruling_note"] = (
                    "Status downgraded from model-authored REJECTED to DISPUTED: "
                    "no VERIFIED_EXACT human ruling from an authorized authority found."
                )
                downgraded_rejections.append(f"Alternative {prop_id}")
            updated_alternatives.append(alt_copy)
        madr.considered_alternatives = updated_alternatives

        # Validate overall decision status
        overall_status = madr.status.upper()
        if overall_status == "REJECTED" and not verified_ruling_alt_ids:
            madr.status = "DISPUTED"
            downgraded_rejections.append("Overall Decision")

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
            "unanchored_warnings": unanchored_warnings,
            "downgraded_rejections": downgraded_rejections,
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
