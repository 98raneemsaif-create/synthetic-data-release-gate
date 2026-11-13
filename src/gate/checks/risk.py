"""Measured statistical fidelity and exact-match risk checks."""
from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from gate.checks.hard import TABLE_KEYS
from gate.models import CheckResult, Status

EXPECTED_TABLES = frozenset(TABLE_KEYS)
FIDELITY_COLUMNS = {
    'customers': ('region', 'age_group'),
    'orders': ('order_date', 'status'),
    'order_items': ('category', 'quantity', 'price'),
}
BEHAVIORAL_COLUMNS = (
    'order_count', 'item_count', 'total_quantity', 'total_spend',
    'completed_rate', 'date_span_days',
)


def _validate_tables(tables: Any, label: str) -> None:
    if not isinstance(tables, dict) or set(tables) != EXPECTED_TABLES:
        raise ValueError(f'{label} must contain exactly {sorted(EXPECTED_TABLES)}')
    for name, frame in tables.items():
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            raise ValueError(f'{label}.{name} must be a non-empty DataFrame')


def _validate_columns(tables: dict[str, pd.DataFrame], label: str) -> None:
    required = {
        'customers': {'customer_id', 'region', 'age_group'},
        'orders': {'order_id', 'customer_id', 'order_date', 'status'},
        'order_items': {'item_id', 'order_id', 'category', 'quantity', 'price'},
    }
    for name, columns in required.items():
        missing = columns - set(tables[name].columns)
        if missing:
            raise ValueError(f'{label}.{name} missing columns {sorted(missing)}')


def _finite_series(series: pd.Series, name: str) -> None:
    if series.isna().any():
        raise ValueError(f'{name} contains missing values')
    if pd.api.types.is_numeric_dtype(series):
        values = pd.to_numeric(series, errors='coerce').to_numpy(dtype=float)
        if not np.isfinite(values).all():
            raise ValueError(f'{name} contains non-finite values')


def _date_values(series: pd.Series, name: str) -> pd.Series:
    parsed = pd.to_datetime(series, errors='raise', utc=True)
    _finite_series(parsed, name)
    return pd.Series(parsed.astype('int64') / 86_400_000_000_000, index=series.index)


def _column_score(real: pd.Series, synthetic: pd.Series, name: str) -> float:
    """Return a real SDMetrics score in [0, 1] for one non-key column."""
    from sdmetrics.single_table import KSComplement, TVComplement

    _finite_series(real, name)
    _finite_series(synthetic, name)
    if 'date' in name:
        real_data = pd.DataFrame({name: _date_values(real, name)})
        synthetic_data = pd.DataFrame({name: _date_values(synthetic, name)})
        score = KSComplement.compute(real_data, synthetic_data)
    elif (pd.api.types.is_numeric_dtype(real)
          and pd.api.types.is_numeric_dtype(synthetic)):
        real_data = pd.DataFrame({name: pd.to_numeric(real, errors='raise')})
        synthetic_data = pd.DataFrame({name: pd.to_numeric(synthetic, errors='raise')})
        score = KSComplement.compute(real_data, synthetic_data)
    else:
        score = TVComplement.compute(
            pd.DataFrame({name: real.astype(str)}),
            pd.DataFrame({name: synthetic.astype(str)}),
        )
    score = float(score)
    if not math.isfinite(score) or not 0 <= score <= 1:
        raise ValueError(f'{name} produced an invalid score: {score!r}')
    return score


def measure_fidelity(reference: dict[str, pd.DataFrame],
                     synthetic: dict[str, pd.DataFrame]) -> dict[str, float]:
    """Measure fidelity using TVComplement and KSComplement on non-key columns."""
    _validate_tables(reference, 'reference')
    _validate_tables(synthetic, 'synthetic')
    _validate_columns(reference, 'reference')
    _validate_columns(synthetic, 'synthetic')
    scores = {}
    for table_name, columns in FIDELITY_COLUMNS.items():
        values = [_column_score(
            reference[table_name][column], synthetic[table_name][column],
            f'{table_name}.{column}',
        ) for column in columns]
        scores[table_name] = float(np.mean(values))
    return scores


