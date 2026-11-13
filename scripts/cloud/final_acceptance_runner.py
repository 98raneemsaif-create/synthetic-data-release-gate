"""Execute the two approved Milestone 4 final BigQuery acceptance tests.

This runner intentionally performs no DDL. It requires the five permanent
tables to exist with the expected schemas and is restricted to two exact test
release IDs. It preserves audit history and cleans published rows only after
both tests pass.
"""
from __future__ import annotations

from datetime import datetime, timezone
import argparse
import json
from pathlib import Path
from typing import Any

from gate.cloud_bigquery import (
    BigQueryAdapterError, BigQueryPublicationAdapter, BigQueryPublicationStore,
    LOCATION, LOCK_RELEASE_ID, MAX_BYTES_BILLED, PROJECT, PUBLISHER_ACCOUNT,
    create_impersonated_client, create_operator_client, verify_impersonated_identity,
)
from gate.canonical import canonical_content_hash
from gate.publisher import Publisher, prepare_publication
from gate.models import Decision

from fixture_runner import (
    _audit_events, _consumer_snapshot, _evidence, _fixture, _release_counts,
)


AMBIGUOUS_ID = "srg-cloud-ambiguous-20261113-01"
AUDIT_FAILURE_ID = "srg-cloud-audit-failure-20261113-01"
TEST_IDS = (AMBIGUOUS_ID, AUDIT_FAILURE_ID)
REPORT = Path("artifacts/cloud_final_acceptance_20261113.json")


class _LostResponseJob:
    def __init__(self, job: Any):
        self._job = job
        self.job_id = job.job_id
        self.total_bytes_processed = getattr(job, "total_bytes_processed", 0)

    def result(self):
        result = self._job.result()
        raise TimeoutError("controlled loss of successful client response")


class _LostResponseClient:
    def __init__(self, client: Any):
        self._client = client
        self._lost = False

    def query(self, sql: str, **kwargs: Any):
        job = self._client.query(sql, **kwargs)
        if not self._lost and "BEGIN TRANSACTION" in sql:
            self._lost = True
            return _LostResponseJob(job)
        return job

    def __getattr__(self, name: str):
        return getattr(self._client, name)


def _query_one(client: Any, sql: str) -> dict[str, Any]:
    from google.cloud import bigquery
    job = client.query(sql, job_config=bigquery.QueryJobConfig(
        use_legacy_sql=False, maximum_bytes_billed=MAX_BYTES_BILLED,
    ), location=LOCATION)
    rows = list(job.result())
    if len(rows) != 1:
        raise BigQueryAdapterError(f"Expected one row from read-only query: {sql}")
    return dict(rows[0])


def _operator_identity(client: Any) -> str:
    return str(_query_one(client, "SELECT SESSION_USER() AS principal")["principal"])


def _cleanup(client: Any) -> dict[str, Any]:
    from google.cloud import bigquery
    parameter = bigquery.ArrayQueryParameter("release_ids", "STRING", list(TEST_IDS))
    results = {}
    for table in ("customers", "orders", "order_items", "release_manifest"):
        sql = (
            f"DELETE FROM `{PROJECT}.published_synthetic.{table}` "
            "WHERE release_id IN UNNEST(@release_ids)"
        )
        job = client.query(sql, job_config=bigquery.QueryJobConfig(
            use_legacy_sql=False, maximum_bytes_billed=MAX_BYTES_BILLED,
            query_parameters=[parameter],
        ), location=LOCATION)
        job.result()
        results[table] = {
            "job_id": job.job_id,
            "bytes_processed": int(getattr(job, "total_bytes_processed", 0) or 0),
            "bytes_billed": getattr(job, "total_bytes_billed", None),
        }
    return results


def _assert_unused(adapter: BigQueryPublicationAdapter) -> None:
    for release_id in TEST_IDS:
        if adapter.lookup_release(release_id).status != "ABSENT":
            raise BigQueryAdapterError(f"Fixture release ID is already used: {release_id}")


def _plan(synthetic: dict[str, Any], evidence: dict[str, Any], release_id: str):
    return prepare_publication(
        synthetic, evidence, "policies/synthetic-release-v1.yaml",
        run_id=evidence["evaluation_run_id"], release_id=release_id,
    )


