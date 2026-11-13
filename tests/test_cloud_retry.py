import pytest

from gate.cloud_retry import (
    AmbiguousTransactionOutcome,
    BigQueryTransactionRetrier,
    PermanentTransactionError,
    ReleaseOutcome,
    RetryError,
)


class FakeBigQueryError(Exception):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


def _run(operation, *, lookup=None, sleeps=None, max_attempts=3):
    sleeper = sleeps.append if sleeps is not None else (lambda _: None)
    return BigQueryTransactionRetrier(
        max_attempts=max_attempts, sleep=sleeper
    ).execute(
        operation, release_id='release-1', expected_content_hash='content',
        expected_policy_hash='policy',
        lookup_outcome=lookup or (lambda _: ReleaseOutcome('ABSENT')),
    )


def test_transient_conflict_retries_after_sixty_seconds():
    calls, sleeps = [], []

    def operation(attempt):
        calls.append(attempt)
        if attempt == 1:
            raise FakeBigQueryError('Transaction is aborted due to concurrent update')
        return 'committed'

    result = _run(operation, sleeps=sleeps)
    assert result.value == 'committed'
    assert calls == [1, 2]
    assert sleeps == [60.0]


def test_retry_limit_is_bounded():
    sleeps = []

    def operation(_):
        raise FakeBigQueryError('concurrent update')

    with pytest.raises(RetryError, match='limit exhausted'):
        _run(operation, sleeps=sleeps, max_attempts=3)
    assert sleeps == [60.0, 120.0]


def test_permanent_error_is_never_retried():
    calls, sleeps = [], []

    def operation(attempt):
        calls.append(attempt)
        raise FakeBigQueryError('permission denied', code=403)

    with pytest.raises(PermanentTransactionError):
        _run(operation, sleeps=sleeps)
    assert calls == [1]
    assert sleeps == []


def test_ambiguous_timeout_matching_commit_is_idempotent():
    calls, sleeps, lookups = [], [], []

    def operation(attempt):
        calls.append(attempt)
        raise TimeoutError('deadline exceeded')

    def lookup(release_id):
        lookups.append(release_id)
        return ReleaseOutcome('COMMITTED', 'content', 'policy')

    result = _run(operation, lookup=lookup, sleeps=sleeps)
    assert result.already_committed
    assert calls == [1]
    assert lookups == ['release-1']
    assert sleeps == []


def test_ambiguous_unknown_outcome_fails_closed():
    with pytest.raises(AmbiguousTransactionOutcome):
        _run(lambda _: (_ for _ in ()).throw(TimeoutError('timeout')),
             lookup=lambda _: ReleaseOutcome('UNKNOWN'))


def test_ambiguous_conflicting_commit_is_rejected():
    with pytest.raises(PermanentTransactionError, match='conflicting'):
        _run(lambda _: (_ for _ in ()).throw(TimeoutError('timeout')),
             lookup=lambda _: ReleaseOutcome('COMMITTED', 'other', 'policy'))
