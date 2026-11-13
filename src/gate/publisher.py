"""Fail-closed publisher validation and a deterministic local transaction adapter.

The local adapter is deliberately separate from BigQuery. It proves the handoff,
hash, policy, decision, duplicate-release, rollback, and audit-failure rules
without claiming cloud authorization or cloud atomicity.
"""

from __future__ import annotations

from copy import deepcopy
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Any, Mapping, Protocol

import pandas as pd

from gate.canonical import CanonicalizationError, canonical_content_hash, snapshot_tables
from gate.checks.hard import run_hard_checks
from gate.engine import evaluate
from gate.models import CheckResult, Decision, Evaluation, Status
from gate.policy import load_policy


class PublicationError(RuntimeError):
    """Any validation, authorization, audit, or transaction failure."""


class PublicationBlocked(PublicationError):
    """A valid evaluation is not eligible for publication."""


@dataclass(frozen=True)
class PublicationPlan:
    run_id: str
    release_id: str
    content_hash: str
    policy_hash: str
    policy_version: str
    snapshot: dict[str, pd.DataFrame]
    evaluation: Evaluation


@dataclass(frozen=True)
class PublicationResult:
    release_id: str
    run_id: str
    content_hash: str
    decision: Decision
    already_committed: bool = False


class Transaction(Protocol):
    def insert_publication(self, plan: PublicationPlan) -> None: ...
    def insert_manifest(self, plan: PublicationPlan) -> None: ...
    def insert_audit_success(self, plan: PublicationPlan) -> None: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...


class PublicationStore(Protocol):
    def get_manifest(self, release_id: str) -> Mapping[str, Any] | None: ...
    def write_audit_intent(self, event: Mapping[str, Any]) -> None: ...
    def write_audit_failure(self, event: Mapping[str, Any]) -> None: ...
    def begin(self) -> Transaction: ...


def _require_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PublicationError(f"Missing or invalid evidence field: {field}")
    return value


def _check_from_evidence(raw: Any) -> CheckResult:
    if not isinstance(raw, dict):
        raise PublicationError("Malformed check evidence")
    try:
        return CheckResult(
            name=_require_text(raw.get("name"), "checks.name"),
            status=Status(raw["status"]),
            kind=_require_text(raw.get("kind"), "checks.kind"),
            value=raw.get("value"),
            detail=str(raw.get("detail", "")),
        )
    except (KeyError, ValueError, TypeError) as exc:
        raise PublicationError(f"Malformed check evidence: {exc}") from exc


def _same_value(left: Any, right: Any) -> bool:
    return left == right


