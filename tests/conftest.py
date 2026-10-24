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
def job_cases() -> dict[str, Any]:
    """The vendored job case list (contract/job-cases.json, kit v3)."""
    cases: dict[str, Any] = json.loads((ROOT / "contract" / "job-cases.json").read_bytes())
    return cases


@pytest.fixture(scope="session")
def golden() -> dict[str, Any]:
    """The golden multipart fixtures (contract/golden.json and contract/golden/)."""
    fixtures: dict[str, Any] = json.loads((ROOT / "contract" / "golden.json").read_bytes())
    return fixtures


# Each served kit file: (vendored file name, the stub route that serves it).
KIT_ROUTES = (("sdk-cases.json", "/__stub/sdk-cases"), ("job-cases.json", "/__stub/job-cases"))


@pytest.fixture(scope="session")
def _kit_integrity() -> None:
    """Prove the served kit equals the vendored kit before any contract test runs (T-11-17).

    For each of ``sdk-cases.json`` (``GET /__stub/sdk-cases``) and
    ``job-cases.json`` (``GET /__stub/job-cases``) three sha256 values must
    agree: the bytes the stub serves, the file's entry in the vendored
    manifest, and the vendored file itself. Any mismatch — or a non-200 —
    fails the session; this check can fail, never silently skip. Without a
    stub URL there is nothing to compare and ``stub_url`` owns the
    require-vs-skip decision.
    """
    base = os.environ.get("DOCQL_STUB_URL", "").rstrip("/")
    if not base:
        return
    manifest: dict[str, Any] = json.loads((ROOT / "contract" / "manifest.json").read_bytes())
    for name, route in KIT_ROUTES:
        response = httpx.get(f"{base}{route}")
        served = hashlib.sha256(response.content).hexdigest()
        declared = manifest["files"].get(name)
        vendored = hashlib.sha256((ROOT / "contract" / name).read_bytes()).hexdigest()
        if response.status_code != 200 or served != declared or served != vendored:
            pytest.fail(
                f"CONTRACT_KIT_MISMATCH {name}: served sha256 "
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
