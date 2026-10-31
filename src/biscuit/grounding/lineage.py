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
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, check=False,
            timeout=30, env={**os.environ, "GIT_NO_REPLACE_OBJECTS": "1"},
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
    probe = subprocess.run(
        ["git", "-C", str(root), "merge-base", "--is-ancestor", past, current],
        capture_output=True, check=False, timeout=30,
    )
    if probe.returncode:
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
    result = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "-e", f"{commit}:{path}"],
        capture_output=True, check=False, timeout=30,
    )
    if result.returncode:
        return None
    return _git(repo, "show", f"{commit}:{path}")


def _symbol_at(
    repo: Path, commit: str, path: str, language: str,
    exemplar: Symbol, *, require_tree_sitter: bool,
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
    if len(same_name) == 1:
        return same_name[0]
    if len(same_name) > 1:
        return None
    if exemplar.structural_fingerprint:
        structurally_same = [s for s in symbols if s.structural_fingerprint ==
                             exemplar.structural_fingerprint and s.symbol_type == exemplar.symbol_type]
        if len(structurally_same) == 1:
            return structurally_same[0]
    return None


def _matching_paths(
    repo: Path, commit: str, paths: set[str], language: str,
    exemplar: Symbol, require_tree_sitter: bool,
) -> set[str]:
    return {p for p in paths if _symbol_at(repo, commit, p, language, exemplar,
                                          require_tree_sitter=require_tree_sitter) is not None}


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
                next_paths.append(new)
                operation, old_file, new_file = "COPY_C", old, new
            elif status.startswith("D") and new == old_path:
                next_paths = []
                operation, old_file = "DELETE", old_path
            elif status.startswith("M") and new == old_path:
                operation, old_file, new_file = "MODIFY", old_path, old_path
            elif status.startswith("B") and new == old_path:
                operation, old_file, new_file = "REWRITE_SPLIT_B", old_path, old_path
        candidates |= _matching_paths(repo, commit, set(next_paths), language, exemplar,
                                      require_tree_sitter)
    # Symbol moves/splits may not be recognized by Git's file similarity score.
    # Scan only touched source files, including new copies while the source
    # still exists. Otherwise a silent copy would be falsely marked unique.
    extension = ".py" if language == "python" else ".c"
    if touched:
        scan = {p for p in touched if p.endswith(extension)}
        candidates |= _matching_paths(repo, commit, scan, language, exemplar,
                                      require_tree_sitter)
    if len(candidates) > 1:
        operation = "COPY_C" if operation == "COPY_C" else "REWRITE_SPLIT_B"
    if not candidates and parent_paths:
        operation = "DELETE" if operation == "UNCHANGED" else operation
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
        considered: list[set[str]] = []
        result_sets: list[set[str]] = []
        records: list[tuple[str, int, set[str], set[str], str, str | None, str | None, str]] = []
        for index, parent in enumerate(parents):
            prev = states.get(parent, set())
            considered.append(prev)
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
        if len(parents) > 1:
            # A clean merge must match one surviving symbol at the merge tree.
            final_candidates = _matching_paths(root, commit, paths, language, exemplar,
                                               require_tree_sitter)
            resolution = resolve_merge_parents(result_sets, final_candidates)
            if resolution.state == AnchorState.AMBIGUOUS:
                ambiguous = True
            elif resolution.state == AnchorState.MERGED:
                merged = True
            paths = set(resolution.paths)
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
                parent_index=index, old_path=old_p or (next(iter(prev)) if len(prev) == 1 else None),
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
        anchor_state=state, candidates=tuple(sorted(final)),
        events=tuple(events),
    )


# Descriptive aliases used by the architecture and API layers.
trace_forward_lineage = trace_lineage
enumerate_forward_dag = forward_commits