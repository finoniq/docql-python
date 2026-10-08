"""Shared fixtures: the vendored contract kit and the stub URL."""

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import httpx
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


@pytest.fixture(scope="session")
def _kit_integrity() -> None:
    """Prove the served kit equals the vendored kit before any contract test runs (T-11-17).

    Three sha256 values must agree: the bytes ``GET /__stub/sdk-cases`` serves,
    the ``sdk-cases.json`` entry of the vendored manifest, and the vendored
    file itself. Any mismatch — or a non-200 — fails the session; this check
    can fail, never silently skip. Without a stub URL there is nothing to
    compare and ``stub_url`` owns the require-vs-skip decision.
    """
    base = os.environ.get("DOCQL_STUB_URL", "").rstrip("/")
    if not base:
        return
    response = httpx.get(f"{base}/__stub/sdk-cases")
    served = hashlib.sha256(response.content).hexdigest()
    manifest: dict[str, Any] = json.loads((ROOT / "contract" / "manifest.json").read_bytes())
    declared = manifest["files"]["sdk-cases.json"]
    vendored = hashlib.sha256((ROOT / "contract" / "sdk-cases.json").read_bytes()).hexdigest()
    if response.status_code != 200 or served != declared or served != vendored:
        pytest.fail(
            "CONTRACT_KIT_MISMATCH sdk-cases.json: served sha256 "
            f"{served} (HTTP {response.status_code}), manifest {declared}, vendored {vendored}"
        )


@pytest.fixture
def stub_url(_kit_integrity: None) -> str:
    """The running stub's base URL; fail loudly in CI, skip in a local run."""
    url = os.environ.get("DOCQL_STUB_URL")
    if url:
        return url
    if os.environ.get("DOCQL_REQUIRE_STUB") == "1":
        pytest.fail(
            "DOCQL_STUB_URL is not set: start the pinned stub with scripts/stub-up.sh --run"
        )
    pytest.skip("DOCQL_STUB_URL is not set: start the pinned stub with scripts/stub-up.sh --run")
