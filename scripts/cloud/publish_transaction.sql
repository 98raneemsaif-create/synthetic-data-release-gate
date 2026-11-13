-- Design only; do not execute until permanent DDL and cloud approval exist.
-- @customers, @orders, and @order_items are bounded ARRAY<STRUCT> query parameters
-- created from the one verified in-memory snapshot. No candidate table is reread.
-- release_id is a validated parameter value, never an interpolated identifier.
-- The lock row is an actual conflicting mutation. Each transaction deletes and
-- recreates it first, so concurrent transactions contend on the same row.
-- The external BigQueryTransactionRetrier must cap attempts (default 3), wait
-- at least 60 seconds before the first retry, use exponential backoff, inspect
-- job status and release_manifest after ambiguous timeouts, and fail closed if
-- the outcome remains unknown.
-- The publisher job must set a strict maximumBytesBilled bound.

BEGIN
  BEGIN TRANSACTION;

  -- Acquire the manifest table write lock before checking release_id.
  DELETE FROM `synthetic-release-gate-2026.published_synthetic.release_manifest`
  WHERE release_id = @lock_release_id;

  INSERT INTO `synthetic-release-gate-2026.published_synthetic.release_manifest`
    (release_id, run_id, content_hash, policy_hash, policy_version, decision,
     status, committed_at, publisher_job_id)
  VALUES (@lock_release_id, 'SYSTEM_LOCK', 'LOCK', 'LOCK', 'LOCK', 'LOCK',
          'LOCK', CURRENT_TIMESTAMP(), NULL);

  ASSERT NOT EXISTS (
    SELECT 1
    FROM `synthetic-release-gate-2026.published_synthetic.release_manifest`
    WHERE release_id = @release_id
      AND release_id != @lock_release_id
  ) AS 'duplicate release_id';

  INSERT INTO `synthetic-release-gate-2026.published_synthetic.customers`
    (customer_id, region, age_group, release_id)
  SELECT customer_id, region, age_group, @release_id FROM UNNEST(@customers);

  INSERT INTO `synthetic-release-gate-2026.published_synthetic.orders`
    (order_id, customer_id, order_date, status, release_id)
  SELECT order_id, customer_id, order_date, status, @release_id FROM UNNEST(@orders);

  INSERT INTO `synthetic-release-gate-2026.published_synthetic.order_items`
    (item_id, order_id, category, quantity, price, release_id)
  SELECT item_id, order_id, category, quantity, price, @release_id FROM UNNEST(@order_items);

  INSERT INTO `synthetic-release-gate-2026.published_synthetic.release_manifest`
    (release_id, run_id, content_hash, policy_hash, policy_version, decision,
     status, committed_at, publisher_job_id)
  VALUES (@release_id, @run_id, @content_hash, @policy_hash, @policy_version,
          'APPROVED', 'COMMITTED', CURRENT_TIMESTAMP(), @publisher_job_id);

  -- This success event and the manifest commit are atomic across datasets.
  INSERT INTO `synthetic-release-gate-2026.audit_private.audit_events`
    (event_id, event_type, run_id, release_id, content_hash, policy_hash,
     evaluation_decision, publisher_decision, verification_json,
     error_reason, event_timestamp, bigquery_job_id)
  VALUES (@event_id, 'PUBLICATION_COMMITTED', @run_id, @release_id,
          @content_hash, @policy_hash, 'APPROVED', 'APPROVED',
          PARSE_JSON(@verification_json), NULL, CURRENT_TIMESTAMP(), @publisher_job_id);

  COMMIT TRANSACTION;
EXCEPTION WHEN ERROR THEN
  ROLLBACK TRANSACTION;
  RAISE;
END;

-- Before this transaction, a separate mandatory audit intent is inserted.
-- After crashes, reconcile PUBLICATION_INTENT rows against COMMITTED manifests;
-- an intent without a committed manifest never authorizes publication.
