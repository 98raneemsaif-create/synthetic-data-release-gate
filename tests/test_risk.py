import copy
import json
import math

import pandas as pd
import pytest

from gate.checks.risk import measure_exact_match_rate, measure_fidelity, measured_risk_checks
from gate.engine import evaluate
from gate.models import Decision, Status
from gate.reference import make_reference


def test_real_measurements_are_bounded_and_not_key_based():
    tables = make_reference(25, seed=7)
    fidelity = measure_fidelity(tables, tables)
    assert fidelity == {'customers': 1.0, 'orders': 1.0, 'order_items': 1.0}
    assert measure_exact_match_rate(tables, tables) == 1.0


def test_low_fidelity_data_is_measured_as_low():
    reference = make_reference(25, seed=8)
    synthetic = {name: frame.copy(deep=True) for name, frame in reference.items()}
    synthetic['customers']['region'] = 'Other'
    synthetic['orders']['status'] = 'unknown'
    synthetic['order_items']['category'] = 'Other'
    synthetic['order_items']['quantity'] = 99
    synthetic['order_items']['price'] = 9999.0
    scores = measure_fidelity(reference, synthetic)
    assert min(scores.values()) < 0.8


def test_behavioral_exact_match_ignores_ids_but_detects_changed_behavior():
    reference = make_reference(25, seed=9)
    synthetic = {name: frame.copy(deep=True) for name, frame in reference.items()}
    synthetic['customers']['customer_id'] = [f'NEW{i}' for i in range(len(synthetic['customers']))]
    synthetic['orders']['customer_id'] = synthetic['orders']['customer_id'].map(
        dict(zip(reference['customers']['customer_id'], synthetic['customers']['customer_id']))
    )
    assert measure_exact_match_rate(reference, synthetic) == 1.0
    synthetic['order_items']['price'] += 1.23
    assert measure_exact_match_rate(reference, synthetic) == 0.0


@pytest.mark.parametrize('mutator', [
    lambda tables: tables['order_items'].drop(columns=['price'], inplace=True),
    lambda tables: tables['order_items'].__setitem__('price', math.nan),
    lambda tables: tables['order_items'].__setitem__('price', math.inf),
])
def test_invalid_measurements_are_errors(mutator):
    reference = make_reference(20, seed=10)
    synthetic = {name: frame.copy(deep=True) for name, frame in reference.items()}
    mutator(synthetic)
    checks = measured_risk_checks(reference=reference, synthetic=synthetic)
    assert {check.name for check in checks} == {'fidelity_per_table', 'exact_match_rate'}
    assert all(check.status is Status.ERROR for check in checks)


def test_legacy_measurement_values_are_validated():
    checks = measured_risk_checks(
        {'customers': 1.0, 'orders': 1.0, 'order_items': 1.0}, math.nan,
    )
    assert checks[0].status is Status.PASS
    assert checks[1].status is Status.ERROR


def test_measured_checks_feed_policy_decisions(policy):
    policy_data, policy_hash = policy
    reference = make_reference(20, seed=11)
    same = measured_risk_checks(reference=reference, synthetic=reference)
    hard = []
    from gate.checks.hard import run_hard_checks
    hard.extend(run_hard_checks(reference))
    rejected = evaluate(hard + same, policy_data, policy_hash)
    assert rejected.decision is Decision.REJECTED

    measured = measured_risk_checks(
        {'customers': 0.9, 'orders': 0.9, 'order_items': 0.9}, 0.0,
    )
    approved = evaluate(hard + measured, policy_data, policy_hash)
    assert approved.decision is Decision.APPROVED


def test_policy_threshold_boundaries(policy):
    policy_data, policy_hash = policy
    from gate.checks.hard import run_hard_checks
    hard = run_hard_checks(make_reference(20, seed=13))
    at_limit = measured_risk_checks(
        {'customers': 0.8, 'orders': 0.8, 'order_items': 0.8}, 0.01,
    )
    assert evaluate(hard + at_limit, policy_data, policy_hash).decision is Decision.APPROVED
    just_over = measured_risk_checks(
        {'customers': 0.799999, 'orders': 0.8, 'order_items': 0.8}, 0.01,
    )
    assert evaluate(hard + just_over, policy_data, policy_hash).decision is Decision.REVIEW_REQUIRED


def test_measurement_error_prevents_approval(policy):
    policy_data, policy_hash = policy
    reference = make_reference(20, seed=12)
    from gate.checks.hard import run_hard_checks
    synthetic = {name: frame.copy(deep=True) for name, frame in reference.items()}
    del synthetic['order_items']['price']
    checks = run_hard_checks(synthetic) + measured_risk_checks(
        reference=reference, synthetic=synthetic,
    )
    result = evaluate(checks, policy_data, policy_hash)
    assert result.decision is Decision.ERROR
