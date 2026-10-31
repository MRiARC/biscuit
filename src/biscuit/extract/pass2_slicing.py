"""
Pass 2: Deliberation Slicing module (Deterministic DAG Slicer).
Operates purely deterministically with 0 GB model memory.
Extracts directed subgraphs around each candidate proposal into targeted ~1,000-word packets.
"""

from typing import List, Dict, Any, Optional, Set
from biscuit.extract.types import ProposalCandidate, DeliberationPacket


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
        messages: List[Dict[str, Any]],
        proposals: List[ProposalCandidate],
        edges: Optional[List[Dict[str, Any]]] = None,
    ) -> List[DeliberationPacket]:
        """
        Slices the thread messages into targeted deliberation packets for each proposal.

        Args:
            messages: List of message dictionaries, each containing:
                      'message_id', 'author', 'content' (or 'raw_content'),
                      and optional 'parent_message_id', 'author_role'.
            proposals: List of ProposalCandidate instances discovered in Pass 1.
            edges: Optional list of discussion_edges dicts with:
                   'source_message_id', 'target_message_id', 'edge_type'.

        Returns:
            List[DeliberationPacket]: One packet per proposal candidate.
        """
        if not proposals:
            return []

        # Index messages by ID for fast graph lookup
        msg_by_id: Dict[str, Dict[str, Any]] = {}
        for idx, m in enumerate(messages):
            mid = str(m.get("message_id") or m.get("id") or f"msg-{idx}")
            # Ensure message_id is consistently populated
            m_copy = dict(m)
            m_copy["message_id"] = mid
            # Normalize content field
            if "content" not in m_copy and "raw_content" in m_copy:
                m_copy["content"] = m_copy["raw_content"]
            elif "content" not in m_copy:
                m_copy["content"] = ""
            msg_by_id[mid] = m_copy

        # Build adjacency graph if edges provided
        outgoing_edges: Dict[str, List[Dict[str, Any]]] = {}
        incoming_edges: Dict[str, List[Dict[str, Any]]] = {}
        for edge in edges or []:
            src = str(edge.get("source_message_id", ""))
            tgt = str(edge.get("target_message_id", ""))
            if src and tgt:
                outgoing_edges.setdefault(src, []).append(edge)
                incoming_edges.setdefault(tgt, []).append(edge)

        packets: List[DeliberationPacket] = []

        for prop in proposals:
            selected_ids: Set[str] = set()
            prop_terms = self._extract_proposal_search_terms(prop)

            # 1. Identify primary/seed messages that mention or introduce the proposal
            seed_ids: List[str] = []
            for mid, m in msg_by_id.items():
                content_lower = str(m.get("content", "")).lower()
                # Check for direct proposal ID match (e.g. "P1", "proposal 1") or term match
                if prop.proposal_id.lower() in content_lower:
                    seed_ids.append(mid)
                elif any(term in content_lower for term in prop_terms):
                    seed_ids.append(mid)

            # If no seed message found by terms, include the first message (often the thread OP)
            if not seed_ids and messages:
                seed_ids.append(str(messages[0].get("message_id", "")))

            for sid in seed_ids:
                selected_ids.add(sid)

            # 2. Expand subgraph using discussion_edges and parent_message_id
            for sid in seed_ids:
                # Add incoming replies/objections/supports
                for in_edge in incoming_edges.get(sid, []):
                    src_id = in_edge.get("source_message_id")
                    if src_id in msg_by_id:
                        selected_ids.add(src_id)

                # Add outgoing targets
                for out_edge in outgoing_edges.get(sid, []):
                    tgt_id = out_edge.get("target_message_id")
                    if tgt_id in msg_by_id:
                        selected_ids.add(tgt_id)

            # Also check parent_message_id references
            for mid, m in msg_by_id.items():
                parent_id = m.get("parent_message_id")
                if parent_id and parent_id in selected_ids:
                    selected_ids.add(mid)

            # 3. Preserve chronological message order
            ordered_packet_msgs: List[Dict[str, Any]] = []
            total_words = 0

            for m in messages:
                mid = str(m.get("message_id") or m.get("id"))
                if mid in selected_ids:
                    content = str(msg_by_id[mid].get("content", ""))
                    msg_words = len(content.split())
                    # If adding this message exceeds the word limit and we already have some messages, stop
                    if total_words + msg_words > self.target_word_limit and ordered_packet_msgs:
                        break
                    ordered_packet_msgs.append(msg_by_id[mid])
                    total_words += msg_words

            # Fallback: If no message was captured, take at least the first few messages
            if not ordered_packet_msgs:
                for m in messages:
                    content = str(m.get("content") or m.get("raw_content") or "")
                    msg_words = len(content.split())
                    if total_words + msg_words > self.target_word_limit and ordered_packet_msgs:
                        break
                    mid = str(m.get("message_id") or m.get("id"))
                    m_copy = dict(m)
                    m_copy["message_id"] = mid
                    m_copy["content"] = content
                    ordered_packet_msgs.append(m_copy)
                    total_words += msg_words

            packets.append(
                DeliberationPacket(
                    proposal_id=prop.proposal_id,
                    proposal_name=prop.name,
                    message_ids=[str(m["message_id"]) for m in ordered_packet_msgs],
                    messages=ordered_packet_msgs,
                    word_count=total_words,
                )
            )

        return packets

    def _extract_proposal_search_terms(self, prop: ProposalCandidate) -> List[str]:
        """Extracts distinctive lowercase search keywords from a proposal candidate."""
        terms = []
        if prop.name:
            terms.append(prop.name.lower())
        if prop.syntax_or_api:
            terms.append(prop.syntax_or_api.lower())
        return [t for t in terms if len(t) >= 3]
