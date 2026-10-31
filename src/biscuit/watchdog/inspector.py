"""PR diff inspection: unified git diff parsing and AST structural analysis.

``PRDiffInspector`` implements the first stage of the PR watchdog pipeline
(docs/CODE_GROUNDING_AND_AST.md):

* parses unified git diff hunks (added/removed/context lines per file),
* extracts the added-side code fragment of every file,
* for Python fragments, computes top-level symbols and *masked normalized
  AST subtree fingerprints* (Tier 3 hashing with identifier masking, so
  variable renames and reformatting do not defeat structural matching).

Structural fingerprints are SHA-256 digests of the identifier-masked AST
dump using normalization ruleset ``python-ast-mask-v1``:

* function/class definition names and parameter names are masked,
* local variable names are consistently alpha-renamed (``$N0``, ``$N1``...),
* call targets (e.g. ``coalesce``) and attribute names are preserved,
* line/column attributes are dropped (formatting independence).

Anything the fragment parser cannot handle is reported as unparseable; the
authorization gate then abstains. File or symbol overlap alone is never
treated as structural equivalence.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import re
import textwrap
from itertools import count

from biscuit.watchdog.types import DiffFile, DiffHunk, FileAnalysis, PRDiff

AST_NORMALIZATION_VERSION = "python-ast-mask-v1"

_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)")
_LANGUAGE_BY_SUFFIX = {
    ".py": "python",
    ".pyi": "python",
    ".c": "c",
    ".h": "c",
    ".rs": "rust",
    ".go": "go",
    ".java": "java",
    ".js": "javascript",
    ".ts": "typescript",
}


class _IdentifierMasker(ast.NodeTransformer):
    """Alpha-renames local identifiers while preserving API surface names.

    Call target names (``coalesce(...)``) and attribute names
    (``stream.peek()``) are kept because they identify the construct;
    definition names, parameters, and local variables are masked so renames
    and reformatting do not change the fingerprint. The same source name
    always maps to the same token within one fragment.
    """

    def __init__(self) -> None:
        self._name_tokens: dict[str, str] = {}
        self._name_counter = count()

    def _mask_name(self, name: str) -> str:
        if name not in self._name_tokens:
            self._name_tokens[name] = f"$N{next(self._name_counter)}"
        return self._name_tokens[name]

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        self.generic_visit(node)
        node.name = "$DEF"
        return node

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AST:
        self.generic_visit(node)
        node.name = "$DEF"
        return node

    def visit_ClassDef(self, node: ast.ClassDef) -> ast.AST:
        self.generic_visit(node)
        node.name = "$CLASS"
        return node

    def visit_arg(self, node: ast.arg) -> ast.AST:
        node.arg = self._mask_name(node.arg)
        return node

    def visit_Call(self, node: ast.Call) -> ast.AST:
        if isinstance(node.func, ast.Name):
            # Preserve a direct callee name; it identifies the construct.
            node.args = [self.visit(child) for child in node.args]
            node.keywords = [self.visit(child) for child in node.keywords]
        else:
            self.generic_visit(node)
        return node

    def visit_Name(self, node: ast.Name) -> ast.AST:
        node.id = self._mask_name(node.id)
        return node


def _masked_dump(node: ast.AST) -> str:
    return ast.dump(node, annotate_fields=True, include_attributes=False)


def mask_module(module: ast.Module) -> ast.Module:
    """Mask local identifiers of a parsed module, in place."""
    masker = _IdentifierMasker()
    masked = masker.visit(module)
    return ast.fix_missing_locations(masked)


def subtree_fingerprints(module: ast.Module) -> frozenset[str]:
    """Fingerprints of every subtree in ``module``.

    Each subtree is masked *independently* with a fresh masker on a copy, so
    a construct's fingerprint does not depend on surrounding code: the
    ``coalesce(a, b)`` call hashes identically whether it stands alone (an
    anchor snippet) or sits at the end of a larger added fragment whose
    earlier lines already consumed the ``$N`` tokens.
    """
    fingerprints = set()
    for node in ast.walk(module):
        candidate = copy.deepcopy(node)
        masker = _IdentifierMasker()
        masked = masker.visit(candidate)
        fingerprints.add(
            hashlib.sha256(_masked_dump(masked).encode("utf-8")).hexdigest()
        )
    return frozenset(fingerprints)


def structural_fingerprint(code: str) -> str:
    """Fingerprint of the primary construct in a Python snippet.

    The construct is the first top-level statement; when that statement is a
    bare expression, the expression node itself is fingerprinted so an anchor
    snippet like ``coalesce(a, b)`` matches the ``Call`` subtree wherever it
    appears in reintroduced code.
    """
    module = ast.parse(code)
    if not module.body:
        raise ValueError("snippet contains no statements")
    node = module.body[0]
    if isinstance(node, ast.Expr):
        node = node.value
    masker = _IdentifierMasker()
    masked_node = masker.visit(node)
    return hashlib.sha256(_masked_dump(masked_node).encode("utf-8")).hexdigest()


def _language_for(path: str) -> str:
    for suffix, language in _LANGUAGE_BY_SUFFIX.items():
        if path.endswith(suffix):
            return language
    return "unknown"


def _strip_prefix(path: str) -> str | None:
    path = path.strip().strip('"')
    if path == "/dev/null":
        return None
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


class PRDiffInspector:
    """Parses unified git diffs and extracts AST-level diff signals."""

    normalization_version = AST_NORMALIZATION_VERSION

    def parse(self, diff_text: str) -> PRDiff:
        """Parse a unified git diff into files and hunks.

        Supports ``diff --git`` headers plus the standard
        ``--- a/…`` / ``+++ b/…`` / ``@@ … @@`` hunk grammar, including new
        files, deleted files, renames, and ``\\ No newline at end of file``
        markers. Raises ``ValueError`` for empty or header-less input.
        """
        files: list[DiffFile] = []
        current: dict | None = None
        hunks: list[dict] | None = None
        hunk: dict | None = None

        def flush_hunk() -> None:
            nonlocal hunk
            if hunk is not None and hunks is not None:
                hunks.append(hunk)
            hunk = None

        def flush_file() -> None:
            nonlocal current, hunks
            flush_hunk()
            if current is not None and hunks is not None:
                files.append(
                    DiffFile(
                        path=current["new_path"] or current["old_path"] or "",
                        old_path=current["old_path"],
                        hunks=tuple(
                            DiffHunk(
                                new_start=h["new_start"],
                                old_start=h["old_start"],
                                added_lines=tuple(h["added"]),
                                removed_lines=tuple(h["removed"]),
                                context_lines=tuple(h["context"]),
                                section_heading=h["section_heading"],
                            )
                            for h in hunks
                        ),
                        is_new_file=current["is_new"],
                        is_deleted_file=current["is_deleted"],
                        is_rename=current["is_rename"],
                    )
                )
            current = None
            hunks = None

        def open_file(old_path: str | None = None) -> None:
            nonlocal current, hunks
            flush_file()
            current = {
                "old_path": old_path,
                "new_path": None,
                "is_new": False,
                "is_deleted": False,
                "is_rename": False,
            }
            hunks = []

        for raw_line in diff_text.splitlines():
            if raw_line.startswith("diff --git "):
                open_file()
                continue
            if current is None:
                # Tolerate header-less unified patches: a ---/+++ pair opens a file.
                if raw_line.startswith("--- "):
                    open_file(_strip_prefix(raw_line[4:].split("\t")[0]))
                continue
            hunk_match = _HUNK_HEADER_RE.match(raw_line)
            if hunk_match is not None:
                flush_hunk()
                hunk = {
                    "old_start": int(hunk_match.group(1)),
                    "new_start": int(hunk_match.group(3)),
                    "added": [],
                    "removed": [],
                    "context": [],
                    "section_heading": hunk_match.group(5).strip(),
                }
                continue
            if hunk is None:
                self._consume_file_header(current, raw_line)
                continue
            if raw_line.startswith("+"):
                hunk["added"].append(raw_line[1:])
            elif raw_line.startswith("-"):
                hunk["removed"].append(raw_line[1:])
            elif raw_line.startswith("\\"):
                # "\ No newline at end of file" — belongs to neither side.
                continue
            else:
                hunk["context"].append(raw_line[1:] if raw_line.startswith(" ") else "")

        flush_file()

        if not diff_text.strip():
            raise ValueError("empty diff text")
        if not files:
            raise ValueError("no parsable file headers found in diff text")
        return PRDiff(files=tuple(files))

    @staticmethod
    def _consume_file_header(current: dict, raw_line: str) -> None:
        if raw_line.startswith("+++ "):
            current["new_path"] = _strip_prefix(raw_line[4:].split("\t")[0])
        elif raw_line.startswith("--- "):
            current["old_path"] = _strip_prefix(raw_line[4:].split("\t")[0])
        elif raw_line.startswith("new file mode"):
            current["is_new"] = True
        elif raw_line.startswith("deleted file mode"):
            current["is_deleted"] = True
        elif raw_line.startswith("rename to "):
            current["is_rename"] = True
            current["new_path"] = raw_line[len("rename to "):]
        elif raw_line.startswith("rename from "):
            current["is_rename"] = True
            current["old_path"] = raw_line[len("rename from "):]
        # index / mode / similarity lines carry no hunk content: ignored.

    def analyze(self, pr_diff: PRDiff) -> tuple[FileAnalysis, ...]:
        """Run AST analysis on the added side of every file in the diff."""
        return tuple(self.analyze_file(diff_file) for diff_file in pr_diff.files)

    def analyze_file(self, diff_file: DiffFile) -> FileAnalysis:
        """Extract symbols and masked-AST subtree fingerprints for one file."""
        language = _language_for(diff_file.path)
        added_text = diff_file.added_text
        if not added_text.strip() or diff_file.is_deleted_file:
            return FileAnalysis(
                path=diff_file.path, language=language, added_text=added_text, parseable=False
            )
        if language != "python":
            # Structural equivalence is only implemented for Python ASTs;
            # other languages cause abstention at the gate.
            return FileAnalysis(
                path=diff_file.path, language=language, added_text=added_text, parseable=False
            )
        module = self._parse_fragment(added_text)
        if module is None:
            return FileAnalysis(
                path=diff_file.path, language=language, added_text=added_text, parseable=False
            )
        symbols = tuple(
            node.name
            for node in module.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        )
        return FileAnalysis(
            path=diff_file.path,
            language=language,
            added_text=added_text,
            parseable=True,
            top_level_symbols=symbols,
            subtree_fingerprints=subtree_fingerprints(module),
        )

    @staticmethod
    def _parse_fragment(added_text: str) -> ast.Module | None:
        """Parse an added-lines fragment; returns None when unparseable.

        Fragments lifted from the middle of a file may carry indentation, so
        a dedented retry is attempted before giving up (and abstaining).
        """
        for candidate in (added_text, textwrap.dedent(added_text)):
            try:
                return ast.parse(candidate)
            except (SyntaxError, ValueError):
                continue
        return None

    @staticmethod
    def fingerprint_construct(code: str) -> str:
        """Public helper for building anchor fixtures: fingerprint a snippet."""
        return structural_fingerprint(code)

    def parse_and_analyze(self, diff_text: str) -> PRDiff:
        """Convenience: parse a diff and attach AST analyses to it."""
        pr_diff = self.parse(diff_text)
        return PRDiff(files=pr_diff.files, analyses=self.analyze(pr_diff))
