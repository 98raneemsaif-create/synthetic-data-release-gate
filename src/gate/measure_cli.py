"""Evaluate an SDV candidate with measured checks and a YAML policy."""
from __future__ import annotations

import argparse
import json
from importlib.metadata import PackageNotFoundError, version
from dataclasses import asdict
from pathlib import Path

import pandas as pd

from gate.audit import audit_record
from gate.canonical import canonical_content_hash, canonical_reference_hash
from gate.checks.hard import run_hard_checks
from gate.checks.risk import FIDELITY_COLUMNS, BEHAVIORAL_COLUMNS, measured_risk_checks
from gate.engine import evaluate_policy_file
from gate.models import Evaluation
from gate.reference import make_reference


TABLES = ('customers', 'orders', 'order_items')


def _load_candidate(path: Path) -> tuple[dict[str, pd.DataFrame], dict]:
    manifest_path = path / 'generation_manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    tables = {name: pd.read_csv(path / f'{name}.csv') for name in TABLES}
    return tables, manifest


def _fingerprint(tables: dict[str, pd.DataFrame], *, customers: int, seed: int) -> str:
    return canonical_reference_hash(tables, customers=customers, seed=seed)


def _runtime_version(package: str) -> str:
    try:
        return version(package)
    except PackageNotFoundError:
        return 'unavailable'


def evaluate_candidate(reference: dict[str, pd.DataFrame], candidate_dir: str | Path,
                       policy_path: str | Path, report_path: str | Path,
                       *, run_id: str, reference_customers: int,
                       reference_seed: int) -> Evaluation:
    candidate_path = Path(candidate_dir)
    synthetic, manifest = _load_candidate(candidate_path)
    checks = run_hard_checks(synthetic) + measured_risk_checks(
        reference=reference, synthetic=synthetic,
    )
    generation_metadata = manifest.get('generation_metadata', {})
    sdv_version = generation_metadata.get('sdv_version', 'unknown')
    generator_version = f"{manifest.get('backend', 'unknown')}/{sdv_version}"
    result = evaluate_policy_file(
        checks, policy_path,
        split_seed=None,
        source_fingerprint=_fingerprint(
            reference, customers=reference_customers, seed=reference_seed
        ),
        generator_version=generator_version,
    )
    report = audit_record(run_id, result)
    provenance = manifest.get('provenance', {})
    generation_run_id = provenance.get('generation_run_id')
    candidate_hash_binding_id = (
        generation_run_id if isinstance(generation_run_id, str) and generation_run_id.strip()
        else run_id
    )
    candidate_content_hash = canonical_content_hash(synthetic, candidate_hash_binding_id)
    lineage_fields = {
        'generation_run_id', 'reference_parameters', 'reference_hash',
        'candidate_content_hash',
    }
    lineage_verified = lineage_fields.issubset(provenance) and (
        provenance.get('candidate_content_hash') == candidate_content_hash
    )
    report.update({
        'evaluation_run_id': run_id,
        'generation_run_id': provenance.get(
            'generation_run_id', 'UNVERIFIED_LEGACY_GENERATION'
        ),
        'lineage_status': 'VERIFIED' if lineage_verified else 'LEGACY_UNVERIFIED',
        'reference_parameters': {
            'customers': reference_customers, 'seed': reference_seed
        },
        'reference_hash': canonical_reference_hash(
            reference, customers=reference_customers, seed=reference_seed
        ),
        'candidate_content_hash': candidate_content_hash,
        'candidate_hash_binding_run_id': candidate_hash_binding_id,
        'candidate_directory': str(candidate_path),
        'generator_backend': manifest.get('backend'),
        'generation_manifest': manifest,
        'measurement_configuration': {
            'fidelity_metric': 'SDMetrics KSComplement/TVComplement',
            'fidelity_columns': FIDELITY_COLUMNS,
            'exact_match_metric': 'customer behavioral fingerprint exact match',
            'exact_match_columns': list(BEHAVIORAL_COLUMNS),
            'ids_excluded_from_exact_match': True,
            'dcr_enabled': False,
            'privacy_claim': 'NO_PRIVACY_CERTIFICATION',
            'privacy_limitation': 'Exact-match checks can miss near-copies; DCR is disabled.',
        },
        'metrics': {
            check.name: {
                'status': check.status.value,
                'value': check.value,
                'detail': check.detail,
            }
            for check in checks if check.kind == 'risk'
        },
        'runtime_versions': {
            'sdv': sdv_version,
            'rdt': _runtime_version('rdt'),
        },
        'decision': result.decision.value,
    })
    Path(report_path).write_text(json.dumps(report, indent=2, default=str), encoding='utf-8')
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--customers', type=int, required=True)
    parser.add_argument('--seed', type=int, required=True)
    parser.add_argument('--candidate', required=True)
    parser.add_argument('--policy', default='policies/synthetic-release-v1.yaml')
    parser.add_argument('--report', required=True)
    parser.add_argument('--run-id', default='milestone-2-measurement')
    args = parser.parse_args()
    reference = make_reference(args.customers, args.seed)
    result = evaluate_candidate(
        reference, args.candidate, args.policy, args.report, run_id=args.run_id,
        reference_customers=args.customers, reference_seed=args.seed,
    )
    print(f'Decision: {result.decision.value}')
    print(f'Reasons: {result.reasons}')


if __name__ == '__main__':
    main()
