# Contributing to Biscuit

Thanks for your interest in contributing to Biscuit! This document describes the
practical workflow, conventions, and gates that every change must pass. It
reflects how the core team works on the project — if anything here is unclear,
please open an issue labeled `question`.

## Table of contents

1. [Code of Conduct](#code-of-conduct)
2. [Quick path for a small change](#quick-path-for-a-small-change)
3. [Development environment](#development-environment)
4. [Branching and commit conventions](#branching-and-commit-conventions)
5. [Pull request workflow](#pull-request-workflow)
6. [CI gates](#ci-gates)
7. [Stream-scoped changes](#stream-scoped-changes)
8. [Style and testing requirements](#style-and-testing-requirements)
9. [Updating documentation](#updating-documentation)
10. [Releasing](#releasing)

---

## Code of Conduct

Participation in this project is governed by the
[Code of Conduct](CODE_OF_CONDUCT.md). By participating you agree to abide by
its terms. Please report any unacceptable behavior to the maintainers
privately.

---

## Quick path for a small change

```bash
# 1. Fork on GitHub, then:
git clone https://github.com/<your-username>/biscuit.git
cd biscuit
git remote add upstream https://github.com/MRiARC/biscuit.git

# 2. Branch off the latest main
git checkout -b fix/my-bug-fix main

# 3. Set up the dev environment
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,grounding]"

# 4. Make your change, then verify locally
ruff check .
pytest -q

# 5. Commit using Conventional Commits (see below)
git commit -m "fix(watchdog): abstain when ruling author is revoked"

# 6. Push and open a PR against main
git push -u origin fix/my-bug-fix
```

---

## Development environment

| Requirement | Version |
|-------------|---------|
| Python | 3.11 or 3.12 (3.12 recommended) |
| PostgreSQL | 16+ with `pgvector` (optional — only for the live-PG integration tests) |
| Node.js | 22 (only for changes under `intro/`) |
| Ollama or equivalent | optional — only for live LLM inference; tests use recorded replay |

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,grounding]"
```

To exercise the strict tree-sitter grammars locally:

```bash
BISCUIT_REQUIRE_TREE_SITTER=1 pytest -q tests/test_grounding.py
```

To exercise the live PostgreSQL integration tests:

```bash
export TEST_POSTGRES_URL="postgresql://biscuit:biscuit@localhost:5432/biscuit"
pytest tests/test_db.py
```

---

## Branching and commit conventions

### Branch naming

Use a short, descriptive branch name prefixed with the change type:

```
feat/<short-scope>      # new feature
fix/<short-scope>       # bug fix
docs/<short-scope>      # documentation only
build/<short-scope>      # CI / packaging / dependency changes
test/<short-scope>      # test-only changes
chore/<short-scope>     # housekeeping with no behavior change
```

Examples: `feat/watchdog-edit-rationale`, `fix/switch-callback-ttl`,
`docs/readme-and-contributing`.

### Conventional Commits

Every commit message follows [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<scope>): <imperative summary in lowercase>

<optional body explaining why, not what>

<optional footer: Closes #123, BREAKING CHANGE: ...>
```

Allowed `type`s: `feat`, `fix`, `docs`, `build`, `test`, `chore`, `refactor`,
`perf`, `style`, `ci`. Use the imperative mood in the summary
(`add`, not `added` / `adds`).

When a PR closes an issue, include `Closes #NN` in the body or PR description
so GitHub auto-closes it on merge.

---

## Pull request workflow

1. **Open against `main`** from a feature branch on your fork.
2. **Fill in the PR template.** All sections are required — particularly the
   testing checklist and the AI Coding Assistants disclosure (the hackathon
   this project was born from requires it; we keep it as a transparency norm).
3. **CI must be green.** The `CI` workflow runs `ruff` and `pytest` on Python
   3.11 and 3.12. If your change touches `intro/`, the `Intro App CI` workflow
   also runs `npm run typecheck` and `npm run build` on Node 22.
4. **Two-reviewer approval.** Biscuit uses a two-reviewer gate: every PR
   requires approval from at least two collaborators who did not author the
   PR. For stream-scoped changes (see below), at least one approver must be
   from outside the stream's owning team.
5. **Squash-merge.** All PRs are squash-merged into `main`. The squash commit
   title should be the Conventional Commits summary, optionally followed by
   `(Closes #NN)` or `(#NN)` referencing the PR or issue.

If a reviewer requests changes, push follow-up commits to the same branch
rather than force-pushing a rewrite — this keeps the review trail readable.
Force-pushes are acceptable only when rebasing a stacked PR onto new `main`.

---

## CI gates

The following checks must pass before a PR can merge:

| Workflow | What it checks | When it runs |
|----------|----------------|--------------|
| `CI` (`.github/workflows/ci.yml`) | `ruff check .`, `pytest -q` on Python 3.11 and 3.12; strict tree-sitter mode for `tests/test_grounding.py` | every push and PR to `main` |
| `Intro App CI` (`.github/workflows/intro.yml`) | `npm install`, `npm run typecheck`, `npm run build` on Node 22 | pushes and PRs that touch `intro/**` or `.github/workflows/intro.yml` |
| `Release` (`.github/workflows/release.yml`) | Builds `sdist` + `wheel`, publishes to PyPI via Trusted Publishing | on tag push matching `v*` |

You can reproduce the `CI` workflow locally before pushing:

```bash
ruff check .
pytest -q
BISCUIT_REQUIRE_TREE_SITTER=1 pytest -q tests/test_grounding.py
```

The 4 skipped tests in the default suite require a live PostgreSQL DSN
(`TEST_POSTGRES_URL`); they are skipped by design and are not blockers.

---

## Stream-scoped changes

Biscuit is organized into five implementation streams, each with an owning
collaborator. When making a change that touches a stream's exclusive files,
respect the stream's ownership boundaries documented in
[`docs/TWO_PILLAR_ARCHITECTURE.md`](docs/TWO_PILLAR_ARCHITECTURE.md):

| Stream | Owning area | Default reviewer |
|--------|-------------|-------------------|
| 1 — Core models & storage | `src/biscuit/models/`, `src/biscuit/db/`, `src/biscuit/ingest/` | `@mriarc-08` |
| 2 — Code grounding & lineage | `src/biscuit/grounding/` | `@utksh1` |
| 3 — Extraction & provenance | `src/biscuit/extract/`, `src/biscuit/provenance/` | `@daksheshsharma2409` |
| 4 — Watchdog & Switch bridge | `src/biscuit/watchdog/`, `src/biscuit/switch/` | `@mriarc-08` |
| 5 — Evaluation & CLI | `src/biscuit/evaluate/`, `src/biscuit/cli/` | `@utksh1` |

A change to one stream's exclusive files should not modify another stream's
files. Cross-stream changes (e.g. a `pyproject.toml` dependency addition that
multiple streams need) should be coordinated in the PR description and may be
split into stacked PRs to keep reviews focused.

---

## Style and testing requirements

### Python style

- **Formatter / linter:** `ruff` with `target-version = "py311"` and
  `line-length = 100` (set in `pyproject.toml`).
- **Type hints** are expected on all new public functions.
- **No silent `except:`** — catch specific exceptions and record a verdict
  (the watchdog authorization gate abstains rather than guessing; follow
  that pattern).

### Tests

- New behavior ships with tests. The suite is organized by stream:
  `tests/test_models.py`, `tests/test_grounding.py`, `tests/test_extract.py`,
  `tests/test_watchdog.py`, `tests/test_switch.py`, `tests/test_evaluate.py`,
  `tests/test_cli.py`, plus focused sub-modules (`test_extract_audit.py`,
  `test_provenance.py`, `test_review_blockers.py`,
  `test_evaluate_acceptance_gates.py`, `test_evaluate_provenance.py`,
  `test_evaluate_embeddings.py`, `test_evaluate_memory_profile.py`).
- Tests run **without** a live LLM, **without** a live PostgreSQL, and
  **without** network access by default. Use the recorded-replay fixtures
  under `tests/fixtures/`. Live integration tests gate on an env var
  (`TEST_POSTGRES_URL`) so they are skipped, not failed, in CI.
- Security-sensitive code (HMAC verification, replay prevention, audit-log
  immutability, watchdog fail-closed semantics) must include negative-path
  tests: tampered body, wrong secret, expired TTL, finalized-request 409,
  cross-alternative mismatch, revoked ruling author, all-zero head SHA, etc.

### Grounding tests

If your change touches `src/biscuit/grounding/` or adds new tree-sitter
grammar support, also exercise the strict mode locally:

```bash
BISCUIT_REQUIRE_TREE_SITTER=1 pytest -q tests/test_grounding.py
```

CI runs this same invocation; do not let optional-grammar tests silently skip
in strict mode.

---

## Updating documentation

Design specs live under `docs/` and are the authoritative source for
architecture decisions. When you change behavior in a way that affects the
spec, update the matching doc in the same PR:

| Change area | Update this doc |
|-------------|-----------------|
| Storage schema, pgvector config | `docs/STORAGE_AND_RETRIEVAL.md` |
| Ingestion adapters, 4-pass pipeline | `docs/INGESTION_AND_EXTRACTION.md` |
| Grounding, AST anchors, lineage | `docs/CODE_GROUNDING_AND_AST.md` |
| Evaluation, fixtures, metric contracts | `docs/EVALUATION_AND_BENCHMARKS.md` |
| Watchdog, Switch bridge, callback security | `docs/LIVING_DECISION_INTELLIGENCE.md` |
| Cross-cutting architecture | `docs/TWO_PILLAR_ARCHITECTURE.md` |
| Tech stack choices | `docs/TECH_STACK.md` |

If your change introduces user-visible behavior, also update the
`CHANGELOG.md` entry under the `[Unreleased]` section.

---

## Releasing

Releases are cut by tagging `main`:

```bash
git checkout main && git pull
git tag -a v0.X.Y -m "v0.X.Y — <short summary>"
git push origin v0.X.Y
```

The `Release` workflow then builds the `sdist` and `wheel`, creates a GitHub
Release with auto-generated notes, and publishes to PyPI via Trusted
Publishing (no long-lived API tokens). Update `CHANGELOG.md` with the release
date and copy the `[Unreleased]` block to a new `## [v0.X.Y] — YYYY-MM-DD`
section before tagging.
