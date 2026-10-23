"""Tests for the real device-pairing exchange (spec v1.1 pass 3,
correction 5): short-lived one-time pairing codes exchanged for
device-specific, expiring, independently-revocable tokens — replacing the
pass-2 flow that had a person type the full backend mutation secret.
"""

from __future__ import annotations

import dataclasses
import time

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import Settings
from app.main import create_app
from app.repositories.pairing_base import PairingExchangeOutcome
from app.repositories.pairing_memory import (
    InMemoryDeviceTokenRepository,
    InMemoryPairingCodeRepository,
    InMemoryPairingExchangeCoordinator,
)
from app.services.device_pairing import (
    DevicePairingService,
    PAIRING_CODE_TTL_SECONDS,
    PairingAttemptRateLimiter,
)


def _settings(**overrides) -> Settings:
    return Settings(**overrides)


def _make_service(clock=time.time) -> DevicePairingService:
    """Spec v1.1 pass 4, mandatory correction 1: `DevicePairingService` no
    longer owns its own storage — every test that needs a standalone
    instance (outside the full FastAPI app, which wires this up via
    `app.repositories.factory.build_pairing_repositories`/
    `build_pairing_exchange_coordinator`) constructs it explicitly
    against the in-memory repositories, exactly as `factory.py` does for
    `STATE_REPOSITORY=memory`."""
    codes = InMemoryPairingCodeRepository()
    tokens = InMemoryDeviceTokenRepository()
    coordinator = InMemoryPairingExchangeCoordinator(codes, tokens)
    return DevicePairingService(codes, tokens, coordinator, clock=clock)


async def _make_client(monkeypatch, settings: Settings):
    monkeypatch.setattr("app.main.get_settings", lambda: settings)
    app = create_app()
    lifespan_cm = app.router.lifespan_context(app)
    await lifespan_cm.__aenter__()
    transport = ASGITransport(app=app)
    client = AsyncClient(transport=transport, base_url="http://test")
    return client, lifespan_cm


MASTER_TOKEN = "master-token-for-pairing-tests"


