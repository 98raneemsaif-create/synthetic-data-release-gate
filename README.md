# Synthetic Data Release Gate

Synthetic Data Release Gate generates related synthetic tables, measures
quality and risk, evaluates a versioned YAML policy, and blocks publication
unless the evidence is internally consistent and the resulting decision is
`APPROVED`.

### Project Background

**Synthetic Data Release Gate** is an independent project developed during the **Data Engineering on Google Cloud Bootcamp** at [Tuwaiq Academy](https://github.com/Tuwaiq-Academy-Training), Saudi Arabia.

The project was designed and implemented as a practical data engineering solution, focusing on synthetic data evaluation, policy-based release decisions, and controlled publication on Google Cloud.

## Architecture and data flow

```text
reference.py
    -> generate.py / HMASynthesizer
    -> private candidate CSVs + generation_manifest.json
    -> measure.py / measured checks
    -> measurement_report.json + policy hash + lineage evidence
    -> publisher.py: snapshot, hard checks, hashes, policy re-evaluation
    -> candidate_private -> BigQuery transaction
       -> published tables + COMMITTED release_manifest + audit event
    -> consumer reads only rows joined to a COMMITTED manifest
```

The fixed relational schema is `customers -> orders -> order_items`. SDV
metadata must contain exactly the two required relationships. Candidate
generation never approves or publishes data. The policy engine returns
`APPROVED`, `REVIEW_REQUIRED`, `REJECTED`, or `ERROR`; hard-check failures,
missing evidence, and measurement errors fail closed.

## Google Cloud boundary

The verified project is `synthetic-release-gate-2026` in BigQuery location
`US`. The four datasets are:

- `source_private`: evaluator read access; no publisher or consumer access.
- `candidate_private`: evaluator writes; publisher reads; no consumer access.
- `published_synthetic`: publisher writes; consumer reads; evaluator has no
  data access.
- `audit_private`: publisher writes audit events; evaluator and consumer have
  no data access.

The service accounts are:

- `srg-evaluator`: reads source data and writes candidate evidence/data.
- `srg-publisher`: reads candidate evidence and writes approved releases and
  audit events.
- `srg-consumer`: reads published data.

Each account receives only the required project-level BigQuery job execution
permission and dataset-level data permissions. No service-account keys are
used. The publisher and consumer paths use short-lived impersonated
credentials; administrative table provisioning uses the operator identity.

## Evidence handoff and trust boundary

The evaluator handoff contains the generation/evaluation IDs, source
parameters and reference hash, candidate content hash, policy version/hash,
measurements and configuration, SDV/RDT versions, decision, reasons, and
timestamps. The publisher loads candidate tables once into a stable snapshot,
recomputes source-independent hard checks, validates the evidence, recomputes
the policy decision, and compares it with the recorded decision.

The canonical SHA-256 envelope includes table identity, schema and column
types, row counts, normalized values, and sorted rows. Row order is ignored;
duplicate-row multiplicity is retained. `order_items.price` is validated as
BigQuery `NUMERIC(38,9)` without arbitrary rounding. The reference hash is
bound to its generation parameters; candidate hashes are bound to the
generation run. Missing or legacy provenance is not release-eligible.

Hashing detects stale or modified snapshots but does not authenticate the
evaluator. The evaluator is trusted for source-dependent fidelity and risk
measurements; the publisher does not have source-data access to independently
recompute them.

## Atomic publication and audit

Permanent schemas and SQL are documented in
[publisher_schema.sql](./scripts/cloud/publisher_schema.sql) and
[publish_transaction.sql](./scripts/cloud/publish_transaction.sql). The
publisher supplies bounded `ARRAY<STRUCT>` parameters from the verified
in-memory snapshot; it never rereads mutable candidate tables.

The transaction:

1. Mutates the shared `SYSTEM_LOCK` row in `release_manifest` to serialize
   publication attempts.
2. Checks the requested `release_id` after lock acquisition.
3. Inserts all three release-tagged data tables.
4. Inserts the `COMMITTED` `release_manifest` row.
5. Inserts the success audit event across `published_synthetic` and
   `audit_private`.
6. Commits only when every mandatory operation succeeds.

Permanent DDL is outside the transaction. The bounded retry layer waits at
least 60 seconds before a real transient retry, reconciles ambiguous jobs
against the original job and authoritative manifest, returns an idempotent
result for matching committed releases, rejects conflicting hashes, and
fails closed when the outcome is unknown. BigQuery does not enforce primary
key uniqueness; the application validates it.

The manifest is the authoritative logical release marker. Dataset-wide
`dataViewer` does not prevent direct reads of underlying tables, so the
manifest is a consumer contract, not an IAM security boundary. Audit storage
is not claimed to be immutable or append-only. Historical audit rows may
remain after fixture data cleanup so the lifecycle and failure evidence stay
auditable.

## Milestone completion summary

### M1 — Real multi-table generation

Real SDV 1.17.0 / RDT 1.14.0 `HMASynthesizer` generation was verified for the
three-table relationship chain, metadata validation, keys, referential
integrity, required columns, and reproducible seeded tests. The CLI exports
private, unapproved candidates and never falls back silently from SDV.

### M2 — Measured quality and risk

Fidelity is measured per table from real reference and synthetic data. Exact
match risk uses behavioral features rather than generated identifiers. The
policy engine consumes measured results and fails closed for invalid or
missing measurements. DCR is disabled.

### M3 — Cloud infrastructure and IAM

The four datasets and three service accounts were provisioned in the target
project. Real impersonated access tests verified the least-privilege matrix,
including allowed and denied reads/writes. Audit storage was added as a
separate publisher-writable boundary.
The public evidence copy is
[iam_verification_report.public.json](./artifacts/iam_verification_report.public.json);
the original operator-identity report remains local.

### M4 — Secure publisher and fixture publication

The publisher validates evidence, lineage, policy/hash binding, snapshot
integrity, hard checks, and the recomputed decision before publication. Local
transaction simulations and real BigQuery fixture tests covered atomic commit,
rollback, lock serialization, idempotent retry, consumer hash readback,
negative gating, ambiguous COMMIT recovery, and mandatory audit-write failure.

## Verified cloud evidence

The original final machine-readable report is retained locally as
`cloud_final_acceptance_20261113.json`; the publication-safe copy is
[cloud_final_acceptance_20261113.public.json](./artifacts/cloud_final_acceptance_20261113.public.json).
The runner is [final_acceptance_runner.py](./scripts/cloud/final_acceptance_runner.py).

The functional fixture contained 30 customers, 64 orders, and 167 order items.
It was `APPROVED` with `VERIFIED` lineage and explicitly labeled
`PIPELINE_ONLY_NOT_PRIVACY_SAFE`.

Ambiguous recovery used job
`314e39ff-2476-4420-9f00-df4e8fd4335b`; the response was suppressed after the
underlying commit, the original outcome was reconciled, and retry returned
idempotently. Exactly one manifest, one success audit, and one set of rows
were observed. Consumer hash readback matched the manifest.

Mandatory audit failure used job
`7bddf773-b7e8-4da3-90ee-a0653f7cd507`; malformed JSON was injected into the
success-audit `PARSE_JSON` expression. The transaction failed before commit,
leaving zero data rows, zero manifest rows, and zero success-audit events. A
separate failure audit persisted and did not authorize publication.

Both fixture release IDs were removed from published tables and manifests.
The five permanent tables, `SYSTEM_LOCK`, unrelated data, and audit history
were retained. No IAM, billing, schema, or production candidate changes
were made by these tests.

## Demo walkthrough

Use existing local artifacts and evidence only:

1. Explain that the legacy private candidate and its CSVs are retained
   locally but are not staged: their recorded content hash predates the
   current NUMERIC(38,9) canonicalization contract.
2. Show the publisher tests that block `REVIEW_REQUIRED`, `REJECTED`, and
   `ERROR` evidence.
3. Show [cloud_final_acceptance_20261113.public.json](./artifacts/cloud_final_acceptance_20261113.public.json)
   for the controlled APPROVED fixture, transaction job IDs, rollback counts,
   consumer hash, and cleanup reconciliation.
4. Emphasize that the APPROVED fixture validates publication mechanics only;
   it is **NOT a privacy-safety demonstration**.

No new cloud resource, publication, or candidate upload is needed for this
walkthrough.

## Test evidence

The final local run reported **108 passed, 1 skipped**. The focused canonical,
cloud adapter, retry, and publisher tests reported **37 passed**. The single
skip is the intentionally marked placeholder
`test_consumer_cannot_read_private_but_can_read_published` in
[test_iam.py](./tests/test_iam.py), which requires live deployed IAM fixtures;
its equivalent live IAM coverage was executed by the cloud verification
scripts rather than by pytest discovery.

Warnings from SciPy, RDT, and SDV were non-failing dependency warnings.

## Known limitations

- `APPROVED` means only that the configured policy checks passed; it is not
  privacy certification.
- DCR is disabled. Exact-match and behavioral checks can miss near-copies.
- The trusted evaluator can provide dishonest source-dependent measurements;
  this MVP does not defend against a fully compromised evaluator.
- Real cloud coverage is controlled fixture coverage, not a production load or
  disaster-recovery certification.
- The single-writer lock is an application transaction mechanism, not a
  general distributed publication service.
- BigQuery primary keys are `NOT ENFORCED`.
- Audit rows are retained for evidence but are not immutable or append-only.
- No Cloud Run, orchestration, public publishing workflow, dashboard, or
  production data deployment is implemented.
- Test artifacts and source data in this repository are synthetic and must not
  be replaced with sensitive records.

## Local commands

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pytest -q tests/test_sdv_real_integration.py
python -m gate.generate_cli --backend sdv --customers 60 --seed 2026 --scale 0.5 --out artifacts/private_candidate
```

The cloud fixture runner defaults to read-only preflight. Its mutation mode
requires explicit approval and is restricted to the controlled test scope.
