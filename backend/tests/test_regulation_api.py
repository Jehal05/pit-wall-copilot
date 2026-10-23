"""REST-level tests for GET /v1/regulations — spec v1.1 repair 8.

Unit coverage of the matching algorithm itself lives in
tests/test_regulation_service.py (including the >= 20 unsupported-query
requirement); these tests only check the HTTP contract: status codes,
response shape, and that `found`/`evidence` round-trip correctly through
the API layer.
"""

from __future__ import annotations


async def test_get_regulation_supported_query_returns_200_with_evidence(api_client):
    client, _service = api_client
    response = await client.get(
        "/v1/regulations", params={"query": "What tire compounds are available: soft, medium, hard?"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["found"] is True
    assert len(body["evidence"]) >= 1
    evidence = body["evidence"][0]
    assert evidence["document_id"] == "reg-tires-001"
    assert evidence["support_status"] == "SUPPORTED"
    assert evidence["version"]
    assert evidence["section_id"]
    assert evidence["excerpt"]


async def test_get_regulation_unsupported_query_returns_200_with_no_evidence(api_client):
    client, _service = api_client
    response = await client.get(
        "/v1/regulations",
        params={"query": "Does the tire degradation rule change or get ignored after lap 40?"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["found"] is False
    assert body["evidence"] == []
    # Spec v1.1 repair 14: an ordinary unsupported query against a working
    # corpus is `available=True` — see test_regulation_service.py for the
    # `available=False` (corpus unreachable) case, which needs a
    # RegulationService constructed with a missing directory and so isn't
    # reachable through this fixture's normally-configured app.
    assert body["available"] is True


async def test_get_regulation_requires_a_non_empty_query(api_client):
    client, _service = api_client
    response = await client.get("/v1/regulations", params={"query": "   "})
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"


async def test_get_regulation_does_not_require_auth(api_client):
    # This is a read-only lookup (like GET .../state or GET .../alerts),
    # not a mutating demo-control action, so it must NOT require
    # X-Demo-Access-Token — unlike /v1/demo/start|pause|reset.
    client, _service = api_client
    response = await client.get("/v1/regulations", params={"query": "What is an undercut?"})
    assert response.status_code == 200
