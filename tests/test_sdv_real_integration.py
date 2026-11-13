"""ACTUAL SDV smoke test, not a mock. Requires pip install -e '.[sdv,test]'."""
import importlib.util
import pytest
from gate.reference import make_reference
from gate.generate import generate
from gate.generate import _create_metadata
from gate.checks.hard import run_hard_checks
from gate.models import Status

@pytest.mark.skipif(importlib.util.find_spec('sdv') is None,reason='Optional SDV dependency not installed')
def test_real_sdv_multitable_fit_sample():
    output=generate(make_reference(40,seed=500),backend='sdv',seed=500,scale=0.5)
    assert output.backend=='sdv-hma'
    assert set(output.tables) == {'customers', 'orders', 'order_items'}
    assert all(len(t)>0 for t in output.tables.values())
    assert set(output.tables['customers']) == {'customer_id', 'region', 'age_group'}
    assert set(output.tables['orders']) == {'order_id', 'customer_id', 'order_date', 'status'}
    assert set(output.tables['order_items']) == {
        'item_id', 'order_id', 'category', 'quantity', 'price'
    }
    assert all(output.tables[name][key].is_unique for name, key in {
        'customers': 'customer_id', 'orders': 'order_id', 'order_items': 'item_id'
    }.items())
    assert set(output.tables['orders']['customer_id']) <= set(output.tables['customers']['customer_id'])
    assert set(output.tables['order_items']['order_id']) <= set(output.tables['orders']['order_id'])
    assert all(c.status is Status.PASS for c in run_hard_checks(output.tables))


def test_real_sdv_seed_is_reproducible():
    source = make_reference(20, seed=501)
    first = generate(source, backend='sdv', seed=501, scale=0.5)
    second = generate(source, backend='sdv', seed=501, scale=0.5)
    import pandas as pd
    for name in first.tables:
        pd.testing.assert_frame_equal(first.tables[name], second.tables[name])


def test_real_metadata_has_exact_relationships():
    metadata = _create_metadata(make_reference(20, seed=502))
    relationships = metadata.to_dict()['relationships']
    assert len(relationships) == 2
    assert {(r['parent_table_name'], r['parent_primary_key'],
             r['child_table_name'], r['child_foreign_key']) for r in relationships} == {
        ('customers', 'customer_id', 'orders', 'customer_id'),
        ('orders', 'order_id', 'order_items', 'order_id'),
    }
