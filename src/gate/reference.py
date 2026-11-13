"""Deterministic, fully simulated e-commerce reference data for SDV training."""
from __future__ import annotations
import numpy as np
import pandas as pd
from gate.checks.hard import run_hard_checks
from gate.models import Status


def make_reference(n_customers: int = 300, seed: int = 2026) -> dict[str, pd.DataFrame]:
    if type(n_customers) is not int or n_customers < 12:
        raise ValueError('n_customers must be an integer >= 12')
    if type(seed) is not int or seed < 0:
        raise ValueError('seed must be a nonnegative integer')
    rng = np.random.default_rng(seed)
    regions = rng.choice(['Riyadh', 'Makkah', 'Eastern', 'Asir'], n_customers, p=[.48,.25,.19,.08])
    ages = rng.choice(['18-24', '25-34', '35-44', '45+'], n_customers, p=[.16,.41,.28,.15])
    customers = pd.DataFrame({'customer_id': [f'C{i:07d}' for i in range(1,n_customers+1)], 'region': regions, 'age_group': ages})
    order_rows, item_rows = [], []
    oid, iid = 0, 0
    categories = ['Electronics', 'Home', 'Groceries', 'Fashion']
    for i, region in enumerate(regions, 1):
        count = int(rng.integers(1, 6 if region == 'Riyadh' else 4))
        for _ in range(count):
            oid += 1
            date = pd.Timestamp('2025-01-01') + pd.Timedelta(days=int(rng.integers(0,365)))
            order_rows.append({'order_id':f'O{oid:08d}', 'customer_id':f'C{i:07d}',
                               'order_date':date, 'status':str(rng.choice(['completed','cancelled'],p=[.9,.1]))})
            for _ in range(int(rng.integers(1,5))):
                iid += 1
                cat = str(rng.choice(categories,p=[.15,.24,.40,.21]))
                price = {'Electronics':180,'Home':92,'Groceries':22,'Fashion':75}[cat]
                item_rows.append({'item_id':f'I{iid:09d}','order_id':f'O{oid:08d}',
                                  'category':cat,'quantity':int(rng.integers(1,5)),
                                  'price':round(float(rng.lognormal(np.log(price),.35)),2)})
    tables = {'customers':customers,'orders':pd.DataFrame(order_rows),'order_items':pd.DataFrame(item_rows)}
    failures = [c for c in run_hard_checks(tables) if c.status is not Status.PASS]
    if failures:
        raise RuntimeError(f'Reference integrity failure: {failures}')
    return tables
