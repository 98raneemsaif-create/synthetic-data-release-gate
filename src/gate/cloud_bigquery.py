"""Real BigQuery adapter for the approved publisher path.

Cloud SDK imports are lazy so local policy and SDV tests do not require GCP
packages or credentials. The adapter accepts a Publisher plan containing one
already validated snapshot and never reloads candidate tables.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
import json
from pathlib import Path
from typing import Any, Mapping

from gate.cloud_retry import BigQueryTransactionRetrier, ReleaseOutcome
from gate.canonical import canonical_numeric_decimal
from gate.publisher import PublicationPlan, PublicationStore, Transaction


PROJECT = "synthetic-release-gate-2026"
LOCATION = "US"
PUBLISHER_ACCOUNT = "srg-publisher@synthetic-release-gate-2026.iam.gserviceaccount.com"
MAX_BYTES_BILLED = 104857600
LOCK_RELEASE_ID = "__srg_publication_lock__"


class BigQueryAdapterError(RuntimeError):
    """Fail-closed cloud adapter error."""


EXPECTED_FIELDS: dict[str, tuple[tuple[str, str], ...]] = {
    "published_synthetic.customers": (
        ("customer_id", "STRING"), ("region", "STRING"),
        ("age_group", "STRING"), ("release_id", "STRING"),
    ),
    "published_synthetic.orders": (
        ("order_id", "STRING"), ("customer_id", "STRING"),
        ("order_date", "DATE"), ("status", "STRING"), ("release_id", "STRING"),
    ),
    "published_synthetic.order_items": (
        ("item_id", "STRING"), ("order_id", "STRING"),
        ("category", "STRING"), ("quantity", "INT64"),
        ("price", "NUMERIC"), ("release_id", "STRING"),
    ),
    "published_synthetic.release_manifest": (
        ("release_id", "STRING"), ("run_id", "STRING"),
        ("content_hash", "STRING"), ("policy_hash", "STRING"),
        ("policy_version", "STRING"), ("decision", "STRING"),
        ("status", "STRING"), ("committed_at", "TIMESTAMP"),
        ("publisher_job_id", "STRING"),
    ),
    "audit_private.audit_events": (
        ("event_id", "STRING"), ("event_type", "STRING"),
        ("run_id", "STRING"), ("release_id", "STRING"),
        ("content_hash", "STRING"), ("policy_hash", "STRING"),
        ("evaluation_decision", "STRING"), ("publisher_decision", "STRING"),
        ("verification_json", "JSON"), ("error_reason", "STRING"),
        ("event_timestamp", "TIMESTAMP"), ("bigquery_job_id", "STRING"),
    ),
}


def _not_found(error: BaseException) -> bool:
    return getattr(error, "code", None) == 404 or type(error).__name__ == "NotFound"


def _schema_type(field_type: str) -> str:
    # BigQuery's Python API exposes SQL INT64 as INTEGER.
    return "INT64" if field_type == "INTEGER" else field_type


def create_impersonated_client(
    *, project: str = PROJECT, location: str = LOCATION,
    target_principal: str = PUBLISHER_ACCOUNT, source_credentials: Any = None,
) -> Any:
    """Create a BigQuery client using short-lived publisher impersonation."""
    try:
        import google.auth
        from google.auth import impersonated_credentials
        from google.cloud import bigquery
    except ImportError as exc:
        raise BigQueryAdapterError(
            "Install the cloud extra before real BigQuery tests"
        ) from exc
    if source_credentials is None:
        source_credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
    credentials = impersonated_credentials.Credentials(
        source_credentials=source_credentials,
        target_principal=target_principal,
        target_scopes=["https://www.googleapis.com/auth/cloud-platform"],
        lifetime=3600,
    )
    return bigquery.Client(project=project, credentials=credentials, location=location)


def create_operator_client(*, project: str = PROJECT, location: str = LOCATION) -> Any:
    """Create the administrative client used only for pre-approved DDL.

    Table provisioning is deliberately separate from publisher DML. This uses
    the operator's user ADC and never creates a service-account key.
    """
    try:
        import google.auth
        from google.cloud import bigquery
    except ImportError as exc:
        raise BigQueryAdapterError(
            "Install the cloud extra before real BigQuery tests"
        ) from exc
    credentials, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    return bigquery.Client(project=project, credentials=credentials, location=location)


def verify_impersonated_identity(client: Any, *, expected_principal: str = PUBLISHER_ACCOUNT) -> str:
    """Run a read-only identity query without exposing tokens."""
    try:
        from google.cloud import bigquery
        config = bigquery.QueryJobConfig(
            use_legacy_sql=False, maximum_bytes_billed=MAX_BYTES_BILLED,
        )
        rows = list(client.query(
            "SELECT SESSION_USER() AS principal", job_config=config, location=LOCATION
        ).result())
    except Exception as exc:
        raise BigQueryAdapterError("Unable to verify impersonated BigQuery identity") from exc
    if not rows or rows[0]["principal"] != expected_principal:
        raise BigQueryAdapterError("BigQuery identity is not the expected service account")
    return str(rows[0]["principal"])


class BigQueryPublicationAdapter:
    """Execute publication SQL with bounded retries and outcome reconciliation."""

    def __init__(self, client: Any, *, project: str = PROJECT, location: str = LOCATION,
                 max_bytes_billed: int = MAX_BYTES_BILLED,
                 retrier: BigQueryTransactionRetrier | None = None):
        if project != PROJECT or location != LOCATION:
            raise ValueError("Adapter is restricted to the approved project and US location")
        self.client = client
        self.project = project
        self.location = location
        self.max_bytes_billed = max_bytes_billed
        self.retrier = retrier or BigQueryTransactionRetrier()
        self.last_job_id: str | None = None
        self.last_bytes_processed: int | None = None
        self.last_already_committed = False
        self.transaction_sql = Path(__file__).parents[2] / "scripts" / "cloud" / "publish_transaction.sql"

    def _config(self, parameters: list[Any], *, dry_run: bool = False) -> Any:
        try:
            from google.cloud import bigquery
        except ImportError as exc:
            raise BigQueryAdapterError("google-cloud-bigquery is required") from exc
        return bigquery.QueryJobConfig(
            use_legacy_sql=False, dry_run=dry_run,
            maximum_bytes_billed=self.max_bytes_billed,
            query_parameters=parameters,
        )

    def estimate_transaction(self, plan: PublicationPlan) -> int:
        """Dry-run the fixed transaction and return estimated bytes processed."""
        job = self.client.query(
            self.transaction_sql.read_text(encoding="utf-8"),
            job_config=self._config(self._parameters(plan, "dry-run"), dry_run=True),
            location=self.location,
        )
        return int(getattr(job, "total_bytes_processed", 0) or 0)

    def ensure_lock_row(self) -> None:
        """Create the single manifest lock row once, outside publication transactions."""
        from google.cloud import bigquery
        sql = f"""MERGE `{self.project}.published_synthetic.release_manifest` T
        USING (SELECT @lock_release_id AS release_id) S
        ON T.release_id = S.release_id
        WHEN NOT MATCHED THEN INSERT
          (release_id,run_id,content_hash,policy_hash,policy_version,decision,
           status,committed_at,publisher_job_id)
        VALUES (@lock_release_id,'SYSTEM_LOCK','LOCK','LOCK','LOCK','LOCK',
                'LOCK',CURRENT_TIMESTAMP(),NULL)"""
        config = self._config([self._scalar("lock_release_id", "STRING", LOCK_RELEASE_ID)])
        self.client.query(sql, job_config=config, location=self.location).result()

    def inspect_target_tables(self) -> dict[str, dict[str, Any]]:
        """Read-only collision/schema inspection for the five exact targets."""
        result: dict[str, dict[str, Any]] = {}
        for short_name, expected in EXPECTED_FIELDS.items():
            reference = f"{self.project}.{short_name}"
            try:
                table = self.client.get_table(reference)
            except Exception as exc:
                if _not_found(exc):
                    result[short_name] = {"exists": False, "schema_matches": False}
                    continue
                raise BigQueryAdapterError(f"Table inspection failed: {reference}") from exc
            actual = tuple((field.name, _schema_type(field.field_type)) for field in table.schema)
            result[short_name] = {
                "exists": True, "schema_matches": actual == expected,
                "actual_schema": actual, "expected_schema": expected,
            }
            if actual != expected:
                raise BigQueryAdapterError(f"Existing table schema collision: {reference}")
        return result

    def _scalar(self, name: str, type_: str, value: Any) -> Any:
        from google.cloud import bigquery
        return bigquery.ScalarQueryParameter(name, type_, value)

    def _struct_array(self, name: str, fields: tuple[tuple[str, str], ...], rows: list[dict[str, Any]]) -> Any:
        from google.cloud.bigquery.query import (
            ArrayQueryParameter, ScalarQueryParameter, ScalarQueryParameterType,
            StructQueryParameter, StructQueryParameterType,
        )
        field_types = [ScalarQueryParameterType(type_, name=field) for field, type_ in fields]
        struct_values = [
            StructQueryParameter(
                None, *(ScalarQueryParameter(field, type_, row[field]) for field, type_ in fields)
            )
            for row in rows
        ]
        return ArrayQueryParameter(name, StructQueryParameterType(*field_types), struct_values)

    @staticmethod
    def _rows(plan: PublicationPlan, table: str, fields: tuple[tuple[str, str], ...]) -> list[dict[str, Any]]:
        output = []
        for row in plan.snapshot[table].to_dict(orient="records"):
            converted: dict[str, Any] = {}
            for field, type_ in fields:
                value = row[field]
                if field == "order_date":
                    value = __import__("pandas").Timestamp(value).date()
                elif type_ == "INT64":
                    value = int(value)
                elif type_ == "NUMERIC":
                    value = canonical_numeric_decimal(value)
                converted[field] = value
            output.append(converted)
        return output

    def _parameters(self, plan: PublicationPlan, publisher_job_id: str) -> list[Any]:
        customers = (("customer_id", "STRING"), ("region", "STRING"), ("age_group", "STRING"))
        orders = (("order_id", "STRING"), ("customer_id", "STRING"), ("order_date", "DATE"), ("status", "STRING"))
        items = (("item_id", "STRING"), ("order_id", "STRING"), ("category", "STRING"), ("quantity", "INT64"), ("price", "NUMERIC"))
        verification = json.dumps({"decision": plan.evaluation.decision.value, "reasons": plan.evaluation.reasons})
        return [
            self._struct_array("customers", customers, self._rows(plan, "customers", customers)),
            self._struct_array("orders", orders, self._rows(plan, "orders", orders)),
            self._struct_array("order_items", items, self._rows(plan, "order_items", items)),
            self._scalar("release_id", "STRING", plan.release_id),
            self._scalar("run_id", "STRING", plan.run_id),
            self._scalar("content_hash", "STRING", plan.content_hash),
            self._scalar("policy_hash", "STRING", plan.policy_hash),
            self._scalar("policy_version", "STRING", plan.policy_version),
            # The BigQuery job ID is assigned after submission and cannot be
            # injected into the same atomic script without a second write.
            self._scalar("publisher_job_id", "STRING", None),
            self._scalar("event_id", "STRING", f"commit-{plan.release_id}"),
            self._scalar("verification_json", "STRING", verification),
            self._scalar("lock_release_id", "STRING", LOCK_RELEASE_ID),
        ]

    def lookup_release(self, release_id: str, *, job_id: str | None = None) -> ReleaseOutcome:
        """Inspect job state where available, then authoritative manifest state."""
        if job_id:
            try:
                job = self.client.get_job(job_id, project=self.project, location=self.location)
                if not getattr(job, "done", lambda: False)():
                    return ReleaseOutcome("UNKNOWN")
            except Exception:
                return ReleaseOutcome("UNKNOWN")
        try:
            from google.cloud import bigquery
            sql = (f"SELECT status, content_hash, policy_hash, run_id, policy_version, decision FROM `"
                   f"{self.project}.published_synthetic.release_manifest` "
                   "WHERE release_id = @release_id LIMIT 2")
            job = self.client.query(sql, job_config=self._config([
                self._scalar("release_id", "STRING", release_id)
            ]), location=self.location)
            rows = list(job.result())
        except Exception as exc:
            if _not_found(exc):
                return ReleaseOutcome("ABSENT")
            return ReleaseOutcome("UNKNOWN")
        if not rows:
            return ReleaseOutcome("ABSENT")
        row = rows[0]
        if row["status"] == "COMMITTED":
            return ReleaseOutcome(
                "COMMITTED", row["content_hash"], row["policy_hash"],
                row["run_id"], row["policy_version"],
            )
        return ReleaseOutcome("CONFLICT")

    def execute_transaction(self, plan: PublicationPlan) -> None:
        sql = self.transaction_sql.read_text(encoding="utf-8")
        self.last_already_committed = False
        job_holder: dict[str, str | None] = {"id": None}

        def operation(_: int) -> Any:
            job = self.client.query(sql, job_config=self._config(
                self._parameters(plan, plan.release_id)
            ), location=self.location)
            job_holder["id"] = getattr(job, "job_id", None)
            self.last_job_id = job_holder["id"]
            result = job.result()
            self.last_bytes_processed = int(getattr(job, "total_bytes_processed", 0) or 0)
            return result

        result = self.retrier.execute(
            operation, release_id=plan.release_id,
            expected_content_hash=plan.content_hash,
            expected_policy_hash=plan.policy_hash,
            lookup_outcome=lambda release: self.lookup_release(
                release, job_id=job_holder["id"]
            ),
        )
        self.last_already_committed = result.already_committed

    def write_audit_intent(self, event: Mapping[str, Any]) -> None:
        from google.cloud import bigquery
        event_id = f"intent-{event['release_id']}"
        sql = f"""MERGE `{self.project}.audit_private.audit_events` T
        USING (SELECT @event_id AS event_id) S ON T.event_id = S.event_id
        WHEN NOT MATCHED THEN INSERT
          (event_id,event_type,run_id,release_id,content_hash,policy_hash,
           evaluation_decision,publisher_decision,verification_json,error_reason,
           event_timestamp,bigquery_job_id)
        VALUES (@event_id,'PUBLICATION_INTENT',@run_id,@release_id,@content_hash,
          @policy_hash,@decision,NULL,PARSE_JSON(@verification_json),NULL,
          CURRENT_TIMESTAMP(),NULL)"""
        params = [
            self._scalar("event_id", "STRING", event_id),
            self._scalar("run_id", "STRING", event["run_id"]),
            self._scalar("release_id", "STRING", event["release_id"]),
            self._scalar("content_hash", "STRING", event["content_hash"]),
            self._scalar("policy_hash", "STRING", event["policy_hash"]),
            self._scalar("decision", "STRING", event["decision"]),
            self._scalar("verification_json", "STRING", json.dumps(dict(event))),
        ]
        job = self.client.query(sql, job_config=self._config(params), location=self.location)
        job.result()

    def write_audit_failure(self, event: Mapping[str, Any]) -> None:
        """Best-effort independent failure/rejection audit after rollback."""
        from google.cloud import bigquery
        event_id = str(event["event_id"])
        sql = f"""MERGE `{self.project}.audit_private.audit_events` T
        USING (SELECT @event_id AS event_id) S ON T.event_id = S.event_id
        WHEN NOT MATCHED THEN INSERT
          (event_id,event_type,run_id,release_id,content_hash,policy_hash,
           evaluation_decision,publisher_decision,verification_json,error_reason,
           event_timestamp,bigquery_job_id)
        VALUES (@event_id,@event_type,@run_id,@release_id,@content_hash,
          @policy_hash,@evaluation_decision,@publisher_decision,
          PARSE_JSON(@verification_json),@error_reason,CURRENT_TIMESTAMP(),@job_id)"""
        params = [
            self._scalar("event_id", "STRING", event_id),
            self._scalar("event_type", "STRING", str(event.get("event_type", "PUBLICATION_FAILED"))),
            self._scalar("run_id", "STRING", str(event.get("run_id", "UNAVAILABLE"))),
            self._scalar("release_id", "STRING", str(event["release_id"])),
            self._scalar("content_hash", "STRING", str(event.get("content_hash", "UNAVAILABLE"))),
            self._scalar("policy_hash", "STRING", str(event.get("policy_hash", "UNAVAILABLE"))),
            self._scalar("evaluation_decision", "STRING", str(event.get("evaluation_decision", "ERROR"))),
            self._scalar("publisher_decision", "STRING", str(event.get("publisher_decision", "ERROR"))),
            self._scalar("verification_json", "STRING", json.dumps(dict(event))),
            self._scalar("error_reason", "STRING", str(event.get("error_reason", "unspecified"))),
            self._scalar("job_id", "STRING", str(event.get("job_id")) if event.get("job_id") else None),
        ]
        job = self.client.query(sql, job_config=self._config(params), location=self.location)
        job.result()

    def begin(self) -> "BigQueryTransaction":
        return BigQueryTransaction(self)


class BigQueryTransaction(Transaction):
    def __init__(self, adapter: BigQueryPublicationAdapter, store: "BigQueryPublicationStore" | None = None):
        self.adapter = adapter
        self.store = store
        self.plan: PublicationPlan | None = None
        self.closed = False

    def _set_plan(self, plan: PublicationPlan) -> None:
        if self.plan is not None and self.plan is not plan:
            raise BigQueryAdapterError("Transaction received inconsistent publication plans")
        self.plan = plan

    def insert_publication(self, plan: PublicationPlan) -> None:
        self._set_plan(plan)

    def insert_manifest(self, plan: PublicationPlan) -> None:
        self._set_plan(plan)

    def insert_audit_success(self, plan: PublicationPlan) -> None:
        self._set_plan(plan)

    def commit(self) -> None:
        if self.closed or self.plan is None:
            raise BigQueryAdapterError("Cannot commit an empty or closed transaction")
        self.adapter.execute_transaction(self.plan)
        if self.store is not None:
            self.store.last_already_committed = self.adapter.last_already_committed
        self.closed = True

    def rollback(self) -> None:
        self.closed = True


class BigQueryPublicationStore(PublicationStore):
    def __init__(self, adapter: BigQueryPublicationAdapter):
        self.adapter = adapter
        self.last_already_committed = False

    def get_manifest(self, release_id: str) -> Mapping[str, Any] | None:
        outcome = self.adapter.lookup_release(release_id)
        if outcome.status == "ABSENT":
            return None
        if outcome.status == "UNKNOWN":
            raise BigQueryAdapterError("Unable to determine release manifest state")
        return {
            "release_id": release_id, "status": outcome.status,
            "content_hash": outcome.content_hash, "policy_hash": outcome.policy_hash,
            "run_id": outcome.run_id, "policy_version": outcome.policy_version,
            "decision": "APPROVED",
        }

    def write_audit_intent(self, event: Mapping[str, Any]) -> None:
        self.adapter.write_audit_intent(event)

    def write_audit_failure(self, event: Mapping[str, Any]) -> None:
        self.adapter.write_audit_failure(event)

    def begin(self) -> BigQueryTransaction:
        return BigQueryTransaction(self.adapter, self)
