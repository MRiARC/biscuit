# Bootstrap fixtures

JSON fixtures consumed by `biscuit db bootstrap-roles --file <fixture>` and
`biscuit db import-decision --file <fixture>`.

## Format

```jsonc
{
  "contributors": {
    "<handle>": {
      "source_platform": "discourse",         // or github, mbox, slack, ...
      "platform_user_id": "<platform id>",
      "canonical_name": "<full name>",
      "author_handle": "<handle>",
      "email_prefix": "<optional, for mbox>"
    }
  },
  "role_grants": [
    {
      "contributor": "<handle>",
      "project_context": "python/cpython",
      "role": "BDFL",                          // BDFL | Core Maintainer | Working Group Chair | Working Group Member | Contributor
      "verification_source_url": "<proof URL>",
      "verified_by": "<handle>",
      "valid_from": "2024-01-01T00:00:00+00:00",
      "valid_until": null                     // or ISO 8601 datetime
    }
  ],
  "decisions": [
    {
      "project_context": "python/cpython",
      "proposer": "<handle>",
      "ruling_actor": "<handle>",
      "ruling_actor_grant": "<UUID of a role_grant created above>",
      "thread": {
        "source_platform": "DISCOURSE",
        "external_thread_id": "505",
        "title": "PEP 505: ?-aware default-aware operators",
        "canonical_url": "https://discuss.python.org/t/505"
      },
      "decision": { "title": "...", "context_summary": "..." },
      "alternative": {
        "proposal_text": "coalesce(*args)",
        "rejection_summary": "eager evaluation",
        "rejection_category": "PERFORMANCE",   // PERFORMANCE | SYNTAX_AMBIGUITY | BACKWARD_COMPAT | COMPLEXITY | SECURITY | PHILOSOPHICAL
        "ruling_authority": "BDFL"              // BDFL | CORE_MAINTAINER_CONSENSUS | WORKING_GROUP
      },
      "ruling": {
        "raw_content": "<exact source message text>",
        "exact_quote": "<substring of raw_content>",
        "start_char": 0, "end_char": 123,
        "permalink": "https://discuss.python.org/t/505/1",
        "original_timestamp": "2024-01-15T10:00:00+00:00"
      },
      "signoffs": [
        {
          "contributor": "<handle>",
          "grant": "<UUID of a role_grant>",
          "evidence": { /* same shape as `ruling` */ }
        }
      ]
    }
  ]
}
```

## Provenance contract

- `raw_content` is the exact decoded UTF-8 bytes of the source message.
- `exact_quote` must equal `raw_content[start_char:end_char]` (zero-based, end
  exclusive). The bootstrap verifies this substring + SHA-256 contract before
  writing, so a mismatched span cannot be stored even on SQLite (which lacks
  the `verify_evidence_span_exactness` PostgreSQL trigger).
- `ruling_actor_grant` must reference a `role_grant` created earlier in the
  same fixture (or already present in the DB). The grant's `role` must match
  the `ruling_authority`:
  - `BDFL` authority requires the grant's role to be `BDFL`.
  - `CORE_MAINTAINER_CONSENSUS` requires the ruling actor's grant to be
    `Core Maintainer`, plus one additional distinct `Core Maintainer` sign-off.
  - `WORKING_GROUP` requires the ruling actor's grant to be
    `Working Group Chair`, plus one distinct `Working Group Member` sign-off.

## Files

- `sample_roles.json` — two contributors + a BDFL grant + a Core Maintainer
  grant. Use as a starting point for bootstrapping the watchdog's invariant 2
  (authoritative ruling) in a fresh environment.
