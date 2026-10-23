from __future__ import annotations

import pytest


def pytest_collection_modifyitems(config: pytest.Config, items: list) -> None:  # noqa: ARG001
    """Spec v1.1 pass 3, correction 10: this suite calls the Lambda handler
    function in-process with synthetic Alexa request payloads — fast,
    dependency-free — tagged `unit` uniformly."""
    for item in items:
        item.add_marker(pytest.mark.unit)