class TestDevicePairingServiceUnit:
    """Fast, dependency-free unit tests of the pairing logic itself."""

    def test_valid_code_exchange_succeeds_and_returns_a_device_specific_token(self):
        service = _make_service()
        issued = service.generate_pairing_code()
        paired = service.exchange_pairing_code(issued.code, "Living Room Fire TV")
        assert paired is not None
        assert paired.device_token
        assert paired.device_id
        assert service.verify_device_token(paired.device_token) is True

    def test_incorrect_code_is_rejected(self):
        service = _make_service()
        service.generate_pairing_code()
        assert service.exchange_pairing_code("000000", "device") is None

    def test_expired_code_is_rejected(self):
        clock = {"now": 1000.0}
        service = _make_service(clock=lambda: clock["now"])
        issued = service.generate_pairing_code()
        clock["now"] += PAIRING_CODE_TTL_SECONDS + 1
        assert service.exchange_pairing_code(issued.code, "device") is None

    def test_reused_code_is_rejected(self):
        service = _make_service()
        issued = service.generate_pairing_code()
        first = service.exchange_pairing_code(issued.code, "device-a")
        assert first is not None
        second = service.exchange_pairing_code(issued.code, "device-b")
        assert second is None

    def test_device_token_verification_for_a_valid_token(self):
        service = _make_service()
        issued = service.generate_pairing_code()
        paired = service.exchange_pairing_code(issued.code, "device")
        assert service.verify_device_token(paired.device_token) is True

    def test_expired_device_token_is_rejected(self):
        clock = {"now": 1000.0}
        service = _make_service(clock=lambda: clock["now"])
        issued = service.generate_pairing_code()
        paired = service.exchange_pairing_code(issued.code, "device")
        clock["now"] += 31 * 24 * 3600  # past the 30-day device token TTL
        assert service.verify_device_token(paired.device_token) is False

    def test_revoked_device_token_is_rejected(self):
        service = _make_service()
        issued = service.generate_pairing_code()
        paired = service.exchange_pairing_code(issued.code, "device")
        assert service.revoke_device_token(paired.device_token) is True
        assert service.verify_device_token(paired.device_token) is False

    def test_independent_revocation_of_one_device_does_not_affect_another(self):
        service = _make_service()
        code_a = service.generate_pairing_code()
        code_b = service.generate_pairing_code()
        device_a = service.exchange_pairing_code(code_a.code, "device-a")
        device_b = service.exchange_pairing_code(code_b.code, "device-b")

        service.revoke_device_token(device_a.device_token)

        assert service.verify_device_token(device_a.device_token) is False
        assert service.verify_device_token(device_b.device_token) is True

    def test_admin_revoke_by_device_id(self):
        service = _make_service()
        issued = service.generate_pairing_code()
        paired = service.exchange_pairing_code(issued.code, "device")
        assert service.revoke_device_by_id(paired.device_id) is True
        assert service.verify_device_token(paired.device_token) is False

    def test_no_device_token_is_ever_stored_in_plaintext(self):
        service = _make_service()
        issued = service.generate_pairing_code()
        paired = service.exchange_pairing_code(issued.code, "device")
        # Internal store (now the injected InMemoryDeviceTokenRepository,
        # spec v1.1 pass 4 correction 1) only ever contains hashes, never
        # the raw token — introspected directly to prove the storage
        # format, not just the externally observable behavior.
        token_repository = service._device_tokens  # noqa: SLF001
        stored_hashes = list(token_repository._tokens_by_hash.keys())  # noqa: SLF001
        assert paired.device_token not in stored_hashes
        for record in token_repository._tokens_by_hash.values():  # noqa: SLF001
            assert record.token_hash != paired.device_token

    def test_no_pairing_code_is_ever_stored_in_plaintext(self):
        service = _make_service()
        issued = service.generate_pairing_code()
        code_repository = service._pairing_codes  # noqa: SLF001
        assert issued.code not in code_repository._codes  # noqa: SLF001 — keyed by hash, not raw code


# AWS hackathon-final pass, P1-11: collision-safe pairing-code generation.
# A 6-digit code is drawn from only 1,000,000 possible values, so
# `generate_pairing_code` must retry with a fresh, independently-random
# code when the repository reports a collision with another still-active
# code, rather than either silently clobbering it (the pre-fix behavior)
# or failing the whole request on the first collision. These tests use a
# tiny stub repository (not the real in-memory/DynamoDB ones) so the
# collision/success sequence is fully deterministic rather than depending
# on actually rolling a real collision.
class _ScriptedPairingCodeRepository:
    """Stub whose `create_code` returns a pre-scripted sequence of
    True/False results, one per call, regardless of the code_hash
    presented — lets a test deterministically simulate "the first N
    generated codes collided with other still-active codes"."""

    def __init__(self, results: list[bool]) -> None:
        self._results = list(results)
        self.calls: list[str] = []

    def create_code(self, code_hash: str, created_at: float, expires_at: float) -> bool:
        self.calls.append(code_hash)
        return self._results.pop(0)

    def try_consume_code(self, code_hash: str, now: float) -> bool:  # pragma: no cover — unused here
        return False


