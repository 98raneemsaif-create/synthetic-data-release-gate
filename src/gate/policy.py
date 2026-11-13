"""Strictly validate and fingerprint the policy. YAML is data, not code."""
import hashlib
import math
from pathlib import Path
from typing import Any
import yaml

REQUIRED = {'policy_id', 'policy_version', 'quality', 'privacy', 'integrity', 'release'}


def _number(value: Any, field: str, *, minimum: float = 0, maximum: float = 1) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f'{field} must be a finite number')
    if not minimum <= value <= maximum:
        raise ValueError(f'{field} must be between {minimum} and {maximum}')


def validate_policy(policy: Any) -> None:
    if not isinstance(policy, dict) or not REQUIRED.issubset(policy):
        raise ValueError('Incomplete release policy')
    for section in ('quality', 'privacy', 'integrity', 'release'):
        if not isinstance(policy[section], dict):
            raise ValueError(f'{section} must be a mapping')
    for field in ('policy_id', 'policy_version'):
        if not isinstance(policy[field], str) or not policy[field].strip():
            raise ValueError(f'{field} must be a nonempty string')
    if policy['release'].get('require_all_checks') is not True:
        raise ValueError('release.require_all_checks must be true')
    _number(policy['quality'].get('min_fidelity_per_table'), 'quality.min_fidelity_per_table')
    if policy['quality']['min_fidelity_per_table'] == 0:
        raise ValueError('quality.min_fidelity_per_table must be > 0 (non-vacuous gate)')
    for field in ('max_exact_match_rate', 'critical_exact_match_rate'):
        _number(policy['privacy'].get(field), f'privacy.{field}')
    if policy['privacy']['critical_exact_match_rate'] < policy['privacy']['max_exact_match_rate']:
        raise ValueError('critical_exact_match_rate must be >= max_exact_match_rate')
    enabled = policy['privacy'].get('dcr_enabled')
    if type(enabled) is not bool:
        raise ValueError('privacy.dcr_enabled must be boolean')
    if enabled:
        _number(policy['privacy'].get('max_dcr_risk_rate'), 'privacy.max_dcr_risk_rate')
    for field in ('max_foreign_key_violations', 'max_constraint_violations', 'max_duplicate_primary_keys'):
        val = policy['integrity'].get(field)
        if type(val) is not int or val < 0:
            raise ValueError(f'integrity.{field} must be a nonnegative integer')


def load_policy(path: str | Path) -> tuple[dict, str]:
    raw = Path(path).read_bytes()
    policy = yaml.safe_load(raw)
    validate_policy(policy)
    return policy, hashlib.sha256(raw).hexdigest()