def _validate_evidence(
    evidence: Mapping[str, Any], *, run_id: str, content_hash: str,
    policy: dict[str, Any], policy_hash: str, snapshot: Mapping[str, pd.DataFrame],
) -> Evaluation:
    if not isinstance(evidence, Mapping):
        raise PublicationError("Evaluation evidence must be an object")
    if _require_text(evidence.get("run_id"), "run_id") != run_id:
        raise PublicationError("Evidence run_id does not match the publication request")
    if _require_text(evidence.get("candidate_content_hash"), "candidate_content_hash") != content_hash:
        raise PublicationError("Candidate content hash mismatch")
    if _require_text(evidence.get("policy_hash"), "policy_hash") != policy_hash:
        raise PublicationError("Policy hash mismatch")
    if _require_text(evidence.get("policy_version"), "policy_version") != str(policy["policy_version"]):
        raise PublicationError("Policy version mismatch")
    _require_text(evidence.get("source_fingerprint"), "source_fingerprint")
    _require_text(evidence.get("generator_version"), "generator_version")
    timestamp = _require_text(evidence.get("timestamp"), "timestamp")
    try:
        parsed_timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PublicationError("Invalid evaluation timestamp") from exc
    if parsed_timestamp.tzinfo is None or parsed_timestamp.utcoffset() is None:
        raise PublicationError("Evaluation timestamp must include a timezone")
    configuration = evidence.get("measurement_configuration")
    if not isinstance(configuration, Mapping) or not configuration:
        raise PublicationError("Missing measurement configuration")
    if configuration.get("dcr_enabled") is not False or configuration.get("privacy_claim") != "NO_PRIVACY_CERTIFICATION":
        raise PublicationError("Evidence contains an unsupported privacy claim")
    versions = evidence.get("runtime_versions")
    if not isinstance(versions, Mapping):
        raise PublicationError("Missing SDV/RDT runtime provenance")
    _require_text(versions.get("sdv"), "runtime_versions.sdv")
    _require_text(versions.get("rdt"), "runtime_versions.rdt")
    if not isinstance(evidence.get("generation_manifest"), Mapping):
        raise PublicationError("Missing generation/run provenance")
    if evidence.get("evaluation_run_id") != run_id:
        raise PublicationError("Evaluation run identifier is missing or inconsistent")
    reference_parameters = evidence.get("reference_parameters")
    if not isinstance(reference_parameters, Mapping):
        raise PublicationError("Missing reference generation parameters")
    if type(reference_parameters.get("customers")) is not int or type(reference_parameters.get("seed")) is not int:
        raise PublicationError("Invalid reference generation parameters")
    reference_hash = _require_text(evidence.get("reference_hash"), "reference_hash")
    if evidence.get("source_fingerprint") != reference_hash:
        raise PublicationError("Reference fingerprint and hash disagree")
    manifest = evidence["generation_manifest"]
    provenance = manifest.get("provenance")
    if evidence.get("lineage_status") != "VERIFIED" or not isinstance(provenance, Mapping):
        raise PublicationError("Generation lineage is legacy or unverified")
    if provenance.get("generation_run_id") != evidence.get("generation_run_id"):
        raise PublicationError("Generation run provenance mismatch")
    if provenance.get("reference_parameters") != dict(reference_parameters):
        raise PublicationError("Reference parameters do not match generation provenance")
    if provenance.get("reference_hash") != reference_hash:
        raise PublicationError("Reference hash does not match generation provenance")
    if provenance.get("candidate_content_hash") != content_hash:
        raise PublicationError("Generation and publication candidate hashes disagree")

    raw_checks = evidence.get("checks")
    if not isinstance(raw_checks, list):
        raise PublicationError("Missing checks evidence")
    checks = [_check_from_evidence(raw) for raw in raw_checks]
    hard_checks = run_hard_checks(dict(snapshot))
    evidence_hard = {check.name: check for check in checks if check.kind == "hard"}
    for actual in hard_checks:
        submitted = evidence_hard.get(actual.name)
        if submitted is None or submitted.status is not actual.status or not _same_value(submitted.value, actual.value):
            raise PublicationError(f"Source-independent hard check mismatch: {actual.name}")

    metrics = evidence.get("metrics")
    if not isinstance(metrics, Mapping):
        raise PublicationError("Missing metric evidence")
    for check in checks:
        if check.kind == "risk":
            metric = metrics.get(check.name)
            if not isinstance(metric, Mapping) or metric.get("status") != check.status.value or not _same_value(metric.get("value"), check.value):
                raise PublicationError(f"Metric evidence mismatch: {check.name}")

    try:
        evaluation = evaluate(
            checks, policy, policy_hash,
            source_fingerprint=str(evidence["source_fingerprint"]),
            generator_version=str(evidence["generator_version"]),
        )
    except Exception as exc:
        raise PublicationError(f"Policy evaluation failed: {type(exc).__name__}: {exc}") from exc
    try:
        recorded_decision = Decision(evidence["decision"])
    except (KeyError, ValueError) as exc:
        raise PublicationError("Missing or invalid recorded decision") from exc
    if evaluation.decision is not recorded_decision:
        raise PublicationError("Recomputed decision differs from recorded decision")
    if evidence.get("reasons") != evaluation.reasons:
        raise PublicationError("Recorded reasons differ from recomputed reasons")
    if evaluation.decision is not Decision.APPROVED:
        raise PublicationBlocked(f"Candidate decision is {evaluation.decision.value}")
    return evaluation


