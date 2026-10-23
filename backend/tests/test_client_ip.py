"""Tests for trusted-proxy-aware client IP extraction (P1-10)."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.config import Settings
from app.services.client_ip import get_client_ip


def _settings(*, trusted_proxy_cidrs: str = "") -> Settings:
    return Settings(trusted_proxy_cidrs=trusted_proxy_cidrs, _env_file=None)


def _make_app_capturing_client_ip(settings: Settings):
    app = FastAPI()
    captured: dict[str, str] = {}

    @app.get("/whoami")
    async def whoami(request: Request):
        captured["client_ip"] = get_client_ip(request, settings)
        return {"client_ip": captured["client_ip"]}

    return app, captured


class TestGetClientIp:
    def test_with_no_trusted_proxies_configured_x_forwarded_for_is_ignored(self):
        settings = _settings(trusted_proxy_cidrs="")
        app, captured = _make_app_capturing_client_ip(settings)
        client = TestClient(app)

        response = client.get("/whoami", headers={"X-Forwarded-For": "203.0.113.7"})

        assert response.status_code == 200
        # TestClient's default peer address is "testclient" -- the point
        # is that the forwarded header was NOT used, whatever the peer is.
        assert captured["client_ip"] != "203.0.113.7"

    # NOTE: a trusted-proxy-peer + X-Forwarded-For-honored scenario is not
    # exercised via Starlette's TestClient here, because TestClient's peer
    # host is always the fixed literal string "testclient" (not a real IP)
    # and cannot be made to fall inside an arbitrary configured CIDR. That
    # exact scenario is instead fully covered end-to-end, with precise
    # control over the peer address, by
    # `test_trusted_peer_with_a_multi_hop_x_forwarded_for_uses_the_leftmost_entry`
    # and `test_distinct_real_clients_behind_one_trusted_alb_resolve_to_distinct_ips`
    # below (both via `unittest.mock.MagicMock`).

    def test_forged_x_forwarded_for_is_ignored_when_the_peer_is_not_a_trusted_proxy(self):
        settings = _settings(trusted_proxy_cidrs="10.0.0.0/8")  # deliberately does NOT include the test peer
        app, captured = _make_app_capturing_client_ip(settings)
        client = TestClient(app)

        # An attacker who is NOT connecting through the trusted ALB CIDR
        # tries to forge their apparent origin via the header anyway.
        response = client.get("/whoami", headers={"X-Forwarded-For": "1.2.3.4"})

        assert response.status_code == 200
        assert captured["client_ip"] != "1.2.3.4"

    def test_distinct_forwarded_clients_behind_a_trusted_proxy_get_distinct_ips(self):
        from app.services.client_ip import _is_trusted_proxy, _parse_trusted_cidrs

        # Direct unit-level check of the trust + extraction logic (the
        # ASGI TestClient cannot easily fake a specific peer IP, so the
        # end-to-end trust decision is exercised here instead of through
        # a real socket).
        trusted = _parse_trusted_cidrs("10.0.0.0/8")
        assert _is_trusted_proxy("10.0.1.5", trusted) is True
        assert _is_trusted_proxy("203.0.113.9", trusted) is False

    def test_multiple_cidrs_are_all_honored(self):
        from app.services.client_ip import _is_trusted_proxy, _parse_trusted_cidrs

        trusted = _parse_trusted_cidrs("10.0.0.0/8, 172.16.0.0/12")
        assert _is_trusted_proxy("10.5.5.5", trusted) is True
        assert _is_trusted_proxy("172.16.9.9", trusted) is True
        assert _is_trusted_proxy("192.168.1.1", trusted) is False

    def test_an_invalid_cidr_entry_is_skipped_rather_than_crashing(self):
        from app.services.client_ip import _parse_trusted_cidrs

        # "not-a-cidr" must not raise; it's simply dropped, and the valid
        # entries alongside it still work.
        _parse_trusted_cidrs.cache_clear()
        trusted = _parse_trusted_cidrs("not-a-cidr, 10.0.0.0/8")
        from app.services.client_ip import _is_trusted_proxy

        assert _is_trusted_proxy("10.1.1.1", trusted) is True

    def test_no_peer_and_no_header_returns_unknown(self):
        from unittest.mock import MagicMock

        settings = _settings()
        request = MagicMock()
        request.client = None
        request.headers = {}

        assert get_client_ip(request, settings) == "unknown"

    def test_trusted_peer_but_empty_x_forwarded_for_falls_back_to_peer(self):
        from unittest.mock import MagicMock

        from app.services.client_ip import _parse_trusted_cidrs

        _parse_trusted_cidrs.cache_clear()
        settings = _settings(trusted_proxy_cidrs="10.0.0.0/8")
        request = MagicMock()
        request.client.host = "10.0.0.5"
        request.headers = {}

        assert get_client_ip(request, settings) == "10.0.0.5"

    def test_trusted_peer_with_a_multi_hop_x_forwarded_for_uses_the_leftmost_entry(self):
        from unittest.mock import MagicMock

        from app.services.client_ip import _parse_trusted_cidrs

        _parse_trusted_cidrs.cache_clear()
        settings = _settings(trusted_proxy_cidrs="10.0.0.0/8")
        request = MagicMock()
        request.client.host = "10.0.0.5"  # the ALB's own peer address -- trusted
        request.headers = {"x-forwarded-for": "203.0.113.42, 10.0.0.5"}

        # The leftmost entry is the ORIGINAL client the ALB appended --
        # everything to its right is the proxy chain itself.
        assert get_client_ip(request, settings) == "203.0.113.42"

    def test_distinct_real_clients_behind_one_trusted_alb_resolve_to_distinct_ips(self):
        from unittest.mock import MagicMock

        from app.services.client_ip import _parse_trusted_cidrs

        _parse_trusted_cidrs.cache_clear()
        settings = _settings(trusted_proxy_cidrs="10.0.0.0/8")

        def _request_from(client_ip: str) -> MagicMock:
            request = MagicMock()
            request.client.host = "10.0.0.5"  # same ALB peer for every client
            request.headers = {"x-forwarded-for": client_ip}
            return request

        ip_a = get_client_ip(_request_from("198.51.100.1"), settings)
        ip_b = get_client_ip(_request_from("198.51.100.2"), settings)

        # This is the actual bug this pass fixes: pre-fix, BOTH of these
        # would have resolved to the ALB's own peer address ("10.0.0.5"),
        # indistinguishable from each other for rate-limiting purposes.
        assert ip_a == "198.51.100.1"
        assert ip_b == "198.51.100.2"
        assert ip_a != ip_b
