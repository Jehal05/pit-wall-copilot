"""Real device-pairing exchange for the Fire TV client (spec v1.1 pass 3,
correction 5; made durable in pass 4, mandatory correction 1).

Replaces the previous "pairing" flow (pass 2), which had a person type the
FULL backend mutation secret (`DemoAccessTokenSecret`/`DEMO_ACCESS_TOKEN`)
directly into the Fire TV UI. That is not a pairing exchange — it is
manual entry of the master credential, and it means every paired Fire TV
holds a copy of the one secret that can also start/stop/reset the demo
from anywhere and can never be revoked for a single device without
rotating it for every device at once.

This module implements the smallest REAL pairing exchange that closes
that gap:

  1. An authorized administrator (anyone who already holds the master
     `DEMO_ACCESS_TOKEN`) requests a short-lived, one-time PAIRING CODE —
     a 6-digit, D-pad-friendly number, not the master secret itself.
  2. Only a SHA-256 hash of the code is ever stored — the plaintext code
     exists only in the HTTP response returned to whoever generated it,
     and in the memory of the person reading it aloud/typing it into the
     Fire TV, never at rest here.
  3. The code expires after `PAIRING_CODE_TTL_SECONDS` (5 minutes),
     whichever comes first: it is also single-use (consumed atomically —
     see `PairingCodeRepository.try_consume_code`) and is rejected on any
     repeat attempt.
  4. A successful exchange mints a DEVICE-SPECIFIC token — independent of
     the master secret and of every other device's token — again stored
     here only as a hash, never in plaintext. It carries its own
     expiration (`DEVICE_TOKEN_TTL_SECONDS`) and can be revoked
     independently of any other device (`revoke_device_token`/
     `revoke_device_by_id`) without touching the master secret or any
     other device's pairing.

*** Pass 4, mandatory correction 1: durable storage ***
This service used to own two process-local `dict`s directly. It now
delegates ALL storage to a `PairingCodeRepository`/`DeviceTokenRepository`
pair (`app/repositories/pairing_base.py`) that is injected at construction
time, selected by `app/repositories/factory.py:build_pairing_repositories`
the same way `STATE_REPOSITORY` already selects race-state/watch storage.
This class itself is now a thin, storage-agnostic orchestration layer:
generating codes/tokens, hashing them, and applying the TTL/business
rules (5-minute code expiry, 30-day device-token expiry) on top of
whichever repository is configured — memory (single-process, local dev)
or DynamoDB (durable, shared across every ECS task, survives a restart).
See `pairing_dynamodb.py`'s module docstring for exactly how atomic
single-use code consumption is guaranteed across concurrently-running
backend instances with no shared process memory.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from app.metrics import emit_pairing_outcome
from app.repositories.pairing_base import (
    DeviceTokenRepository,
    PairingCodeRepository,
    PairingExchangeCoordinator,
    PairingExchangeOutcome,
)

PAIRING_CODE_TTL_SECONDS = 300  # 5 minutes, per spec v1.1 pass 3 correction 5
PAIRING_CODE_LENGTH = 6
DEVICE_TOKEN_TTL_SECONDS = 30 * 24 * 3600  # 30 days
DEVICE_TOKEN_NBYTES = 32
# AWS hackathon-final pass, P1-11: a 6-digit code is drawn from only
# 1,000,000 possible values, so a collision with another still-active
# code is a real (if individually unlikely) event at any meaningful
# concurrency -- see PairingCodeRepository.create_code's docstring. This
# bounds how many times generate_pairing_code will draw a fresh,
# independently-random code and retry before giving up; each attempt is
# an independent ~1-in-a-million-ish draw against the currently
# outstanding codes, so this many attempts makes an exhausted retry
# budget astronomically unlikely to ever actually occur, while still
# keeping request latency bounded in the pathological case.
PAIRING_CODE_GENERATION_MAX_ATTEMPTS = 5
# AWS hackathon-final pass, phase 3: analogous bound for
# `exchange_pairing_code`'s atomic-exchange retries. device_id is 16 hex
# chars (8 random bytes) and the device token is 32 random bytes
# urlsafe-b64-encoded, so a collision with an existing, still-valid
# device_id or token_hash is astronomically unlikely — this bound exists
# purely to keep one pathological request's latency finite, exactly like
# PAIRING_CODE_GENERATION_MAX_ATTEMPTS above.
DEVICE_EXCHANGE_MAX_ATTEMPTS = 5


def _hash(value: str) -> str:
    """SHA-256 hex digest — used for BOTH pairing codes and device tokens
    so neither is ever stored in plaintext. Not a password hash (no
    per-value salt/stretching): a pairing code is single-use and expires
    in 5 minutes, and a device token is a 32-byte cryptographically random
    value (`secrets.token_urlsafe`) — both have enough entropy or
    short-enough lifetime that a fast hash is an appropriate, honest
    trade-off here (unlike a human-chosen password, which would need
    something like bcrypt/argon2 to resist offline guessing)."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass
class PairingCodeIssued:
    code: str
    expires_at: float


@dataclass
class DevicePaired:
    device_id: str
    device_token: str
    expires_at: float


class PairingCodeGenerationExhaustedError(Exception):
    """Raised by `generate_pairing_code` when every one of
    `PAIRING_CODE_GENERATION_MAX_ATTEMPTS` freshly, independently-drawn
    random codes collided with another still-active code. This should be
    vanishingly rare in practice (see that constant's own comment) --
    if it is ever actually observed, it is a strong signal that either
    far more pairing codes are concurrently active than this deployment
    was sized for, or PAIRING_CODE_LENGTH itself needs to grow."""


class PairingCodeRateLimitedError(Exception):
    """Raised when too many pairing attempts have come from one source in
    a short window — see `DevicePairingService.record_attempt` /
    `PairingAttemptRateLimiter`. `app/repositories/pairing_dynamodb.py`'s
    `DynamoDBPairingRateLimiterExceeded` is the distributed-mode
    equivalent; `routes.py` catches both under one 429 response."""


class PairingAttemptRateLimiter:
    """A tiny fixed-window limiter dedicated to pairing-code EXCHANGE
    attempts (as opposed to `app/services/rate_limiter.py`'s
    `FixedWindowRateLimiter`, which is reused as-is for this — kept as a
    thin, explicitly-named wrapper here so device_pairing.py has no
    import-time dependency on the unrelated `/explain` rate limiter's
    call site wiring). Keyed by caller-supplied key (the client's IP, at
    the route layer) — deliberately coarse, matching the honest scoping
    note in `rate_limiter.py`'s own module docstring.

    IMPORTANT: this is single-process only. It is the correct choice for
    `STATE_REPOSITORY=memory` (local development — a single process by
    construction) but NOT for a multi-task AWS deployment, where
    `app/repositories/pairing_dynamodb.py:DynamoDBPairingRateLimiter` is
    selected instead by `factory.py:build_pairing_rate_limiter` — see that
    function's docstring."""

    def __init__(self, max_attempts: int, window_seconds: float, clock: Callable[[], float] = time.time) -> None:
        self._max_attempts = max_attempts
        self._window_seconds = window_seconds
        self._clock = clock
        self._attempts: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def check_and_record(self, key: str) -> None:
        now = self._clock()
        with self._lock:
            timestamps = [t for t in self._attempts.get(key, []) if now - t < self._window_seconds]
            if len(timestamps) >= self._max_attempts:
                self._attempts[key] = timestamps
                raise PairingCodeRateLimitedError(
                    f"Too many pairing attempts from this source; try again in "
                    f"{self._window_seconds:.0f}s."
                )
            timestamps.append(now)
            self._attempts[key] = timestamps