def _test_ambiguous(publisher_client: Any, consumer_client: Any, adapter: BigQueryPublicationAdapter,
                    synthetic: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
    plan = _plan(synthetic, evidence, AMBIGUOUS_ID)
    adapter.write_audit_intent({
        "run_id": plan.run_id, "release_id": plan.release_id,
        "content_hash": plan.content_hash, "policy_hash": plan.policy_hash,
        "decision": "APPROVED", "fixture": "ambiguous-commit",
    })
    lost_adapter = BigQueryPublicationAdapter(
        _LostResponseClient(publisher_client), project=PROJECT, location=LOCATION,
        retrier=adapter.retrier,
    )
    lost_adapter.execute_transaction(plan)
    if not lost_adapter.last_already_committed:
        raise BigQueryAdapterError("Lost-response recovery did not reconcile as idempotent")
    outcome = adapter.lookup_release(AMBIGUOUS_ID)
    if outcome.status != "COMMITTED" or outcome.content_hash != plan.content_hash:
        raise BigQueryAdapterError("Ambiguous commit did not produce the expected manifest")
    counts = _release_counts(publisher_client, AMBIGUOUS_ID)
    if counts != {"customers": 30, "orders": 64, "order_items": 167, "release_manifest": 1}:
        raise BigQueryAdapterError(f"Unexpected ambiguous-release counts: {counts}")
    events = _audit_events(publisher_client, AMBIGUOUS_ID)
    if [event["event_type"] for event in events].count("PUBLICATION_COMMITTED") != 1:
        raise BigQueryAdapterError("Ambiguous retry duplicated success audit")
    snapshot = _consumer_snapshot(consumer_client, AMBIGUOUS_ID)
    consumer_hash = canonical_content_hash(snapshot, evidence["generation_run_id"])
    if consumer_hash != plan.content_hash:
        raise BigQueryAdapterError("Consumer hash does not match ambiguous-release manifest")
    return {
        "status": "PASS", "release_id": AMBIGUOUS_ID,
        "job_id": lost_adapter.last_job_id, "transaction_id": None,
        "counts": counts, "audit_events": events,
        "consumer_hash": consumer_hash, "already_committed": True,
    }


def _test_audit_failure(publisher_client: Any, adapter: BigQueryPublicationAdapter,
                        synthetic: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
    plan = _plan(synthetic, evidence, AUDIT_FAILURE_ID)
    adapter.write_audit_intent({
        "run_id": plan.run_id, "release_id": plan.release_id,
        "content_hash": plan.content_hash, "policy_hash": plan.policy_hash,
        "decision": "APPROVED", "fixture": "audit-failure",
    })
    sql = adapter.transaction_sql.read_text(encoding="utf-8").replace(
        "PARSE_JSON(@verification_json)", "PARSE_JSON(@invalid_verification_json)", 1
    )
    params = adapter._parameters(plan, "audit-failure-injected")
    from google.cloud import bigquery
    params.append(bigquery.ScalarQueryParameter(
        "invalid_verification_json", "STRING", "{not valid json",
    ))
    job = publisher_client.query(
        sql, job_config=adapter._config(params), location=LOCATION,
    )
    try:
        job.result()
    except Exception as exc:
        failure = {"type": type(exc).__name__, "message": str(exc)[:500]}
    else:
        raise BigQueryAdapterError("Injected success-audit failure unexpectedly committed")
    counts = _release_counts(publisher_client, AUDIT_FAILURE_ID)
    events = _audit_events(publisher_client, AUDIT_FAILURE_ID)
    if any(counts.values()):
        raise BigQueryAdapterError(f"Audit failure left committed rows: {counts}")
    if any(event["event_type"] == "PUBLICATION_COMMITTED" for event in events):
        raise BigQueryAdapterError("Audit failure left a success audit event")
    failure_audit = "PENDING"
    try:
        adapter.write_audit_failure({
            "event_id": f"failure-{AUDIT_FAILURE_ID}",
            "event_type": "PUBLICATION_FAILED", "run_id": plan.run_id,
            "release_id": AUDIT_FAILURE_ID, "content_hash": plan.content_hash,
            "policy_hash": plan.policy_hash, "evaluation_decision": "APPROVED",
            "publisher_decision": "ERROR", "error_reason": "injected success-audit failure",
        })
        failure_audit = "PERSISTED"
    except Exception as exc:
        failure_audit = f"UNSUCCESSFUL:{type(exc).__name__}"
    return {
        "status": "PASS", "release_id": AUDIT_FAILURE_ID,
        "job_id": job.job_id, "transaction_id": None,
        "counts": counts, "audit_events": events,
        "failure": failure, "failure_audit": failure_audit,
        "publication_approved": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--preflight", action="store_true", help="run read-only checks only")
    args = parser.parse_args()
    publisher = create_impersonated_client(
        project=PROJECT, location=LOCATION, target_principal=PUBLISHER_ACCOUNT,
    )
    consumer = create_impersonated_client(
        project=PROJECT, location=LOCATION,
        target_principal=f"srg-consumer@{PROJECT}.iam.gserviceaccount.com",
    )
    operator = create_operator_client(project=PROJECT, location=LOCATION)
    publisher_identity = verify_impersonated_identity(publisher)
    consumer_identity = verify_impersonated_identity(
        consumer, expected_principal=f"srg-consumer@{PROJECT}.iam.gserviceaccount.com",
    )
    operator_identity = _operator_identity(operator)
    adapter = BigQueryPublicationAdapter(publisher, project=PROJECT, location=LOCATION)
    schemas = adapter.inspect_target_tables()
    if not all(item["exists"] and item["schema_matches"] for item in schemas.values()):
        raise BigQueryAdapterError(f"Permanent schema precondition failed: {schemas}")
    _assert_unused(adapter)
    reference, synthetic = _fixture()
    evidence = _evidence(
        reference, synthetic, generation_run_id="cloud-final-acceptance-generation",
        evaluation_run_id="cloud-final-acceptance-evaluation",
    )
    if evidence["decision"] != Decision.APPROVED.value or evidence["lineage_status"] != "VERIFIED":
        raise BigQueryAdapterError("Functional fixture is not genuinely APPROVED and VERIFIED")
    plan_hash = canonical_content_hash(synthetic, evidence["generation_run_id"])
    if plan_hash != evidence["candidate_content_hash"]:
        raise BigQueryAdapterError("Fixture candidate hash binding failed")
    active_jobs = []
    for job in operator.list_jobs(state_filter="running", max_results=100):
        active_jobs.append({"job_id": job.job_id, "query": getattr(job, "query", None)})
    preflight = {
        "project": PROJECT, "location": LOCATION,
        "operator_identity": operator_identity,
        "publisher_identity": publisher_identity,
        "consumer_identity": consumer_identity,
        "schemas": schemas, "release_ids_unused": list(TEST_IDS),
        "active_jobs": active_jobs, "lock_release_id": LOCK_RELEASE_ID,
        "fixture_rows": {name: len(frame) for name, frame in synthetic.items()},
        "fixture_decision": evidence["decision"],
        "lineage_status": evidence["lineage_status"],
        "candidate_hash_binding": plan_hash == evidence["candidate_content_hash"],
        "max_bytes_billed": MAX_BYTES_BILLED,
    }
    if args.preflight:
        print(json.dumps(preflight, indent=2, default=str))
        return 0
    report: dict[str, Any] = {
        **preflight,
        "operator_identity": operator_identity,
        "publisher_identity": publisher_identity,
        "consumer_identity": consumer_identity,
        "schemas": schemas, "lock_release_id": LOCK_RELEASE_ID,
        "fixture_rows": {name: len(frame) for name, frame in synthetic.items()},
        "privacy_disclaimer": "PIPELINE_ONLY_NOT_PRIVACY_SAFE",
        "max_bytes_billed": MAX_BYTES_BILLED,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        report["ambiguous_commit"] = _test_ambiguous(
            publisher, consumer, adapter, synthetic, evidence,
        )
        report["audit_failure"] = _test_audit_failure(
            publisher, adapter, synthetic, evidence,
        )
        report["cleanup"] = _cleanup(publisher)
        report["cleanup_reconciliation"] = {
            release_id: _release_counts(publisher, release_id) for release_id in TEST_IDS
        }
        if any(any(counts.values()) for counts in report["cleanup_reconciliation"].values()):
            raise BigQueryAdapterError("Fixture rows remained after cleanup")
        report["status"] = "PASS"
    except Exception as exc:
        report["status"] = "FAIL_STOPPED_WITHOUT_CLEANUP"
        report["error"] = {"type": type(exc).__name__, "message": str(exc)}
        raise
    finally:
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
