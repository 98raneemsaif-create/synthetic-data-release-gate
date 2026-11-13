from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_transaction_design_is_parameterized_and_single_writer():
    sql = (ROOT / "scripts/cloud/publish_transaction.sql").read_text(encoding="utf-8")
    assert "BEGIN TRANSACTION" in sql
    assert "DELETE FROM" in sql and "@lock_release_id" in sql
    assert "SYSTEM_LOCK" in sql
    assert "@customers" in sql and "@orders" in sql and "@order_items" in sql
    assert "audit_private.audit_events" in sql
    assert "COMMIT TRANSACTION" in sql and "ROLLBACK TRANSACTION" in sql


def test_consumer_contract_requires_committed_manifest():
    sql = (ROOT / "scripts/cloud/consumer_contract.sql").read_text(encoding="utf-8")
    assert "release_manifest" in sql
    assert "status = 'COMMITTED'" in sql
    assert "@release_id" in sql


def test_fixture_runner_is_explicit_and_does_not_drop_permanent_tables():
    runner = (ROOT / "scripts/cloud/fixture_runner.py").read_text(encoding="utf-8")
    assert '"--execute"' in runner
    assert 'if not args.execute' in runner
    assert "permanent_tables_retained" in runner
    assert "DROP TABLE" not in runner.upper()
    assert "create_operator_client" in runner
