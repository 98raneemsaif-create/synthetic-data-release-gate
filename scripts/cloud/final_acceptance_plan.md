# Milestone 4 final cloud acceptance plan

This is a runbook only. It is not an authorization to execute BigQuery DDL or
DML. The current project is `synthetic-release-gate-2026`, location `US`, and
the five permanent tables already exist. Do not recreate, replace, or delete
them.

## Fixed test identifiers and fixture

Use a newly generated unique suffix after approval, preserving these prefixes:

- `srg-cloud-ambiguous-20261113-01`
- `srg-cloud-audit-failure-20261113-01`

The fixture must be the existing functional pipeline-only APPROVED fixture:
30 customers, 64 orders, and 167 order items, with verified reference and
candidate hashes, unchanged policy, `dcr_enabled: false`, and an explicit
`NO_PRIVACY_CERTIFICATION` disclaimer. Use the verified in-memory snapshot for
all publication attempts; never reread candidate tables.

## Test A: ambiguous COMMIT recovery

1. As `srg-publisher`, write the idempotent `PUBLICATION_INTENT` audit event.
2. Submit the existing parameterized transaction once, including the lock-row
   mutation, all three inserts, the COMMITTED manifest, and the success audit.
3. Record the submitted BigQuery job ID, then deliberately discard the client
   response after submission (the harness must not call a second publication
   before reconciliation).
4. Reconcile that original job ID and query the authoritative manifest as
   `srg-publisher`. Require one COMMITTED manifest with the expected hashes and
   counts of 30/64/167.
5. Invoke the normal retry path using the same immutable plan. It must return
   `already_committed` and insert no data or success audit.
6. As `srg-consumer`, read only rows joined to a COMMITTED manifest and compare
   the reconstructed snapshot hash with the manifest content hash.

Assertions: exactly one manifest, one `PUBLICATION_COMMITTED` event, no
duplicate rows, one successful BigQuery transaction, and idempotent recovery
from the lost response rather than a normal second publish.

## Test B: mandatory success-audit failure

1. Use the second release ID and write its independent intent.
2. Execute a test-only transaction variant with the same lock and three data
   inserts, but bind deliberately malformed JSON to the
   `PARSE_JSON(@verification_json)` expression in the mandatory success-audit
   INSERT. This forces the audit write to fail before `COMMIT`; do not alter
   permanent table schemas or use `CREATE OR REPLACE`.
3. Require the job to fail and query counts as `srg-publisher`: zero candidate
   rows, no COMMITTED manifest, and no success audit event.
4. Attempt an independent `PUBLICATION_FAILED` audit after rollback. Record
   whether it succeeds; its presence never authorizes publication.

## Bounds, identities, and cleanup

All query jobs use location `US` and `maximumBytesBilled=104857600`; stop if a
dry-run estimate exceeds 100 MiB. Operator ADC is used only for prior approved
administrative work. Publication and audit DML use impersonated
`srg-publisher`; readback uses impersonated `srg-consumer`.

After evidence is captured, delete only rows whose `release_id` is exactly one
of the two fixed test IDs from the three published tables and manifest. Never
delete `SYSTEM_LOCK`, permanent tables, unrelated releases, or audit history.
Retain audit history and record the test lifecycle. If any dependent assertion
fails, stop and preserve diagnostics and remaining test rows.

The real cloud tests are still required to establish lost-response behavior,
audit-write rollback, BigQuery job reconciliation, and actual transaction
visibility. Local tests cannot prove those properties.
