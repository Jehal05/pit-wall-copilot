from __future__ import annotations

import pytest

# Spec v1.1 pass 3, correction 10: every test collected in this directory
# statically validates template.yaml (no real AWS account/deploy involved)
# — tagged `infrastructure` uniformly rather than requiring each test
# function to opt in individually.


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:  # noqa: ARG001
    for item in items:
        item.add_marker(pytest.mark.infrastructure)
