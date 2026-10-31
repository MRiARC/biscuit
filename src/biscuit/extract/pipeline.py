"""
4-Pass Deliberation Extraction Pipeline Orchestrator.
Sequences Pass 1 (Discovery) -> Pass 2 (Slicing) -> Pass 3 (Arguments) -> Pass 4 (MADR),
followed by zero-based exact evidence verification.
Strictly adheres to sequential memory offloading (<=8 GB RAM budget) and auditability.
Enforces non-negotiable provenance and authority invariants:
1. Valid EvidenceRole enum values only (no non-existent roles).
2. Unauthorized REJECTED statuses are strictly downgraded to DISPUTED without verified human rulings.
3. Quotes are matched to originating message authors, not quoted replies.
4. Exactly one target ID (decision_id, alternative_id, argument_id) is bound per EvidenceSpan.
5. Missing or invalid source_message_version_id values never receive fabricated synthetic UUIDs.
6. Target alternative_id / argument_id values are never fabricated via synthetic UUIDs without caller grants.
7. Rulings require independent source-to-alternative verification before authorizing alternative rejection.
8. Core Maintainer consensus rejections require the ruling author PLUS at least one second distinct
   maintainer sign-off on that same alternative.
"""

import re
from typing import Any
from uuid import UUID, uuid4

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


