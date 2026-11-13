-- Milestone 4 permanent DDL only. Run outside a transaction after approval.
-- Identifiers are fixed project resources; row values are supplied as query parameters.

CREATE TABLE IF NOT EXISTS `synthetic-release-gate-2026.published_synthetic.customers` (
  customer_id STRING NOT NULL,
  region STRING NOT NULL,
  age_group STRING NOT NULL,
  release_id STRING NOT NULL
);

CREATE TABLE IF NOT EXISTS `synthetic-release-gate-2026.published_synthetic.orders` (
  order_id STRING NOT NULL,
  customer_id STRING NOT NULL,
  order_date DATE NOT NULL,
  status STRING NOT NULL,
  release_id STRING NOT NULL
);

CREATE TABLE IF NOT EXISTS `synthetic-release-gate-2026.published_synthetic.order_items` (
  item_id STRING NOT NULL,
  order_id STRING NOT NULL,
  category STRING NOT NULL,
  quantity INT64 NOT NULL,
  -- NUMERIC is precision 38, scale 9; canonical.py rejects excess scale.
  price NUMERIC NOT NULL,
  release_id STRING NOT NULL
);

-- BigQuery does not enforce primary-key uniqueness; the publisher validates it.
CREATE TABLE IF NOT EXISTS `synthetic-release-gate-2026.published_synthetic.release_manifest` (
  release_id STRING NOT NULL,
  run_id STRING NOT NULL,
  content_hash STRING NOT NULL,
  policy_hash STRING NOT NULL,
  policy_version STRING NOT NULL,
  decision STRING NOT NULL,
  status STRING NOT NULL,
  committed_at TIMESTAMP NOT NULL,
  publisher_job_id STRING,
  PRIMARY KEY (release_id) NOT ENFORCED
);

CREATE TABLE IF NOT EXISTS `synthetic-release-gate-2026.audit_private.audit_events` (
  event_id STRING NOT NULL,
  event_type STRING NOT NULL,
  run_id STRING NOT NULL,
  release_id STRING NOT NULL,
  content_hash STRING NOT NULL,
  policy_hash STRING NOT NULL,
  evaluation_decision STRING NOT NULL,
  publisher_decision STRING,
  verification_json JSON,
  error_reason STRING,
  event_timestamp TIMESTAMP NOT NULL,
  bigquery_job_id STRING
);
