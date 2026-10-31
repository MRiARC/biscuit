"""Forward-only, parent-aware Git symbol lineage with persisted diff evidence.

Does not use ``git log --follow`` or a shell; all Git queries run locally with
subprocess argument arrays. Every merge parent is examined independently.
Unknown confidence remains None. AMBIGUOUS/SPLIT/DELETED never authorize a move.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path, PurePosixPath

from .cst_parser import ParseError, extract_symbols
from .merge_resolver import resolve_merge_parents
from .types import AnchorState, LineageEvent, LineageResult, Symbol


class GitLineageError(RuntimeError):
    pass


def _git(repo: Path, *args: str, allow_failure: bool = False) -> bytes:
    # Inherited Git routing/config variables may silently redirect object reads
    # into a different repository. A reproducible evidence traversal ignores them.
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith("GIT_")}
    environment["GIT_NO_REPLACE_OBJECTS"] = "1"
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, check=False,
            timeout=30, env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitLineageError(f"Git invocation failed: {exc}") from exc
    if proc.returncode and not allow_failure:
        raise GitLineageError(proc.stderr.decode("utf-8", "replace").strip() or
                              f"git {args[0]} returned {proc.returncode}")
    return proc.stdout


def _relative_path(path: str) -> str:
    p = PurePosixPath(path)
    if not path or path.startswith("-") or p.is_absolute() or ".." in p.parts:
        raise ValueError(f"Unsafe repository-relative path: {path!r}")
    return p.as_posix()


def _rev(repo: Path, ref: str) -> str:
    # --end-of-options stops a user-supplied ref from being parsed as an option.
    return _git(repo, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}").decode().strip()


def forward_commits(repo: str | Path, historical_commit: str, head: str = "HEAD") -> tuple[str, ...]:
    """Only commits *after* a verified ancestor, in topological forward order."""
    root = Path(repo).resolve()
    past = _rev(root, historical_commit)
    current = _rev(root, head)
    if _git(root, "merge-base", past, current).decode("ascii").strip() != past:
        raise GitLineageError("Historical commit is not an ancestor of the requested HEAD")
    lines = _git(root, "rev-list", "--topo-order", "--reverse", f"{past}..{current}")
    return tuple(lines.decode("ascii").split())


def _parents(repo: Path, commit: str) -> tuple[str, ...]:
    fields = _git(repo, "rev-list", "--parents", "-n", "1", commit).decode("ascii").split()
    return tuple(fields[1:])


def _name_status(repo: Path, parent: str, commit: str) -> tuple[tuple[str, str | None, str], ...]:
    # -z ensures paths with whitespace and tabs are unambiguous.
    raw = _git(repo, "diff-tree", "-r", "-M80%", "-C", "--find-copies-harder", "-B",
               "--name-status", "-z", parent, commit)
    fields = raw.decode("utf-8", "surrogateescape").split("\0")
    changes: list[tuple[str, str | None, str]] = []
    pos = 0
    while pos < len(fields) and fields[pos]:
        status = fields[pos]
        pos += 1
        if status.startswith(("R", "C")):
            if pos + 1 >= len(fields):
                raise GitLineageError("Incomplete Git rename/copy status")
            old, new = fields[pos], fields[pos + 1]
            pos += 2
            changes.append((status, old, new))
        else:
            if pos >= len(fields):
                raise GitLineageError("Incomplete Git file status")
            new = fields[pos]
            pos += 1
            changes.append((status, None, new))
    return tuple(changes)


def _commit_bytes(repo: Path, commit: str, path: str) -> bytes | None:
    # An empty source blob cannot contain a tracked symbol. Avoid an unguarded
    # subprocess call so GIT_* sanitization and replace-ref disabling match _rev.
    blob = _git(repo, "show", f"{commit}:{path}", allow_failure=True)
    return blob if blob else None


def _symbol_at(
    repo: Path, commit: str, path: str, language: str,
    exemplar: Symbol, *, require_tree_sitter: bool, require_identity: bool = False,
) -> Symbol | None:
    data = _commit_bytes(repo, commit, path)
    if data is None:
        return None
    try:
        symbols = extract_symbols(data, language, path,
                                  require_tree_sitter=require_tree_sitter)
    except ParseError:
        return None
    same_name = [s for s in symbols if s.symbol_name == exemplar.symbol_name
                 and s.symbol_type == exemplar.symbol_type]
    # Candidate discovery in another file needs evidence independent of the
    # name. A common function name in an unrelated touched file is NOT proof
    # that the historical function moved there.
    if len(same_name) > 1:
        return None
    if len(same_name) == 1 and not require_identity:
        return same_name[0]
    structurally_same = [
        s for s in symbols
        if exemplar.structural_fingerprint is not None
        and s.structural_fingerprint == exemplar.structural_fingerprint
        and s.symbol_type == exemplar.symbol_type
    ]
    if len(structurally_same) == 1:
        return structurally_same[0]
    # C parsers may not provide a structural fingerprint for every declaration.
    # A matching normalized signature is a conservative fallback for that case.
    if (exemplar.structural_fingerprint is None and len(same_name) == 1
            and same_name[0].signature_hash == exemplar.signature_hash):
        return same_name[0]
    return None


def _matching_paths(
    repo: Path, commit: str, paths: set[str], language: str,
    exemplar: Symbol, require_tree_sitter: bool, *, require_identity: bool = False,
) -> set[str]:
    return {p for p in paths if _symbol_at(repo, commit, p, language, exemplar,
                                          require_tree_sitter=require_tree_sitter,
                                          require_identity=require_identity) is not None}


def _transition(
    repo: Path, parent: str, commit: str, parent_paths: set[str], language: str,
    exemplar: Symbol, require_tree_sitter: bool,
) -> tuple[set[str], str, str | None, str | None, str]:
    statuses = _name_status(repo, parent, commit)
    touched = set()
    for _, old, new in statuses:
        touched.add(new)
        if old:
            touched.add(old)
    candidates: set[str] = set()
    operation = "UNCHANGED"
    old_file = None
    new_file = None
    # Direct continuity and Git's actual -M80% rename/copy classifications.
    for old_path in sorted(parent_paths):
        next_paths = [old_path]
        for status, old, new in statuses:
            if status.startswith("R") and old == old_path:
                next_paths = [new]
                operation, old_file, new_file = "RENAME_M", old, new
            elif status.startswith("C") and old == old_path:
                # The original and copy both remain plausible only if their
                # symbols actually match. Treat this as a split, not a move.
                next_paths.append(new)
                operation, old_file, new_file = "COPY_C", old, new
            elif status.startswith("D") and new == old_path:
                next_paths = []
                operation, old_file = "DELETE", old_path
            elif status.startswith("M") and new == old_path:
                operation, old_file, new_file = "MODIFY", old_path, old_path
            elif status.startswith("B") and new == old_path:
                operation, old_file, new_file = "REWRITE_SPLIT_B", old_path, old_path
        for next_path in set(next_paths):
            candidates |= _matching_paths(
                repo, commit, {next_path}, language, exemplar, require_tree_sitter,
                require_identity=(next_path != old_path and
                                  not any(status.startswith("R") and old == old_path
                                          and new == next_path for status, old, new in statuses)),
            )
    # Symbol moves/splits may not be recognized by Git's file similarity score.
    # Scan only touched source files, including new copies while the source
    # still exists. Otherwise a silent copy would be falsely marked unique.
    extension = ".py" if language == "python" else ".c"
    if touched:
        scan = {p for p in touched if p.endswith(extension)}
        # This broad scan is strictly fingerprint-based: unrelated same-name
        # functions are not enough to establish a move/copy.
        candidates |= _matching_paths(repo, commit, scan - parent_paths, language, exemplar,
                                      require_tree_sitter, require_identity=True)
    if len(candidates) > 1:
        operation = "COPY_C" if operation == "COPY_C" else "REWRITE_SPLIT_B"
    if not candidates and parent_paths:
        # A modified file may survive while its historical symbol is rewritten
        # away. This is a conservative -B-style rewrite *candidate*; the event
        # does not claim Git supplied a measured similarity percentage.
        if operation == "MODIFY" and any(
            _commit_bytes(repo, commit, p) is not None for p in parent_paths
        ):
            operation = "REWRITE_SPLIT_B"
        elif operation == "UNCHANGED":
            operation = "DELETE"
    return candidates, operation, old_file, new_file, hashlib.sha256(
        _git(repo, "diff-tree", "-r", "-M80%", "-C", "--find-copies-harder", "-B",
             "-p", parent, commit)).hexdigest()


def trace_lineage(
    repo: str | Path, historical_commit: str, historical_path: str,
    symbol_name: str, *, head: str = "HEAD", language: str = "python",
    require_tree_sitter: bool = False,
) -> LineageResult:
    """Follow a named symbol across the commit DAG; abstain on uncertain moves.

    Production callers should pass ``require_tree_sitter=True``; the default
    permits isolated stdlib-only unit tests, clearly tagged by the parser backend.
    """
    root = Path(repo).resolve()
    historical_path = _relative_path(historical_path)
    if language not in ("python", "c"):
        raise ValueError("Supported languages: python, c")
    past = _rev(root, historical_commit)
    current = _rev(root, head)
    initial = _commit_bytes(root, past, historical_path)
    if initial is None:
        raise GitLineageError("Historical path does not exist at the historical commit")
    symbols = extract_symbols(initial, language, historical_path,
                              require_tree_sitter=require_tree_sitter)
    matches = [s for s in symbols if s.qualified_name == symbol_name or
               s.symbol_name == symbol_name]
    if len(matches) != 1:
        raise GitLineageError("Historical symbol not found uniquely; refusing to invent an anchor")
    exemplar = matches[0]
    commits = forward_commits(root, past, current)
    states: dict[str, set[str]] = {past: {historical_path}}
    events: list[LineageEvent] = []
    changed = False
    ambiguous = False
    split = False
    merged = False
    for commit in commits:
        parents = _parents(root, commit)
        result_sets: list[set[str]] = []
        records: list[tuple[str, int, set[str], set[str], str, str | None, str | None, str]] = []
        for index, parent in enumerate(parents):
            prev = states.get(parent, set())
            if not prev:
                # Parallel branch that did not descend from historical commit.
                # Still log its diff; it conveys no proven historical lineage.
                digest = hashlib.sha256(_git(root, "diff-tree", "-r", "-M80%", "-C",
                    "--find-copies-harder", "-B", "-p", parent, commit)).hexdigest()
                next_candidates, kind, old_p, new_p = set(), "UNRELATED_PARENT", None, None
            else:
                next_candidates, kind, old_p, new_p, digest = _transition(
                    root, parent, commit, prev, language, exemplar, require_tree_sitter
                )
            result_sets.append(next_candidates)
            records.append((parent, index, prev, next_candidates, kind, old_p, new_p, digest))
        paths = set().union(*result_sets) if result_sets else set()
        combined_diff_digest = None
        combined_changed_paths: tuple[str, ...] = ()
        merge_reason = None
        first_parent_selected = False
        if len(parents) > 1:
            # Combined diffs reveal conflict-resolution changes against all
            # parents; per-parent diffs alone are insufficient evidence.
            combined_diff_digest = hashlib.sha256(
                _git(root, "diff-tree", "-c", "-p", commit)
            ).hexdigest()
            # Only files modified relative to *all* merge parents appear in
            # combined diff output. Such conflict-resolution rewrites demand
            # structural proof, never merely a familiar symbol name.
            combined_names = _git(root, "diff-tree", "-c", "--name-only", "-z", commit)
            combined_changed_paths = tuple(sorted({
                p.decode("utf-8", "surrogateescape")
                for p in combined_names.split(b"\0") if p
            }))
            final_candidates = _matching_paths(root, commit, paths, language, exemplar,
                                               require_tree_sitter)
            resolution = resolve_merge_parents(result_sets, final_candidates)
            merge_reason = resolution.reason
            if (set(combined_changed_paths) & set(final_candidates)
                    and len([x for x in result_sets if x]) > 1
                    and any(_symbol_at(root, commit, p, language, exemplar,
                                       require_tree_sitter=require_tree_sitter,
                                       require_identity=True) is None
                            for p in final_candidates)):
                # The human-created merge result differs structurally from
                # the historical anchor. Without verification, abstain.
                ambiguous = True
                merge_reason = "Combined merge diff rewrote symbol without structural match"
            if resolution.state == AnchorState.AMBIGUOUS:
                ambiguous = True
            elif resolution.state == AnchorState.MERGED:
                merged = True
            paths = set(resolution.paths)
            # If a conflict resolution discarded changes from another parent,
            # we may track the first-parent (mainline) version ONLY when that
            # exact file exists in the merge tree. Never resurrect a deleted
            # symbol from a parent that is absent in the merge result.
            if len(paths) == 1 and len(parents) > 1:
                selected_path = next(iter(paths))
                merged_bytes = _commit_bytes(root, commit, selected_path)
                mainline_bytes = _commit_bytes(root, parents[0], selected_path)
                first_parent_selected = (
                    merged_bytes is not None and merged_bytes == mainline_bytes
                    and any(_commit_bytes(root, p, selected_path) != merged_bytes
                            for p in parents[1:])
                )
        if len(paths) > 1:
            ambiguous, split = True, True
        # Once ambiguous, never silently become 'safe' due to a later delete.
        states[commit] = paths
        for parent, index, prev, after, kind, old_p, new_p, digest in records:
            if kind not in ("UNCHANGED", "UNRELATED_PARENT"):
                changed = True
            if kind in ("RENAME_M", "COPY_C", "REWRITE_SPLIT_B", "MODIFY"):
                changed = True
            state = (AnchorState.AMBIGUOUS if ambiguous else
                     AnchorState.SPLIT if len(after) > 1 else
                     AnchorState.DELETED if not after and prev else
                     AnchorState.MIGRATED if kind in ("RENAME_M", "COPY_C", "REWRITE_SPLIT_B") else
                     AnchorState.CURRENT)
            events.append(LineageEvent(
                transition_commit_sha=commit, compared_parent_sha=parent,
                target_commit_sha=commit, parent_index=index, old_path=old_p or (next(iter(prev)) if len(prev) == 1 else None),
                new_path=new_p or (next(iter(after)) if len(after) == 1 else None),
                transition_type=kind, state=state,
                mapping_evidence={
                    "diff_patch_sha256": digest, "symbol_name": exemplar.qualified_name,
                    "historical_commit_sha": past,
                    "parser_backend": exemplar.parser_backend,
                    "parser_version": exemplar.parser_version,
                    "grammar_version": exemplar.grammar_version,
                    "normalization_version": exemplar.normalization_version,
                    "parent_paths": sorted(prev), "result_paths": sorted(after),
                    "parent_count": len(parents),
                    "combined_diff_sha256": combined_diff_digest,
                    "combined_changed_paths": list(combined_changed_paths),
                    "merge_resolution_reason": merge_reason,
                    "first_parent_selected": first_parent_selected,
                    "all_parent_candidate_paths": [sorted(s) for s in result_sets],
                    "requires_structural_verification": True,
                },
            ))
    final = states[current]
    if ambiguous or split:
        state = AnchorState.AMBIGUOUS
    elif not final:
        state = AnchorState.DELETED
    elif len(final) != 1:
        state = AnchorState.AMBIGUOUS
    elif merged:
        state = AnchorState.MIGRATED if changed else AnchorState.CURRENT
    elif changed:
        state = AnchorState.MIGRATED
    else:
        state = AnchorState.CURRENT
    return LineageResult(
        historical_commit_sha=past, head_commit_sha=current,
        historical_path=historical_path,
        resolved_path=next(iter(final)) if len(final) == 1 and not ambiguous else None,
        anchor_state=state, verified_parser=exemplar.parser_backend == "tree-sitter",
        candidates=tuple(sorted(final)), events=tuple(events),
    )


# Descriptive aliases used by the architecture and API layers.
trace_forward_lineage = trace_lineage
enumerate_forward_dag = forward_commits