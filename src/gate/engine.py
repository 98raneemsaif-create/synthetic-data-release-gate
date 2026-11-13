"""Pure, fail-closed evaluator: validated measurements + policy -> decision."""
import math
from gate.models import CheckResult, Decision, Evaluation, Status
from gate.policy import validate_policy

EXPECTED_KINDS = {
    'foreign_key_violations': 'hard',
    'duplicate_primary_keys': 'hard',
    'constraint_violations': 'hard',
    'fidelity_per_table': 'risk',
    'exact_match_rate': 'risk',
}
EXPECTED_TABLES = frozenset({'customers', 'orders', 'order_items'})


def _finite_number(x, *, fraction=False, integer=False) -> bool:
    if type(x) is bool or not isinstance(x, (int, float)) or not math.isfinite(x):
        return False
    if integer and (not isinstance(x, int) or x < 0):
        return False
    return 0 <= x <= 1 if fraction else x >= 0


def evaluate(checks: list[CheckResult], policy: dict, policy_hash: str,
             *, calibration_id: str | None = None, split_seed: int | None = None,
             source_fingerprint: str | None = None, generator_version: str | None = None) -> Evaluation:
    checks = list(checks)
    version = str(policy.get('policy_version', 'UNKNOWN')) if isinstance(policy, dict) else 'UNKNOWN'

    def _result(decision: Decision, reasons: list[str]) -> Evaluation:
        return Evaluation(decision, reasons, checks, policy_hash, version,
                          calibration_id, split_seed, source_fingerprint, generator_version)

    try:
        validate_policy(policy)
    except (ValueError, TypeError, KeyError) as exc:
        return _result(Decision.ERROR, [f'Invalid policy: {exc}'])

    expected = dict(EXPECTED_KINDS)
    if policy['privacy']['dcr_enabled']:
        expected['dcr_risk_rate'] = 'risk'
        if not calibration_id:
            return _result(Decision.ERROR, ['DCR enabled but calibration_id missing'])

    names = [c.name for c in checks]
    if len(names) != len(set(names)):
        return _result(Decision.ERROR, ['Duplicate check names'])
    if set(names) != set(expected):
        return _result(Decision.ERROR, [f'Check set mismatch: missing={sorted(set(expected)-set(names))}, unexpected={sorted(set(names)-set(expected))}'])
    for c in checks:
        if c.kind != expected[c.name] or type(c.status) is not Status:
            return _result(Decision.ERROR, [f'Invalid check kind or status: {c.name}'])
        if c.status is Status.ERROR:
            return _result(Decision.ERROR, [f'Check error: {c.name}: {c.detail}'])

    by_name = {c.name: c for c in checks}
    hard_fail, risk_fail = [], []
    for metric, limit in (('foreign_key_violations', 'max_foreign_key_violations'),
                          ('duplicate_primary_keys', 'max_duplicate_primary_keys'),
                          ('constraint_violations', 'max_constraint_violations')):
        c = by_name[metric]
        if not _finite_number(c.value, integer=True):
            return _result(Decision.ERROR, [f'Invalid hard check measurement: {metric}'])
        failed = c.value > policy['integrity'][limit]
        if (c.status is Status.FAIL) != failed:
            return _result(Decision.ERROR, [f'Inconsistent check status: {metric}'])
        if failed:
            hard_fail.append(f'{metric}={c.value} > {policy["integrity"][limit]}')

    fidelity = by_name['fidelity_per_table']
    if (not isinstance(fidelity.value, dict) or set(fidelity.value) != EXPECTED_TABLES
            or any(not _finite_number(v, fraction=True) for v in fidelity.value.values())):
        return _result(Decision.ERROR, ['Invalid or incomplete fidelity_per_table'])
    if fidelity.status is Status.FAIL or min(fidelity.value.values()) < policy['quality']['min_fidelity_per_table']:
        risk_fail.append('Fidelity below per-table threshold or check flagged failure')

    match = by_name['exact_match_rate']
    if not _finite_number(match.value, fraction=True):
        return _result(Decision.ERROR, ['Invalid exact_match_rate'])
    if match.value > policy['privacy']['critical_exact_match_rate']:
        hard_fail.append('Critical exact-match threshold exceeded')
    elif match.status is Status.FAIL or match.value > policy['privacy']['max_exact_match_rate']:
        risk_fail.append('Exact-match risk exceeded or check flagged failure')

    if policy['privacy']['dcr_enabled']:
        dcr = by_name['dcr_risk_rate']
        if not _finite_number(dcr.value, fraction=True):
            return _result(Decision.ERROR, ['Invalid dcr_risk_rate'])
        if dcr.status is Status.FAIL or dcr.value > policy['privacy']['max_dcr_risk_rate']:
            risk_fail.append('DCR risk rate above calibrated threshold or check flagged failure')

    if hard_fail:
        return _result(Decision.REJECTED, sorted(set(hard_fail)))
    if risk_fail:
        return _result(Decision.REVIEW_REQUIRED, sorted(set(risk_fail)))
    return _result(Decision.APPROVED, ['All configured checks passed'])


def evaluate_policy_file(checks: list[CheckResult], path, **provenance) -> Evaluation:
    """Application boundary: invalid policy becomes an auditable ERROR, never publishable.

    The evaluator above remains pure; this wrapper is responsible for file I/O.
    """
    import hashlib
    from pathlib import Path
    import yaml
    from gate.policy import load_policy
    raw = b''
    policy_hash = 'UNAVAILABLE'
    try:
        raw = Path(path).read_bytes()
        policy_hash = hashlib.sha256(raw).hexdigest()
        policy, _ = load_policy(path)
    except (OSError, ValueError, yaml.YAMLError, TypeError) as exc:
        return Evaluation(Decision.ERROR, [f'Policy load/validation error: {type(exc).__name__}: {exc}'],
                          list(checks), policy_hash, 'UNKNOWN',
                          provenance.get('calibration_id'), provenance.get('split_seed'),
                          provenance.get('source_fingerprint'), provenance.get('generator_version'))
    return evaluate(checks, policy, policy_hash, **provenance)
