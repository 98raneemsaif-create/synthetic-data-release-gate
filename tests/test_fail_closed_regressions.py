"""Regression coverage for all four previously observed fail-open paths."""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import math
import pytest
import yaml
from gate.models import CheckResult, Decision, Status
from gate.engine import evaluate, evaluate_policy_file
from gate.policy import load_policy
from gate.audit import audit_record
from gate.checks.hard import run_hard_checks
from gate.publisher import publish_decision_guard


def replace(checks, name, value):
    return [CheckResult(c.name, c.status, c.kind, value if c.name == name else c.value, c.detail)
            for c in checks]


@pytest.mark.parametrize('metric', ['foreign_key_violations','duplicate_primary_keys','constraint_violations'])
@pytest.mark.parametrize('value', [math.nan, math.inf, -math.inf, True])
def test_bad_hard_values_fail_closed(metric, value, good_checks, policy):
    p,h = policy
    result = evaluate(replace(good_checks, metric, value), p, h)
    assert result.decision is Decision.ERROR
    with pytest.raises(PermissionError):
        publish_decision_guard(result)


def test_fidelity_must_have_all_three_tables(good_checks, policy):
    p,h=policy
    result = evaluate(replace(good_checks, 'fidelity_per_table', {'customers':0.99}),p,h)
    assert result.decision is Decision.ERROR


@pytest.mark.parametrize('value', [math.nan, math.inf, True])
def test_bad_risk_values_fail_closed(value, good_checks, policy):
    p,h=policy
    assert evaluate(replace(good_checks, 'exact_match_rate', value),p,h).decision is Decision.ERROR


def test_missing_quantity_returns_error(tables):
    tables['order_items'].drop(columns=['quantity'], inplace=True)
    results=run_hard_checks(tables)
    assert any(c.status is Status.ERROR for c in results)
    assert 'quantity' in results[0].detail


def test_invalid_quantity_is_not_silently_passed(tables):
    tables['order_items']['quantity'] = tables['order_items']['quantity'].astype(object)
    tables['order_items'].loc[0,'quantity']='not-a-number'
    result=run_hard_checks(tables)
    assert next(c for c in result if c.name == 'constraint_violations').status is Status.FAIL


def test_missing_policy_threshold_auditable_error(good_checks, tmp_path, policy):
    p,_=policy
    changed=deepcopy(p)
    del changed['quality']['min_fidelity_per_table']
    path=tmp_path/'invalid-policy.yaml'
    path.write_text(yaml.safe_dump(changed), encoding='utf-8')
    with pytest.raises(ValueError):
        load_policy(path)
    result=evaluate_policy_file(good_checks, path)
    assert result.decision is Decision.ERROR
    record=audit_record('bad-policy',result,timestamp=datetime(2026,11,10,tzinfo=timezone.utc))
    assert record['decision']=='ERROR'
    assert record['policy_hash']!='UNAVAILABLE'
    assert 'quality.min_fidelity_per_table' in record['reasons'][0]
    with pytest.raises(PermissionError): publish_decision_guard(result)


def test_policy_file_changes_hash_and_decision_without_regeneration(tmp_path,good_checks,policy):
    p,_=policy
    f=tmp_path/'policy.yaml'
    f.write_text(yaml.safe_dump(p, sort_keys=True),encoding='utf-8')
    original=evaluate_policy_file(good_checks, f)
    changed=deepcopy(p)
    changed['quality']['min_fidelity_per_table']=0.95
    f.write_text(yaml.safe_dump(changed, sort_keys=True),encoding='utf-8')
    updated=evaluate_policy_file(good_checks, f)
    assert original.decision is Decision.APPROVED
    assert updated.decision is Decision.REVIEW_REQUIRED
    assert original.policy_hash != updated.policy_hash
    assert [c.value for c in original.checks] == [c.value for c in updated.checks]


@pytest.mark.parametrize('broken', [
    ('privacy','critical_exact_match_rate',0.0),
    ('privacy','dcr_enabled',True),
    ('integrity','max_foreign_key_violations',-1),
    ('quality','min_fidelity_per_table',float('nan')),
    ('quality','min_fidelity_per_table',0),
])
def test_invalid_policy_is_error(broken, good_checks, policy):
    p,h=policy
    p=deepcopy(p)
    section, key, val=broken
    p[section][key]=val
    assert evaluate(good_checks,p,h).decision is Decision.ERROR


def test_datetime_explicit_and_stable(policy,good_checks):
    p,h=policy
    result=evaluate(good_checks,p,h)
    t=datetime(2026,11,10,12,0,tzinfo=timezone.utc)
    assert audit_record('run',result,timestamp=t)==audit_record('run',result,timestamp=t)


def test_unexpected_metric_is_error(policy, good_checks):
    p,h=policy
    extra=CheckResult('unrecognized_metric',Status.PASS,'risk',0)
    assert evaluate(good_checks+[extra],p,h).decision is Decision.ERROR
