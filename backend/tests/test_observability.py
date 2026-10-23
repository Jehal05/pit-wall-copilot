"""Tests for app/observability.py (spec v1.1 pass 4, mandatory correction
7): fail-closed production configuration validation, structured audit
logging with a hard field-safety guard, and the per-request correlation-ID
middleware.
"""

from __future__ import annotations

import json
import logging

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import Settings
from app.main import create_app
from app.observability import (
    AuditFieldViolation,
    CORRELATION_ID_HEADER,
    ProductionConfigurationError,
    audit_log,
    validate_production_configuration,
)

pytestmark = pytest.mark.unit


def _production_safe_settings(**overrides) -> Settings:
    """A configuration that IS safe for ENVIRONMENT=production, so tests
    can flip exactly one field at a time to prove each individual check
    is the one actually catching the problem (rather than several
    problems being bundled and only asserted on collectively)."""
    base = dict(
        environment="production",
        local_demo_auth_mode=False,
        demo_access_token="a-real-production-secret-from-secrets-manager",
        state_repository="dynamodb",
        pairing_repository=None,  # mirrors state_repository -> "dynamodb"
    )
    base.update(overrides)
    return Settings(**base)


class TestValidateProductionConfiguration:
    def test_a_fully_safe_production_configuration_passes(self):
        validate_production_configuration(_production_safe_settings())

    def test_development_environment_is_never_checked_even_when_everything_else_is_unsafe(self):
        unsafe_but_development = Settings(
            environment="development",
            local_demo_auth_mode=True,
            demo_access_token=None,
            state_repository="memory",
        )
        validate_production_configuration(unsafe_but_development)

    def test_local_demo_auth_mode_on_in_production_is_rejected(self):
        settings = _production_safe_settings(local_demo_auth_mode=True)
        with pytest.raises(ProductionConfigurationError, match="LOCAL_DEMO_AUTH_MODE"):
            validate_production_configuration(settings)

    def test_missing_demo_access_token_in_production_is_rejected(self):
        settings = _production_safe_settings(demo_access_token=None)
        with pytest.raises(ProductionConfigurationError, match="DEMO_ACCESS_TOKEN"):
            validate_production_configuration(settings)

    def test_non_dynamodb_state_repository_in_production_is_rejected(self):
        settings = _production_safe_settings(state_repository="memory")
        with pytest.raises(ProductionConfigurationError, match="STATE_REPOSITORY"):
            validate_production_configuration(settings)

    def test_non_dynamodb_effective_pairing_repository_in_production_is_rejected(self):
        settings = _production_safe_settings(state_repository="dynamodb", pairing_repository="memory")
        with pytest.raises(ProductionConfigurationError, match="pairing repository"):
            validate_production_configuration(settings)

    def test_multiple_simultaneous_problems_are_all_reported_together(self):
        settings = _production_safe_settings(
            local_demo_auth_mode=True,
            demo_access_token=None,
            state_repository="memory",
        )
        with pytest.raises(ProductionConfigurationError) as excinfo:
            validate_production_configuration(settings)
        message = str(excinfo.value)
        assert "LOCAL_DEMO_AUTH_MODE" in message
        assert "DEMO_ACCESS_TOKEN" in message
        assert "STATE_REPOSITORY" in message

    def test_error_message_never_includes_the_actual_configured_secret_value(self):
        settings = _production_safe_settings(local_demo_auth_mode=True)
        with pytest.raises(ProductionConfigurationError) as excinfo:
            validate_production_configuration(settings)
        assert settings.demo_access_token not in str(excinfo.value)

    # AWS hackathon-final pass, phase 4, Gap A: the app-side half of the
    # infra-level BedrockProviderRequiresModelArn Rule (see
    # infrastructure/template.yaml).
    def test_bedrock_provider_with_no_model_id_in_production_is_rejected(self):
        settings = _production_safe_settings(llm_provider="bedrock", bedrock_model_id="")
        with pytest.raises(ProductionConfigurationError, match="BEDROCK_MODEL_ID"):
            validate_production_configuration(settings)

    def test_bedrock_provider_with_a_model_id_in_production_passes(self):
        settings = _production_safe_settings(
            llm_provider="bedrock", bedrock_model_id="anthropic.claude-3-haiku-20240307-v1:0"
        )
        validate_production_configuration(settings)

    def test_template_provider_never_needs_a_model_id_in_production(self):
        settings = _production_safe_settings(llm_provider="template", bedrock_model_id="")
        validate_production_configuration(settings)

    def test_an_unrecognized_llm_provider_in_production_is_rejected(self):
        settings = _production_safe_settings(llm_provider="not-a-real-provider")
        with pytest.raises(ProductionConfigurationError, match="LLM_PROVIDER"):
            validate_production_configuration(settings)


