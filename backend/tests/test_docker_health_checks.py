"""Static assertions that both container images declare a real health
check, and that docker-compose.yml wires them up correctly.

Spec v1.1 pass 3, correction 6: "Add tests/static assertions confirming
both images have health checks." These tests read the Dockerfile/compose
source directly rather than building the images (this environment's
outbound network allowlist blocks the base-image pull — see
backend/Dockerfile's own module docstring), so they run anywhere, and
still catch a HEALTHCHECK being silently removed.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def _healthcheck_instruction(dockerfile: str) -> str:
    """Returns just the HEALTHCHECK instruction's own text (through its
    CMD clause), not the whole Dockerfile — so assertions about what the
    check DOES or DOESN'T reference aren't confused by unrelated prose
    elsewhere in the file (e.g. a comment mentioning "curl" while
    explaining why curl is deliberately not used)."""
    start = dockerfile.index("HEALTHCHECK")
    end = dockerfile.index("\n\n", start)
    return dockerfile[start : end if end != -1 else len(dockerfile)]


def test_backend_dockerfile_declares_a_healthcheck():
    dockerfile = _read("backend/Dockerfile")
    healthcheck = _healthcheck_instruction(dockerfile)
    assert "/health" in healthcheck
    # Uses the Python stdlib rather than curl/wget, neither of which is
    # installed in the slim base image.
    assert "urllib.request" in healthcheck
    assert "curl" not in healthcheck.lower()
    assert "wget" not in healthcheck.lower()


def test_mcp_server_dockerfile_declares_a_healthcheck():
    dockerfile = _read("mcp_server/Dockerfile")
    healthcheck = _healthcheck_instruction(dockerfile)
    # /ready (not just /health) so the check confirms backend reachability,
    # per the correction's explicit requirement.
    assert "/ready" in healthcheck
    assert "urllib.request" in healthcheck
    assert "curl" not in healthcheck.lower()
    assert "wget" not in healthcheck.lower()


def test_no_credentials_appear_in_either_healthcheck_command():
    for dockerfile_path in ("backend/Dockerfile", "mcp_server/Dockerfile"):
        dockerfile = _read(dockerfile_path)
        # Extract just the HEALTHCHECK instruction's CMD line(s) for a
        # narrower, more meaningful check than scanning the whole file.
        healthcheck_start = dockerfile.index("HEALTHCHECK")
        healthcheck_clause = dockerfile[healthcheck_start : healthcheck_start + 600]
        for forbidden in ("TOKEN", "SECRET", "PASSWORD", "Authorization"):
            assert forbidden not in healthcheck_clause, (
                f"{dockerfile_path}'s HEALTHCHECK must not reference "
                f"credentials ({forbidden} found)"
            )


def test_compose_file_declares_health_checks_for_both_services():
    compose = yaml.safe_load(_read("docker-compose.yml"))
    services = compose["services"]

    assert "backend" in services
    assert "healthcheck" in services["backend"], "backend service must declare a healthcheck"

    assert "mcp_server" in services
    assert "healthcheck" in services["mcp_server"], "mcp_server service must declare a healthcheck"


def test_mcp_server_depends_on_a_healthy_backend():
    compose = yaml.safe_load(_read("docker-compose.yml"))
    mcp_service = compose["services"]["mcp_server"]
    depends_on = mcp_service.get("depends_on")
    assert depends_on is not None, "mcp_server must declare depends_on: backend"

    # depends_on may be a short form (list of names) or long form (a dict
    # of name -> {condition: ...}) — assert on whichever form is used.
    if isinstance(depends_on, dict):
        assert "backend" in depends_on
        assert depends_on["backend"].get("condition") == "service_healthy", (
            "mcp_server must wait for the backend healthcheck to pass "
            "(condition: service_healthy), not merely for it to start"
        )
    else:
        raise AssertionError(
            "mcp_server's depends_on must use the long form with "
            "condition: service_healthy, not the short list form, so it "
            "actually waits on the backend's health check"
        )


def test_no_credentials_hardcoded_into_compose_environment_values():
    compose_source = _read("docker-compose.yml")
    # Every *_TOKEN/*_SECRET/*_PASSWORD environment entry must be sourced
    # from a shell/.env variable substitution (${...}), never a literal
    # value baked into this file — spec v1.1 repair 11's rule, re-checked
    # here now that mcp_server's own token env vars were added.
    import re

    for match in re.finditer(r"^\s*(\w*(?:TOKEN|SECRET|PASSWORD)\w*):\s*(.+)$", compose_source, re.MULTILINE):
        key, value = match.group(1), match.group(2).strip()
        assert value.startswith("${") or value == "", (
            f"docker-compose.yml sets {key} to a literal value ({value!r}) "
            "instead of a ${...} substitution — this would bake a fixed "
            "credential/fallback into the file"
        )
