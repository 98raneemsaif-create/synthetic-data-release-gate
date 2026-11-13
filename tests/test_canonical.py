import pandas as pd
import pytest
from decimal import Decimal

from gate.canonical import CanonicalizationError, canonical_content_hash


def _tables():
    return {
        "customers": pd.DataFrame({"customer_id": ["c1", "c2"], "region": ["R", "J"], "age_group": ["18-24", "25-34"]}),
        "orders": pd.DataFrame({"order_id": ["o1", "o2"], "customer_id": ["c1", "c2"], "order_date": ["2026-01-01", "2026-01-02"], "status": ["completed", "pending"]}),
        "order_items": pd.DataFrame({"item_id": ["i1", "i2"], "order_id": ["o1", "o2"], "category": ["A", "B"], "quantity": [1, 2], "price": pd.Series([1.5, 2.5], dtype=object)}),
    }


def test_hash_is_bound_to_run_and_ignores_row_order():
    tables = _tables()
    reordered = {name: frame.iloc[::-1].reset_index(drop=True) for name, frame in tables.items()}
    assert canonical_content_hash(tables, "run-1") == canonical_content_hash(reordered, "run-1")
    assert canonical_content_hash(tables, "run-2") != canonical_content_hash(tables, "run-1")


def test_hash_preserves_duplicate_row_multiplicity_and_detects_one_cell():
    tables = _tables()
    duplicate = {name: frame.copy() for name, frame in tables.items()}
    duplicate["orders"] = pd.concat([duplicate["orders"], duplicate["orders"].iloc[[0]]], ignore_index=True)
    changed = {name: frame.copy() for name, frame in tables.items()}
    changed["order_items"].loc[0, "price"] = 1.51
    base = canonical_content_hash(tables, "run-1")
    assert canonical_content_hash(duplicate, "run-1") != base
    assert canonical_content_hash(changed, "run-1") != base


def test_bigquery_decimal_numeric_matches_float_snapshot():
    tables = _tables()
    decimal_tables = {name: frame.copy(deep=True) for name, frame in tables.items()}
    decimal_tables["order_items"]["price"] = decimal_tables["order_items"]["price"].map(Decimal)
    assert canonical_content_hash(decimal_tables, "run-1") == canonical_content_hash(tables, "run-1")


def test_numeric_distinct_scale9_values_remain_distinct():
    low = _tables()
    high = _tables()
    low["order_items"].loc[0, "price"] = Decimal("1.000000001")
    high["order_items"].loc[0, "price"] = Decimal("1.000000002")
    assert canonical_content_hash(low, "run-1") != canonical_content_hash(high, "run-1")


def test_numeric_precision_and_scale_boundaries_are_fail_closed():
    valid = _tables()
    valid["order_items"].loc[0, "price"] = Decimal("99999999999999999999999999999.999999999")
    canonical_content_hash(valid, "run-1")
    too_precise = _tables()
    too_precise["order_items"].loc[0, "price"] = Decimal("1.0000000001")
    with pytest.raises(CanonicalizationError):
        canonical_content_hash(too_precise, "run-1")
    too_large = _tables()
    too_large["order_items"].loc[0, "price"] = Decimal("100000000000000000000000000000")
    with pytest.raises(CanonicalizationError):
        canonical_content_hash(too_large, "run-1")


@pytest.mark.parametrize("mutator", [
    lambda t: t["order_items"].__setitem__("price", float("inf")),
    lambda t: t["customers"].__setitem__("region", None),
    lambda t: t.pop("orders"),
])
def test_malformed_snapshot_is_rejected(mutator):
    tables = _tables()
    mutator(tables)
    with pytest.raises(CanonicalizationError):
        canonical_content_hash(tables, "run-1")
