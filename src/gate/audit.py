"""Create audit dictionaries. Cloud persistence is not implemented in this milestone."""
from datetime import datetime, timezone
from dataclasses import asdict
from gate.models import Evaluation


def audit_record(run_id: str, result: Evaluation, *, timestamp: datetime | None = None) -> dict:
    ts = timestamp if timestamp is not None else datetime.now(timezone.utc)
    if ts.tzinfo is None or ts.utcoffset() is None:
        raise ValueError('Audit timestamp must be timezone-aware')
    return {
        'run_id': run_id,
        'timestamp': ts.isoformat(),
        'decision': result.decision.value,
        'policy_hash': result.policy_hash,
        'policy_version': result.policy_version,
        'calibration_id': result.calibration_id,
        'split_seed': result.split_seed,
        'source_fingerprint': result.source_fingerprint,
        'generator_version': result.generator_version,
        'reasons': result.reasons,
        'checks': [{**asdict(c), 'status': c.status.value} for c in result.checks],
    }
