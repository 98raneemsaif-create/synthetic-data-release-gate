"""Explicit check, decision, and provenance types."""
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

class Status(str, Enum):
    PASS = 'PASS'
    FAIL = 'FAIL'
    ERROR = 'ERROR'

class Decision(str, Enum):
    APPROVED = 'APPROVED'
    REJECTED = 'REJECTED'
    REVIEW_REQUIRED = 'REVIEW_REQUIRED'
    ERROR = 'ERROR'

@dataclass(frozen=True)
class CheckResult:
    name: str
    status: Status
    kind: str  # 'hard' or 'risk'
    value: Any = None
    detail: str = ''

@dataclass(frozen=True)
class Evaluation:
    decision: Decision
    reasons: list[str]
    checks: list[CheckResult]
    policy_hash: str
    policy_version: str
    calibration_id: str | None = None
    split_seed: int | None = None
    source_fingerprint: str | None = None
    generator_version: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
