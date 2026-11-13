from pathlib import Path
import pytest
import pandas as pd
from gate.policy import load_policy
from gate.checks.hard import run_hard_checks
from gate.models import CheckResult, Status

@pytest.fixture
def policy():
    return load_policy(Path(__file__).resolve().parent.parent / "policies" / "synthetic-release-v1.yaml")

@pytest.fixture
def tables():
    return {
        'customers':pd.DataFrame({'customer_id':[1,2], 'region':['Riyadh','Jeddah']}),
        'orders':pd.DataFrame({'order_id':[10,11], 'customer_id':[1,2]}),
        'order_items':pd.DataFrame({'item_id':[100,101], 'order_id':[10,11], 'quantity':[1,2]})
    }

@pytest.fixture
def good_checks(tables):
    return run_hard_checks(tables)+[
        CheckResult('fidelity_per_table', Status.PASS, 'risk', {'customers':0.91,'orders':0.88,'order_items':0.90}),
        CheckResult('exact_match_rate', Status.PASS, 'risk', 0.0)
    ]