def _behavioral_features(tables: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Build ID-free, entity-level behavioral fingerprints for customers."""
    _validate_columns(tables, 'tables')
    customers = tables['customers']
    orders = tables['orders'].copy()
    items = tables['order_items'].copy()
    if not customers['customer_id'].is_unique or not orders['order_id'].is_unique:
        raise ValueError('Primary keys must be unique for exact-match measurement')
    if not set(orders['customer_id']) <= set(customers['customer_id']):
        raise ValueError('orders contains orphan customer IDs')
    if not set(items['order_id']) <= set(orders['order_id']):
        raise ValueError('order_items contains orphan order IDs')
    orders['order_date'] = pd.to_datetime(orders['order_date'], errors='raise', utc=True)
    items['quantity'] = pd.to_numeric(items['quantity'], errors='raise')
    items['price'] = pd.to_numeric(items['price'], errors='raise')
    _finite_series(orders['order_date'], 'orders.order_date')
    _finite_series(items['quantity'], 'order_items.quantity')
    _finite_series(items['price'], 'order_items.price')
    items_by_customer = items.merge(orders[['order_id', 'customer_id']], on='order_id', how='left')
    order_stats = orders.groupby('customer_id').agg(
        order_count=('order_id', 'size'),
        completed_rate=('status', lambda values: float((values == 'completed').mean())),
        first_order=('order_date', 'min'),
        last_order=('order_date', 'max'),
    )
    item_stats = items_by_customer.groupby('customer_id').agg(
        item_count=('item_id', 'size'),
        total_quantity=('quantity', 'sum'),
        total_spend=('price', lambda values: float((values * items_by_customer.loc[values.index, 'quantity']).sum())),
    )
    features = customers[['customer_id']].merge(order_stats, on='customer_id', how='left')
    features = features.merge(item_stats, on='customer_id', how='left')
    features['order_count'] = features['order_count'].fillna(0)
    features['completed_rate'] = features['completed_rate'].fillna(0)
    features['item_count'] = features['item_count'].fillna(0)
    features['total_quantity'] = features['total_quantity'].fillna(0)
    features['total_spend'] = features['total_spend'].fillna(0)
    features['date_span_days'] = (
        features['last_order'] - features['first_order']
    ).dt.total_seconds().fillna(0).div(86_400)
    features = features[list(('customer_id',) + BEHAVIORAL_COLUMNS)]
    numeric = features[list(BEHAVIORAL_COLUMNS)].to_numpy(dtype=float)
    if not np.isfinite(numeric).all():
        raise ValueError('Behavioral features contain non-finite values')
    features['total_spend'] = features['total_spend'].round(2)
    features['completed_rate'] = features['completed_rate'].round(3)
    features['date_span_days'] = features['date_span_days'].round(3)
    return features


def measure_exact_match_rate(reference: dict[str, pd.DataFrame],
                             synthetic: dict[str, pd.DataFrame]) -> float:
    """Measure matching customer behavioral fingerprints, excluding all IDs."""
    real = _behavioral_features(reference)
    candidate = _behavioral_features(synthetic)
    if candidate.empty or real.empty:
        raise ValueError('Cannot measure exact matches on empty entities')
    real_keys = set(map(tuple, real[list(BEHAVIORAL_COLUMNS)].to_numpy()))
    matched = sum(tuple(row) in real_keys for row in candidate[list(BEHAVIORAL_COLUMNS)].to_numpy())
    rate = matched / len(candidate)
    if not math.isfinite(rate) or not 0 <= rate <= 1:
        raise ValueError(f'Invalid exact-match rate: {rate!r}')
    return float(rate)


def _valid_fidelity(value: Any) -> bool:
    return (isinstance(value, dict) and set(value) == EXPECTED_TABLES
            and all(type(score) is not bool and isinstance(score, (int, float))
                    and math.isfinite(score) and 0 <= score <= 1
                    for score in value.values()))


def _valid_rate(value: Any) -> bool:
    return (type(value) is not bool and isinstance(value, (int, float))
            and math.isfinite(value) and 0 <= value <= 1)


def measured_risk_checks(fidelity_per_table: dict[str, float] | None = None,
                         exact_match_rate: float | None = None, *,
                         reference: dict[str, pd.DataFrame] | None = None,
                         synthetic: dict[str, pd.DataFrame] | None = None) -> list[CheckResult]:
    """Return measured risk checks while preserving the original value contract."""
    if (reference is None) != (synthetic is None):
        raise ValueError('reference and synthetic must be supplied together')
    errors = {}
    if reference is not None:
        try:
            fidelity_per_table = measure_fidelity(reference, synthetic)
        except Exception as exc:
            errors['fidelity_per_table'] = f'{type(exc).__name__}: {exc}'
        try:
            exact_match_rate = measure_exact_match_rate(reference, synthetic)
        except Exception as exc:
            errors['exact_match_rate'] = f'{type(exc).__name__}: {exc}'
    output = []
    if 'fidelity_per_table' in errors:
        output.append(CheckResult('fidelity_per_table', Status.ERROR, 'risk',
                                  detail=errors['fidelity_per_table']))
    elif not _valid_fidelity(fidelity_per_table):
        output.append(CheckResult('fidelity_per_table', Status.ERROR, 'risk',
                                  detail='Missing or invalid measured fidelity'))
    else:
        output.append(CheckResult('fidelity_per_table', Status.PASS, 'risk',
                                  fidelity_per_table))
    if 'exact_match_rate' in errors:
        output.append(CheckResult('exact_match_rate', Status.ERROR, 'risk',
                                  detail=errors['exact_match_rate']))
    elif not _valid_rate(exact_match_rate):
        output.append(CheckResult('exact_match_rate', Status.ERROR, 'risk',
                                  detail='Missing or invalid measured exact match'))
    else:
        output.append(CheckResult('exact_match_rate', Status.PASS, 'risk',
                                  float(exact_match_rate)))
    return output
