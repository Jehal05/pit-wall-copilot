"""In-memory pairing/device-token repositories — the original (pass 3)
single-process behavior, now behind the `PairingCodeRepository`/
`DeviceTokenRepository` interfaces (pairing_base.py) so it is one
selectable implementation rather than the only one. This remains the
default when `STATE_REPOSITORY=memory` (local development, and most of
the automated test suite) — nothing about local dev changes in this pass.
"""

from __future__ import annotations

import threading
from typing import Optional

from app.repositories.pairing_base import (
    DeviceTokenRepository,
    PairingCodeRepository,
    PairingExchangeCoordinator,
    PairingExchangeOutcome,
    StoredDeviceToken,
)


class InMemoryPairingCodeRepository(PairingCodeRepository):
    def __init__(self) -> None:
        self._codes: dict[str, dict] = {}
        self._lock = threading.Lock()

    def create_code(self, code_hash: str, created_at: float, expires_at: float) -> bool:
        # AWS hackathon-final pass, P1-11: collision-safe pairing-code
        # storage -- refuse to clobber a still-active (unused,
        # unexpired) code that happens to share this 6-digit value's
        # hash; an expired or already-used code is safe to recycle. See
        # `PairingCodeRepository.create_code`'s docstring for the full
        # rationale (this mirrors pairing_dynamodb.py's conditional-put
        # behavior exactly, just via the in-process lock instead of a
        # DynamoDB ConditionExpression).
        with self._lock:
            existing = self._codes.get(code_hash)
            if existing is not None and not existing["used"] and existing["expires_at"] >= created_at:
                return False
            self._codes[code_hash] = {"created_at": created_at, "expires_at": expires_at, "used": False}
            return True

    def try_consume_code(self, code_hash: str, now: float) -> bool:
        with self._lock:
            record = self._codes.get(code_hash)
            if record is None:
                return False
            if record["used"] or record["expires_at"] < now:
                return False
            record["used"] = True
            return True


class InMemoryDeviceTokenRepository(DeviceTokenRepository):
    def __init__(self) -> None:
        self._tokens_by_hash: dict[str, StoredDeviceToken] = {}
        # AWS hackathon-final pass, phase 3: a device_id -> token_hash
        # index, mirroring pairing_dynamodb.py's DEVICEIDX# item — used by
        # InMemoryPairingExchangeCoordinator to detect a device_id
        # collision without a full scan, matching how the DynamoDB
        # implementation detects the same collision via a conditional Put
        # on its own index item.
        self._device_index: dict[str, str] = {}
        self._lock = threading.Lock()

    def create_token(
        self,
        device_id: str,
        token_hash: str,
        device_name: str,
        created_at: float,
        expires_at: float,
    ) -> None:
        with self._lock:
            self._tokens_by_hash[token_hash] = StoredDeviceToken(
                device_id=device_id,
                token_hash=token_hash,
                device_name=device_name,
                created_at=created_at,
                expires_at=expires_at,
                revoked=False,
            )
            self._device_index[device_id] = token_hash

    def get_token(self, token_hash: str) -> Optional[StoredDeviceToken]:
        with self._lock:
            return self._tokens_by_hash.get(token_hash)

    def revoke_token(self, token_hash: str) -> bool:
        with self._lock:
            record = self._tokens_by_hash.get(token_hash)
            if record is None or record.revoked:
                return False
            self._tokens_by_hash[token_hash] = StoredDeviceToken(
                device_id=record.device_id,
                token_hash=record.token_hash,
                device_name=record.device_name,
                created_at=record.created_at,
                expires_at=record.expires_at,
                revoked=True,
            )
            return True

    def revoke_by_device_id(self, device_id: str) -> bool:
        with self._lock:
            for token_hash, record in list(self._tokens_by_hash.items()):
                if record.device_id == device_id and not record.revoked:
                    self._tokens_by_hash[token_hash] = StoredDeviceToken(
                        device_id=record.device_id,
                        token_hash=record.token_hash,
                        device_name=record.device_name,
                        created_at=record.created_at,
                        expires_at=record.expires_at,
                        revoked=True,
                    )
                    return True
            return False


class InMemoryPairingExchangeCoordinator(PairingExchangeCoordinator):
    """AWS hackathon-final pass, phase 3. Operates directly on the SAME
    `InMemoryPairingCodeRepository`/`InMemoryDeviceTokenRepository`
    instances the rest of the app uses (constructed alongside them in
    `factory.py:build_pairing_exchange_coordinator`), so a read via
    either repository's own public methods always sees the effects of a
    successful `atomic_exchange` and vice versa.

    Atomicity here comes from acquiring BOTH repositories' locks for the
    whole operation, in a fixed order (codes, then tokens) to avoid any
    possibility of deadlock against another concurrent caller doing the
    same — there is currently only one call site that ever needs both
    locks at once (this class), so a fixed order is trivially
    deadlock-free, but is used anyway as cheap, explicit insurance against
    a future caller introducing a second one."""

    def __init__(
        self,
        code_repository: InMemoryPairingCodeRepository,
        token_repository: InMemoryDeviceTokenRepository,
    ) -> None:
        self._codes = code_repository
        self._tokens = token_repository

    def atomic_exchange(
        self,
        *,
        code_hash: str,
        now: float,
        device_id: str,
        token_hash: str,
        device_name: str,
        token_expires_at: float,
    ) -> PairingExchangeOutcome:
        with self._codes._lock:  # noqa: SLF001 - intentional, same-package internal coordination
            record = self._codes._codes.get(code_hash)  # noqa: SLF001
            if record is None or record["used"] or record["expires_at"] < now:
                return PairingExchangeOutcome.CODE_REJECTED

            with self._tokens._lock:  # noqa: SLF001
                if device_id in self._tokens._device_index:  # noqa: SLF001
                    return PairingExchangeOutcome.DEVICE_ID_COLLISION
                if token_hash in self._tokens._tokens_by_hash:  # noqa: SLF001
                    return PairingExchangeOutcome.TOKEN_HASH_COLLISION

                # Every effect below is applied only once every precondition
                # above has already been confirmed, and while still holding
                # BOTH locks — no other caller can observe a partial state.
                record["used"] = True
                self._tokens._tokens_by_hash[token_hash] = StoredDeviceToken(  # noqa: SLF001
                    device_id=device_id,
                    token_hash=token_hash,
                    device_name=device_name,
                    created_at=now,
                    expires_at=token_expires_at,
                    revoked=False,
                )
                self._tokens._device_index[device_id] = token_hash  # noqa: SLF001

            return PairingExchangeOutcome.SUCCESS