def prepare_publication(
    tables: Mapping[str, pd.DataFrame], evidence: Mapping[str, Any], policy_path: str | Path,
    *, run_id: str, release_id: str,
) -> PublicationPlan:
    if not isinstance(release_id, str) or not release_id.strip():
        raise PublicationError("release_id must be a non-empty string")
    try:
        snapshot = snapshot_tables(tables)
        generation_run_id = evidence.get("generation_run_id")
        content_hash = canonical_content_hash(snapshot, generation_run_id)
        policy, policy_hash = load_policy(policy_path)
        evaluation = _validate_evidence(
            evidence, run_id=run_id, content_hash=content_hash,
            policy=policy, policy_hash=policy_hash, snapshot=snapshot,
        )
    except PublicationError:
        raise
    except (CanonicalizationError, OSError, ValueError, TypeError, KeyError) as exc:
        raise PublicationError(f"Publication validation failed: {type(exc).__name__}: {exc}") from exc
    return PublicationPlan(
        run_id, release_id, content_hash, policy_hash, str(policy["policy_version"]), snapshot, evaluation
    )


class Publisher:
    """Publish one verified snapshot through one transactional adapter."""

    def __init__(self, store: PublicationStore, policy_path: str | Path):
        self.store = store
        self.policy_path = policy_path

    def _failure_audit(self, *, evidence: Mapping[str, Any], run_id: str,
                       release_id: str, error: BaseException,
                       event_type: str = "PUBLICATION_FAILED") -> str:
        event = {
            "event_id": f"failure-{release_id}-{run_id}",
            "event_type": event_type,
            "run_id": run_id,
            "release_id": release_id,
            "content_hash": evidence.get("candidate_content_hash", "UNAVAILABLE"),
            "policy_hash": evidence.get("policy_hash", "UNAVAILABLE"),
            "evaluation_decision": evidence.get("decision", "ERROR"),
            "publisher_decision": evidence.get("decision", "ERROR"),
            "error_reason": f"{type(error).__name__}: {error}",
        }
        try:
            self.store.write_audit_failure(event)
        except Exception as audit_error:
            return f"failure audit unsuccessful: {type(audit_error).__name__}: {audit_error}"
        return "failure audit persisted"

    def publish(self, tables: Mapping[str, pd.DataFrame], evidence: Mapping[str, Any], *, run_id: str, release_id: str) -> PublicationResult:
        try:
            plan = prepare_publication(tables, evidence, self.policy_path, run_id=run_id, release_id=release_id)
        except PublicationError as exc:
            audit_status = self._failure_audit(
                evidence=evidence, run_id=run_id, release_id=release_id,
                error=exc,
                event_type="PUBLICATION_REJECTED" if isinstance(exc, PublicationBlocked) else "PUBLICATION_FAILED",
            )
            if isinstance(exc, PublicationBlocked):
                raise PublicationBlocked(f"{exc}; {audit_status}") from exc
            raise PublicationError(f"{exc}; {audit_status}") from exc
        lock_context = self.store.writer_lock() if hasattr(self.store, "writer_lock") else nullcontext()
        with lock_context:
            existing = self.store.get_manifest(release_id)
            if existing is not None:
                same = all(existing.get(key) == value for key, value in {
                    "release_id": release_id, "run_id": run_id, "content_hash": plan.content_hash,
                    "policy_hash": plan.policy_hash, "status": "COMMITTED",
                }.items())
                if same:
                    return PublicationResult(release_id, run_id, plan.content_hash, Decision.APPROVED, True)
                raise PublicationError("release_id already exists with inconsistent evidence")
            try:
                self.store.write_audit_intent({
                    "event": "PUBLICATION_INTENT", "run_id": run_id, "release_id": release_id,
                    "content_hash": plan.content_hash, "policy_hash": plan.policy_hash,
                    "decision": plan.evaluation.decision.value,
                })
            except Exception as exc:
                raise PublicationError(f"Mandatory audit intent failed: {type(exc).__name__}: {exc}") from exc
            transaction = self.store.begin()
            try:
                transaction.insert_publication(plan)
                transaction.insert_manifest(plan)
                transaction.insert_audit_success(plan)
                transaction.commit()
            except Exception as exc:
                try:
                    transaction.rollback()
                except Exception:
                    pass
                audit_status = self._failure_audit(
                    evidence=evidence, run_id=run_id, release_id=release_id, error=exc,
                )
                raise PublicationError(
                    f"Publication transaction rolled back: {type(exc).__name__}: {exc}; {audit_status}"
                ) from exc
            return PublicationResult(
                release_id, run_id, plan.content_hash, Decision.APPROVED,
                bool(getattr(self.store, "last_already_committed", False)),
            )


