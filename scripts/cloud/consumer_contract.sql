-- Design only. Consumers must use the authoritative COMMITTED manifest.
-- Dataset-wide dataViewer does not prevent direct reads of base tables.
SELECT c.*
FROM `synthetic-release-gate-2026.published_synthetic.customers` AS c
JOIN `synthetic-release-gate-2026.published_synthetic.release_manifest` AS m
  ON m.release_id = c.release_id
WHERE m.status = 'COMMITTED'
  AND m.release_id = @release_id;
