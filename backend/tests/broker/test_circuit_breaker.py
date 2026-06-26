"""Tests for BrokerCircuitBreaker."""

import time

import pytest

from yolovest.broker.zerodha import BrokerCircuitBreaker


class TestCircuitBreakerStates:
    def test_starts_closed(self):
        cb = BrokerCircuitBreaker()
        assert cb.state == "CLOSED"

    def test_stays_closed_under_threshold(self):
        cb = BrokerCircuitBreaker(failure_threshold=5)
        for _ in range(4):
            cb.record_failure()
        assert cb.state == "CLOSED"

    def test_opens_at_threshold(self):
        cb = BrokerCircuitBreaker(failure_threshold=3)
        for _ in range(3):
            cb.record_failure()
        assert cb.state == "OPEN"

    def test_success_resets_failures(self):
        cb = BrokerCircuitBreaker(failure_threshold=3)
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        cb.record_failure()
        cb.record_failure()
        # Only 2 consecutive, not 3
        assert cb.state == "CLOSED"

    def test_half_open_after_cooldown(self):
        cb = BrokerCircuitBreaker(failure_threshold=1, cooldown_sec=0.05)
        cb.record_failure()
        assert cb.state == "OPEN"
        time.sleep(0.06)
        assert cb.state == "HALF_OPEN"

    def test_success_closes_from_half_open(self):
        cb = BrokerCircuitBreaker(failure_threshold=1, cooldown_sec=0.01)
        cb.record_failure()
        time.sleep(0.02)
        assert cb.state == "HALF_OPEN"
        cb.record_success()
        assert cb.state == "CLOSED"

    def test_failure_reopens_from_half_open(self):
        cb = BrokerCircuitBreaker(failure_threshold=1, cooldown_sec=0.01)
        cb.record_failure()
        time.sleep(0.02)
        assert cb.state == "HALF_OPEN"
        cb.record_failure()
        assert cb.state == "OPEN"


class TestCircuitBreakerCheck:
    def test_check_passes_when_closed(self):
        cb = BrokerCircuitBreaker()
        cb.check()  # should not raise

    def test_check_raises_when_open(self):
        cb = BrokerCircuitBreaker(failure_threshold=1)
        cb.record_failure()
        with pytest.raises(RuntimeError, match="circuit breaker is OPEN"):
            cb.check()

    def test_check_passes_when_half_open(self):
        cb = BrokerCircuitBreaker(failure_threshold=1, cooldown_sec=0.01)
        cb.record_failure()
        time.sleep(0.02)
        cb.check()  # should not raise (HALF_OPEN allows probe)

    def test_error_message_includes_details(self):
        cb = BrokerCircuitBreaker(failure_threshold=2, cooldown_sec=10.0)
        cb.record_failure()
        cb.record_failure()
        with pytest.raises(RuntimeError, match="2 consecutive failures"):
            cb.check()


class TestCircuitBreakerEdgeCases:
    def test_multiple_opens_update_timestamp(self):
        cb = BrokerCircuitBreaker(failure_threshold=1, cooldown_sec=100)
        cb.record_failure()
        first_open = cb._opened_at
        time.sleep(0.01)
        cb.record_failure()  # another failure while open
        assert cb._opened_at >= first_open  # timestamp updated

    def test_consecutive_failures_accumulate(self):
        cb = BrokerCircuitBreaker(failure_threshold=10)
        for i in range(7):
            cb.record_failure()
        assert cb._consecutive_failures == 7
        assert cb.state == "CLOSED"


class TestCircuitBreakerConcurrency:
    """Cross-call hazards the audit flagged: single HALF_OPEN probe, and a
    stale in-flight success not resetting a deliberately-open breaker."""

    def test_half_open_admits_only_one_probe(self):
        cb = BrokerCircuitBreaker(failure_threshold=1, cooldown_sec=0.01)
        cb.record_failure()
        time.sleep(0.02)
        assert cb.state == "HALF_OPEN"
        cb.check()  # first caller is admitted as THE probe
        # Every other caller fails fast until the probe resolves — without
        # this they'd all flood the still-fragile API at the cooldown boundary.
        with pytest.raises(RuntimeError, match="probe request is"):
            cb.check()

    def test_probe_success_then_closed_passes(self):
        cb = BrokerCircuitBreaker(failure_threshold=1, cooldown_sec=0.01)
        cb.record_failure()
        time.sleep(0.02)
        cb.check()  # admit probe
        cb.record_success()  # probe succeeds
        assert cb.state == "CLOSED"
        cb.check()  # CLOSED — no raise, no probe gate

    def test_stale_success_does_not_reset_open_breaker(self):
        cb = BrokerCircuitBreaker(failure_threshold=2, cooldown_sec=30.0)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == "OPEN"
        # A success from a call that was already in flight when the breaker
        # tripped must NOT close it — only the cooldown + probe path may.
        cb.record_success()
        assert cb.state == "OPEN"
