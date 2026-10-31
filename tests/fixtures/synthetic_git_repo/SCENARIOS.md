# Synthetic-Git Grounding Regression Contract

The fixture directory is a **scenario manifest**, not a checked-in `.git` directory. Each
`tests/test_grounding.py` test generates a self-contained temporary Git repository using
`git init`; this ensures deterministic IDs within each test's own commit graph and avoids
shipping mutable repository state.

| Scenario | Test | Invariant |
|---|---|---|
| exact filename rename (`-M80%`) | `test_forward_rename_is_detected` | `RENAME_M` and new relative path |
| Git similarity score R081 vs R079 | `test_git_similarity_boundary_79_vs_81` | threshold is 80%, not 79% |
| Python symbol moved across files | `test_symbol_moves_between_files` | require structural identity |
| copying while original survives | `test_copy_with_original_remaining_requires_abstention` | `AMBIGUOUS`, no automatic migration |
| Git-detected copy | `test_exact_git_copy_is_classified_copy_c` | `COPY_C` event recorded |
| total rewrite / lost symbol | `test_file_rewrite_without_symbol_is_classified_and_deleted` | `REWRITE_SPLIT_B` is a conservative **symbol-vanish heuristic**, not a Git `B` name-status (Git 2.47 `--name-status` does not emit `B`) |
| conflict resolved to first parent | `test_conflict_mainline_selected_with_combined_diff_evidence` | only select P1 if resulting bytes/symbol remain in merge tree |
| conflicting third implementation | `test_combined_diff_conflict_rewrite_abstains` | abstain, don't silently accept path continuity |
| diverging parent renames | `test_divergent_merge_paths_are_ambiguous` | unknown confidence and stop automated migration |
| multi-parent octopus merge | `test_octopus_merge_inspects_all_three_parents` | record *all* parent comparisons, including first parent |
| C function + file rename | `test_c_symbol_renamed_through_forward_git_lineage` | C lineage uses same forward Git algorithm |
| Python and C real Tree-sitter | `test_real_tree_sitter_both_grammars_and_git_lineage` | both grammars and parser-proven lineage with strict opt-in |
| Tier-1 anchor model contract | `test_anchor_contract_serialization_and_authority_scope` | DB-bound alternative and exact schema keys |
| DDL lineage record mapping | `test_lineage_storage_records_cover_canonical_ddl` | actor-bound anchor ID, target commit, forward strategy, UTC timestamp |

**Ruling requested — first-parent exception:** When merge resolution entirely removes a
historical symbol from the merge tree, the result is `DELETED`; it must **not** resurrect
P1's deleted symbol. If the merge tree retains P1's bytes exactly, a
`first_parent_selected` evidence flag is emitted. This is a safety-motivated deviation
from issue §D.2 and needs explicit maintainer acceptance.

**Integration boundaries:** Stream 1 owns SQLModel database transactions/persistence,
Stream 2 owns pure anchor/lineage record production, and the watchdog stream must
consume `LineageResult.safe_for_automatic_migration` as an additional mandatory
*authorization* gate. Neither Git file similarity nor AST equality by itself grants a
maintainer-authoritative warning. Performance optimization/caching should be added only
with a CPython-scale benchmark and without weakening provenance/abstention semantics.