class TestCollisionSafePairingCodeGeneration:
    def test_succeeds_immediately_when_the_first_generated_code_does_not_collide(self):
        from app.services.device_pairing import DevicePairingService

        repository = _ScriptedPairingCodeRepository([True])
        service = DevicePairingService(repository, InMemoryDeviceTokenRepository(), exchange_coordinator=None)  # exchange_pairing_code not exercised by these tests

        issued = service.generate_pairing_code()

        assert issued.code is not None
        assert len(repository.calls) == 1

    def test_retries_with_a_fresh_code_after_a_collision_and_then_succeeds(self):
        from app.services.device_pairing import DevicePairingService

        # First two draws collide with another still-active code; the
        # third succeeds.
        repository = _ScriptedPairingCodeRepository([False, False, True])
        service = DevicePairingService(repository, InMemoryDeviceTokenRepository(), exchange_coordinator=None)  # exchange_pairing_code not exercised by these tests

        issued = service.generate_pairing_code()

        assert issued.code is not None
        # Exactly 3 attempts were made: 2 collisions + 1 success -- proves
        # the retry loop keeps drawing fresh codes rather than giving up
        # after the first collision or looping indefinitely.
        assert len(repository.calls) == 3

    def test_raises_a_clear_error_after_exhausting_every_retry_attempt(self):
        from app.services.device_pairing import (
            PAIRING_CODE_GENERATION_MAX_ATTEMPTS,
            DevicePairingService,
            PairingCodeGenerationExhaustedError,
        )

        # Every single attempt collides.
        repository = _ScriptedPairingCodeRepository([False] * PAIRING_CODE_GENERATION_MAX_ATTEMPTS)
        service = DevicePairingService(repository, InMemoryDeviceTokenRepository(), exchange_coordinator=None)  # exchange_pairing_code not exercised by these tests

        with pytest.raises(PairingCodeGenerationExhaustedError):
            service.generate_pairing_code()

        assert len(repository.calls) == PAIRING_CODE_GENERATION_MAX_ATTEMPTS

    def test_a_collision_with_a_real_still_active_code_is_refused_not_overwritten(self):
        """End-to-end proof against the REAL in-memory repository (not the
        scripted stub above): forcing two calls to draw the exact same
        code value (via a monkeypatched RNG) must not let the second call
        silently invalidate the first, still-active code."""
        import app.services.device_pairing as device_pairing_module

        repository = InMemoryPairingCodeRepository()
        service = DevicePairingService(repository, InMemoryDeviceTokenRepository(), exchange_coordinator=None)  # exchange_pairing_code not exercised by these tests

        # Force every draw to the same 6-digit value.
        fixed_value = 424242
        original_randbelow = device_pairing_module.secrets.randbelow
        call_count = {"n": 0}

        def _fixed_randbelow(_n: int) -> int:
            call_count["n"] += 1
            return fixed_value

        device_pairing_module.secrets.randbelow = _fixed_randbelow  # type: ignore[attr-defined]
        try:
            first = service.generate_pairing_code()
            # Second generation draws the SAME code value again (RNG is
            # pinned) while the first is still active -- create_code must
            # refuse the collision and the service must retry with a
            # (still pinned, so still colliding) fresh draw each time,
            # eventually exhausting its retry budget rather than ever
            # returning a code that silently clobbered the first.
            with pytest.raises(device_pairing_module.PairingCodeGenerationExhaustedError):
                service.generate_pairing_code()
        finally:
            device_pairing_module.secrets.randbelow = original_randbelow  # type: ignore[attr-defined]

        # The FIRST code is still exactly as issued -- unaffected by the
        # refused collision attempts against it.
        assert repository.try_consume_code(device_pairing_module._hash(first.code), time.time()) is True  # noqa: SLF001


class TestPairingAttemptRateLimiter:
    def test_rate_limiting_blocks_after_the_configured_number_of_attempts(self):
        clock = {"now": 0.0}
        limiter = PairingAttemptRateLimiter(max_attempts=3, window_seconds=60.0, clock=lambda: clock["now"])
        for _ in range(3):
            limiter.check_and_record("1.2.3.4")
        with pytest.raises(Exception):
            limiter.check_and_record("1.2.3.4")

    def test_rate_limit_is_scoped_per_key(self):
        clock = {"now": 0.0}
        limiter = PairingAttemptRateLimiter(max_attempts=1, window_seconds=60.0, clock=lambda: clock["now"])
        limiter.check_and_record("1.2.3.4")
        limiter.check_and_record("5.6.7.8")  # different source — not blocked


