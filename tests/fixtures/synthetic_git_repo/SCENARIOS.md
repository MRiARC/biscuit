# Synthetic-Git Grounding Regression Contract

The fixture directory is a **scenario manifest**, not a checked-in `.git` directory. Each
`tests/test_grounding.py` test generates a self-contained temporary Git repository using
`git init`; this ensures deterministic IDs within each test's own commit graph and avoids
shipping mutable repository state.

| Scenario | Test | Invariant |
|---|---|---|
| exact filename rename (`-M80%`) | `test_forward_rename_and_immutable_mapping_evidence` | `RENAME_M` and new relative path |
| Git similarity score R081 vs R079 | `test_git_similarity_boundary_79_vs_81` | threshold is 80%, not 79% |
| Python symbol moved across files | `test_symbol_moves_into_another_module_without_git_file_rename` | require structural identity |
| copying while original survives | `test_copy_with_original_remaining_requires_abstention` | `AMBIGUOUS`, no automatic migration |
| Git-detected copy | `test_exact_git_copy_is_classified_copy_c` | `COPY_C` event recorded |
| total rewrite / lost symbol | `test_file_rewrite_without_symbol_is_classified_and_deleted` | `REWRITE_SPLIT_B` is a conservative **symbol-vanish heuristic**, not a Git `B` name-status (Git 2.47 `--name-status` does not emit `B`) |
| conflict resolved to first parent | `test_conflict_mainline_selected_with_combined_diff_evidence` | only select P1 if resulting bytes/symbol remain in merge tree |
| conflicting third implementation | `test_combined_diff_conflict_rewrite_abstains` | abstain, don't silently accept path continuity |
| diverging parent renames | `test_divergent_merge_paths_are_ambiguous` | unknown confidence and stop automated migration |
| multi-parent octopus merge | `test_octopus_merge_inspects_all_three_parents` | record *all* parent comparisons, including first parent |
| C function + file rename | `test_c_symbol_renamed_through_forward_git_lineage` | C lineage uses same forward Git algorithm |
| comprehension/genexp/walrus bindings | `test_comprehension_and_walrus_rename_invariance` | capture bindings before use |
| fallback C literal/comment braces | `test_c_fallback_ignores_braces_in_literals_and_comments` | exact locators despite quotes/comments |
| edit then move below Git threshold | `test_edit_then_move_uses_latest_verified_symbol_exemplar` | exemplar follows verified edits |
| one-sided clean merge | `test_clean_merge_with_only_mainline_edit_never_claims_first_parent_selection` | no false conflict choice |
| post-deletion unrelated change | `test_dead_lineage_has_no_current_state_on_later_unrelated_commit` | no false CURRENT event |
| sanitized local Git environment | `test_git_environment_does_not_redirect_lineage` | ambient Git variables cannot redirect reads |
| compact audit replay | `test_noop_history_compacts_lineage_events_but_preserves_replay_digest` | transitions + commit DAG digest |
| stream-1 enum vocabulary | `test_storage_transition_labels_match_stream_one_enum` | storage mapper uses canonical values |

| Python and C real Tree-sitter | `test_real_tree_sitter_both_grammars_and_git_lineage` | both grammars and parser-proven lineage with strict opt-in |
| Tier-1 anchor model contract | `test_anchor_contract_serialization_and_authority_scope` | DB-bound alternative and exact schema keys |
| DDL lineage record mapping | `test_lineage_storage_records_cover_canonical_ddl` | actor-bound anchor ID, target commit, forward strategy, UTC timestamp |

**Ruling requested — first-parent exception:** When merge resolution entirely removes a
historical symbol from the merge tree, the result is `DELETED`; it must **not** resurrect
P1's deleted symbol. If the merge tree retains P1's bytes exactly, a
`first_parent_selected` evidence flag is emitted only if both parents changed the
symbol relative to their merge base (a clean one-sided merge is not a human conflict choice). This is a safety-motivated deviation
from issue §D.2 and needs explicit maintainer acceptance.

**Integration boundaries:** Stream 1 owns SQLModel database transactions/persistence,
Stream 2 owns pure anchor/lineage record production, and the watchdog stream must
consume `LineageResult.safe_for_automatic_migration` as an additional mandatory
*authorization* gate. Neither Git file similarity nor AST equality by itself grants a
maintainer-authoritative warning. Performance optimization/caching should be added only
with a CPython-scale benchmark and without weakening provenance/abstention semantics.
**Copy-scan performance:** Exhaustive Git `--find-copies-harder` is opt-in via
`trace_lineage(..., find_copies_harder=True)`. Default traversal retains
`-M80% -C -B` and structurally verifies touched-file moves/copies.
Single-parent no-op records are suppressed; transition evidence includes a
SHA-256 digest and count of the traversed commit IDs for replay. This avoids
per-anchor persisted no-op rows but does not establish CPython-scale throughput.

**Python fingerprint provenance:** AST dumps vary by CPython interpreter;
both Python stdlib and hybrid Tree-sitter+stdlib backends record interpreter
version in `parser_version`. Cross-version fingerprints are not comparable
without re-parsing the input under the same Python interpreter.
