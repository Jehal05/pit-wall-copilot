"""Fixed-window rate limiter — fixes the other half of the previously-
documented Gate 3 limitation ("No caching or rate-limiting on /explain").

This backs the `GET /v1/races/{race_id}/alerts/{alert_id}/explain`
endpoint specifically, since that is the one route that can trigger a
real, paid Bedrock call per request (see explanation_engine.py's cache,
which handles the *repeated-request-for-the-same-alert* half of this;
this handles the *many-different-requests-in-a-burst* half).

Design, and why it's an honestly-scoped choice for this environment:
- In-process, single counter dict — not Redis/DynamoDB-backed. This
  matches every other piece of Gate 0-3 state (race state, watches,
  alerts) that is explicitly documented as in-memory-only pending a
  later gate's real persistence layer; a single hackathon-demo backend
  process is exactly the deployment shape this is scoped for.
- Fixed window, not sliding/token-bucket. A fixed window can allow up to
  2x the nominal rate across a window boundary, which is a known,
  accepted imprecision for this scale — the goal is "stop an accidental
  or abusive tight loop from generating an unbounded number of Bedrock
  calls," not precise traffic shaping.
- Keyed by client IP (`request.client.host`), since /explain has no
  per-user auth (spec 52 only requires demo-access-token on *mutating*
  endpoints; this one is a read). A production deployment with real
  auth would key by account/API key instead.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable


@dataclass
class _Window:
    window_start: float
    count: int


class FixedWindowRateLimiter:
    """Allows up to `limit` calls per `window_seconds` for a given key.

    `limit <= 0` disables the limiter entirely (every call is allowed) —
    this is the default-off escape hatch used by the test suite and by
    anyone who wants to turn rate-limiting back off via configuration
    without touching code.
    """

    def __init__(self, *, limit: int, window_seconds: float = 60.0, clock: Callable[[], float] = time.monotonic):
        self._limit = limit
        self._window_seconds = window_seconds
        self._clock = clock
        self._windows: dict[str, _Window] = {}

    @property
    def enabled(self) -> bool:
        return self._limit > 0

    def check(self, key: str) -> tuple[bool, float]:
        """Records one attempt for `key` and returns `(allowed,
        retry_after_seconds)`. `retry_after_seconds` is `0.0` when
        `allowed` is True, otherwise how long until the window resets."""
        if not self.enabled:
            return True, 0.0

        now = self._clock()
        window = self._windows.get(key)

        if window is None or (now - window.window_start) >= self._window_seconds:
            self._windows[key] = _Window(window_start=now, count=1)
            return True, 0.0

        if window.count < self._limit:
            window.count += 1
            return True, 0.0

        retry_after = self._window_seconds - (now - window.window_start)
        return False, max(retry_after, 0.0)
