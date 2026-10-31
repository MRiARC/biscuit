"""
Pass 2: Deliberation Slicing module (Deterministic DAG Slicer).
Operates purely deterministically with 0 GB model memory.
Extracts directed subgraphs around each candidate proposal into targeted ~1,000-word packets.
Guarantees late maintainer rulings are preserved during thread truncation.
"""

from typing import Any

from biscuit.extract.types import DeliberationPacket, ProposalCandidate

# Recognized authoritative roles whose messages must never be omitted during truncation
RULING_ROLES = {
    "bdfl",
    "maintainer",
    "core maintainer",
    "core_maintainer",
    "working group chair",
    "working_group_chair",
    "wg_chair",
}

RULING_KEYWORDS = {"reject", "rejected", "accept", "accepted", "decide", "ruling", "consensus"}


class Pass2DeliberationSlicing:
    """
    Pass 2: Deliberation Slicing (Deterministic Graph & Text Slicer).
    Subgraphs the discussion thread into targeted context packets centered around each proposal.
    Consumes 0 GB model memory.
    """

    def __init__(self, target_word_limit: int = 1500):
        self.target_word_limit = target_word_limit

    def slice_deliberation(
        self,
        messages: list[dict[str, Any]],
        proposals: list[ProposalCandidate],
        edges: list[dict[str, Any]] | None = None,
    ) -> list[DeliberationPacket]:
        """
        Slices thread messages into targeted deliberation packets for each proposal.
        Guarantees that both opening proposal introductions and late maintainer rulings
        are preserved even when thread length exceeds the word budget.

        Args:
            messages: List of message dictionaries.
            proposals: List of ProposalCandidate instances discovered in Pass 1.
            edges: Optional list of discussion_edges dicts.

        Returns:
            list[DeliberationPacket]: One packet per proposal candidate.
        """
        if not proposals:
            return []

        # Index messages by ID for fast lookup
        msg_by_id: dict[str, dict[str, Any]] = {}
        for idx, m in enumerate(messages):
            mid = str(m.get("message_id") or m.get("id") or f"msg-{idx}")
            m_copy = dict(m)
            m_copy["message_id"] = mid
            if "content" not in m_copy and "raw_content" in m_copy:
                m_copy["content"] = m_copy["raw_content"]
            elif "content" not in m_copy:
                m_copy["content"] = ""
            msg_by_id[mid] = m_copy

        # Build adjacency graph if edges provided
        outgoing_edges: dict[str, list[dict[str, Any]]] = {}
        incoming_edges: dict[str, list[dict[str, Any]]] = {}
        for edge in edges or []:
            src = str(edge.get("source_message_id", ""))
            tgt = str(edge.get("target_message_id", ""))
            if src and tgt:
                outgoing_edges.setdefault(src, []).append(edge)
                incoming_edges.setdefault(tgt, []).append(edge)

        packets: list[DeliberationPacket] = []

        for prop in proposals:
            selected_ids: set[str] = set()
            prop_terms = self._extract_proposal_search_terms(prop)

            # 1. Identify seed messages that mention or introduce the proposal
            seed_ids: list[str] = []
            for mid, m in msg_by_id.items():
                content_lower = str(m.get("content", "")).lower()
                if prop.proposal_id.lower() in content_lower or any(
                    term in content_lower for term in prop_terms
                ):
                    seed_ids.append(mid)

            if not seed_ids and messages:
                seed_ids.append(str(messages[0].get("message_id", "")))

            for sid in seed_ids:
                selected_ids.add(sid)

            # 2. Expand subgraph using discussion_edges and parent_message_id
            for sid in seed_ids:
                for in_edge in incoming_edges.get(sid, []):
                    src_id = in_edge.get("source_message_id")
                    if src_id in msg_by_id:
                        selected_ids.add(src_id)

                for out_edge in outgoing_edges.get(sid, []):
                    tgt_id = out_edge.get("target_message_id")
                    if tgt_id in msg_by_id:
                        selected_ids.add(tgt_id)

            for mid, m in msg_by_id.items():
                parent_id = m.get("parent_message_id")
                if parent_id and parent_id in selected_ids:
                    selected_ids.add(mid)

            # 3. Collect candidates in chronological order
            ordered_candidates = [
                msg_by_id[str(m.get("message_id") or m.get("id"))]
                for m in messages
                if str(m.get("message_id") or m.get("id")) in selected_ids
            ]

            if not ordered_candidates:
                ordered_candidates = [
                    msg_by_id[str(m.get("message_id") or m.get("id"))] for m in messages
                ]

            # 4. Word-bounding with Anchor Preservation (fixes reviewer issue 6)
            # Must preserve:
            # - Primary proposal seed message(s)
            # - Crucial late maintainer/ruling messages
            final_messages = self._budget_messages_preserving_anchors(ordered_candidates, seed_ids)
            total_words = sum(len(str(m.get("content", "")).split()) for m in final_messages)

            packets.append(
                DeliberationPacket(
                    proposal_id=prop.proposal_id,
                    proposal_name=prop.name,
                    message_ids=[str(m["message_id"]) for m in final_messages],
                    messages=final_messages,
                    word_count=total_words,
                )
            )

        return packets

    def _budget_messages_preserving_anchors(
        self, candidate_msgs: list[dict[str, Any]], seed_ids: list[str]
    ) -> list[dict[str, Any]]:
        """
        Budgets messages up to target_word_limit while guaranteeing that:
        1. Initial proposal seed messages are preserved.
        2. Authoritative ruling / maintainer messages (especially late in the thread) are preserved.
        3. Intermediate debate fills the remaining budget in chronological order.
        """
        total_words = sum(len(str(m.get("content", "")).split()) for m in candidate_msgs)
        if total_words <= self.target_word_limit:
            return candidate_msgs

        # Identify critical anchors
        seed_set = set(seed_ids)
        must_include_indices: set[int] = set()

        for idx, m in enumerate(candidate_msgs):
            mid = str(m.get("message_id", ""))
            role = str(m.get("author_role") or m.get("role") or "").strip().lower()
            content_lower = str(m.get("content", "")).lower()

            # Anchor 1: Proposal introducing message
            if mid in seed_set:
                must_include_indices.add(idx)

            # Anchor 2: Authoritative maintainer / BDFL ruling message
            if role in RULING_ROLES:
                if any(kw in content_lower for kw in RULING_KEYWORDS):
                    must_include_indices.add(idx)

        # Always include the very last message if authored by an authorized maintainer
        if candidate_msgs:
            last_role = (
                str(candidate_msgs[-1].get("author_role") or candidate_msgs[-1].get("role") or "")
                .strip()
                .lower()
            )
            if last_role in RULING_ROLES:
                must_include_indices.add(len(candidate_msgs) - 1)

        # Calculate word cost of must-include anchors
        anchors_words = sum(
            len(str(candidate_msgs[i].get("content", "")).split()) for i in must_include_indices
        )

        selected_indices = set(must_include_indices)
        current_words = anchors_words

        # Fill remaining budget with intermediate messages in chronological order
        for idx, m in enumerate(candidate_msgs):
            if idx in selected_indices:
                continue
            msg_words = len(str(m.get("content", "")).split())
            if current_words + msg_words <= self.target_word_limit:
                selected_indices.add(idx)
                current_words += msg_words
            elif len(selected_indices) == len(must_include_indices) and not must_include_indices:
                # If budget is extremely small, ensure at least one message is included
                selected_indices.add(idx)
                break

        # Return selected messages in strictly preserved chronological order
        return [candidate_msgs[i] for i in sorted(selected_indices)]

    def _extract_proposal_search_terms(self, prop: ProposalCandidate) -> list[str]:
        """Extracts distinctive lowercase search keywords from a proposal candidate."""
        terms = []
        if prop.name:
            terms.append(prop.name.lower())
        if prop.syntax_or_api:
            terms.append(prop.syntax_or_api.lower())
        return [t for t in terms if len(t) >= 3]
