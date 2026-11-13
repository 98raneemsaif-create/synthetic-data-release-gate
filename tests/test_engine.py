from copy import deepcopy
from gate.engine import evaluate
from gate.policy import load_policy
from gate.models import Status, CheckResult, Decision
from gate.publisher import publish_decision_guard
from gate.audit import audit_record
import pytest

def test_approve(good_checks,policy):
    p,h=policy
    result=evaluate(good_checks,p,h)
    assert result.decision is Decision.APPROVED
    publish_decision_guard(result)
    assert len(audit_record('run1', result)['checks']) == 5

def test_hard_failure_rejected(good_checks,policy):
    p,h=policy
    changed=[CheckResult(c.name,Status.FAIL if c.name=='foreign_key_violations' else c.status,c.kind,1 if c.name=='foreign_key_violations' else c.value) for c in good_checks]
    result=evaluate(changed,p,h)
    assert result.decision is Decision.REJECTED
    with pytest.raises(PermissionError): publish_decision_guard(result)

def test_critical_match_rejected(good_checks,policy):
    p,h=policy
    changed=[CheckResult(c.name,c.status,c.kind,0.9 if c.name=='exact_match_rate' else c.value) for c in good_checks]
    assert evaluate(changed,p,h).decision is Decision.REJECTED

def test_uncalibrated_risk_review(good_checks,policy):
    p,h=policy
    changed=[CheckResult(c.name,c.status,c.kind,0.08 if c.name=='exact_match_rate' else c.value) for c in good_checks]
    assert evaluate(changed,p,h).decision is Decision.REVIEW_REQUIRED

def test_checker_exception_fail_closed(good_checks,policy):
    def broken_checker():
        raise RuntimeError('Injected failure')
    try:
        broken_checker()
    except Exception as exc:
        checks=[c for c in good_checks if c.name!='foreign_key_violations']+[CheckResult('foreign_key_violations',Status.ERROR,'hard',detail=str(exc))]
    p,h=policy
    result=evaluate(checks,p,h)
    assert result.decision is Decision.ERROR
    with pytest.raises(PermissionError): publish_decision_guard(result)

def test_missing_check_fail_closed(good_checks,policy):
    p,h=policy
    assert evaluate(good_checks[:-1],p,h).decision is Decision.ERROR

def test_policy_changes_decision_without_regeneration(good_checks,policy):
    p,h=policy
    previous=evaluate(good_checks,p,h)
    changed=deepcopy(p)
    changed['quality']['min_fidelity_per_table']=0.95
    later=evaluate(good_checks,changed,'new_policy_hash')
    assert previous.decision is Decision.APPROVED
    assert later.decision is Decision.REVIEW_REQUIRED
    assert previous.policy_hash != later.policy_hash

def test_dcr_requires_calibration(good_checks,policy):
    p,h=policy
    changed=deepcopy(p)
    changed['privacy']['dcr_enabled']=True
    assert evaluate(good_checks,changed,h).decision is Decision.ERROR
