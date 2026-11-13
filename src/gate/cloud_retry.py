"""Bounded, fail-closed retry policy for BigQuery publication transactions.

The cloud adapter supplies one operation built from an already verified
snapshot and a release_manifest outcome lookup. Retries never reload candidate
tables. This module intentionally does not require the BigQuery client, so its
failure and timing behavior is deterministic in local tests.
"""
from __future__ import annotations

from dataclasses import dataclass
from time import sleep as real_sleep
from typing import Any, Callable


class RetryError(RuntimeError):
    """Base class for transaction retry failures."""


class PermanentTransactionError(RetryError):
    """A non-retryable authorization, validation, or schema failure."""


class AmbiguousTransactionOutcome(RetryError):
    """The original transaction outcome cannot be established safely."""


@dataclass(frozen=True)
class ReleaseOutcome:
    status: str  # COMMITTED, ABSENT, CONFLICT, or UNKNOWN
    content_hash: str | None = None
    policy_hash: str | None = None
    run_id: str | None = None
    policy_version: str | None = None


@dataclass(frozen=True)
class RetryResult:
    value: Any = None
    already_committed: bool = False


def _error_parts(error: BaseException) -> tuple[int | None, str]:
    code = getattr(error, "code", None)
    if callable(code):
        code = code()
    if not isinstance(code, int):
        code = None
    return code, str(error).lower()


def is_transient_transaction_error(error: BaseException) -> bool:
    """Recognize transient service/concurrency failures only."""
    code, message = _error_parts(error)
    conflict = any(token in message for token in (
        "concurrent update", "transaction is aborted", "transaction conflict",
    ))
    return conflict or code in {429, 500, 502, 503, 504}


def is_ambiguous_transaction_error(error: BaseException) -> bool:
    code, message = _error_parts(error)
    return isinstance(error, TimeoutError) or code in {408, 504} or any(
        token in message for token in ("deadline exceeded", "timed out", "timeout")
    )


class BigQueryTransactionRetrier:
    """Execute one immutable-snapshot transaction with bounded retries."""

    def __init__(self, *, max_attempts: int = 3, initial_delay: float = 60.0,
                 multiplier: float = 2.0, max_delay: float = 300.0,
                 sleep: Callable[[float], None] | None = None):
        if type(max_attempts) is not int or max_attempts < 1:
            raise ValueError("max_attempts must be a positive integer")
        if initial_delay < 60 or multiplier < 1 or max_delay < initial_delay:
            raise ValueError("retry delays must honor the 60-second minimum")
        self.max_attempts = max_attempts
        self.initial_delay = float(initial_delay)
        self.multiplier = float(multiplier)
        self.max_delay = float(max_delay)
        self.sleep = sleep or real_sleep

    def execute(
        self, operation: Callable[[int], Any], *, release_id: str,
        expected_content_hash: str, expected_policy_hash: str,
        lookup_outcome: Callable[[str], ReleaseOutcome],
    ) -> RetryResult:
        delay = self.initial_delay
        for attempt in range(1, self.max_attempts + 1):
            try:
                return RetryResult(operation(attempt))
            except BaseException as error:
                if is_ambiguous_transaction_error(error):
                    outcome = lookup_outcome(release_id)
                    if outcome.status == "COMMITTED":
                        if (outcome.content_hash != expected_content_hash or
                                outcome.policy_hash != expected_policy_hash):
                            raise PermanentTransactionError(
                                "Committed release has conflicting hashes"
                            ) from error
                        return RetryResult(already_committed=True)
                    if outcome.status == "UNKNOWN":
                        raise AmbiguousTransactionOutcome(
                            "Transaction outcome cannot be established"
                        ) from error
                    if outcome.status == "CONFLICT":
                        raise PermanentTransactionError(
                            "Release outcome conflicts with requested publication"
                        ) from error
                elif not is_transient_transaction_error(error):
                    raise PermanentTransactionError(
                        f"Non-retryable BigQuery transaction failure: {error}"
                    ) from error
                else:
                    outcome = lookup_outcome(release_id)
                    if outcome.status == "COMMITTED":
                        if (outcome.content_hash != expected_content_hash or
                                outcome.policy_hash != expected_policy_hash):
                            raise PermanentTransactionError(
                                "Committed release has conflicting hashes"
                            ) from error
                        return RetryResult(already_committed=True)
                    if outcome.status == "CONFLICT":
                        raise PermanentTransactionError(
                            "Release outcome conflicts with requested publication"
                        ) from error
                if attempt >= self.max_attempts:
                    raise RetryError(
                        f"Transaction retry limit exhausted after {attempt} attempts"
                    ) from error
                self.sleep(min(delay, self.max_delay))
                delay = min(delay * self.multiplier, self.max_delay)
        raise RetryError("Unreachable retry state")
