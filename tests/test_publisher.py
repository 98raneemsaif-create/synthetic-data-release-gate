from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
import math
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import pytest

from gate.canonical import canonical_content_hash, canonical_reference_hash
from gate.checks.hard import run_hard_checks
from gate.checks.risk import measured_risk_checks
from gate.engine import evaluate
from gate.models import Decision
from gate.policy import load_policy
from gate.publisher import (
    InMemoryPublicationStore,
    PublicationBlocked,
    PublicationError,
    Publisher,
)
from gate.reference import make_reference


POLICY = "policies/synthetic-release-v1.yaml"


def _approved_candidate():
    reference = make_reference(30, seed=4)
    synthetic = {name: frame.copy(deep=True) for name, frame in reference.items()}
    customer_ids = {old: f"new-customer-{i}" for i, old in enumerate(reference["customers"]["customer_id"])}
    synthetic["customers"]["customer_id"] = synthetic["customers"]["customer_id"].map(customer_ids)
    synthetic["orders"]["customer_id"] = synthetic["orders"]["customer_id"].map(customer_ids)
    order_ids = {old: f"new-order-{i}" for i, old in enumerate(reference["orders"]["order_id"])}
    synthetic["orders"]["order_id"] = synthetic["orders"]["order_id"].map(order_ids)
    synthetic["order_items"]["order_id"] = synthetic["order_items"]["order_id"].map(order_ids)
    synthetic["order_items"]["item_id"] = synthetic["order_items"]["item_id"].map(
        lambda value: f"new-item-{value}"
    )
    synthetic["order_items"]["price"] += 0.01
    return reference, synthetic


def _evidence(reference, synthetic, run_id="run-approved"):
    policy, policy_hash = load_policy(POLICY)
    reference_hash = canonical_reference_hash(reference, customers=len(reference["customers"]), seed=4)
    checks = run_hard_checks(synthetic) + measured_risk_checks(reference=reference, synthetic=synthetic)
    evaluation = evaluate(
        checks,
        policy,
        policy_hash,
        source_fingerprint=reference_hash,
        generator_version="test/sdv-1.17.0",
    )
    return {
        "run_id": run_id,
        "candidate_content_hash": canonical_content_hash(synthetic, "generation-test"),
        "policy_hash": policy_hash,
        "policy_version": policy["policy_version"],
        "timestamp": datetime(2026, 11, 12, tzinfo=timezone.utc).isoformat(),
        "decision": evaluation.decision.value,
        "reasons": evaluation.reasons,
        "source_fingerprint": reference_hash,
        "evaluation_run_id": run_id,
        "generation_run_id": "generation-test",
        "lineage_status": "VERIFIED",
        "reference_parameters": {"customers": len(reference["customers"]), "seed": 4},
        "reference_hash": reference_hash,
        "generator_version": "test/sdv-1.17.0",
        "runtime_versions": {"sdv": "1.17.0", "rdt": "1.14.0"},
        "generation_manifest": {"backend": "test", "release_status": "UNVALIDATED_PRIVATE_CANDIDATE",
                                "provenance": {"generation_run_id": "generation-test",
                                               "reference_parameters": {"customers": len(reference["customers"]), "seed": 4},
                                               "reference_hash": reference_hash,
                                               "candidate_content_hash": canonical_content_hash(synthetic, "generation-test")}},
        "measurement_configuration": {"fidelity_metric": "measured fixture", "dcr_enabled": False,
                                       "privacy_claim": "NO_PRIVACY_CERTIFICATION"},
        "checks": [{**asdict(check), "status": check.status.value} for check in checks],
        "metrics": {
            check.name: {"status": check.status.value, "value": check.value, "detail": check.detail}
            for check in checks if check.kind == "risk"
        },
    }


