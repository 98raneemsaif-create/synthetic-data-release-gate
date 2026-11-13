"""Approval-gated BigQuery fixture runner.

Default mode is read-only collision inspection. ``--execute`` is intentionally
required for DDL/DML and must only be used after explicit cloud approval.
The functional fixture is a near-copy policy test, not a privacy demonstration.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from threading import Barrier

import pandas as pd

from gate.cloud_bigquery import (
    BigQueryAdapterError, BigQueryPublicationAdapter, BigQueryPublicationStore,
    LOCATION, MAX_BYTES_BILLED, PROJECT, PUBLISHER_ACCOUNT, create_impersonated_client,
    create_operator_client, verify_impersonated_identity,
)
from gate.audit import audit_record
from gate.canonical import canonical_content_hash, canonical_reference_hash
from gate.checks.hard import run_hard_checks
from gate.checks.risk import measured_risk_checks
from gate.engine import evaluate
from gate.models import Decision
from gate.policy import load_policy
from gate.publisher import PublicationBlocked, Publisher, prepare_publication
from gate.reference import make_reference


SCHEMA = Path(__file__).with_name("publisher_schema.sql")
TABLE_NAMES = {
    "published_synthetic.customers", "published_synthetic.orders",
    "published_synthetic.order_items", "published_synthetic.release_manifest",
    "audit_private.audit_events",
}


def _fixture():
    reference = make_reference(30, seed=4)
    synthetic = {name: frame.copy(deep=True) for name, frame in reference.items()}
    customer_ids = {old: f"fixture-customer-{i}" for i, old in enumerate(reference["customers"]["customer_id"])}
    synthetic["customers"]["customer_id"] = synthetic["customers"]["customer_id"].map(customer_ids)
    synthetic["orders"]["customer_id"] = synthetic["orders"]["customer_id"].map(customer_ids)
    order_ids = {old: f"fixture-order-{i}" for i, old in enumerate(reference["orders"]["order_id"])}
    synthetic["orders"]["order_id"] = synthetic["orders"]["order_id"].map(order_ids)
    synthetic["order_items"]["order_id"] = synthetic["order_items"]["order_id"].map(order_ids)
    synthetic["order_items"]["item_id"] = synthetic["order_items"]["item_id"].map(lambda value: f"fixture-{value}")
    synthetic["order_items"]["price"] += 0.01
    return reference, synthetic


def _evidence(reference, synthetic, *, generation_run_id: str, evaluation_run_id: str):
    policy, policy_hash = load_policy("policies/synthetic-release-v1.yaml")
    reference_hash = canonical_reference_hash(reference, customers=30, seed=4)
    checks = run_hard_checks(synthetic) + measured_risk_checks(reference=reference, synthetic=synthetic)
    evaluation = evaluate(checks, policy, policy_hash, source_fingerprint=reference_hash,
                          generator_version="functional-approved-fixture/sdv-1.17.0")
    candidate_hash = canonical_content_hash(synthetic, generation_run_id)
    manifest = {
        "backend": "functional-approved-fixture",
        "release_status": "UNVALIDATED_PRIVATE_CANDIDATE",
        "provenance": {
            "generation_run_id": generation_run_id,
            "reference_parameters": {"customers": 30, "seed": 4},
            "reference_hash": reference_hash,
            "candidate_content_hash": candidate_hash,
        },
    }
    report = audit_record(evaluation_run_id, evaluation, timestamp=datetime.now(timezone.utc))
    report.update({
        "evaluation_run_id": evaluation_run_id, "generation_run_id": generation_run_id,
        "lineage_status": "VERIFIED", "reference_parameters": {"customers": 30, "seed": 4},
        "reference_hash": reference_hash, "candidate_content_hash": candidate_hash,
        "candidate_hash_binding_run_id": generation_run_id, "generation_manifest": manifest,
        "measurement_configuration": {"fidelity_metric": "SDMetrics", "dcr_enabled": False,
                                       "privacy_claim": "NO_PRIVACY_CERTIFICATION"},
        "runtime_versions": {"sdv": "1.17.0", "rdt": "1.14.0"},
        "metrics": {check.name: {"status": check.status.value, "value": check.value, "detail": check.detail}
                    for check in checks if check.kind == "risk"},
        "checks": [{**check.__dict__, "status": check.status.value} for check in checks],
        "decision": evaluation.decision.value,
    })
    return report


def _legacy_candidate():
    candidate_dir = Path("artifacts/private_candidate")
    tables = {
        name: pd.read_csv(
            candidate_dir / f"{name}.csv",
            parse_dates=["order_date"] if name == "orders" else None,
        )
        for name in ("customers", "orders", "order_items")
    }
    evidence = json.loads(
        (candidate_dir / "measurement_report.json").read_text(encoding="utf-8")
    )
    return tables, evidence


def _consumer_count(client, release_id: str) -> int:
    from google.cloud import bigquery
    sql = (f"SELECT COUNT(*) AS n FROM `{PROJECT}.published_synthetic.customers` c "
           f"JOIN `{PROJECT}.published_synthetic.release_manifest` m "
           "ON m.release_id = c.release_id "
           "WHERE m.status = 'COMMITTED' AND m.release_id = @release_id")
    config = bigquery.QueryJobConfig(
        use_legacy_sql=False, maximum_bytes_billed=MAX_BYTES_BILLED,
        query_parameters=[bigquery.ScalarQueryParameter("release_id", "STRING", release_id)],
    )
    return int(list(client.query(sql, job_config=config, location=LOCATION).result())[0]["n"])


def _consumer_snapshot(client, release_id: str) -> dict[str, pd.DataFrame]:
    from google.cloud import bigquery
    columns = {
        "customers": ["customer_id", "region", "age_group"],
        "orders": ["order_id", "customer_id", "order_date", "status"],
        "order_items": ["item_id", "order_id", "category", "quantity", "price"],
    }
    snapshot = {}
    for table_name, table_columns in columns.items():
        sql = (
            f"SELECT {', '.join(f't.{column}' for column in table_columns)} "
            f"FROM `{PROJECT}.published_synthetic.{table_name}` t "
            f"JOIN `{PROJECT}.published_synthetic.release_manifest` m "
            "ON m.release_id = t.release_id "
            "WHERE m.status = 'COMMITTED' AND m.release_id = @release_id"
        )
        config = bigquery.QueryJobConfig(
            use_legacy_sql=False, maximum_bytes_billed=MAX_BYTES_BILLED,
            query_parameters=[bigquery.ScalarQueryParameter("release_id", "STRING", release_id)],
        )
        rows = list(client.query(sql, job_config=config, location=LOCATION).result())
        snapshot[table_name] = pd.DataFrame([dict(row) for row in rows], columns=table_columns)
    return snapshot


def _audit_events(client, release_id: str) -> list[dict]:
    from google.cloud import bigquery
    sql = (
        f"SELECT event_type, bigquery_job_id FROM `{PROJECT}.audit_private.audit_events` "
        "WHERE release_id = @release_id ORDER BY event_type"
    )
    config = bigquery.QueryJobConfig(
        use_legacy_sql=False, maximum_bytes_billed=MAX_BYTES_BILLED,
        query_parameters=[bigquery.ScalarQueryParameter("release_id", "STRING", release_id)],
    )
    return [dict(row) for row in client.query(sql, job_config=config, location=LOCATION).result()]


def _release_counts(client, release_id: str) -> dict[str, int]:
    from google.cloud import bigquery
    config = bigquery.QueryJobConfig(
        use_legacy_sql=False, maximum_bytes_billed=MAX_BYTES_BILLED,
        query_parameters=[bigquery.ScalarQueryParameter("release_id", "STRING", release_id)],
    )
    counts = {}
    for table_name in ("customers", "orders", "order_items", "release_manifest"):
        sql = (
            f"SELECT COUNT(*) AS n FROM `{PROJECT}.published_synthetic.{table_name}` "
            "WHERE release_id = @release_id"
        )
        counts[table_name] = int(list(client.query(sql, job_config=config, location=LOCATION).result())[0]["n"])
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default=PROJECT)
    parser.add_argument("--location", default=LOCATION)
    parser.add_argument("--release-id", default="srg-cloud-fixture-release")
    parser.add_argument("--execute", action="store_true",
                        help="perform approved fixture DDL/DML; default is read-only")
    parser.add_argument("--cleanup", action="store_true",
                        help="delete only this run's uniquely prefixed fixture rows; tables remain")
    args = parser.parse_args()
    if args.project != PROJECT or args.location != LOCATION:
        parser.error("runner is restricted to synthetic-release-gate-2026 in US")
    if args.cleanup and not args.execute:
        parser.error("--cleanup requires --execute")
    if not args.release_id.startswith("srg-cloud-fixture-"):
        parser.error("--release-id must start with srg-cloud-fixture-")
    try:
        client = create_impersonated_client(
            project=args.project, location=args.location,
            target_principal=PUBLISHER_ACCOUNT,
        )
        print({"publisher_identity": verify_impersonated_identity(client)})
        adapter = BigQueryPublicationAdapter(client, project=args.project, location=args.location)
        inspection = adapter.inspect_target_tables()
        print({"mode": "execute" if args.execute else "preflight", "tables": inspection})
        if not args.execute:
            return 0
        # DDL is fixed, IF NOT EXISTS, and only run after the exact collision
        # inspection above. Existing tables must match the adapter schema.
        schema_sql = re.sub(
            r"(?m)^\s*--[^\r\n]*(?:\r?\n|$)", "",
            SCHEMA.read_text(encoding="utf-8"),
        )
        statements = [statement.strip() for statement in schema_sql.split(";") if statement.strip()]
        # Administrative provisioning is performed by the authenticated
        # operator. The publisher identity is reserved for publication DML.
        admin_client = create_operator_client(project=args.project, location=args.location)
        created = []
        for statement in statements:
            if "CREATE TABLE IF NOT EXISTS" not in statement.upper():
                continue
            table_name = statement.split("`")[1]
            short_name = table_name.split(f"{args.project}.", 1)[-1]
            if short_name not in TABLE_NAMES:
                raise BigQueryAdapterError(f"Unexpected DDL target: {table_name}")
            if inspection[short_name]["exists"]:
                continue
            from google.cloud import bigquery
            dry_run = admin_client.query(statement, job_config=bigquery.QueryJobConfig(
                use_legacy_sql=False, dry_run=True, maximum_bytes_billed=MAX_BYTES_BILLED,
            ), location=args.location)
            estimated = int(getattr(dry_run, "total_bytes_processed", 0) or 0)
            if estimated > MAX_BYTES_BILLED:
                raise BigQueryAdapterError(
                    f"DDL dry-run estimate {estimated} exceeds {MAX_BYTES_BILLED} bytes"
                )
            job = admin_client.query(statement, job_config=bigquery.QueryJobConfig(
                use_legacy_sql=False, maximum_bytes_billed=MAX_BYTES_BILLED,
            ), location=args.location)
            job.result()
            created.append(table_name)
        print({"ddl_created_or_reused": created})
        adapter.ensure_lock_row()
        print({"manifest_lock_row": "ready"})
        reference, synthetic = _fixture()
        generation_id = "cloud-fixture-generation"
        evaluation_id = "cloud-fixture-evaluation"
        evidence = _evidence(reference, synthetic, generation_run_id=generation_id,
                             evaluation_run_id=evaluation_id)
        if evidence["decision"] != Decision.APPROVED.value:
            raise BigQueryAdapterError("Functional APPROVED fixture did not pass unchanged policy")
        store = BigQueryPublicationStore(adapter)
        publisher = Publisher(store, "policies/synthetic-release-v1.yaml")
        plan = prepare_publication(
            synthetic, evidence, "policies/synthetic-release-v1.yaml",
            run_id=evaluation_id, release_id=args.release_id,
        )
        estimated_bytes = adapter.estimate_transaction(plan)
        if estimated_bytes > MAX_BYTES_BILLED:
            raise BigQueryAdapterError(
                f"Publication dry-run estimate {estimated_bytes} exceeds {MAX_BYTES_BILLED} bytes"
            )
        result = publisher.publish(synthetic, evidence, run_id=evaluation_id, release_id=args.release_id)
        if result.decision is not Decision.APPROVED:
            raise BigQueryAdapterError("Approved fixture did not publish")
        if adapter.lookup_release(args.release_id).status != "COMMITTED":
            raise BigQueryAdapterError("Missing COMMITTED release manifest")
        success_events = _audit_events(client, args.release_id)
        if {event["event_type"] for event in success_events} != {
            "PUBLICATION_COMMITTED", "PUBLICATION_INTENT",
        }:
            raise BigQueryAdapterError("Committed release has incomplete audit events")
        print({"publisher_job_id": adapter.last_job_id,
               "transaction_bytes_processed": adapter.last_bytes_processed,
               "success_audit_events": success_events})
        # Negative gate: the existing legacy REVIEW_REQUIRED candidate is
        # attempted only as a blocked publication and is never inserted.
        legacy_tables, legacy_evidence = _legacy_candidate()
        if legacy_evidence.get("decision") != Decision.REVIEW_REQUIRED.value:
            raise BigQueryAdapterError("Existing legacy candidate is not REVIEW_REQUIRED")
        legacy_release_id = f"{args.release_id}-legacy-rejected"
        try:
            publisher.publish(
                legacy_tables, legacy_evidence,
                run_id=legacy_evidence["run_id"], release_id=legacy_release_id,
            )
        except Exception as blocked:
            if adapter.lookup_release(legacy_release_id).status != "ABSENT":
                raise BigQueryAdapterError("Legacy REVIEW_REQUIRED candidate was published")
            print({"legacy_candidate": "blocked", "reason": type(blocked).__name__})
        else:
            raise BigQueryAdapterError("Legacy REVIEW_REQUIRED candidate was accepted")

        consumer = create_impersonated_client(
            project=args.project, location=args.location,
            target_principal=f"srg-consumer@{args.project}.iam.gserviceaccount.com",
        )
        consumer_snapshot = _consumer_snapshot(consumer, args.release_id)
        if _consumer_count(consumer, args.release_id) != len(synthetic["customers"]):
            raise BigQueryAdapterError("Consumer committed-release visibility check failed")
        consumer_hash = canonical_content_hash(consumer_snapshot, generation_id)
        manifest_outcome = adapter.lookup_release(args.release_id)
        if consumer_hash != manifest_outcome.content_hash:
            raise BigQueryAdapterError("Consumer snapshot hash differs from committed manifest")
        retry = publisher.publish(synthetic, evidence, run_id=evaluation_id, release_id=args.release_id)
        if not retry.already_committed:
            raise BigQueryAdapterError("Successful retry was not idempotent")
        changed = {name: frame.copy(deep=True) for name, frame in synthetic.items()}
        changed["order_items"].loc[0, "price"] += 0.02
        conflicting = _evidence(reference, changed, generation_run_id=generation_id,
                                evaluation_run_id="cloud-fixture-conflict-evaluation")
        try:
            publisher.publish(changed, conflicting, run_id="cloud-fixture-conflict-evaluation",
                              release_id=args.release_id)
        except Exception:
            pass
        else:
            raise BigQueryAdapterError("Conflicting release_id was accepted")
        rollback_id = f"{args.release_id}-rollback"
        rollback_plan = prepare_publication(
            synthetic, evidence, "policies/synthetic-release-v1.yaml",
            run_id=evaluation_id, release_id=rollback_id,
        )
        failing_sql = adapter.transaction_sql.read_text(encoding="utf-8").replace(
            "  INSERT INTO `synthetic-release-gate-2026.published_synthetic.release_manifest`",
            "  ASSERT FALSE AS 'forced fixture rollback';\n\n  INSERT INTO `synthetic-release-gate-2026.published_synthetic.release_manifest`", 1
        )
        try:
            job = client.query(
                failing_sql,
                job_config=adapter._config(adapter._parameters(rollback_plan, "forced-rollback")),
                location=args.location,
            )
            job.result()
        except Exception:
            pass
        else:
            raise BigQueryAdapterError("Forced rollback unexpectedly committed")
        if adapter.lookup_release(rollback_id).status != "ABSENT":
            raise BigQueryAdapterError("Forced rollback left a committed manifest")
        rollback_counts = _release_counts(client, rollback_id)
        rollback_events = _audit_events(client, rollback_id)
        if any(rollback_counts.values()):
            raise BigQueryAdapterError(f"Forced rollback left rows: {rollback_counts}")
        if any(event["event_type"] == "PUBLICATION_COMMITTED" for event in rollback_events):
            raise BigQueryAdapterError("Forced rollback left a success audit event")
        print({"rollback_counts": rollback_counts, "rollback_audit_events": rollback_events})
        try:
            adapter.write_audit_failure({
                "event_id": f"failure-{rollback_id}-{evaluation_id}",
                "event_type": "PUBLICATION_FAILED",
                "run_id": evaluation_id, "release_id": rollback_id,
                "content_hash": rollback_plan.content_hash,
                "policy_hash": rollback_plan.policy_hash,
                "evaluation_decision": "APPROVED", "publisher_decision": "ERROR",
                "error_reason": "forced fixture rollback",
            })
        except Exception as audit_error:
            print({"failure_audit": "unsuccessful", "error": type(audit_error).__name__})
        else:
            print({"failure_audit": "persisted", "release_id": rollback_id})

        concurrent_id = f"{args.release_id}-concurrent"
        barrier = Barrier(2)
        def concurrent_publish(index):
            try:
                local_client = create_impersonated_client(
                    project=args.project, location=args.location,
                    target_principal=PUBLISHER_ACCOUNT,
                )
                local_adapter = BigQueryPublicationAdapter(local_client, project=args.project, location=args.location)
                local_publisher = Publisher(BigQueryPublicationStore(local_adapter), "policies/synthetic-release-v1.yaml")
                barrier.wait(timeout=30)
                return local_publisher.publish(
                    synthetic, evidence, run_id=evaluation_id, release_id=concurrent_id
                )
            except Exception as exc:
                return exc
        with ThreadPoolExecutor(max_workers=2) as pool:
            concurrent_results = list(pool.map(concurrent_publish, (1, 2)))
        committed_attempts = [
            result for result in concurrent_results
            if not isinstance(result, Exception) and not result.already_committed
        ]
        if len(committed_attempts) != 1 or adapter.lookup_release(concurrent_id).status != "COMMITTED":
            raise BigQueryAdapterError("Concurrent same-release test did not yield exactly one commit")
        print({"published": args.release_id, "customers": len(synthetic["customers"]),
               "orders": len(synthetic["orders"]), "order_items": len(synthetic["order_items"]),
               "consumer_visible_customers": len(synthetic["customers"])})
        if args.cleanup:
            from google.cloud import bigquery
            cleanup_ids = [
                args.release_id, f"{args.release_id}-rollback",
                f"{args.release_id}-concurrent", f"{args.release_id}-legacy-rejected",
            ]
            array_parameter = bigquery.ArrayQueryParameter("release_ids", "STRING", cleanup_ids)
            for table_name in (
                "customers", "orders", "order_items", "release_manifest",
            ):
                cleanup_sql = (
                    f"DELETE FROM `{args.project}.published_synthetic.{table_name}` "
                    "WHERE release_id IN UNNEST(@release_ids)"
                )
                job = client.query(cleanup_sql, job_config=bigquery.QueryJobConfig(
                    use_legacy_sql=False, maximum_bytes_billed=MAX_BYTES_BILLED,
                    query_parameters=[array_parameter],
                ), location=args.location)
                job.result()
            print({"cleanup": "published fixture rows only",
                   "audit_history_retained": True, "permanent_tables_retained": True})
        return 0
    except Exception as exc:
        raise SystemExit(f"CLOUD FIXTURE FAILED CLOSED: {type(exc).__name__}: {exc}") from exc


if __name__ == "__main__":
    raise SystemExit(main())
