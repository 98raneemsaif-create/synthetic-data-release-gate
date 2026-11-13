from gate.checks.hard import run_hard_checks
from gate.models import Status

def test_valid_relations(tables):
    assert all(c.status is Status.PASS for c in run_hard_checks(tables))

def test_broken_fk_detected(tables):
    tables['order_items'].loc[0, 'order_id']=9999
    checks=run_hard_checks(tables)
    assert next(c for c in checks if c.name=='foreign_key_violations').status is Status.FAIL

def test_duplicate_pk_detected(tables):
    tables['orders'].loc[1,'order_id']=10
    assert next(c for c in run_hard_checks(tables) if c.name=='duplicate_primary_keys').status is Status.FAIL