def _verify_ruling_alternative_link(
    quote: str,
    message_content: str,
    target_proposal_id: str,
    proposals: list[ProposalCandidate],
) -> bool:
    """
    Independently verifies that a ruling quote or its originating message actually
    addresses target_proposal_id, rather than blindly trusting the LLM's classification.
    Abstains (returns False) if the quote explicitly references a different proposal,
    or if there is no verified textual link to the target proposal in multi-proposal discussions.
    """
    quote_lower = quote.lower()
    msg_lower = message_content.lower()
    target_id_lower = target_proposal_id.lower().strip()

    target_prop = next(
        (p for p in proposals if p.proposal_id.lower().strip() == target_id_lower),
        None,
    )
    other_props = [p for p in proposals if p.proposal_id.lower().strip() != target_id_lower]

    # Check 1: Does the quote explicitly reference a DIFFERENT proposal?
    for other in other_props:
        other_id = other.proposal_id.lower().strip()
        other_num = re.sub(r"[^\d]", "", other_id)

        other_patterns = [rf"\b{re.escape(other_id)}\b"]
        if other_num:
            other_patterns.extend(
                [
                    rf"\bproposal\s*{re.escape(other_num)}\b",
                    rf"\balternative\s*{re.escape(other_num)}\b",
                    rf"\boption\s*{re.escape(other_num)}\b",
                ]
            )
        if other.name:
            other_patterns.append(rf"\b{re.escape(other.name.lower())}\b")

        for pattern in other_patterns:
            if re.search(pattern, quote_lower):
                # Quote references another proposal!
                # Does the quote ALSO reference the target proposal?
                target_num = re.sub(r"[^\d]", "", target_id_lower)
                target_in_quote = (
                    bool(re.search(rf"\b{re.escape(target_id_lower)}\b", quote_lower))
                    or bool(
                        target_num
                        and re.search(rf"\bproposal\s*{re.escape(target_num)}\b", quote_lower)
                    )
                    or bool(
                        target_prop and target_prop.name and target_prop.name.lower() in quote_lower
                    )
                )
                if not target_in_quote:
                    # Explicit mismatch: quote targets another proposal
                    return False

        if (
            other.syntax_or_api
            and len(other.syntax_or_api) >= 3
            and other.syntax_or_api.lower() in quote_lower
            and not (
                target_prop
                and target_prop.syntax_or_api
                and target_prop.syntax_or_api.lower() in quote_lower
            )
        ):
            return False

    # Check 2: If there's only 1 proposal in the deliberation thread, any ruling in the thread
    # addresses that proposal unless it referenced another proposal
    if len(proposals) == 1 and target_prop:
        return True

    # Check 3: For multi-proposal threads, verify positive textual link in quote or message
    target_num = re.sub(r"[^\d]", "", target_id_lower)
    target_patterns = [rf"\b{re.escape(target_id_lower)}\b"]
    if target_num:
        target_patterns.extend(
            [
                rf"\bproposal\s*{re.escape(target_num)}\b",
                rf"\balternative\s*{re.escape(target_num)}\b",
                rf"\boption\s*{re.escape(target_num)}\b",
            ]
        )
    if target_prop and target_prop.name:
        target_patterns.append(rf"\b{re.escape(target_prop.name.lower())}\b")

    quote_matches = any(bool(re.search(pat, quote_lower)) for pat in target_patterns)
    if quote_matches:
        return True

    if (
        target_prop
        and target_prop.syntax_or_api
        and len(target_prop.syntax_or_api) >= 2
        and target_prop.syntax_or_api.lower() in quote_lower
    ):
        return True

    msg_matches = any(bool(re.search(pat, msg_lower)) for pat in target_patterns)
    if msg_matches:
        return True

    return bool(
        target_prop
        and target_prop.syntax_or_api
        and len(target_prop.syntax_or_api) >= 2
        and target_prop.syntax_or_api.lower() in msg_lower
    )


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
        alternative_ids: dict[str, UUID | str] | None = None,
        argument_ids: dict[str, UUID | str] | None = None,
        verified_project_roles: dict[str, str] | None = None,
    ) -> ExtractionRunResult:
        """
        Executes the full 4-pass extraction and provenance verification workflow.

        Args:
            thread_id: Unique identifier of the discussion thread.
            topic: Title or subject of the deliberation thread.
            messages: Chronological list of message dicts.
            edges: Optional graph edges (discussion_edges).
            decision_id: Optional decision ID string or UUID.
            alternative_ids: Optional caller-provided persisted DB UUIDs for proposal alternatives.
            argument_ids: Optional caller-provided persisted DB UUIDs for arguments.
            verified_project_roles: Optional caller-provided project-scoped role grants (username -> role).

        Returns:
            ExtractionRunResult: Complete auditable extraction result.
        """
        run_id = str(uuid4())
        dec_id = decision_id or f"DEC-{thread_id[:8]}"
        dec_uuid = _parse_valid_uuid(dec_id)
        prompt_templates_hash = get_prompt_templates_hash()

        # Parse caller-provided database UUID mappings (Reviewer Issue 5)
        persisted_alt_map: dict[str, UUID] = {}
        if alternative_ids:
            for k, v in alternative_ids.items():
                parsed = _parse_valid_uuid(v)
                if parsed:
                    persisted_alt_map[k] = parsed

        persisted_arg_map: dict[str, UUID] = {}
        if argument_ids:
            for k, v in argument_ids.items():
                parsed = _parse_valid_uuid(v)
                if parsed:
                    persisted_arg_map[k] = parsed

        unpersisted_candidate_targets: list[str] = []

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
        unanchored_warnings: list[str] = []

        # Index messages for exact matching and author lookup
        msg_lookup: list[dict[str, Any]] = []

        for idx, m in enumerate(messages):
            mid = str(m.get("message_id") or m.get("id") or f"msg-{idx}")
            content = str(m.get("raw_content") or m.get("content") or "")
            v_id = m.get("source_message_version_id") or m.get("version_id")
            version_uuid = _parse_valid_uuid(v_id)
            is_edited = bool(m.get("is_edited", False))
            author = str(m.get("author", "unknown")).strip()
            raw_role = str(m.get("author_role") or m.get("role") or "contributor").strip().lower()

            if verified_project_roles is not None:
                # Caller-verified role grants take precedence over untrusted message labels
                author_role = (
                    verified_project_roles.get(author.lower(), "contributor").strip().lower()
                )
            else:
                author_role = raw_role

            msg_lookup.append(
                {
                    "message_id": mid,
                    "author": author,
                    "author_role": author_role,
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

            # Locate originating message (Reviewer Issue 1 & 4: avoid quoted replies)
            orig_msg_dict = find_originating_message(
                quote=quote,
                messages=msg_lookup,
                claimed_author=arg.author,
                require_unquoted=True,
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

            # Reviewer Issue 4: Ruling-to-alternative association verification
            # Verify independently that the quote / message actually addresses arg.proposal_id
            is_valid_alt_link = _verify_ruling_alternative_link(
                quote=quote,
                message_content=orig_msg_dict["content"],
                target_proposal_id=arg.proposal_id,
                proposals=proposals,
            )

            # Target binding: exactly one non-null target
            # Rulings bind alternative_id ONLY if the linkage to the alternative is independently verified
            # and caller provided a persisted alternative UUID (Reviewer Issue 4 & 5).
            if role == EvidenceRole.RULING and is_valid_alt_link:
                target_alt_id = persisted_alt_map.get(arg.proposal_id)
                target_arg_id = None
                target_dec_id = None
                if target_alt_id is None:
                    unpersisted_candidate_targets.append(
                        f"Ruling on proposal '{arg.proposal_id}' has no caller-provided database UUID; "
                        f"alternative_id left unbound."
                    )
            elif role == EvidenceRole.RULING and not is_valid_alt_link:
                # Mismatched or ungrounded proposal linkage - do NOT bind to alternative
                unanchored_warnings.append(
                    f"Ruling quote '{quote[:40]}...' was assigned to proposal '{arg.proposal_id}' by model, "
                    f"but verified text references another proposal or lacks verified linkage. Ruling detached."
                )
                target_alt_id = None
                target_arg_id = persisted_arg_map.get(arg.argument_id) or _parse_valid_uuid(
                    arg.argument_id
                )
                target_dec_id = None
                if target_arg_id is None:
                    unpersisted_candidate_targets.append(
                        f"Argument candidate '{arg.argument_id}' has no caller-provided database UUID; "
                        f"argument_id left unbound."
                    )
            else:
                target_alt_id = None
                target_arg_id = persisted_arg_map.get(arg.argument_id) or _parse_valid_uuid(
                    arg.argument_id
                )
                target_dec_id = None
                if target_arg_id is None:
                    unpersisted_candidate_targets.append(
                        f"Argument candidate '{arg.argument_id}' has no caller-provided database UUID; "
                        f"argument_id left unbound."
                    )

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
                require_unquoted=True,
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

            # MADR-level quotes target decision_id (if a valid UUID was provided by caller)
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
        # STEP 6: Enforce Ruling Authority & Sign-Off Invariants (Reviewer Issues 3 & 4)
        # -------------------------------------------------------------
        # An alternative or decision CANNOT be marked REJECTED without:
        # 1. A VERIFIED_EXACT human ruling evidence span independently verified to link to that proposal.
        # 2. Verified project-scoped authority:
        #    - BDFL: self-authorizing on that alternative.
        #    - Core Maintainer consensus: ruling author + >=1 distinct verified maintainer sign-off on that same alternative.
        #    - Working Group: WG Chair + >=1 distinct verified WG member/maintainer sign-off on that same alternative.
        downgraded_rejections: list[str] = []

        bdfl_rulings_by_prop: dict[str, set[str]] = {p.proposal_id: set() for p in proposals}
        maintainer_rulings_by_prop: dict[str, set[str]] = {p.proposal_id: set() for p in proposals}
        wg_chair_rulings_by_prop: dict[str, set[str]] = {p.proposal_id: set() for p in proposals}
        maintainer_signoffs_by_prop: dict[str, set[str]] = {p.proposal_id: set() for p in proposals}
        wg_member_signoffs_by_prop: dict[str, set[str]] = {p.proposal_id: set() for p in proposals}

        # Collect rulings from verified_spans that link to each proposal
        for v_span in verified_spans:
            if v_span.evidence_role != EvidenceRole.RULING:
                continue

            span_msg = next(
                (m for m in msg_lookup if m["version_uuid"] == v_span.source_message_version_id),
                None,
            )
            if not span_msg:
                continue

            author = span_msg["author"].lower()
            role = span_msg["author_role"]

            for p in proposals:
                pid = p.proposal_id
                # Check independent link between ruling quote/message and this specific proposal
                if _verify_ruling_alternative_link(
                    v_span.exact_quote, span_msg["content"], pid, proposals
                ):
                    if role == "bdfl":
                        bdfl_rulings_by_prop[pid].add(author)
                    elif role in ("maintainer", "core maintainer", "core_maintainer"):
                        maintainer_rulings_by_prop[pid].add(author)
                        maintainer_signoffs_by_prop[pid].add(author)
                    elif role in ("working group chair", "working_group_chair", "wg_chair"):
                        wg_chair_rulings_by_prop[pid].add(author)

        # Collect additional proposal-bound sign-offs from maintainers participating on that specific proposal
        for arg in arguments:
            pid = arg.proposal_id
            if pid not in maintainer_signoffs_by_prop:
                continue

            orig_msg = find_originating_message(
                quote=arg.quote_text,
                messages=msg_lookup,
                claimed_author=arg.author,
                require_unquoted=True,
            )
            if not orig_msg:
                continue

            author = orig_msg["author"].lower()
            role = orig_msg["author_role"]

            # Must be an objection or ruling or concurrence on this specific proposal
            if (
                arg.stance == EvidenceStance.OBJECT or arg.fatal_rejection or arg.is_ruling
            ) and _verify_ruling_alternative_link(
                arg.quote_text, orig_msg["content"], pid, proposals
            ):
                if role in ("maintainer", "core maintainer", "core_maintainer"):
                    maintainer_signoffs_by_prop[pid].add(author)
                elif role in ("working group member", "working_group_member", "wg_member"):
                    wg_member_signoffs_by_prop[pid].add(author)

        # Determine authorized rejections per proposal
        authorized_rejected_proposals: set[str] = set()
        for p in proposals:
            pid = p.proposal_id
            if bdfl_rulings_by_prop[pid]:
                # BDFL is self-authorizing
                authorized_rejected_proposals.add(pid)
            elif maintainer_rulings_by_prop[pid]:
                # Core Maintainer consensus requires ruling author + >=1 distinct maintainer sign-off on same proposal
                all_maintainers = maintainer_rulings_by_prop[pid] | maintainer_signoffs_by_prop[pid]
                if len(all_maintainers) >= 2:
                    authorized_rejected_proposals.add(pid)
            elif wg_chair_rulings_by_prop[pid]:
                # Working Group requires Chair + >=1 distinct WG member/maintainer sign-off on same proposal
                other_members = (
                    maintainer_signoffs_by_prop[pid] | wg_member_signoffs_by_prop[pid]
                ) - wg_chair_rulings_by_prop[pid]
                if len(other_members) >= 1:
                    authorized_rejected_proposals.add(pid)

        # Validate considered_alternatives
        updated_alternatives = []
        for alt in madr.considered_alternatives:
            alt_copy = dict(alt)
            prop_id = str(alt.get("proposal_id", ""))
            status = str(alt.get("status", "")).upper()

            if status == "REJECTED" and prop_id not in authorized_rejected_proposals:
                # Downgrade unauthorized rejection to DISPUTED
                alt_copy["status"] = "DISPUTED"
                alt_copy["unauthorized_ruling_note"] = (
                    f"Status downgraded from model-authored REJECTED to DISPUTED: "
                    f"requires verified BDFL ruling or Core Maintainer consensus sign-offs (>=2 maintainers) "
                    f"specifically bound to alternative {prop_id}."
                )
                downgraded_rejections.append(f"Alternative {prop_id}")
            updated_alternatives.append(alt_copy)
        madr.considered_alternatives = updated_alternatives

        # Validate overall decision status
        overall_status = madr.status.upper()
        if overall_status == "REJECTED" and not authorized_rejected_proposals:
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
            "unpersisted_candidate_targets": unpersisted_candidate_targets,
            "has_unpersisted_target_ids": bool(unpersisted_candidate_targets),
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
