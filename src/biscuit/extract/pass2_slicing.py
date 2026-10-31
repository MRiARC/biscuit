"""
Pass 2: Deliberation Slicing module (Deterministic DAG Slicer).
Operates purely deterministically with 0 GB model memory.
Extracts directed subgraphs around each candidate proposal into targeted ~1,000-word packets.
Guarantees late maintainer rulings are preserved during thread truncation.
"""

import re
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
                content_lower = str(m.get("content") or m.get("raw_content") or "").lower()
                role = str(m.get("author_role") or m.get("role") or "").strip().lower()

                if prop.proposal_id.lower() in content_lower or any(
                    term in content_lower for term in prop_terms
                ):
                    seed_ids.append(mid)

                # Maintainer rulings are critical anchors for deliberation
                if role in RULING_ROLES and any(kw in content_lower for kw in RULING_KEYWORDS):
                    selected_ids.add(mid)

            if not seed_ids and messages:
                seed_ids.append(str(messages[0].get("message_id") or messages[0].get("id") or ""))

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

            # In linear discussions without edges, all chronological messages are candidate deliberation turns
            if not edges:
                for mid in msg_by_id:
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
        Budgets messages strictly up to target_word_limit while guaranteeing that:
        1. Initial proposal seed messages are preserved.
        2. Authoritative ruling / maintainer messages (especially late in the thread) are preserved.
        3. If essential anchors alone exceed target_word_limit, they are capped/chunked
           to strictly enforce the bounded context while preserving identities and provenance.
        4. Intermediate debate fills remaining budget in chronological order.
        """
        if not candidate_msgs:
            return []

        total_words = sum(len(str(m.get("content", "")).split()) for m in candidate_msgs)
        if total_words <= self.target_word_limit:
            return candidate_msgs

        seed_set = set(seed_ids)
        seed_indices = [
            i for i, m in enumerate(candidate_msgs) if str(m.get("message_id", "")) in seed_set
        ]
        ruling_indices: list[int] = []

        for idx, m in enumerate(candidate_msgs):
            role = str(m.get("author_role") or m.get("role") or "").strip().lower()
            content_lower = str(m.get("content", "")).lower()
            if role in RULING_ROLES and any(kw in content_lower for kw in RULING_KEYWORDS):
                ruling_indices.append(idx)

        # Always consider the very last message as an anchor candidate if authored by an authorized maintainer
        last_role = (
            str(candidate_msgs[-1].get("author_role") or candidate_msgs[-1].get("role") or "")
            .strip()
            .lower()
        )
        if last_role in RULING_ROLES and (len(candidate_msgs) - 1) not in ruling_indices:
            ruling_indices.append(len(candidate_msgs) - 1)

        # Determine primary seed anchor and latest ruling anchor
        primary_seed_idx = seed_indices[0] if seed_indices else 0
        latest_ruling_idx = ruling_indices[-1] if ruling_indices else None

        essential_indices: list[int] = [primary_seed_idx]
        if latest_ruling_idx is not None and latest_ruling_idx != primary_seed_idx:
            essential_indices.append(latest_ruling_idx)
        essential_indices.sort()

        # Calculate word count of essential anchors
        essential_words = [
            len(str(candidate_msgs[i].get("content", "")).split()) for i in essential_indices
        ]
        total_essential_words = sum(essential_words)

        # Case 1: Essential anchors alone exceed the target_word_limit
        if total_essential_words > self.target_word_limit:
            budget = max(1, self.target_word_limit)
            if len(essential_indices) == 1:
                capped = self._cap_message(candidate_msgs[essential_indices[0]], budget)
                return [capped]

            idx1, idx2 = essential_indices[0], essential_indices[1]
            w1 = essential_words[0]
            w2 = essential_words[1]

            if w1 <= budget // 2:
                alloc1 = w1
                alloc2 = budget - alloc1
            elif w2 <= budget // 2:
                alloc2 = w2
                alloc1 = budget - alloc2
            else:
                alloc1 = budget // 2
                alloc2 = budget - alloc1

            capped1 = self._cap_message(candidate_msgs[idx1], alloc1)
            capped2 = self._cap_message(candidate_msgs[idx2], alloc2)
            return [capped1, capped2]

        # Case 2: Essential anchors fit. Fill remaining budget with other anchors, then chronological debate.
        selected_map: dict[int, dict[str, Any]] = {i: candidate_msgs[i] for i in essential_indices}
        current_words = total_essential_words

        # Secondary anchors: other rulings (reverse chron) and other seeds
        secondary_anchors: list[int] = []
        for r_idx in reversed(ruling_indices):
            if r_idx not in selected_map and r_idx not in secondary_anchors:
                secondary_anchors.append(r_idx)
        for s_idx in seed_indices:
            if s_idx not in selected_map and s_idx not in secondary_anchors:
                secondary_anchors.append(s_idx)

        for a_idx in secondary_anchors:
            w = len(str(candidate_msgs[a_idx].get("content", "")).split())
            if current_words + w <= self.target_word_limit:
                selected_map[a_idx] = candidate_msgs[a_idx]
                current_words += w

        # Fill remaining budget with intermediate messages in chronological order
        for idx, m in enumerate(candidate_msgs):
            if idx in selected_map:
                continue
            w = len(str(m.get("content", "")).split())
            if current_words + w <= self.target_word_limit:
                selected_map[idx] = m
                current_words += w

        return [selected_map[i] for i in sorted(selected_map.keys())]

    @staticmethod
    def _cap_message(msg: dict[str, Any], max_words: int) -> dict[str, Any]:
        """Truncates a message's content to max_words while preserving its metadata and provenance."""
        capped = dict(msg)
        words = str(msg.get("content", "")).split()
        if len(words) > max_words:
            capped["content"] = " ".join(words[:max_words])
            capped["raw_content"] = capped["content"]
            capped["is_truncated"] = True
        return capped

    def _extract_proposal_search_terms(self, prop: ProposalCandidate) -> list[str]:
        """Extracts distinctive lowercase search keywords from a proposal candidate."""
        terms: list[str] = []
        if prop.name:
            clean_name = re.sub(r"[^\w\s]", " ", prop.name).strip()
            terms.append(clean_name.lower())
            terms.append(prop.name.lower())
            for word in clean_name.split():
                if len(word) >= 3:
                    terms.append(word.lower())
        if prop.syntax_or_api:
            clean_syntax = prop.syntax_or_api.strip()
            if len(clean_syntax) >= 1:
                terms.append(clean_syntax.lower())
        seen: set[str] = set()
        out: list[str] = []
        for t in terms:
            if t and t not in seen:
                seen.add(t)
                out.append(t)
        return out