def test_genuine_approved_snapshot_publishes_and_retry_is_idempotent():
    reference, synthetic = _approved_candidate()
    evidence = _evidence(reference, synthetic)
    store = InMemoryPublicationStore()
    publisher = Publisher(store, POLICY)

    first = publisher.publish(synthetic, evidence, run_id="run-approved", release_id="release-1")
    second = publisher.publish(synthetic, evidence, run_id="run-approved", release_id="release-1")

    assert first.decision is Decision.APPROVED
    assert not first.already_committed
    assert second.already_committed
    assert store.release_manifest["release-1"]["status"] == "COMMITTED"
    assert all(frame["release_id"].eq("release-1").all() for frame in store.published_tables.values())


def test_review_required_is_blocked_with_real_measurements():
    reference, synthetic = _approved_candidate()
    synthetic["order_items"]["price"] = 9999.0
    evidence = _evidence(reference, synthetic, "run-review")
    assert evidence["decision"] == "REVIEW_REQUIRED"
    with pytest.raises(PublicationBlocked):
        Publisher(InMemoryPublicationStore(), POLICY).publish(
            synthetic, evidence, run_id="run-review", release_id="release-review"
        )


def test_rejected_exact_match_is_blocked():
    reference = make_reference(20, seed=12)
    evidence = _evidence(reference, reference, "run-rejected")
    assert evidence["decision"] == "REJECTED"
    with pytest.raises(PublicationBlocked):
        Publisher(InMemoryPublicationStore(), POLICY).publish(
            reference, evidence, run_id="run-rejected", release_id="release-rejected"
        )


def test_error_missing_measurement_evidence_is_blocked():
    reference, synthetic = _approved_candidate()
    evidence = _evidence(reference, synthetic)
    evidence.pop("metrics")
    with pytest.raises(PublicationError):
        Publisher(InMemoryPublicationStore(), POLICY).publish(
            synthetic, evidence, run_id="run-approved", release_id="release-error"
        )


def test_content_modification_after_evaluation_is_rejected():
    reference, synthetic = _approved_candidate()
    evidence = _evidence(reference, synthetic)
    modified = {name: frame.copy(deep=True) for name, frame in synthetic.items()}
    modified["orders"].loc[0, "status"] = "tampered"
    with pytest.raises(PublicationError, match="hash mismatch"):
        Publisher(InMemoryPublicationStore(), POLICY).publish(
            modified, evidence, run_id="run-approved", release_id="release-tampered"
        )


def test_generation_candidate_hash_mismatch_is_rejected():
    reference, synthetic = _approved_candidate()
    evidence = _evidence(reference, synthetic)
    evidence["generation_manifest"]["provenance"]["candidate_content_hash"] = "0" * 64
    with pytest.raises(PublicationError, match="candidate hashes"):
        Publisher(InMemoryPublicationStore(), POLICY).publish(
            synthetic, evidence, run_id="run-approved", release_id="release-hash-mismatch"
        )


def test_policy_hash_mismatch_is_rejected():
    reference, synthetic = _approved_candidate()
    evidence = _evidence(reference, synthetic)
    evidence["policy_hash"] = "0" * 64
    with pytest.raises(PublicationError, match="Policy hash mismatch"):
        Publisher(InMemoryPublicationStore(), POLICY).publish(
            synthetic, evidence, run_id="run-approved", release_id="release-policy"
        )


def test_hard_check_failure_is_rejected():
    reference, synthetic = _approved_candidate()
    synthetic["orders"].loc[0, "customer_id"] = "missing-customer"
    evidence = _evidence(reference, synthetic, "run-hard-failure")
    assert evidence["decision"] == "ERROR"
    with pytest.raises(PublicationBlocked):
        Publisher(InMemoryPublicationStore(), POLICY).publish(
            synthetic, evidence, run_id="run-hard-failure", release_id="release-hard"
        )


