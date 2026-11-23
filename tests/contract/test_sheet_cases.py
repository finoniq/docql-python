"""Every kit v5 spreadsheet case on both routes against the digest-pinned stub (PY-07, SSDK-02).

``contract/sheet-cases.json`` lists 26 cases, each with ``routes`` ``["sync", "jobs"]``,
so this module runs 52 cases. ``sync`` calls ``query_document``; ``jobs`` calls
``submit_document`` and then ``wait``, and a refusal must raise from the submit call.

The SDK sends opaque bytes: the upload name goes out byte for byte (``"a.csv "`` keeps
its trailing space) and the SDK never checks a file's type, name or size. Whether a
file is refused, and whether a value equal to its limit passes, is the server's answer.

Before any request, a fixture's vendored bytes must match the sha256 the vendored
``contract/manifest.json`` lists for it, so a drifted fixture fails the case instead of
testing different bytes. For a 422 the error's ``metadata`` must hold every
``expect.metadata`` entry exactly as the kit states it (the SSDK-02 proof).
"""

import base64
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from docql import DocQL, DocQLError, QueryResult

pytestmark = pytest.mark.contract

ROOT = Path(__file__).resolve().parents[2]
KIT: dict[str, Any] = json.loads((ROOT / "contract" / "sheet-cases.json").read_bytes())
# sheet-cases.json boots the stub with the job-cases.json recipe and its keys.
API_KEY: str = json.loads((ROOT / "contract" / "job-cases.json").read_bytes())["keys"]["valid"]
KNOWN_ROUTES = ("sync", "jobs")
RUNS: list[tuple[dict[str, Any], str]] = [
    (case, route) for case in KIT["cases"] for route in case["routes"]
]


def test_every_case_uses_a_known_route() -> None:
    """A case with an unknown route must fail the run, never silently disappear."""
    unknown = [(c["id"], r) for c in KIT["cases"] for r in c["routes"] if r not in KNOWN_ROUTES]
    assert not unknown, f"sheet-cases.json carries unknown routes: {unknown}"


def fixture_bytes(file: dict[str, Any], root: Path = ROOT) -> bytes:
    """The upload bytes of a case; a fixture must match the vendored manifest first."""
    if "base64" in file:
        decoded: bytes = base64.b64decode(file["base64"])
        return decoded
    name: str = file["fixture"]
    data = (root / "contract" / name).read_bytes()
    manifest: dict[str, Any] = json.loads((root / "contract" / "manifest.json").read_bytes())
    declared = manifest["files"].get(name)
    actual = hashlib.sha256(data).hexdigest()
    if declared != actual:
        pytest.fail(f"CONTRACT_KIT_MISMATCH {name}: vendored sha256 {actual}, manifest {declared}")
    return data


def test_a_fixture_that_drifted_from_the_manifest_fails_before_any_request(
    tmp_path: Path,
) -> None:
    (tmp_path / "contract" / "sheets").mkdir(parents=True)
    (tmp_path / "contract" / "sheets" / "plain.csv").write_bytes(b"tampered\n")
    manifest = {"files": {"sheets/plain.csv": hashlib.sha256(b"original\n").hexdigest()}}
    (tmp_path / "contract" / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(pytest.fail.Exception, match="CONTRACT_KIT_MISMATCH sheets/plain.csv"):
        fixture_bytes({"name": "plain.csv", "fixture": "sheets/plain.csv"}, root=tmp_path)


def _send(client: DocQL, route: str, data: bytes, kwargs: dict[str, Any]) -> QueryResult:
    if route == "sync":
        return client.query_document(data, **kwargs)
    job = client.submit_document(data, **kwargs)
    job = client.wait(job.job_id)
    assert job.result is not None, f"job {job.job_id} ended {job.status} without a result"
    return job.result


@pytest.mark.parametrize(
    ("case", "route"), RUNS, ids=[f"{case['id']}-{route}" for case, route in RUNS]
)
def test_sheet_case(case: dict[str, Any], route: str, stub_url: str) -> None:
    request = case["request"]
    file = request["file"]
    data = fixture_bytes(file)
    # The name is sent verbatim: never stripped or normalised (SSDK-03).
    kwargs: dict[str, Any] = {"filename": file["name"]}
    if "query" in request:
        kwargs["query"] = request["query"]
    if "prompt" in request:
        kwargs["prompt"] = request["prompt"]
    if "mode" in request:
        kwargs["mode"] = request["mode"]
    expect = case["expect"]

    with DocQL(api_key=API_KEY, api_url=stub_url) as client:
        if expect["status"] == 200:
            result = _send(client, route, data, kwargs)
            assert result.page_count == expect["page_count"]
            assert result.pages_ocr == expect["pages_ocr"]
            assert result.metadata.get("file_mimetype") == expect["file_mimetype"]
            return

        if route == "sync":
            with pytest.raises(DocQLError) as excinfo:
                client.query_document(data, **kwargs)
        else:
            # A refusal on the jobs route raises from the submit call itself.
            with pytest.raises(DocQLError) as excinfo:
                client.submit_document(data, **kwargs)
    err = excinfo.value
    assert err.status == expect["status"]
    assert err.reason == expect["reason"]
    assert isinstance(err.metadata, dict)
    assert err.metadata.get("reason") == expect["reason"]
    for key, value in expect.get("metadata", {}).items():
        assert key in err.metadata, f"metadata has no {key!r}: {err.metadata}"
        assert err.metadata[key] == value
        assert type(err.metadata[key]) is type(value)
