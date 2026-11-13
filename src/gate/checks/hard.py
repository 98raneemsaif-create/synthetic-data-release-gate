"""Strict hard checks for a fixed, three-table e-commerce reference schema."""
import pandas as pd
from gate.models import CheckResult, Status

TABLE_KEYS = {'customers': 'customer_id', 'orders': 'order_id', 'order_items': 'item_id'}
REQUIRED_COLUMNS = {
    'customers': {'customer_id'},
    'orders': {'order_id', 'customer_id'},
    'order_items': {'item_id', 'order_id', 'quantity'},
}


def run_hard_checks(tables: dict[str, pd.DataFrame]) -> list[CheckResult]:
    try:
        if set(tables) != set(TABLE_KEYS):
            raise ValueError(f'Incorrect table set: expected {sorted(TABLE_KEYS)}')
        for name, columns in REQUIRED_COLUMNS.items():
            if not isinstance(tables[name], pd.DataFrame):
                raise TypeError(f'{name} is not a DataFrame')
            missing = columns - set(tables[name].columns)
            if missing:
                raise ValueError(f'Missing required columns in {name}: {sorted(missing)}')

        duplicates = sum(int(tables[name][key].duplicated().sum()) for name, key in TABLE_KEYS.items())
        foreign_keys = (
            int((~tables['orders']['customer_id'].isin(tables['customers']['customer_id'])).sum())
            + int((~tables['order_items']['order_id'].isin(tables['orders']['order_id'])).sum())
        )
        nulls = sum(int(tables[name][key].isna().sum()) for name,key in TABLE_KEYS.items())
        nulls += int(tables['orders']['customer_id'].isna().sum())
        nulls += int(tables['order_items']['order_id'].isna().sum())
        numeric_quantity = pd.to_numeric(tables['order_items']['quantity'], errors='coerce')
        bad_quantity = int((numeric_quantity.isna() | (numeric_quantity <= 0) | (numeric_quantity % 1 != 0)).sum())
        constraints = nulls + bad_quantity
        return [
            CheckResult('foreign_key_violations', Status.PASS if foreign_keys==0 else Status.FAIL, 'hard', foreign_keys),
            CheckResult('duplicate_primary_keys', Status.PASS if duplicates==0 else Status.FAIL, 'hard', duplicates),
            CheckResult('constraint_violations', Status.PASS if constraints==0 else Status.FAIL, 'hard', constraints),
        ]
    except Exception as exc:
        return [CheckResult('hard_checks', Status.ERROR, 'hard', detail=f'{type(exc).__name__}: {exc}')]