def test_nonfinite_data_fails_before_publication():
    reference, synthetic = _approved_candidate()
    evidence = _evidence(reference, synthetic, "run-invalid")
    synthetic["order_items"].loc[0, "price"] = math.inf
    with pytest.raises(PublicationError):
        Publisher(InMemoryPublicationStore(), POLICY).publish(
            synthetic, evidence, run_id="run-invalid", release_id="release-invalid"
        )


def test_inconsistent_evidence_decision_is_rejected():
    reference, synthetic = _approved_candidate()
    evidence = _evidence(reference, synthetic)
    evidence["decision"] = "REJECTED"
    with pytest.raises(PublicationError, match="decision"):
        Publisher(InMemoryPublicationStore(), POLICY).publish(
            synthetic, evidence, run_id="run-approved", release_id="release-inconsistent"
        )


def test_duplicate_release_with_different_content_is_rejected():
    reference, synthetic = _approved_candidate()
    evidence = _evidence(reference, synthetic)
    store = InMemoryPublicationStore()
    publisher = Publisher(store, POLICY)
    publisher.publish(synthetic, evidence, run_id="run-approved", release_id="release-duplicate")
    changed = {name: frame.copy(deep=True) for name, frame in synthetic.items()}
    changed["order_items"].loc[0, "price"] += 0.02
    changed_evidence = _evidence(reference, changed, "run-changed")
    with pytest.raises(PublicationError, match="hash mismatch|release_id"):
        publisher.publish(changed, changed_evidence, run_id="run-changed", release_id="release-duplicate")


@pytest.mark.parametrize("store", [
    InMemoryPublicationStore(fail_commit=True),
    InMemoryPublicationStore(fail_audit_success=True),
])
def test_transaction_failure_rolls_back_without_partial_publication(store):
    reference, synthetic = _approved_candidate()
    evidence = _evidence(reference, synthetic)
    with pytest.raises(PublicationError, match="rolled back"):
        Publisher(store, POLICY).publish(synthetic, evidence, run_id="run-approved", release_id="release-fail")
    assert store.published_tables == {}
    assert store.release_manifest == {}
    assert len(store.unreconciled_intents()) == 1


def test_audit_intent_failure_fails_closed_without_publication():
    reference, synthetic = _approved_candidate()
    evidence = _evidence(reference, synthetic)
    store = InMemoryPublicationStore(fail_audit_intent=True)
    with pytest.raises(PublicationError, match="audit intent"):
        Publisher(store, POLICY).publish(synthetic, evidence, run_id="run-approved", release_id="release-audit")
    assert store.published_tables == {}
    assert store.release_manifest == {}


def test_concurrent_identical_release_has_one_commit_and_one_idempotent_retry():
    reference, synthetic = _approved_candidate()
    evidence = _evidence(reference, synthetic)
    publisher = Publisher(InMemoryPublicationStore(), POLICY)

    def publish_once():
        return publisher.publish(synthetic, evidence, run_id="run-approved", release_id="release-concurrent")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: publish_once(), range(2)))
    assert sum(not result.already_committed for result in results) == 1
    assert sum(result.already_committed for result in results) == 1


def test_near_copy_fixture_is_documented_as_not_a_privacy_demonstration():
    readme = open("README.md", encoding="utf-8").read()
    assert "NOT a privacy-safety demonstration" in readme
    assert "near-copies" in readme
    assert "DCR is disabled" in readme


@pytest.mark.parametrize("field, value", [
    ("reference_parameters", {"customers": 31, "seed": 4}),
    ("reference_parameters", {"customers": 30, "seed": 5}),
    ("reference_hash", "0" * 64),
    ("generation_manifest", {"backend": "test"}),
])
def test_inconsistent_or_missing_reference_lineage_fails_closed(field, value):
    reference, synthetic = _approved_candidate()
    evidence = _evidence(reference, synthetic)
    evidence[field] = value
    with pytest.raises(PublicationError, match="lineage|Reference|reference"):
        Publisher(InMemoryPublicationStore(), POLICY).publish(
            synthetic, evidence, run_id="run-approved", release_id="release-lineage"
        )