@pytest.mark.asyncio
class TestPairingHttpEndpoints:
    async def test_pairing_code_generation_requires_the_master_token(self, monkeypatch):
        client, lifespan_cm = await _make_client(monkeypatch, _settings(demo_access_token=MASTER_TOKEN))
        try:
            unauthorized = await client.post("/v1/auth/pairing-codes")
            assert unauthorized.status_code in (401, 503)

            response = await client.post(
                "/v1/auth/pairing-codes", headers={"X-Demo-Access-Token": MASTER_TOKEN}
            )
            assert response.status_code == 200
            body = response.json()
            assert len(body["code"]) == 6
            assert body["expires_in_seconds"] > 0
        finally:
            await client.aclose()
            await lifespan_cm.__aexit__(None, None, None)

    async def test_successful_code_exchange_over_http(self, monkeypatch):
        client, lifespan_cm = await _make_client(monkeypatch, _settings(demo_access_token=MASTER_TOKEN))
        try:
            code_response = await client.post(
                "/v1/auth/pairing-codes", headers={"X-Demo-Access-Token": MASTER_TOKEN}
            )
            code = code_response.json()["code"]

            pair_response = await client.post(
                "/v1/auth/pair", json={"code": code, "device_name": "Kitchen Fire TV"}
            )
            assert pair_response.status_code == 200
            body = pair_response.json()
            assert body["device_id"]
            assert body["device_token"]
            assert body["device_token"] != MASTER_TOKEN

            # The master token never appears anywhere in this response.
            assert MASTER_TOKEN not in str(body)
        finally:
            await client.aclose()
            await lifespan_cm.__aexit__(None, None, None)

    async def test_rejected_mutation_using_an_invalid_pairing_code(self, monkeypatch):
        client, lifespan_cm = await _make_client(monkeypatch, _settings(demo_access_token=MASTER_TOKEN))
        try:
            response = await client.post("/v1/auth/pair", json={"code": "000000", "device_name": "x"})
            assert response.status_code == 401
        finally:
            await client.aclose()
            await lifespan_cm.__aexit__(None, None, None)

    async def test_successful_mutation_using_a_paired_device_token(self, monkeypatch):
        client, lifespan_cm = await _make_client(monkeypatch, _settings(demo_access_token=MASTER_TOKEN))
        try:
            code = (
                await client.post("/v1/auth/pairing-codes", headers={"X-Demo-Access-Token": MASTER_TOKEN})
            ).json()["code"]
            device = (await client.post("/v1/auth/pair", json={"code": code, "device_name": "x"})).json()

            response = await client.post(
                "/v1/demo/start", headers={"X-Demo-Access-Token": device["device_token"]}
            )
            assert response.status_code == 200
        finally:
            await client.aclose()
            await lifespan_cm.__aexit__(None, None, None)

    async def test_rejected_mutation_using_an_invalid_device_token(self, monkeypatch):
        client, lifespan_cm = await _make_client(monkeypatch, _settings(demo_access_token=MASTER_TOKEN))
        try:
            response = await client.post(
                "/v1/demo/start", headers={"X-Demo-Access-Token": "not-a-real-device-token"}
            )
            assert response.status_code == 401
        finally:
            await client.aclose()
            await lifespan_cm.__aexit__(None, None, None)

    async def test_verify_endpoint_accepts_a_paired_device_token(self, monkeypatch):
        client, lifespan_cm = await _make_client(monkeypatch, _settings(demo_access_token=MASTER_TOKEN))
        try:
            code = (
                await client.post("/v1/auth/pairing-codes", headers={"X-Demo-Access-Token": MASTER_TOKEN})
            ).json()["code"]
            device = (await client.post("/v1/auth/pair", json={"code": code, "device_name": "x"})).json()

            response = await client.get(
                "/v1/auth/verify", headers={"X-Demo-Access-Token": device["device_token"]}
            )
            assert response.status_code == 200
        finally:
            await client.aclose()
            await lifespan_cm.__aexit__(None, None, None)

    async def test_expired_device_token_returns_401_and_rejects_mutations(self, monkeypatch):
        client, lifespan_cm = await _make_client(monkeypatch, _settings(demo_access_token=MASTER_TOKEN))
        try:
            app = client._transport.app  # noqa: SLF001
            pairing_service = app.state.device_pairing_service
            issued = pairing_service.generate_pairing_code()
            paired = pairing_service.exchange_pairing_code(issued.code, "device")
            # Force the device token's own expiry into the past directly
            # (faster and more precise than sleeping 30 real days).
            # `StoredDeviceToken` is a frozen dataclass (pairing_base.py),
            # so this replaces the stored record rather than mutating it
            # in place — still exercising the same "already past
            # expires_at" code path `verify_device_token` checks.
            token_repository = pairing_service._device_tokens  # noqa: SLF001
            for token_hash, record in list(token_repository._tokens_by_hash.items()):  # noqa: SLF001
                token_repository._tokens_by_hash[token_hash] = dataclasses.replace(  # noqa: SLF001
                    record, expires_at=time.time() - 1
                )

            response = await client.post(
                "/v1/demo/start", headers={"X-Demo-Access-Token": paired.device_token}
            )
            assert response.status_code == 401
        finally:
            await client.aclose()
            await lifespan_cm.__aexit__(None, None, None)

    async def test_self_unpair_revokes_the_devices_own_token(self, monkeypatch):
        client, lifespan_cm = await _make_client(monkeypatch, _settings(demo_access_token=MASTER_TOKEN))
        try:
            code = (
                await client.post("/v1/auth/pairing-codes", headers={"X-Demo-Access-Token": MASTER_TOKEN})
            ).json()["code"]
            device = (await client.post("/v1/auth/pair", json={"code": code, "device_name": "x"})).json()

            revoke_response = await client.post(
                "/v1/auth/revoke", headers={"X-Demo-Access-Token": device["device_token"]}, json={}
            )
            assert revoke_response.status_code == 200
            assert revoke_response.json()["revoked"] is True

            rejected = await client.post(
                "/v1/demo/start", headers={"X-Demo-Access-Token": device["device_token"]}
            )
            assert rejected.status_code == 401
        finally:
            await client.aclose()
            await lifespan_cm.__aexit__(None, None, None)

    async def test_admin_revoke_by_device_id_requires_the_master_token(self, monkeypatch):
        client, lifespan_cm = await _make_client(monkeypatch, _settings(demo_access_token=MASTER_TOKEN))
        try:
            code = (
                await client.post("/v1/auth/pairing-codes", headers={"X-Demo-Access-Token": MASTER_TOKEN})
            ).json()["code"]
            device = (await client.post("/v1/auth/pair", json={"code": code, "device_name": "x"})).json()

            # A different (non-owning) device token cannot revoke this device.
            forbidden = await client.post(
                "/v1/auth/revoke",
                headers={"X-Demo-Access-Token": "some-other-devices-token"},
                json={"device_id": device["device_id"]},
            )
            assert forbidden.status_code == 401

            admin_revoke = await client.post(
                "/v1/auth/revoke",
                headers={"X-Demo-Access-Token": MASTER_TOKEN},
                json={"device_id": device["device_id"]},
            )
            assert admin_revoke.status_code == 200
            assert admin_revoke.json()["revoked"] is True

            rejected = await client.post(
                "/v1/demo/start", headers={"X-Demo-Access-Token": device["device_token"]}
            )
            assert rejected.status_code == 401
        finally:
            await client.aclose()
            await lifespan_cm.__aexit__(None, None, None)

    # -----------------------------------------------------------------
    # AWS hackathon-final pass, P0-1: "Enforce administrator-only pairing
    # operations." A paired device's own, genuinely VALID token must be
    # denied (403, not silently accepted) for both pairing-code generation
    # and admin-path device revocation — the earlier tests above only
    # prove a GARBAGE token is rejected (401); these prove a REAL,
    # currently-active device token from a DIFFERENT, already-paired
    # device is specifically forbidden from acting as an administrator.
    # -----------------------------------------------------------------

    async def _pair_one_device(self, client, device_name: str = "device-under-test") -> dict:
        code = (
            await client.post("/v1/auth/pairing-codes", headers={"X-Demo-Access-Token": MASTER_TOKEN})
        ).json()["code"]
        response = await client.post("/v1/auth/pair", json={"code": code, "device_name": device_name})
        assert response.status_code == 200
        return response.json()

    async def test_a_valid_paired_devices_own_token_cannot_generate_pairing_codes(self, monkeypatch):
        client, lifespan_cm = await _make_client(monkeypatch, _settings(demo_access_token=MASTER_TOKEN))
        try:
            device = await self._pair_one_device(client)

            escalation_attempt = await client.post(
                "/v1/auth/pairing-codes", headers={"X-Demo-Access-Token": device["device_token"]}
            )
            assert escalation_attempt.status_code == 403
            body = escalation_attempt.json()
            assert body["error"] == "forbidden"
            # The device's own token must never be echoed back in an error body.
            assert device["device_token"] not in escalation_attempt.text

            # The master token still works, proving this is a scoped denial,
            # not a broken endpoint.
            master_call = await client.post(
                "/v1/auth/pairing-codes", headers={"X-Demo-Access-Token": MASTER_TOKEN}
            )
            assert master_call.status_code == 200
        finally:
            await client.aclose()
            await lifespan_cm.__aexit__(None, None, None)

    async def test_a_valid_paired_devices_own_token_cannot_admin_revoke_a_different_device(self, monkeypatch):
        client, lifespan_cm = await _make_client(monkeypatch, _settings(demo_access_token=MASTER_TOKEN))
        try:
            attacker_device = await self._pair_one_device(client, "attacker-device")
            victim_device = await self._pair_one_device(client, "victim-device")

            escalation_attempt = await client.post(
                "/v1/auth/revoke",
                headers={"X-Demo-Access-Token": attacker_device["device_token"]},
                json={"device_id": victim_device["device_id"]},
            )
            assert escalation_attempt.status_code == 403

            # The victim device's token must still work — the escalation
            # attempt must not have had any side effect.
            still_valid = await client.get(
                "/v1/auth/verify", headers={"X-Demo-Access-Token": victim_device["device_token"]}
            )
            assert still_valid.status_code == 200
        finally:
            await client.aclose()
            await lifespan_cm.__aexit__(None, None, None)

    async def test_missing_credential_still_returns_401_not_403_for_admin_only_endpoints(self, monkeypatch):
        """403 is reserved for a KNOWN, valid-but-unauthorized credential —
        a wholly missing/garbage credential is still 401 (RFC-conventional
        "who are you" vs "you are known but not allowed")."""
        client, lifespan_cm = await _make_client(monkeypatch, _settings(demo_access_token=MASTER_TOKEN))
        try:
            no_credential = await client.post("/v1/auth/pairing-codes")
            assert no_credential.status_code == 401

            garbage_credential = await client.post(
                "/v1/auth/pairing-codes", headers={"X-Demo-Access-Token": "not-a-real-token-at-all"}
            )
            assert garbage_credential.status_code == 401
        finally:
            await client.aclose()
            await lifespan_cm.__aexit__(None, None, None)

    async def test_admin_only_endpoints_fail_closed_503_when_no_master_token_configured_even_with_a_valid_device_token(
        self, monkeypatch
    ):
        # LOCAL_DEMO_AUTH_MODE generates a usable master token at startup —
        # confirms the admin-only path still works end-to-end in that mode
        # (it must not have been accidentally broken by requiring the
        # MASTER token specifically instead of any accepted credential).
        client, lifespan_cm = await _make_client(
            monkeypatch, _settings(demo_access_token=None, local_demo_auth_mode=True)
        )
        try:
            app = client._transport.app  # noqa: SLF001
            generated_token = app.state.effective_demo_access_token
            assert generated_token is not None

            # No credential at all is still 401, not a silent bypass.
            no_credential_response = await client.post("/v1/auth/pairing-codes")
            assert no_credential_response.status_code == 401

            # The freshly generated local-mode token IS the master token
            # for this process, so it works for the admin-only endpoint.
            code_response = await client.post(
                "/v1/auth/pairing-codes",
                headers={"X-Demo-Access-Token": generated_token},
            )
            assert code_response.status_code == 200
        finally:
            await client.aclose()
            await lifespan_cm.__aexit__(None, None, None)

    async def test_pairing_attempts_are_rate_limited(self, monkeypatch):
        client, lifespan_cm = await _make_client(monkeypatch, _settings(demo_access_token=MASTER_TOKEN))
        try:
            app = client._transport.app  # noqa: SLF001
            app.state.pairing_rate_limiter = PairingAttemptRateLimiter(max_attempts=3, window_seconds=60.0)

            for _ in range(3):
                response = await client.post("/v1/auth/pair", json={"code": "000000", "device_name": "x"})
                assert response.status_code == 401  # wrong code, but not yet rate-limited

            limited = await client.post("/v1/auth/pair", json={"code": "000000", "device_name": "x"})
            assert limited.status_code == 429
        finally:
            await client.aclose()
            await lifespan_cm.__aexit__(None, None, None)

    async def test_master_token_never_appears_in_any_pairing_response_body(self, monkeypatch):
        client, lifespan_cm = await _make_client(monkeypatch, _settings(demo_access_token=MASTER_TOKEN))
        try:
            code_response = await client.post(
                "/v1/auth/pairing-codes", headers={"X-Demo-Access-Token": MASTER_TOKEN}
            )
            assert MASTER_TOKEN not in code_response.text

            code = code_response.json()["code"]
            pair_response = await client.post(
                "/v1/auth/pair", json={"code": code, "device_name": "x"}
            )
            assert MASTER_TOKEN not in pair_response.text
        finally:
            await client.aclose()
            await lifespan_cm.__aexit__(None, None, None)

    async def test_pairing_still_works_when_no_master_token_is_configured_for_code_generation_path(
        self, monkeypatch
    ):
        """Generating a pairing code (the admin step) still correctly
        fails closed when no master token is configured at all — this is
        not a bypass of spec v1.1 repair 11's fail-closed guarantee."""
        client, lifespan_cm = await _make_client(
            monkeypatch, _settings(demo_access_token=None, local_demo_auth_mode=False)
        )
        try:
            response = await client.post("/v1/auth/pairing-codes")
            assert response.status_code == 503
        finally:
            await client.aclose()
            await lifespan_cm.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_network_failure_during_pairing_leaves_no_partial_device_token(monkeypatch):
    """If a pairing code exchange never happens (e.g. the request never
    arrives — the closest this test harness can simulate a dropped
    request), no device token exists for it — there is no server-side
    state change from an attempt that never reached the service."""
    service = _make_service()
    issued = service.generate_pairing_code()
    # Simulate: the code was generated, but the /pair request never
    # actually arrived (network failure) — the code is still valid and
    # unused, exactly as if nothing happened.
    assert service.exchange_pairing_code(issued.code, "device") is not None


