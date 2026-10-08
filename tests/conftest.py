"""Shared fixtures: the vendored contract kit and the stub URL."""

import json
import os
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def sdk_cases() -> dict[str, Any]:
    """The vendored shared case list (contract/sdk-cases.json)."""
    cases: dict[str, Any] = json.loads((ROOT / "contract" / "sdk-cases.json").read_bytes())
    return cases


@pytest.fixture(scope="session")
def golden() -> dict[str, Any]:
    """The golden multipart fixtures (contract/golden.json and contract/golden/)."""
    fixtures: dict[str, Any] = json.loads((ROOT / "contract" / "golden.json").read_bytes())
    return fixtures


@pytest.fixture
def stub_url() -> str:
    """The running stub's base URL; fail loudly in CI, skip in a local run."""
    url = os.environ.get("DOCQL_STUB_URL")
    if url:
        return url
    if os.environ.get("DOCQL_REQUIRE_STUB") == "1":
        pytest.fail(
            "DOCQL_STUB_URL is not set: start the pinned stub with scripts/stub-up.sh --run"
        )
    pytest.skip("DOCQL_STUB_URL is not set: start the pinned stub with scripts/stub-up.sh --run")