class InMemoryPublicationStore:
    """Deterministic local transaction fake, not a BigQuery implementation."""

    def __init__(self, *, fail_audit_intent: bool = False, fail_audit_success: bool = False, fail_commit: bool = False):
        self.published_tables: dict[str, pd.DataFrame] = {}
        self.release_manifest: dict[str, dict[str, Any]] = {}
        self.audit_events: list[dict[str, Any]] = []
        self.fail_audit_intent = fail_audit_intent
        self.fail_audit_success = fail_audit_success
        self.fail_commit = fail_commit
        self._writer_lock = Lock()

    def writer_lock(self):
        """Local analogue of the BigQuery manifest-table single-writer lock."""
        return self._writer_lock

    def get_manifest(self, release_id: str) -> Mapping[str, Any] | None:
        return self.release_manifest.get(release_id)

    def write_audit_intent(self, event: Mapping[str, Any]) -> None:
        if self.fail_audit_intent:
            raise OSError("simulated audit persistence failure")
        self.audit_events.append(dict(event))

    def write_audit_failure(self, event: Mapping[str, Any]) -> None:
        self.audit_events.append(dict(event))

    def begin(self) -> "_InMemoryTransaction":
        return _InMemoryTransaction(self)

    def unreconciled_intents(self) -> list[dict[str, Any]]:
        """Return intents without an authoritative COMMITTED manifest."""
        committed = {
            release_id
            for release_id, manifest in self.release_manifest.items()
            if manifest.get("status") == "COMMITTED"
        }
        return [
            event for event in self.audit_events
            if event.get("event") == "PUBLICATION_INTENT"
            and event.get("release_id") not in committed
        ]


class _InMemoryTransaction:
    def __init__(self, store: InMemoryPublicationStore):
        self.store = store
        self.staged_tables: dict[str, pd.DataFrame] = {}
        self.staged_manifest: dict[str, dict[str, Any]] = {}
        self.staged_audit: dict[str, Any] | None = None
        self.closed = False

    def insert_publication(self, plan: PublicationPlan) -> None:
        for name, frame in plan.snapshot.items():
            staged = frame.copy(deep=True)
            staged.insert(len(staged.columns), "release_id", plan.release_id)
            self.staged_tables[name] = staged

    def insert_manifest(self, plan: PublicationPlan) -> None:
        self.staged_manifest[plan.release_id] = {
            "release_id": plan.release_id, "run_id": plan.run_id, "content_hash": plan.content_hash,
            "policy_hash": plan.policy_hash, "policy_version": plan.policy_version,
            "decision": plan.evaluation.decision.value, "status": "COMMITTED",
        }

    def insert_audit_success(self, plan: PublicationPlan) -> None:
        if self.store.fail_audit_success:
            raise OSError("simulated transactional audit failure")
        self.staged_audit = {
            "event": "PUBLICATION_COMMITTED", "run_id": plan.run_id,
            "release_id": plan.release_id, "content_hash": plan.content_hash,
            "policy_hash": plan.policy_hash,
        }

    def commit(self) -> None:
        if self.store.fail_commit:
            raise OSError("simulated transaction failure")
        if self.closed:
            raise OSError("transaction already closed")
        self.store.published_tables = {name: frame.copy(deep=True) for name, frame in self.staged_tables.items()}
        self.store.release_manifest.update(deepcopy(self.staged_manifest))
        if self.staged_audit is not None:
            self.store.audit_events.append(deepcopy(self.staged_audit))
        self.closed = True

    def rollback(self) -> None:
        self.staged_tables.clear()
        self.staged_manifest.clear()
        self.staged_audit = None
        self.closed = True


def publish_decision_guard(result: Evaluation) -> None:
    """Backward-compatible local guard for existing callers."""
    if result.decision is not Decision.APPROVED:
        raise PermissionError(f"Not publishable: {result.decision.value}")


def main() -> None:
    raise SystemExit("Cloud publication is not enabled; use Publisher with an explicit local test adapter.")
