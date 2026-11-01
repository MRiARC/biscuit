# Synthetic Git lineage test fixture

`tests/test_grounding.py` creates isolated temporary Git repositories using
`git init` and local user identity (no remote, network or credentials).
Scenarios cover 80%-similarity renames, symbol moves, clean two-parent merges,
divergent-parent merges, copies/splits and non-ancestor traversal.

The fixture is generated at test runtime so its commit IDs are reproducible
from the test setup but no `.git` directories or binary Git objects are checked
into this repository.