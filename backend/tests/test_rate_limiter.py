"""Unit tests for the fixed-window rate limiter (app/services/rate_limiter.py),
independent of FastAPI/HTTP — see test_explanation_api.py for the
HTTP-level 429/Retry-After test wired through the real /explain route.
"""

from __future__ import annotations

from app.services.rate_limiter import FixedWindowRateLimiter


class _FakeClock:
    def __init__(self, start: float = 0.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_allows_up_to_the_limit_within_a_window():
    clock = _FakeClock()
    limiter = FixedWindowRateLimiter(limit=3, window_seconds=60.0, clock=clock)

    for _ in range(3):
        allowed, retry_after = limiter.check("client-a")
        assert allowed is True
        assert retry_after == 0.0


def test_blocks_once_the_limit_is_exceeded_within_the_same_window():
    clock = _FakeClock()
    limiter = FixedWindowRateLimiter(limit=2, window_seconds=60.0, clock=clock)

    assert limiter.check("client-a")[0] is True
    assert limiter.check("client-a")[0] is True
    allowed, retry_after = limiter.check("client-a")
    assert allowed is False
    assert retry_after > 0.0


def test_resets_after_the_window_elapses():
    clock = _FakeClock()
    limiter = FixedWindowRateLimiter(limit=1, window_seconds=60.0, clock=clock)

    assert limiter.check("client-a")[0] is True
    assert limiter.check("client-a")[0] is False

    clock.advance(60.1)
    allowed, retry_after = limiter.check("client-a")
    assert allowed is True
    assert retry_after == 0.0


def test_tracks_each_key_independently():
    clock = _FakeClock()
    limiter = FixedWindowRateLimiter(limit=1, window_seconds=60.0, clock=clock)

    assert limiter.check("client-a")[0] is True
    assert limiter.check("client-a")[0] is False
    # A different key (a different client IP) has its own, unaffected budget.
    assert limiter.check("client-b")[0] is True


def test_a_non_positive_limit_disables_the_limiter():
    limiter = FixedWindowRateLimiter(limit=0)
    assert limiter.enabled is False
    for _ in range(1000):
        allowed, retry_after = limiter.check("client-a")
        assert allowed is True
        assert retry_after == 0.0