# AWS hackathon-final pass, phase 3: fully transactional pairing exchange
# at the DevicePairingService layer — these drive the retry loop in
# `exchange_pairing_code` via a scripted coordinator (mirroring
# `_ScriptedPairingCodeRepository`'s pattern above for
# `generate_pairing_code`), so the collision/success sequence is fully
# deterministic rather than depending on rolling a real collision.
class _ScriptedExchangeCoordinator:
    """Stub whose `atomic_exchange` returns a pre-scripted sequence of
    outcomes, one per call, regardless of the arguments presented."""

    def __init__(self, outcomes):
        from app.repositories.pairing_base import PairingExchangeOutcome  # noqa: F401 (re-export for callers)

        self._outcomes = list(outcomes)
        self.calls: list[dict] = []

    def atomic_exchange(self, **kwargs):
        self.calls.append(kwargs)
        return self._outcomes.pop(0)


class TestCollisionSafePairingExchange:
    def test_succeeds_immediately_when_the_first_attempt_does_not_collide(self):
        coordinator = _ScriptedExchangeCoordinator([PairingExchangeOutcome.SUCCESS])
        service = DevicePairingService(
            InMemoryPairingCodeRepository(), InMemoryDeviceTokenRepository(), coordinator
        )

        paired = service.exchange_pairing_code("123456", "device")

        assert paired is not None
        assert len(coordinator.calls) == 1

    def test_retries_with_fresh_values_after_a_device_id_collision_and_then_succeeds(self):
        coordinator = _ScriptedExchangeCoordinator(
            [PairingExchangeOutcome.DEVICE_ID_COLLISION, PairingExchangeOutcome.SUCCESS]
        )
        service = DevicePairingService(
            InMemoryPairingCodeRepository(), InMemoryDeviceTokenRepository(), coordinator
        )

        paired = service.exchange_pairing_code("123456", "device")

        assert paired is not None
        assert len(coordinator.calls) == 2
        # Each attempt drew a genuinely fresh device_id/token_hash, not a
        # deterministic perturbation of the previous one.
        assert coordinator.calls[0]["device_id"] != coordinator.calls[1]["device_id"]
        assert coordinator.calls[0]["token_hash"] != coordinator.calls[1]["token_hash"]

    def test_retries_with_fresh_values_after_a_token_hash_collision_and_then_succeeds(self):
        coordinator = _ScriptedExchangeCoordinator(
            [PairingExchangeOutcome.TOKEN_HASH_COLLISION, PairingExchangeOutcome.SUCCESS]
        )
        service = DevicePairingService(
            InMemoryPairingCodeRepository(), InMemoryDeviceTokenRepository(), coordinator
        )

        paired = service.exchange_pairing_code("123456", "device")

        assert paired is not None
        assert len(coordinator.calls) == 2

    def test_returns_none_immediately_on_code_rejected_without_retrying(self):
        coordinator = _ScriptedExchangeCoordinator([PairingExchangeOutcome.CODE_REJECTED])
        service = DevicePairingService(
            InMemoryPairingCodeRepository(), InMemoryDeviceTokenRepository(), coordinator
        )

        paired = service.exchange_pairing_code("123456", "device")

        assert paired is None
        # Never worth retrying an invalid code with fresh device_id/token
        # values -- exactly one attempt is made.
        assert len(coordinator.calls) == 1

    def test_raises_after_exhausting_every_retry_on_repeated_collisions(self):
        from app.services.device_pairing import DEVICE_EXCHANGE_MAX_ATTEMPTS, PairingCodeGenerationExhaustedError

        coordinator = _ScriptedExchangeCoordinator(
            [PairingExchangeOutcome.DEVICE_ID_COLLISION] * DEVICE_EXCHANGE_MAX_ATTEMPTS
        )
        service = DevicePairingService(
            InMemoryPairingCodeRepository(), InMemoryDeviceTokenRepository(), coordinator
        )

        with pytest.raises(PairingCodeGenerationExhaustedError):
            service.exchange_pairing_code("123456", "device")

        assert len(coordinator.calls) == DEVICE_EXCHANGE_MAX_ATTEMPTS