class TestAuditLog:
    def _capture(self, caplog):
        caplog.set_level(logging.INFO, logger="pit_wall.audit")
        return caplog

    def test_a_safe_event_is_logged_as_one_json_line(self, caplog):
        self._capture(caplog)
        audit_log("device_pairing_succeeded", correlation_id="corr-123", device_id="device-abc", client_host="1.2.3.4")
        assert len(caplog.records) == 1
        record = json.loads(caplog.records[0].message)
        assert record["audit_event"] == "device_pairing_succeeded"
        assert record["correlation_id"] == "corr-123"
        assert record["device_id"] == "device-abc"
        assert record["client_host"] == "1.2.3.4"

    def test_correlation_id_is_omitted_entirely_when_not_supplied(self, caplog):
        self._capture(caplog)
        audit_log("pairing_code_generated", expires_in_seconds=300)
        record = json.loads(caplog.records[0].message)
        assert "correlation_id" not in record

    @pytest.mark.parametrize(
        "forbidden_field",
        [
            "code",
            "token",
            "secret",
            "password",
            "authorization",
            "x-demo-access-token",
            "pairing_code",
            "device_token",
            "master_token",
            "client_secret",
            "token_hash",
            "admin_password",
            "signing_key",
        ],
    )
    def test_a_secret_shaped_field_name_is_refused_rather_than_logged(self, caplog, forbidden_field):
        self._capture(caplog)
        with pytest.raises(AuditFieldViolation):
            audit_log("some_event", **{forbidden_field: "would-have-been-a-real-secret-value"})
        # The forbidden call must not have logged anything at all -- the
        # exception must be raised BEFORE any log record is emitted.
        assert len(caplog.records) == 0

    def test_forbidden_field_check_is_case_insensitive(self, caplog):
        self._capture(caplog)
        with pytest.raises(AuditFieldViolation):
            audit_log("some_event", Token="whatever")

    def test_field_names_that_merely_contain_but_do_not_end_with_a_forbidden_suffix_are_allowed(self, caplog):
        # e.g. "revocation_path" ends with "_path", not "_key"/"_token"/etc,
        # and must not be caught by an overly broad substring match.
        self._capture(caplog)
        audit_log("device_revoked", revocation_path="admin", revoked=True)
        assert len(caplog.records) == 1


class TestCorrelationIdMiddleware:
    async def _client(self) -> AsyncClient:
        app = create_app()
        lifespan_cm = app.router.lifespan_context(app)
        await lifespan_cm.__aenter__()
        transport = ASGITransport(app=app)
        return AsyncClient(transport=transport, base_url="http://test")

    @pytest.mark.asyncio
    async def test_a_fresh_correlation_id_is_generated_and_echoed_when_none_is_supplied(self):
        client = await self._client()
        response = await client.get("/health")
        assert CORRELATION_ID_HEADER in response.headers
        assert len(response.headers[CORRELATION_ID_HEADER]) > 0

    @pytest.mark.asyncio
    async def test_an_inbound_correlation_id_is_reused_rather_than_replaced(self):
        client = await self._client()
        response = await client.get("/health", headers={CORRELATION_ID_HEADER: "caller-supplied-id-123"})
        assert response.headers[CORRELATION_ID_HEADER] == "caller-supplied-id-123"

    @pytest.mark.asyncio
    async def test_two_requests_with_no_inbound_id_get_two_different_generated_ids(self):
        client = await self._client()
        first = await client.get("/health")
        second = await client.get("/health")
        assert first.headers[CORRELATION_ID_HEADER] != second.headers[CORRELATION_ID_HEADER]

    # AWS hackathon-final pass, P2-12: correlation-ID validation. An
    # inbound X-Request-ID is only trusted when well-formed; anything
    # else is silently replaced with a freshly generated one (never a
    # 400 — this is a correlation convenience, not authenticated input).
    @pytest.mark.asyncio
    async def test_a_well_formed_inbound_id_with_dots_underscores_and_hyphens_is_reused(self):
        client = await self._client()
        response = await client.get("/health", headers={CORRELATION_ID_HEADER: "trace.abc_123-XYZ"})
        assert response.headers[CORRELATION_ID_HEADER] == "trace.abc_123-XYZ"

    def test_an_inbound_id_containing_a_newline_is_replaced_not_reflected(self):
        # The real-world concern: header-value injection / log-line
        # splitting via a caller-controlled CR/LF sequence. httpx itself
        # refuses to even send a header value containing a literal
        # newline (client-side protection), so `_sanitize_correlation_id`
        # -- the function the middleware actually calls on whatever
        # Starlette handed it from the raw request headers -- is
        # exercised directly here to prove the SERVER-side sanitization
        # independently stops it, not incidentally relying on some
        # client library's own protections upstream.
        from app.observability import _sanitize_correlation_id

        malicious = "legit-looking-id\r\nX-Injected: evil"
        sanitized = _sanitize_correlation_id(malicious)
        assert sanitized != malicious
        assert "\r" not in sanitized
        assert "\n" not in sanitized

    def test_an_overly_long_inbound_id_is_replaced_with_a_fresh_one(self):
        from app.observability import _MAX_CORRELATION_ID_LENGTH, _sanitize_correlation_id

        too_long = "a" * (_MAX_CORRELATION_ID_LENGTH + 1)
        sanitized = _sanitize_correlation_id(too_long)
        assert sanitized != too_long
        assert len(sanitized) < len(too_long)

    def test_an_id_at_exactly_the_length_limit_is_accepted(self):
        from app.observability import _MAX_CORRELATION_ID_LENGTH, _sanitize_correlation_id

        exactly_max = "a" * _MAX_CORRELATION_ID_LENGTH
        assert _sanitize_correlation_id(exactly_max) == exactly_max

    def test_an_empty_inbound_id_is_replaced(self):
        from app.observability import _sanitize_correlation_id

        sanitized = _sanitize_correlation_id("")
        assert sanitized != ""
        assert len(sanitized) > 0

    def test_a_missing_inbound_id_generates_a_fresh_one(self):
        from app.observability import _sanitize_correlation_id

        sanitized = _sanitize_correlation_id(None)
        assert len(sanitized) > 0

    def test_ids_with_disallowed_characters_are_replaced(self):
        from app.observability import _sanitize_correlation_id

        for bad in ["has space", "has/slash", "has<tag>", "semi;colon", "quote\"here"]:
            sanitized = _sanitize_correlation_id(bad)
            assert sanitized != bad
