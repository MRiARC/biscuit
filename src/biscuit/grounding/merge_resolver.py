"""Conservative merge-parent reconciliation; conflicting histories abstain."""

from __future__ import annotations

from collections.abc import Iterable

from .types import AnchorState, MergeResolution


def resolve_merge_parents(
    parent_candidates: Iterable[Iterable[str]],
    merge_candidates: Iterable[str],
) -> MergeResolution:
    """Decide whether the resolved symbol has one unambiguous lineage.

    Parent paths can disagree after independently moved symbols. Such a merge
    must not silently pick first-parent or assign confidence 1.0. If a symbol
    is absent in the final merge tree, its lineage is DELETED, not guessed.
    """
    parents = [set(c) for c in parent_candidates]
    paths = tuple(sorted(set(merge_candidates)))
    parent_paths = set().union(*parents) if parents else set()
    if len(paths) > 1:
        return MergeResolution(
            AnchorState.AMBIGUOUS, paths, "Multiple plausible symbols survive merge"
        )
    if len(parent_paths) > 1 and len([p for p in parents if p]) > 1:
        return MergeResolution(
            AnchorState.AMBIGUOUS, tuple(sorted(parent_paths | set(paths))),
            "Divergent parent symbol paths require human review",
        )
    if len(paths) == 0:
        return MergeResolution(AnchorState.DELETED, (), "No matching symbol in merge tree")
    return MergeResolution(
        AnchorState.MERGED, paths, "One symbol survives compatible parent histories"
    )