class DevicePairingService:
    def __init__(
        self,
        pairing_code_repository: PairingCodeRepository,
        device_token_repository: DeviceTokenRepository,
        exchange_coordinator: PairingExchangeCoordinator,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._clock = clock
        self._pairing_codes = pairing_code_repository
        self._device_tokens = device_token_repository
        # AWS hackathon-final pass, phase 3: the atomic
        # consume-code+create-token+create-index operation. See
        # `exchange_pairing_code` and `PairingExchangeCoordinator`'s own
        # docstring (pairing_base.py) for what this replaces and why.
        self._exchange_coordinator = exchange_coordinator

    def generate_pairing_code(self) -> PairingCodeIssued:
        """Called only by an already-authorized caller (an administrator
        holding the master demo-access token — see routes.py's
        `dependencies=[Depends(require_demo_access_token)]` on the
        `/v1/auth/pairing-codes` route). Never called by the Fire TV
        client itself."""
        # AWS hackathon-final pass, P1-11: collision-safe generation --
        # each attempt draws a FRESH, independently cryptographically
        # random code (never a deterministic perturbation of the
        # previous attempt, which could narrow the effective search
        # space) and only stops retrying once the repository confirms it
        # was actually stored (i.e. did not collide with another
        # still-active code) -- see PairingCodeRepository.create_code's
        # docstring and PAIRING_CODE_GENERATION_MAX_ATTEMPTS's comment.
        now = self._clock()
        expires_at = now + PAIRING_CODE_TTL_SECONDS
        for _attempt in range(PAIRING_CODE_GENERATION_MAX_ATTEMPTS):
            code = f"{secrets.randbelow(10 ** PAIRING_CODE_LENGTH):0{PAIRING_CODE_LENGTH}d}"
            if self._pairing_codes.create_code(code_hash=_hash(code), created_at=now, expires_at=expires_at):
                return PairingCodeIssued(code=code, expires_at=expires_at)
        raise PairingCodeGenerationExhaustedError(
            f"Could not generate a unique pairing code after {PAIRING_CODE_GENERATION_MAX_ATTEMPTS} attempts."
        )

    def exchange_pairing_code(self, code: str, device_name: str) -> Optional[DevicePaired]:
        """Consumes a pairing code and mints a device-specific token, as
        ONE atomic operation (AWS hackathon-final pass, phase 3 — see
        `PairingExchangeCoordinator`'s docstring in pairing_base.py for
        exactly what this fixes relative to the pre-phase-3 two-step
        consume-then-create flow: a failure between those two steps used
        to permanently burn the code with no token ever produced).

        Returns None for ANY invalid input (unknown code, expired code,
        already-used code, or a code that a concurrent request consumed a
        moment earlier) — deliberately a single outcome rather than
        distinguishing "wrong" from "expired" from "reused" in the return
        value, so the route layer returns one uniform 401 without leaking
        which specific reason applies (the same reasoning
        `require_demo_access_token` already applies to a bad master
        token).

        A `device_id`/`token_hash` collision (see `DEVICE_EXCHANGE_MAX_
        ATTEMPTS`'s own comment for why this is expected to be
        astronomically rare, not a sign of a bug) is retried with FRESH,
        independently random values — the coordinator's atomicity
        guarantee means a collision on one attempt never consumes the
        code, so every retry still sees the exact same still-valid code
        it started with."""
        now = self._clock()
        code_hash = _hash(code)

        for _attempt in range(DEVICE_EXCHANGE_MAX_ATTEMPTS):
            device_id = secrets.token_hex(8)
            device_token = secrets.token_urlsafe(DEVICE_TOKEN_NBYTES)
            expires_at = now + DEVICE_TOKEN_TTL_SECONDS
            outcome = self._exchange_coordinator.atomic_exchange(
                code_hash=code_hash,
                now=now,
                device_id=device_id,
                token_hash=_hash(device_token),
                device_name=device_name,
                token_expires_at=expires_at,
            )
            if outcome is PairingExchangeOutcome.SUCCESS:
                emit_pairing_outcome(outcome="success")
                return DevicePaired(device_id=device_id, device_token=device_token, expires_at=expires_at)
            if outcome is PairingExchangeOutcome.CODE_REJECTED:
                emit_pairing_outcome(outcome="code_rejected")
                return None  # unknown / used / expired / lost-the-race — never worth retrying
            # DEVICE_ID_COLLISION or TOKEN_HASH_COLLISION: the code was
            # NOT consumed (see PairingExchangeCoordinator's contract) —
            # safe to retry the whole exchange with fresh random values.
            emit_pairing_outcome(outcome="collision_retry")

        emit_pairing_outcome(outcome="exchange_exhausted")
        raise PairingCodeGenerationExhaustedError(
            f"Could not complete a pairing exchange after {DEVICE_EXCHANGE_MAX_ATTEMPTS} attempts "
            "due to repeated device_id/token_hash collisions."
        )

    def verify_device_token(self, token: str) -> bool:
        """True iff `token` is a currently valid (not expired, not
        revoked) device token. Used both by `require_demo_access_token`
        (deps.py) to authorize mutating requests, and by `/v1/auth/verify`
        directly. Rejects an expired-but-not-yet-TTL-deleted DynamoDB item
        just as reliably as an already-physically-deleted one — see
        `pairing_dynamodb.py`'s module docstring."""
        now = self._clock()
        record = self._device_tokens.get_token(_hash(token))
        if record is None:
            return False
        if record.revoked or record.expires_at < now:
            return False
        return True

    def revoke_device_token(self, token: str) -> bool:
        """Self-revoke: the caller presents ITS OWN device token (the
        Fire TV's own "unpair" action never needs or sees the master
        secret to do this). Returns True iff a real, not-already-revoked
        token was found and revoked."""
        return self._device_tokens.revoke_token(_hash(token))

    def revoke_device_by_id(self, device_id: str) -> bool:
        """Admin revoke: revokes whichever device currently holds
        `device_id`, without needing that device's own token — used when
        an administrator (holding the master token) needs to revoke a
        lost/compromised Fire TV remotely. Independent of every other
        device's token, per spec v1.1 pass 3 correction 5's "independent
        revocation" requirement, and true across restarts/instances in
        DynamoDB mode (pass 4, correction 1)."""
        return self._device_tokens.revoke_by_device_id(device_id)

    def device_id_for_token(self, token: str) -> Optional[str]:
        record = self._device_tokens.get_token(_hash(token))
        if record is None:
            return None
        return record.device_id
