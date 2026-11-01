# Security Policy

## Reporting a vulnerability

**Please do not open a public GitHub issue for security vulnerabilities.**

Instead, report vulnerabilities privately using one of these channels:

1. **Preferred:** use GitHub's
   [private vulnerability reporting](https://github.com/MRiARC/biscuit/security/advisories/new).
   This keeps the report encrypted in transit, limits visibility to the
   maintainers, and lets us coordinate a fix and disclosure together.
2. **Fallback:** email the maintainers at
   **biscuit-security@lists.example.com** with a description of the issue, the
   affected version, and a minimal reproduction if you have one.

Please include the following in your report:

- **Affected component** — e.g. `src/biscuit/switch/callback_security.py`,
  `src/biscuit/watchdog/gate.py`, `src/biscuit/provenance/verifier.py`.
- **Affected version(s)** — the commit SHA or release tag you tested against.
- **Reproduction** — minimal steps or a script. If you have a fix idea,
  include it; you'll be credited in the advisory.
- **Impact** — what an attacker could accomplish (RCE, auth bypass,
  audit-log tampering, false-positive watchdog warning, etc.).

## Response timeline

| Step | Target |
|------|--------|
| Acknowledge receipt | within 48 hours |
| Initial assessment + triage | within 5 business days |
| Fix or mitigation published | within 30 days for high-severity; 90 days for low-severity |
| Coordinated public disclosure | after a fix is released, on a date we agree on with the reporter |

We will credit reporters in the release notes and the GitHub Security Advisory
unless you ask to remain anonymous.

## Supported versions

Only the latest minor release line receives security fixes. The project is
pre-1.0, so breaking changes are possible between minor versions; we will
back-port security fixes to the previous minor for 30 days after a new minor
ships.

| Version | Supported |
|---------|-----------|
| 0.1.x | ✅ |
| < 0.1 | ❌ |

## Security scope

Biscuit is a decision-intelligence platform with several security-sensitive
surfaces. Reports in the following areas are especially welcome:

### 1. Switch FastMCP callback security (`src/biscuit/switch/callback_security.py`)

- HMAC-SHA256 signature verification over raw callback bodies
- Constant-time comparison
- UUIDv4 idempotency keys and replay prevention (409 Conflict semantics)
- 72-hour TTL expiry on signed callbacks
- Request/action/payload/idempotency compatibility binding

A bypass that lets an attacker forge a callback, replay a finalized request,
or extend TTL past expiry is a high-severity issue.

### 2. PR Diff Watchdog authorization gate (`src/biscuit/watchdog/gate.py`)

The watchdog authorizes a GitHub review comment **only** when all four
invariants pass:

1. Decision `lifecycle_status == 'REJECTED'`
2. Authoritative ruling resolves to a `VERIFIED_EXACT` `RULING` span under a
   verified project-scoped human quorum (`MODEL_INFERRED` never authorizes)
3. The diff touches an active `CURRENT`/`MIGRATED` code anchor of *this*
   alternative with unambiguous parent-aware lineage
4. The masked-AST fingerprint of the rejected construct is present in the
   added code

A regression that lets a warning fire on `MODEL_INFERRED` authority, against
a different alternative, against a non-existent or all-zero head SHA, or
without a confirmed side effect is a high-severity issue. Any path that
**claims** `HITL_CARD_POSTED` or `COMMENT_PUBLISHED` without an actual
confirmed side effect is also high-severity.

### 3. Evidence provenance and audit log (`src/biscuit/provenance/`,
   `src/biscuit/db/migrations/`)

- Wire-byte SHA-256 over the exact decoded UTF-8 content of source messages
- Zero-based Unicode code-point offsets into the decoded string
- Append-only `source_message_versions`, `extraction_runs`, and
  `pr_watchdog_audit_log` tables

A path that lets an attacker mutate an append-only row, forge a SHA-256
digest, or insert a `VERIFIED_EXACT` evidence span without the underlying
source bytes is a critical issue.

### 4. AST normalization and lineage (`src/biscuit/grounding/`)

- Masked-AST fingerprints must survive variable renames and reformatting
  **without** producing false matches across semantically distinct
  constructs (e.g. `coalesce(a, b)` must not match a ternary expression)
- Forward commit-DAG lineage must not produce a false active-anchor match
  across repositories or against a revoked ruling author

A regression that produces a false-positive structural match, a cross-
repository anchor match, or binds a decision to the wrong code path is a
medium- to high-severity issue depending on blast radius.

### 5. CLI and local store (`src/biscuit/cli/`, `src/biscuit/evaluate/`)

- The CLI never exposes withheld oracle text to a runtime predictor
- The local SQLite audit store is not the authoritative decision store;
  verified recovered decisions require the storage/extraction integration
- `--no-simulate` must fail closed rather than fabricate authorization

A path that leaks withheld ground truth to a predictor, or that lets a
fixture-replay mode authorize a real warning without an explicit opt-in, is
a high-severity issue.

## Out of scope

The following are **not** security issues:

- Bugs in third-party dependencies (report upstream). We will still bump the
  dependency if a fix is available.
- Theoretical timing side channels in non-cryptographic code paths.
- The 8 GB memory budget being exceeded — that is a performance and
  acceptance concern tracked in `docs/TECH_STACK.md`, not a security
  vulnerability.
- The synthetic test fixtures under `tests/fixtures/` — they are clearly
  labeled `SYNTHETIC_REPLAY_NOT_HISTORICAL` and contain no real user data.

If you are unsure whether something is in scope, err on the side of
reporting it privately.

## Hardening notes for self-hosters

If you self-host Biscuit:

- Run the Switch FastMCP server behind TLS. The HMAC verification protects
  integrity but not confidentiality of the callback body.
- Rotate the HMAC secret and the Switch activity-client token on a regular
  schedule. Both are injected as environment variables; never commit them.
- Run the watchdog in `AUDIT_DRY_RUN` mode until you have reviewed at least
  one full cycle of audit-log entries. Switch to `INTERACTIVE_HITL` before
  enabling `PRODUCTION_COMMENT`.
- Back up the PostgreSQL `pr_watchdog_audit_log`, `extraction_runs`, and
  `source_message_versions` tables. They are append-only; do not truncate
  them to "save space" — they are the audit trail.
